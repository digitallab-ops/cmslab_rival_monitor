"""
MCP 서버 — 경쟁 인텔리전스 데이터를 LLM 툴로 노출.

Slack 봇(gpt-4o 함수호출)이 이 툴들을 호출해 셀퓨전씨 관점으로 답변한다.
기존 analytics/queries·summarizer를 그대로 감싼 얇은 계층 (조회 전용).

FastAPI(server.py)에 streamable-http로 마운트되며(/mcp), stateless 모드.
로컬 단독 실행: `python mcp_server.py` (기본 8100포트).
"""

import logging

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from sqlalchemy import text

from storage.models import get_session
from config.settings import DB_SCHEMA
from config.brands import TIER1_BRANDS, TIER2_BRANDS, BRAND_KO_NAMES, COUNTRIES
from analytics.queries import (
    get_brand_insights_raw,
    get_category_battle,
    get_expansion_playbook,
    compute_brand_momentum,
    get_demand_triangulation,
)
from analytics.summarizer import generate_brand_strategy_summary

logger = logging.getLogger(__name__)

rival_mcp = FastMCP(
    "cellfusionc-rival-intel",
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
    # Render 프록시 뒤에서 Host 헤더가 도메인이라 기본 DNS 리바인딩 보호가 421 거부함.
    # 프록시 신뢰 + 자체 Bearer(MCP_API_KEY) 보호로 대체 → 보호 비활성.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    instructions=(
        "씨엠에스랩(더마 선케어 브랜드 '셀퓨전씨')의 K-뷰티 경쟁사 인텔리전스 데이터.\n"
        "경쟁 브랜드의 해외 활동(신시장 진출·유통 채널·신제품·인플루언서·투자 등)을 "
        "수집·분류·번역한 결과를 조회한다. 답변은 항상 우리(셀퓨전씨) 관점의 시사점으로 연결할 것."
    ),
)

# 한국어 국가명 → ISO 코드 (get_market_intel 입력 유연화)
_KO_TO_CC = {v["name"]: k for k, v in COUNTRIES.items()}
_CC_TO_KO = {k: v["name"] for k, v in COUNTRIES.items()}


def _resolve_country(country: str) -> str:
    """ISO 코드 또는 한국어 국가명 → ISO 코드."""
    c = (country or "").strip()
    if c.upper() in COUNTRIES:
        return c.upper()
    return _KO_TO_CC.get(c, c.upper())


def _resolve_brand(raw: dict, brand: str) -> str | None:
    """대소문자/부분일치로 브랜드 키 해석."""
    b = (brand or "").strip().lower()
    if not b:
        return None
    exact = next((k for k in raw if k.lower() == b), None)
    if exact:
        return exact
    # 한국어명 매칭
    for eng, kos in BRAND_KO_NAMES.items():
        if any(b == ko.lower() or b in ko.lower() for ko in kos) and eng in raw:
            return eng
    return next((k for k in raw if b in k.lower()), None)


@rival_mcp.tool()
def list_brands() -> dict:
    """추적 중인 경쟁 브랜드 목록. Tier1=매일 수집, Tier2=주간. 다른 툴의 brand 인자로 영문명을 쓴다."""
    return {
        "tier1_daily": TIER1_BRANDS,
        "tier2_weekly": TIER2_BRANDS,
        "korean_names": BRAND_KO_NAMES,
        "covered_countries": _CC_TO_KO,
    }


@rival_mcp.tool()
def get_brand_intel(brand: str, days: int = 30) -> dict:
    """특정 경쟁 브랜드의 최근 활동 + 전략 요약(셀퓨전씨 관점).

    brand: 영문 브랜드명(예: 'Anua', 'Beauty of Joseon') 또는 한국어명('아누아').
    days: 조회 기간(일). 기본 30.
    """
    s = get_session()
    try:
        raw = get_brand_insights_raw(s, days=days)
        key = _resolve_brand(raw, brand)
        if not key:
            return {"brand": brand, "found": False,
                    "note": f"최근 {days}일 활동이 없거나 이름 불일치. list_brands로 정확한 영문명 확인."}
        d = raw[key]
        summary = generate_brand_strategy_summary(key, d.get("articles", []))
        return {
            "brand": key, "found": True, "days": days,
            "top_activity": d["top_act"], "high_ratio_pct": d["high_pct"],
            "top_markets": [{"country": c, "count": n} for c, n in d["top_countries"]],
            "recent_articles": [
                {"importance": a["imp"], "activity": a["act"], "title": a["title_ko"],
                 "country": a["country"], "date": a["date"], "url": a["url"]}
                for a in d.get("articles", [])
            ],
            "strategy_summary": summary,
        }
    finally:
        s.close()


