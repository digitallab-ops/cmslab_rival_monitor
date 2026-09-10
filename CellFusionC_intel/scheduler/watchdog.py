"""
파이프라인 AI 파수꾼(Watchdog) — 1단계: 수집 헬스 이상감지 + 실패 자동진단.

수집 완료 후 소스/리테일 이상(0건·급감·오래됨·오류율)과 잡 실패(traceback)를 감지하고,
AI(mini)로 '가장 가능성 높은 원인 + 확인/조치 방향'을 진단해 슬랙(개인/수집 채널)에 리포트.
자동 수정은 하지 않음(감지→진단→제안). 중복 알림은 high_alert_log 재사용해 쿨다운.
"""

import os
import logging
from datetime import datetime

from sqlalchemy import text

from storage.models import get_session
from config.settings import DB_SCHEMA

logger = logging.getLogger(__name__)

_WD_MODEL = os.getenv("BRIEF_MODEL", "gpt-4o-mini")
_COOLDOWN_HOURS = 72          # 같은 이상은 3일에 한 번만 알림(나그 방지)


# ── 중복 방지(high_alert_log 재사용) ─────────────────────────────────────────
def _dedup_ok(session, key: str) -> bool:
    """이 이상 키를 지금 알려도 되는지(쿨다운 내 기존 알림 없으면 True). fail-open."""
    try:
        row = session.execute(text(f"""
            SELECT 1 FROM {DB_SCHEMA}.high_alert_log
            WHERE brand = '__WATCHDOG__' AND sig = :k
              AND sent_at >= now() - (:h || ' hours')::interval
            LIMIT 1"""), {"k": key, "h": _COOLDOWN_HOURS}).fetchone()
        if row:
            return False
        session.execute(text(f"""
            INSERT INTO {DB_SCHEMA}.high_alert_log (brand, country, activity_type, sig, sent_at)
            VALUES ('__WATCHDOG__', '', 'watchdog', :k, now())"""), {"k": key})
        session.commit()
        return True
    except Exception as e:
        logger.debug("watchdog dedup 실패(알림 진행): %s", e)
        try:
            session.rollback()
        except Exception:
            pass
        return True


