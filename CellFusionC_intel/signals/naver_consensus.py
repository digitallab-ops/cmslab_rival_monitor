"""상장 경쟁사의 확정·추정 실적 — 네이버 증권(컨센서스).

DART·NICE는 **이미 나온 실적**만 준다. 앞으로 얼마 할 것 같은지는 증권사
컨센서스뿐이고, 그건 상장사에만 존재한다. 우리 경쟁사 중 8개가 상장사다
(아누아·조선미녀·토리든·티르티르·스킨1004·메디힐·닥터자르트는 비상장 —
데이터가 없는 게 아니라 **애초에 존재하지 않는다**. 화면에서 구분해야 한다).

경로가 둘이다.
  1) 모바일 API — 안정적. 연간 4년·분기 6개. 다만 추정 분기가 1개만 온다.
  2) WiseReport — 추정 분기를 2개 다 준다(2026/09(E)+2026/12(E)). 대신
     encparam 토큰을 페이지에서 긁어야 해서 깨지기 쉽다.
1을 기본으로 쓰고 2로 모자란 분기만 채운다. 2가 막혀도 1은 계속 나온다.

공식 오픈API가 아니라 화면이 쓰는 내부 엔드포인트다. 예고 없이 바뀔 수 있으니
실패해도 기존 DART·NICE 재무는 건드리지 않는다.
"""

import logging
import re
import time

import requests
from sqlalchemy import text

from config.settings import DB_SCHEMA
from storage.models import get_session

logger = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/140.0 Safari/537.36")
_TIMEOUT = 20
_GAP = 0.5          # 호출 간격 — 공식 API가 아니므로 최소한의 예의

# 브랜드 → 상장 모회사. 네이버 종목검색으로 코드를 찾는다.
# 비상장은 아예 넣지 않는다(빈 조회로 매번 실패 로그를 남기지 않게).
LISTED_PARENTS: dict = {
    "Medicube": "에이피알",
    "Rejuran": "파마리서치",
    "Zeroid": "네오팜",
    "Centellian24": "동국제약",
    "Goodal": "클리오",
    "Aestura": "아모레퍼시픽",
    "Dalba": "달바글로벌",
    "VT Cosmetics": "브이티",
}

# 우리가 쓰는 지표만. PER·PBR·목표주가·시세는 주식 투자용이라 받지 않는다.
_WANTED = {"매출액": "revenue", "영업이익": "op_income", "당기순이익": "net_income",
           "영업이익률": "op_margin", "순이익률": "net_margin"}


def _sess() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": _UA, "Referer": "https://m.stock.naver.com/"})
    return s


def _num(v):
    """'15,273' → 15273.0. '-'·공백·None은 None."""
    if v is None:
        return None
    t = str(v).replace(",", "").strip()
    if not t or t == "-":
        return None
    try:
        return float(t)
    except ValueError:
        return None


def find_stock_code(sess, corp_name: str):
    """회사명 → (종목코드, 시장). 코스피·코스닥만. 못 찾으면 None."""
    try:
        r = sess.get("https://ac.stock.naver.com/ac",
                     params={"q": corp_name, "target": "stock", "st": 1}, timeout=_TIMEOUT)
        for it in (r.json().get("items") or []):
            if it.get("typeCode") in ("KOSPI", "KOSDAQ"):
                return it["code"], it["typeName"]
    except Exception as e:
        logger.warning("종목 검색 실패 %s: %s", corp_name, str(e)[:80])
    return None


def fetch_finance(sess, code: str, period: str) -> list:
    """모바일 API. period는 'annual'|'quarter'.
    반환 [{period_key, period_label, is_estimate, revenue, ...}]."""
    url = f"https://m.stock.naver.com/api/stock/{code}/finance/{period}"
    fi = sess.get(url, timeout=_TIMEOUT).json()["financeInfo"]
    by_metric = {r.get("title"): (r.get("columns") or {}) for r in fi.get("rowList", [])}
    out = []
    for t in fi.get("trTitleList", []):
        key = t.get("key")
        row = {"period_key": key, "period_label": (t.get("title") or "").rstrip("."),
               "is_estimate": t.get("isConsensus") == "Y"}
        for ko, col in _WANTED.items():
            row[col] = _num((by_metric.get(ko, {}).get(key) or {}).get("value"))
        out.append(row)
    return out


