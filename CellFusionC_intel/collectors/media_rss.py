"""
뷰티 전문 미디어 RSS 수집기
- 글로벌 뷰티 업계지: BeautyMatter, WWD Beauty, Glossy
- 글로벌 뷰티 전문: Global Cosmetics News, CosmeticsDesign Asia/Europe
- 보도자료 서비스: PR Newswire, BusinessWire Cosmetics
- 지역 미디어: WWD Japan, Korea Herald, SCMP Lifestyle, Nikkei Asia
- 각 피드에서 최신 기사를 가져와 브랜드명 언급 여부로 필터링
- 국가 비종속적 글로벌 미디어 → country는 GPT가 기사 내용에서 판단
"""

import logging
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser

from collectors.base_collector import BaseCollector, RawArticle
from config.settings import RSS_REQUEST_DELAY

logger = logging.getLogger(__name__)

# 일부 매체는 기본 feedparser UA를 차단 → 브라우저 UA로 위장
_FEED_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0 Safari/537.36"

MEDIA_FEEDS = [
    # ── 기존: 미국 뷰티 업계지 ──────────────────────────────────────────
    {
        "key": "beautymatter",
        "name": "BeautyMatter",
        "url": "https://beautymatter.com/feed/",
        "language": "en",
    },
    {
        "key": "wwd",
        "name": "WWD Beauty",
        "url": "https://wwd.com/beauty-industry-news/feed/",
        "language": "en",
    },
    {
        "key": "glossy",
        "name": "Glossy",
        "url": "https://www.glossy.co/feed/",
        "language": "en",
    },
    # ── 글로벌 뷰티 전문 미디어 ────────────────────────────────────────
    {
        "key": "global_cosmetics_news",
        "name": "Global Cosmetics News",
        "url": "https://www.globalcosmeticsnews.com/feed/",
        "language": "en",
    },
    {
        "key": "cosmetics_business",
        "name": "Cosmetics Business",
        "url": "https://www.cosmeticsbusiness.com/rss",
        "language": "en",
    },
    {
        "key": "premium_beauty_news",
        "name": "Premium Beauty News",
        "url": "https://www.premiumbeautynews.com/spip.php?page=backend",
        "language": "en",
    },
    {
        "key": "allure",
        "name": "Allure",
        "url": "https://www.allure.com/feed/rss",
        "language": "en",
    },
    {
        "key": "cosmeticsdesign_asia",
        "name": "CosmeticsDesign Asia",
        "url": "https://www.cosmeticsdesign-asia.com/Info/CosmeticsDesign-Asia-RSS",
        "language": "en",
    },
    {
        "key": "cosmeticsdesign_europe",
        "name": "CosmeticsDesign Europe",
        "url": "https://www.cosmeticsdesign-europe.com/Info/CosmeticsDesign-Europe-RSS",
        "language": "en",
    },
    # ── 글로벌 보도자료 서비스 ─────────────────────────────────────────
    {
        "key": "prnewswire",
        "name": "PR Newswire",
        "url": "https://www.prnewswire.com/rss/news-releases-list.rss",
        "language": "en",
    },
    {
        "key": "businesswire_cosmetics",
        "name": "BusinessWire Cosmetics",
        "url": "https://feed.businesswire.com/rss/home/?rss=G1&rssid=1080",
        "language": "en",
    },
    # ── 지역 미디어 ────────────────────────────────────────────────────
    {
        "key": "wwdjapan",
        "name": "WWD Japan Beauty",
        "url": "https://www.wwdjapan.com/category/beauty/feed",
        "language": "ja",
    },
    {
        "key": "korea_herald",
        "name": "Korea Herald",
        "url": "https://www.koreaherald.com/common/rss.php",
        "language": "en",
    },
    {
        "key": "scmp_lifestyle",
        "name": "SCMP Lifestyle",
        "url": "https://www.scmp.com/rss/91/feed",
        "language": "en",
    },
    {
        "key": "nikkei_asia",
        "name": "Nikkei Asia",
        "url": "https://asia.nikkei.com/rss/feed/nar",
        "language": "en",
    },
    # ── 가이드 신규 권역 매체 (RSS 실응답 검증 완료) ──────────────────────
    {  # 영국 뷰티 리테일
        "key": "theindustry_beauty",
        "name": "TheIndustry.beauty",
        "url": "https://theindustry.beauty/feed/",
        "language": "en",
    },
    {  # Benelux·서유럽 리테일
        "key": "retaildetail",
        "name": "RetailDetail",
        "url": "https://www.retaildetail.eu/en/feed/",
        "language": "en",
    },
    {  # 동남아 리테일
        "key": "inside_retail_asia",
        "name": "Inside Retail Asia",
        "url": "https://insideretail.asia/feed/",
        "language": "en",
    },
    {  # 인도 광고·마케팅
        "key": "et_brandequity",
        "name": "ET BrandEquity",
        "url": "https://brandequity.economictimes.indiatimes.com/rss/topstories",
        "language": "en",
    },
    {  # 중동(GCC) 캠페인
        "key": "campaign_me",
        "name": "Campaign Middle East",
        "url": "https://campaignme.com/feed/",
        "language": "en",
    },
    {  # 남아공·범아프리카
        "key": "bizcommunity",
        "name": "Bizcommunity",
        "url": "https://www.bizcommunity.com/rss/196/1.html",
        "language": "en",
    },
    # ── 확장시장 P1/P2 보강 (2026-07 RSS 실응답 검증 — 얇은 권역 강화) ──────
    {  # 이탈리아 뷰티 전문 (P2)
        "key": "pambianco_beauty",
        "name": "Pambianco Beauty",
        "url": "https://beauty.pambianconews.com/feed/",
        "language": "it",
    },
    {  # 폴란드·CEE 화장품 전문 (P1)
        "key": "wiadomosci_kosmetyczne",
        "name": "Wiadomosci Kosmetyczne",
        "url": "https://www.wiadomoscikosmetyczne.pl/feed",
        "language": "pl",
    },
    {  # 브라질 리테일·유통 (P1)
        "key": "mercado_consumo",
        "name": "Mercado & Consumo",
        "url": "https://mercadoeconsumo.com.br/feed/",
        "language": "pt",
    },
    {  # 브라질 화장품 전문 (P2)
        "key": "cosmetic_innovation_br",
        "name": "Cosmetic Innovation BR",
        "url": "https://cosmeticinnovation.com.br/feed/",
        "language": "pt",
    },
    {  # 중동(GCC) 브랜드·리테일 미디어 (P1)
        "key": "communicate_me",
        "name": "Communicate Middle East",
        "url": "https://communicateonline.me/feed/",
        "language": "en",
    },
    {  # UAE·사우디 비즈니스 (P2 — 대형 진출·투자)
        "key": "arabian_business",
        "name": "Arabian Business",
        "url": "https://www.arabianbusiness.com/feed",
        "language": "en",
    },
    {  # 인도 광고·브랜드 전략 (P1)
        "key": "afaqs",
        "name": "afaqs!",
        "url": "https://www.afaqs.com/rss",
        "language": "en",
    },
    {  # 나이지리아 캠페인·마케팅 (P1)
        "key": "brand_communicator_ng",
        "name": "Brand Communicator",
        "url": "https://brandcom.ng/feed",
        "language": "en",
    },
    {  # 인도네시아 뷰티 (P2)
        "key": "female_daily_id",
        "name": "Female Daily",
        "url": "https://editorial.femaledaily.com/feed",
        "language": "id",
    },
]


