"""
Reddit RSS 수집기 — API 키 불필요
- Reddit 서브레딧 검색 RSS 피드 사용 (공개 접근)
- r/AsianBeauty, r/SkincareAddiction, r/KoreanBeauty
- 소비자 커뮤니티 신호 (입점 소식, 바이럴, 리뷰 트렌드)
- 국가 비종속적 (country 무시, 글로벌 영어 커뮤니티)
"""

import logging
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import feedparser

from collectors.base_collector import BaseCollector, RawArticle
from config.settings import RSS_REQUEST_DELAY

logger = logging.getLogger(__name__)

SUBREDDITS = ["AsianBeauty", "SkincareAddiction", "KoreanBeauty"]

# Reddit 서브레딧 검색 RSS — 인증 불필요
REDDIT_SEARCH_RSS = (
    "https://www.reddit.com/r/{subreddit}/search.rss"
    "?q={query}&sort=new&restrict_sr=1&t=month&limit=10"
)

HEADERS = {"User-Agent": "kbeauty-monitor/1.0 (RSS reader)"}


def _parse_date(entry) -> datetime:
    for field in ("published", "updated"):
        val = getattr(entry, field, None)
        if val:
            try:
                return parsedate_to_datetime(val).astimezone(timezone.utc).replace(tzinfo=None)
            except Exception:
                pass
    return datetime.utcnow()


class RedditCollector(BaseCollector):
    """Reddit 서브레딧 RSS 수집기 — API 키 불필요."""

    collector_type = "reddit"

    def collect(self, brand: str, country: str) -> list[RawArticle]:
        articles: list[RawArticle] = []
        seen_urls: set[str] = set()
        brand_lower = brand.lower()

        for sub in SUBREDDITS:
            url = REDDIT_SEARCH_RSS.format(
                subreddit=sub,
                query=quote_plus(brand),
            )
            try:
                feed = feedparser.parse(url, request_headers=HEADERS)

                matched = 0
                for entry in feed.entries:
                    link = getattr(entry, "link", "").strip()
                    title = getattr(entry, "title", "").strip()
                    summary = getattr(entry, "summary", "").strip()

                    if not link or link in seen_urls:
                        continue
                    if brand_lower not in f"{title} {summary}".lower():
                        continue

                    seen_urls.add(link)
                    articles.append(RawArticle(
                        title=title,
                        url=link,
                        published=_parse_date(entry),
                        summary=summary[:300],
                        source_name=f"Reddit r/{sub}",
                        language="en",
                        brand_hint=brand,
                        country_hint=country,
                    ))
                    matched += 1

                logger.debug("[%s] r/%s → %d건", brand, sub, matched)
                time.sleep(RSS_REQUEST_DELAY)

            except Exception as e:
                logger.warning("Reddit RSS 오류 (%s/r/%s): %s", brand, sub, e)

        logger.info("Reddit RSS 수집: %s → %d건", brand, len(articles))
        return articles
