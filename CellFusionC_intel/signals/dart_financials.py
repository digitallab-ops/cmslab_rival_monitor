"""
DART(전자공시) 재무 수집 — 경쟁사 실매출·영업이익 (성과 신호).

OpenDART API로 경쟁 브랜드 '운영사'의 연간 매출액·영업이익·당기순이익을
rival_intel.competitor_financials에 적재. 뉴스 활동량(공급)·수출(시장성과)에 이어
'회사 실적(브랜드 성과)' 축을 더함.

자격증명: .env OPENDART_KEY (opendart.fss.or.kr 무료 인증키).

한계(정직히):
- 표준 재무 API(fnlttSinglAcntAll)는 주로 '상장/등록법인 사업보고서' 커버. 순수
  비상장 외감법인은 감사보고서만 내는 경우 데이터가 없을 수 있음 → 로그로 남김.
- 브랜드가 대기업의 한 브랜드면(구달=클리오, 센텔리안24=동국제약, 에스트라=아모레)
  수치는 '회사 전체'라 브랜드 단독 아님. is_brand_level=False로 표기.
- 운영사 매핑은 best-effort. corpCode.xml 매칭 결과를 로그로 출력하니 교정 가능.
"""

import io
import os
import time
import zipfile
import logging
import xml.etree.ElementTree as ET

import requests
from sqlalchemy import text

from config.settings import DB_SCHEMA
from config.brands import ALL_BRANDS
from storage.models import get_session

logger = logging.getLogger(__name__)

_CORP_URL = "https://opendart.fss.or.kr/api/corpCode.xml"
_FIN_URL = "https://opendart.fss.or.kr/api/fnlttSinglAcntAll.json"

# 브랜드 → 운영사 후보명(들). 앞쪽이 우선. corpCode.xml corp_name과 매칭.
#  is_brand_level=False: 브랜드가 대기업 일부 → 수치는 회사 전체.
#  '?' 주석 = 확신 낮음(로그 매칭 결과로 교정 필요).
BRAND_CORP: dict[str, dict] = {
    # ── 상장사(깔끔) ──
    "Rejuran":      {"names": ["파마리서치"], "brand_level": False},          # 리쥬란=파마리서치(상장)
    "Centellian24": {"names": ["동국제약"], "brand_level": False},            # 센텔리안24=동국제약(상장)
    "Goodal":       {"names": ["클리오"], "brand_level": False},              # 구달=클리오(상장)
    "VT Cosmetics": {"names": ["브이티", "브이티지엠피"], "brand_level": True},  # 브이티(상장)
    "Aestura":      {"names": ["아모레퍼시픽"], "brand_level": False},         # 에스트라=아모레퍼시픽
    "Dr.Jart+":     {"names": ["해브앤비"], "brand_level": True},             # 닥터자르트=해브앤비(에스티로더)
    # ── 비상장/외감(될 수도, 안 될 수도) ──
    "Anua":            {"names": ["더파운더즈"], "brand_level": True},
    "Beauty of Joseon":{"names": ["구다이글로벌"], "brand_level": True},
    "Mediheal":        {"names": ["메디힐", "엘앤피코스메틱"], "brand_level": True},
    "Dalba":           {"names": ["달바글로벌", "달바"], "brand_level": True},
    "Torriden":        {"names": ["토리든"], "brand_level": True},
    "Roundlab":        {"names": ["라운드랩"], "brand_level": True},
    "By Wishtrend":    {"names": ["위시컴퍼니"], "brand_level": True},
    "Mixsoon":         {"names": ["믹순", "구다이글로벌"], "brand_level": True},   # ?
    "Numbuzin":        {"names": ["넘버즈인"], "brand_level": True},              # ?
    # Abib: corpCode의 유일한 '아비브'는 IT회사(아비브정보통신) → 오매칭이라 제외.
    "Skin1004":        {"names": ["스킨천사", "스킨1004"], "brand_level": True},   # ?
    "Cos de Baha":     {"names": ["코스드바하"], "brand_level": True},            # ?
    "b.plain":         {"names": ["비플레인"], "brand_level": True},             # ?
    "Zeroid":          {"names": ["제로이드"], "brand_level": True},             # ?
    "Celimax":         {"names": ["셀리맥스"], "brand_level": True},             # ?
}

# 재무제표 계정명 → 표준 키 (정확일치. '매출채권/매출원가/매출총이익'과 구분 필수)
_ACCOUNTS = {
    "매출": "revenue", "매출액": "revenue", "수익(매출액)": "revenue", "영업수익": "revenue",
    "영업이익": "op_income", "영업이익(손실)": "op_income",
    "당기순이익": "net_income", "당기순이익(손실)": "net_income",
}


def _key() -> str:
    return os.getenv("OPENDART_KEY", "").strip()


def _load_corp_index() -> dict[str, list[tuple[str, str]]]:
    """corpCode.xml 다운로드 → {corp_name: [(corp_code, stock_code), ...]}."""
    r = requests.get(_CORP_URL, params={"crtfc_key": _key()}, timeout=60)
    r.raise_for_status()
    # 응답이 ZIP(정상) 또는 XML 에러(키 오류)일 수 있음
    if r.content[:2] != b"PK":
        msg = ET.fromstring(r.content).findtext(".//message") or r.text[:120]
        raise RuntimeError(f"corpCode 오류(키 확인): {msg}")
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        xml = z.read(z.namelist()[0])
    idx: dict[str, list] = {}
    for el in ET.fromstring(xml).iter("list"):
        name = (el.findtext("corp_name") or "").strip()
        code = (el.findtext("corp_code") or "").strip()
        stock = (el.findtext("stock_code") or "").strip()
        if name and code:
            idx.setdefault(name, []).append((code, stock))
    return idx