@rival_mcp.tool()
def get_market_intel(country: str, days: int = 30) -> dict:
    """특정 국가(시장)에서 경쟁사들이 무엇을 하고 있는지 요약.

    country: ISO 코드(US, VN, BR, JP...) 또는 한국어 국가명('베트남', '브라질').
    days: 조회 기간(일). 기본 30.
    """
    cc = _resolve_country(country)
    s = get_session()
    try:
        rows = s.execute(text(f"""
            SELECT brand, activity_type, importance,
                   COALESCE(NULLIF(title_ko,''), title) AS ttl,
                   COALESCE(channel,''), source_url,
                   published_date::date::text, COALESCE(strategic_score,0)
            FROM {DB_SCHEMA}.news_articles
            WHERE (is_duplicate IS NOT TRUE)
              AND country = :cc
              AND importance IN ('high','medium')
              AND (brand_focus != 'incidental' OR brand_focus IS NULL)
              AND published_date >= NOW() - (:days || ' days')::interval
            ORDER BY COALESCE(strategic_score,0) DESC, published_date DESC
            LIMIT 25
        """), {"cc": cc, "days": days}).fetchall()
        by_brand: dict = {}
        moves = []
        for r in rows:
            by_brand[r[0]] = by_brand.get(r[0], 0) + 1
            moves.append({"brand": r[0], "activity": r[1], "importance": r[2],
                          "title": r[3], "channel": r[4], "url": r[5],
                          "date": r[6], "score": r[7]})
        return {
            "country": cc, "country_ko": _CC_TO_KO.get(cc, cc), "days": days,
            "total_moves": len(moves),
            "active_brands": sorted(by_brand.items(), key=lambda x: -x[1]),
            "top_moves": moves[:12],
        }
    finally:
        s.close()


@rival_mcp.tool()
def search_news(query: str, brand: str = "", country: str = "",
                days: int = 60, limit: int = 15) -> dict:
    """경쟁사 뉴스 자유 검색. 키워드가 제목/한국어제목/본문에 포함된 기사 반환.

    query: 검색어(한국어/영어). brand·country(선택)로 필터. days 기본 60.
    """
    s = get_session()
    try:
        clauses = ["(is_duplicate IS NOT TRUE)",
                   "published_date >= NOW() - (:days || ' days')::interval"]
        params: dict = {"days": days, "limit": max(1, min(limit, 30))}
        if query.strip():
            clauses.append("(title ILIKE :q OR title_ko ILIKE :q OR details ILIKE :q)")
            params["q"] = f"%{query.strip()}%"
        if brand.strip():
            clauses.append("brand ILIKE :b")
            params["b"] = f"%{brand.strip()}%"
        if country.strip():
            clauses.append("country = :cc")
            params["cc"] = _resolve_country(country)
        rows = s.execute(text(f"""
            SELECT brand, country, activity_type, importance,
                   COALESCE(NULLIF(title_ko,''), title), source_url,
                   published_date::date::text, COALESCE(details,'')
            FROM {DB_SCHEMA}.news_articles
            WHERE {' AND '.join(clauses)}
            ORDER BY published_date DESC
            LIMIT :limit
        """), params).fetchall()
        return {
            "query": query, "count": len(rows),
            "results": [
                {"brand": r[0], "country": r[1], "activity": r[2], "importance": r[3],
                 "title": r[4], "url": r[5], "date": r[6], "details": (r[7] or "")[:200]}
                for r in rows
            ],
        }
    finally:
        s.close()


@rival_mcp.tool()
def get_category_battle_view(days: int = 30) -> dict:
    """우리(셀퓨전씨) 카테고리별로 경쟁사 활동이 얼마나 몰리는지(선케어·크림·앰플 등)."""
    s = get_session()
    try:
        battle = get_category_battle(s, days=days)
        return {"days": days, "categories": [
            {"category": c["category"], "total": c["total"], "high": c["high"],
             "top_moves": [{"brand": m["brand"], "country": m["country"],
                            "activity": m["activity_type"], "title": m["title"]}
                           for m in c["moves"][:3]]}
            for c in battle if c["total"]
        ]}
    finally:
        s.close()


@rival_mcp.tool()
def get_expansion_playbook_view(country: str = "", days: int = 90) -> dict:
    """경쟁사가 각 해외 시장에 어떤 채널로 진입했는지(우리 진출 참고서).

    country(선택): 특정 시장만. 비우면 활동 많은 시장 순으로 전체.
    """
    s = get_session()
    try:
        pb = get_expansion_playbook(s, days=days)
        if country.strip():
            cc = _resolve_country(country)
            pb = [m for m in pb if m["country"] == cc]
        return {"days": days, "markets": [
            {"country": m["country"], "country_ko": _CC_TO_KO.get(m["country"], m["country"]),
             "moves": m["moves"], "high": m["high"], "competitor_count": m["brand_count"],
             "entry_channels": m["channels"],
             "key_moves": [{"brand": it["brand"], "activity": it["activity_type"],
                            "channel": it["channel"], "title": it["title"], "url": it["url"]}
                           for it in m["items"][:4]]}
            for m in pb[:10]
        ]}
    finally:
        s.close()


