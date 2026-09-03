"""
제품 전성분 인텔 — 신호로 포착된 핵심 제품의 전성분 → 효능·피부타입·우리 대응각.

"신제품 나왔다"에서 "뭐가 들었고 뭐가 좋은지, 그래서 우리가 뭘"로.
- A: 올리브영 전성분(원격 스크래퍼 MCP get_ingredients, goods_no 기반).
- B: (선택) 아마존 전성분.
- C: 전성분 소스가 없으면 LLM 추정(is_estimated=True, '추정' 표기).
LLM(gpt-4o-mini)으로 전성분/제품명 → {핵심성분·효능·피부타입·셀퓨전씨 대응각} 배치 요약.

주기 수집(주 1회 권장): python -m signals.ingredient_intel
규격: docs/ingredient_scraper_contract.md
"""

import os
import json
import asyncio
import logging

from sqlalchemy import text

from config.settings import DB_SCHEMA
from storage.models import get_session

logger = logging.getLogger(__name__)

CHANNEL_MCP_URL = os.getenv("CHANNEL_MCP_URL", "https://oliveyoung-review.vercel.app/api/mcp")
CHANNEL_MCP_API_KEY = os.getenv("CHANNEL_MCP_API_KEY", "")

_MAX_PRODUCTS = 15          # 회당 인리치 상한(LLM 비용 관리)


def _ensure_table(session) -> None:
    session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.product_ingredients (
            id BIGSERIAL PRIMARY KEY,
            goods_no VARCHAR(40),
            brand VARCHAR(100),
            product_name VARCHAR(300),
            category VARCHAR(40),
            ingredients_raw TEXT,
            key_ingredients TEXT,
            benefits TEXT,
            skin_types TEXT,
            our_angle TEXT,
            source VARCHAR(20),          -- oliveyoung | amazon | estimated
            is_estimated BOOLEAN DEFAULT FALSE,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            UNIQUE(goods_no)
        )
    """))
    session.execute(text(
        f"CREATE INDEX IF NOT EXISTS ix_prod_ing_brand "
        f"ON {DB_SCHEMA}.product_ingredients (brand, updated_at DESC)"))
    session.commit()


async def _get_ingredients(goods_no: str) -> dict:
    """원격 스크래퍼 MCP의 get_ingredients 호출. 툴 없거나 실패 시 {available:False}."""
    try:
        from mcp.client.streamable_http import streamablehttp_client
        from mcp import ClientSession
        headers = {"Authorization": f"Bearer {CHANNEL_MCP_API_KEY}"} if CHANNEL_MCP_API_KEY else {}
        async def _call():
            async with streamablehttp_client(CHANNEL_MCP_URL, headers=headers) as (r, w, _):
                async with ClientSession(r, w) as sess:
                    await sess.initialize()
                    res = await sess.call_tool("get_ingredients", {"goods_no": goods_no})
                    return json.loads(res.content[0].text) if res.content else {}
        d = await asyncio.wait_for(_call(), timeout=20)
        if isinstance(d, dict):
            return d
    except Exception as e:
        # 툴 미구현(A 미배포) 등 — 조용히 C로 폴백
        logger.debug("get_ingredients 미가용(%s): %s", goods_no, e)
    return {"available": False, "ingredients_raw": ""}


def _target_products(session) -> list[dict]:
    """인리치 대상 — 최신 올영 스냅샷의 모니터링 브랜드 제품(goods_no 보유)."""
    try:
        cap = session.execute(text(
            f"SELECT MAX(capture_date) FROM {DB_SCHEMA}.oliveyoung_rankings")).scalar()
        if not cap:
            return []
        rows = session.execute(text(f"""
            SELECT DISTINCT ON (goods_no) goods_no, brand, goods_name, category
            FROM {DB_SCHEMA}.oliveyoung_rankings
            WHERE capture_date = :cap AND is_monitored
              AND goods_no IS NOT NULL AND goods_no <> ''
            ORDER BY goods_no, rank_position ASC
            LIMIT :lim
        """), {"cap": cap, "lim": _MAX_PRODUCTS}).fetchall()
    except Exception:
        return []
    return [{"goods_no": r[0], "brand": r[1], "product_name": r[2] or "", "category": r[3] or ""}
            for r in rows]


def _enrich_llm(products: list[dict]) -> dict:
    """전성분/제품명 → {핵심성분·효능·피부타입·대응각} 배치 요약(1 LLM 콜).

    products: [{i, brand, product_name, ingredients_raw(빈값이면 추정)}].
    반환: {i: {key_ingredients, benefits, skin_types, our_angle}}.
    """
    if not products:
        return {}
    lines = []
    for p in products:
        raw = p.get("ingredients_raw", "")
        tag = f"전성분: {raw[:400]}" if raw else "전성분: (없음 — 제품명/브랜드로 추정)"
        lines.append(f"[{p['i']}] {p['brand']} · {p['product_name'][:60]} | {tag}")
    prompt = f"""당신은 씨엠에스랩(더마 선케어 '셀퓨전씨')의 제품·성분 분석가입니다.
