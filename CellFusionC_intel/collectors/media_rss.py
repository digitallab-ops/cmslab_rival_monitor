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
    # ── 러시아·CIS 매체 (RSS 실응답 검증 완료 · 브랜드명 필터로 K뷰티만 추출) ──────
    {  # 러시아 리테일·화장품/향수 유통
        "key": "retail_ru",
        "name": "Retail.ru",
        "url": "https://www.retail.ru/rss/news/",
        "language": "ru",
    },
    {  # 러시아 리테일·이커머스(K-뷰티 수입·유통)
        "key": "new_retail_ru",
        "name": "New Retail",
        "url": "https://new-retail.ru/rss/",
        "language": "ru",
    },
    {  # 카자흐스탄 비즈니스·유통(Golden Apple 등)
        "key": "kursiv_kz",
        "name": "Kursiv Kazakhstan",
        "url": "https://kursiv.media/feed/",
        "language": "ru",
    },
    {  # 우즈베키스탄 신사업·이커머스(Uzum 등)
        "key": "spot_uz",
        "name": "Spot.uz",
        "url": "https://www.spot.uz/rss/",
        "language": "ru",
    },
    {  # 벨라루스 뷰티 리테일·이커머스
        "key": "belretail_by",
        "name": "BelRetail",
        "url": "https://belretail.by/rss",
        "language": "ru",
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
    """BeautyMatter / WWD Beauty / Glossy RSS 수집기.

    피드 35종은 브랜드·국가와 무관한 '글로벌' 소스라 collect() 호출마다 재파싱하면
    브랜드×국가 수만큼(예: 7×15=105) × 35피드 = 3,675회 중복 fetch + 피드당 sleep으로
    일일 수집의 최대 병목이 된다. → 수집 run 1회 동안 피드를 URL당 딱 한 번만 파싱해
    캐시하고(엔트리 재사용), 브랜드 필터만 메모리에서 반복한다. reset_cache()로 run 경계.
    """

    collector_type = "media_rss"

    def __init__(self):
        self._entry_cache: dict = {}      # url → [{"title","link","summary","entry"}...]

    def reset_cache(self):
        """수집 run 시작 시 호출 — 피드 캐시 비움(다음 run에 최신 피드 재파싱)."""
        self._entry_cache = {}

    def _feed_entries(self, feed_cfg: dict) -> list:
        """피드 엔트리 캐시 조회/파싱. run당 URL 1회만 네트워크 fetch."""
        url = feed_cfg["url"]
        if url in self._entry_cache:
            return self._entry_cache[url]
        entries = []
        try:
            feed = feedparser.parse(url, agent=_FEED_UA)
            for entry in feed.entries:
                title = getattr(entry, "title", "").strip()
                link = getattr(entry, "link", "").strip()
                summary = getattr(entry, "summary", "").strip()
                if not title or not link:
                    continue
                entries.append({"title": title, "link": link,
                                "summary": summary, "entry": entry})
            time.sleep(RSS_REQUEST_DELAY)     # 유니크 fetch당 1회만(브랜드마다 X)
        except Exception as e:
            logger.warning("미디어 RSS 피드 오류 (%s): %s", feed_cfg["key"], e)
        self._entry_cache[url] = entries
        return entries

    def collect(self, brand: str, country: str) -> list[RawArticle]:
        """브랜드명이 제목 또는 요약에 포함된 기사만 반환(캐시된 피드에서 필터)."""
        brand_lower = brand.lower()
        articles = []
        for feed_cfg in MEDIA_FEEDS:
            for e in self._feed_entries(feed_cfg):
                if brand_lower not in f"{e['title']} {e['summary']}".lower():
                    continue
                articles.append(RawArticle(
                    title=e["title"],
                    url=e["link"],
                    published=_parse_date(e["entry"]),
                    summary=e["summary"],
                    source_name=feed_cfg["name"],
                    language=feed_cfg["language"],
                    brand_hint=brand,
                    country_hint=country,
                ))
        logger.info("미디어 RSS 수집: %s → %d건 (%d개 피드, 캐시 %d)",
                    brand, len(articles), len(MEDIA_FEEDS), len(self._entry_cache))
        return articles
