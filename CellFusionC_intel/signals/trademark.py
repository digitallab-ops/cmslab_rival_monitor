"""
KIPRIS 해외상표 출원 수집 (진출 선행신호).

경쟁 브랜드가 미국·일본에 상표를 내면 = 해당 시장 진출 임박(뉴스보다 선행).
KIPRIS Plus '해외상표' API(ForeignTradeMark)로 브랜드 상표명 검색 → US/JP 상표를
rival_intel.trademark_filings에 적재.

자격증명: .env KIPRIS_KEY (plus.kipris.or.kr accessKey, data.go.kr 키와 다름).
한계: 이 API는 미국·일본 등록공보만(EU·중국 없음). '등록'공보라 출원 직후엔 시차 가능
      (단 applicationDate 필드로 실제 출원 시점 파악). 화장품 NICE 03류로 정밀도 보정.
"""

import os
import logging
import xml.etree.ElementTree as ET
from datetime import date

import requests
from sqlalchemy import text

from config.settings import DB_SCHEMA
from storage.models import get_session
from storage.repository import get_active_brand_names

logger = logging.getLogger(__name__)

_URL = "http://plus.kipris.or.kr/openapi/rest/ForeignTradeMarkAdvencedSearchService/advancedSearch"
_COUNTRIES = ["US", "JP"]                 # 이 API 지원 국가(collectionValues)

# 브랜드 → 상표명 검색어(영문 우선; 해외 등록은 영문/현지어). 오탐 줄이려 NICE 03류 필터 병행.
SEARCH_TERMS: dict[str, str] = {
    "Anua": "Anua", "Mediheal": "Mediheal", "Dalba": "dalba",
    "Beauty of Joseon": "Beauty of Joseon", "Skin1004": "SKIN1004",
    "Dr.Jart+": "Dr.Jart", "Torriden": "Torriden", "Cos de Baha": "Cos De Baha",
    "By Wishtrend": "Wishtrend", "Roundlab": "Round Lab", "Centellian24": "Centellian",
    "VT Cosmetics": "VT COSMETICS", "Numbuzin": "numbuzin", "b.plain": "b.plain",
    "Goodal": "Goodal", "Abib": "Abib", "Rejuran": "Rejuran", "Mixsoon": "mixsoon",
    "Aestura": "Aestura", "Zeroid": "Zeroid", "Celimax": "Celimax",
}

# 브랜드별 '실제 운영사' 출원인 별칭(정규화 후 부분일치). 스쿼터·동명 오탐과 구분용.
#  라이브 응답에서 확인한 실제 출원인명 기반. (없는 브랜드는 is_own 판정 불가 → False)
OWN_APPLICANTS: dict[str, list[str]] = {
    "Anua": ["FOUNDERS"], "Aestura": ["AMOREPACIFIC", "PACIFICPHARMA"],
    "Beauty of Joseon": ["GOODAIGLOBAL"], "By Wishtrend": ["WISHCOMPANY", "SOUNGHOPARK", "PARKSOUNGHO"],
    "Celimax": ["ABSORBLAB"], "Centellian24": ["DONGKOOK"], "Cos de Baha": ["PARKSUNGIL"],
    "Dalba": ["GLOWKOREA", "DALBA"], "Dr.Jart+": ["HAVE&BE", "HAVEBE", "HAVE&"],
    "Goodal": ["CLIO"], "Mediheal": ["L&PCOSMETIC", "LPCOSMETIC", "L&PCOSMETICS"],
    "Mixsoon": ["PARKET"], "Numbuzin": ["BENOW"], "Rejuran": ["PHARMARESEARCH"],
    "Roundlab": ["ROUNDLAB"], "Skin1004": ["CRAVER", "SKIN1004"], "Torriden": ["TORRIDEN"],
    "VT Cosmetics": ["VTCOSMETIC", "VTGMP"], "Abib": ["FOURCOMPANY", "ABIB"], "Zeroid": ["ZEROID"],
}


def _norm_applicant(s: str) -> str:
    import re
    return re.sub(r"[^A-Z0-9&]", "", (s or "").upper())


def _is_own(brand: str, applicant: str) -> bool:
    aliases = OWN_APPLICANTS.get(brand)
    if not aliases:
        return False
    a = _norm_applicant(applicant)
    return any(_norm_applicant(al) in a for al in aliases)


_FIELDS = {"applicant", "applicationNumber", "applicationDate", "registrationDate",
           "registrationNumber", "rightHolder", "niceCode",
           "tradeMarkClassificationCode", "tradeMarkName", "tradeMarkType",
           "viennaCode", "colString"}


def _key() -> str:
    return os.getenv("KIPRIS_KEY", "").strip()


def _parse_date(s: str) -> "date | None":
    s = (s or "").strip()
    if len(s) != 8 or not s.isdigit():
        return None
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None


def _nice_has_cosmetic(nice: str, cls: str) -> bool:
    """NICE 상품분류에 3류(화장품) 포함 여부. '003'(3자리) / '3' / '003005' 형식 대응."""
    import re
    classes = set()
    for tok in re.findall(r"\d+", (nice or "") + " " + (cls or "")):
        if len(tok) >= 3 and len(tok) % 3 == 0:      # '003', '003005' → 3자리씩
            classes.update(int(tok[i:i + 3]) for i in range(0, len(tok), 3))
        else:
            classes.add(int(tok))
    return 3 in classes