@rival_mcp.tool()
def get_briefings(kind: str = "weekly", limit: int = 1) -> dict:
    """저장된 브리핑(주간/일간) 전문 반환. '지난주 리포트'·'주간 브리핑 요약' 등에 사용.

    kind: 'weekly'(주간, 기본) 또는 'daily'(일간). 한국어 '주간'/'일간'도 허용.
    limit: 최근 N건(기본 1=최신). 기간별로 여러 개 보려면 늘림.
    """
    k = (kind or "weekly").strip().lower()
    if k in ("주간", "week", "weekly"):
        k = "weekly"
    elif k in ("일간", "day", "daily"):
        k = "daily"
    s = get_session()
    try:
        rows = s.execute(text(f"""
            SELECT generated_at::date::text, period_from::date::text, period_to::date::text,
                   content, total, high, brands, countries, model
            FROM {DB_SCHEMA}.briefings
            WHERE kind = :k
            ORDER BY generated_at DESC
            LIMIT :lim
        """), {"k": k, "lim": max(1, min(limit, 8))}).fetchall()
    except Exception:
        return {"kind": k, "count": 0,
                "note": "저장된 브리핑이 아직 없습니다(다음 브리핑 생성 시부터 축적)."}
    finally:
        s.close()
    return {
        "kind": k, "count": len(rows),
        "briefings": [
            {"generated": r[0], "period": f"{r[1]} ~ {r[2]}",
             "stats": {"total": r[4], "high": r[5], "brands": r[6], "countries": r[7]},
             "model": r[8], "content": r[3]}
            for r in rows
        ],
    }


@rival_mcp.tool()
def get_brand_momentum_view() -> dict:
    """최근 4주 vs 직전 4주 활동량 기준 급상승/식는 경쟁 브랜드(속도 신호)."""
    s = get_session()
    try:
        mo = compute_brand_momentum(s)
        def _fmt(m):
            return {"brand": m["brand"], "momentum_x": m["momentum"], "signal": m["signal"],
                    "recent_4w": m["recent_4w"], "prev_4w": m["prev_4w"],
                    "recent_high": m["recent_high"], "tier": m["tier"]}
        return {
            "rising":  [_fmt(m) for m in mo if m["signal"] == "rising"][:8],
            "cooling": [_fmt(m) for m in mo if m["signal"] == "cooling"][:6],
            "all_ranked": [_fmt(m) for m in mo][:15],
        }
    finally:
        s.close()


@rival_mcp.tool()
def get_demand_signal(brand: str = "") -> dict:
    """
    수요 신호(네이버 검색 트렌드) vs 공급 신호(뉴스 보도량) 삼각검증.

    "실제로 검색·수요가 느는가, 아니면 보도만 뜨는가?"를 판별한다.
    verdict: real(뉴스↑+검색↑=진짜 무브) / pr(뉴스↑+검색↓=PR노이즈 의심) /
             latent(검색↑+보도 정체=숨은 수요, 선제 주목) / stable(안정).
    brand 지정 시 해당 브랜드만, 없으면 판별된 것(real/pr/latent) 위주로 반환.
    """
    s = get_session()
    try:
        tri = get_demand_triangulation(s)
        if not any(t["search_momentum"] is not None for t in tri):
            return {"available": False,
                    "note": "검색 트렌드 데이터 없음(아직 수집 전이거나 네이버 API 미설정)."}
        _LABEL = {"real": "실질(뉴스↑·검색↑)", "pr": "PR우세(뉴스↑·검색↓)",
                  "latent": "숨은수요(검색↑·보도정체)", "stable": "안정"}
        def _fmt(t):
            return {"brand": t["brand"], "verdict": t["verdict"],
                    "verdict_ko": _LABEL.get(t["verdict"], t["verdict"]),
                    "news_momentum_x": t["news_momentum"], "news_signal": t["news_signal"],
                    "search_momentum_x": t["search_momentum"], "search_signal": t["search_signal"],
                    "search_index_recent": t["search_recent"], "tier": t["tier"]}
        if brand:
            by_brand = {t["brand"]: t for t in tri}
            resolved = _resolve_brand(by_brand, brand)
            return {"available": True, "brand": resolved or brand,
                    "result": _fmt(by_brand[resolved]) if resolved else None}
        flagged = [t for t in tri if t["verdict"] in ("real", "pr", "latent")]
        return {"available": True,
                "flagged": [_fmt(t) for t in flagged],
                "all_ranked": [_fmt(t) for t in tri][:15]}
    finally:
        s.close()


if __name__ == "__main__":
    # 로컬 단독 실행 (Slack 봇 로컬 테스트용)
    import os
    logging.basicConfig(level=logging.INFO)
    rival_mcp.settings.host = "127.0.0.1"
    rival_mcp.settings.port = int(os.getenv("MCP_PORT", "8100"))
    rival_mcp.run(transport="streamable-http")
