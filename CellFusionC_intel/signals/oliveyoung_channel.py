"""
올리브영 국내 채널 랭킹 수집 (국내 최대 H&B 채널 = 서사의 '국내에서 잘 나간다' 고리).

원격 채널 인사이트 MCP(oliveyoung-review.vercel.app)가 스크래핑한 올리브영 실랭킹을
호출→브랜드 매칭→rival_intel.oliveyoung_rankings 에 일별 스냅샷 적재.

우리 리테일 신호는 그동안 아마존(해외)뿐 → 국내 최대 채널(올영)을 붙여
"해외(아마존) ↔ 국내(올영)" 대조 + 국내 순위 추세("누가 뜨고 지나") 확보.

- 데이터 출처: 원격 MCP get_market_rankings(8개 카테고리 × Top20).
- is_ours=true = 셀퓨전씨(자사) 상품. 경쟁 지형용으로 브랜드 매칭 별도 수행.
- 무자격증(원격 MCP가 무인증 응답) · Streamable HTTP.
수동 실행: python -m signals.oliveyoung_channel
"""

import os
import re
import json
import asyncio
import logging
from datetime import date

from sqlalchemy import text

from config.settings import DB_SCHEMA
from config.brands import ALL_BRANDS, BRAND_KO_NAMES
from storage.models import get_session

logger = logging.getLogger(__name__)

CHANNEL_MCP_URL = os.getenv("CHANNEL_MCP_URL", "https://oliveyoung-review.vercel.app/api/mcp")
CHANNEL_MCP_API_KEY = os.getenv("CHANNEL_MCP_API_KEY", "")

# 자사(셀퓨전씨) 식별 토큰
_OUR_TOKENS = ("셀퓨전씨", "셀퓨전C", "cellfusion")

# 올영 상품명에서 자주 쓰는 브랜드 표기(한국어명에 없는 영문 짧은 토큰 보강).
# 짧은 영문은 단어경계로만 매칭(오탐 방지).
_OY_ALIASES = {
    "VT Cosmetics": ["VT"], "Skin1004": ["skin1004"], "Dr.Jart+": ["닥터자르트"],
    "Roundlab": ["라운드랩"], "Numbuzin": ["넘버즈인"], "Torriden": ["토리든"],
    "Goodal": ["구달"], "Abib": ["아비브"], "Anua": ["아누아"],
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9가-힣]", "", (s or "").lower())


def _build_dict() -> dict:
    """브랜드 → [매칭 키워드]. 한국어명 우선 + 영문/보강 별칭."""
    d: dict = {}
    for b in ALL_BRANDS:
        keys = set(BRAND_KO_NAMES.get(b, []))    # 한국어 별칭(메디힐·조선미녀 등)
        keys.add(b)                              # 영문명 원형
        keys.update(_OY_ALIASES.get(b, []))      # 올영 표기 보강
        d[b] = [k for k in keys if k]
    return d


_BRAND_DICT = _build_dict()
_MONITORED = set(ALL_BRANDS)


def _match_brand(goods_name: str) -> "str | None":
    """상품명(한국어 마케팅 문자열)에서 모니터링 브랜드 매칭."""
    t = goods_name or ""
    n = _norm(t)
    for brand, keys in _BRAND_DICT.items():
        for k in keys:
            if not k:
                continue
            if re.search(r"[가-힣]", k):
                if k in t:                        # 한국어: 원문 부분일치
                    return brand
            elif len(k) <= 3:
                # 짧은 영문(VT 등): 단어경계 + 대소문자 무시, 오탐 방지
                if re.search(rf"(?<![A-Za-z]){re.escape(k)}(?![A-Za-z])", t, re.I):
                    return brand
            elif _norm(k) in n:                   # 긴 영문: 정규화 부분일치
                return brand
    return None


def _is_ours(goods_name: str, flag) -> bool:
    if flag is True:
        return True
    t = (goods_name or "").lower()
    return any(tok.lower() in t for tok in _OUR_TOKENS)


async def _call(sess, name: str, args: dict | None = None):
    r = await sess.call_tool(name, args or {})
    if not r.content:
        return None
    return json.loads(r.content[0].text)


