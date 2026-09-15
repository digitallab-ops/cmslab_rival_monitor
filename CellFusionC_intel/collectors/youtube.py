"""
YouTube Data API v3 수집기
- 브랜드 관련 최근 영상(제목·설명)을 인플루언서/바이럴 신호로 수집
- 무료: 1일 10,000 유닛 (search.list = 100유닛/콜)
- API 키: https://console.cloud.google.com → YouTube Data API v3 활성화
- .env: YOUTUBE_API_KEY (미설정 시 자동 스킵)
- 글로벌 커뮤니티 성격 → 국가 무관하게 1회만 수집 (country 게이트로 중복 방지)
"""

import logging
import time
from datetime import datetime, timezone

import requests

from collectors.base_collector import BaseCollector, RawArticle
from config.settings import YOUTUBE_API_KEY, RSS_REQUEST_DELAY

logger = logging.getLogger(__name__)

YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
RESULTS_PER_QUERY = 10          # search.list 최대 50, 비용 절감 위해 10
_PRIMARY_COUNTRY = "US"         # 이 국가 수집 시에만 실행 (전 국가 중복 방지)


def _record_buzz(brand: str, video_ids: list, titles: dict) -> None:
    """검색된 영상들의 조회수를 모아 '소셜 버즈' 지표로 적재.

    videos.list는 1유닛(최대 50건)이라 search.list(100유닛) 대비 부담 없음.
    영상을 기사로만 담으면 '몇 개 올라왔나'만 알 뿐 — 실제 반응(조회수)이 버즈의 핵심.
    """
    if not video_ids:
        return
    try:
        resp = requests.get(YOUTUBE_VIDEOS_URL, timeout=10, params={
            "key": YOUTUBE_API_KEY, "part": "statistics",
            "id": ",".join(video_ids[:50]),
        })
        resp.raise_for_status()
        views = {}
        for it in resp.json().get("items", []):
            st = it.get("statistics", {}) or {}
            try:
                views[it.get("id", "")] = int(st.get("viewCount", 0) or 0)
            except (TypeError, ValueError):
                continue
        if not views:
            return
        total = sum(views.values())
        top_id = max(views, key=views.get)
        from storage.models import get_session
        from storage.repository import upsert_social_metric
        s = get_session()
        try:
            upsert_social_metric(s, "youtube", brand, "recent_videos", len(views))
            upsert_social_metric(s, "youtube", brand, "recent_views", total)
            upsert_social_metric(s, "youtube", brand, "top_video_views", views[top_id],
                                 meta=(titles.get(top_id) or "")[:200])
        finally:
            s.close()
        logger.info("YouTube 버즈: %s → 영상 %d · 조회 %s (최고 %s)",
                    brand, len(views), f"{total:,}", f"{views[top_id]:,}")
    except Exception as e:
        logger.warning("YouTube 버즈 지표 스킵 (%s): %s", brand, e)


def _parse_yt_date(date_str: str) -> datetime:
    try:
        return datetime.fromisoformat(date_str.replace("Z", "+00:00")).astimezone(
            timezone.utc
        ).replace(tzinfo=None)
    except Exception:
        return datetime.utcnow()


class YouTubeCollector(BaseCollector):
    """YouTube Data API v3 수집기 — 브랜드 영상 신호 (US 파이프라인에서 1회)."""

    collector_type = "youtube"

    def collect(self, brand: str, country: str) -> list[RawArticle]:
        # 글로벌 소스 — 대표 국가 파이프라인에서만 1회 실행
        if country.upper() != _PRIMARY_COUNTRY:
            return []
        if not YOUTUBE_API_KEY:
            logger.debug("YouTube API 키 미설정 — 수집 스킵")
            return []

        params = {
            "key": YOUTUBE_API_KEY,
            "q": f"{brand} kbeauty",
            "part": "snippet",
            "type": "video",
            "order": "date",
            "maxResults": RESULTS_PER_QUERY,
            "relevanceLanguage": "en",
        }

        articles: list[RawArticle] = []
        try:
            resp = requests.get(YOUTUBE_SEARCH_URL, params=params, timeout=10)
            resp.raise_for_status()
            items = resp.json().get("items", [])

            brand_lower = brand.lower()
            vid_ids: list = []
            vid_titles: dict = {}
            for item in items:
                vid = item.get("id", {}).get("videoId", "")
                sn = item.get("snippet", {})
                title = (sn.get("title") or "").strip()
                desc = (sn.get("description") or "").strip()
                channel = (sn.get("channelTitle") or "").strip()
                if not vid or not title:
                    continue
                # 브랜드명이 제목/설명에 실제 등장하는 것만 (검색 노이즈 억제)
                if brand_lower not in f"{title} {desc}".lower():
                    continue
                vid_ids.append(vid)
                vid_titles[vid] = title

                articles.append(RawArticle(
                    title=title,
                    url=f"https://www.youtube.com/watch?v={vid}",
                    published=_parse_yt_date(sn.get("publishedAt", "")),
                    summary=desc[:500],
                    source_name=f"YouTube · {channel}" if channel else "YouTube",
                    language="en",
                    brand_hint=brand,
                    country_hint=country.upper(),
                ))

            _record_buzz(brand, vid_ids, vid_titles)   # 조회수 → 소셜 버즈 축
            time.sleep(RSS_REQUEST_DELAY)

        except requests.HTTPError as e:
            logger.warning("YouTube API HTTP 오류 (%s): %s", brand, e)
        except Exception as e:
            logger.warning("YouTube API 오류 (%s): %s", brand, e)

        logger.info("YouTube 수집: %s → %d건", brand, len(articles))
        return articles
