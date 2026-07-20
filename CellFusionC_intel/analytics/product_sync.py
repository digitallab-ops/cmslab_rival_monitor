"""
자사 제품 라인 자동 동기화 — Cafe24 카탈로그 요약 API

소스: GET {PRODUCT_CATALOG_URL}  (기본: 사내 Cafe24 프록시의 ?summary=true)
      → {brand, sells:[...], categories:[{category, product_count, examples:[...]}]}
      인증 불필요(프록시가 Cafe24 OAuth를 내부 처리). 우리는 GET 1회만.

config/company_profile.md 의 <!-- AUTO:PRODUCTS:START/END --> 블록만 자동 갱신.
전략/포지셔닝(사람 판단)은 건드리지 않는다.

실행: python cli.py sync-profile   (로컬 스케줄러 주 1회 자동)
env:  PRODUCT_CATALOG_URL (미설정 시 기본 URL 사용)
"""

import logging
import os

import requests

logger = logging.getLogger(__name__)

_PROFILE_PATH = os.path.join(os.path.dirname(__file__), "..", "config", "company_profile.md")
_MARK_START = "<!-- AUTO:PRODUCTS:START"
_MARK_END = "<!-- AUTO:PRODUCTS:END -->"

_DEFAULT_URL = "https://cafe24-api.onrender.com/cafe24/catalog?summary=true"


def fetch_catalog_summary() -> dict:
    """카탈로그 요약 조회. {brand, sells, categories}."""
    url = os.getenv("PRODUCT_CATALOG_URL", _DEFAULT_URL)
    # Render 무료플랜 콜드스타트 대비 넉넉한 타임아웃
    resp = requests.get(url, timeout=60)
    resp.raise_for_status()
    return resp.json()


def build_product_section(summary: dict) -> str:
    """요약 JSON → company_profile 제품 블록(프롬프트용 큐레이션 텍스트)."""
    brand = summary.get("brand", "CellFusionC")
    sells = summary.get("sells", []) or []
    cats = summary.get("categories", []) or []

    lines = []
    if sells:
        lines.append(f"- 실제 판매 라인 (Cafe24 자동 동기화) — {brand}: " + "·".join(sells))
    else:
        lines.append(f"- 실제 판매 라인 (Cafe24 자동 동기화) — {brand}")
    # 카테고리를 종수 많은 순으로 (미분류/빈 카테고리는 노이즈라 프로필에서 제외)
    def _skip(name: str) -> bool:
        n = (name or "").strip().strip("()")
        return (not n) or ("미분류" in n) or (n == "기타")

    for c in sorted(cats, key=lambda x: -(x.get("product_count") or 0)):
        cat = c.get("category", "기타")
        if _skip(cat):
            continue
        cnt = c.get("product_count") or 0
        ex = ", ".join((c.get("examples") or [])[:5])
        ex_str = f": {ex}" if ex else ""
        lines.append(f"  · {cat}({cnt}종){ex_str}")
    return "\n".join(lines)


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
    """카탈로그 요약 → company_profile.md AUTO 블록 갱신. 갱신된 제품 블록 반환."""
    summary = fetch_catalog_summary()
    section = build_product_section(summary)

    with open(_PROFILE_PATH, encoding="utf-8") as f:
        text = f.read()
    updated = _replace_auto_block(text, section)
    with open(_PROFILE_PATH, "w", encoding="utf-8") as f:
        f.write(updated)

    n = sum((c.get("product_count") or 0) for c in (summary.get("categories") or []))
    logger.info("제품 프로필 동기화 완료 (%d종, %d카테고리)", n, len(summary.get("categories") or []))
    return section
