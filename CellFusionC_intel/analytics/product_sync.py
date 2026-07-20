"""
Cafe24 Admin API → 자사 제품 라인 자동 동기화

config/company_profile.md 의 <!-- AUTO:PRODUCTS:START/END --> 블록만 자동 갱신.
전략/포지셔닝 등 나머지(사람 판단 영역)는 건드리지 않는다.

실행: python cli.py sync-profile   (로컬 스케줄러 주 1회 권장)

필요 env:
  CAFE24_MALL_ID              몰 아이디 (https://{MALL_ID}.cafe24api.com)
  CAFE24_API_VERSION          API 버전 (기본 2024-06-01)
  # 토큰 획득 — 아래 둘 중 하나:
  CAFE24_ACCESS_TOKEN         (직접 토큰 주입 — 테스트/수동용. 2시간 만료 주의)
  또는 Mongo에서 최신 토큰 읽기(자동 갱신되는 기존 토큰 재사용, 스키마는 env로 지정):
  CAFE24_MONGO_URI, CAFE24_MONGO_DB, CAFE24_MONGO_COLLECTION,
  CAFE24_MONGO_TOKEN_FIELD (기본 access_token)
"""

import logging
import os
from collections import defaultdict

import requests

logger = logging.getLogger(__name__)

_PROFILE_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "company_profile.md")
_MARK_START = "<!-- AUTO:PRODUCTS:START"
_MARK_END = "<!-- AUTO:PRODUCTS:END -->"


# ── 토큰 획득 ────────────────────────────────────────────────────────────────

def _get_access_token() -> str:
    """직접 토큰(env) 우선, 없으면 Mongo에서 최신 토큰 읽기."""
    tok = os.getenv("CAFE24_ACCESS_TOKEN", "").strip()
    if tok:
        return tok

    uri = os.getenv("CAFE24_MONGO_URI", "").strip()
    if uri:
        try:
            from pymongo import MongoClient
            client = MongoClient(uri, serverSelectionTimeoutMS=5000)
            db = client[os.getenv("CAFE24_MONGO_DB", "")]
            coll = db[os.getenv("CAFE24_MONGO_COLLECTION", "")]
            field = os.getenv("CAFE24_MONGO_TOKEN_FIELD", "access_token")
            # 가장 최근 문서(_id 역순) 기준
            doc = coll.find_one(sort=[("_id", -1)])
            client.close()
            if doc and doc.get(field):
                return str(doc[field])
            raise ValueError(f"Mongo 토큰 문서/필드({field}) 없음")
        except Exception as e:
            raise RuntimeError(f"Mongo 토큰 읽기 실패: {e}")

    raise RuntimeError(
        "Cafe24 토큰 소스 미설정 — CAFE24_ACCESS_TOKEN 또는 CAFE24_MONGO_URI(+DB/COLLECTION) 필요"
    )


# ── Cafe24 조회 ──────────────────────────────────────────────────────────────

def _cafe24_get(path: str, params: dict) -> dict:
    mall = os.getenv("CAFE24_MALL_ID", "").strip()
    if not mall:
        raise RuntimeError("CAFE24_MALL_ID 미설정")
    url = f"https://{mall}.cafe24api.com/api/v2/admin/{path}"
    headers = {
        "Authorization": f"Bearer {_get_access_token()}",
        "Content-Type": "application/json",
        "X-Cafe24-Api-Version": os.getenv("CAFE24_API_VERSION", "2024-06-01"),
    }
    resp = requests.get(url, headers=headers, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def fetch_products() -> list:
    """판매중(selling='T')·진열(display='T') 상품 전체 (페이지네이션)."""
    out: list = []
    offset = 0
    while True:
        data = _cafe24_get("products", {"limit": 100, "offset": offset})
        batch = data.get("products", [])
        if not batch:
            break
        out.extend(batch)
        offset += len(batch)
        if len(batch) < 100:
            break
    return out


def fetch_categories() -> dict:
    """{category_no: category_name}."""
    result: dict = {}
    try:
        offset = 0
        while True:
            data = _cafe24_get("categories", {"limit": 100, "offset": offset})
            batch = data.get("categories", [])
            if not batch:
                break
            for c in batch:
                if c.get("category_no") is not None:
                    result[c["category_no"]] = c.get("category_name", "")
            offset += len(batch)
            if len(batch) < 100:
                break
    except Exception as e:
        logger.warning("카테고리 조회 실패(무시하고 진행): %s", e)
    return result


# ── 큐레이션: 355개 → 카테고리별 요약 블록 ────────────────────────────────────

def build_product_section(products: list, categories: dict) -> str:
    """판매중·진열 상품을 카테고리별로 묶어 대표 제품명 요약(프롬프트용)."""
    live = [
        p for p in products
        if str(p.get("selling", "T")).upper() != "F"
        and str(p.get("display", "T")).upper() != "F"
    ]
    by_cat: dict = defaultdict(list)
    for p in live:
        name = (p.get("product_name") or "").strip()
        if not name:
            continue
        # 상품의 categories 필드에서 카테고리명 추출 (스키마 유연 처리)
        cat_name = "기타"
        cats = p.get("categories")
        if isinstance(cats, list) and cats:
            first = cats[0]
            if isinstance(first, dict):
                cat_name = (categories.get(first.get("category_no"))
                            or first.get("category_name") or "기타")
            else:
                cat_name = categories.get(first) or "기타"
        by_cat[cat_name].append(name)

    total = len(live)
    lines = [f"- 실제 판매 제품 (Cafe24 자동 동기화, 판매·진열중 {total}종):"]
    for cat, names in sorted(by_cat.items(), key=lambda kv: -len(kv[1])):
        rep = ", ".join(names[:6])
        more = f" 외 {len(names)-6}종" if len(names) > 6 else ""
        lines.append(f"  · {cat}({len(names)}종): {rep}{more}")
    return "\n".join(lines)


# ── 프로필 파일의 AUTO 블록 교체 ─────────────────────────────────────────────

def _replace_auto_block(profile_text: str, new_body: str) -> str:
    s = profile_text.find(_MARK_START)
    e = profile_text.find(_MARK_END)
    if s == -1 or e == -1:
        raise RuntimeError("company_profile.md에 AUTO:PRODUCTS 마커가 없음")
    start_line_end = profile_text.find("\n", s)
    head = profile_text[: start_line_end + 1]
    tail = profile_text[e:]
    return f"{head}{new_body}\n{tail}"


def sync_company_profile() -> str:
    """Cafe24 → 큐레이션 → company_profile.md AUTO 블록 갱신. 요약 문자열 반환."""
    products = fetch_products()
    categories = fetch_categories()
    section = build_product_section(products, categories)

    with open(_PROFILE_PATH, encoding="utf-8") as f:
        text = f.read()
    updated = _replace_auto_block(text, section)
    with open(_PROFILE_PATH, "w", encoding="utf-8") as f:
        f.write(updated)

    logger.info("제품 프로필 동기화 완료 (%d종)", len(products))
    return section
