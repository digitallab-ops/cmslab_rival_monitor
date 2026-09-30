"""곧 나올 것 — 기다릴 거리.

화면이 답으로만 끝나면 다시 올 이유가 없다. "이건 아직 안 나왔다, 언제 나온다"가
있어야 그때 다시 열어본다.

지어내지 않는다. 날짜는 전부 근거가 있다 —
  · 분기 실적: 분기보고서 법정기한은 분기말 + 45일(11/14, 5/15 …).
    scheduler/runner.py의 DART 잡이 같은 근거로 4일·19일에 돈다.
  · 적중표 결론: watch_items.due_on 중 가장 가까운 날.
  · 관세청 수출: 확정분이 두어 달 늦는다 — 우리가 가진 최신 월 + 2개월.
근거가 없으면 그 줄은 아예 만들지 않는다.
"""

import logging
from datetime import date, timedelta

from sqlalchemy import text

from config.settings import DB_SCHEMA

logger = logging.getLogger(__name__)


def _quarter_report_due(today: date) -> "tuple[date, str] | None":
    """다음 분기보고서 법정기한. 분기말 + 45일."""
    ends = [(date(today.year, 3, 31), "1분기"), (date(today.year, 6, 30), "반기"),
            (date(today.year, 9, 30), "3분기"), (date(today.year, 12, 31), "연간"),
            (date(today.year + 1, 3, 31), "1분기")]
    for end, label in ends:
        due = end + timedelta(days=45 if label != "연간" else 90)
        if due >= today:
            return due, f"{end.year}년 {label}"
    return None


def get_upcoming(session, today: "date | None" = None) -> list:
    """반환 [{when, days, title, note}] — 가까운 순. 근거 없는 항목은 넣지 않는다."""
    today = today or date.today()
    out = []

    # ① 분기 실적 — 지금 화면의 '추정'이 '확정'으로 바뀌는 날
    q = _quarter_report_due(today)
    if q:
        due, label = q
        try:
            n = session.execute(text(f"""
                SELECT count(DISTINCT brand) FROM {DB_SCHEMA}.consensus_financials
                WHERE period_type = 'quarter' AND is_estimate AND revenue IS NOT NULL
            """)).scalar() or 0
        except Exception:
            session.rollback()
            n = 0
        if n:
            out.append({
                "when": due, "days": (due - today).days,
                "title": f"{label} 실적 발표",
                "note": f"상장 {n}개사의 추정치가 확정으로 바뀝니다 — 증권사 예상이 맞았는지 그때 보입니다",
            })

    # ② 적중표 — 가장 가까운 결론
    try:
        r = session.execute(text(f"""
            SELECT min(due_on), count(*) FROM {DB_SCHEMA}.watch_items
            WHERE status = 'pending' AND due_on >= :t
        """), {"t": today}).fetchone()
    except Exception:
        session.rollback()
        r = None
    if r and r[0]:
        try:
            same = session.execute(text(f"""
                SELECT count(*) FROM {DB_SCHEMA}.watch_items
                WHERE status = 'pending' AND due_on = :d
            """), {"d": r[0]}).scalar() or 1
        except Exception:
            session.rollback()
            same = 1
        out.append({
            "when": r[0], "days": (r[0] - today).days,
            "title": "지난 판단 첫 채점",
            "note": f"그날 {same}건의 결론이 납니다 · 대기 중 전체 {r[1]}건",
        })

    # ③ 관세청 수출 — 확정분이 두어 달 늦는다
    try:
        last = session.execute(text(
            f"SELECT max(period) FROM {DB_SCHEMA}.export_stats")).scalar()
    except Exception:
        session.rollback()
        last = None
    if last:
        nxt = (last.replace(day=1) + timedelta(days=32)).replace(day=1)
        eta = (nxt + timedelta(days=75))          # 관세청 확정까지 두어 달
        if eta >= today:
            out.append({
                "when": eta, "days": (eta - today).days,
                # %-m은 윈도우 strftime이 못 읽는다 — 숫자를 직접 쓴다
                "title": f"관세청 {nxt.month}월 수출 확정",
                "note": f"지금은 {last:%Y-%m}까지 확정 — 수출로 확인하기로 한 항목들이 이때 판가름납니다",
            })

    out.sort(key=lambda x: x["days"])
    return [{**x, "when": str(x["when"])} for x in out]


if __name__ == "__main__":
    import sys

    from dotenv import load_dotenv

    from storage.models import get_session

    sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    se = get_session()
    try:
        for u in get_upcoming(se):
            print(f"  D-{u['days']:<3} {u['when']}  {u['title']}")
            print(f"        {u['note']}")
    finally:
        se.close()