async def _fetch() -> dict:
    """원격 MCP에서 랭킹(8카테고리) + 급변동 수집."""
    from mcp.client.streamable_http import streamablehttp_client
    from mcp import ClientSession

    headers: dict[str, str] = {}
    if CHANNEL_MCP_API_KEY:
        headers["Authorization"] = f"Bearer {CHANNEL_MCP_API_KEY}"

    out = {"rankings": [], "movers": [], "reviews": []}
    async with streamablehttp_client(CHANNEL_MCP_URL, headers=headers) as (r, w, _):
        async with ClientSession(r, w) as sess:
            await sess.initialize()
            out["rankings"] = await _call(sess, "get_market_rankings") or []
            out["movers"] = await _call(sess, "get_top_movers") or []
            # 카테고리별 경쟁사 리뷰 감성(평점·긍정/부정 키워드). 희소·주간 데이터.
            for cat in _REVIEW_CATS:
                try:
                    d = await _call(sess, "get_competitor_analysis", {"category": cat})
                    if isinstance(d, list):
                        out["reviews"].extend(d)
                except Exception:
                    continue
    return out


# 리뷰 감성 수집 대상 카테고리(선케어=간판 우선, 나머지는 데이터 있는 것만).
_REVIEW_CATS = ["선케어", "스킨케어", "마스크팩", "클렌징", "더모 코스메틱", "전체"]


def _ensure_table(session) -> None:
    session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.oliveyoung_rankings (
            id BIGSERIAL PRIMARY KEY,
            category VARCHAR(40) NOT NULL,
            rank_position INTEGER,
            prev_rank INTEGER,
            delta INTEGER,
            goods_no VARCHAR(40),
            goods_name VARCHAR(300),
            brand VARCHAR(100),
            is_monitored BOOLEAN DEFAULT FALSE,
            is_ours BOOLEAN DEFAULT FALSE,
            capture_date DATE NOT NULL,
            captured_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            UNIQUE(category, goods_no, capture_date)
        )
    """))
    session.execute(text(
        f"CREATE INDEX IF NOT EXISTS ix_oy_rank_date "
        f"ON {DB_SCHEMA}.oliveyoung_rankings (capture_date DESC, category)"))
    session.execute(text(
        f"CREATE INDEX IF NOT EXISTS ix_oy_brand_date "
        f"ON {DB_SCHEMA}.oliveyoung_rankings (brand, capture_date DESC)"))
    session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.oliveyoung_reviews (
            id BIGSERIAL PRIMARY KEY,
            week_start DATE,
            category VARCHAR(40) NOT NULL,
            goods_no VARCHAR(40) NOT NULL,
            goods_name VARCHAR(300),
            brand_name VARCHAR(120),
            brand VARCHAR(100),
            is_monitored BOOLEAN DEFAULT FALSE,
            is_ours BOOLEAN DEFAULT FALSE,
            rank_position INTEGER,
            review_count INTEGER,
            avg_score REAL,
            positive_keywords TEXT,
            negative_keywords TEXT,
            capture_date DATE NOT NULL,
            captured_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            UNIQUE(category, goods_no, week_start)
        )
    """))
    session.execute(text(
        f"CREATE INDEX IF NOT EXISTS ix_oy_rev_date "
        f"ON {DB_SCHEMA}.oliveyoung_reviews (capture_date DESC, category)"))
    session.commit()


def _kw_json(kws) -> str:
    """[{word,cnt}] → JSON 텍스트(상위 8, ensure_ascii=False)."""
    try:
        return json.dumps([{"word": k.get("word"), "cnt": k.get("cnt")}
                           for k in (kws or [])[:8]], ensure_ascii=False)
    except Exception:
        return "[]"