아래 경쟁 제품들의 전성분(또는 제품명)을 보고, 각 제품에 대해 JSON으로 요약하세요.

{chr(10).join(lines)}

각 제품마다:
- key_ingredients: 핵심 기능성 성분 3~5개(쉼표, 물·부형제 제외. 예: "PDRN, 나이아신아마이드, 판테놀")
- benefits: 그 성분들이 피부에 주는 효능 한 줄(예: "재생·장벽강화·미백")
- skin_types: 어울리는 피부타입(예: "민감성·건성")
- our_angle: 셀퓨전씨(더마·선케어·PDRN·시카·배리어) 관점의 대응각 한 줄
  (예: "우리 PDRN 라인으로 재생 소구 맞대응 고려"). 조언형 어미.
전성분이 '(없음)'인 항목은 제품명·브랜드로 **추정**하되 과장 금지.

반드시 JSON만: {{"items": [{{"i":0,"key_ingredients":"...","benefits":"...","skin_types":"...","our_angle":"..."}}, ...]}} — 모든 인덱스 포함."""
    try:
        from openai import OpenAI
        from config.settings import INSIGHT_MODEL_MARKET
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        resp = client.chat.completions.create(
            model=INSIGHT_MODEL_MARKET, max_tokens=1400, temperature=0.4,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        out = {}
        for it in data.get("items", []):
            i = it.get("i")
            if isinstance(i, int):
                out[i] = {k: (it.get(k) or "").strip()
                          for k in ("key_ingredients", "benefits", "skin_types", "our_angle")}
        return out
    except Exception as e:
        logger.warning("전성분 LLM 요약 실패: %s", e)
        return {}


def _save(session, p: dict) -> None:
    session.execute(text(f"""
        INSERT INTO {DB_SCHEMA}.product_ingredients
            (goods_no, brand, product_name, category, ingredients_raw,
             key_ingredients, benefits, skin_types, our_angle, source, is_estimated, updated_at)
        VALUES (:goods_no, :brand, :product_name, :category, :ingredients_raw,
                :key_ingredients, :benefits, :skin_types, :our_angle, :source, :is_estimated, NOW())
        ON CONFLICT (goods_no) DO UPDATE SET
            ingredients_raw = EXCLUDED.ingredients_raw, key_ingredients = EXCLUDED.key_ingredients,
            benefits = EXCLUDED.benefits, skin_types = EXCLUDED.skin_types,
            our_angle = EXCLUDED.our_angle, source = EXCLUDED.source,
            is_estimated = EXCLUDED.is_estimated, updated_at = NOW()
    """), p)


def run() -> dict:
    """전성분 인텔 수집·요약·저장. 반환 {enriched, real, estimated}."""
    session = get_session()
    real = estimated = 0
    try:
        _ensure_table(session)
        targets = _target_products(session)
        if not targets:
            logger.info("전성분 인텔: 대상 제품 없음(올영 랭킹 수집 후)")
            return {"enriched": 0, "real": 0, "estimated": 0}
        # A/B: 전성분 fetch (툴 있으면 실제, 없으면 빈값)
        for t in targets:
            d = asyncio.run(_get_ingredients(t["goods_no"]))
            raw = (d.get("ingredients_raw") or "") if d.get("available") else ""
            t["ingredients_raw"] = raw
            t["source"] = d.get("source", "oliveyoung") if raw else "estimated"
            t["is_estimated"] = not bool(raw)
        # LLM 요약(배치)
        for i, t in enumerate(targets):
            t["i"] = i
        enr = _enrich_llm(targets)
        for t in targets:
            e = enr.get(t["i"], {})
            _save(session, {
                "goods_no": t["goods_no"], "brand": t["brand"],
                "product_name": t["product_name"][:300], "category": t["category"],
                "ingredients_raw": t["ingredients_raw"], "source": t["source"],
                "is_estimated": t["is_estimated"],
                "key_ingredients": e.get("key_ingredients", ""), "benefits": e.get("benefits", ""),
                "skin_types": e.get("skin_types", ""), "our_angle": e.get("our_angle", ""),
            })
            if t["is_estimated"]:
                estimated += 1
            else:
                real += 1
        session.commit()
        logger.info("전성분 인텔: %d개(실측 %d · 추정 %d)", len(targets), real, estimated)
        return {"enriched": len(targets), "real": real, "estimated": estimated}
    finally:
        session.close()


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(run())
