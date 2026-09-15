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
from datetime import datetime, timedelta, timezone

import requests

from collectors.base_collector import BaseCollector, RawArticle
from config.settings import YOUTUBE_API_KEY, RSS_REQUEST_DELAY

logger = logging.getLogger(__name__)

YOUTUBE_SEARCH_URL = "https://www.googleapis.com/youtube/v3/search"
YOUTUBE_VIDEOS_URL = "https://www.googleapis.com/youtube/v3/videos"
RESULTS_PER_QUERY = 10          # search.list 최대 50, 비용 절감 위해 10
_PRIMARY_COUNTRY = "US"         # 이 국가 수집 시에만 실행 (전 국가 중복 방지)
_BUZZ_WINDOW_DAYS = 30          # 버즈 측정 창(최근 N일 내 조회수 상위 영상)


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


def _guess_lang(text: str) -> str:
    """제목 기준 대략적 언어(메타데이터용) — 언어 무제한 수집이라 'en' 고정은 부정확."""
    # 첫 글자에서 즉시 반환하면 '深澤辰哉(한자)+かな' 같은 일본어가 중국어로 오판되므로
    # 문자열 전체를 훑어 가나·한글을 먼저 판정하고, 한자는 마지막에 본다.
    o = [ord(c) for c in (text or "")]
    if any(0xAC00 <= c <= 0xD7A3 for c in o):           # 한글
        return "ko"
    if any(0x3040 <= c <= 0x30FF for c in o):           # 히라가나·가타카나 → 일본어 확정
        return "ja"
    if any(0x0600 <= c <= 0x06FF for c in o):           # 아랍문자
        return "ar"
    if any(0x4E00 <= c <= 0x9FFF for c in o):           # 한자만 → 중·일 구분 불가(zh로 추정)
        return "zh"
    return "en"


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

        # 최근 30일 중 '실제로 많이 본' 영상.
        #  · order=date는 갓 올라온 영상만 잡혀 조회수가 바닥 → 바이럴/앰배서더 효과 검증 불가
        #  · q에 'kbeauty'를 붙이면 회수율이 급감(메디힐 1건 vs 'skincare' 4건)하고
        #    정작 앰배서더 TVCM 같은 핵심 영상이 가려짐. 브랜드명만 쓰면 동음이의 노이즈
        #    (예: Amuse) → 'skincare' 조합이 회수율·정밀도 균형점(실측 비교로 결정).
        #  · relevanceLanguage=en은 일본·중동 등 현지 영상을 밀어내 글로벌 모니터링에 불리
        #    (같은 쿼터로 커버리지만 좁아짐) → 언어 무제한.
        since = (datetime.now(timezone.utc) - timedelta(days=_BUZZ_WINDOW_DAYS)
                 ).strftime("%Y-%m-%dT%H:%M:%SZ")
        params = {
            "key": YOUTUBE_API_KEY,
            "q": f"{brand} skincare",
            "part": "snippet",
            "type": "video",
            "order": "viewCount",
            "publishedAfter": since,
            "maxResults": RESULTS_PER_QUERY,
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
                    language=_guess_lang(f"{title} {desc}"),
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