def _encparam(sess, code: str):
    """WiseReport 토큰 — 기업 페이지 HTML에 박혀 있다. 실패하면 None."""
    try:
        r = sess.get("https://navercomp.wisereport.co.kr/v3/company/c1010001.aspx",
                     params={"cmp_cd": code, "theme": "light", "cn": ""},
                     headers={"Referer":
                              f"https://finance.naver.com/item/coinfo.naver?code={code}"},
                     timeout=_TIMEOUT)
        m = re.search(r"encparam\s*[=:]\s*['\"]([^'\"]+)", r.text)
        return m.group(1) if m else None
    except Exception as e:
        logger.info("WiseReport 토큰 실패 %s: %s", code, str(e)[:60])
        return None


def _parse_wise_quarters(html: str) -> list:
    """WiseReport 분기표 → [{period_key, period_label, is_estimate, revenue, ...}].

    기간 헤더(2026/12(E))와 각 지표 행의 숫자를 자리 순서로 맞춘다.
    """
    heads = re.findall(r"(\d{4})/(\d{2})(\(E\))?", html)
    if not heads:
        return []
    periods, seen = [], set()
    for y, m, e in heads:
        k = y + m
        if k in seen:
            continue
        seen.add(k)
        periods.append({"period_key": k, "period_label": f"{y}.{m}",
                        "is_estimate": bool(e)})
    rows = {}
    for ko, col in _WANTED.items():
        m = re.search(r">\s*" + re.escape(ko) + r"\s*</th>(.*?)</tr>", html, re.S)
        if not m:
            continue
        rows[col] = [_num(re.sub(r"<[^>]+>", "", c))
                     for c in re.findall(r"<td[^>]*>(.*?)</td>", m.group(1), re.S)]
    out = []
    for i, p in enumerate(periods):
        r = dict(p)
        for col, vals in rows.items():
            r[col] = vals[i] if i < len(vals) else None
        # 추정 분기는 금액 행이 비어 있고 이익률 행만 채워져 있다. 매출×이익률로
        # 되돌리면 화면에 뜨는 값과 정확히 같다(7,776×25.20%=1,960, 화면값 1,960).
        for amt, mrg in (("op_income", "op_margin"), ("net_income", "net_margin")):
            if r.get(amt) is None and r.get("revenue") and r.get(mrg) is not None:
                r[amt] = round(r["revenue"] * r[mrg] / 100)
        if any(r.get(c) is not None for c in _WANTED.values()):
            out.append(r)
    return out


def fetch_extra_quarters(sess, code: str) -> list:
    """모바일 API에 없는 추정 분기를 WiseReport에서 보충. 실패하면 빈 리스트."""
    enc = _encparam(sess, code)
    if not enc:
        return []
    try:
        r = sess.get("https://navercomp.wisereport.co.kr/v3/company/ajax/cF1001.aspx",
                     params={"cmp_cd": code, "fin_typ": 0, "freq_typ": "Q",
                             "encparam": enc, "id": ""},
                     headers={"Referer": "https://navercomp.wisereport.co.kr/v3/company/"
                                         f"c1010001.aspx?cmp_cd={code}"},
                     timeout=_TIMEOUT)
        return _parse_wise_quarters(r.text)
    except Exception as e:
        logger.info("WiseReport 분기 실패 %s: %s", code, str(e)[:60])
        return []