def _parse_date(entry) -> datetime:
    for field in ("published", "updated"):
        val = getattr(entry, field, None)
        if val:
            try:
                return parsedate_to_datetime(val).astimezone(timezone.utc).replace(tzinfo=None)
            except Exception:
                pass
    return datetime.utcnow()


class MediaRSSCollector(BaseCollector):
    """BeautyMatter / WWD Beauty / Glossy RSS 수집기."""

    collector_type = "media_rss"

    def collect(self, brand: str, country: str) -> list[RawArticle]:
        """브랜드명이 제목 또는 요약에 포함된 기사만 반환."""
        brand_lower = brand.lower()
        articles = []

        for feed_cfg in MEDIA_FEEDS:
            try:
                feed = feedparser.parse(feed_cfg["url"], agent=_FEED_UA)
                matched = 0
                for entry in feed.entries:
                    title = getattr(entry, "title", "").strip()
                    link = getattr(entry, "link", "").strip()
                    summary = getattr(entry, "summary", "").strip()

                    if not title or not link:
                        continue

                    if brand_lower not in f"{title} {summary}".lower():
                        continue

                    articles.append(RawArticle(
                        title=title,
                        url=link,
                        published=_parse_date(entry),
                        summary=summary,
                        source_name=feed_cfg["name"],
                        language=feed_cfg["language"],
                        brand_hint=brand,
                        country_hint=country,
                    ))
                    matched += 1

                logger.debug("[%s] %s → %d건 매칭", brand, feed_cfg["name"], matched)
                time.sleep(RSS_REQUEST_DELAY)

            except Exception as e:
                logger.warning("미디어 RSS 오류 (%s/%s): %s", feed_cfg["key"], brand, e)

        logger.info("미디어 RSS 수집: %s → %d건 (%d개 피드)", brand, len(articles), len(MEDIA_FEEDS))
        return articles
