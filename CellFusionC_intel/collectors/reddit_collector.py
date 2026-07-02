"""
Reddit 수집기 — 소비자 커뮤니티 신호
- r/AsianBeauty, r/SkincareAddiction, r/KoreanBeauty
- 언론 보도보다 빠른 소비자 반응 (입점 소식, 바이럴, 리뷰 트렌드)
- 국가 비종속적 (country 무시, 글로벌 영어 커뮤니티)
- API 등록: https://www.reddit.com/prefs/apps
- .env: REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USER_AGENT
"""

import logging
import time
from datetime import datetime

import praw
from praw.exceptions import PRAWException

from collectors.base_collector import BaseCollector, RawArticle
from config.settings import REDDIT_CLIENT_ID, REDDIT_CLIENT_SECRET, REDDIT_USER_AGENT

logger = logging.getLogger(__name__)

SUBREDDITS = ["AsianBeauty", "SkincareAddiction", "KoreanBeauty"]
RESULTS_PER_SUBREDDIT = 5  # 서브레딧당 최대 수집 건수


class RedditCollector(BaseCollector):
    """Reddit 커뮤니티 K-뷰티 언급 수집기."""

    collector_type = "reddit"
    _reddit: praw.Reddit | None = None

    def _get_client(self) -> praw.Reddit | None:
        if self._reddit is not None:
            return self._reddit
        if not REDDIT_CLIENT_ID or not REDDIT_CLIENT_SECRET:
            logger.warning("Reddit API 키 미설정 — REDDIT_CLIENT_ID/SECRET 확인")
            return None
        try:
            self._reddit = praw.Reddit(
                client_id=REDDIT_CLIENT_ID,
                client_secret=REDDIT_CLIENT_SECRET,
                user_agent=REDDIT_USER_AGENT or "kbeauty-monitor/1.0",
            )
            return self._reddit
        except Exception as e:
            logger.warning("Reddit 클라이언트 초기화 실패: %s", e)
            return None

    def collect(self, brand: str, country: str) -> list[RawArticle]:
        reddit = self._get_client()
        if reddit is None:
            return []

        articles: list[RawArticle] = []
        seen_ids: set[str] = set()
        brand_lower = brand.lower()

        for sub_name in SUBREDDITS:
            try:
                subreddit = reddit.subreddit(sub_name)
                results = subreddit.search(
                    brand,
                    sort="new",
                    time_filter="month",
                    limit=RESULTS_PER_SUBREDDIT,
                )

                matched = 0
                for post in results:
                    if post.id in seen_ids:
                        continue

                    title = post.title or ""
                    body = post.selftext or ""

                    # 브랜드명 2차 필터 (검색 결과에서 관련 없는 글 제거)
                    if brand_lower not in f"{title} {body}".lower():
                        continue

                    seen_ids.add(post.id)
                    pub_dt = datetime.utcfromtimestamp(post.created_utc)
                    summary = body[:300].strip() if body else f"[Reddit] {title}"

                    articles.append(RawArticle(
                        title=title,
                        url=f"https://www.reddit.com{post.permalink}",
                        published=pub_dt,
                        summary=summary,
                        source_name=f"Reddit r/{sub_name}",
                        language="en",
                        brand_hint=brand,
                        country_hint=country,
                    ))
                    matched += 1

                logger.debug("[%s] r/%s → %d건", brand, sub_name, matched)
                time.sleep(1)  # Reddit rate limit 준수

            except PRAWException as e:
                logger.warning("Reddit API 오류 (%s/r/%s): %s", brand, sub_name, e)
            except Exception as e:
                logger.warning("Reddit 수집 오류 (%s/r/%s): %s", brand, sub_name, e)

        logger.info("Reddit 수집: %s → %d건", brand, len(articles))
        return articles
