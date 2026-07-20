"""
APScheduler 스케줄

- Tier1 브랜드 × Tier1 국가: 매일 18:00 KST (업무시간 이후 — 피크 16시 이후 수집)
- 전체 브랜드 × 전체 국가: 매주 월요일 20:00 KST (주간 풀스캔)
- 주간 모멘텀 계산: 매주 월요일 19:00 KST
- 주간 중복 정리: 매주 일요일 19:00 KST
- Render keep-alive 핑: 10분마다 (무료 플랜 15분 슬립 방지, 시작 즉시 1회)
"""

import logging
import os
import urllib.request
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from config.brands import TIER1_BRANDS, ALL_BRANDS, TIER1_COUNTRIES, COUNTRIES
from config.settings import TITLE_SIMILARITY_THRESHOLD
from scheduler.pipeline import run_pipeline, reset_jangup_cache
from scheduler.briefing import generate_weekly_briefing
from storage.models import get_session
from storage.repository import save_dedup_candidate, get_recent_titles
from deduplication.url_hasher import title_similarity

logger = logging.getLogger(__name__)


def _get_tier_brands(tier: int) -> list[str]:
    """monitored_brands DB에서 활성 브랜드 목록 조회. DB 실패 시 하드코딩 fallback."""
    try:
        from sqlalchemy import text
        session = get_session()
        rows = session.execute(text(
            "SELECT name FROM rival_intel.monitored_brands "
            "WHERE tier = :tier AND is_active = TRUE ORDER BY name"
        ), {"tier": tier}).fetchall()
        session.close()
        brands = [r[0] for r in rows]
        if brands:
            return brands
    except Exception as e:
        logger.warning("DB 브랜드 목록 조회 실패, fallback 사용: %s", e)
    return TIER1_BRANDS if tier == 1 else ALL_BRANDS


def _run_collection(label: str, brands: list[str], countries: list[str],
                    deep_query: bool = False) -> None:
    """브랜드×국가 수집 루프 + 완료 후 Slack 요약 리포트.

    deep_query: 구글뉴스 보조(활동) 쿼리 사용 여부. 주간 풀스캔만 True(비용 절감).
    """
    import time
    from collections import defaultdict
    from notifications.slack import notify_collection_summary
    from classifier.claude_classifier import reset_token_usage, get_token_usage
    import collectors.google_rss as _gr

    _gr.DEEP_QUERY = deep_query
    reset_token_usage()
    t0 = time.time()
    agg = {"found": 0, "saved": 0, "classified": 0, "high": 0, "errors": 0}
    saved_by_brand: dict = defaultdict(int)

    for brand in brands:
        for country in countries:
            try:
                st = run_pipeline(brand, country)
                agg["found"]      += st.found
                agg["saved"]      += st.saved
                agg["classified"] += st.classified
                agg["high"]       += st.high
                agg["errors"]     += st.errors
                if st.saved:
                    saved_by_brand[brand] += st.saved
            except Exception as e:
                agg["errors"] += 1
                logger.error("오류 [%s/%s]: %s", brand, country, e)

    usage = get_token_usage()
    agg["brands"]    = len(brands)
    agg["countries"] = len(countries)
    agg["duration"]  = time.time() - t0
    agg["top_saved"] = sorted(saved_by_brand.items(), key=lambda x: -x[1])
    agg["tokens_in"]  = usage["in"]
    agg["tokens_out"] = usage["out"]
    agg["api_calls"]  = usage["calls"]
    agg["cost_usd"]   = usage["cost_usd"]

    logger.info("=== [%s] 수집 완료 — 신규 %d건(HIGH %d) / 오류 %d / OpenAI %d콜 $%.3f ===",
                label, agg["saved"], agg["high"], agg["errors"],
                usage["calls"], usage["cost_usd"])
    try:
        notify_collection_summary(label, agg)
    except Exception as e:
        logger.warning("수집 요약 Slack 전송 실패: %s", e)


def job_daily_tier1() -> None:
    """Tier1 브랜드 × Tier1 국가 — 매일 수집 (구글RSS + 전문미디어 + 장업신문 + PRTIMES)."""
    reset_jangup_cache()
    tier1 = _get_tier_brands(1)
    logger.info("=== [일별] Tier1 수집 시작 (브랜드 %d개 x 국가 %d개) ===",
                len(tier1), len(TIER1_COUNTRIES))
    _run_collection("일별 Tier1", tier1, TIER1_COUNTRIES, deep_query=False)


