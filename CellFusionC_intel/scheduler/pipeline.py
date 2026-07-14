"""
수집 → 중복제거 → 분류 → 저장 파이프라인 (스케줄러·CLI 공용)

컬렉터 종류:
  - google_rss  : 구글 뉴스 RSS (브랜드×국가 조합)
  - media_rss   : BeautyMatter / WWD / Glossy (글로벌 뷰티 미디어)
  - jangup      : 장업신문 (한국 뷰티 전문지)
  - prtimes     : PRTIMES Japan (JP 전용)
"""

import logging
import time
from dataclasses import dataclass

from collectors.base_collector import BaseCollector
from collectors.google_rss import GoogleRSSCollector
from collectors.media_rss import MediaRSSCollector
from collectors.jangup import JangupCollector
from collectors.prtimes import PRTimesCollector
from collectors.naver_news import NaverNewsCollector
from collectors.reddit_collector import RedditCollector
from collectors.youtube import YouTubeCollector
from collectors.body_fetcher import fetch_body
from classifier.claude_classifier import classify_articles
from deduplication.url_hasher import url_hash, deduplicate_batch
from storage.models import NewsArticle, CollectionRun, get_session
from storage.repository import article_exists, save_article, get_recent_titles, save_collection_run
from config.settings import CLASSIFIER_MODEL_DETAIL
from notifications.slack import notify_high_importance

logger = logging.getLogger(__name__)

# 싱글턴 컬렉터 (피드 캐시 재사용)
_google = GoogleRSSCollector()
_media = MediaRSSCollector()
_jangup = JangupCollector()
_prtimes = PRTimesCollector()
_naver = NaverNewsCollector()
_reddit = RedditCollector()
_youtube = YouTubeCollector()


@dataclass
class PipelineStats:
    brand: str
    country: str
    found: int = 0
    url_duped: int = 0
    title_duped: int = 0
    classified: int = 0
    saved: int = 0
    high: int = 0
    errors: int = 0
    duration: float = 0.0
    error_message: str = ""


def _run_single(
    collector: BaseCollector,
    brand: str,
    country: str,
    session,
    stats: PipelineStats,
) -> None:
    """단일 컬렉터로 수집 → 중복제거 → 분류 → 저장."""
    raw_articles = collector.collect(brand, country)
    stats.found += len(raw_articles)

    if not raw_articles:
        return

    # URL 해시 중복 제거
    url_passed = []
    for art in raw_articles:
        h = url_hash(art.url)
        if article_exists(session, h):
            stats.url_duped += 1
        else:
            url_passed.append(art)

    if not url_passed:
        return

    # 제목 유사도 중복 제거
    existing_titles = get_recent_titles(session)
    deduped = deduplicate_batch(url_passed, existing_titles)
    stats.title_duped += len(url_passed) - len(deduped)

    if not deduped:
        return

    # 본문 수집 (필터 통과 기사만 fetch — 비용/속도 균형)
    for art in deduped:
        art.body = fetch_body(art.url)

    # GPT 분류
    classified = classify_articles(deduped, brand, country)
    stats.classified += len(classified)

    # DB 저장
    for raw, clf in classified:
        try:
            article = NewsArticle(
                url_hash=url_hash(raw.url),
                published_date=raw.published,
                brand=clf.brand,
                country=clf.country,
                source_country=country,      # 파이프라인 수집 국가 (크로스마켓 추적용)
                activity_type=clf.activity_type,
                details=clf.details,
                product_name=clf.product_name,
                title_ko=clf.title_ko or None,
                article_body=raw.body or None,
                article_body_ko=clf.article_body_ko or None,
                source_url=raw.url,
                importance=clf.importance,
                note=clf.note,
                title=raw.title,
                source_name=raw.source_name,
                language=raw.language,
                brand_focus=getattr(clf, "brand_focus", None),
                classification_confidence=clf.confidence,
                classifier_model=CLASSIFIER_MODEL_DETAIL,
                collector_type=collector.collector_type,
            )
            save_article(session, article)
            stats.saved += 1

            # high importance → 즉시 Slack 알림
            if clf.importance == "high":
                stats.high += 1
                try:
                    notify_high_importance(article)
                except Exception:
                    pass

        except Exception as e:
            logger.error("저장 실패 (%s): %s", raw.title[:60], e)
            stats.errors += 1


def run_pipeline(brand: str, country: str) -> PipelineStats:
    """구글 RSS + 전문 미디어 + 장업신문 + PRTIMES 통합 파이프라인."""
    stats = PipelineStats(brand=brand, country=country)
    t0 = time.time()
    session = get_session()

    # 사용할 컬렉터 목록
    collectors: list[BaseCollector] = [_google, _media, _jangup]
    if country.upper() == "JP":
        collectors.append(_prtimes)
    if country.upper() == "KR":
        collectors.append(_naver)
    collectors.append(_reddit)   # 글로벌 커뮤니티 — 모든 국가
    collectors.append(_youtube)  # 영상 신호 — US 파이프라인에서만 1회(내부 게이트)

    try:
        for collector in collectors:
            try:
                _run_single(collector, brand, country, session, stats)
            except Exception as e:
                logger.error("[%s/%s] %s 오류: %s", brand, country, collector.collector_type, e)
                stats.errors += 1

    finally:
        stats.duration = round(time.time() - t0, 2)

        run = CollectionRun(
            collector_type="multi",
            brand=brand,
            country=country,
            articles_found=stats.found,
            articles_new=stats.saved,
            articles_duped=stats.url_duped + stats.title_duped,
            error_message=stats.error_message or None,
            duration_secs=stats.duration,
        )
        save_collection_run(session, run)
        session.close()

    logger.info(
        "[%s/%s] 수집 %d → URL중복 %d → 제목중복 %d → 분류 %d → 저장 %d (%.1fs)",
        brand, country,
        stats.found, stats.url_duped, stats.title_duped,
        stats.classified, stats.saved, stats.duration,
    )
    return stats


def reset_jangup_cache():
    """장업신문 피드 캐시 초기화 (매일 첫 수집 전 호출)."""
    _jangup.reset_cache()