# ── 수집 헬스 이상감지 ───────────────────────────────────────────────────────
def check_collection_health(session, agg: dict | None = None) -> list[dict]:
    """소스별 급감·리테일 미갱신·오류율 이상을 찾아 findings 리스트로 반환."""
    findings: list[dict] = []

    # 1) 소스(collector_type)별 — '평소 매일 오던 소스가 오늘 0건'을 핵심 신호로(오탐 최소화).
    #    주간 풀스캔이 평균을 부풀리므로 평균 대신 '활동일수 + 중앙값'으로 판정.
    try:
        rows = session.execute(text(f"""
            SELECT collector_type, collected_at::date AS dt, count(*) AS c
            FROM {DB_SCHEMA}.news_articles
            WHERE collected_at >= now() - interval '15 days' AND collector_type IS NOT NULL
            GROUP BY 1, 2""")).fetchall()
        today = session.execute(text("SELECT now()::date")).scalar()   # 서버 tz 기준 오늘
        per: dict = {}
        for ct, dt, c in rows:
            d = per.setdefault(ct, {"today": 0, "prior": []})
            if dt == today:
                d["today"] += int(c)
            else:
                d["prior"].append(int(c))
        for ct, d in per.items():
            prior = sorted(d["prior"])
            active_days = len(prior)                       # 최근 14일 중 수집된 날 수
            med = prior[len(prior) // 2] if prior else 0   # 중앙값(주간 스파이크 영향 적음)
            t = d["today"]
            if t == 0 and active_days >= 8:
                # 거의 매일 오던 소스가 오늘 0건 → 명백한 이상
                findings.append({
                    "type": "source_drop", "key": f"src:{ct}",
                    "title": f"수집 소스 '{ct}' 오늘 0건 (평소 거의 매일 수집)",
                    "detail": (f"'{ct}'가 오늘 0건. 최근 14일 중 {active_days}일 수집(중앙값 {med}건/일)했는데 "
                               f"오늘은 전무. 소스 URL/HTML 변경·차단·API 키 만료 등 가능성."),
                })
            elif med >= 15 and active_days >= 10 and 0 < t < med * 0.2:
                # 고빈도 소스가 중앙값의 20% 미만으로 급감
                findings.append({
                    "type": "source_drop", "key": f"src:{ct}",
                    "title": f"수집 소스 '{ct}' 급감 — 오늘 {t}건 (중앙값 {med}건/일)",
                    "detail": (f"'{ct}'가 오늘 {t}건으로 중앙값 {med}건의 {t/med*100:.0f}% 수준. "
                               f"부분 실패·쿼리 변경·차단 가능성."),
                })
    except Exception as e:
        logger.warning("소스 헬스 체크 실패: %s", e)

    # 2) 아마존 리테일 신선도 — 며칠째 미갱신(스크래핑 차단/구조변경 신호)
    try:
        _r = session.execute(text(
            f"SELECT MAX(capture_date), (now()::date - MAX(capture_date)) "
            f"FROM {DB_SCHEMA}.retail_rankings")).fetchone()
        cap = _r[0] if _r else None
        age = int(_r[1]) if _r and _r[1] is not None else None
        if cap and age is not None:
            if age >= 4:      # 주2회(월·목) 수집이라 4일 이상이면 이상
                findings.append({
                    "type": "retail_stale", "key": f"retail:{cap}",
                    "title": f"아마존 리테일 {age}일째 미갱신 (최신 {cap})",
                    "detail": (f"retail_rankings 최신 capture_date={cap} ({age}일 전). 아마존 베스트셀러 "
                               f"페이지 구조 변경·봇 차단·네트워크 문제 가능성."),
                })
    except Exception as e:
        logger.warning("리테일 신선도 체크 실패: %s", e)

    # 3) 이번 수집 오류율 — agg 전달 시
    if agg:
        attempts = (agg.get("brands", 0) * agg.get("countries", 0)) or 0
        errs = agg.get("errors", 0)
        if attempts and errs and errs / attempts > 0.4:
            samples = agg.get("error_samples") or []
            findings.append({
                "type": "error_rate", "key": "errrate",
                "title": f"수집 오류율 높음 — {errs}/{attempts}건 오류",
                "detail": (f"이번 수집에서 {attempts}건 중 {errs}건 오류(>40%). 예시: "
                           + " / ".join(samples[:3]) if samples else
                           f"이번 수집 {attempts}건 중 {errs}건 오류(>40%)."),
            })

    return findings


# ── AI 진단 ─────────────────────────────────────────────────────────────────
def _diagnose(findings: list[dict]) -> dict:
    """이상들에 대해 '가장 가능성 높은 원인 + 확인/조치 방향' 한 줄씩. {key: read}."""
    if not findings:
        return {}
    lines = [f"[{i}] {f['title']} — {f['detail']}" for i, f in enumerate(findings)]
    prompt = f"""너는 데이터 수집 파이프라인(뉴스·리테일 스크래핑, Python/APScheduler) 운영 엔지니어다.
아래는 오늘 감지된 이상 목록이다. 각 항목의 **가장 가능성 높은 원인**과 **확인/조치 방향**을 한 줄로 제시하라.

{chr(10).join(lines)}

- 각 45자 내외, 기술적·구체적으로(예: "RSS 피드 URL 변경 추정 → 소스 목록 점검", "아마존 노드ID 변경 → 셀렉터 확인").
- 억측 금지, 근거 약하면 '점검 필요' 수준으로.
반드시 JSON만: {{"reads": [{{"i": 0, "read": "..."}}, ...]}} — 모든 인덱스 포함."""
    try:
        from openai import OpenAI
        import json as _json
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        resp = client.chat.completions.create(
            model=_WD_MODEL, max_tokens=500, temperature=0.3,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
        )
        data = _json.loads(resp.choices[0].message.content or "{}")
        out = {}
        for it in data.get("reads", []):
            i = it.get("i")
            rd = (it.get("read") or "").strip()
            if isinstance(i, int) and 0 <= i < len(findings) and rd:
                out[findings[i]["key"]] = rd
        return out
    except Exception as e:
        logger.warning("watchdog 진단 실패: %s", e)
        return {}


# ── 실행 진입점 ──────────────────────────────────────────────────────────────
def run_watchdog(agg: dict | None = None) -> int:
    """수집 완료 후 호출 — 헬스 이상 감지→쿨다운 필터→AI 진단→슬랙. 반환: 알린 건수."""
    session = get_session()
    try:
        findings = check_collection_health(session, agg)
        fresh = [f for f in findings if _dedup_ok(session, f["key"])]
        if not fresh:
            logger.info("watchdog: 새 이상 없음(전체 %d, 쿨다운 후 0)", len(findings))
            return 0
        reads = _diagnose(fresh)
        try:
            from notifications.slack import send_watchdog
            body_lines = []
            for f in fresh:
                rd = reads.get(f["key"])
                body_lines.append(f"• *{f['title']}*\n{f['detail']}"
                                  + (f"\n🔧 _AI 진단:_ {rd}" if rd else ""))
            send_watchdog(f"🐕 수집 파수꾼 — 이상 {len(fresh)}건 감지", "\n\n".join(body_lines))
        except Exception as e:
            logger.warning("watchdog 슬랙 전송 실패: %s", e)
        logger.info("watchdog: 이상 %d건 알림", len(fresh))
        return len(fresh)
    except Exception as e:
        logger.warning("watchdog 실행 실패: %s", e)
        return 0
    finally:
        session.close()


def diagnose_failure(job_id: str, exc: Exception, tb_str: str = "") -> None:
    """잡이 예기치 않게 크래시했을 때(APScheduler EVENT_JOB_ERROR) 원인·수정방향 AI 진단→슬랙."""
    tb_tail = (tb_str or "")[-1600:]
    prompt = f"""파이썬 데이터 수집 잡 '{job_id}'이 예기치 않게 실패했다. 아래 traceback을 보고
(1) 실패 원인 한 줄, (2) 수정 방향 한 줄을 한국어로 제시하라. 기술적·구체적으로.

에러: {type(exc).__name__}: {exc}

traceback(끝부분):
{tb_tail}"""
    summary = ""
    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        resp = client.chat.completions.create(
            model=_WD_MODEL, max_tokens=300, temperature=0.2,
            messages=[{"role": "user", "content": prompt}])
        summary = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.warning("실패 진단 LLM 실패: %s", e)
        summary = f"{type(exc).__name__}: {exc}"
    # 같은 잡 실패 쿨다운
    session = get_session()
    try:
        if not _dedup_ok(session, f"fail:{job_id}"):
            return
    finally:
        session.close()
    try:
        from notifications.slack import send_watchdog
        send_watchdog(f"🐕 파이프라인 실패 — 잡 '{job_id}' 크래시",
                      f"*{type(exc).__name__}:* {exc}\n\n🔧 _AI 진단:_\n{summary}")
    except Exception as e:
        logger.warning("실패 진단 슬랙 전송 실패: %s", e)