def job_weekly_full() -> None:
    """전체 브랜드 × 전체 국가 — 주간 풀스캔."""
    all_countries = list(COUNTRIES.keys())
    try:
        from sqlalchemy import text
        session = get_session()
        rows = session.execute(text(
            "SELECT name FROM rival_intel.monitored_brands WHERE is_active = TRUE ORDER BY tier, name"
        )).fetchall()
        session.close()
        all_active = [r[0] for r in rows] or ALL_BRANDS
    except Exception:
        all_active = ALL_BRANDS
    logger.info("=== [주간] 전체 수집 시작 (브랜드 %d개 x 국가 %d개) ===",
                len(all_active), len(all_countries))
    _run_collection("주간 풀스캔", all_active, all_countries, deep_query=True)


TIER_CHANGE_COOLDOWN_DAYS = 14   # 최근 변경 후 이 기간 내 재변경 금지 (플립플롭 방지)


def job_weekly_momentum() -> None:
    """브랜드 모멘텀 계산 → momentum_score 갱신 + tier 자동 승급/강등."""
    logger.info("=== [주간] 모멘텀 계산 시작 ===")
    from analytics.queries import (
        compute_brand_momentum, upsert_brand_momentum,
        update_brand_tier, days_since_tier_change,
    )
    session = get_session()
    promoted, demoted = [], []
    try:
        scores = compute_brand_momentum(session)
        for s in scores:
            upsert_brand_momentum(session, s["brand"], s["momentum"])

            # 자동 티어링: 승급 T2→1(rising & 최근4주≥5), 강등 T1→2(cooling & 최근4주≤2)
            want_promote = s["signal"] == "rising"  and s["tier"] == 2 and s["recent_4w"] >= 5
            want_demote  = s["signal"] == "cooling" and s["tier"] == 1 and s["recent_4w"] <= 2
            if not (want_promote or want_demote):
                continue

            # 히스테리시스: 최근 변경 후 쿨다운 기간 내면 스킵
            since = days_since_tier_change(session, s["brand"])
            if since is not None and since < TIER_CHANGE_COOLDOWN_DAYS:
                logger.info("… 티어 변경 보류(쿨다운 %.0f일): %s", since, s["brand"])
                continue

            new_tier = 1 if want_promote else 2
            update_brand_tier(session, s["brand"], new_tier)
            if want_promote:
                promoted.append(s["brand"])
                logger.info("⬆  승급 T2→1: %-20s  momentum=%.2fx  (최근4주=%d건)",
                            s["brand"], s["momentum"], s["recent_4w"])
            else:
                demoted.append(s["brand"])
                logger.info("⬇  강등 T1→2: %-20s  momentum=%.2fx  (최근4주=%d건)",
                            s["brand"], s["momentum"], s["recent_4w"])

        logger.info("모멘텀 갱신 완료 (%d개 브랜드, 승급 %d / 강등 %d)",
                    len(scores), len(promoted), len(demoted))
        if promoted or demoted:
            _notify_tier_changes(promoted, demoted)
    except Exception as e:
        logger.error("모멘텀 계산 오류: %s", e)
    finally:
        session.close()
    logger.info("=== [주간] 모멘텀 계산 완료 ===")


def _notify_tier_changes(promoted: list[str], demoted: list[str]) -> None:
    """티어 변경 시 Slack 알림 (webhook 없으면 스킵)."""
    url = os.getenv("SLACK_WEBHOOK_URL", "")
    if not url:
        return
    lines = []
    if promoted:
        lines.append("⬆ *승급 (Tier2→1)*: " + ", ".join(promoted))
    if demoted:
        lines.append("⬇ *강등 (Tier1→2)*: " + ", ".join(demoted))
    try:
        import json
        data = json.dumps({"text": "*브랜드 티어 자동 조정*\n" + "\n".join(lines)}).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        logger.warning("티어 변경 Slack 알림 실패: %s", e)


def job_keepalive() -> None:
    """Render 무료 플랜 슬립 방지 — 자기 자신 /health 핑."""
    url = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
    if not url:
        return
    try:
        urllib.request.urlopen(f"{url}/health", timeout=10)
        logger.debug("keep-alive ping OK: %s", url)
    except Exception as e:
        logger.warning("keep-alive ping 실패: %s", e)


def job_profile_sync() -> None:
    """Cafe24 → 자사 제품 라인 자동 동기화 (company_profile.md). 실패해도 파이프라인 무영향."""
    logger.info("=== 자사 제품 프로필 동기화 시작 ===")
    try:
        from analytics.product_sync import sync_company_profile
        sync_company_profile()
        logger.info("=== 제품 프로필 동기화 완료 ===")
    except Exception as e:
        logger.warning("제품 프로필 동기화 스킵(자격증명·연결 확인): %s", e)