def _resolve(cands: list[str], idx: dict) -> "tuple[str, str, str] | None":
    """운영사 후보명 → (corp_name, corp_code, stock_code). 정확일치 우선, 없으면 포함."""
    for nm in cands:
        if nm in idx:
            # 상장(stock_code 있음) 우선
            hits = sorted(idx[nm], key=lambda t: (t[1] == "", t[0]))
            return nm, hits[0][0], hits[0][1]
    for nm in cands:
        for cname, lst in idx.items():
            if nm in cname:
                hits = sorted(lst, key=lambda t: (t[1] == "", t[0]))
                return cname, hits[0][0], hits[0][1]
    return None


def _fetch_financials(corp_code: str, year: int) -> "dict | None":
    """연간 재무(연결 우선, 없으면 별도). 반환 {revenue, op_income, net_income} 또는 None."""
    for fs_div in ("CFS", "OFS"):        # 연결 → 별도
        try:
            r = requests.get(_FIN_URL, params={
                "crtfc_key": _key(), "corp_code": corp_code,
                "bsns_year": str(year), "reprt_code": "11011", "fs_div": fs_div,
            }, timeout=30)
            data = r.json()
        except Exception as e:
            logger.warning("DART 재무 요청 실패 %s %s: %s", corp_code, year, e)
            continue
        if data.get("status") != "000":
            continue
        out: dict = {}
        for row in data.get("list", []):
            key = _ACCOUNTS.get((row.get("account_nm") or "").strip())
            if not key or key in out:
                continue
            amt = (row.get("thstrm_amount") or "").replace(",", "").strip()
            try:
                out[key] = int(amt)
            except ValueError:
                pass
        if out.get("revenue"):
            out["fs_div"] = fs_div
            return out
    return None


def _ensure_table(session) -> None:
    session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.competitor_financials (
            id BIGSERIAL PRIMARY KEY,
            brand VARCHAR(100) NOT NULL,
            corp_name VARCHAR(120),
            corp_code VARCHAR(12),
            stock_code VARCHAR(12),
            is_brand_level BOOLEAN DEFAULT TRUE,
            bsns_year INT NOT NULL,
            fs_div VARCHAR(4),
            revenue BIGINT,
            op_income BIGINT,
            net_income BIGINT,
            fetched_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            UNIQUE(brand, bsns_year)
        )
    """))
    session.commit()


def _save(session, brand, meta, year, fin) -> None:
    session.execute(text(f"""
        INSERT INTO {DB_SCHEMA}.competitor_financials
            (brand, corp_name, corp_code, stock_code, is_brand_level,
             bsns_year, fs_div, revenue, op_income, net_income)
        VALUES (:b, :cn, :cc, :sc, :bl, :yr, :fd, :rev, :op, :net)
        ON CONFLICT (brand, bsns_year) DO UPDATE SET
            revenue = EXCLUDED.revenue, op_income = EXCLUDED.op_income,
            net_income = EXCLUDED.net_income, fs_div = EXCLUDED.fs_div,
            corp_name = EXCLUDED.corp_name, fetched_at = NOW()
    """), {"b": brand, "cn": meta["corp_name"], "cc": meta["corp_code"],
           "sc": meta["stock_code"], "bl": meta["brand_level"], "yr": year,
           "fd": fin.get("fs_div"), "rev": fin.get("revenue"),
           "op": fin.get("op_income"), "net": fin.get("net_income")})
    session.commit()


def run(years: int = 3) -> dict:
    """경쟁사 운영사 최근 years년 재무 수집. 반환 {resolved, saved, unmatched, no_data}."""
    if not _key():
        logger.warning("OPENDART_KEY 미설정 — DART 재무 수집 스킵")
        return {"resolved": 0, "saved": 0, "unmatched": [], "no_data": []}

    from datetime import datetime
    idx = _load_corp_index()
    logger.info("corpCode 로드: 법인 %d개", len(idx))
    cur_year = datetime.utcnow().year
    target_years = list(range(cur_year - 1, cur_year - 1 - years, -1))  # 전년부터 역순

    resolved, saved, unmatched, no_data = 0, 0, [], []
    session = get_session()
    try:
        _ensure_table(session)
        for brand in ALL_BRANDS:
            spec = BRAND_CORP.get(brand)
            if not spec:
                unmatched.append(brand)
                continue
            hit = _resolve(spec["names"], idx)
            if not hit:
                unmatched.append(brand)
                logger.info("  ✗ 미매칭: %-18s 후보=%s", brand, spec["names"])
                continue
            corp_name, corp_code, stock_code = hit
            resolved += 1
            meta = {"corp_name": corp_name, "corp_code": corp_code,
                    "stock_code": stock_code, "brand_level": spec["brand_level"]}
            got_any = False
            for yr in target_years:
                fin = _fetch_financials(corp_code, yr)
                if fin:
                    _save(session, brand, meta, yr, fin)
                    saved += 1
                    got_any = True
                time.sleep(0.15)          # API 예의(초당 제한 회피)
            tag = "상장" if stock_code else "비상장"
            logger.info("  ✓ %-18s → %s(%s) %s%s", brand, corp_name, tag,
                        "데이터 O" if got_any else "데이터 X",
                        "" if spec["brand_level"] else " ※회사전체")
            if not got_any:
                no_data.append(f"{brand}({corp_name})")
    finally:
        session.close()

    logger.info("DART 재무: 매칭 %d, 저장 %d행, 미매칭 %s, 데이터없음 %s",
                resolved, saved, unmatched, no_data)
    return {"resolved": resolved, "saved": saved,
            "unmatched": unmatched, "no_data": no_data}


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    logging.basicConfig(level=logging.INFO)
    print(run())
