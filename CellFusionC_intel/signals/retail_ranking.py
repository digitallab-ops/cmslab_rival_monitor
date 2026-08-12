"""
아마존 베스트셀러 리테일 랭킹 수집 (실판매 성과 신호 = 서사의 '잘 나간다' 고리).

경쟁 브랜드가 아마존 카테고리 베스트셀러에서 몇 위인지 + 별점·리뷰수 =
뉴스(공급)·검색(수요) 다음의 '실제로 팔리나' 실측. 미국 시장 핵심 채널.

무자격증(공개 베스트셀러 페이지) · requests+bs4 서버렌더 파싱 · 봇체크 없음 확인.
한계: 아마존US만(다국가 확장 여지), 상위 100위 내 K뷰티 브랜드만 매칭 저장.
"""

import os
import re
import time
import logging
from datetime import date

import requests
from bs4 import BeautifulSoup
from sqlalchemy import text

from config.settings import DB_SCHEMA
from config.brands import ALL_BRANDS
from storage.models import get_session

logger = logging.getLogger(__name__)

_UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/125.0 Safari/537.36",
    "Accept-Language": "en-US,en;q=0.9",
}

# 셀퓨전씨 핵심영역(특화): 선케어 + 베이스/BB(미국 주력). is_core=True로 강조·가중.
CORE_CATEGORIES = {"선케어", "BB크림", "CC크림", "파운데이션", "틴티드모이스처", "DD크림"}

# (아마존 노드ID, 국가, 카테고리 라벨) — 전 카테고리 커버(넓게) + 핵심영역 강조.
# 나쁜 노드는 런타임에 스킵. 아마존 실노드ID 확인 완료.
AMAZON_NODES = [
    # ── 핵심영역(선·베이스/BB) — 미국 주력 ──
    ("11062591",   "US", "선케어"),
    ("7792268011", "US", "BB크림"),
    ("7792269011", "US", "CC크림"),
    ("11058871",   "US", "파운데이션"),
    ("7792276011", "US", "틴티드모이스처"),
    ("7792270011", "US", "DD크림"),
    # ── 광역 K뷰티 스킨케어 맥락 ──
    ("11060451",   "US", "스킨케어"),
    ("11061301",   "US", "크림·모이스처"),
    ("11060901",   "US", "클렌저"),
    ("11061931",   "US", "토너"),
    ("11062031",   "US", "트리트먼트·마스크"),
    ("11061091",   "US", "스크럽"),
    ("11061941",   "US", "아이케어"),
]
_BS_URL = "https://www.amazon.com/gp/bestsellers/beauty/{node}?pg={pg}"