def job_weekly_dedup() -> None:
    """제목 유사도 기반 중복 쌍 기록."""
    logger.info("=== 주간 중복 정리 시작 ===")
    session = get_session()
    try:
        recent = get_recent_titles(session, days=7)
        count = 0
        for i in range(len(recent)):
            for j in range(i + 1, len(recent)):
                id1, title1 = recent[i]
                id2, title2 = recent[j]
                score = title_similarity(title1, title2)
                if score >= TITLE_SIMILARITY_THRESHOLD:
                    save_dedup_candidate(session, id1, id2, score)
                    count += 1
        logger.info("중복 후보 %d쌍 기록 완료", count)
    except Exception as e:
        logger.error("주간 중복 정리 오류: %s", e)
    finally:
        session.close()
    logger.info("=== 주간 중복 정리 완료 ===")


def create_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="Asia/Seoul")

    # 매일 18:00 KST (피크 16시 이후 — 하루치 기사 다 올라온 뒤 수집)
    scheduler.add_job(
        job_daily_tier1,
        trigger=CronTrigger(hour=18, minute=0),
        id="daily_tier1",
        name="[일별] Tier1 브랜드x국가 수집",
        max_instances=1,
        coalesce=True,
    )

    # 매주 월요일 20:00 KST (일별 수집 완료 후 풀스캔)
    scheduler.add_job(
        job_weekly_full,
        trigger=CronTrigger(day_of_week="mon", hour=20, minute=0),
        id="weekly_full",
        name="[주간] 전체 브랜드x국가 풀스캔",
        max_instances=1,
        coalesce=True,
    )

    # 매주 월요일 19:00 KST — 모멘텀 계산 (풀스캔 전 실행)
    scheduler.add_job(
        job_weekly_momentum,
        trigger=CronTrigger(day_of_week="mon", hour=19, minute=0),
        id="weekly_momentum",
        name="[주간] 브랜드 모멘텀 계산",
        max_instances=1,
        coalesce=True,
    )

    # 매주 월요일 17:00 KST — 자사 제품 프로필 Cafe24 동기화 (인사이트 생성 전 최신화)
    scheduler.add_job(
        job_profile_sync,
        trigger=CronTrigger(day_of_week="mon", hour=17, minute=0),
        id="profile_sync",
        name="[주간] 자사 제품 프로필 동기화",
        max_instances=1,
        coalesce=True,
    )

    # 매주 일요일 19:00 KST
    scheduler.add_job(
        job_weekly_dedup,
        trigger=CronTrigger(day_of_week="sun", hour=19, minute=0),
        id="weekly_dedup",
        name="[주간] 중복 정리",
        max_instances=1,
    )

    # 매주 화요일 09:00 KST — 월요일 수집 완료 후 다음날 아침 브리핑
    scheduler.add_job(
        generate_weekly_briefing,
        trigger=CronTrigger(day_of_week="tue", hour=9, minute=0),
        id="weekly_briefing",
        name="[주간] 브리핑 생성 및 Slack 전송",
        max_instances=1,
    )

    # 10분마다 — Render 무료 플랜 슬립 방지 (15분 비활성 → 슬립).
    # next_run_time=now: 스케줄러 시작(또는 재시작) 즉시 1회 핑 → 재시작 시
    # 인터벌 리셋으로 생기는 공백(→ 슬립) 방지.
    scheduler.add_job(
        job_keepalive,
        trigger=IntervalTrigger(minutes=10),
        id="keepalive",
        name="[상시] Render keep-alive 핑",
        max_instances=1,
        coalesce=True,
        next_run_time=datetime.now(),
    )

    return scheduler


def start() -> None:
    """스케줄러 독립 실행 (CLI용 — Ctrl+C 로 종료)."""
    import sys
    import time
    log_dir = os.path.join(os.path.dirname(__file__), "..", "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "scheduler.log")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    if hasattr(stream_handler.stream, "reconfigure"):
        try:
            stream_handler.stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    # force=True: cli() 그룹이 이미 basicConfig를 호출했으므로 기존 핸들러를
    # 교체해야 file_handler가 실제로 붙는다 (없으면 no-op → scheduler.log 미기록).
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, stream_handler], force=True)
    scheduler = create_scheduler()

    logger.info("스케줄러 시작")
    for job in scheduler.get_jobs():
        logger.info("  %-14s %s", job.id, job.name)

    scheduler.start()
    try:
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        logger.info("스케줄러 종료")