def _save_review(session, r: dict, cap: date) -> dict:
    gname = r.get("goods_name", "")
    brand = _match_brand(gname) or _match_brand(r.get("brand_name", ""))
    ours = _is_ours(gname, r.get("is_ours"))
    row = {
        "week_start": r.get("week_start"), "category": r.get("category_name", ""),
        "goods_no": r.get("goods_no", ""), "goods_name": gname[:300],
        "brand_name": (r.get("brand_name") or "")[:120], "brand": brand,
        "is_monitored": brand in _MONITORED, "is_ours": ours,
        "rank_position": r.get("rank_position"), "review_count": r.get("review_count"),
        "avg_score": r.get("avg_score"),
        "positive_keywords": _kw_json(r.get("positive_keywords")),
        "negative_keywords": _kw_json(r.get("negative_keywords")),
        "cap": cap,
    }
    session.execute(text(f"""
        INSERT INTO {DB_SCHEMA}.oliveyoung_reviews
            (week_start, category, goods_no, goods_name, brand_name, brand,
             is_monitored, is_ours, rank_position, review_count, avg_score,
             positive_keywords, negative_keywords, capture_date)
        VALUES (:week_start, :category, :goods_no, :goods_name, :brand_name, :brand,
                :is_monitored, :is_ours, :rank_position, :review_count, :avg_score,
                :positive_keywords, :negative_keywords, :cap)
        ON CONFLICT (category, goods_no, week_start) DO UPDATE SET
            goods_name = EXCLUDED.goods_name, brand_name = EXCLUDED.brand_name,
            brand = EXCLUDED.brand, is_monitored = EXCLUDED.is_monitored,
            is_ours = EXCLUDED.is_ours, rank_position = EXCLUDED.rank_position,
            review_count = EXCLUDED.review_count, avg_score = EXCLUDED.avg_score,
            positive_keywords = EXCLUDED.positive_keywords,
            negative_keywords = EXCLUDED.negative_keywords, captured_at = NOW()
    """), row)
    return row


def _save_ranking(session, category: str, e: dict, cap: date) -> dict:
    gname = e.get("goods_name", "")
    brand = _match_brand(gname)
    ours = _is_ours(gname, e.get("is_ours"))
    row = {
        "category": category, "rank_position": e.get("rank_position"),
        "prev_rank": e.get("prev_rank"), "delta": e.get("delta"),
        "goods_no": e.get("goods_no", ""), "goods_name": gname[:300],
        "brand": brand, "is_monitored": brand in _MONITORED,
        "is_ours": ours, "cap": cap,
    }
    session.execute(text(f"""
        INSERT INTO {DB_SCHEMA}.oliveyoung_rankings
            (category, rank_position, prev_rank, delta, goods_no, goods_name,
             brand, is_monitored, is_ours, capture_date)
        VALUES (:category, :rank_position, :prev_rank, :delta, :goods_no, :goods_name,
                :brand, :is_monitored, :is_ours, :cap)
        ON CONFLICT (category, goods_no, capture_date) DO UPDATE SET
            rank_position = EXCLUDED.rank_position, prev_rank = EXCLUDED.prev_rank,
            delta = EXCLUDED.delta, goods_name = EXCLUDED.goods_name,
            brand = EXCLUDED.brand, is_monitored = EXCLUDED.is_monitored,
            is_ours = EXCLUDED.is_ours, captured_at = NOW()
    """), row)
    return row


def run() -> dict:
    """올리브영 랭킹 수집·적재. 반환 {captured, monitored, ours, brands}."""
    cap = date.today()
    try:
        data = asyncio.run(_fetch())
    except Exception as e:
        logger.error("올영 MCP 호출 실패: %s", e)
        return {"captured": 0, "monitored": 0, "ours": 0, "brands": {}, "error": str(e)}

    captured = monitored = ours = reviews = 0
    brands: dict = {}
    session = get_session()
    try:
        _ensure_table(session)
        for cat in data["rankings"]:
            cname = cat.get("category_name", "")
            for e in cat.get("entries", []):
                row = _save_ranking(session, cname, e, cap)
                captured += 1
                if row["is_ours"]:
                    ours += 1
                if row["is_monitored"]:
                    monitored += 1
                    brands.setdefault(row["brand"], []).append(
                        (cname, row["rank_position"], row["delta"]))
            session.commit()
        # 경쟁사 리뷰 감성(평점·긍정/부정 키워드)
        seen_rev = set()
        for r in data.get("reviews", []):
            key = (r.get("category_name", ""), r.get("goods_no", ""), r.get("week_start"))
            if key in seen_rev or not r.get("goods_no"):
                continue
            seen_rev.add(key)
            _save_review(session, r, cap)
            reviews += 1
        session.commit()
        logger.info("올영 수집: 랭킹 %d건 · 모니터링 %d · 자사 %d · 브랜드 %d · 리뷰감성 %d건",
                    captured, monitored, ours, len(brands), reviews)
    finally:
        session.close()
    return {"captured": captured, "monitored": monitored, "ours": ours,
            "brands": brands, "reviews": reviews}


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(run())
