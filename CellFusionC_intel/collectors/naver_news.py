"""
Naver News Search API 수집기
- 한국 실시간 뉴스 (country == "KR"일 때만 동작)
- 무료: 1일 25,000 호출
- API 키 등록: https://developers.naver.com
- .env: NAVER_CLIENT_ID, NAVER_CLIENT_SECRET
"""

import logging
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
import re

import requests

from collectors.base_collector import BaseCollector, RawArticle
from config.brands import BRAND_KO_NAMES
from config.settings import NAVER_CLIENT_ID, NAVER_CLIENT_SECRET, RSS_REQUEST_DELAY

logger = logging.getLogger(__name__)

NAVER_NEWS_URL = "https://openapi.naver.com/v1/search/news.json"
RESULTS_PER_QUERY = 10  # 브랜드당 최대 수집 건수 (최대 100)


def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", unescape(text)).strip()


def _parse_naver_date(date_str: str) -> datetime:
    try:
        return parsedate_to_datetime(date_str).astimezone(timezone.utc).replace(tzinfo=None)
    except Exception:
        return datetime.utcnow()


class NaverNewsCollector(BaseCollector):
    """Naver News Search API 수집기 — KR 전용."""

    collector_type = "naver_news"

    def collect(self, brand: str, country: str) -> list[RawArticle]:
        if country.upper() != "KR":
            return []

        if not NAVER_CLIENT_ID or not NAVER_CLIENT_SECRET:
            logger.warning("Naver API 키 미설정 — NAVER_CLIENT_ID/SECRET 확인")
            return []

        headers = {
            "X-Naver-Client-Id": NAVER_CLIENT_ID,
            "X-Naver-Client-Secret": NAVER_CLIENT_SECRET,
        }

        # 검색어: 영문 브랜드명 + 한국어명 (있으면) OR 조합
        ko_names = BRAND_KO_NAMES.get(brand, [])
        queries = [brand] + ko_names  # e.g. ["Anua", "아누아"]

        articles: list[RawArticle] = []
        seen_urls: set[str] = set()

        for query in queries:
            try:
                resp = requests.get(
                    NAVER_NEWS_URL,
                    headers=headers,
                    params={"query": query, "display": RESULTS_PER_QUERY, "sort": "date"},
                    timeout=10,
                )
                resp.raise_for_status()
                items = resp.json().get("items", [])

                for item in items:
                    url = item.get("originallink") or item.get("link", "")
                    if not url or url in seen_urls:
                        continue
                    seen_urls.add(url)

                    title = _strip_html(item.get("title", ""))
                    description = _strip_html(item.get("description", ""))
                    pub_date = _parse_naver_date(item.get("pubDate", ""))

                    articles.append(RawArticle(
                        title=title,
                        url=url,
                        published=pub_date,
                        summary=description,
                        source_name="Naver News",
                        language="ko",
                        brand_hint=brand,
                        country_hint=country,
                    ))

                time.sleep(RSS_REQUEST_DELAY)

            except requests.HTTPError as e:
                logger.warning("Naver API HTTP 오류 (%s/%s): %s", brand, query, e)
            except Exception as e:
                logger.warning("Naver API 오류 (%s/%s): %s", brand, query, e)

        logger.info("Naver 수집: %s/KR → %d건", brand, len(articles))
        return articles
