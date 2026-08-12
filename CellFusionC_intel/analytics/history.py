"""
시계열 히스토리 — 브랜드 종합스코어·모멘텀·리테일 순위의 주간 스냅샷.

모든 지표가 시점값이라 '누가 뜨고 지는지' 추세를 못 봤음. 주간 스냅샷을 쌓아
브랜드 스코어 추이(스파크라인·상승/하락)를 제공. 리테일은 retail_rankings에
capture_date 이력이 이미 있어 추세 산출 가능.

스냅샷은 결정적(현재 지표 그대로 저장) — 재실행 시 같은 날짜는 덮어씀(idempotent).
"""

import logging
from datetime import date

from sqlalchemy import text
from sqlalchemy.orm import Session

from config.settings import DB_SCHEMA

logger = logging.getLogger(__name__)


def ensure_table(session: Session) -> None:
    session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.brand_score_history (
            id BIGSERIAL PRIMARY KEY,
            brand VARCHAR(100) NOT NULL,
            capture_date DATE NOT NULL,
            composite_score REAL,
            momentum REAL,
            sub_momentum REAL,
            sub_financial REAL,
            sub_trademark REAL,
            sub_demand REAL,
            retail_best_rank INTEGER,
            retail_core_rank INTEGER,
            captured_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            UNIQUE(brand, capture_date)
        )
    """))
    session.execute(text(
        f"CREATE INDEX IF NOT EXISTS ix_score_hist_brand_date "
        f"ON {DB_SCHEMA}.brand_score_history (brand, capture_date DESC)"))
    session.commit()


def snapshot_now(session: Session) -> int:
    """현재 종합스코어·모멘텀·리테일 순위를 오늘 날짜로 스냅샷. 반환: 저장 브랜드 수."""
    from analytics.queries import (
        get_brand_composite_score, compute_brand_momentum, get_retail_performance,
    )
    ensure_table(session)
    cap = date.today()

    composite = {c["brand"]: c for c in (get_brand_composite_score(session) or [])}
    momentum = {m["brand"]: m.get("momentum") for m in (compute_brand_momentum(session) or [])}
    retail = get_retail_performance(session) or {}

    brands = set(composite) | set(momentum) | set(retail)
    n = 0
    for b in brands:
        c = composite.get(b) or {}
        subs = c.get("subs") or {}
        rt = retail.get(b) or {}
        core = rt.get("core") or {}
        session.execute(text(f"""
            INSERT INTO {DB_SCHEMA}.brand_score_history
                (brand, capture_date, composite_score, momentum, sub_momentum,
                 sub_financial, sub_trademark, sub_demand, retail_best_rank, retail_core_rank)
            VALUES (:b, :cap, :cs, :mom, :sm, :sf, :st, :sd, :rbr, :rcr)
            ON CONFLICT (brand, capture_date) DO UPDATE SET
                composite_score=EXCLUDED.composite_score, momentum=EXCLUDED.momentum,
                sub_momentum=EXCLUDED.sub_momentum, sub_financial=EXCLUDED.sub_financial,
                sub_trademark=EXCLUDED.sub_trademark, sub_demand=EXCLUDED.sub_demand,
                retail_best_rank=EXCLUDED.retail_best_rank, retail_core_rank=EXCLUDED.retail_core_rank,
                captured_at=NOW()
        """), {"b": b, "cap": cap,
               "cs": c.get("score"), "mom": momentum.get(b),
               "sm": subs.get("momentum"), "sf": subs.get("financial"),
               "st": subs.get("trademark"), "sd": subs.get("demand"),
               "rbr": rt.get("rank"), "rcr": core.get("rank")})
        n += 1
    session.commit()
    logger.info("브랜드 스코어 스냅샷 저장: %d개 브랜드 (%s)", n, cap.isoformat())
    return n


def get_score_trend(session: Session, weeks: int = 12) -> dict:
    """브랜드별 종합스코어 추세. 반환: {brand: {dates:[...], scores:[...], delta, latest}}."""
    try:
        rows = session.execute(text(f"""
            SELECT brand, capture_date::text, composite_score
            FROM {DB_SCHEMA}.brand_score_history
            WHERE capture_date >= CURRENT_DATE - (:wk * 7)
              AND composite_score IS NOT NULL
            ORDER BY brand, capture_date
        """), {"wk": weeks}).fetchall()
    except Exception:
        return {}
    out: dict = {}
    for brand, d, sc in rows:
        o = out.setdefault(brand, {"dates": [], "scores": []})
        o["dates"].append(d)
        o["scores"].append(round(float(sc), 1))
    for brand, o in out.items():
        s = o["scores"]
        o["latest"] = s[-1] if s else None
        o["delta"] = round(s[-1] - s[0], 1) if len(s) >= 2 else 0.0
        o["points"] = len(s)
    return out


if __name__ == "__main__":
    import os
    from dotenv import load_dotenv
    from storage.models import get_session
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    logging.basicConfig(level=logging.INFO)
    s = get_session()
    try:
        print("스냅샷:", snapshot_now(s), "브랜드")
    finally:
        s.close()
