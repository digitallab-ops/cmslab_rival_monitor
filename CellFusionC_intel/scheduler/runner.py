"""
APScheduler 스케줄

- Tier1 브랜드 × Tier1 국가: 매일 18:00 KST (업무시간 이후 — 피크 16시 이후 수집)
- 전체 브랜드 × 전체 국가: 매주 월요일 20:00 KST (주간 풀스캔)
- 주간 모멘텀 계산: 매주 월요일 19:00 KST
- 주간 중복 정리: 매주 일요일 19:00 KST
- Render keep-alive 핑: 14분마다 (무료 플랜 슬립 방지)
"""

import logging
import os
import urllib.request

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


def job_daily_tier1() -> None:
    """Tier1 브랜드 × Tier1 국가 — 매일 수집 (구글RSS + 전문미디어 + 장업신문 + PRTIMES)."""
    reset_jangup_cache()
    tier1 = _get_tier_brands(1)
    logger.info("=== [일별] Tier1 수집 시작 (브랜드 %d개 x 국가 %d개) ===",
                len(tier1), len(TIER1_COUNTRIES))
    for brand in tier1:
        for country in TIER1_COUNTRIES:
            try:
                run_pipeline(brand, country)
            except Exception as e:
                logger.error("오류 [%s/%s]: %s", brand, country, e)
    logger.info("=== [일별] Tier1 수집 완료 ===")


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
    for brand in all_active:
        for country in all_countries:
            try:
                run_pipeline(brand, country)
            except Exception as e:
                logger.error("오류 [%s/%s]: %s", brand, country, e)
    logger.info("=== [주간] 전체 수집 완료 ===")


def job_weekly_momentum() -> None:
    """브랜드 모멘텀 스코어 계산 및 DB 업데이트. 승급/강등 후보 로깅."""
    logger.info("=== [주간] 모멘텀 계산 시작 ===")
    from analytics.queries import compute_brand_momentum, upsert_brand_momentum
    session = get_session()
    try:
        scores = compute_brand_momentum(session)
        for s in scores:
            upsert_brand_momentum(session, s["brand"], s["momentum"])
            if s["signal"] == "rising" and s["tier"] == 2 and s["recent_4w"] >= 5:
                logger.info("⬆  승급 후보: %-20s  momentum=%.2fx  (최근4주=%d건)",
                            s["brand"], s["momentum"], s["recent_4w"])
            elif s["signal"] == "cooling" and s["tier"] == 1 and s["recent_4w"] <= 2:
                logger.info("⬇  강등 후보: %-20s  momentum=%.2fx  (최근4주=%d건)",
                            s["brand"], s["momentum"], s["recent_4w"])
        logger.info("모멘텀 갱신 완료 (%d개 브랜드)", len(scores))
    except Exception as e:
        logger.error("모멘텀 계산 오류: %s", e)
    finally:
        session.close()
    logger.info("=== [주간] 모멘텀 계산 완료 ===")


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

    # 14분마다 — Render 무료 플랜 슬립 방지 (15분 비활성 → 슬립)
    scheduler.add_job(
        job_keepalive,
        trigger=IntervalTrigger(minutes=14),
        id="keepalive",
        name="[상시] Render keep-alive 핑",
        max_instances=1,
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
