"""
EUIPO(유럽연합지식재산청) 상표 출원 수집 (EU 진출 선행신호).

KIPRIS(US·JP)를 보완 — EU 상표(EUTM) 하나 = EU 27개국 커버. 화장품(NICE 3류)에서
경쟁 브랜드명이 들어간 상표를 검색해 rival_intel.trademark_filings(country='EU')에 적재.
KIPRIS 테이블/분석/대시보드를 그대로 공유(같은 스키마).

자격증명: .env EUIPO_KEY / EUIPO_SECRET (dev.euipo.europa.eu, OAuth2 client_credentials).
  토큰: euipo.europa.eu/cas-server-webapp/oidc/accessToken (grant=client_credentials, scope=uid)
  검색: GET api.euipo.europa.eu/trademark-search/trademarks (헤더 Bearer + X-IBM-Client-Id)
  쿼리(RSQL): niceClasses=all=(3) and wordMarkSpecification.verbalElement==*TERM*

한계: client_credentials로는 출원인명이 마스킹될 수 있음 → 상표명(verbalElement)으로 검색·매칭.
      is_own은 출원인명 있으면 매칭, 없으면 상표명 일치를 신뢰(EU 상표 스쿼팅은 드묾).
"""

import os
import time
import logging
from datetime import date

import requests
from sqlalchemy import text

from config.settings import DB_SCHEMA
from storage.models import get_session
from storage.repository import get_active_brand_names
from signals.trademark import (
    SEARCH_TERMS, _is_own, _ensure_table, _nice_has_cosmetic,  # 재사용
)

logger = logging.getLogger(__name__)

_TOKEN_URL = "https://euipo.europa.eu/cas-server-webapp/oidc/accessToken"
_SEARCH_URL = "https://api.euipo.europa.eu/trademark-search/trademarks"
_PAGE_SLEEP = 0.4


def _key() -> str:
    return os.getenv("EUIPO_KEY", "").strip()


def _secret() -> str:
    return os.getenv("EUIPO_SECRET", "").strip()


def _token() -> "str | None":
    try:
        r = requests.post(_TOKEN_URL, data={
            "grant_type": "client_credentials",
            "client_id": _key(), "client_secret": _secret(), "scope": "uid",
        }, headers={"Accept": "application/json"}, timeout=20)
        r.raise_for_status()
        return r.json().get("access_token")
    except Exception as e:
        logger.warning("EUIPO 토큰 발급 실패: %s", e)
        return None


def _parse_iso(s: str) -> "date | None":
    try:
        return date.fromisoformat((s or "")[:10])
    except (ValueError, TypeError):
        return None


def _search(term: str, token: str) -> list[dict]:
    """화장품(3류) EU상표 중 상표명에 term 포함. 최신순 최대 100건."""
    headers = {"Authorization": f"Bearer {token}", "X-IBM-Client-Id": _key(),
               "Accept": "application/json"}
    # RSQL은 URL 인코딩 불필요(스펙) — requests가 인코딩해도 서버가 처리
    q = f'niceClasses=all=(3) and wordMarkSpecification.verbalElement==*{term}*'
    params = {"query": q, "size": 100, "page": 0, "sort": "applicationDate:desc"}
    r = requests.get(_SEARCH_URL, headers=headers, params=params, timeout=30)
    if r.status_code == 403:
        raise PermissionError("EUIPO 403 — 구독 승인 대기 또는 권한 없음")
    r.raise_for_status()
    return r.json().get("trademarks", [])


