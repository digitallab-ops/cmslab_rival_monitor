"""
네이버 데이터랩 검색어 트렌드 수집 (수요 신호).

뉴스(공급/PR)와 대조할 '실제 검색 수요' 시계열. 브랜드별 + 셀퓨전씨 핵심 성분/
카테고리 키워드의 주간 검색지수(0~100, 상대값)를 search_trends 테이블에 적재.

자격증명: 기존 NAVER_CLIENT_ID/SECRET 재사용. 단 네이버 개발자센터에서 해당 앱에
'데이터랩(검색어트렌드)' API를 추가해야 함(뉴스검색만 켜져 있으면 401 scope invalid).

주의: 데이터랩 검색지수는 '한 요청 내 키워드 그룹들 간 상대값'. 브랜드 간 비교는
같은 요청(최대 5그룹)에 넣어야 유효. 같은 브랜드의 시간 추이(모멘텀)는 항상 유효.
"""

import json
import logging
from datetime import datetime, timedelta

import requests
from sqlalchemy import text

from config.settings import DB_SCHEMA
from config.brands import ALL_BRANDS, BRAND_KO_NAMES
from storage.models import get_session

logger = logging.getLogger(__name__)

# 네이버가 검색어트렌드를 NAVER API HUB로 이관 중 → 두 방식 모두 지원.
#  - HUB(신규): NAVER_HUB_KEY_ID/NAVER_HUB_KEY 설정 시. NCP 엔드포인트 + X-NCP-APIGW 헤더.
#  - 레거시: 기존 개발자센터 NAVER_CLIENT_ID/SECRET + X-Naver-Client 헤더(데이터랩 스코프 필요).
_URL_LEGACY = "https://openapi.naver.com/v1/datalab/search"
# NAVER API HUB(신규): 호스트/경로가 구 NCP(naveropenapi/datalab)와 다름 — 실측 확인.
_URL_HUB = "https://naverapihub.apigw.ntruss.com/search-trend/v1/search"

# 셀퓨전씨(더마 선케어) 핵심 성분·카테고리 트렌드 키워드
INGREDIENT_TERMS = {
    "선크림":   ["선크림", "선스크린"],
    "톤업선크림": ["톤업선크림", "톤업선크림추천"],
    "선스틱":   ["선스틱"],
    "PDRN":    ["PDRN", "연어핵산"],
    "엑소좀":   ["엑소좀"],
    "시카":    ["시카", "병풀"],
    "리들샷":   ["리들샷"],
    "콜라겐":   ["콜라겐앰플", "콜라겐"],
}


def _endpoint_and_headers() -> tuple[str, dict]:
    import os
    hub_id = os.getenv("NAVER_HUB_KEY_ID", "").strip()
    hub_key = os.getenv("NAVER_HUB_KEY", "").strip()
    if hub_id and hub_key:
        return _URL_HUB, {
            "X-NCP-APIGW-API-KEY-ID": hub_id,
            "X-NCP-APIGW-API-KEY": hub_key,
            "Content-Type": "application/json",
        }
    return _URL_LEGACY, {
        "X-Naver-Client-Id": os.getenv("NAVER_CLIENT_ID", ""),
        "X-Naver-Client-Secret": os.getenv("NAVER_CLIENT_SECRET", ""),
        "Content-Type": "application/json",
    }


def _brand_keyword_groups() -> list[dict]:
    """브랜드별 검색어 그룹 (한국어명 + 영문명)."""
    groups = []
    for b in ALL_BRANDS:
        kws = list(BRAND_KO_NAMES.get(b, []))
        if b not in kws:
            kws.append(b)                  # 영문 브랜드명도 키워드로
        groups.append({"groupName": b, "keywords": kws[:20], "kind": "brand"})
    return groups


def _ingredient_keyword_groups() -> list[dict]:
    return [{"groupName": name, "keywords": kws[:20], "kind": "ingredient"}
            for name, kws in INGREDIENT_TERMS.items()]


def _call_datalab(groups: list[dict], start: str, end: str, time_unit: str = "week") -> list[dict]:
    """최대 5그룹씩 요청. 반환: [{groupName, kind, data:[{period, ratio}]}]."""
    out = []
    url, headers = _endpoint_and_headers()
    for i in range(0, len(groups), 5):
        batch = groups[i:i + 5]
        body = {
            "startDate": start, "endDate": end, "timeUnit": time_unit,
            "keywordGroups": [{"groupName": g["groupName"], "keywords": g["keywords"][:5]} for g in batch],
        }
        try:
            r = requests.post(url, headers=headers, data=json.dumps(body), timeout=15)
            if r.status_code != 200:
                logger.warning("데이터랩 오류 %s: %s", r.status_code, r.text[:150])
                continue
            kind_by_name = {g["groupName"]: g["kind"] for g in batch}
            for res in r.json().get("results", []):
                out.append({
                    "groupName": res["title"],
                    "kind": kind_by_name.get(res["title"], "brand"),
                    "data": res.get("data", []),
                })
        except Exception as e:
            logger.warning("데이터랩 요청 실패: %s", e)
    return out


def _ensure_table(session) -> None:
    session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.search_trends (
            id BIGSERIAL PRIMARY KEY,
            source VARCHAR(20) NOT NULL,      -- 'search' | 'shopping'
            kind VARCHAR(20),                 -- 'brand' | 'ingredient'
            term VARCHAR(120) NOT NULL,       -- 그룹명(브랜드/성분)
            brand VARCHAR(100),               -- 브랜드면 매핑, 아니면 NULL
            period DATE NOT NULL,
            ratio FLOAT,
            fetched_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            UNIQUE(source, term, period)
        )
    """))
    session.execute(text(
        f"CREATE INDEX IF NOT EXISTS ix_search_trends_term_period "
        f"ON {DB_SCHEMA}.search_trends (term, period DESC)"
    ))
    session.commit()


def _save(session, source: str, results: list[dict]) -> int:
    brand_set = set(ALL_BRANDS)
    n = 0
    for res in results:
        term = res["groupName"]
        kind = res["kind"]
        brand = term if term in brand_set else None
        for pt in res["data"]:
            session.execute(text(f"""
                INSERT INTO {DB_SCHEMA}.search_trends (source, kind, term, brand, period, ratio)
                VALUES (:s, :k, :t, :b, :p, :r)
                ON CONFLICT (source, term, period) DO UPDATE SET ratio = EXCLUDED.ratio, fetched_at = NOW()
            """), {"s": source, "k": kind, "t": term, "b": brand,
                   "p": pt["period"], "r": pt.get("ratio")})
            n += 1
    session.commit()
    return n


def run(days: int = 120) -> dict:
    """브랜드 + 성분 검색 트렌드 수집·저장. 반환: {groups, rows}."""
    end = datetime.utcnow().date()
    start = end - timedelta(days=days)
    groups = _brand_keyword_groups() + _ingredient_keyword_groups()
    results = _call_datalab(groups, start.isoformat(), end.isoformat(), "week")
    if not results:
        logger.warning("데이터랩 검색 트렌드: 결과 없음(스코프 미설정 가능)")
        return {"groups": 0, "rows": 0}
    session = get_session()
    try:
        _ensure_table(session)
        rows = _save(session, "search", results)
    finally:
        session.close()
    logger.info("네이버 검색 트렌드 저장: 그룹 %d, 행 %d", len(results), rows)
    return {"groups": len(results), "rows": rows}


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    logging.basicConfig(level=logging.INFO)
    print(run(days=90))
