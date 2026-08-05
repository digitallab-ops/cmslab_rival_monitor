"""
관세청 화장품 수출통계 수집 (성과/시장 신호).

data.go.kr '관세청_품목별 국가별 수출입실적(GW)' API로 HS 3304(화장품) 국가별·월별
수출액(USD)을 rival_intel.export_stats에 적재. 뉴스(공급)·검색(수요)에 이어
'실제 수출(성과)' 축을 더해 삼각검증 완성 + 시장 우선순위 하드데이터.

자격증명: .env DATA_GO_KR_KEY (data.go.kr 서비스키; 인코딩/디코딩 형태 모두 허용).
제약: 조회기간 1년 이내(→12개월 청크로 분할), 데이터 월1회 갱신.

HS 세부코드: 330410 입술, 330420 눈, 330430 손발톱, 330491 파우더,
             330499 기초화장품·기타(스킨케어·선케어 핵심 — 셀퓨전씨 카테고리).
"""

import os
import logging
import urllib.parse
import xml.etree.ElementTree as ET
from datetime import date, timedelta

import requests
from sqlalchemy import text

from config.settings import DB_SCHEMA
from config.brands import COUNTRIES
from storage.models import get_session

logger = logging.getLogger(__name__)

_URL = "http://apis.data.go.kr/1220000/nitemtrade/getNitemtradeList"
HS_CODES = ["3304"]                       # 화장품(4자리 조회 시 6자리 세부코드로 반환)
_MONITORED = set(COUNTRIES.keys())        # ISO2 국가코드(statCd와 매칭)


def _service_key() -> str:
    k = os.getenv("DATA_GO_KR_KEY", "").strip()
    return urllib.parse.unquote(k) if "%" in k else k


def _yymm(d: date) -> str:
    return f"{d.year:04d}{d.month:02d}"


def _add_months(d: date, n: int) -> date:
    m = d.month - 1 + n
    return date(d.year + m // 12, m % 12 + 1, 1)


def _month_windows(months: int) -> list[tuple[str, str]]:
    """최근 months개월을 12개월 이내 청크 (strtYymm, endYymm)로 분할 — 1년 제한 대응."""
    first_this = date.today().replace(day=1)
    end = _add_months(first_this, -1)              # 최근 완료월(지난달)
    start = _add_months(end, -(months - 1))
    windows, cur = [], start
    while cur <= end:
        chunk_end = min(end, _add_months(cur, 11))  # 최대 12개월 span
        windows.append((_yymm(cur), _yymm(chunk_end)))
        cur = _add_months(chunk_end, 1)
    return windows


def _parse_period(year_str: str) -> "date | None":
    """'2025.01' → date(2025,1,1). '총계' 등은 None."""
    s = (year_str or "").strip()
    if "." not in s:
        return None
    try:
        y, m = s.split(".")
        return date(int(y), int(m), 1)
    except (ValueError, TypeError):
        return None


def _fetch(hs: str, strt: str, end: str) -> list[dict]:
    p = {"serviceKey": _service_key(), "strtYymm": strt, "endYymm": end,
         "hsSgn": hs, "numOfRows": 100000, "pageNo": 1}
    r = requests.get(_URL, params=p, timeout=60)
    r.raise_for_status()
    root = ET.fromstring(r.text)
    code = root.findtext("./header/resultCode")
    if code not in (None, "00"):
        logger.warning("관세청 API 오류 %s: %s", code, root.findtext("./header/resultMsg"))
        return []

    def _int(it, tag):
        v = (it.findtext(tag) or "").strip().replace(",", "")
        try:
            return int(v)
        except (ValueError, TypeError):
            return 0

    rows = []
    for it in root.findall(".//item"):
        cc = (it.findtext("statCd") or "").strip()
        period = _parse_period(it.findtext("year"))
        hs_cd = (it.findtext("hsCd") or "").strip()
        if period is None or hs_cd in ("", "-") or cc not in _MONITORED:
            continue   # 총계행·비대상국 제외
        rows.append({
            "period": period, "hs_cd": hs_cd, "country_code": cc,
            "country_name": (it.findtext("statCdCntnKor1") or "").strip(),
            "stat_name": (it.findtext("statKor") or "").strip(),
            "exp_usd": _int(it, "expDlr"), "imp_usd": _int(it, "impDlr"),
            "exp_wgt": _int(it, "expWgt"), "trade_balance": _int(it, "balPayments"),
        })
    return rows


def _ensure_table(session) -> None:
    session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.export_stats (
            id BIGSERIAL PRIMARY KEY,
            period DATE NOT NULL,
            hs_cd VARCHAR(10) NOT NULL,        -- 6자리 세부 HS(330499 등)
            country_code VARCHAR(8) NOT NULL,
            country_name VARCHAR(80),
            stat_name VARCHAR(150),            -- 품목명(한글)
            exp_usd BIGINT,                    -- 수출액(USD)
            imp_usd BIGINT,
            exp_wgt BIGINT,                    -- 수출중량
            trade_balance BIGINT,
            fetched_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            UNIQUE(period, hs_cd, country_code)
        )
    """))
    session.execute(text(
        f"CREATE INDEX IF NOT EXISTS ix_export_stats_ctry_period "
        f"ON {DB_SCHEMA}.export_stats (country_code, period DESC)"
    ))
    session.commit()


def _save(session, rows: list[dict]) -> int:
    n = 0
    for r in rows:
        session.execute(text(f"""
            INSERT INTO {DB_SCHEMA}.export_stats
                (period, hs_cd, country_code, country_name, stat_name,
                 exp_usd, imp_usd, exp_wgt, trade_balance)
            VALUES (:period, :hs_cd, :country_code, :country_name, :stat_name,
                    :exp_usd, :imp_usd, :exp_wgt, :trade_balance)
            ON CONFLICT (period, hs_cd, country_code) DO UPDATE SET
                exp_usd = EXCLUDED.exp_usd, imp_usd = EXCLUDED.imp_usd,
                exp_wgt = EXCLUDED.exp_wgt, trade_balance = EXCLUDED.trade_balance,
                stat_name = EXCLUDED.stat_name, country_name = EXCLUDED.country_name,
                fetched_at = NOW()
        """), r)
        n += 1
    session.commit()
    return n


def run(months: int = 25) -> dict:
    """HS 3304 국가별·월별 수출 수집·저장. months=25 → YoY 비교용 2년+."""
    if not _service_key():
        logger.warning("DATA_GO_KR_KEY 미설정 — 관세청 수출통계 수집 스킵")
        return {"rows": 0, "windows": 0}

    all_rows = []
    windows = _month_windows(months)
    for hs in HS_CODES:
        for strt, end in windows:
            try:
                all_rows.extend(_fetch(hs, strt, end))
            except Exception as e:
                logger.warning("관세청 수집 실패 hs=%s %s~%s: %s", hs, strt, end, e)

    if not all_rows:
        return {"rows": 0, "windows": len(windows)}

    session = get_session()
    try:
        _ensure_table(session)
        rows = _save(session, all_rows)
    finally:
        session.close()
    logger.info("관세청 수출통계 저장: 행 %d (윈도우 %d, HS %s)", rows, len(windows), HS_CODES)
    return {"rows": rows, "windows": len(windows)}


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    logging.basicConfig(level=logging.INFO)
    print(run())
