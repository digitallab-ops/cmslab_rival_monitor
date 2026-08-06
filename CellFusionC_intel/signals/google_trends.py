"""
구글 트렌드 검색 수요 수집 (글로벌 수요 신호) + 급등 감지.

네이버(국내 한정)의 한계를 보완 — 해외 시장(글로벌·미국·일본) 검색 관심도를
일 단위로 수집. 수출 데이터와 삼각검증: 시장별 검색 급등 → 수출 증가 → 진출.

비공식 pytrends 사용 → 구글이 429로 자주 막음. 우회/완화:
  - 브라우저 User-Agent 필수(없으면 즉시 429).
  - pytrends 내장 retries 옵션은 urllib3 최신과 충돌 → 사용 안 함(수동 백오프).
  - 페이로드 간 sleep + 429 시 1회 재시도. 실패해도 나머지 진행(비파괴).

자격증명 불필요(무료·비공식). 불안정하므로 실패는 스킵하고 로그만 남긴다.
"""

import time
import logging
from datetime import datetime

from sqlalchemy import text

from config.settings import DB_SCHEMA
from storage.models import get_session
from storage.repository import get_active_brand_names

logger = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# geo 코드 → 저장 라벨. ''=전세계. 핵심 수출시장 US·JP 우선(호출수·429 위험 관리).
GEOS = {"": "GLOBAL", "US": "US", "JP": "JP"}

# 브랜드 → 구글 검색어(영문). 특수문자/모호어 정리.
GT_TERMS: dict[str, str] = {
    "Dr.Jart+": "Dr.Jart", "VT Cosmetics": "VT Cosmetics", "Cos de Baha": "Cos De Baha",
    "By Wishtrend": "Wishtrend", "Beauty of Joseon": "Beauty of Joseon",
}
_PAYLOAD_SLEEP = 8          # 페이로드 간 대기(초) — 429 완화(길수록 커버리지↑)
_RETRY_SLEEP = 30           # 429 시 재시도 대기


def _term(brand: str) -> str:
    return GT_TERMS.get(brand, brand)


def _client():
    from pytrends.request import TrendReq
    return TrendReq(hl="en-US", tz=540, requests_args={"headers": {"User-Agent": _UA}})


def _fetch_batch(terms: list[str], geo: str) -> "list[tuple]":
    """[(term, date, ratio, is_partial)] — 최대 5개 term, 실패 시 []."""
    from pytrends.request import TrendReq  # noqa: F401 (지연 임포트)
    for attempt in range(2):
        try:
            pt = _client()
            pt.build_payload(terms, timeframe="today 3-m", geo=geo)
            df = pt.interest_over_time()
            if df is None or df.empty:
                return []
            partial = df.get("isPartial")
            out = []
            for dt, row in df.iterrows():
                d = dt.date()
                is_p = bool(partial.loc[dt]) if partial is not None else False
                for t in terms:
                    if t in row:
                        out.append((t, d, float(row[t]), is_p))
            return out
        except Exception as e:
            if "429" in str(e) and attempt == 0:
                logger.info("구글 트렌드 429 — %d초 후 재시도(%s)", _RETRY_SLEEP, geo)
                time.sleep(_RETRY_SLEEP)
                continue
            logger.warning("구글 트렌드 실패 geo=%s terms=%s: %s", geo, terms[:2], str(e)[:100])
            return []
    return []


def _ensure_table(session) -> None:
    session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.google_trends (
            id BIGSERIAL PRIMARY KEY,
            term VARCHAR(120) NOT NULL,
            brand VARCHAR(100),
            geo VARCHAR(10) NOT NULL,        -- GLOBAL | US | JP
            period DATE NOT NULL,
            ratio FLOAT,
            is_partial BOOLEAN DEFAULT FALSE,
            fetched_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            UNIQUE(term, geo, period)
        )
    """))
    session.execute(text(
        f"CREATE INDEX IF NOT EXISTS ix_google_trends_term_geo "
        f"ON {DB_SCHEMA}.google_trends (term, geo, period DESC)"
    ))
    session.commit()


def _save(session, term: str, brand: str, geo_label: str, rows: list) -> int:
    n = 0
    for t, d, ratio, is_p in rows:
        if t != term:
            continue
        session.execute(text(f"""
            INSERT INTO {DB_SCHEMA}.google_trends (term, brand, geo, period, ratio, is_partial)
            VALUES (:t, :b, :g, :p, :r, :ip)
            ON CONFLICT (term, geo, period) DO UPDATE SET
                ratio = EXCLUDED.ratio, is_partial = EXCLUDED.is_partial, fetched_at = NOW()
        """), {"t": term, "b": brand, "g": geo_label, "p": d, "r": ratio, "ip": is_p})
        n += 1
    return n


def run() -> dict:
    """브랜드 글로벌·US·JP 일별 검색 관심도 수집. 반환 {geos, rows, failed}."""
    term_by_brand = {_term(b): b for b in get_active_brand_names()}
    terms = list(term_by_brand.keys())
    rows_total, failed = 0, 0
    session = get_session()
    try:
        _ensure_table(session)
        for geo, label in GEOS.items():
            for i in range(0, len(terms), 5):
                batch = terms[i:i + 5]
                data = _fetch_batch(batch, geo)
                if not data:
                    failed += 1
                else:
                    for t in batch:
                        rows_total += _save(session, t, term_by_brand.get(t, t), label, data)
                    session.commit()
                time.sleep(_PAYLOAD_SLEEP)
            logger.info("구글 트렌드 %s 수집 완료", label)
    finally:
        session.close()
    logger.info("구글 트렌드 저장: 행 %d (실패 배치 %d)", rows_total, failed)
    return {"geos": list(GEOS.values()), "rows": rows_total, "failed": failed}


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    logging.basicConfig(level=logging.INFO)
    print(run())