def _ensure_table(session) -> None:
    session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.consensus_financials (
            id BIGSERIAL PRIMARY KEY,
            brand VARCHAR(100) NOT NULL,
            corp_name VARCHAR(100),
            stock_code VARCHAR(10),
            market VARCHAR(10),
            period_type VARCHAR(8) NOT NULL,
            period_key VARCHAR(8) NOT NULL,
            period_label VARCHAR(16),
            is_estimate BOOLEAN DEFAULT FALSE,
            revenue DOUBLE PRECISION,
            op_income DOUBLE PRECISION,
            net_income DOUBLE PRECISION,
            op_margin DOUBLE PRECISION,
            net_margin DOUBLE PRECISION,
            source VARCHAR(16),
            fetched_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            UNIQUE(brand, period_type, period_key)
        )
    """))
    session.execute(text(
        f"CREATE INDEX IF NOT EXISTS ix_cons_brand "
        f"ON {DB_SCHEMA}.consensus_financials (brand, period_type, period_key DESC)"))


def _save(session, brand, corp, code, market, ptype, row, source) -> None:
    session.execute(text(f"""
        INSERT INTO {DB_SCHEMA}.consensus_financials
            (brand, corp_name, stock_code, market, period_type, period_key,
             period_label, is_estimate, revenue, op_income, net_income,
             op_margin, net_margin, source)
        VALUES (:b, :c, :sc, :mk, :pt, :pk, :pl, :est, :rev, :op, :net,
                :opm, :netm, :src)
        ON CONFLICT (brand, period_type, period_key) DO UPDATE SET
            corp_name = EXCLUDED.corp_name, stock_code = EXCLUDED.stock_code,
            market = EXCLUDED.market, period_label = EXCLUDED.period_label,
            is_estimate = EXCLUDED.is_estimate, revenue = EXCLUDED.revenue,
            op_income = EXCLUDED.op_income, net_income = EXCLUDED.net_income,
            op_margin = EXCLUDED.op_margin, net_margin = EXCLUDED.net_margin,
            source = EXCLUDED.source, fetched_at = NOW()
    """), {"b": brand, "c": corp, "sc": code, "mk": market, "pt": ptype,
           "pk": row["period_key"], "pl": row.get("period_label"),
           "est": bool(row.get("is_estimate")), "rev": row.get("revenue"),
           "op": row.get("op_income"), "net": row.get("net_income"),
           "opm": row.get("op_margin"), "netm": row.get("net_margin"),
           "src": source})


def run() -> dict:
    """상장 경쟁사 확정·추정 실적 수집. 반환 {companies, rows, estimates, failed}."""
    sess = _sess()
    session = get_session()
    companies = rows = estimates = failed = 0
    try:
        _ensure_table(session)
        session.commit()
        for brand, corp in LISTED_PARENTS.items():
            found = find_stock_code(sess, corp)
            if not found:
                failed += 1
                logger.warning("상장 코드 못 찾음: %s (%s)", brand, corp)
                continue
            code, market = found
            got = 0
            for ptype in ("annual", "quarter"):
                try:
                    for r in fetch_finance(sess, code, ptype):
                        _save(session, brand, corp, code, market, ptype, r, "naver")
                        got += 1
                        estimates += 1 if r.get("is_estimate") else 0
                except Exception as e:
                    logger.warning("실적 수집 실패 %s/%s: %s", brand, ptype, str(e)[:80])
                time.sleep(_GAP)
            # 모바일 API가 못 준 추정 분기(다음 분기)를 보충한다.
            # 확정분은 모바일 API 값을 신뢰하므로 추정만 받는다.
            try:
                for r in fetch_extra_quarters(sess, code):
                    if r.get("is_estimate"):
                        _save(session, brand, corp, code, market, "quarter", r, "wisereport")
                        got += 1
            except Exception as e:
                logger.info("추정 분기 보충 실패 %s: %s", brand, str(e)[:60])
            session.commit()
            companies += 1
            rows += got
            logger.info("  %s(%s %s) %d행", brand, corp, code, got)
            time.sleep(_GAP)
        logger.info("컨센서스 수집 완료 — 회사 %d · %d행 · 추정 %d · 실패 %d",
                    companies, rows, estimates, failed)
        return {"companies": companies, "rows": rows,
                "estimates": estimates, "failed": failed}
    finally:
        session.close()


if __name__ == "__main__":
    import sys

    from dotenv import load_dotenv

    sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    print(run())