def _search(term: str, country: str) -> list[dict]:
    params = {
        "tradeMarkName": term, "collectionValues": country,
        "accessKey": _key(), "docsCount": 500, "currentPage": 1,
        "sortField": "applicationDate", "sortState": "true",
    }
    r = requests.get(_URL, params=params, timeout=30)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    # 에러 체크(KIPRIS Plus: successYN=N / resultCode)
    yn = root.findtext(".//successYN")
    if yn and yn.upper() == "N":
        logger.warning("KIPRIS 오류(%s/%s): %s", term, country,
                       root.findtext(".//resultMsg") or root.findtext(".//resultCode"))
        return []
    # 컨테이너 태그명을 몰라도 되도록 parent-map으로 레코드 그룹핑
    parent = {c: p for p in root.iter() for c in p}
    recs: dict = {}
    for f in _FIELDS:
        for el in root.iter(f):
            p = parent.get(el)
            if p is None:
                continue
            recs.setdefault(id(p), {})[f] = (el.text or "").strip()
    out = []
    for rec in recs.values():
        if not (rec.get("applicationNumber") or rec.get("tradeMarkName")):
            continue
        out.append(rec)
    return out


def _ensure_table(session) -> None:
    session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.trademark_filings (
            id BIGSERIAL PRIMARY KEY,
            brand VARCHAR(100) NOT NULL,
            country VARCHAR(4) NOT NULL,          -- US | JP
            mark_name VARCHAR(200),
            applicant VARCHAR(200),
            right_holder VARCHAR(200),
            app_number VARCHAR(40) NOT NULL,
            app_date DATE,
            reg_date DATE,
            nice_code VARCHAR(80),
            cls_code VARCHAR(80),
            is_cosmetic BOOLEAN DEFAULT FALSE,
            is_own BOOLEAN DEFAULT FALSE,        -- 출원인이 실제 운영사(스쿼터·오탐 구분)
            fetched_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            UNIQUE(brand, country, app_number)
        )
    """))
    # 기존 테이블에 is_own 컬럼 보강(첫 실행 후 추가된 컬럼)
    session.execute(text(
        f"ALTER TABLE {DB_SCHEMA}.trademark_filings "
        f"ADD COLUMN IF NOT EXISTS is_own BOOLEAN DEFAULT FALSE"
    ))
    session.execute(text(
        f"CREATE INDEX IF NOT EXISTS ix_trademark_brand_date "
        f"ON {DB_SCHEMA}.trademark_filings (brand, app_date DESC)"
    ))
    session.commit()


def _save(session, brand: str, country: str, rec: dict) -> None:
    session.execute(text(f"""
        INSERT INTO {DB_SCHEMA}.trademark_filings
            (brand, country, mark_name, applicant, right_holder, app_number,
             app_date, reg_date, nice_code, cls_code, is_cosmetic, is_own)
        VALUES (:b, :c, :mk, :ap, :rh, :an, :ad, :rd, :nc, :cl, :cos, :own)
        ON CONFLICT (brand, country, app_number) DO UPDATE SET
            reg_date = EXCLUDED.reg_date, is_cosmetic = EXCLUDED.is_cosmetic,
            is_own = EXCLUDED.is_own, fetched_at = NOW()
    """), {
        "b": brand, "c": country,
        "mk": rec.get("tradeMarkName", "")[:200],
        "ap": rec.get("applicant", "")[:200],
        "rh": rec.get("rightHolder", "")[:200],
        "an": rec.get("applicationNumber", "")[:40] or "-",
        "ad": _parse_date(rec.get("applicationDate", "")),
        "rd": _parse_date(rec.get("registrationDate", "")),
        "nc": rec.get("niceCode", "")[:80],
        "cl": rec.get("tradeMarkClassificationCode", "")[:80],
        "cos": _nice_has_cosmetic(rec.get("niceCode", ""),
                                  rec.get("tradeMarkClassificationCode", "")),
        "own": _is_own(brand, rec.get("applicant", "")),
    })


def run() -> dict:
    """경쟁 브랜드 US·JP 상표 수집. 반환 {searched, saved, cosmetic, by_brand}."""
    if not _key():
        logger.warning("KIPRIS_KEY 미설정 — 해외상표 수집 스킵")
        return {"searched": 0, "saved": 0, "cosmetic": 0, "by_brand": {}}

    saved, cosmetic, own = 0, 0, 0
    by_brand: dict = {}
    session = get_session()
    try:
        _ensure_table(session)
        brands = get_active_brand_names(session)
        for brand in brands:
            term = SEARCH_TERMS.get(brand, brand)
            n_b, n_own = 0, 0
            for cc in _COUNTRIES:
                try:
                    recs = _search(term, cc)
                except Exception as e:
                    logger.warning("해외상표 검색 실패 %s/%s: %s", brand, cc, e)
                    continue
                for rec in recs:
                    _save(session, brand, cc, rec)
                    saved += 1
                    n_b += 1
                    if _nice_has_cosmetic(rec.get("niceCode", ""),
                                          rec.get("tradeMarkClassificationCode", "")):
                        cosmetic += 1
                    if _is_own(brand, rec.get("applicant", "")):
                        own += 1
                        n_own += 1
            session.commit()
            if n_b:
                by_brand[brand] = n_b
                logger.info("  %-18s US+JP 상표 %d건(자기출원 %d)", brand, n_b, n_own)
    finally:
        session.close()
    logger.info("해외상표 수집: 저장 %d건(화장품류 %d · 자기출원 %d) · 브랜드 %d",
                saved, cosmetic, own, len(by_brand))
    return {"searched": len(brands), "saved": saved,
            "cosmetic": cosmetic, "own": own, "by_brand": by_brand}


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    logging.basicConfig(level=logging.INFO)
    print(run())
