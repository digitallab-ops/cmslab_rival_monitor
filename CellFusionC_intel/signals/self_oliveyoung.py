"""
자사(셀퓨전씨) 올리브영 성과 수집 — '경쟁사 대비 우리 위치' 기준선.

원격 채널 MCP의 자사-중심 툴(get_product_stats·get_insights·get_stats)로
셀퓨전씨 제품별 올영 리뷰수·평점·재구매율 + 긍/부정 키워드를 적재.
경쟁사 집계와 분리된 '자사 위치' 대시보드용.

수동 실행: python -m signals.self_oliveyoung
"""

import os
import json
import asyncio
import logging
from datetime import date

from sqlalchemy import text

from config.settings import DB_SCHEMA
from storage.models import get_session

logger = logging.getLogger(__name__)

CHANNEL_MCP_URL = os.getenv("CHANNEL_MCP_URL", "https://oliveyoung-review.vercel.app/api/mcp")
CHANNEL_MCP_API_KEY = os.getenv("CHANNEL_MCP_API_KEY", "")

_SELF_TOKENS = ("셀퓨전", "cellfusion")


async def _fetch() -> dict:
    from mcp.client.streamable_http import streamablehttp_client
    from mcp import ClientSession
    headers = {"Authorization": f"Bearer {CHANNEL_MCP_API_KEY}"} if CHANNEL_MCP_API_KEY else {}
    out = {"products": [], "insights": {}, "stats": {}}

    async def _call(sess, name, args=None):
        r = await sess.call_tool(name, args or {})
        return json.loads(r.content[0].text) if r.content else None

    async with streamablehttp_client(CHANNEL_MCP_URL, headers=headers) as (r, w, _):
        async with ClientSession(r, w) as sess:
            await sess.initialize()
            out["products"] = await _call(sess, "get_product_stats") or []
            out["insights"] = await _call(sess, "get_insights") or {}
            out["stats"] = await _call(sess, "get_stats") or {}
    return out


def _is_self(name: str) -> bool:
    t = (name or "").lower()
    return any(tok in t for tok in _SELF_TOKENS)


def _ensure_tables(session) -> None:
    session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.self_channel_products (
            goods_no VARCHAR(40) PRIMARY KEY,
            product_name VARCHAR(300),
            review_cnt INTEGER,
            avg_score REAL,
            repurchase_pct REAL,
            five_star_cnt INTEGER,
            first_seen VARCHAR(20),
            capture_date DATE,
            captured_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """))
    session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.self_channel_meta (
            id INTEGER PRIMARY KEY DEFAULT 1,
            payload TEXT,
            capture_date DATE,
            captured_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """))
    session.commit()


def _num(v, cast=float):
    try:
        return cast(v)
    except (TypeError, ValueError):
        return None


def run() -> dict:
    cap = date.today()
    try:
        data = asyncio.run(_fetch())
    except Exception as e:
        logger.error("자사 올영 MCP 호출 실패: %s", e)
        return {"products": 0, "error": str(e)}

    session = get_session()
    prod_n = 0
    try:
        _ensure_tables(session)
        for p in data.get("products", []):
            name = p.get("goods_name", "")
            if not _is_self(name):
                continue
            session.execute(text(f"""
                INSERT INTO {DB_SCHEMA}.self_channel_products
                    (goods_no, product_name, review_cnt, avg_score, repurchase_pct,
                     five_star_cnt, first_seen, capture_date)
                VALUES (:gn, :nm, :rc, :sc, :rp, :fs, :fseen, :cap)
                ON CONFLICT (goods_no) DO UPDATE SET
                    product_name=EXCLUDED.product_name, review_cnt=EXCLUDED.review_cnt,
                    avg_score=EXCLUDED.avg_score, repurchase_pct=EXCLUDED.repurchase_pct,
                    five_star_cnt=EXCLUDED.five_star_cnt, capture_date=EXCLUDED.capture_date,
                    captured_at=NOW()
            """), {"gn": p.get("goods_no", ""), "nm": name[:300],
                   "rc": _num(p.get("review_cnt"), int), "sc": _num(p.get("avg_score")),
                   "rp": _num(p.get("repurchase_pct")), "fs": _num(p.get("five_star_cnt"), int),
                   "fseen": p.get("first_seen"), "cap": cap})
            prod_n += 1
        # 자사 키워드·총계 메타(긍/부정 키워드 상위, 전체 통계) 저장
        ins = data.get("insights", {})
        meta = {
            "pos": [k.get("word") for k in (ins.get("positive_keywords") or [])[:8]],
            "neg": [k.get("word") for k in (ins.get("negative_keywords") or [])[:8]],
        }
        session.execute(text(f"""
            INSERT INTO {DB_SCHEMA}.self_channel_meta (id, payload, capture_date)
            VALUES (1, :p, :cap)
            ON CONFLICT (id) DO UPDATE SET payload=EXCLUDED.payload,
                capture_date=EXCLUDED.capture_date, captured_at=NOW()
        """), {"p": json.dumps(meta, ensure_ascii=False), "cap": cap})
        session.commit()
        logger.info("자사 올영 성과 수집: 제품 %d · 긍정키워드 %d · 부정키워드 %d",
                    prod_n, len(meta["pos"]), len(meta["neg"]))
        return {"products": prod_n, "pos": len(meta["pos"]), "neg": len(meta["neg"])}
    finally:
        session.close()


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(run())
