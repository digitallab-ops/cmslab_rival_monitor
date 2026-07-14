import time
import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import feedparser

from collectors.base_collector import BaseCollector, RawArticle
from config.brands import COUNTRIES
from config.settings import RSS_REQUEST_DELAY

logger = logging.getLogger(__name__)

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search?q={query}&hl={hl}&gl={gl}&ceid={ceid}"

# 전략 활동 지향 보조 쿼리(유통·진출·투자·협업 신호 커버리지 보강).
# DEEP_QUERY=True일 때만 추가 실행 → 비용 절감 위해 주간 풀스캔에서만 켬(일별은 1쿼리).
_ACTIVITY_TERMS = "launch OR Sephora OR Ulta OR expansion OR funding OR partnership OR flagship OR collaboration"
DEEP_QUERY = False


def _parse_date(entry) -> datetime:
    for field in ("published", "updated"):
        val = getattr(entry, field, None)
        if val:
            try:
                return parsedate_to_datetime(val).astimezone(timezone.utc).replace(tzinfo=None)
            except Exception:
                pass
    return datetime.utcnow()


class GoogleRSSCollector(BaseCollector):
    collector_type = "google_rss"

    def collect(self, brand: str, country: str) -> list[RawArticle]:
        country_cfg = COUNTRIES.get(country.upper())
        if not country_cfg:
            logger.warning("미지원 국가 코드: %s", country)
            return []

        queries = [f'"{brand}" beauty']
        if DEEP_QUERY:
            queries.append(f'"{brand}" ({_ACTIVITY_TERMS})')

        seen_links: set[str] = set()
        articles: list[RawArticle] = []
        for q in queries:
            url = GOOGLE_NEWS_RSS.format(
                query=quote_plus(q),
                hl=country_cfg["hl"],
                gl=country_cfg["gl"],
                ceid=country_cfg["ceid"],
            )
            try:
                feed = feedparser.parse(url)
            except Exception as e:
                logger.error("RSS 파싱 오류 (%s/%s): %s", brand, country, e)
                continue

            for entry in feed.entries:
                title = getattr(entry, "title", "").strip()
                link = getattr(entry, "link", "").strip()
                summary = getattr(entry, "summary", "").strip()
                source = getattr(entry, "source", {})
                source_name = source.get("title", "") if isinstance(source, dict) else ""

                if not title or not link or link in seen_links:
                    continue
                seen_links.add(link)

                articles.append(
                    RawArticle(
                        title=title,
                        url=link,
                        published=_parse_date(entry),
                        summary=summary,
                        source_name=source_name,
                        language=country_cfg["hl"],
                        brand_hint=brand,
                        country_hint=country.upper(),
                    )
                )
            time.sleep(RSS_REQUEST_DELAY)

        logger.info("수집 완료: %s/%s → %d건 (쿼리 %d개)", brand, country, len(articles), len(queries))
        return articles
