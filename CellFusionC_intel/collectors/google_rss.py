import time
import json as _json
import re as _re
from html import unescape as _unescape
import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus

import feedparser

from collectors.base_collector import BaseCollector, RawArticle
from config.brands import COUNTRIES, LOCALE_KEYWORDS
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




# ── 원문 본문 확보 ───────────────────────────────────────────────────────────
# 구글뉴스 RSS는 본문도 요약도 주지 않는다. link는 news.google.com 리디렉션이고
# summary는 '제목을 감싼 링크 HTML'일 뿐이다(실측 40건 전부 제목 반복).
# 그래서 지금껏 구글RSS 기사가 **제목 한 줄로만** 분류돼 왔다.
# 구글 batchexecute(Fbv4je)로 실제 발행사 URL을 복원한 뒤 본문을 긁는다.
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/122.0 Safari/537.36")
_BODY_HEADERS = {"User-Agent": _UA, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8"}
_BODY_MIN = 300      # 이보다 짧으면 본문으로 치지 않는다(안내문·광고만 걸린 것)
_BODY_MAX = 4000     # 분류는 1,000자만 쓰므로 이 정도면 충분
_BODY_SESSION = __import__("requests").Session()   # 커넥션 재사용


def _resolve_google_url(session, gurl):
    """news.google.com 기사 링크 → 실제 발행사 URL. 실패 시 None."""
    m = _re.search(r"/articles/([^?]+)", gurl or "")
    if not m:
        return None
    try:
        html = session.get(gurl, headers=_BODY_HEADERS, timeout=12).text
        sg = _re.search(r'data-n-a-sg="([^"]+)"', html)
        ts = _re.search(r'data-n-a-ts="([^"]+)"', html)
        if not (sg and ts):
            return None
        inner = _json.dumps([
            "garturlreq",
            [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1,
              None, None, None, None, None, 0, 1],
             "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
            m.group(1), int(ts.group(1)), sg.group(1)])
        r = session.post(
            "https://news.google.com/_/DotsSplashUi/data/batchexecute",
            data={"f.req": _json.dumps([[["Fbv4je", inner, None, "general"]]])},
            headers={**_BODY_HEADERS,
                     "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"},
            timeout=15)
        for line in r.text.split(chr(10)):
            if "garturlres" not in line:
                continue
            for part in _json.loads(line):
                if isinstance(part, list) and len(part) > 2 and part[1] == "Fbv4je":
                    return _json.loads(part[2])[1]
    except Exception as e:
        logger.debug("구글 URL 복원 실패: %s", e)
    return None


def _extract_body(html):
    """기사 HTML → 본문 텍스트.

    사이트마다 구조가 달라 article/div 컨테이너를 정규식으로 특정하려 하면 오히려
    본문을 통째로 놓친다(실측 6곳 중 6곳 실패). 스크립트·네비만 걷어내고
    문단 태그를 모으는 쪽이 국내·해외 모두에서 안정적이었다.
    """
    h = _re.sub(r"(?is)<(script|style|noscript|nav|header|footer|aside|form|figure)[^>]*>.*?</\1>",
                " ", html or "")
    parts = _re.findall(r"(?is)<(?:p|h2|h3|li)[^>]*>(.*?)</(?:p|h2|h3|li)>", h)
    txt = " ".join(_re.sub(r"<[^>]+>", " ", p) for p in parts)
    txt = _re.sub(r"\\s+", " ", _unescape(txt)).strip()
    if len(txt) < 200:                    # 문단 태그를 안 쓰는 사이트 폴백
        plain = _re.sub(r"(?is)<[^>]+>", " ", h)
        txt = _re.sub(r"\\s+", " ", _unescape(plain)).strip()
    return txt[:_BODY_MAX]


def fetch_article_body(session, gurl):
    """구글뉴스 링크 → (실제 URL, 본문). 실패하면 (None, "")."""
    real = _resolve_google_url(session, gurl)
    if not real:
        return None, ""
    try:
        r = session.get(real, headers=_BODY_HEADERS, timeout=12)
        if r.status_code != 200:
            return real, ""
        body = _extract_body(r.text)
        return real, (body if len(body) >= _BODY_MIN else "")
    except Exception as e:
        logger.debug("본문 수집 실패: %s", e)
        return real, ""


_BODY_WORKERS = 6        # 동시 요청 수 — 발행사 서버 부담과 속도의 절충
_BODY_LIMIT = 40         # 한 조합에서 본문을 시도할 최대 건수(시간 상한)


def _fill_bodies(articles) -> int:
    """수집한 기사들의 실제 URL·본문을 병렬로 채운다. 반환: 본문 확보 건수."""
    from concurrent.futures import ThreadPoolExecutor
    targets = [a for a in articles if "news.google.com" in (a.url or "")][:_BODY_LIMIT]
    if not targets:
        return 0

    def _one(a):
        try:
            real, body = fetch_article_body(_BODY_SESSION, a.url)
            if real:
                a.url = real
            if body:
                a.body = body
                return 1
        except Exception as e:
            logger.debug("본문 채우기 실패: %s", e)
        return 0

    with ThreadPoolExecutor(max_workers=_BODY_WORKERS) as ex:
        return sum(ex.map(_one, targets))


def _clean_rss_summary(raw: str, title: str) -> str:
    """구글뉴스 RSS summary에서 마크업을 걷고, 제목 반복이면 빈 문자열."""
    import re as _re
    from html import unescape as _un
    t = _un(_re.sub(r"<[^>]+>", " ", raw or ""))
    t = _re.sub(r"[\s ]+", " ", t).strip()
    if not t:
        return ""
    # 제목(및 ' - 발행사' 꼬리)을 빼고 남는 게 거의 없으면 정보가 아니다
    base = _re.sub(r"\s*-\s*[^-]{1,40}$", "", (title or "")).strip()
    rest = t.replace(base, "").replace(title or "", "")
    rest = _re.sub(r"[\s ]+", " ", rest).strip()
    return t if len(rest) >= 40 else ""


class GoogleRSSCollector(BaseCollector):
    collector_type = "google_rss"

    def collect(self, brand: str, country: str) -> list[RawArticle]:
        country_cfg = COUNTRIES.get(country.upper())
        if not country_cfg:
            logger.warning("미지원 국가 코드: %s", country)
            return []

        n_body = 0          # 본문까지 확보한 건수(로그용)
        queries = [f'"{brand}" beauty']
        if DEEP_QUERY:
            queries.append(f'"{brand}" ({_ACTIVITY_TERMS})')
            # 현지어 키워드 쿼리 (해당 국가 언어권 기사 recall 보강) — 주간 심층수집만
            loc_kw = LOCALE_KEYWORDS.get(country.upper())
            if loc_kw:
                terms = " OR ".join(f'"{k}"' for k in loc_kw)
                queries.append(f'"{brand}" ({terms})')

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
                # 구글뉴스 RSS의 summary는 본문이 아니라 '제목을 감싼 링크 HTML'이다.
                #   <a href="...">제목</a>&nbsp;<font color="#6f6f6f">발행사</font>
                # 평균 326자인데 전부 마크업이라, 그대로 넘기면 분류기 프롬프트의
                # '요약' 칸이 제목의 HTML 복사본으로 채워져 토큰만 먹고 정보는 0이다.
                # 태그를 걷어 제목과 다른 내용이 남을 때만 요약으로 쓴다.
                summary = _clean_rss_summary(getattr(entry, "summary", ""), title)
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

        # 본문은 마지막에 한 번에 채운다. 건별 순차 요청은 한 조합에 2분을 넘겼다
        # (URL 복원 1회 + 본문 1회 = 건당 왕복 2번). 스레드로 겹쳐 돌린다.
        n_body = _fill_bodies(articles)
        logger.info("수집 완료: %s/%s → %d건 (쿼리 %d개 · 본문 %d건)",
                    brand, country, len(articles), len(queries), n_body)
        return articles