# 비모니터링이지만 아마존 상위 K뷰티 메이저(경쟁 지형 파악용 — 브랜드 추가 후보)
KBEAUTY_MAJORS = [
    "COSRX", "Medicube", "Biodance", "TirTir", "Laneige", "Isntree", "Some By Mi",
    "Haruharu", "Purito", "Pyunkang Yul", "Axis-Y", "Innisfree", "Iunik", "Skinfood",
    "Beauty of Joseon", "Anua",  # (모니터링과 겹쳐도 무해 — 매칭 사전 보강용)
]
# 매칭 까다로운 브랜드 별칭(정규화 후 부분일치)
_BRAND_ALIASES = {
    "Beauty of Joseon": ["beautyofjoseon"], "Dr.Jart+": ["drjart"],
    "Round Lab": ["roundlab"], "Roundlab": ["roundlab"], "Cos de Baha": ["cosdebaha"],
    "By Wishtrend": ["wishtrend"], "VT Cosmetics": ["vtcosmetics"], "Centellian24": ["centellian"],
    "b.plain": ["bplain"], "Some By Mi": ["somebymi"], "Pyunkang Yul": ["pyunkangyul"],
    "Axis-Y": ["axisy"],
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _build_dict() -> dict:
    """브랜드 → [정규화 키워드 목록]. 모니터링 + 메이저."""
    d: dict = {}
    for b in list(ALL_BRANDS) + KBEAUTY_MAJORS:
        keys = set(_BRAND_ALIASES.get(b, []))
        keys.add(_norm(b))
        d[b] = [k for k in keys if len(k) >= 4]
    return d


_BRAND_DICT = _build_dict()
_MONITORED = set(ALL_BRANDS)


def _match_brand(title: str) -> "str | None":
    n = _norm(title)
    for brand, keys in _BRAND_DICT.items():
        if any(k in n for k in keys):
            return brand
    return None


def _parse_reviews(txt: str) -> "int | None":
    m = re.search(r"([\d,]{1,9})", txt or "")
    if not m:
        return None
    try:
        return int(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _parse_rating(txt: str) -> "float | None":
    m = re.search(r"([\d.]+)\s*out of", txt or "")
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def _fetch_node(node: str, country: str, category: str, pages: int = 2) -> list[dict]:
    """한 카테고리 베스트셀러에서 매칭된 K뷰티 브랜드 행 추출."""
    out: list[dict] = []
    for pg in range(1, pages + 1):
        url = _BS_URL.format(node=node, pg=pg)
        try:
            r = requests.get(url, headers=_UA, timeout=20)
            if r.status_code != 200:
                logger.info("리테일 노드 %s(%s) status=%s → 스킵", node, category, r.status_code)
                break
            soup = BeautifulSoup(r.text, "lxml")
        except Exception as e:
            logger.warning("리테일 노드 %s 실패: %s", node, e)
            break
        faceouts = soup.select("div.zg-grid-general-faceout")
        if not faceouts:
            break
        for fo in faceouts:
            nm = fo.select_one(
                "div._cDEzb_p13n-sc-css-line-clamp-3_g3dy1, "
                "div._cDEzb_p13n-sc-css-line-clamp-4_2q2cc, span.a-size-base-plus")
            if not nm:
                continue
            title = nm.get_text(strip=True)
            brand = _match_brand(title)
            if not brand:
                continue
            rk = fo.find_previous(class_="zg-bdg-text")
            rank = None
            if rk:
                mrk = re.search(r"\d+", rk.get_text(strip=True))
                rank = int(mrk.group(0)) if mrk else None
            rating = _parse_rating((fo.select_one("span.a-icon-alt") or _Empty()).get_text(strip=True))
            rev_el = fo.select_one("span.a-size-small, a.a-size-small")
            reviews = _parse_reviews(rev_el.get_text(strip=True)) if rev_el else None
            link = fo.select_one("a.a-link-normal[href]")
            href = ("https://www.amazon.com" + link["href"].split("?")[0]) if link and link.get("href", "").startswith("/") else ""
            out.append({
                "retailer": "amazon", "country": country, "category": category,
                "brand": brand, "is_monitored": brand in _MONITORED,
                "is_core": category in CORE_CATEGORIES,
                "product_name": title[:300], "rank": rank,
                "rating": rating, "review_count": reviews, "product_url": href,
            })
        time.sleep(2)
    return out


class _Empty:
    def get_text(self, *a, **k): return ""


def _ensure_table(session) -> None:
    session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.retail_rankings (
            id BIGSERIAL PRIMARY KEY,
            retailer VARCHAR(20) NOT NULL DEFAULT 'amazon',
            country VARCHAR(4) NOT NULL,
            category VARCHAR(40),
            brand VARCHAR(100),
            is_monitored BOOLEAN DEFAULT FALSE,
            is_core BOOLEAN DEFAULT FALSE,
            product_name VARCHAR(300),
            rank INTEGER,
            rating REAL,
            review_count INTEGER,
            product_url TEXT,
            capture_date DATE NOT NULL,
            captured_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            UNIQUE(retailer, country, category, product_name, capture_date)
        )
    """))
    session.execute(text(
        f"ALTER TABLE {DB_SCHEMA}.retail_rankings "
        f"ADD COLUMN IF NOT EXISTS is_core BOOLEAN DEFAULT FALSE"))
    session.execute(text(
        f"CREATE INDEX IF NOT EXISTS ix_retail_brand_date "
        f"ON {DB_SCHEMA}.retail_rankings (brand, capture_date DESC)"))
    session.commit()


def _save(session, row: dict, cap_date: date) -> None:
    session.execute(text(f"""
        INSERT INTO {DB_SCHEMA}.retail_rankings
            (retailer, country, category, brand, is_monitored, is_core, product_name,
             rank, rating, review_count, product_url, capture_date)
        VALUES (:retailer, :country, :category, :brand, :is_monitored, :is_core, :product_name,
                :rank, :rating, :review_count, :product_url, :cap)
        ON CONFLICT (retailer, country, category, product_name, capture_date) DO UPDATE SET
            rank = EXCLUDED.rank, rating = EXCLUDED.rating, is_core = EXCLUDED.is_core,
            review_count = EXCLUDED.review_count, captured_at = NOW()
    """), {**row, "cap": cap_date})


def run() -> dict:
    """아마존 베스트셀러 리테일 랭킹 수집. 반환 {captured, monitored, by_brand}."""
    cap_date = date.today()
    captured, monitored = 0, 0
    by_brand: dict = {}
    session = get_session()
    try:
        _ensure_table(session)
        for node, country, category in AMAZON_NODES:
            try:
                rows = _fetch_node(node, country, category)
            except Exception as e:
                logger.warning("리테일 %s 노드 오류: %s", category, e)
                continue
            for row in rows:
                _save(session, row, cap_date)
                captured += 1
                if row["is_monitored"]:
                    monitored += 1
                by_brand.setdefault(row["brand"], []).append((category, row["rank"]))
            session.commit()
            logger.info("  리테일 %-12s → 매칭 %d건", category, len(rows))
    finally:
        session.close()
    logger.info("리테일 랭킹 수집: 저장 %d건(모니터링 %d) · 브랜드 %d",
                captured, monitored, len(by_brand))
    return {"captured": captured, "monitored": monitored, "by_brand": by_brand}


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    logging.basicConfig(level=logging.INFO)
    print(run())
