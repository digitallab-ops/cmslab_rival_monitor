"""
Reddit RSS 수집기 — API 키 불필요
- r/AsianBeauty, r/SkincareAddiction, r/KoreanBeauty 의 **최신글 피드**를 run당 1회씩만 받아
  모든 브랜드를 로컬에서 매칭한다.
- 소비자 커뮤니티 신호 (입점 소식, 바이럴, 리뷰 트렌드)
- 국가 비종속적 (country 무시, 글로벌 영어 커뮤니티)

왜 검색 RSS를 안 쓰나 (2026-11-12 교체):
  예전엔 브랜드마다 `search.rss?q=<브랜드>`를 때렸다. 브랜드 58개 × 서브레딧 3개 =
  하루 2,628회, 피크 분당 18회. Reddit이 이걸 몇 주에 걸쳐 조이다가 11-11에
  **검색 엔드포인트만** 막았다(403/429, x-ratelimit-remaining: 0.0).
  일반 최신글 피드(new.rss)는 그대로 200이다. 그래서 피드를 통째로 받아
  로컬에서 거른다 — 요청이 2,628회에서 3회로 준다.
"""

import logging
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser

from collectors.base_collector import BaseCollector, RawArticle
from config.settings import RSS_REQUEST_DELAY

logger = logging.getLogger(__name__)

SUBREDDITS = ["AsianBeauty", "SkincareAddiction", "KoreanBeauty"]

# 서브레딧 최신글 RSS — 인증 불필요. 검색(search.rss)은 익명 차단됨.
REDDIT_NEW_RSS = "https://www.reddit.com/r/{subreddit}/new.rss?limit=100"

HEADERS = {"User-Agent": "kbeauty-monitor/1.0 (RSS reader)"}

# 피드 캐시 수명(초). reset_cache()는 job_daily_tier1에서만 부른다 — 주간 풀스캔
# 같은 다른 잡은 비우지 않아서, 수명이 없으면 저녁 수집이 아침에 받은 100건을
# 그대로 다시 본다. 모든 잡이 reset을 기억해주길 기대하는 대신 스스로 늙게 둔다.
# 한 잡 안에서 브랜드 58개를 도는 데는 몇 초뿐이라 30분이면 넉넉하다.
FEED_TTL_SEC = 1800

# 짧은 브랜드명은 일반 영어 단어에 박혀 오탐을 낸다("Eum"이 "museum"에,
# "Abib"이 "Abibas"에). 부분일치 대신 단어 경계로 맞춘다.
_WORD_CACHE: dict = {}


def _brand_pattern(brand: str):
    """브랜드명을 단어 경계로 찾는 정규식. 공백·점은 유연하게 둔다
    (Beauty of Joseon / beautyofjoseon, S.NATURE / S NATURE)."""
    p = _WORD_CACHE.get(brand)
    if p is None:
        parts = [re.escape(t) for t in re.split(r"[\s._-]+", brand) if t]
        if not parts:
            return None
        body = r"[\s._-]*".join(parts)
        p = re.compile(rf"(?<![0-9A-Za-z]){body}(?![0-9A-Za-z])", re.IGNORECASE)
        _WORD_CACHE[brand] = p
    return p


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
    """Reddit 서브레딧 최신글 RSS 수집기 — API 키 불필요."""

    collector_type = "reddit"

    def __init__(self):
        self._entry_cache: dict = {}      # subreddit → (받은시각, [entry...])

    def reset_cache(self):
        """수집 run 시작 시 호출 — 다음 run에 최신 피드를 다시 받는다."""
        self._entry_cache = {}

    def _entries(self, sub: str) -> list:
        """서브레딧 최신글. 한 run 안에서 서브레딧마다 1회만 네트워크 fetch."""
        hit = self._entry_cache.get(sub)
        if hit is not None and (time.monotonic() - hit[0]) < FEED_TTL_SEC:
            return hit[1]

        entries: list = []
        url = REDDIT_NEW_RSS.format(subreddit=sub)
        try:
            feed = feedparser.parse(url, request_headers=HEADERS)

            # HTTP 오류를 반드시 밖으로 꺼낸다. Reddit은 403을 **형식이 멀쩡한
            # 빈 Atom 피드**로 돌려준다 — feedparser는 정상 파싱하고 bozo도 안 뜬다.
            # status를 안 보면 "차단됐다"가 "오늘 글이 없다"로 둔갑해서,
            # 수집이 이틀 죽어도 로그엔 '0건'만 찍힌다. 실제로 그렇게 당했다.
            # 글이 들어왔으면 상태코드와 무관하게 쓴다(리다이렉트 대응).
            # 0건일 때만 '없는 것'과 '못 받은 것'을 가른다.
            st = getattr(feed, "status", None)
            if not feed.entries and st is not None and st != 200:
                logger.warning(
                    "Reddit 피드 HTTP %s (r/%s) — 수집 0건은 '글이 없음'이 아니라 "
                    "'차단됨'입니다. 요청 빈도나 엔드포인트를 확인하세요.", st, sub)
                self._entry_cache[sub] = (time.monotonic(), [])
                return []

            entries = list(feed.entries)
            if not entries:
                logger.warning("Reddit 피드가 비어 있음 (r/%s, HTTP %s)", sub, st)

            time.sleep(RSS_REQUEST_DELAY)

        except Exception as e:
            logger.warning("Reddit RSS 오류 (r/%s): %s", sub, e)

        self._entry_cache[sub] = (time.monotonic(), entries)
        return entries

    def collect(self, brand: str, country: str) -> list[RawArticle]:
        articles: list[RawArticle] = []
        seen_urls: set[str] = set()
        pat = _brand_pattern(brand)
        if pat is None:
            return articles

        for sub in SUBREDDITS:
            matched = 0
            for entry in self._entries(sub):
                link = getattr(entry, "link", "").strip()
                title = getattr(entry, "title", "").strip()
                summary = getattr(entry, "summary", "").strip()

                if not link or link in seen_urls:
                    continue
                if not pat.search(f"{title} {summary}"):
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

        logger.info("Reddit RSS 수집: %s → %d건", brand, len(articles))
        return articles
