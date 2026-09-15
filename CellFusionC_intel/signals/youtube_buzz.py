"""
유튜브 소셜 버즈 — 전 브랜드 일별 커버 보정.

뉴스 파이프라인에 얹힌 YouTubeCollector는 Tier1(매일)·전체(주간 풀스캔)만 타므로
Tier2 브랜드는 조회수 지표가 주 1회만 찍혀 추세선이 끊긴다. 이 잡이 '오늘 지표가 없는'
브랜드만 골라 채워 23개 전 브랜드의 일별 시계열을 보장한다(이미 찍힌 브랜드는 스킵 →
중복 API 소모 없음).

쿼터: search.list 100유닛 × 미수집 브랜드 수. 아침 수집 뒤에 돌면 보통 Tier2 7개(700유닛).
"""

import logging

from sqlalchemy import text

from config.settings import DB_SCHEMA, YOUTUBE_API_KEY
from storage.models import get_session
from storage.repository import get_active_brand_names

logger = logging.getLogger(__name__)


def _brands_done_today(session) -> set:
    """오늘 이미 버즈 지표가 적재된 브랜드."""
    try:
        rows = session.execute(text(f"""
            SELECT DISTINCT brand FROM {DB_SCHEMA}.social_metrics
            WHERE platform = 'youtube' AND captured_date = CURRENT_DATE
        """)).fetchall()
        return {r[0] for r in rows}
    except Exception:
        return set()


def run() -> dict:
    """오늘 지표가 빠진 브랜드만 수집. 반환: {checked, collected, skipped}."""
    if not YOUTUBE_API_KEY:
        logger.info("YouTube API 키 미설정 — 버즈 보정 스킵")
        return {"checked": 0, "collected": 0, "skipped": 0}

    from collectors.youtube import YouTubeCollector

    session = get_session()
    try:
        brands = get_active_brand_names(session) or []
        done = _brands_done_today(session)
    finally:
        session.close()

    targets = [b for b in brands if b not in done]
    logger.info("유튜브 버즈 보정 — 전체 %d · 오늘 완료 %d · 대상 %d",
                len(brands), len(done), len(targets))

    collector = YouTubeCollector()
    ok = 0
    for b in targets:
        try:
            # 기사 저장은 뉴스 파이프라인 몫 — 여기선 지표 적재(수집기 부수효과)만 취한다.
            collector.collect(b, "US")
            ok += 1
        except Exception as e:
            logger.warning("유튜브 버즈 수집 실패 [%s]: %s", b, e)
    logger.info("유튜브 버즈 보정 완료: %d/%d", ok, len(targets))
    return {"checked": len(brands), "collected": ok, "skipped": len(done)}