def _rec_from_item(brand: str, it: dict) -> "dict | None":
    """EUIPO 검색결과 item → trademark_filings 저장용 dict."""
    app_no = (it.get("applicationNumber") or "").strip()
    if not app_no:
        return None
    verbal = ((it.get("wordMarkSpecification") or {}).get("verbalElement") or "").strip()
    apps = it.get("applicants") or []
    applicant = ""
    for a in apps:
        if a.get("name"):
            applicant = a["name"].strip()
            break
    # is_own 판정.
    # 출원인명이 없다고 상표명 앞 4글자로 자기출원을 단정하면 안 된다. 그 규칙을 실제
    # 상표 308건에 적용하면 80건이 타사인데 자기출원이 된다 — 'GOODALL'(ERIKS N.V.),
    # 'DALBA BEAUTY'(중국 무역회사), 'BEAUTY OF JOSEON'(개인 출원) 같은 **스쿼터의
    # 선점 출원**이 대표적이다. 이 모듈의 목적이 'EU 진출 선행신호'인데 스쿼터의
    # 선점을 브랜드 자신의 진출로 정반대로 보고하게 된다.
    # 출원인을 모르면 모르는 채로 둔다(False) — 틀린 신호보다 빈 신호가 낫다.
    term = SEARCH_TERMS.get(brand, brand).upper().strip()
    if applicant:
        own = _is_own(brand, applicant)
    else:
        own = verbal.upper().strip() == term      # 상표명이 브랜드와 정확히 같을 때만
    return {
        "brand": brand, "country": "EU",
        "mark": verbal[:200], "applicant": applicant[:200],
        "right_holder": "",
        "app_number": app_no[:40],
        "app_date": _parse_iso(it.get("applicationDate")),
        "reg_date": _parse_iso(it.get("registrationDate")),
        "nice_code": "003", "cls_code": "003",   # 쿼리로 3류만 조회 → 화장품 확정
        "is_cosmetic": True, "is_own": own,
    }


def _save(session, rec: dict) -> None:
    session.execute(text(f"""
        INSERT INTO {DB_SCHEMA}.trademark_filings
            (brand, country, mark_name, applicant, right_holder, app_number,
             app_date, reg_date, nice_code, cls_code, is_cosmetic, is_own)
        VALUES (:brand, :country, :mark, :applicant, :right_holder, :app_number,
                :app_date, :reg_date, :nice_code, :cls_code, :is_cosmetic, :is_own)
        ON CONFLICT (brand, country, app_number) DO UPDATE SET
            reg_date = EXCLUDED.reg_date, is_cosmetic = EXCLUDED.is_cosmetic,
            is_own = EXCLUDED.is_own, mark_name = EXCLUDED.mark_name,
            applicant = EXCLUDED.applicant, fetched_at = NOW()
    """), rec)


def run() -> dict:
    """경쟁 브랜드 EU 상표 수집. 반환 {searched, saved, own, by_brand}."""
    if not (_key() and _secret()):
        logger.warning("EUIPO_KEY/SECRET 미설정 — EU 상표 수집 스킵")
        return {"searched": 0, "saved": 0, "own": 0, "by_brand": {}}
    token = _token()
    if not token:
        return {"searched": 0, "saved": 0, "own": 0, "by_brand": {}}

    saved, own = 0, 0
    by_brand: dict = {}
    session = get_session()
    try:
        _ensure_table(session)
        brands = get_active_brand_names(session)
        for brand in brands:
            term = SEARCH_TERMS.get(brand, brand)
            try:
                items = _search(term, token)
            except PermissionError as e:
                logger.warning("EU 상표 수집 중단(%s) — 승인 대기 추정", e)
                break
            except Exception as e:
                logger.warning("EU 상표 검색 실패 %s: %s", brand, str(e)[:100])
                continue
            n = 0
            for it in items:
                rec = _rec_from_item(brand, it)
                if not rec:
                    continue
                _save(session, rec)
                saved += 1
                n += 1
                if rec["is_own"]:
                    own += 1
            session.commit()
            if n:
                by_brand[brand] = n
                logger.info("  %-18s EU 상표 %d건", brand, n)
            time.sleep(_PAGE_SLEEP)
    finally:
        session.close()
    logger.info("EU 상표 수집: 저장 %d건(자기출원 %d) · 브랜드 %d", saved, own, len(by_brand))
    return {"searched": len(by_brand), "saved": saved, "own": own, "by_brand": by_brand}


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    logging.basicConfig(level=logging.INFO)
    print(run())
