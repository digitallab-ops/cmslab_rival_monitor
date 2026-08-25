"""
HTML 대시보드 보고서 생성기

generate_report(output_path, days) → self-contained HTML 파일 절대경로 반환.
외부 CDN 없이 브라우저에서 바로 열 수 있는 단일 파일을 생성한다.
"""

import html as html_lib
import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path

import requests as req

from analytics.queries import (
    get_activity_distribution,
    get_brand_activity_matrix,
    get_brand_country_matrix,
    get_brand_high_ratio,
    get_brand_insights_raw,
    get_brand_radar,
    get_collection_stats,
    get_country_signal_stats,
    get_category_battle,
    get_expansion_playbook,
    get_briefings_list,
    get_digest_cache,
    get_high_articles,
    get_insights_cache,
    get_insights_cache_by_period,
    get_weekly_trend,
    upsert_insight_cache,
    compute_brand_momentum,
    get_demand_triangulation,
    get_market_export_growth,
    get_market_growth_story,
    get_competitor_financials,
    get_trademark_signals,
    get_google_spikes,
    get_brand_composite_score,
    get_opportunity_stories,
    get_nice_financials,
    get_brand_signal_summary,
    get_collection_stats_range,
    get_brand_insights_raw_by_range,
)
from analytics.summarizer import (
    generate_brand_strategy_summary, generate_market_overview,
    generate_opportunity_actions,
)
from storage.models import get_session

logger = logging.getLogger(__name__)

CHARTJS_URL = "https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"
_CACHE_PATH = Path(__file__).parent / "chartjs_cache.min.js"

COUNTRY_FLAGS = {
    "US": "🇺🇸", "JP": "🇯🇵", "KR": "🇰🇷", "CN": "🇨🇳",
    "PL": "🇵🇱", "SG": "🇸🇬", "TH": "🇹🇭", "GB": "🇬🇧",
    "CA": "🇨🇦", "AU": "🇦🇺", "DE": "🇩🇪", "FR": "🇫🇷",
    "ID": "🇮🇩", "MY": "🇲🇾", "VN": "🇻🇳", "PH": "🇵🇭",
    "IT": "🇮🇹", "BR": "🇧🇷", "MX": "🇲🇽", "IN": "🇮🇳",
    "AE": "🇦🇪", "SA": "🇸🇦", "ZA": "🇿🇦", "ES": "🇪🇸",
    "RU": "🇷🇺", "KZ": "🇰🇿", "UZ": "🇺🇿", "BY": "🇧🇾",
}

ACTIVITY_LABELS = {
    "신시장_진출":    "신시장 진출",
    "유통_채널":      "유통 채널",
    "신제품_런칭":    "신제품 런칭",
    "인플루언서_협업": "인플루언서 협업",
    "투자_BD":        "투자·BD",
    "브랜드_마케팅":  "브랜드 마케팅",
    "실적_공시":      "실적·공시",
    "가격_프로모션":  "가격·프로모션",
    "기타":           "기타",
}

# 활동 유형별 차트 색상
ACTIVITY_COLORS = [
    "#2b6cb0", "#e53e3e", "#2f855a", "#744210",
    "#553c9a", "#c05621", "#4a5568",
]


# ---------------------------------------------------------------------------
# Chart.js 로컬 캐시
# ---------------------------------------------------------------------------

def _get_chartjs() -> str:
    """Chart.js minified 소스 반환. 캐시 파일 우선, 없으면 다운로드."""
    if _CACHE_PATH.exists():
        try:
            return _CACHE_PATH.read_text(encoding="utf-8")
        except Exception:
            pass
    try:
        resp = req.get(CHARTJS_URL, timeout=15)
        if resp.status_code == 200:
            _CACHE_PATH.write_text(resp.text, encoding="utf-8")
            logger.info("Chart.js 캐시 완료: %s", _CACHE_PATH)
            return resp.text
    except Exception as e:
        logger.warning("Chart.js 다운로드 실패 (CSS 폴백 사용): %s", e)
    return ""


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------

def _esc(s) -> str:
    return html_lib.escape(str(s)) if s else ""


def _fmt_date(iso_str: str) -> str:
    """ISO 날짜 문자열 → KST YYYY-MM-DD."""
    if not iso_str:
        return "?"
    try:
        dt = datetime.fromisoformat(iso_str[:19]) + timedelta(hours=9)
        return dt.strftime("%Y-%m-%d")
    except Exception:
        return iso_str[:10]


def _fmt_art_for_js(a: dict) -> dict:
    """기사 dict → JS PERIOD_DATA.articles 항목 형식."""
    return {
        "brand":   a["brand"],
        "country": a["country"],
        "date":    _fmt_date(a["published_date"]),
        "act":     ACTIVITY_LABELS.get(a["activity_type"], a["activity_type"]),
        "imp":     a.get("importance", "high"),
        "title":   (a.get("title_ko") or a["title"] or (a.get("details") or "")[:120]),
        "details": a.get("details") or "",
        "url":     a.get("source_url") or "",
        "conf":    f"{a['confidence']:.0%}" if a.get("confidence") is not None else "?",
        "source":  a.get("source_name") or "",
        "score":   a.get("score") or 0,
        "channel": a.get("channel") or "",
        "city":    a.get("city") or "",
        "price":   a.get("price_info") or "",
        "evidence": a.get("evidence_level") or "",
    }


def _cell_color(value: int, max_value: int) -> str:
    """히트맵 셀 배경색. 0=빈 셀, max=골드, 가독성 우선."""
    if max_value == 0 or value == 0:
        return "background:#eef1f6;color:#b3bacb;"
    norm = min(value / max_value, 1.0)
    # 라이트 테마: 연한 블루 → 골드 (낮음=연함, 높음=진한 골드). 텍스트는 진하게.
    r = int(216 - norm * (216 - 196))
    g = int(226 - norm * (226 - 156))
    b = int(240 - norm * (240 - 56))
    text = "#20242f"
    return f"background:rgb({r},{g},{b});color:{text};"


# ---------------------------------------------------------------------------
# 섹션별 HTML 렌더러
# ---------------------------------------------------------------------------

def _render_kpi_cards(stats: dict) -> str:
    items = [
        ("총 수집",   stats["total"],             "건", "#8b95ff", "kpi-total"),
        ("HIGH",     stats["high"],              "건", "#e05353", "kpi-high"),
        ("활성 브랜드", stats["brands_active"],   "개", "#4a8fd4", "kpi-brands"),
        ("커버 국가",  stats["countries_active"], "개", "#6b7488", "kpi-countries"),
    ]
    cards = "".join(
        f'<div class="kpi-card">'
        f'<div class="kpi-value" style="color:{color}">'
        f'<span id="{kid}">{val}</span><span class="kpi-unit">{unit}</span></div>'
        f'<div class="kpi-label">{_esc(label)}</div>'
        f'</div>'
        for label, val, unit, color, kid in items
    )
    return f'<div class="kpi-grid">{cards}</div>'


def _render_high_table(articles: list) -> str:
    if not articles:
        return '<p class="no-data">HIGH/MEDIUM 기사 없음</p>'

    rows = []
    for i, art in enumerate(articles):
        flag = COUNTRY_FLAGS.get(art["country"], "🌐")
        act_label = ACTIVITY_LABELS.get(art["activity_type"], art["activity_type"])
        date_str = _fmt_date(art["published_date"])
        conf_str = f"{art['confidence']:.0%}" if art["confidence"] is not None else "?"
        imp = art.get("importance", "high")
        imp_badge = (
            '<span class="imp-badge imp-high">HIGH</span>' if imp == "high"
            else '<span class="imp-badge imp-med">MED</span>'
        )
        # 제목: title_ko → details 첫 줄(한국어) → 원문 순서로 fallback
        title_display = (
            art.get("title_ko")
            or art["title"]
            or (art.get("details") or "")[:120]
        )
        product_line = (
            f'<p><strong>제품:</strong> {_esc(art["product_name"])}</p>'
            if art.get("product_name") else ""
        )
        note_line = (
            f'<p class="note-line"><strong>메모:</strong> {_esc(art["note"])}</p>'
            if art.get("note") else ""
        )
        # 제목: 한국어 번역이 있으면 원문 + 번역 모두 표시
        title_ko_line = (
            f'<p class="title-ko-line"><strong>제목(한):</strong> {_esc(art["title_ko"])}</p>'
            if art.get("title_ko") else ""
        )
        # 본문: 한국어 번역이 있으면 표시, 없으면 details로 대체
        body_ko = art.get("article_body_ko") or ""
        body_ko_line = (
            f'<p><strong>본문(한):</strong> {_esc(body_ko)}</p>'
            if body_ko else ""
        )
        # 원문 본문이 있으면 아코디언으로 표시
        orig_body = art.get("article_body") or ""
        orig_body_line = (
            f'<details class="body-orig"><summary>원문 본문 보기</summary>'
            f'<pre class="body-text">{_esc(orig_body[:1500])}'
            f'{"…" if len(orig_body) > 1500 else ""}</pre></details>'
            if orig_body else ""
        )
        title_disp2 = title_display[:160] + ("…" if len(title_display) > 160 else "")

        rows.append(
            f'<tr class="main-row" data-brand="{_esc(art["brand"])}" data-act="{_esc(act_label)}" onclick="toggleRow({i})">'
            f'<td class="date-cell">{_esc(date_str)}</td>'
            f'<td>{imp_badge} <span class="brand-tag">{_esc(art["brand"])}</span></td>'
            f'<td class="flag-cell">{flag} {_esc(art["country"])}</td>'
            f'<td><span class="act-tag">{_esc(act_label)}</span></td>'
            f'<td class="title-cell">{_esc(title_disp2)}</td>'
            f'<td class="conf-cell">{_esc(conf_str)}</td>'
            f'<td><a href="{_esc(art["source_url"])}" target="_blank" '
            f'onclick="event.stopPropagation()">원문↗</a></td>'
            f'</tr>'
            f'<tr id="dr-{i}" class="detail-row hidden">'
            f'<td colspan="7">'
            f'<div class="detail-box">'
            f'{title_ko_line}'
            f'<p><strong>요약(한):</strong> {_esc(art["details"])}</p>'
            f'{body_ko_line}'
            f'{product_line}{note_line}'
            f'{orig_body_line}'
            f'<p class="src-info">출처: {_esc(art.get("source_name","?"))}'
            + (
                f' &nbsp;<span style="color:var(--gold);font-size:11.5px">↗ 크로스마켓 '
                f'(수집:{_esc(art["source_country"])}→시장:{_esc(art["country"])})</span>'
                if art.get("source_country") and art.get("source_country") != art.get("country")
                else ""
            ) +
            f'</p>'
            f'</div></td></tr>'
        )

    return (
        '<div class="table-wrap">'
        '<table class="data-table">'
        '<thead><tr>'
        '<th>날짜</th><th>브랜드</th><th>국가</th>'
        '<th>활동 유형</th><th>제목</th><th>신뢰도</th><th>링크</th>'
        '</tr></thead>'
        f'<tbody id="articles-tbody">{"".join(rows)}</tbody>'
        '</table></div>'
    )


def _render_heatmap(matrix_data: dict) -> str:
    brands = matrix_data["brands"]
    countries = matrix_data["countries"]
    if not brands:
        return '<p class="no-data">데이터 없음</p>'

    matrix = matrix_data["matrix"]
    brand_totals = matrix_data["brand_totals"]
    country_totals = matrix_data["country_totals"]
    max_val = max(
        (matrix.get(b, {}).get(c, 0) for b in brands for c in countries),
        default=1,
    ) or 1

    header = "".join(
        f'<th title="{_esc(c)}">{COUNTRY_FLAGS.get(c,"")} {_esc(c)}</th>'
        for c in countries
    )
    thead = f'<thead><tr><th class="sticky-col">브랜드</th>{header}<th>합계</th></tr></thead>'

    body_rows = []
    for brand in brands:
        cells = []
        for c in countries:
            v = matrix.get(brand, {}).get(c, 0)
            style = _cell_color(v, max_val)
            if v:
                b_esc = _esc(brand).replace("'", "\\'")
                click = f' onclick="openHeatmapDrilldown(\'{b_esc}\',\'{c}\',{v})"'
                extra = f' title="{_esc(brand)} × {c} ({v}건)" style="{style}cursor:pointer;"'
            else:
                click = ""
                extra = f' style="{style}"'
            cells.append(f'<td{extra}{click}>{v or ""}</td>')
        total = brand_totals.get(brand, 0)
        body_rows.append(
            f'<tr><td class="sticky-col brand-name">{_esc(brand)}</td>'
            f'{"".join(cells)}<td class="total-cell">{total}</td></tr>'
        )

    # 합계 행
    foot_cells = "".join(
        f'<td class="total-cell">{country_totals.get(c,0)}</td>'
        for c in countries
    )
    grand = matrix_data["grand_total"]
    foot = (
        f'<tr class="total-row"><td class="sticky-col">합계</td>'
        f'{foot_cells}<td class="total-cell">{grand}</td></tr>'
    )

    return (
        '<div class="table-wrap heatmap-wrap">'
        f'<table class="data-table heatmap-table">{thead}'
        f'<tbody>{"".join(body_rows)}{foot}</tbody></table>'
        '</div>'
    )


def _canvas_or_table_trend(trend: dict, has_chartjs: bool) -> str:
    """트렌드 섹션 HTML (Chart.js 없으면 테이블 폴백)."""
    if not trend["weeks"]:
        return '<p class="no-data">주별 트렌드 데이터 없음</p>'
    if has_chartjs:
        return '<div class="chart-container"><canvas id="trendChart"></canvas></div>'
    # 폴백: 테이블
    rows = "".join(
        f'<tr><td>{_esc(w)}</td>'
        f'<td style="color:#c53030;font-weight:700">{h}</td>'
        f'<td style="color:#dd6b20">{m}</td>'
        f'<td style="color:#a0aec0">{lo}</td></tr>'
        for w, h, m, lo in zip(trend["weeks"], trend["high"], trend["medium"], trend["low"])
    )
    return (
        '<div class="table-wrap"><table class="data-table">'
        '<thead><tr><th>주차</th><th>HIGH</th><th>MEDIUM</th><th>LOW</th></tr></thead>'
        f'<tbody>{rows}</tbody></table></div>'
    )


def _canvas_or_table_activity(distribution: list, has_chartjs: bool) -> str:
    """활동 분포 섹션 HTML (Chart.js 없으면 테이블 폴백)."""
    if not distribution:
        return '<p class="no-data">데이터 없음</p>'
    if has_chartjs:
        return '<div class="chart-container chart-sm"><canvas id="actChart"></canvas></div>'
    rows = "".join(
        f'<tr><td>{_esc(ACTIVITY_LABELS.get(d["activity_type"],d["activity_type"]))}</td>'
        f'<td>{d["total"]}</td><td>{d["pct"]}%</td>'
        f'<td style="color:#c53030">{d["high"]}</td></tr>'
        for d in distribution
    )
    return (
        '<div class="table-wrap"><table class="data-table">'
        '<thead><tr><th>활동 유형</th><th>건수</th><th>비율</th><th>HIGH</th></tr></thead>'
        f'<tbody>{rows}</tbody></table></div>'
    )


def _render_filter_bar(brands: list, activity_types: list) -> str:
    """브랜드 + 활동유형 필터 pill 바."""
    brand_pills = '<button class="filter-pill active" data-brand="all">전체</button>'
    for b in brands:
        brand_pills += f'<button class="filter-pill" data-brand="{_esc(b)}">{_esc(b)}</button>'

    act_pills = '<button class="filter-pill active" data-act="all">전체</button>'
    for a in activity_types:
        label = ACTIVITY_LABELS.get(a, a)
        act_pills += f'<button class="filter-pill" data-act="{_esc(label)}">{_esc(label)}</button>'

    return (
        '<div class="filter-bar" id="filter-bar">'
        f'<span class="filter-label">브랜드</span>'
        f'<div class="filter-group" id="brand-filters">{brand_pills}</div>'
        '<div class="filter-sep"></div>'
        f'<span class="filter-label">활동</span>'
        f'<div class="filter-group" id="act-filters">{act_pills}</div>'
        '<span class="filter-count" id="filter-count"></span>'
        '</div>'
    )


def _render_brand_radar(radar: list) -> str:
    """Brand Radar — 모멘텀 스코어 바 + 티어 표시."""
    if not radar:
        return '<p class="no-data">모멘텀 데이터 없음 (첫 주간 계산 전)</p>'

    SIGNAL_ICON  = {"rising": "▲", "stable": "▶", "cooling": "▼"}
    SIGNAL_COLOR = {"rising": "#4ab884", "stable": "#8891a8", "cooling": "#e05353"}
    TIER_LABEL   = {1: "Tier 1", 2: "Tier 2"}

    rows = []
    max_m = max((s["momentum"] for s in radar), default=1.0)
    max_m = max(max_m, 1.0)
    for s in radar:
        brand   = _esc(s["brand"])
        m       = s["momentum"]
        signal  = s.get("signal", "stable")
        tier    = s.get("tier", 2)
        recent  = s.get("recent_4w", 0)
        prev    = s.get("prev_4w", 0)
        icon    = SIGNAL_ICON[signal]
        color   = SIGNAL_COLOR[signal]
        bar_pct = min(int(m / max_m * 100), 100)
        tier_cls = "radar-tier1" if tier == 1 else "radar-tier2"

        promo_badge = ""
        if signal == "rising" and tier == 2 and recent >= 5:
            promo_badge = '<span class="radar-promo">승급 후보</span>'
        elif signal == "cooling" and tier == 1 and recent <= 2:
            promo_badge = '<span class="radar-demote">강등 후보</span>'

        rows.append(
            f'<div class="radar-row">'
            f'<span class="radar-icon" style="color:{color}">{icon}</span>'
            f'<span class="radar-brand">{brand}</span>'
            f'<span class="{tier_cls}">{TIER_LABEL[tier]}</span>'
            f'<div class="radar-bar-bg"><div class="radar-bar-fill" '
            f'style="width:{bar_pct}%;background:{color}"></div></div>'
            f'<span class="radar-score" style="color:{color}">{m:.1f}x</span>'
            f'<span class="radar-meta">{recent}건↗{prev}건</span>'
            f'{promo_badge}'
            f'</div>'
        )
    return '<div class="radar-list">' + "".join(rows) + '</div>'


def _render_demand_signal(tri: list) -> str:
    """수요 검증 — 뉴스(공급/PR) vs 네이버 검색(수요) 삼각검증."""
    if not tri or all(t.get("search_momentum") is None for t in tri):
        return ('<p class="no-data">검색 수요 데이터 없음 '
                '(네이버 검색 트렌드 첫 수집 전 · 월·목 07:00 KST 갱신)</p>')

    VERDICT = {
        "real":   ("실질",     "ds-real",   "뉴스도 검색도 상승 — 진짜 무브"),
        "latent": ("숨은수요",  "ds-latent", "검색은 느는데 보도 적음 — 선제 주목"),
        "pr":     ("PR우세",   "ds-pr",     "보도는 뜨는데 검색 수요는 식음 — 노이즈 의심"),
        "stable": ("안정",     "ds-stable", ""),
    }
    SIG = {"rising": "▲", "stable": "▶", "cooling": "▼"}
    SIGC = {"rising": "#4ab884", "stable": "#8891a8", "cooling": "#e05353"}

    def _row(t):
        v = t.get("verdict") or "stable"
        label, cls, _ = VERDICT.get(v, VERDICT["stable"])
        ns = t["news_signal"]; ss = t["search_signal"]
        sm = t["search_momentum"]
        sm_txt = f'{sm:.2f}x' if sm is not None else '—'
        idx = t["search_recent"]
        idx_txt = f'검색지수 {idx:.0f}' if idx is not None else ''
        return (
            f'<div class="ds-row">'
            f'<span class="ds-brand">{_esc(t["brand"])}</span>'
            f'<span class="ds-badge {cls}">{label}</span>'
            f'<span class="ds-leg">뉴스 <b style="color:{SIGC.get(ns)}">{SIG.get(ns,"")} {t["news_momentum"]}x</b></span>'
            f'<span class="ds-leg">검색 <b style="color:{SIGC.get(ss)}">{SIG.get(ss,"")} {sm_txt}</b></span>'
            f'<span class="ds-idx">{idx_txt}</span>'
            f'</div>'
        )

    flagged = [t for t in tri if t.get("verdict") in ("real", "latent", "pr")]
    body = "".join(_row(t) for t in flagged) if flagged else (
        '<p class="no-data" style="margin:6px 0">이번 주 뉴스·검색이 엇갈리는 브랜드 없음 '
        '(대부분 안정). 검색 이력이 쌓일수록 판별력이 올라갑니다.</p>')

    legend = (
        '<div class="ds-help">'
        '<span class="ds-badge ds-real">실질</span> 뉴스↑·검색↑ &nbsp; '
        '<span class="ds-badge ds-latent">숨은수요</span> 검색↑·보도정체 &nbsp; '
        '<span class="ds-badge ds-pr">PR우세</span> 뉴스↑·검색↓'
        '</div>')
    return legend + '<div class="ds-list">' + body + '</div>'


def _render_brand_high_ratio(brand_high: list) -> str:
    """브랜드별 HIGH 비중 CSS 바 차트."""
    if not brand_high:
        return '<p class="no-data">데이터 없음</p>'
    rows = []
    for d in brand_high:
        pct = d["pct"]
        rows.append(
            f'<div class="hr-row">'
            f'<div class="hr-brand">{_esc(d["brand"])}</div>'
            f'<div class="hr-bar-bg"><div class="hr-bar-fill" style="width:{pct}%"></div></div>'
            f'<div class="hr-badge">{pct:.1f}%</div>'
            f'<div class="hr-meta">{d["high"]}/{d["total"]}건</div>'
            f'</div>'
        )
    return f'<div class="high-ratio-wrap">{"".join(rows)}</div>'


def _render_category_battle(battle: list) -> str:
    """자사 카테고리 × 경쟁 활동 대결 뷰 (서버 렌더)."""
    if not battle:
        return ""
    max_total = max((c["total"] for c in battle), default=1) or 1
    rows = []
    for c in battle:
        total, high = c["total"], c["high"]
        pct = int(total / max_total * 100)
        high_pct = int(high / total * 100) if total else 0
        top = c["moves"][0] if c["moves"] else None
        top_html = ""
        if top:
            ch = f" · {_esc(top['channel'])}" if top.get("channel") else ""
            top_html = (f'<div class="catb-top">최고 위협: <b>{_esc(top["brand"])}</b> '
                        f'({_esc(top["country"])}, {ACTIVITY_LABELS.get(top["activity_type"], top["activity_type"])}'
                        f'{ch}) · {top["score"]}점</div>')
        rows.append(
            f'<div class="catb-row">'
            f'<div class="catb-name">{_esc(c["category"])}</div>'
            f'<div class="catb-bar-wrap"><div class="catb-bar" style="width:{pct}%">'
            f'<span class="catb-bar-hi" style="width:{high_pct}%"></span></div>'
            f'<span class="catb-cnt">{total}건 <span class="catb-hi-cnt">HIGH {high}</span></span></div>'
            f'{top_html}</div>'
        )
    return (
        '<div class="section" id="category-battle">'
        '<div class="section-title">🥊 우리 카테고리 vs 경쟁 활동'
        '<span class="section-sub">셀퓨전씨가 파는 카테고리에서 경쟁사가 얼마나 활발한가 (중복 제외)</span>'
        '</div>'
        f'<div class="catb-list">{"".join(rows)}</div>'
        '</div>'
    )


_COUNTRY_KO_LBL = {
    "US": "미국", "JP": "일본", "KR": "한국", "CN": "중국", "GB": "영국",
    "PL": "폴란드", "SG": "싱가포르", "TH": "태국", "CA": "캐나다", "AU": "호주",
    "DE": "독일", "FR": "프랑스", "ID": "인도네시아", "MY": "말레이시아",
    "VN": "베트남", "PH": "필리핀", "IT": "이탈리아", "BR": "브라질",
    "MX": "멕시코", "IN": "인도", "AE": "UAE", "SA": "사우디", "ZA": "남아공",
    "RU": "러시아", "KZ": "카자흐스탄", "UZ": "우즈베키스탄", "BY": "벨라루스",
}


def _render_expansion_playbook(playbook: list) -> str:
    """해외 진출 플레이북 — 경쟁사가 각 시장에 어떤 채널로 들어갔나 (우리 진출 참고서)."""
    if not playbook:
        return ""
    cards = []
    for m in playbook[:9]:
        cc = m["country"]
        flag = COUNTRY_FLAGS.get(cc, "🌐")
        name = _COUNTRY_KO_LBL.get(cc, cc)
        chips = "".join(
            f'<span class="pb-chip">{_esc(ch)}</span>' for ch in m.get("channels", [])[:6]
        )
        chips_html = (f'<div class="pb-chips"><span class="pb-chips-lbl">진입 채널</span>{chips}</div>'
                      if chips else
                      '<div class="pb-chips pb-chips-empty">채널 데이터 축적 중 (신규 수집분부터 채워짐)</div>')
        moves = []
        for it in m.get("items", [])[:4]:
            ch = f' · <span class="pb-mv-ch">{_esc(it["channel"])}</span>' if it.get("channel") else ""
            url = _esc(it.get("url", ""))
            title = _esc((it.get("title") or "")[:80])
            act = ACTIVITY_LABELS.get(it["activity_type"], it["activity_type"])
            title_html = f'<a href="{url}" target="_blank" rel="noopener">{title}</a>' if url else title
            moves.append(
                f'<li><span class="pb-mv-b">{_esc(it["brand"])}</span> '
                f'<span class="pb-mv-act">{act}</span>{ch}<br>{title_html}</li>'
            )
        cards.append(
            f'<div class="pb-card">'
            f'<div class="pb-head"><span class="pb-flag">{flag}</span>'
            f'<span class="pb-name">{_esc(name)}</span>'
            f'<span class="pb-stat">{m["moves"]}건 · HIGH {m["high"]} · 경쟁사 {m["brand_count"]}</span></div>'
            f'{chips_html}'
            f'<ul class="pb-moves">{"".join(moves)}</ul>'
            f'</div>'
        )
    return (
        '<div class="section" id="expansion-playbook">'
        '<div class="section-title">🧭 해외 진출 플레이북'
        '<span class="section-sub">경쟁사는 이 시장에 이렇게 들어갔다 — 우리 진출 참고서 (신시장 진출·유통 채널 활동, 중복 제외)</span>'
        '</div>'
        f'<div class="pb-grid">{"".join(cards)}</div>'
        '</div>'
    )


def _render_export_growth(growth: list) -> str:
    """관세청 화장품 수출 성장 — 국가별 최근3M 수출액 + 전년 YoY (성과 신호)."""
    if not growth:
        return ('<p class="no-data">수출통계 데이터 없음 '
                '(관세청 수집 전 · 매월 3일 갱신)</p>')

    # 수출 규모순 상위 12개국. 바 = 현재(틸) 위에 전년(회색) 오버레이 → 성장분이 틸로 드러남.
    rows = []
    top = growth[:12]
    max_e = max((g["exp_usd_3m"] for g in top), default=1.0) or 1.0
    for g in top:
        cc = g["country_code"]
        flag = COUNTRY_FLAGS.get(cc, "🌐")
        name = _COUNTRY_KO_LBL.get(cc, g["country_name"] or cc)
        cur = g["exp_usd_3m"] / 1e6
        prev = g["prev_usd_3m"] / 1e6
        yoy = g["yoy_pct"]
        if yoy is None:
            yoy_txt, yoy_col = "—", "var(--lo)"
        else:
            yoy_col = "var(--teal)" if yoy >= 15 else ("var(--coral)" if yoy <= -10 else "var(--mid)")
            yoy_txt = f'{"+" if yoy >= 0 else ""}{yoy:.0f}%'
        curw = max(3, cur / max_e * 100)
        prevw = min(max(prev / max_e * 100, 0), 100)
        rows.append(
            f'<div class="xg-row">'
            f'<span class="xg-flag">{flag}</span>'
            f'<span class="xg-name">{_esc(name)}</span>'
            f'<div class="xg-bar-bg">'
            f'<div class="xg-bar-cur" style="width:{curw:.1f}%"></div>'
            f'<div class="xg-bar-prev" style="width:{prevw:.1f}%"></div></div>'
            f'<span class="xg-val"><span class="xg-prevv">${prev:,.0f}M →</span> ${cur:,.0f}M</span>'
            f'<span class="xg-yoy" style="color:{yoy_col}">{yoy_txt}</span>'
            f'</div>'
        )
    return (
        '<div class="xg-help">최근 3개월 누적 수출액(USD) · '
        '<b style="color:var(--mid)">회색=전년</b> <b style="color:var(--teal)">틸=현재</b> (틸 초과분 = 성장) · '
        'YoY <b style="color:var(--teal)">+15%↑</b> / <b style="color:var(--coral)">-10%↓</b></div>'
        '<div class="xg-list">' + "".join(rows) + '</div>'
    )


def _won_eok(v) -> str:
    """원 → '억'/'조' 한국어 표기."""
    if not v:
        return "-"
    eok = v / 1e8
    return f"{eok/1e4:,.2f}조" if eok >= 1e4 else f"{eok:,.0f}억"


def _render_financials(fins: list) -> str:
    """경쟁사 실적(DART) — 매출·영업이익·영업이익률·매출 YoY 테이블."""
    if not fins:
        return ('<p class="no-data">재무 데이터 없음 '
                '(DART 수집 전 · 매월 4일 갱신)</p>')
    body = []
    for f in fins:
        yoy = f["rev_yoy_pct"]
        if yoy is None:
            yoy_html = '<span class="fin-yoy" style="color:var(--lo)">—</span>'
        else:
            c = "#4ab884" if yoy >= 10 else ("#e05353" if yoy <= -5 else "var(--mid)")
            yoy_html = f'<span class="fin-yoy" style="color:{c}">{"+" if yoy >= 0 else ""}{yoy:.0f}%</span>'
        listed = ('<span class="fin-tag fin-listed">상장</span>' if f["stock_code"]
                  else '<span class="fin-tag fin-unlisted">비상장</span>')
        whole = '' if f["is_brand_level"] else '<span class="fin-whole">회사전체</span>'
        opm = f'{f["opm"]:.0f}%' if f["opm"] is not None else '-'
        body.append(
            f'<tr><td class="fin-brand">{_esc(f["brand"])}</td>'
            f'<td class="fin-corp">{_esc(f["corp_name"])}{listed}{whole}</td>'
            f'<td class="fin-num">{f["year"]}</td>'
            f'<td class="fin-num fin-rev">{_won_eok(f["revenue"])}</td>'
            f'<td class="fin-num">{yoy_html}</td>'
            f'<td class="fin-num">{_won_eok(f["op_income"])}</td>'
            f'<td class="fin-num">{opm}</td></tr>'
        )
    return (
        '<div class="table-wrap"><table class="data-table fin-table">'
        '<thead><tr><th>브랜드</th><th>운영사</th><th>기준연도</th>'
        '<th>매출</th><th>YoY</th><th>영업이익</th><th>영업이익률</th></tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table></div>'
        '<div class="fin-note">※ DART 전자공시 기준(연결). ‘회사전체’는 해당 브랜드가 대기업의 '
        '일부라 수치가 회사 전체임(예: 구달=클리오, 센텔리안24=동국제약, 에스트라=아모레퍼시픽). '
        '비상장 외감법인(아누아·조선미녀·토리든 등)은 표준 재무 API 미제공으로 제외.</div>'
    )


# NICE 매칭 기업 중 상장사(코스피/코스닥) — 상장/비상장 구분 표기용(수동 큐레이션)
_NICE_LISTED = {"동국제약(주)", "(주)파마리서치", "(주)클리오", "(주)브이티", "(주)에이피알"}


def _eok(thousands) -> str:
    """천원 → 억원 표기. NICE 재무 단위(천원) 전용."""
    if thousands is None:
        return "—"
    return f"{thousands / 100000:,.0f}억"


def _render_financials_nice(fins: list) -> str:
    """NICE BizLine 재무 — 브랜드별 매출·영업이익·광고비 2023~25(비상장 포함). 연 단위."""
    if not fins:
        return ('<p class="no-data">재무 데이터 없음 (NICE BizLine 적재 전 · '
                'python -m signals.nice_financials)</p>')
    YRS = [2023, 2024, 2025]
    body = []
    for f in fins:
        yrs = f["years"]
        company = f["company"]
        listed = ('<span class="fin-tag fin-listed">상장</span>' if company in _NICE_LISTED
                  else '<span class="fin-tag fin-unlisted">비상장</span>')
        scope = ('<span class="fin-tag fin-single">단일브랜드</span>' if f["is_single_brand"]
                 else '<span class="fin-whole">회사전체</span>')
        # 매출 3개년 + 최신 YoY
        rev = [yrs.get(y, {}).get("revenue") for y in YRS]
        rev_cells = " → ".join(_eok(v) for v in rev)
        yoy_html = "—"
        if rev[2] and rev[1]:
            yoy = (rev[2] / rev[1] - 1) * 100
            c = "#4ab884" if yoy >= 10 else ("#e05353" if yoy <= -5 else "var(--mid)")
            yoy_html = f'<span style="color:{c}">{"+" if yoy >= 0 else ""}{yoy:.0f}%</span>'
        # 영업이익'25 + OPM
        op25 = yrs.get(2025, {}).get("op_income")
        opm = (f'{op25 / rev[2] * 100:.0f}%' if op25 and rev[2] else "—")
        # 광고비'25 + 매출대비
        ad25 = yrs.get(2025, {}).get("ad_spend")
        adr = (f'{ad25 / rev[2] * 100:.0f}%' if ad25 and rev[2] else "—")
        body.append(
            f'<tr><td class="fin-brand">{_esc(f["brand"])}</td>'
            f'<td class="fin-corp">{_esc(company)} {listed}{scope}</td>'
            f'<td class="fin-num fin-rev">{rev_cells}</td>'
            f'<td class="fin-num">{yoy_html}</td>'
            f'<td class="fin-num">{_eok(op25)} <span class="fin-sub">({opm})</span></td>'
            f'<td class="fin-num">{_eok(ad25)} <span class="fin-sub">({adr})</span></td></tr>'
        )
    return (
        '<div class="table-wrap"><table class="data-table fin-table">'
        '<thead><tr><th>브랜드</th><th>운영사</th>'
        '<th>매출 2023 → 2024 → 2025</th><th>매출 YoY</th>'
        '<th>영업이익 2025 <span class="fin-sub">(이익률)</span></th>'
        '<th>광고비 2025 <span class="fin-sub">(매출比)</span></th></tr></thead>'
        f'<tbody>{"".join(body)}</tbody></table></div>'
        '<div class="fin-note">※ NICE BizLine 산업경쟁현황(연 단위, 단위 억원). '
        '‘회사전체’는 브랜드가 대기업 일부라 수치가 회사 전체(예: 구달=클리오, 센텔리안24=동국제약). '
        '‘단일브랜드’는 회사≈브랜드(예: 아누아=더파운더즈). '
        'Cos de Baha·b.plain·에스트라·제로이드는 NICE 미포함.</div>'
    )


def _render_search_spikes(spikes: list) -> str:
    """글로벌 검색 급등(구글) — 최근7일 vs 직전28일 급증 브랜드×시장."""
    if not spikes:
        return ('<p class="no-data">현재 급등 브랜드 없음 '
                '(또는 구글 트렌드 수집 전 · 월·수·금 갱신)</p>')
    _GEO = {"GLOBAL": "🌐 글로벌", "US": "🇺🇸 미국", "JP": "🇯🇵 일본"}
    rows = []
    for s in spikes[:12]:
        rows.append(
            f'<div class="sp-row">'
            f'<span class="sp-brand">{_esc(s["brand"])}</span>'
            f'<span class="sp-geo">{_GEO.get(s["geo"], s["geo"])}</span>'
            f'<span class="sp-x">▲ {s["spike_ratio"]}배</span>'
            f'<span class="sp-idx">지수 {s["recent"]:.0f} ← {s["baseline"]:.0f}</span>'
            f'</div>'
        )
    return '<div class="sp-list">' + "".join(rows) + '</div>'


def _render_trademark(sig: dict) -> str:
    """해외 상표 출원 선행신호 — 최근 자기출원(화장품) 피드 + 브랜드 요약."""
    feed = (sig or {}).get("feed") or []
    brands = (sig or {}).get("brands") or []
    if not feed and not brands:
        return ('<p class="no-data">해외 상표 데이터 없음 '
                '(KIPRIS 수집 전 · 매월 4일 갱신)</p>')

    # 브랜드 요약 칩 (최근 출원 많은 순)
    chips = []
    for b in brands[:10]:
        if not b["recent"]:
            continue
        flag = COUNTRY_FLAGS.get(b["country"], "🌐")
        chips.append(
            f'<span class="tm-chip"><span class="tm-chip-flag">{flag}</span>'
            f'<b>{_esc(b["brand"])}</b> <span class="tm-chip-n">최근 {b["recent"]}건</span></span>'
        )
    chips_html = f'<div class="tm-chips">{"".join(chips)}</div>' if chips else ""

    # 최근 출원 피드
    rows = []
    for f in feed:
        flag = COUNTRY_FLAGS.get(f["country"], "🌐")
        rows.append(
            f'<div class="tm-row">'
            f'<span class="tm-date">{_esc(f["date"])}</span>'
            f'<span class="tm-flag">{flag}</span>'
            f'<span class="tm-brand">{_esc(f["brand"])}</span>'
            f'<span class="tm-mark">{_esc(f["mark"] or "")}</span>'
            f'</div>'
        )
    return chips_html + '<div class="tm-list">' + "".join(rows) + '</div>'


def _yoy_badge_color(yoy) -> str:
    if yoy is None:
        return "var(--lo)"
    return "#4ab884" if yoy >= 15 else ("#e05353" if yoy <= -10 else "var(--mid)")


def _render_growth_story(story: dict) -> str:
    """시장 성장 스토리 — 수출 YoY(성과) + 그 시장의 경쟁사 활동(뉴스)을 카드로 엮음."""
    markets = (story or {}).get("markets") or []
    if not markets:
        return ('<p class="no-data">수출·활동 연계 데이터 없음 '
                '(관세청 수집 후 표시 · 매월 3일 갱신)</p>')

    cards = []
    for m in markets:
        cc = m["country_code"]
        flag = COUNTRY_FLAGS.get(cc, "🌐")
        name = _COUNTRY_KO_LBL.get(cc, m["country_name"] or cc)
        yoy = m["yoy_pct"]
        yoy_txt = f'{"+" if (yoy or 0) >= 0 else ""}{yoy:.0f}%' if yoy is not None else "—"
        col = _yoy_badge_color(yoy)
        moves = []
        for mv in m["moves"][:4]:
            act = ACTIVITY_LABELS.get(mv["activity_type"], mv["activity_type"])
            title = _esc(mv["title"])
            title_html = (f'<a href="{_esc(mv["url"])}" target="_blank" rel="noopener">{title}</a>'
                          if mv["url"] else title)
            hi = ' gs-mv-hi' if mv["importance"] == "high" else ''
            moves.append(
                f'<li class="gs-move{hi}">'
                f'<span class="gs-mv-brand">{_esc(mv["brand"])}</span>'
                f'<span class="gs-mv-act">{_esc(act)}</span>'
                f'<span class="gs-mv-title">{title_html}</span></li>'
            )
        moves_html = ("<ul class='gs-moves'>" + "".join(moves) + "</ul>") if moves else \
            "<div class='gs-nomv'>이 시장 경쟁사 활동 기사 없음(수집 축적 중)</div>"
        cards.append(
            f'<div class="gs-card">'
            f'<div class="gs-head">'
            f'<span class="gs-flag">{flag}</span>'
            f'<span class="gs-name">{_esc(name)}</span>'
            f'<span class="gs-yoy" style="color:{col}">{yoy_txt}</span>'
            f'</div>'
            f'<div class="gs-exp">수출 ${m["exp_musd"]:,.0f}M '
            f'<span class="gs-delta">(전년 대비 +${m["delta_musd"]:,.0f}M)</span></div>'
            f'<div class="gs-why">이 시장에서 경쟁사가 한 일</div>'
            f'{moves_html}'
            f'</div>'
        )
    return f'<div class="gs-grid">{"".join(cards)}</div>'


def _render_growth_headline(story: dict) -> str:
    """개요 상단 배너 — 전체 수출 YoY + 성장 top 시장(경쟁사 활동 맥락) 요약."""
    o = (story or {}).get("overall")
    markets = (story or {}).get("markets") or []
    if not o or o.get("yoy_pct") is None:
        return ""
    yoy = o["yoy_pct"]
    col = _yoy_badge_color(yoy)
    arrow = "▲" if yoy >= 0 else "▼"
    chips = []
    for m in markets[:4]:
        cc = m["country_code"]
        flag = COUNTRY_FLAGS.get(cc, "🌐")
        name = _COUNTRY_KO_LBL.get(cc, m["country_name"] or cc)
        top_brand = m["moves"][0]["brand"] if m["moves"] else ""
        ctx = f'<span class="gh-chip-ctx">{_esc(top_brand)} 등 {len(m["moves"])}건</span>' if top_brand else ""
        chips.append(
            f'<button class="gh-chip" onclick="switchTab(\'strategy\')">'
            f'<span class="gh-chip-flag">{flag}</span>'
            f'<span class="gh-chip-name">{_esc(name)}</span>'
            f'<span class="gh-chip-yoy" style="color:{_yoy_badge_color(m["yoy_pct"])}">'
            f'{"+" if (m["yoy_pct"] or 0) >= 0 else ""}{m["yoy_pct"]:.0f}%</span>'
            f'{ctx}</button>'
        )
    return (
        '<div class="gh-band">'
        '<div class="gh-left">'
        '<div class="gh-label">주요국 화장품 수출 (관세청 · 최근 3개월 YoY)</div>'
        f'<div class="gh-big" style="color:{col}">{arrow} {"+" if yoy >= 0 else ""}{yoy:.1f}%</div>'
        f'<div class="gh-sub">${o["cur_musd"]:,.0f}M · 성장 {o["growers"]}개국 / 둔화 {o["decliners"]}개국</div>'
        '</div>'
        f'<div class="gh-right"><div class="gh-right-lbl">🔥 뜨는 시장 — 실수출↑ 그리고 경쟁사 활동↑</div>'
        f'<div class="gh-chips">{"".join(chips)}</div></div>'
        '</div>'
    )


def _briefing_md_to_html(text: str) -> str:
    """브리핑 마크다운-라이트(### 헤더 / - 불릿 / *굵게*) → HTML."""
    import re as _re
    out, in_ul = [], False
    for raw in (text or "").split("\n"):
        ln = raw.rstrip()
        s = ln.strip()
        if not s:
            continue
        s_html = _esc(s)
        # *굵게* → <strong> (별표 한 쌍)
        s_html = _re.sub(r"\*([^*]+)\*", r"<strong>\1</strong>", s_html)
        if s.startswith("### "):
            if in_ul:
                out.append("</ul>"); in_ul = False
            out.append(f'<h4 class="bfa-h">{s_html[4:].strip()}</h4>')
        elif s.startswith(("- ", "•")):
            if not in_ul:
                out.append('<ul class="bfa-ul">'); in_ul = True
            item = s_html.lstrip("-• ").strip()
            out.append(f"<li>{item}</li>")
        else:
            if in_ul:
                out.append("</ul>"); in_ul = False
            # 들여쓴 분석 줄(→) 등
            out.append(f'<p class="bfa-p">{s_html}</p>')
    if in_ul:
        out.append("</ul>")
    return "".join(out)


def _render_briefing_archive(briefings: list) -> str:
    """브리핑 아카이브 — 날짜별 주간/일간 리포트 목록(클릭 시 펼침)."""
    if not briefings:
        return ""
    KIND = {"weekly": ("주간", "bfa-weekly"), "daily": ("일간", "bfa-daily")}
    items = []
    for b in briefings:
        kko, kcls = KIND.get(b["kind"], (b["kind"], "bfa-daily"))
        gen = (b.get("generated_at") or "")[:10]
        period = ""
        if b.get("period_from") and b.get("period_to"):
            period = f'{b["period_from"]} ~ {b["period_to"]}'
        stats = []
        if b.get("total") is not None:
            stats.append(f'{b["total"]}건')
        if b.get("high") is not None:
            stats.append(f'HIGH {b["high"]}')
        stat_str = " · ".join(stats)
        body = _briefing_md_to_html(b.get("content", ""))
        items.append(
            f'<details class="bfa-item">'
            f'<summary class="bfa-sum">'
            f'<span class="bfa-badge {kcls}">{kko}</span>'
            f'<span class="bfa-date">{_esc(gen)}</span>'
            f'<span class="bfa-period">{_esc(period)}</span>'
            f'<span class="bfa-stat">{_esc(stat_str)}</span>'
            f'</summary>'
            f'<div class="bfa-body">{body}</div>'
            f'</details>'
        )
    return (
        '<div class="section" id="briefing-archive">'
        '<div class="section-title">📚 브리핑 아카이브'
        '<span class="section-sub">지난 주간·일간 브리핑 (클릭하면 전문 펼침)</span>'
        '</div>'
        f'<div class="bfa-list">{"".join(items)}</div>'
        '</div>'
    )


def _digest_market_teaser(market_text: str) -> list:
    """시장 인사이트 첫 섹션의 앞 불릿 2~3개 추출."""
    if not market_text:
        return []
    import re as _re
    for part in _re.split(r"###\s+", market_text):
        part = part.strip()
        if not part:
            continue
        nl = part.find("\n")
        body = part[nl + 1:] if nl >= 0 else ""
        bullets = [ln.strip().lstrip("-•").strip().replace("**", "")
                   for ln in body.split("\n") if ln.strip().startswith(("-", "•"))]
        if bullets:
            return bullets[:3]
    return []


def _render_overview_digest(stats, momentum, category_battle, expansion_playbook,
                            high_articles, market_text, ref_date="") -> str:
    """개요 탭 지도 아래 '이번 주(최근 7일) 종합 요약' — 내러티브 중심 + 데이터 + 탭 점프."""
    import re as _re
    rising = [m for m in momentum if m.get("signal") == "rising"][:4]
    cooling = [m for m in momentum if m.get("signal") == "cooling"][:2]
    cats = [c for c in category_battle if c.get("total")][:4]
    mkts = [m for m in expansion_playbook if m.get("moves")][:4]
    # 최고 무브: 같은 사건 반복 방지 — (브랜드·국가·활동) 조합당 1건
    _seen, moves = set(), []
    for a in (high_articles or []):
        k = (a.get("brand", ""), a.get("country", ""), a.get("activity_type", ""))
        if k in _seen:
            continue
        _seen.add(k); moves.append(a)
        if len(moves) >= 6:
            break

    def jump(tab, label):
        return f'<button class="dg-jump" onclick="switchTab(\'{tab}\')">{label} →</button>'

    # KPI 라인 (최근 7일)
    kpi = (f'<div class="dg-kpi">'
           f'<span>📥 수집 <b>{stats.get("total", 0)}</b></span>'
           f'<span>🔴 HIGH <b>{stats.get("high", 0)}</b></span>'
           f'<span>🏷 브랜드 <b>{stats.get("brands_active", 0)}</b></span>'
           f'<span>🌐 국가 <b>{stats.get("countries_active", 0)}</b></span>'
           f'</div>')

    # 1) 시장 인사이트 → 3색 컬럼(🚨대응/🎯기회/👀점검)으로 직관화
    narr_html = ""
    if market_text.strip():
        cols = []
        for part in _re.split(r"###\s+", market_text):
            part = part.strip()
            if not part:
                continue
            nl = part.find("\n")
            label = (part if nl < 0 else part[:nl]).strip()
            body = "" if nl < 0 else part[nl + 1:]
            bullets = [ln.strip().lstrip("-•").strip().replace("**", "")
                       for ln in body.split("\n") if ln.strip().startswith(("-", "•"))]
            if not bullets:
                continue
            if "대응" in label:
                kind, icon = "respond", "🚨"
            elif "기회" in label or "확장" in label:
                kind, icon = "opportunity", "🎯"
            else:
                kind, icon = "check", "👀"
            lis = "".join(_urgency_li(b) for b in bullets[:3])
            cols.append(f'<div class="dg-col dg-col-{kind}">'
                        f'<div class="dg-col-h">{icon} {_esc(label)}</div>'
                        f'<ul>{lis}</ul></div>')
        if cols:
            narr_html = (
                '<div class="dg-block dg-narr">'
                '<div class="dg-block-h">🧠 이번 주 시장 인사이트 · 셀퓨전씨 액션'
                + jump("strategy", "전략 탭 자세히") + '</div>'
                '<div class="dg-cols">' + "".join(cols) + '</div>'
                '</div>'
            )

    # 2) 이번 주 최고 임팩트 무브 (top 8, details 포함)
    mv_rows = ""
    for a in moves:
        cc = a.get("country", "")
        flag = COUNTRY_FLAGS.get(cc, "")
        impc = "dg-imp-high" if a.get("importance") == "high" else "dg-imp-med"
        impl = "HIGH" if a.get("importance") == "high" else "MED"
        act = ACTIVITY_LABELS.get(a.get("activity_type", ""), a.get("activity_type", ""))
        title = _esc((a.get("title_ko") or a.get("title") or "")[:80])
        url = _esc(a.get("source_url", ""))
        thtml = f'<a href="{url}" target="_blank" rel="noopener">{title}</a>' if url else title
        sc = a.get("score") or 0
        det = _esc((a.get("details") or "")[:70])
        det_html = f'<div class="dg-mv-det">{det}</div>' if det else ""
        mv_rows += (f'<div class="dg-move2"><div class="dg-move2-top">'
                    f'<span class="dg-badge {impc}">{impl}</span>'
                    f'<span class="dg-sc">{sc}점</span>'
                    f'<span class="dg-mv-b">{_esc(a.get("brand",""))}</span>'
                    f'<span class="dg-mv-cc">{flag}{_esc(cc)}</span>'
                    f'<span class="dg-mv-act">{_esc(act)}</span></div>'
                    f'<div class="dg-mv-t">{thtml}</div>{det_html}</div>')
    mv_rows = mv_rows or '<div class="dg-empty">이번 주 무브먼트 없음</div>'

    # 3) 모멘텀 / 카테고리 / 핫스팟 미니 그리드
    mo_rows = ""
    for m in rising:
        mo_rows += (f'<div class="dg-li"><span class="dg-up">▲</span> '
                    f'<b>{_esc(m["brand"])}</b> <span class="dg-x">{m["momentum"]}x</span>'
                    f'<span class="dg-sub">최근 {m["recent_4w"]}건·HIGH {m["recent_high"]}</span></div>')
    for m in cooling:
        mo_rows += (f'<div class="dg-li"><span class="dg-down">▼</span> '
                    f'<b>{_esc(m["brand"])}</b> <span class="dg-x dg-x-dn">{m["momentum"]}x</span>'
                    f'<span class="dg-sub">최근 {m["recent_4w"]}건</span></div>')
    mo_rows = mo_rows or '<div class="dg-empty">데이터 축적 중</div>'

    cat_rows = ""
    for c in cats:
        lead = (c["moves"][0]["brand"] if c.get("moves") else "?")
        cat_rows += (f'<div class="dg-li"><b>{_esc(c["category"])}</b> '
                     f'<span class="dg-cnt">{c["total"]}건 <span class="dg-hi">HIGH {c["high"]}</span></span>'
                     f'<span class="dg-sub">주도 {_esc(lead)}</span></div>')
    cat_rows = cat_rows or '<div class="dg-empty">데이터 축적 중</div>'

    mk_rows = ""
    for m in mkts:
        cc = m["country"]
        flag = COUNTRY_FLAGS.get(cc, "🌐")
        name = _COUNTRY_KO_LBL.get(cc, cc)
        chs = " · ".join(m.get("channels", [])[:2]) or "채널 파악중"
        mk_rows += (f'<div class="dg-li">{flag} <b>{_esc(name)}</b> '
                    f'<span class="dg-cnt">{m["moves"]}건</span>'
                    f'<span class="dg-sub">{_esc(chs)}</span></div>')
    mk_rows = mk_rows or '<div class="dg-empty">데이터 축적 중</div>'

    ref = f' · 기준 {_esc(ref_date)}' if ref_date else ""
    return f"""
  <div class="section" id="overview-digest">
    <div class="section-title">📋 이번 주 종합 요약
      <span class="section-sub">최근 7일{ref} · 매일 수집분 반영 · 항목별 '자세히'로 상세 탭 이동</span>
    </div>
    {kpi}
    {narr_html}
    <div class="dg-block">
      <div class="dg-block-h">🔴 이번 주 최고 임팩트 무브 {jump("feed", "기사 전체")}</div>
      <div class="dg-moves">{mv_rows}</div>
    </div>
    <div class="dg-grid">
      <div class="dg-card">
        <div class="dg-card-h">📈 브랜드 모멘텀 {jump("brands", "경쟁사")}</div>
        {mo_rows}
      </div>
      <div class="dg-card">
        <div class="dg-card-h">🥊 우리 카테고리 압박 {jump("strategy", "우리 관점")}</div>
        {cat_rows}
      </div>
      <div class="dg-card">
        <div class="dg-card-h">🧭 해외 진출 핫스팟 {jump("strategy", "우리 관점")}</div>
        {mk_rows}
      </div>
    </div>
  </div>"""


def _render_brand_activity_bar(brand_act: list) -> str:
    """브랜드별 활동 유형 수평 스택바 (Canvas)."""
    if not brand_act:
        return '<p class="no-data">데이터 없음</p>'
    return '<div class="stacked-wrap"><canvas id="stackedBar"></canvas></div>'


def _build_stacked_bar_script(brand_act: list) -> str:
    """브랜드별 활동유형 스택바 Canvas 스크립트."""
    if not brand_act:
        return ""
    act_keys = ["유통_채널", "신시장_진출", "신제품_런칭", "인플루언서_협업",
                "투자_BD", "브랜드_마케팅", "실적_공시", "가격_프로모션", "기타"]
    act_labels_list = [ACTIVITY_LABELS.get(k, k) for k in act_keys]
    act_colors = ["#4a8fd4", "#9b7fe8", "#4ab884", "#8b95ff",
                  "#e05353", "#e0894a", "#46b0b0", "#d64f8f", "#6f7aa0"]

    # [{brand, acts: [count...]}]
    rows_data = []
    for d in brand_act:
        acts = [d["activities"].get(k, {}).get("total", 0) for k in act_keys]
        rows_data.append({"brand": d["brand"], "acts": acts})

    data_json = json.dumps({
        "brands": [r["brand"] for r in rows_data],
        "act_labels": act_labels_list,
        "act_colors": act_colors,
        "rows": [r["acts"] for r in rows_data],
    })
    return f"""
(function() {{
  var d = {data_json};
  window._drawStacked = function() {{
  var canvas = document.getElementById('stackedBar');
  if (!canvas) return;
  var dpr = window.devicePixelRatio || 1;
  var BAR_H = 34, GAP = 14, LABEL_W = 110, PAD_R = 16, PAD_T = 8, PAD_B = 28;
  var n = d.brands.length;
  var totalH = PAD_T + n * BAR_H + (n - 1) * GAP + PAD_B;
  var W = canvas.parentElement.getBoundingClientRect().width;
  if (!W) return;
  canvas.width = W * dpr; canvas.height = totalH * dpr;
  canvas.style.width = W + 'px'; canvas.style.height = totalH + 'px';
  var ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  var chartW = W - LABEL_W - PAD_R;
  d.brands.forEach(function(brand, ri) {{
    var acts = d.rows[ri];
    var total = acts.reduce(function(s, v) {{ return s + v; }}, 0);
    var y = PAD_T + ri * (BAR_H + GAP);
    ctx.fillStyle = '#4c5468';
    ctx.font = '600 11px system-ui,-apple-system,sans-serif';
    ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
    ctx.fillText(brand, LABEL_W - 8, y + BAR_H / 2);
    var x = LABEL_W;
    acts.forEach(function(v, ki) {{
      if (!v || !total) return;
      var segW = (v / total) * chartW;
      ctx.fillStyle = d.act_colors[ki];
      ctx.beginPath();
      if (typeof ctx.roundRect === 'function') {{
        ctx.roundRect(x, y, segW, BAR_H, 3);
      }} else {{ ctx.rect(x, y, segW, BAR_H); }}
      ctx.fill();
      if (v / total > 0.1) {{
        ctx.fillStyle = '#fff';
        ctx.font = '500 10px system-ui';
        ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
        ctx.fillText(d.act_labels[ki], x + segW / 2, y + BAR_H / 2);
      }}
      x += segW;
    }});
    ctx.fillStyle = '#4c5468';
    ctx.font = '11px system-ui';
    ctx.textAlign = 'left';
    ctx.fillText(total + '건', x + 5, y + BAR_H / 2);
  }});
  // Legend
  var leg = document.getElementById('stacked-legend');
  if (leg) {{
    leg.innerHTML = '';
    d.act_labels.forEach(function(lb, i) {{
      var el = document.createElement('div');
      el.className = 'legend-item';
      el.innerHTML = '<span class="legend-dot" style="background:' + d.act_colors[i] + '"></span>' + lb;
      leg.appendChild(el);
    }});
  }}
  }};
  window._drawStacked();
}})();"""


def _build_insights_script(brand_insights: dict) -> str:
    """window._renderInsights(data) 함수 정의 + 초기 렌더링 스크립트."""
    if not brand_insights:
        return ""

    flag_json = json.dumps({"US":"🇺🇸","JP":"🇯🇵","KR":"🇰🇷","SG":"🇸🇬","PL":"🇵🇱","TH":"🇹🇭","CA":"🇨🇦","GB":"🇬🇧"})
    imp_json  = json.dumps({"high": "#e05353", "medium": "#d4943a", "low": "#9aa3b5"})

    return f"""
// ── Brand Insight Cards ──
function _fmtInsightStrategy(raw) {{
  if (!raw) return '';
  var parts = raw.split(/###\\s+/).filter(function(s) {{ return s.trim(); }});
  if (parts.length === 0) return '<div class="insight-strat-body">' + raw + '</div>';
  return parts.map(function(chunk) {{
    var nl = chunk.indexOf('\\n');
    var label = nl === -1 ? chunk.trim() : chunk.slice(0, nl).trim();
    var body  = nl === -1 ? '' : chunk.slice(nl + 1).trim();
    body = _mdBold(body).replace(/\\n/g, ' ');
    var watch = label.indexOf('관전') !== -1;
    return '<div class="insight-strat-sec"><div class="insight-strat-h' + (watch ? ' watch' : '') + '">'
      + _mdBold(label) + '</div><div class="insight-strat-body">' + body + '</div></div>';
  }}).join('');
}}

window._renderInsights = function(data) {{
  var FLAGS = {flag_json};
  var IMP_C = {imp_json};
  var ACT_COLORS_MAP = {{"유통_채널":"#4a8fd4","신시장_진출":"#9b7fe8","신제품_런칭":"#4ab884","인플루언서_협업":"#8b95ff","투자_BD":"#e05353","브랜드_마케팅":"#e0894a","실적_공시":"#46b0b0","가격_프로모션":"#d64f8f","기타":"#6f7aa0"}};
  var grid = document.getElementById('insight-grid');
  if (!grid || !data) return;
  var html = '';
  Object.keys(data).forEach(function(brand) {{
    var ins = data[brand];
    var highCls = ins.high_pct >= 15 ? 'insight-badge-high-hot'
                : ins.high_pct >= 8  ? 'insight-badge-high-warm'
                :                      'insight-badge-high-low';
    var actColor = ACT_COLORS_MAP[ins.top_act] || '#6f7aa0';
    var mkts = (ins.top_countries || []).map(function(cc_cnt) {{
      return '<span class="insight-market-item mkt-click" data-brand="' + escH(brand) + '" data-country="' + escH(cc_cnt[0]) + '" title="' + escH(brand) + ' × ' + escH(cc_cnt[0]) + ' 요약 보기">' + (FLAGS[cc_cnt[0]] || cc_cnt[0]) +
             ' <span class="insight-market-cnt">' + cc_cnt[1] + '건</span></span>';
    }}).join('');
    var nart = (ins.key_articles || []).length;
    var safeId = 'insight-' + brand.replace(/\\s/g, '_');
    html += '<div class="insight-card" id="' + safeId + '">' +
      '<div class="insight-hdr">' +
        '<span class="insight-brand">' + brand + '</span>' +
        '<span class="insight-badge insight-badge-act" style="background:' + actColor + '">' + ins.top_act + ' ' + ins.top_pct + '%</span>' +
        '<span class="insight-badge ' + highCls + '">HIGH ' + ins.high_pct + '%</span>' +
      '</div>' +
      '<div class="insight-strategy clamp" id="strat-' + safeId + '">' + _fmtInsightStrategy(ins.strategy || '') + '</div>' +
      '<button class="insight-more" onclick="toggleStrat(\\'' + safeId + '\\')">더보기 ▾</button>' +
      '<div class="insight-markets">' + mkts + '</div>' +
      (nart ? '<div class="insight-src" onclick="switchTab(\\'feed\\')">핵심 근거 기사 ' + nart + '건 · 기록 탭에서 보기 →</div>' : '') +
    '</div>';
  }});
  grid.innerHTML = html;
}};
// 초기 렌더링 — 현재 기간의 PERIOD_DATA insights 사용
(function() {{
  // 국가 칩 클릭 → 히트맵 드릴다운 (위임 방식 — 카드 재렌더에도 유지)
  var grid = document.getElementById('insight-grid');
  if (grid && !grid._mktBound) {{
    grid._mktBound = true;
    grid.addEventListener('click', function(e) {{
      var t = e.target.closest ? e.target.closest('.mkt-click') : null;
      if (t && t.dataset.country && typeof openHeatmapDrilldown === 'function') {{
        openHeatmapDrilldown(t.dataset.brand, t.dataset.country);
      }}
    }});
  }}
  var d = PERIOD_DATA[String(_currentPeriod)];
  if (d && d.insights) window._renderInsights(d.insights);
  if (d && window._renderMarket) window._renderMarket(d.market || '');
}})();"""


def _build_market_script() -> str:
    """window._renderMarket(text) — 시장 종합 인사이트 렌더."""
    return """
window._mdUrgency = function(t) {
  var m = /\\[?\\s*시급\\s*[:\\-]?\\s*(높음|중간|낮음|high|medium|low)\\s*\\]?/i.exec(t || '');
  if (!m) return { text: t, badge: '' };
  var lv = m[1].toLowerCase();
  var map = {
    '높음': ['insight-badge-high-hot','시급 높음'], 'high': ['insight-badge-high-hot','시급 높음'],
    '중간': ['insight-badge-high-warm','시급 중간'], 'medium': ['insight-badge-high-warm','시급 중간'],
    '낮음': ['insight-badge-high-low','시급 낮음'], 'low': ['insight-badge-high-low','시급 낮음']
  };
  var d = map[lv] || ['insight-badge-high-warm','시급'];
  var clean = (t || '').replace(/\\s*\\[?\\s*시급\\s*[:\\-]?\\s*(높음|중간|낮음|high|medium|low)\\s*\\]?\\s*/ig, '').replace(/[\\s·\\-—]+$/, '');
  return { text: clean, badge: '<span class="insight-badge ' + d[0] + '">' + d[1] + '</span>' };
};
window._renderMarket = function(raw) {
  var el = document.getElementById('market-body');
  if (!el) return;
  if (!raw) { el.innerHTML = '<div class="market-empty">종합할 만한 최근 경쟁 활동이 없습니다.</div>'; return; }
  var parts = raw.split(/###\\s+/).filter(function(s) { return s.trim(); });
  if (!parts.length) { el.innerHTML = '<div class="market-sec-b">' + raw + '</div>'; return; }
  el.innerHTML = parts.map(function(chunk) {
    var nl = chunk.indexOf('\\n');
    var label = nl === -1 ? chunk.trim() : chunk.slice(0, nl).trim();
    var body  = nl === -1 ? '' : chunk.slice(nl + 1).trim();
    // 버킷 판별: 대응/기회/점검(확인)
    var kind = 'check';
    if (label.indexOf('대응') !== -1) kind = 'respond';
    else if (label.indexOf('기회') !== -1) kind = 'opportunity';
    var lines = body.split(/\\n/).map(function(l) { return l.trim(); }).filter(Boolean);
    var items = lines.map(function(l) { return l.replace(/^\\s*[-•\\d.]+\\s*/, '').trim(); }).filter(Boolean);
    var inner = items.length
      ? '<ul class="market-list">' + items.map(function(i) {
          var u = _mdUrgency(i);
          return '<li>' + _mdBold(u.text) + u.badge + '</li>';
        }).join('') + '</ul>'
      : '<div class="market-sec-b">' + _mdBold(body).replace(/\\n/g, '<br>') + '</div>';
    return '<div class="market-sec ' + kind + '">'
      + '<div class="market-sec-h ' + kind + '">' + _mdBold(label) + '</div>' + inner + '</div>';
  }).join('');
};"""


# Natural Earth 110m land polygons [lon, lat].
# Generated via tools/extract_world_map.py (RDP eps=1.0, span>=2° filter).
# Ring 0=Afro-Eurasia, Ring 1=Americas, Ring 2=Antarctica (excluded at runtime),
# Ring 3=Greenland, Ring 4=Australia, Rings 5+=islands/Japan/Britain/etc.
_NE_LAND_POLYS = [
  [[39.2,-4.7],[40.8,-14.7],[34.8,-19.8],[35.5,-24.1],[32.6,-25.7],[32.2,-28.8],[28.2,-32.8],[19.6,-34.8],[11.8,-18.1],[13.7,-10.7],[11.9,-5.0],[8.8,-1.1],[9.4,3.7],[5.9,4.3],[4.3,6.3],[-8.0,4.4],[-12.9,7.8],[-17.6,14.7],[-16.0,23.7],[-5.9,35.8],[9.5,37.4],[11.1,36.9],[10.3,33.8],[19.1,30.3],[21.5,32.8],[33.8,31.0],[36.2,36.7],[27.6,36.7],[26.2,39.5],[33.5,42.0],[41.6,41.5],[36.7,45.2],[39.1,47.3],[35.0,46.3],[36.3,45.1],[33.9,44.4],[32.5,45.3],[33.3,46.1],[30.7,46.6],[27.7,42.6],[28.8,41.1],[22.6,40.3],[24.0,37.7],[22.5,36.4],[19.5,41.7],[13.1,45.7],[12.6,44.1],[18.5,40.2],[16.9,40.4],[16.1,38.0],[15.4,40.0],[8.9,44.4],[3.1,43.1],[-2.1,36.7],[-8.9,36.9],[-9.4,43.0],[-1.4,44.0],[-1.2,46.0],[-4.6,48.7],[-1.6,48.6],[-1.9,49.8],[8.1,53.5],[8.5,57.1],[10.6,57.7],[9.6,55.5],[10.9,54.0],[19.7,54.4],[21.6,57.4],[24.1,57.0],[23.3,59.2],[29.1,60.0],[21.3,60.7],[21.5,63.2],[25.4,65.1],[23.9,66.0],[17.8,62.7],[17.1,61.3],[18.8,60.1],[15.9,56.1],[12.9,55.4],[10.4,59.5],[5.7,58.6],[5.0,62.0],[14.8,67.8],[28.2,71.2],[41.1,67.5],[38.4,66.0],[33.2,66.6],[37.0,63.9],[37.2,65.1],[44.0,66.1],[43.5,68.6],[46.3,68.2],[46.3,66.7],[53.7,68.9],[59.9,68.3],[60.6,69.9],[68.5,68.1],[66.7,71.0],[69.9,73.0],[72.8,72.2],[71.8,71.4],[73.7,68.4],[71.3,66.3],[72.4,66.2],[75.1,67.8],[73.1,71.4],[74.7,72.8],[76.4,71.2],[81.5,71.7],[80.5,73.6],[104.4,77.7],[114.1,75.8],[109.4,74.2],[127.0,73.6],[131.3,70.8],[139.9,71.5],[139.1,72.4],[140.5,72.8],[159.0,70.9],[160.9,69.4],[178.6,69.4],[-180.0,69.0],[-169.9,66.0],[-173.0,64.3],[-178.7,66.1],[-180.0,65.0],[180.0,65.0],[177.4,64.6],[179.2,62.3],[170.3,59.9],[163.5,59.9],[162.0,58.2],[163.2,57.6],[162.1,54.9],[156.8,51.0],[155.9,56.8],[164.5,62.6],[160.1,60.5],[156.7,61.4],[154.2,59.8],[155.0,59.1],[142.2,59.0],[135.1,54.7],[139.9,54.2],[141.4,52.2],[138.2,46.3],[127.5,39.8],[129.1,35.1],[126.5,34.4],[125.3,39.6],[121.1,38.9],[121.6,40.9],[118.0,39.2],[118.9,37.4],[122.4,37.5],[119.2,34.9],[121.9,31.7],[121.7,28.2],[115.9,22.8],[110.4,20.3],[108.5,21.7],[105.9,19.8],[109.3,13.4],[109.2,11.7],[105.2,8.6],[100.1,13.4],[99.2,9.2],[103.0,5.5],[104.2,1.3],[101.4,2.8],[98.3,7.8],[97.2,16.9],[94.2,16.0],[91.4,22.8],[87.0,21.5],[80.3,15.9],[79.9,10.4],[77.5,8.0],[72.6,21.4],[70.5,20.9],[66.4,25.4],[57.4,25.7],[56.5,27.1],[51.5,27.9],[50.1,30.1],[48.0,30.0],[51.8,24.0],[56.4,26.4],[56.8,24.2],[59.8,22.3],[55.3,17.2],[43.5,12.6],[42.6,16.8],[34.9,29.5],[33.9,27.6],[32.4,29.9],[37.5,18.6],[42.7,11.7],[44.6,10.4],[51.1,12.0],[48.6,5.3],[39.6,-4.3]],
  [[-141.0,69.7],[-136.5,68.9],[-128.1,70.5],[-113.5,67.7],[-106.1,68.8],[-101.5,67.6],[-97.7,68.6],[-96.1,67.3],[-94.2,69.1],[-96.5,70.1],[-95.2,71.9],[-87.3,67.2],[-85.5,69.9],[-82.6,69.7],[-81.4,67.1],[-85.8,66.6],[-94.2,60.9],[-94.7,58.9],[-92.3,57.1],[-82.3,55.1],[-79.9,51.2],[-78.6,52.6],[-79.8,54.7],[-76.5,56.5],[-78.5,58.8],[-77.3,59.9],[-78.1,62.3],[-73.8,62.4],[-69.6,61.1],[-67.7,58.2],[-64.6,60.3],[-61.8,56.3],[-55.8,53.3],[-60.0,50.2],[-66.4,50.2],[-71.1,46.8],[-65.1,49.2],[-64.5,46.2],[-60.5,47.0],[-59.8,45.9],[-65.4,43.5],[-66.2,44.5],[-64.4,45.3],[-67.1,45.1],[-70.6,43.1],[-70.0,41.6],[-75.5,39.5],[-75.9,37.2],[-76.4,39.1],[-75.7,35.6],[-81.3,31.4],[-80.4,25.2],[-83.7,29.9],[-86.4,30.4],[-93.8,29.7],[-97.4,27.4],[-97.9,22.4],[-95.9,18.8],[-91.4,18.9],[-90.3,21.0],[-87.1,21.5],[-88.9,15.9],[-83.4,15.3],[-83.8,11.1],[-82.2,9.0],[-76.8,8.6],[-71.8,12.4],[-71.7,9.1],[-69.9,12.2],[-68.2,10.6],[-61.9,10.7],[-57.1,6.0],[-51.3,4.2],[-50.7,0.2],[-48.6,-1.2],[-40.0,-2.9],[-35.2,-5.5],[-35.1,-9.0],[-38.7,-13.1],[-40.9,-21.9],[-47.6,-24.9],[-48.9,-28.7],[-53.8,-34.4],[-58.4,-33.9],[-56.8,-36.9],[-62.3,-38.8],[-62.7,-41.0],[-65.1,-41.1],[-63.5,-42.6],[-67.3,-45.6],[-66.0,-48.1],[-69.1,-50.7],[-68.2,-52.4],[-71.4,-53.9],[-74.9,-52.3],[-75.6,-48.7],[-74.1,-46.9],[-75.6,-46.6],[-72.7,-42.4],[-74.3,-43.2],[-70.2,-19.8],[-76.0,-14.6],[-81.3,-6.1],[-79.8,-2.7],[-80.9,-1.1],[-77.1,3.8],[-78.2,8.3],[-79.6,8.9],[-80.9,7.2],[-85.7,9.9],[-87.5,13.3],[-103.5,18.3],[-113.9,31.6],[-114.7,30.2],[-109.4,23.4],[-112.2,24.7],[-117.3,33.0],[-120.6,34.6],[-124.4,40.3],[-124.7,48.2],[-122.6,47.1],[-122.8,49.0],[-127.4,50.8],[-134.1,58.1],[-147.1,60.9],[-151.7,59.2],[-150.6,61.3],[-158.4,56.0],[-164.9,54.6],[-157.0,58.9],[-162.0,58.7],[-165.3,60.5],[-165.7,62.1],[-160.8,64.8],[-168.1,65.7],[-161.7,66.1],[-166.2,68.9],[-156.6,71.4],[-142.1,69.9]],
  [[-46.8,82.6],[-27.1,83.5],[-20.8,82.7],[-31.4,82.0],[-12.2,81.3],[-20.0,80.2],[-17.7,80.1],[-19.7,78.8],[-18.5,77.0],[-21.7,76.6],[-19.4,74.3],[-24.8,72.3],[-21.8,70.7],[-25.5,71.4],[-26.4,70.2],[-22.3,70.1],[-39.8,65.5],[-44.8,60.0],[-51.6,63.6],[-54.0,67.2],[-50.9,69.9],[-54.7,69.6],[-54.4,70.8],[-51.4,70.6],[-55.8,71.7],[-54.7,72.6],[-58.6,75.5],[-68.5,76.1],[-71.4,77.0],[-66.8,77.4],[-73.3,78.0],[-65.7,79.4],[-68.0,80.1],[-62.6,81.8],[-46.9,82.2]],
  [[126.1,-32.2],[118.0,-35.1],[115.0,-34.2],[115.7,-31.6],[113.3,-26.1],[114.1,-21.8],[120.9,-19.7],[125.7,-14.2],[129.6,-15.0],[132.4,-11.1],[136.5,-11.9],[135.5,-15.0],[140.2,-17.7],[142.5,-10.7],[146.4,-19.0],[153.1,-26.1],[153.1,-30.9],[150.0,-37.4],[143.6,-38.8],[140.6,-38.0],[138.2,-34.4],[136.8,-35.3],[137.8,-32.9],[136.0,-34.9],[131.3,-31.5],[127.1,-32.3]],
  [[-78.8,72.4],[-68.8,70.5],[-67.0,69.2],[-68.8,68.7],[-61.9,66.9],[-63.9,65.0],[-68.0,66.3],[-64.7,63.4],[-68.8,63.7],[-66.2,61.9],[-78.6,64.6],[-74.0,65.5],[-72.7,67.3],[-77.3,69.8],[-89.5,70.8],[-88.5,71.2],[-90.2,72.2],[-88.4,73.5],[-85.8,73.8],[-85.8,72.5],[-82.3,73.8],[-80.8,72.1]],
  [[141.0,-2.6],[147.6,-6.1],[147.2,-7.4],[150.8,-10.3],[147.9,-10.1],[144.7,-7.6],[141.0,-9.1],[137.6,-8.4],[137.9,-5.4],[133.0,-4.1],[132.0,-2.8],[133.7,-2.2],[130.5,-0.9],[134.0,-0.8],[135.5,-3.4],[137.4,-1.7],[139.9,-2.4]],
  [[-91.6,81.9],[-61.9,82.6],[-76.9,79.3],[-75.4,78.5],[-80.6,76.2],[-89.5,76.5],[-88.3,77.9],[-85.0,77.5],[-88.0,78.4],[-85.1,79.3],[-86.9,80.3],[-81.8,80.5],[-91.4,81.6]],
  [[-3.1,53.4],[-6.2,56.8],[-5.0,58.6],[-3.0,58.6],[-4.1,57.6],[-2.0,57.7],[-3.1,56.0],[1.7,52.7],[1.4,51.3],[-5.2,50.0],[-3.4,51.4],[-5.3,52.0],[-4.6,53.5]],
  [[-106.5,73.1],[-101.1,69.6],[-113.3,68.5],[-117.3,70.0],[-112.4,70.4],[-117.9,70.5],[-116.1,71.3],[-119.4,71.6],[-118.6,72.3],[-115.2,73.3],[-108.2,71.7],[-107.5,73.2]],
  [[122.9,0.9],[125.1,1.6],[124.4,0.4],[120.0,-0.5],[123.3,-0.6],[121.5,-1.9],[123.2,-5.3],[121.0,-2.6],[119.8,-5.7],[118.8,-2.8],[120.0,0.6],[121.7,1.0]],
  [[141.9,39.2],[140.3,35.1],[135.8,33.5],[135.1,34.6],[131.0,33.9],[132.0,33.1],[130.7,31.0],[129.4,33.3],[139.4,38.2],[140.3,41.2],[141.9,40.0]],
  [[115.5,5.4],[117.1,6.9],[119.2,5.4],[117.3,3.2],[119.0,0.9],[116.1,-4.0],[110.2,-2.9],[109.1,-0.5],[109.7,2.0],[114.6,4.9]],
  [[-107.8,75.8],[-105.9,76.0],[-106.3,75.0],[-112.2,74.4],[-117.7,75.2],[-115.4,76.5],[-108.2,76.2]],
  [[-100.4,72.7],[-101.5,73.4],[-100.4,73.8],[-97.4,73.8],[-96.7,71.7],[-98.4,71.3],[-102.5,72.8]],
  [[53.5,73.8],[68.2,76.9],[58.5,74.3],[55.4,72.4],[57.5,70.7],[51.6,71.5],[54.4,73.6]],
  [[176.9,-40.1],[174.7,-41.3],[174.7,-37.4],[172.6,-34.5],[176.0,-37.6],[178.5,-37.7],[177.0,-39.9]],
  [[-55.6,51.3],[-56.8,49.8],[-53.5,49.2],[-53.1,46.7],[-59.4,47.9],[-55.4,51.6]],
  [[-121.5,74.4],[-115.5,73.5],[-123.1,70.9],[-125.9,71.9],[-123.9,73.7],[-124.9,74.3]],
  [[-68.6,-52.6],[-65.1,-54.7],[-69.2,-55.5],[-74.7,-52.8],[-71.1,-54.1],[-69.3,-52.5]],
]

_DASHBOARD_CSS = """
*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
:root {
  /* ── 목업 v2: 딥네이비/코발트 테마 ── 변수명 유지 → 전 렌더러 자동 리테마 */
  --bg:      #141b38;   /* 편안한 다크 네이비 (near-black에서 리프트, 눈 편하게) */
  --void:    #141b38;
  --surface: #1e2750;   /* 카드 (리프트) */
  --ink:     #1e2750;
  --ink2:    #27315e;
  --elevated:#27315e;
  --deep:    #2f3a6b;
  --raise:   #2f3a6b;
  --border:  #3a4682;   /* inkline-violet 밝게 */
  --bhi:     #4c5aa0;
  /* 액센트 — 코발트/라벤더/틸 */
  --champ:   #8b95ff;   /* 주 텍스트 액센트 (구 골드 대체) */
  --gold:    #8b95ff;
  --accent:  #8b95ff;
  --champ2:  #aab1f2;   /* quartz-lavender */
  --champ-d: #8189cc;   /* 딤 라벤더 (eyebrow) */
  --cobalt:  #3d50fc;   /* pulse-cobalt (강조 fill) */
  --blue:    #5b8def;
  --violet:  #8b7cf6;
  --teal:    #05e0e0;   /* signal-teal */
  --hi:      #ffffff;   /* 순백 (본문 강조 — 최대 대비) */
  --mid:     #e7eafc;   /* near-white (본문 — 확실히 잘 보이게) */
  --lo:      #c6ccf2;   /* 보조 텍스트 (밝게 — 흐릿함 제거) */
  --high:    #ff6b7a;
  --med:     #f0a256;
  --coral:   #ff6b7a;
  --amber:   #f0a256;
  --shadow:  0 2px 12px rgba(0,0,0,.45);
  --mono: ui-monospace, "SF Mono", "JetBrains Mono", Menlo, Consolas, monospace;
  /* ── 간격·radius·타이포 토큰 (목업: 둥근 카드) ── */
  --space-1: 4px; --space-2: 8px; --space-3: 12px; --space-4: 16px; --space-5: 24px;
  --radius: 14px; --radius-sm: 9px; --pad-card: 18px; --badge-r: 8px;
  --fs-hero: 25px; --fs-num: 29px; --fs-title: 17px; --fs-body: 15px;
  --fs-small: 13.5px; --fs-label: 11.5px;
  --fw-bold: 800; --fw-semi: 650; --fw-med: 600;
  --card: var(--ink); --card-bd: 1px solid var(--border);
}
/* ── v2 공통 카드/배지 시스템 ── */
.card-sys { background: var(--card); border: var(--card-bd); border-radius: var(--radius); padding: var(--pad-card); }
.badge { font-family: var(--mono); font-size: 12px; font-weight: 700; padding: 2px 7px;
  border-radius: var(--badge-r); letter-spacing: .04em; white-space: nowrap; display: inline-block; }
.badge.b-high{ background: rgba(255,106,86,.15); color: var(--coral); }
.badge.b-med { background: rgba(242,169,59,.16); color: var(--amber); }
.badge.b-real{ background: rgba(70,214,195,.15); color: var(--teal); }
.badge.b-gold{ background: rgba(139,149,255,.16); color: var(--champ); }
.badge.b-mute{ background: rgba(154,164,166,.16); color: var(--mid); }
/* 정보 (?) 툴팁 */
.info { position: relative; display: inline-flex; align-items: center; justify-content: center;
  width: 14px; height: 14px; border: 1px solid var(--bhi); border-radius: 50%; color: var(--lo);
  font-size: 12px; font-family: var(--mono); cursor: help; margin-left: 6px; }
.info:hover::after { content: attr(data-tip); position: absolute; bottom: 150%; left: 50%; transform: translateX(-50%);
  width: 240px; background: #05090c; border: 1px solid var(--bhi); border-radius: 6px; padding: 8px 10px;
  font-family: var(--sans, system-ui); font-size: 12.5px; font-weight: 400; line-height: 1.5; color: var(--mid);
  letter-spacing: 0; text-transform: none; z-index: 50; box-shadow: 0 6px 20px rgba(0,0,0,.5); }
body {
  font-family: system-ui, -apple-system, "Segoe UI", "Malgun Gothic", "Noto Sans KR", sans-serif;
  background:
    radial-gradient(900px 500px at 78% -8%, rgba(139,149,255,.05), transparent 55%),
    radial-gradient(1100px 700px at 12% 112%, rgba(70,130,150,.05), transparent 55%),
    var(--bg);
  color: var(--hi);
  font-size: 14px;
  line-height: 1.55;
}
a { color: var(--blue); text-decoration: none; }
a:hover { color: var(--gold); }

/* ── Header ── */
.page-header {
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  padding: 0 28px;
  height: 52px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  position: static;
  z-index: 100;
}
.page-header-brand {
  display: flex;
  align-items: center;
  gap: 14px;
}
.page-header-accent {
  width: 3px;
  height: 22px;
  background: linear-gradient(180deg, var(--gold), transparent);
  border-radius: 1px;
  flex-shrink: 0;
}
.page-header h1 {
  font-size: 12.5px;
  font-weight: 800;
  letter-spacing: 0.22em;
  text-transform: uppercase;
  color: var(--hi);
}
.page-header .meta {
  font-size: 12.5px;
  color: var(--mid);
  letter-spacing: 0.02em;
}
.page-header .meta span { color: var(--gold); }

/* ── Layout ── */
.page-body { max-width: 1500px; margin: 0 auto; padding: 20px 24px 64px; }
.section {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 20px 22px;
  margin-bottom: 26px;
  box-shadow: var(--shadow);
  transition: box-shadow 0.2s ease, transform 0.2s ease, border-color 0.2s ease;
}
.section:hover {
  box-shadow: 0 6px 20px rgba(0,0,0,.45);
  transform: translateY(-2px);
  border-color: rgba(139,149,255,.22);
}
.section-title {
  font-size: 13px;
  font-weight: 700;
  color: var(--hi);
  letter-spacing: 0.1em;
  text-transform: uppercase;
  margin-bottom: 16px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--border);
  display: flex;
  align-items: center;
  gap: 10px;
}
.section-title::before {
  content: '';
  display: block;
  width: 2px;
  height: 12px;
  background: var(--gold);
  border-radius: 1px;
  flex-shrink: 0;
}
.section-sub {
  font-size: 11.5px;
  color: var(--lo);
  font-weight: 400;
  letter-spacing: 0.02em;
  text-transform: none;
  flex: 1;
  min-width: 0;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.collapse-btn {
  flex-shrink: 0;
  background: none;
  border: 1px solid var(--border);
  border-radius: 2px;
  padding: 2px 8px;
  font-size: 11.5px;
  color: var(--mid);
  cursor: pointer;
  font-family: inherit;
  white-space: nowrap;
  letter-spacing: 0.04em;
  transition: all 0.15s;
}
.collapse-btn:hover { border-color: var(--gold); color: var(--gold); }

/* ── Period row ── */
/* ── 탭바 ── */
.tabbar {
  position: static; z-index: 40;
  background: var(--surface);
  border-bottom: 1px solid var(--border);
  padding: 9px 28px;
  display: flex; align-items: center; justify-content: space-between;
  gap: 16px; flex-wrap: wrap;
  box-shadow: 0 1px 6px rgba(23,33,64,.04);
}
.tabbar-tabs { display: flex; gap: 6px; flex-wrap: wrap; }
.tab-btn {
  background: transparent; border: 1px solid transparent;
  border-radius: 9px; padding: 8px 16px;
  font-size: 13.5px; font-weight: 700; color: var(--lo);
  cursor: pointer; transition: all 0.15s; font-family: inherit;
}
.tab-btn:hover { color: var(--hi); background: var(--deep); }
.tab-btn.active { color: var(--gold); background: rgba(61,80,252,0.12); border-color: rgba(61,80,252,0.30); }
.tab-panel { display: none; }
.tab-panel.active { display: block; animation: tabfade 0.18s ease; }
@keyframes tabfade { from { opacity: 0; transform: translateY(3px); } to { opacity: 1; transform: none; } }

/* ── 개요 종합 요약 (digest) ── */
.dg-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 14px; }
@media (max-width: 860px) { .dg-grid { grid-template-columns: 1fr; } }
.dg-card { background: var(--deep); border: 1px solid var(--border); border-radius: 10px; padding: 13px 15px; }
.dg-card-h, .dg-block-h { font-size: 13px; font-weight: 800; color: var(--hi); margin-bottom: 10px; display: flex; align-items: center; gap: 8px; }
.dg-jump { margin-left: auto; font-size: 12.5px; font-weight: 700; color: var(--gold); background: rgba(61,80,252,0.10); border: 1px solid rgba(61,80,252,0.28); border-radius: 999px; padding: 3px 10px; cursor: pointer; font-family: inherit; transition: all .15s; }
.dg-jump:hover { background: rgba(61,80,252,0.20); }
.dg-li { display: flex; align-items: center; gap: 7px; font-size: 12.5px; padding: 5px 0; border-top: 1px solid var(--border); color: var(--hi); flex-wrap: wrap; }
.dg-li:first-of-type { border-top: 0; }
.dg-li b { font-weight: 800; }
.dg-up { color: #1f9d6a; font-weight: 800; } .dg-down { color: var(--high); font-weight: 800; }
.dg-x { color: #1f9d6a; font-weight: 800; font-size: 12px; } .dg-x-dn { color: var(--high); }
.dg-cnt { font-weight: 700; color: var(--mid); font-variant-numeric: tabular-nums; }
.dg-hi { color: var(--high); font-weight: 800; }
.dg-sub { margin-left: auto; font-size: 12.5px; color: var(--lo); }
.dg-empty { font-size: 12px; color: var(--lo); font-style: italic; padding: 6px 0; }
.dg-block { background: var(--deep); border: 1px solid var(--border); border-radius: 10px; padding: 13px 15px; margin-bottom: 14px; }
.dg-block:last-child { margin-bottom: 0; }
.dg-moves { display: flex; flex-direction: column; }
.dg-move { display: flex; align-items: center; gap: 9px; padding: 7px 0; border-top: 1px solid var(--border); font-size: 12.5px; }
.dg-move:first-child { border-top: 0; }
.dg-badge { font-size: 9.5px; font-weight: 800; padding: 2px 6px; border-radius: 4px; flex-shrink: 0; }
.dg-imp-high { background: rgba(224,72,63,0.14); color: #d0322b; } .dg-imp-med { background: rgba(224,160,64,0.16); color: #a86a12; }
.dg-sc { font-weight: 800; color: var(--gold); font-variant-numeric: tabular-nums; min-width: 24px; text-align: right; flex-shrink: 0; }
.dg-mv-b { font-weight: 800; color: var(--hi); flex-shrink: 0; }
.dg-mv-cc { color: var(--lo); font-size: 11.5px; flex-shrink: 0; }
.dg-mv-act { font-size: 12px; color: #7a5fc0; font-weight: 700; flex-shrink: 0; }
.dg-mv-t { color: var(--mid); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.dg-mv-t a { color: var(--mid); } .dg-mv-t a:hover { color: var(--gold); }
.dg-insight { }
.dg-ins-ul { margin: 0; padding-left: 2px; list-style: none; }
.dg-ins-ul li { position: relative; padding-left: 15px; margin: 5px 0; font-size: 13px; line-height: 1.6; color: var(--hi); }
.dg-ins-ul li::before { content: '▸'; position: absolute; left: 0; color: var(--gold); }
/* digest v2: KPI 라인 · 내러티브 · 무브 카드 */
.dg-kpi { display: flex; gap: 18px; flex-wrap: wrap; margin-bottom: 14px; font-size: 13px; color: var(--mid); }
.dg-kpi b { color: var(--hi); font-weight: 800; font-variant-numeric: tabular-nums; margin-left: 2px; }
/* 3색 인사이트 컬럼 */
.dg-cols { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
@media (max-width: 860px) { .dg-cols { grid-template-columns: 1fr; } }
.dg-col { border-radius: 10px; padding: 13px 15px; border: 1px solid var(--border); }
.dg-col-respond { background: rgba(224,72,63,0.06); border-color: rgba(224,72,63,0.22); }
.dg-col-opportunity { background: rgba(31,157,106,0.06); border-color: rgba(31,157,106,0.22); }
.dg-col-check { background: rgba(61,80,252,0.06); border-color: rgba(61,80,252,0.22); }
.dg-col-h { font-size: 12.5px; font-weight: 800; margin-bottom: 9px; letter-spacing: 0.01em; }
.dg-col-respond .dg-col-h { color: #d0322b; }
.dg-col-opportunity .dg-col-h { color: #1f9d6a; }
.dg-col-check .dg-col-h { color: #9a7433; }
.dg-col ul { margin: 0; padding-left: 2px; list-style: none; }
.dg-col li { position: relative; padding-left: 14px; margin: 7px 0; font-size: 12.5px; line-height: 1.6; color: var(--hi); }
.dg-col li::before { content: '▸'; position: absolute; left: 0; opacity: 0.6; }
.dg-move2 { padding: 9px 0; border-top: 1px solid var(--border); }
.dg-move2:first-child { border-top: 0; }
.dg-move2-top { display: flex; align-items: center; gap: 9px; flex-wrap: wrap; }
.dg-move2 .dg-mv-t { white-space: normal; margin-top: 4px; color: var(--hi); font-weight: 500; font-size: 13px; line-height: 1.5; }
.dg-move2 .dg-mv-t a { color: var(--hi); } .dg-move2 .dg-mv-t a:hover { color: var(--gold); }
.dg-mv-det { font-size: 12px; color: var(--lo); margin-top: 3px; line-height: 1.5; }
@media (max-width: 640px) { .dg-mv-t { white-space: normal; } .dg-move { flex-wrap: wrap; } }

.period-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
/* 탭바 안에 들어온 기간 컨트롤은 컴팩트하게 (자체 카드 스타일 제거) */
.tabbar .period-row { margin-bottom: 0; }
.period-row-label {
  font-size: 12px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  color: var(--lo);
  white-space: nowrap;
}
.period-basis-hint { margin-left:7px; font-size:9.5px; font-weight:600; color:var(--champ);
  background:rgba(139,149,255,.12); border-radius:6px; padding:1px 7px; letter-spacing:0; cursor:help; text-transform:none; }
.period-presets { display: flex; gap: 4px; }
.period-btn {
  background: transparent;
  border: 1px solid var(--border);
  border-radius: 2px;
  padding: 3px 12px;
  font-size: 12.5px;
  font-weight: 600;
  color: var(--mid);
  cursor: pointer;
  transition: all 0.15s;
  font-family: inherit;
  white-space: nowrap;
  letter-spacing: 0.03em;
}
.period-btn:hover { border-color: var(--gold); color: var(--gold); }
.period-btn.active { background: var(--gold); color: var(--bg); border-color: var(--gold); font-weight: 700; }
.period-vsep { width: 1px; height: 20px; background: var(--border); flex-shrink: 0; }
.period-range { display: flex; align-items: center; gap: 6px; flex-wrap: nowrap; }
.period-date-input {
  padding: 3px 8px;
  border: 1px solid var(--border);
  border-radius: 2px;
  font-size: 12.5px;
  font-family: inherit;
  color: var(--hi);
  background: var(--bg);
  width: 126px;
  cursor: pointer;
}
.period-date-input:focus { outline: none; border-color: var(--gold); box-shadow: 0 0 0 2px rgba(200,169,110,0.15); }
.period-date-sep { font-size: 12.5px; color: var(--lo); }
.period-apply-btn {
  background: rgba(74,143,212,0.12);
  color: var(--blue);
  border: 1px solid rgba(74,143,212,0.35);
  border-radius: 2px;
  padding: 3px 12px;
  font-size: 12.5px;
  font-weight: 600;
  cursor: pointer;
  font-family: inherit;
  white-space: nowrap;
  transition: all 0.15s;
}
.period-apply-btn:hover { background: rgba(74,143,212,0.22); }
.period-msg { width: 100%; font-size: 11.5px; color: #e07e40; font-weight: 500; margin-top: 2px; }

/* ── KPI ── */
.kpi-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
@media (max-width: 680px) { .kpi-grid { grid-template-columns: repeat(2, 1fr); } }
.kpi-card {
  background: var(--elevated);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 20px 18px 16px;
  position: relative;
  overflow: hidden;
  box-shadow: var(--shadow);
}
.kpi-card::after {
  content: '';
  position: absolute;
  bottom: 0; left: 0;
  width: 100%; height: 1px;
  background: var(--gold);
  opacity: 0.3;
}
.kpi-value {
  font-size: 34px;
  font-weight: 800;
  line-height: 1.05;
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.02em;
}
.kpi-unit { font-size: 13px; font-weight: 400; margin-left: 2px; color: var(--mid); }
.kpi-label {
  font-size: 12.5px;
  color: var(--mid);
  margin-top: 8px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.1em;
}

/* ── Tables ── */
.table-wrap { overflow-x: auto; overflow-y: auto; max-height: 620px; }
.data-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.data-table th {
  background: var(--deep);
  color: var(--mid);
  font-weight: 700;
  font-size: 12px;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  padding: 9px 10px;
  text-align: left;
  white-space: nowrap;
  position: sticky;
  top: 0;
  z-index: 3;
  border-bottom: 1px solid var(--border);
  box-shadow: 0 1px 0 var(--border);
}
.data-table td {
  padding: 7px 10px;
  border-bottom: 1px solid var(--border);
  vertical-align: top;
  color: var(--hi);
}
.data-table tbody .main-row:hover td { background: var(--elevated); }
.main-row { cursor: pointer; }
.main-row td { transition: background 0.1s; }

/* ── Drilldown rows ── */
.detail-row.hidden { display: none; }
.detail-box {
  background: var(--elevated);
  border-left: 2px solid var(--blue);
  padding: 10px 14px;
  border-radius: 0 3px 3px 0;
  margin: 2px 0;
}
.detail-box p { margin-bottom: 5px; color: var(--hi); font-size: 12px; }
.detail-box .note-line { color: var(--med); }
.detail-box .src-info { color: var(--mid); font-size: 11.5px; margin-top: 6px; }
.detail-box .title-ko-line { color: var(--blue); font-size: 12.5px; margin-bottom: 4px; }
.body-orig { margin-top: 8px; }
.body-orig summary { font-size: 11.5px; color: var(--mid); cursor: pointer; }
.body-text {
  font-size: 11.5px; line-height: 1.5; color: var(--mid);
  background: var(--bg); padding: 8px 10px; border-radius: 2px;
  margin-top: 4px; white-space: pre-wrap; word-break: break-word;
  max-height: 180px; overflow-y: auto;
}

/* ── Tags / badges ── */
.imp-badge {
  display: inline-block;
  padding: 1px 5px; border-radius: 2px;
  font-size: 12px; font-weight: 700; white-space: nowrap;
  vertical-align: middle; margin-right: 2px;
  letter-spacing: 0.06em;
}
.imp-high { background: rgba(224,83,83,0.15); color: #e05353; }
.imp-med  { background: rgba(212,148,58,0.15); color: #d4943a; }
.brand-tag {
  background: rgba(74,143,212,0.1);
  color: var(--blue);
  padding: 1px 7px; border-radius: 2px;
  font-size: 11.5px; font-weight: 700; white-space: nowrap;
  letter-spacing: 0.04em;
}
.act-tag {
  background: rgba(200,169,110,0.1);
  color: var(--gold);
  padding: 1px 7px; border-radius: 2px;
  font-size: 11.5px; white-space: nowrap;
}
.date-cell { color: var(--mid); font-size: 12.5px; white-space: nowrap; font-variant-numeric: tabular-nums; }
.flag-cell { white-space: nowrap; }
.conf-cell { color: var(--mid); font-size: 12.5px; text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
.title-cell { max-width: 480px; word-break: break-word; }

/* ── Heatmap ── */
.heatmap-wrap { overflow-x: auto; overflow-y: auto; max-height: 560px; }
.heatmap-table th {
  position: sticky; top: 0; z-index: 3;
  background: var(--deep);
  box-shadow: 0 1px 0 var(--border);
}
.heatmap-table .sticky-col {
  position: sticky; left: 0;
  background: var(--deep) !important;
  color: var(--mid) !important;
  z-index: 3; min-width: 110px;
}
.heatmap-table thead .sticky-col { z-index: 4; }
.heatmap-table td {
  text-align: center; min-width: 44px; max-width: 60px;
  font-size: 12.5px; font-weight: 600; font-variant-numeric: tabular-nums;
  border-bottom: 1px solid rgba(255,255,255,0.04);
}
.brand-name { font-weight: 600; font-size: 12.5px; }
.total-cell {
  background: var(--deep) !important;
  color: var(--hi) !important;
  font-weight: 700;
  border-left: 1px solid var(--border);
  position: sticky; right: 0; z-index: 1;
  font-variant-numeric: tabular-nums;
}
.total-row td {
  background: var(--deep) !important;
  color: var(--mid) !important;
  font-weight: 700;
}

/* ── Charts layout ── */
.charts-row { display: grid; grid-template-columns: 3fr 2fr; gap: 16px; }
@media (max-width: 900px) { .charts-row { grid-template-columns: 1fr; } }
.chart-section {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 20px 22px;
  box-shadow: var(--shadow);
}
.chart-container { position: relative; height: 260px; }
.chart-sm { height: 240px; }
.no-data { color: var(--lo); font-style: italic; padding: 12px 0; font-size: 12px; }

/* ── Filter bar ── */
.filter-bar {
  display: flex; gap: 6px; flex-wrap: wrap; align-items: center;
  padding: 10px 12px;
  background: var(--elevated);
  border: 1px solid var(--border);
  border-radius: 3px;
  margin-bottom: 14px;
}
.filter-group { display: flex; gap: 4px; flex-wrap: wrap; align-items: center; }
.filter-sep { width: 1px; height: 18px; background: var(--border); margin: 0 4px; }
.filter-label {
  font-size: 12px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.1em; color: var(--lo); margin-right: 2px; white-space: nowrap;
}
.filter-pill {
  background: transparent;
  border: 1px solid var(--border);
  border-radius: 2px;
  padding: 2px 10px;
  font-size: 11.5px; font-weight: 500; color: var(--mid);
  cursor: pointer; transition: all 0.12s; white-space: nowrap; font-family: inherit;
  letter-spacing: 0.02em;
}
.filter-pill:hover { border-color: var(--gold); color: var(--gold); }
.filter-pill.active { background: var(--gold); color: var(--bg); border-color: var(--gold); font-weight: 700; }
.filter-pill.act-active { background: rgba(74,143,212,0.15); color: var(--blue); border-color: var(--blue); }
.filter-count { font-size: 11.5px; color: var(--lo); margin-left: 4px; white-space: nowrap; }

/* ── Lower 2-col ── */
.lower-row { display: grid; grid-template-columns: minmax(0, 1fr) 300px; gap: 16px; margin-bottom: 16px; }
.lower-row > * { min-width: 0; }
@media (max-width: 900px) { .lower-row { grid-template-columns: 1fr; } }

/* ── Brand HIGH ratio ── */
.high-ratio-wrap { display: flex; flex-direction: column; gap: 10px; }
.hr-row { display: flex; align-items: center; gap: 8px; }
.hr-brand { font-size: 12.5px; font-weight: 600; color: var(--hi); min-width: 86px; white-space: nowrap; }
.hr-bar-bg { flex: 1; height: 12px; background: var(--elevated); border-radius: 1px; overflow: hidden; }
.hr-bar-fill {
  height: 100%;
  background: linear-gradient(90deg, #a83838, #e05353);
  border-radius: 1px;
  transition: width 0.5s ease;
}
.hr-badge { font-size: 11.5px; font-weight: 700; color: #e05353; min-width: 36px; text-align: right; font-variant-numeric: tabular-nums; }
.hr-meta { font-size: 11.5px; color: var(--mid); white-space: nowrap; min-width: 55px; font-variant-numeric: tabular-nums; }

/* ── Stacked bar ── */
.stacked-wrap { position: relative; }
.legend-row { display: flex; gap: 12px; flex-wrap: wrap; margin-top: 10px; }
.legend-item { display: flex; align-items: center; gap: 4px; font-size: 11.5px; color: var(--mid); }
.legend-dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; flex-shrink: 0; }

/* ── Drilldown panel ── */
.dd-overlay {
  position: fixed; inset: 0; background: rgba(0,0,0,0.65); z-index: 200;
  display: none; backdrop-filter: blur(2px);
}
.dd-panel {
  position: fixed; top: 0; right: 0; width: 400px; max-width: 92vw;
  height: 100%; background: var(--surface); overflow-y: auto; z-index: 201;
  transform: translateX(100%); transition: transform 0.22s ease;
  border-left: 1px solid var(--border);
}
.dd-panel.open { transform: translateX(0); }
.dd-header {
  position: sticky; top: 0;
  background: var(--deep);
  border-bottom: 1px solid var(--border);
  padding: 14px 16px;
  display: flex; justify-content: space-between; align-items: flex-start; gap: 10px;
}
.dd-header h3 { font-size: 15px; font-weight: 700; margin: 0; color: var(--hi); letter-spacing: 0.02em; }
.dd-header p { font-size: 11.5px; color: var(--mid); margin: 3px 0 0; }
.dd-close {
  background: rgba(255,255,255,0.05); border: 1px solid var(--border); color: var(--mid);
  width: 24px; height: 24px; border-radius: 2px; cursor: pointer;
  font-size: 14px; display: flex; align-items: center; justify-content: center; flex-shrink: 0;
}
.dd-close:hover { color: var(--hi); border-color: var(--gold); }
.dd-body { padding: 12px 16px; }
.dd-empty { text-align: center; padding: 40px 20px; color: var(--lo); font-size: 12px; }
.dd-item {
  border: 1px solid var(--border); border-radius: 3px; padding: 10px 12px;
  margin-bottom: 8px; background: var(--elevated);
}
.dd-item-top { display: flex; gap: 6px; align-items: center; margin-bottom: 5px; }
.dd-date { font-size: 12.5px; color: var(--mid); white-space: nowrap; font-variant-numeric: tabular-nums; }
.dd-act-chip {
  font-size: 12px; font-weight: 700; padding: 1px 8px;
  border-radius: 2px; white-space: nowrap;
  background: rgba(111,176,236,0.14); color: var(--blue);
  letter-spacing: 0.04em;
}
.dd-title { font-size: 13.5px; color: var(--hi); line-height: 1.5; font-weight: 500; }
.dd-link { display: inline-block; margin-top: 5px; font-size: 12.5px; color: var(--blue); }
.dd-link:hover { color: var(--gold); }
/* 브랜드×국가 전략 요약 카드 */
.dd-summary {
  margin: 14px 16px 0;
  padding: 12px 14px 13px 15px;
  background: linear-gradient(180deg, rgba(200,169,110,0.09), rgba(200,169,110,0.03));
  border: 1px solid rgba(200,169,110,0.22);
  border-left: 3px solid var(--gold);
  border-radius: 3px;
}
.dd-sum-label {
  font-size: 12.5px; font-weight: 800; letter-spacing: 0.14em; text-transform: uppercase;
  color: var(--gold); margin-bottom: 7px;
}
.dd-sum-body { font-size: 13.5px; line-height: 1.7; color: var(--hi); }
.dd-sum-spin {
  display: inline-block; width: 11px; height: 11px; margin-right: 7px; vertical-align: -1px;
  border: 2px solid rgba(200,169,110,0.25); border-top-color: var(--gold);
  border-radius: 50%; animation: ddspin 0.8s linear infinite;
}
@keyframes ddspin { to { transform: rotate(360deg); } }
/* 주력 활동 칩 (1차 직관성) */
.dd-act-row { margin-bottom: 12px; }
.dd-act-row-h {
  font-size: 11.5px; font-weight: 700; letter-spacing: 0.12em; text-transform: uppercase;
  color: var(--lo); margin-bottom: 6px;
}
.dd-act-chips { display: flex; flex-wrap: wrap; gap: 6px; }
.dd-act-focus {
  font-size: 12px; font-weight: 600; padding: 3px 9px;
  border: 1px solid; border-radius: 12px; white-space: nowrap;
  background: rgba(255,255,255,0.025);
}
.dd-act-focus b { font-weight: 800; }
/* 구조화 요약 섹션 */
.dd-sum-sec { margin-bottom: 12px; }
.dd-sum-sec:last-child { margin-bottom: 0; }
.dd-sum-sec-h {
  font-size: 12px; font-weight: 800; color: var(--gold);
  margin-bottom: 4px; letter-spacing: 0.02em;
}
.dd-sum-sec-b { font-size: 13.5px; line-height: 1.68; color: var(--hi); }

/* ── Brand Radar ── */
.radar-list { display: flex; flex-direction: column; gap: 7px; }
.radar-row {
  display: flex; align-items: center; gap: 8px;
  padding: 7px 10px;
  background: var(--elevated); border: 1px solid var(--border); border-radius: 3px;
}
.radar-icon { font-size: 11.5px; font-weight: 700; flex-shrink: 0; width: 12px; text-align: center; }
.radar-brand { font-size: 12px; font-weight: 600; color: var(--hi); min-width: 130px; white-space: nowrap; }
.radar-tier1 {
  font-size: 12px; font-weight: 700; padding: 1px 6px; border-radius: 2px;
  background: rgba(200,169,110,0.12); color: var(--gold);
  letter-spacing: 0.06em; white-space: nowrap; flex-shrink: 0;
}
.radar-tier2 {
  font-size: 12px; font-weight: 700; padding: 1px 6px; border-radius: 2px;
  background: rgba(78,88,112,0.3); color: var(--mid);
  letter-spacing: 0.06em; white-space: nowrap; flex-shrink: 0;
}
.radar-bar-bg { flex: 1; height: 6px; background: var(--deep); border-radius: 1px; overflow: hidden; min-width: 60px; }
.radar-bar-fill { height: 100%; border-radius: 1px; transition: width 0.5s ease; }
.radar-score { font-size: 12.5px; font-weight: 700; min-width: 36px; text-align: right; font-variant-numeric: tabular-nums; }
.radar-meta { font-size: 11.5px; color: var(--lo); white-space: nowrap; min-width: 80px; font-variant-numeric: tabular-nums; }
.radar-promo {
  font-size: 12px; font-weight: 700; padding: 1px 6px; border-radius: 2px; white-space: nowrap;
  background: rgba(74,184,132,0.15); color: #4ab884; letter-spacing: 0.04em; flex-shrink: 0;
}
.radar-demote {
  font-size: 12px; font-weight: 700; padding: 1px 6px; border-radius: 2px; white-space: nowrap;
  background: rgba(224,83,83,0.12); color: #e05353; letter-spacing: 0.04em; flex-shrink: 0;
}

/* ── 수요 검증 (뉴스 vs 검색) ── */
.ds-help { font-size: 12px; color: var(--lo); margin-bottom: 9px; display: flex; flex-wrap: wrap; align-items: center; gap: 4px; }
.ds-list { display: flex; flex-direction: column; gap: 7px; }
.ds-row {
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  padding: 7px 10px; background: var(--elevated); border: 1px solid var(--border); border-radius: 3px;
}
.ds-brand { font-size: 12px; font-weight: 600; color: var(--hi); min-width: 130px; white-space: nowrap; }
.ds-badge { font-size: 12px; font-weight: 700; padding: 1px 7px; border-radius: 2px; letter-spacing: 0.04em; white-space: nowrap; flex-shrink: 0; }
.ds-real   { background: rgba(74,184,132,0.16); color: #4ab884; }
.ds-latent { background: rgba(200,169,110,0.16); color: var(--gold); }
.ds-pr     { background: rgba(224,83,83,0.13); color: #e05353; }
.ds-stable { background: rgba(78,88,112,0.28); color: var(--mid); }
.ds-leg { font-size: 12.5px; color: var(--lo); white-space: nowrap; font-variant-numeric: tabular-nums; }
.ds-idx { font-size: 11.5px; color: var(--lo); margin-left: auto; white-space: nowrap; font-variant-numeric: tabular-nums; }

/* ── 화장품 수출 성장 (관세청) ── */
.xg-help { font-size: 12px; color: var(--lo); margin-bottom: 9px; }
.xg-list { display: flex; flex-direction: column; gap: 6px; }
.xg-row { display: flex; align-items: center; gap: 9px; padding: 6px 10px;
  background: var(--elevated); border: 1px solid var(--border); border-radius: 3px; }
.xg-flag { font-size: 15px; flex-shrink: 0; }
.xg-name { font-size: 12px; font-weight: 600; color: var(--hi); min-width: 84px; white-space: nowrap; }
.xg-bar-bg { position: relative; flex: 1; height: 11px; background: var(--deep); border-radius: 2px; overflow: hidden; min-width: 60px; }
.xg-bar-cur { position: absolute; left: 0; top: 0; height: 100%; border-radius: 2px;
  background: linear-gradient(90deg, rgba(70,214,195,.5), rgba(70,214,195,.85)); }
.xg-bar-prev { position: absolute; left: 0; top: 0; height: 100%; border-radius: 2px 0 0 2px;
  background: rgba(154,164,166,.32); }
.xg-val { font-size: 11.5px; font-weight: 700; color: var(--hi); min-width: 128px; text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.xg-val .xg-prevv { color: var(--lo); font-weight: 500; font-size: 12px; }
.xg-yoy { font-size: 12px; font-weight: 700; min-width: 48px; text-align: right; font-variant-numeric: tabular-nums; }

/* ── 시장 성장 스토리 (수출 x 활동) ── */
.gs-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 13px; }
@media (max-width: 760px) { .gs-grid { grid-template-columns: 1fr; } }
.gs-card { background: rgba(255,255,255,0.025); border: 1px solid var(--border); border-radius: 10px; padding: 13px 15px; }
.gs-head { display: flex; align-items: center; gap: 9px; }
.gs-flag { font-size: 20px; }
.gs-name { font-size: 15px; font-weight: 700; color: var(--hi); }
.gs-yoy { font-size: 19px; font-weight: 800; margin-left: auto; font-variant-numeric: tabular-nums; letter-spacing: -0.01em; }
.gs-exp { font-size: 12px; color: var(--mid); margin-top: 3px; font-variant-numeric: tabular-nums; }
.gs-delta { color: #4ab884; font-weight: 600; }
.gs-why { font-size: 12px; font-weight: 700; color: var(--lo); letter-spacing: 0.04em;
  text-transform: uppercase; margin: 11px 0 6px; padding-top: 9px; border-top: 1px solid var(--border); }
.gs-moves { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 6px; }
.gs-move { display: flex; align-items: baseline; gap: 7px; flex-wrap: wrap; font-size: 12px; line-height: 1.4; }
.gs-mv-brand { font-weight: 700; color: var(--hi); flex-shrink: 0; }
.gs-mv-act { font-size: 9.5px; font-weight: 700; padding: 1px 6px; border-radius: 2px; flex-shrink: 0;
  background: rgba(111,176,236,0.14); color: #6fb0ec; letter-spacing: 0.03em; }
.gs-move.gs-mv-hi .gs-mv-act { background: rgba(224,83,83,0.14); color: #e05353; }
.gs-mv-title { color: var(--mid); flex: 1; min-width: 160px; }
.gs-mv-title a { color: var(--mid); text-decoration: none; border-bottom: 1px dotted var(--border); }
.gs-mv-title a:hover { color: var(--hi); }
.gs-nomv { font-size: 11.5px; color: var(--lo); font-style: italic; }

/* ── 경쟁사 실적(DART) ── */
.fin-table td { vertical-align: middle; }
.fin-brand { font-weight: 700; color: var(--hi); white-space: nowrap; }
.fin-corp { color: var(--mid); white-space: nowrap; }
.fin-num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.fin-rev { font-weight: 700; color: var(--hi); }
.fin-yoy { font-weight: 700; }
.fin-tag { font-size: 12px; font-weight: 700; padding: 1px 5px; border-radius: 2px; margin-left: 6px; letter-spacing: 0.04em; }
.fin-listed { background: rgba(74,184,132,0.14); color: #4ab884; }
.fin-unlisted { background: rgba(78,88,112,0.28); color: var(--mid); }
.fin-whole { font-size: 12px; color: var(--lo); margin-left: 6px; padding: 1px 5px; border: 1px solid var(--border); border-radius: 2px; }
.fin-single { background: rgba(139,149,255,0.14); color: var(--champ2, #e8cfa0); }
/* ── 브랜드 신호 요약(직관형) ── */
.bsig { display:flex; flex-direction:column; }
.bsig-row { display:flex; align-items:center; gap:11px; padding:11px 8px; border-bottom:1px solid var(--border); cursor:pointer; }
.bsig-row:hover { background:var(--ink2); }
.bsig-rank { flex-shrink:0; width:20px; font-family:var(--mono); font-size:13px; color:var(--lo); text-align:right; }
.bsig-brand { flex-shrink:0; width:150px; font-size:14.5px; font-weight:700; color:var(--hi); }
.bsig-tier { font-size:10px; font-weight:700; color:var(--champ); background:rgba(139,149,255,.14); border-radius:5px; padding:1px 5px; margin-left:6px; }
.bsig-chips { display:flex; flex-wrap:wrap; gap:6px; flex:1; }
.bsig-chip { font-size:12.5px; font-weight:600; border-radius:6px; padding:3px 9px; white-space:nowrap; }
.bsig-chip.t-up { color:#7ff0d8; background:rgba(5,224,224,.13); }
.bsig-chip.t-down { color:#ffb0ba; background:rgba(255,107,122,.14); }
.bsig-chip.t-flat { color:var(--mid); background:rgba(255,255,255,.05); }
.bsig-note { font-size:12px; color:var(--lo); padding:12px 8px 2px; line-height:1.6; }
.bsig-empty { padding:16px; color:var(--lo); }
/* ── 검색 탭 (MCP 챗봇) ── */
.search-examples { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:14px; }
.se-chip { font-size:12.5px; color:var(--mid); background:var(--deep); border:1px solid var(--border);
  border-radius:999px; padding:6px 13px; cursor:pointer; font-family:inherit; transition:all .15s; }
.se-chip:hover { color:var(--hi); border-color:var(--champ); background:var(--elevated); }
.search-box { display:flex; gap:8px; margin-bottom:18px; }
.search-input { flex:1; background:var(--deep); border:1px solid var(--border); border-radius:var(--radius-sm);
  padding:12px 15px; color:var(--hi); font-size:14px; font-family:inherit; outline:none; }
.search-input:focus { border-color:var(--champ); }
.search-send { background:var(--cobalt); color:#fff; border:none; border-radius:var(--radius-sm);
  padding:0 22px; font-size:14px; font-weight:700; cursor:pointer; font-family:inherit; }
.search-send:hover { filter:brightness(1.12); }
.search-chat { display:flex; flex-direction:column; gap:16px; }
.sc-pair { border-bottom:1px solid var(--border); padding-bottom:16px; }
.sc-q { font-size:14.5px; font-weight:700; color:var(--champ2); margin-bottom:8px; }
.sc-q::before { content:"Q  "; color:var(--champ); font-family:var(--mono); }
.sc-a { font-size:14px; color:var(--hi); line-height:1.65; white-space:normal; }
.fin-sub { font-size: 11.5px; color: var(--lo); font-weight: 400; }
.fin-rev { font-variant-numeric: tabular-nums; }
.fin-note { font-size: 12px; color: var(--lo); line-height: 1.5; margin-top: 8px; }

/* ── 글로벌 검색 급등(구글) ── */
.sp-list { display: flex; flex-direction: column; gap: 6px; }
.sp-row { display: flex; align-items: center; gap: 10px; padding: 7px 10px;
  background: var(--elevated); border: 1px solid var(--border); border-radius: 3px; }
.sp-brand { font-size: 12px; font-weight: 700; color: var(--hi); min-width: 120px; white-space: nowrap; }
.sp-geo { font-size: 11.5px; color: var(--mid); min-width: 74px; white-space: nowrap; }
.sp-x { font-size: 12px; font-weight: 800; color: #e0533a; white-space: nowrap; font-variant-numeric: tabular-nums; }
.sp-idx { font-size: 12px; color: var(--lo); margin-left: auto; white-space: nowrap; font-variant-numeric: tabular-nums; }

/* ── 해외 상표 출원 선행신호 ── */
.tm-chips { display: flex; flex-wrap: wrap; gap: 7px; margin-bottom: 11px; }
.tm-chip { display: inline-flex; align-items: center; gap: 5px; font-size: 11.5px;
  background: rgba(200,169,110,0.10); border: 1px solid rgba(200,169,110,0.28);
  color: var(--hi); border-radius: 20px; padding: 4px 11px; }
.tm-chip-flag { font-size: 13px; }
.tm-chip-n { color: var(--gold); font-weight: 700; font-variant-numeric: tabular-nums; }
.tm-list { display: flex; flex-direction: column; gap: 5px; }
.tm-row { display: flex; align-items: baseline; gap: 10px; padding: 6px 10px;
  background: var(--elevated); border: 1px solid var(--border); border-radius: 3px; font-size: 12px; }
.tm-date { color: var(--lo); font-variant-numeric: tabular-nums; white-space: nowrap; flex-shrink: 0; font-size: 12.5px; }
.tm-flag { flex-shrink: 0; }
.tm-brand { font-weight: 700; color: var(--hi); min-width: 120px; white-space: nowrap; flex-shrink: 0; }
.tm-mark { color: var(--mid); letter-spacing: 0.02em; }

/* ── 개요 성장 헤드라인 배너 ── */
.gh-band { display: flex; gap: 20px; flex-wrap: wrap; align-items: stretch;
  background: linear-gradient(120deg, rgba(74,184,132,0.07), rgba(111,176,236,0.05));
  border: 1px solid var(--border); border-radius: 12px; padding: 16px 20px; margin-bottom: 18px; }
.gh-left { display: flex; flex-direction: column; justify-content: center; min-width: 190px;
  padding-right: 20px; border-right: 1px solid var(--border); }
.gh-label { font-size: 12.5px; color: var(--lo); letter-spacing: 0.02em; }
.gh-big { font-size: 38px; font-weight: 800; line-height: 1.05; margin: 4px 0; font-variant-numeric: tabular-nums; letter-spacing: -0.02em; }
.gh-sub { font-size: 11.5px; color: var(--mid); font-variant-numeric: tabular-nums; }
.gh-right { flex: 1; min-width: 240px; display: flex; flex-direction: column; gap: 8px; }
.gh-right-lbl { font-size: 12.5px; font-weight: 700; color: var(--mid); letter-spacing: 0.02em; }
.gh-chips { display: flex; flex-wrap: wrap; gap: 7px; }
.gh-chip { display: inline-flex; align-items: center; gap: 6px; cursor: pointer;
  background: var(--elevated); border: 1px solid var(--border); border-radius: 20px;
  padding: 5px 12px; font-size: 12px; color: var(--hi); transition: border-color 0.15s, background 0.15s; }
.gh-chip:hover { border-color: rgba(74,184,132,0.5); background: rgba(74,184,132,0.08); }
.gh-chip-flag { font-size: 14px; }
.gh-chip-name { font-weight: 700; }
.gh-chip-yoy { font-weight: 800; font-variant-numeric: tabular-nums; }
.gh-chip-ctx { font-size: 12px; color: var(--lo); }
@media (max-width: 640px) { .gh-left { border-right: none; padding-right: 0; } .gh-big { font-size: 30px; } }

/* ── 카테고리 대결 뷰 ── */
.catb-list { display: flex; flex-direction: column; gap: 12px; }
.catb-row { display: grid; grid-template-columns: 96px 1fr; gap: 12px 14px; align-items: center; }
.catb-name { font-size: 13.5px; font-weight: 700; color: var(--hi); text-align: right; }
.catb-bar-wrap { display: flex; align-items: center; gap: 10px; }
.catb-bar {
  height: 22px; border-radius: 4px; min-width: 3px;
  background: linear-gradient(90deg, rgba(111,176,236,0.35), rgba(111,176,236,0.6));
  position: relative; display: flex; align-items: center;
}
.catb-bar-hi { position: absolute; left: 0; top: 0; height: 100%; border-radius: 4px 0 0 4px;
  background: linear-gradient(90deg, rgba(239,83,83,0.55), rgba(239,83,83,0.75)); }
.catb-cnt { font-size: 12.5px; color: var(--mid); white-space: nowrap; font-variant-numeric: tabular-nums; }
.catb-hi-cnt { color: #d83a33; font-weight: 700; margin-left: 2px; }
.catb-top { grid-column: 2; font-size: 11.5px; color: var(--lo); margin-top: -4px; }
.catb-top b { color: var(--mid); }
@media (max-width: 640px) { .catb-row { grid-template-columns: 70px 1fr; } .catb-name { font-size: 12px; } }

/* ── 해외 진출 플레이북 ── */
.pb-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
@media (max-width: 900px) { .pb-grid { grid-template-columns: repeat(2, 1fr); } }
@media (max-width: 600px) { .pb-grid { grid-template-columns: 1fr; } }
.pb-card {
  background: rgba(255,255,255,0.025); border: 1px solid var(--border);
  border-radius: 10px; padding: 13px 14px; display: flex; flex-direction: column; gap: 9px;
}
.pb-head { display: flex; align-items: center; gap: 7px; flex-wrap: wrap; }
.pb-flag { font-size: 17px; }
.pb-name { font-size: 14px; font-weight: 800; color: var(--hi); }
.pb-stat { margin-left: auto; font-size: 12.5px; color: var(--lo); white-space: nowrap; font-variant-numeric: tabular-nums; }
.pb-chips { display: flex; flex-wrap: wrap; gap: 5px; align-items: center; }
.pb-chips-lbl { font-size: 12px; color: var(--lo); margin-right: 2px; }
.pb-chip {
  font-size: 12.5px; font-weight: 700; color: #7fd3b5;
  background: rgba(74,184,132,0.12); border: 1px solid rgba(74,184,132,0.3);
  border-radius: 999px; padding: 2px 8px;
}
.pb-chips-empty { font-size: 12.5px; color: var(--lo); font-style: italic; }
.pb-moves { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: 7px; }
.pb-moves li { font-size: 12px; line-height: 1.5; color: var(--mid); border-top: 1px solid var(--border); padding-top: 6px; }
.pb-moves li:first-child { border-top: 0; padding-top: 0; }
.pb-mv-b { font-weight: 800; color: var(--hi); }
.pb-mv-act { font-size: 12px; color: #9b7fe8; font-weight: 700; }
.pb-mv-ch { color: #7fd3b5; font-weight: 700; }
.pb-moves a { color: var(--mid); text-decoration: none; }
.pb-moves a:hover { color: var(--accent); text-decoration: underline; }

/* ── 브리핑 아카이브 ── */
.bfa-list { display: flex; flex-direction: column; gap: 8px; }
.bfa-item { background: rgba(255,255,255,0.025); border: 1px solid var(--border); border-radius: 9px; overflow: hidden; }
.bfa-sum { list-style: none; cursor: pointer; padding: 11px 14px; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
.bfa-sum::-webkit-details-marker { display: none; }
.bfa-sum::before { content: '▸'; color: var(--lo); font-size: 12px; transition: transform .15s; }
.bfa-item[open] .bfa-sum::before { transform: rotate(90deg); }
.bfa-item[open] .bfa-sum { border-bottom: 1px solid var(--border); }
.bfa-badge { font-size: 12.5px; font-weight: 800; padding: 2px 9px; border-radius: 999px; }
.bfa-weekly { color: #2f6fd0; background: rgba(74,143,212,0.14); border: 1px solid rgba(74,143,212,0.35); }
.bfa-daily { color: #b5730f; background: rgba(224,137,74,0.16); border: 1px solid rgba(224,137,74,0.38); }
.bfa-date { font-size: 13px; font-weight: 700; color: var(--hi); font-variant-numeric: tabular-nums; }
.bfa-period { font-size: 11.5px; color: var(--lo); }
.bfa-stat { font-size: 11.5px; color: var(--mid); margin-left: auto; white-space: nowrap; }
.bfa-body { padding: 6px 16px 16px; }
.bfa-h { margin: 14px 0 6px; font-size: 13.5px; color: var(--accent); font-weight: 800; }
.bfa-h:first-child { margin-top: 4px; }
.bfa-ul { margin: 0 0 4px; padding-left: 4px; list-style: none; }
.bfa-ul li { position: relative; padding-left: 14px; margin: 4px 0; font-size: 13px; line-height: 1.6; color: var(--hi); }
.bfa-ul li::before { content: '·'; position: absolute; left: 3px; color: var(--accent); font-weight: 700; }
.bfa-p { margin: 3px 0; font-size: 12.5px; color: var(--mid); line-height: 1.6; padding-left: 8px; }
.bfa-body strong { color: var(--hi); font-weight: 800; }

/* ── 시장 종합 인사이트 (대응/기회/점검 3버킷) ── */
.market-body { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
@media (max-width: 800px) { .market-body { grid-template-columns: 1fr; } }
.market-empty { color: var(--lo); font-size: 13px; padding: 8px; grid-column: 1 / -1; }
.market-sec {
  background: var(--bg); border: 1px solid var(--border); border-radius: 4px;
  padding: 14px 16px; border-left: 3px solid var(--border);
}
/* 대응해야 할 것: 최우선 → full-width + 적색 긴급 */
.market-sec.respond {
  grid-column: 1 / -1;
  background: linear-gradient(180deg, rgba(239,83,83,0.10), rgba(239,83,83,0.03));
  border: 1px solid rgba(239,83,83,0.28); border-left: 3px solid var(--high);
}
.market-sec.opportunity { border-left-color: #4ab884; }
.market-sec.check { border-left-color: var(--gold); }
.market-sec-h {
  font-size: 12.5px; font-weight: 800; letter-spacing: 0.06em; margin-bottom: 8px;
}
.market-sec-h.respond { color: #d83a33; }
.market-sec-h.opportunity { color: #1f9d6a; }
.market-sec-h.check { color: var(--gold); }
.market-sec-b { font-size: 13.5px; line-height: 1.72; color: var(--hi); }
.market-list { margin: 0; padding-left: 4px; list-style: none; }
.market-list li {
  font-size: 13.5px; line-height: 1.64; color: var(--hi);
  padding: 5px 0 5px 18px; position: relative;
}
.market-sec.respond .market-list li::before { content: '▸'; color: var(--high); }
.market-sec.opportunity .market-list li::before { content: '▸'; color: #4ab884; }
.market-sec.check .market-list li::before { content: '▸'; color: var(--gold); }
.market-list li::before { position: absolute; left: 2px; font-weight: 700; }

/* ── Insight Cards ── */
.insight-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }
@media (max-width: 1100px) { .insight-grid { grid-template-columns: 1fr 1fr; } }
@media (max-width: 700px)  { .insight-grid { grid-template-columns: 1fr; } }
.insight-card {
  border: 1px solid var(--border);
  border-radius: 3px;
  padding: 14px;
  background: var(--elevated);
  transition: border-color 0.15s;
  position: relative;
  overflow: hidden;
}
.insight-card::before {
  content: '';
  position: absolute; top: 0; left: 0;
  width: 2px; height: 100%;
  background: var(--gold);
  opacity: 0.45;
  transition: opacity 0.15s;
}
.insight-card:hover { border-color: var(--bhi); }
.insight-card:hover::before { opacity: 1; }
.insight-hdr { display: flex; align-items: center; gap: 6px; margin-bottom: 10px; flex-wrap: wrap; }
.insight-brand { font-size: 13px; font-weight: 700; color: var(--hi); }
.insight-badge {
  font-size: 12px; font-weight: 700; padding: 2px 7px;
  border-radius: 2px; white-space: nowrap; letter-spacing: 0.06em;
}
.insight-badge-act          { color: var(--bg); }
.insight-badge-high-hot     { background: rgba(224,83,83,0.18); color: #e05353; }
.insight-badge-high-warm    { background: rgba(212,148,58,0.18); color: #d4943a; }
.insight-badge-high-low     { background: rgba(62,70,92,0.5); color: var(--mid); }
.insight-strategy {
  font-size: 12.5px; color: var(--hi); line-height: 1.6; margin-bottom: 10px;
  padding: 10px 12px; background: var(--bg); border-radius: 2px;
  border-left: 2px solid rgba(74,143,212,0.35);
}
.insight-strat-sec { margin-bottom: 8px; }
.insight-strat-sec:last-child { margin-bottom: 0; }
.insight-strat-h {
  font-size: 12px; font-weight: 800; letter-spacing: 0.04em;
  color: var(--blue); margin-bottom: 3px;
}
.insight-strat-h.watch { color: var(--gold); }
.insight-strat-body { font-size: 12.5px; color: var(--hi); line-height: 1.6; }
/* v2: 요약 클램프 + 더보기 */
.insight-strategy.clamp { max-height: 92px; overflow: hidden; position: relative; }
.insight-strategy.clamp::after { content:""; position:absolute; left:0; right:0; bottom:0; height:36px;
  background:linear-gradient(transparent, var(--ink)); pointer-events:none; }
.insight-strategy.expanded { max-height: none; }
.insight-strategy.expanded::after { display:none; }
.insight-more { background:none; border:none; color:var(--champ); font-size:12.5px; font-family:var(--mono);
  cursor:pointer; padding:2px 0 8px; letter-spacing:.04em; }
.insight-more:hover { color:var(--champ2); }
.insight-src { font-size:12.5px; color:var(--lo); cursor:pointer; padding-top:8px; border-top:1px solid var(--border); margin-top:8px; }
.insight-src:hover { color:var(--champ); }
.insight-markets { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
.insight-market-item { font-size: 12.5px; color: var(--mid); display: flex; align-items: center; gap: 3px; }
.insight-market-item.mkt-click {
  cursor: pointer; padding: 1px 6px; border-radius: 3px;
  border: 1px solid transparent; transition: border-color 0.15s, background 0.15s;
}
.insight-market-item.mkt-click:hover {
  border-color: rgba(200,169,110,0.4); background: rgba(200,169,110,0.08);
}
.insight-market-cnt { font-weight: 700; color: var(--hi); font-variant-numeric: tabular-nums; }
.insight-articles-hdr {
  font-size: 12px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.1em; color: var(--lo); margin-bottom: 6px;
  padding-top: 8px; border-top: 1px solid var(--border);
}
.insight-art-row {
  display: flex; align-items: flex-start; gap: 6px;
  padding: 4px 0; border-top: 1px solid var(--border); font-size: 12.5px;
}
.insight-art-imp { width: 5px; height: 5px; border-radius: 50%; flex-shrink: 0; margin-top: 4px; }
.insight-art-title { flex: 1; color: var(--hi); line-height: 1.4; }
.insight-art-meta  { color: var(--lo); white-space: nowrap; font-size: 11.5px; font-variant-numeric: tabular-nums; }
.insight-art-link  { color: var(--blue); font-size: 11.5px; white-space: nowrap; }
.insight-art-link:hover { color: var(--gold); }

/* ══════════ COMMAND CENTER (개편) ══════════ */
.mast { display:flex; align-items:center; gap:16px; padding:18px 4px 12px; }
.mast .mk { display:flex; align-items:baseline; gap:11px; }
.mast .sq { width:9px; height:9px; background:var(--champ); transform:rotate(45deg); box-shadow:0 0 12px rgba(139,149,255,.6); }
.mast h1 { font-size:16px; margin:0; font-weight:700; letter-spacing:.32em; color:var(--hi); }
.mast h1 span { color:var(--champ); }
.mast .live { font-family:var(--mono); font-size:12px; color:var(--teal); letter-spacing:.12em; display:flex; align-items:center; gap:7px; }
.mast .live .bl { width:6px; height:6px; border-radius:50%; background:var(--teal); box-shadow:0 0 8px var(--teal); animation:cc-bl 1.8s infinite; }
@keyframes cc-bl { 0%,100%{opacity:1} 50%{opacity:.25} }
.mast .sp { flex:1; }
.mast .clk { font-family:var(--mono); font-size:12.5px; color:var(--lo); letter-spacing:.06em; }

/* segmented tabs (골드 언더라인) */
.tabbar-tabs { display:flex; gap:2px; }
.tab-btn { position:relative; font-family:var(--mono); font-size:11.5px; letter-spacing:.13em; background:none;
  border:none; color:var(--lo); padding:11px 16px; cursor:pointer; text-transform:uppercase; }
.tab-btn:hover { color:var(--mid); }
.tab-btn.active { color:var(--hi); }
.tab-btn.active::after { content:""; position:absolute; left:14px; right:14px; bottom:-1px; height:2px;
  background:var(--champ); box-shadow:0 0 10px rgba(139,149,255,.5); }

/* eyebrow section label */
.eyebrow { display:flex; align-items:center; gap:12px; margin:24px 0 13px; }
.eyebrow .lab { font-family:var(--mono); font-size:12px; letter-spacing:.22em; color:var(--champ-d); text-transform:uppercase; white-space:nowrap; }
.eyebrow .rule { flex:1; height:1px; background:var(--bhi); }
.eyebrow .rt { font-family:var(--mono); font-size:11.5px; color:var(--lo); letter-spacing:.06em; }
.eyebrow .rt.jump { color:var(--champ); cursor:pointer; }
.box { background:var(--ink); border:1px solid var(--border); border-radius:6px; overflow:hidden; }
.hud { position:relative; }
.hud::before, .hud::after { content:""; position:absolute; width:13px; height:13px; pointer-events:none; opacity:.55; z-index:3; }
.hud::before { left:9px; top:34px; border-left:1px solid var(--champ); border-top:1px solid var(--champ); }
.hud::after { right:9px; bottom:9px; border-right:1px solid var(--champ); border-bottom:1px solid var(--champ); }
.box .ph { display:flex; align-items:center; gap:8px; padding:11px 14px; border-bottom:1px solid var(--border);
  font-family:var(--mono); font-size:11.5px; letter-spacing:.15em; color:var(--mid); text-transform:uppercase; }
.box .ph .c { margin-left:auto; color:var(--lo); letter-spacing:.05em; }

/* synthesis */
.synth { display:grid; grid-template-columns:1.5fr 1fr; border:1px solid var(--border); border-radius:6px; overflow:hidden;
  background:linear-gradient(120deg, rgba(139,149,255,.05), transparent 55%); }
.synth .lead { padding:22px 26px; border-right:1px solid var(--border); }
.synth .lead .tl { font-family:var(--mono); font-size:11.5px; letter-spacing:.2em; color:var(--champ); text-transform:uppercase; margin-bottom:12px; }
.synth .lead h2 { font-size:22px; line-height:1.4; margin:0; font-weight:650; letter-spacing:-.01em;
  border-left:2px solid var(--champ); padding-left:16px; color:var(--hi); }
.synth .lead h2 b { color:var(--champ2); }
.synth .lead .synth-body { font-size:13px; line-height:1.7; color:var(--mid); margin:14px 0 0; max-width:64ch; }
.synth .lead .synth-body b { color:var(--hi); font-weight:600; }
.synth .lead .synth-facts { display:flex; gap:10px; flex-wrap:wrap; margin-top:14px; }
.synth .lead .sf { font-family:var(--mono); font-size:12.5px; color:var(--hi); background:rgba(139,149,255,.08);
  border:1px solid var(--border); border-radius:5px; padding:5px 11px; display:inline-flex; gap:7px; align-items:baseline; }
.synth .lead .sf i { font-style:normal; font-size:12px; letter-spacing:.1em; color:var(--champ-d); text-transform:uppercase; }
.synth .lead .by { font-family:var(--mono); font-size:11.5px; color:var(--lo); margin-top:16px; letter-spacing:.05em; }
.synth .cols { display:flex; flex-direction:column; }
.synth .scol { padding:12px 20px; border-bottom:1px solid var(--border); }
.synth .scol:last-child { border-bottom:none; }
.synth .scol .h { font-family:var(--mono); font-size:11.5px; letter-spacing:.13em; text-transform:uppercase; margin-bottom:5px; display:flex; gap:7px; align-items:center; }
.synth .scol .h::before { content:""; width:5px; height:5px; border-radius:50%; }
.scol.respond .h { color:var(--coral); } .scol.respond .h::before { background:var(--coral); }
.scol.opportunity .h { color:var(--champ); } .scol.opportunity .h::before { background:var(--champ); }
.scol.check .h { color:var(--teal); } .scol.check .h::before { background:var(--teal); }
.synth .scol ul { margin:0; padding-left:15px; }
.synth .scol li { font-size:12px; color:var(--mid); line-height:1.5; margin-bottom:2px; }

/* metric rail */
.rail { display:grid; grid-template-columns:repeat(6,1fr); border:1px solid var(--border); border-radius:6px; overflow:hidden; margin-top:14px; }
.met { padding:13px 16px; border-right:1px solid var(--border); position:relative; transition:background 0.18s ease; }
.met:last-child { border-right:none; }
.met:hover { background:rgba(139,149,255,.07); }
.met .l { font-family:var(--mono); font-size:9.5px; letter-spacing:.12em; color:var(--lo); text-transform:uppercase; }
.met .v { font-family:var(--mono); font-size:25px; font-weight:700; margin-top:5px; letter-spacing:-.02em; color:var(--hi); }
.met .d { font-family:var(--mono); font-size:12px; margin-top:2px; display:flex; align-items:center; gap:5px; }
.met .d.pos { color:var(--teal); } .met .d.neg { color:var(--coral); } .met .d.neu { color:var(--lo); }
.met .d .cap { color:var(--lo); }
.met .spark { display:block; width:100%; height:20px; margin-top:6px; overflow:visible; }
.met .spark path { fill:none; stroke:var(--champ); stroke-width:1.4; vector-effect:non-scaling-stroke; }
.met .spark .fill { fill:rgba(139,149,255,.10); stroke:none; }
.met .spark .end { fill:var(--champ); }

/* command grid */
.cmd { display:grid; grid-template-columns:250px minmax(0,1fr) 232px; gap:14px; align-items:start; }
.cmd > .box, .cmd > div { min-width:0; }
.stream { max-height:520px; overflow:auto; }
.ev { padding:11px 14px; border-bottom:1px solid var(--border); cursor:pointer; }
.ev:hover { background:var(--ink2); }
.ev .m { display:flex; align-items:center; gap:6px; font-family:var(--mono); font-size:9.5px; color:var(--lo); letter-spacing:.04em; margin-bottom:5px; text-transform:uppercase; }
.ev .t { font-size:12.5px; line-height:1.4; color:var(--hi); }
.ev .vb { font-family:var(--mono); font-size:8.5px; font-weight:700; padding:2px 6px; border-radius:2px; letter-spacing:.04em; margin-left:auto; white-space:nowrap; }
.vb-real { background:rgba(70,214,195,.15); color:var(--teal); }
.vb-pr { background:rgba(255,106,86,.15); color:var(--coral); }
.vb-latent { background:rgba(139,149,255,.16); color:var(--champ); }
.dot-h { color:var(--coral); } .dot-m { color:var(--amber); } .dot-l { color:var(--teal); }

/* composite leaderboard */
.lb { max-height:520px; overflow:auto; }
.lb .r { display:flex; align-items:center; gap:9px; padding:9px 12px; border-bottom:1px solid var(--border); cursor:pointer; }
.lb .r:hover { background:var(--ink2); }
.lb .rk { font-family:var(--mono); font-size:11.5px; color:var(--lo); width:14px; }
.lb .nm { font-size:12px; flex:1; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; color:var(--hi); }
.lb .subs { display:flex; gap:2px; height:20px; align-items:flex-end; width:38px; flex-shrink:0; }
.lb .subs i { flex:1; border-radius:1px; min-height:1px; }
.sub-mom { background:var(--violet); } .sub-fin { background:var(--teal); } .sub-tm { background:var(--champ); } .sub-dem { background:var(--amber); }
.lb .sc { font-family:var(--mono); font-size:15px; font-weight:700; width:26px; text-align:right; color:var(--hi); }
.lb .lb-tr { font-family:var(--mono); font-size:11.5px; font-weight:700; margin-left:5px; width:26px; text-align:left; }
.lb-key { display:flex; gap:13px; flex-wrap:wrap; padding:10px 12px; font-family:var(--mono); font-size:11.5px; color:var(--lo); letter-spacing:.03em; border-top:1px solid var(--border); }
.lb-key span { display:inline-flex; align-items:center; cursor:default; }
.lb-key i { display:inline-block; width:9px; height:9px; border-radius:2px; margin-right:5px; vertical-align:middle; }

/* market + heatmap duo */
.duo { display:grid; grid-template-columns:minmax(0,1fr) minmax(0,1.4fr); gap:14px; align-items:start; }
.duo .box > div:not(.ph) { padding:6px 8px; }
.mkl .mk-row { display:flex; align-items:center; gap:11px; padding:10px 12px; border-bottom:1px solid var(--border); cursor:pointer; }
.mkl .mk-row:hover { background:var(--ink2); }
.mkl .flag { font-size:16px; } .mkl .nm { font-weight:600; font-size:12.5px; color:var(--hi); }
.mkl .why { color:var(--lo); font-size:12px; } .mkl .yoy { margin-left:auto; font-family:var(--mono); font-weight:700; color:var(--teal); }
.cmd-2 { grid-template-columns: 1fr 300px; }   /* 지도 제거 → 무브 | 스코어 2열 */
@media (max-width:1080px){ .cmd,.cmd-2,.duo,.synth,.rail{ grid-template-columns:1fr; } .rail{ grid-template-columns:repeat(3,1fr);} }

/* ── 기회 스토리 카드 (핵심 서사: 나라·브랜드·무브·제품·성과 → 우리가 할 것) ── */
.ostory-grid { display:grid; grid-template-columns:repeat(auto-fill, minmax(340px,1fr)); gap:12px; margin-bottom:16px; }
.ostory { background:var(--surface); border:1px solid var(--border); border-left:3px solid var(--champ);
  border-radius:var(--radius); padding:13px 15px; cursor:pointer; transition:transform .15s ease, box-shadow .15s ease, border-color .15s ease; }
.ostory:hover { transform:translateY(-2px); box-shadow:0 6px 20px rgba(0,0,0,.4); border-left-color:var(--champ2); }
.os-head { display:flex; align-items:center; gap:8px; margin-bottom:9px; }
.os-loc { font-size:12.5px; color:var(--mid); }
.os-brand { font-size:15px; font-weight:700; color:var(--hi); }
.os-neg { font-family:var(--mono); font-size:11.5px; font-weight:700; color:var(--coral);
  background:rgba(255,106,86,.12); border-radius:3px; padding:1px 6px; }
.os-score { margin-left:auto; font-family:var(--mono); font-size:16px; font-weight:800; color:var(--champ); }
.os-loc b { color:var(--hi); font-size:15px; font-weight:700; }
/* BLUF 판독(결론) */
.os-verdict { font-size:12.5px; font-weight:800; padding:6px 10px; border-radius:6px;
  border-left:3px solid; background:rgba(255,255,255,.03); margin-bottom:11px; cursor:help; }
.os-read-hot { color:var(--coral); border-left-color:var(--coral); background:rgba(255,106,86,.10); }
.os-read-opp { color:var(--champ2); border-left-color:var(--champ); background:rgba(139,149,255,.10); }
.os-read-stealth { color:var(--teal); border-left-color:var(--teal); background:rgba(70,214,195,.10); }
.os-read-grow { color:var(--mid); border-left-color:var(--border); background:rgba(255,255,255,.03); }
/* 섹션(제품·왜·포지션) */
.os-sec { margin-bottom:10px; }
.os-k { font-family:var(--mono); font-size:9.5px; letter-spacing:.07em; color:var(--lo);
  text-transform:uppercase; margin-bottom:4px; }
.os-prod { font-size:14.5px; font-weight:700; color:var(--hi); line-height:1.35; }
.os-rank { font-family:var(--mono); font-size:12px; color:var(--coral); font-weight:700; margin-top:3px; }
.os-rank b { color:var(--coral); font-size:13.5px; }
.os-why { display:flex; flex-wrap:wrap; gap:5px; }
.os-wchip { font-size:11.5px; font-weight:600; color:var(--champ2); background:rgba(139,149,255,.12);
  border:1px solid rgba(139,149,255,.25); border-radius:5px; padding:2px 7px; }
.os-ing { display:inline-block; font-family:var(--mono); font-size:11.5px; color:var(--mid);
  background:rgba(255,255,255,.04); border:1px solid var(--border); border-radius:4px; padding:1px 6px; }
.os-pos { font-family:var(--mono); font-size:11.5px; color:var(--mid); margin-bottom:3px; line-height:1.5; }
.os-tag { font-size:9.5px; font-weight:800; padding:1px 6px; border-radius:4px; margin-right:6px; }
.os-tag.our { color:var(--teal); background:rgba(70,214,195,.14); }
.os-tag.exp { color:var(--champ); background:rgba(139,149,255,.14); }
.os-move { font-size:11.5px; color:var(--lo); line-height:1.4; margin-bottom:2px; }
.os-move .os-act { color:var(--mid); font-weight:700; }
.os-src { color:var(--champ); font-size:12px; white-space:nowrap; text-decoration:none; }
.os-src:hover { text-decoration:underline; }
.os-dim { color:var(--lo); font-style:italic; font-weight:400; }
.os-action { display:flex; gap:8px; align-items:baseline; margin-top:11px; padding-top:10px;
  border-top:1px dashed var(--border); }
.os-ac-k { flex-shrink:0; font-size:12.5px; font-weight:800; color:var(--champ); white-space:nowrap; }
.os-ac-v { flex:1; font-size:13.5px; font-weight:700; color:var(--hi); }

/* ── v2: 액션 배너 (최우선 정보, 크게) ── */
.action-banner { display:flex; gap:16px; align-items:flex-start;
  background:linear-gradient(100deg, rgba(255,106,86,.055), rgba(255,106,86,.01) 60%);
  border:1px solid rgba(255,106,86,.15); border-left:4px solid var(--coral);
  border-radius:var(--radius); padding:16px 20px; margin-bottom:14px; }
.action-banner .ab-label { font-family:var(--mono); font-size:12px; font-weight:800; letter-spacing:.06em;
  color:var(--coral); white-space:nowrap; padding-top:2px; min-width:150px; text-transform:uppercase; }
.action-banner .ab-list { margin:0; padding:0; list-style:none; flex:1; display:flex; flex-direction:column; gap:10px; }
.action-banner .ab-list li { font-size:14.5px; font-weight:600; line-height:1.5; color:var(--hi); position:relative; padding-left:16px; }
.action-banner .ab-list li::before { content:"›"; position:absolute; left:0; color:var(--coral); font-weight:800; }
.action-banner .ab-list li .insight-badge { margin-left:7px; vertical-align:middle; }
@media (max-width:760px){ .action-banner{ flex-direction:column; gap:8px; } }

/* ── v2: 통합 범례 ── */
.legend { border:1px solid var(--border); border-radius:var(--radius); background:var(--ink); margin-bottom:16px; }
.legend > summary { cursor:pointer; list-style:none; padding:9px 14px; font-family:var(--mono); font-size:12.5px;
  letter-spacing:.08em; color:var(--mid); text-transform:uppercase; }
.legend > summary::-webkit-details-marker { display:none; }
.legend .lg-hint { color:var(--lo); }
.legend .lg-body { display:flex; flex-wrap:wrap; gap:8px 22px; padding:4px 16px 14px; }
.lg-grp { display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
.lg-t { font-family:var(--mono); font-size:11.5px; letter-spacing:.08em; color:var(--champ-d); text-transform:uppercase; }
.lg-item { display:inline-flex; align-items:center; gap:5px; font-size:11.5px; color:var(--mid); }
.lg-dot { width:9px; height:9px; border-radius:2px; display:inline-block; }

/* ── v2: 시각 위계 강화 (섹션타이틀·이벤트 헤드라인) ── */
.section-title { font-size: var(--fs-title); }
.ev .t { font-size: 13px; font-weight: 500; }
.synth .lead h2 { font-size: var(--fs-hero); }
"""

_WORLDMAP_CSS = """
/* ── World Map ── */
.wm-section { background: #090e1a; border-color: rgba(30,70,150,0.3); }
.wm-section .section-title { color: #a0aabb; border-bottom-color: rgba(30,70,150,0.3); }
.wm-section .section-sub   { color: #7a82a8; }
.worldmap-container {
  position: relative;
  overflow: hidden;
  border-radius: 4px;
  background: #050c18;
  line-height: 0;
  border: 1px solid rgba(30,80,160,0.25);
  box-shadow: 0 0 40px rgba(0,40,120,0.2), inset 0 0 60px rgba(0,0,0,0.4);
}
#worldmap-canvas {
  display: block;
  position: relative;
  z-index: 1;
  background: transparent;
}
.wm-tooltip {
  position: absolute;
  background: rgba(4,10,22,0.97);
  color: #cbd5e1;
  padding: 7px 14px;
  border-radius: 5px;
  font-size: 12px;
  font-weight: 500;
  pointer-events: none;
  display: none;
  white-space: nowrap;
  border: 1px solid rgba(60,130,220,0.3);
  box-shadow: 0 4px 20px rgba(0,0,0,0.7), 0 0 10px rgba(60,130,220,0.1);
  z-index: 10;
  line-height: 1.8;
  font-family: monospace;
}
.wm-legend-overlay {
  position: absolute;
  top: 10px; right: 12px;
  display: flex; flex-direction: column; gap: 5px; z-index: 5;
  background: rgba(5,12,24,0.85);
  padding: 8px 12px;
  border-radius: 6px;
  border: 1px solid rgba(30,80,160,0.30);
  line-height: 1.5;
}
.wm-lo-row { display: flex; gap: 10px; }
.wm-lo-item {
  font-size: 11.5px; font-family: monospace; font-weight: 700;
  letter-spacing: 0.5px; opacity: 0.85;
}
.wm-lo-size {
  font-size: 12px; font-family: monospace; opacity: 0.7;
  color: #8890b8; letter-spacing: 0.3px;
}
.wm-lo-high { color: #f87171; }
.wm-lo-med  { color: #fbbf24; }
.wm-lo-low  { color: #22d3ee; }
.wm-lo-hint {
  margin-top: 5px; padding-top: 5px; border-top: 1px solid rgba(255,255,255,0.08);
  font-size: 12px; font-family: monospace; color: #b8c0e0; letter-spacing: 0.2px;
}
/* ── HIGH 기사 하단 알림 스트립 ── */
.wm-alert-strip { margin-top: 14px; }
.wm-alert-head {
  font-size: 11.5px; font-weight: 800; color: #ff8a8a;
  letter-spacing: 0.1em; text-transform: uppercase;
  margin-bottom: 10px; display: flex; align-items: center; gap: 6px;
}
.wm-alert-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 10px;
}
.wm-alert-card {
  position: relative;
  padding: 11px 13px 11px 16px;
  background: rgba(255,255,255,0.045);
  border: 1px solid rgba(120,150,220,0.16);
  border-left: 3px solid #ef5a5a;
  border-radius: 6px;
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s, transform 0.1s;
}
.wm-alert-card:hover {
  background: rgba(255,255,255,0.09);
  border-color: rgba(120,150,220,0.4);
  border-left-color: #ef5a5a;
  transform: translateY(-1px);
}
.wm-alert-badges { display: flex; align-items: center; gap: 6px; margin-bottom: 6px; flex-wrap: wrap; }
.wm-alert-brand {
  font-size: 12.5px; font-weight: 700; color: #aab1f2;
  background: rgba(212,184,126,0.14); border-radius: 3px; padding: 2px 8px;
  letter-spacing: 0.02em;
}
.wm-alert-cc {
  font-size: 12.5px; font-weight: 600; color: #c2ccdf;
  background: rgba(255,255,255,0.08); border-radius: 3px; padding: 2px 8px;
}
.wm-alert-title {
  font-size: 13.5px; color: #eaf0fb; line-height: 1.5; font-weight: 500;
  overflow: hidden; display: -webkit-box;
  -webkit-line-clamp: 2; -webkit-box-orient: vertical;
}
.wm-alert-meta { font-size: 12px; color: #8b95b0; margin-top: 6px; font-variant-numeric: tabular-nums; }
.wm-alert-empty { padding: 16px; color: #8b95b0; font-size: 12px; grid-column: 1 / -1; }
"""


def _render_worldmap_section(high_articles: list | None = None) -> str:
    alert_cards = []
    high_only = [a for a in (high_articles or []) if a.get("importance") == "high"][:8]
    for a in high_only:
        cc = a.get("country", "")
        flag = COUNTRY_FLAGS.get(cc, "🌐")
        date = _fmt_date(a.get("published_date", ""))[:10]
        brand = _esc(a.get("brand", ""))
        brand_js = brand.replace("'", "")
        title = _esc(a.get("title_ko") or a.get("title") or (a.get("details") or "")[:90])
        src = _esc(a.get("source_name") or "")
        meta = _esc(date) + (f" · {src}" if src else "")
        alert_cards.append(
            f'<div class="wm-alert-card" onclick="openHeatmapDrilldown(\'{brand_js}\',\'{_esc(cc)}\')" '
            f'title="{brand} · {_esc(cc)} 전략 요약 보기">'
            f'<div class="wm-alert-badges">'
            f'<span class="wm-alert-brand">{brand}</span>'
            f'<span class="wm-alert-cc">{flag} {_esc(cc)}</span>'
            f'</div>'
            f'<div class="wm-alert-title">{title}</div>'
            f'<div class="wm-alert-meta">{meta}</div>'
            f'</div>'
        )

    strip_inner = "".join(alert_cards) if alert_cards else (
        '<div class="wm-alert-empty">최근 HIGH 기사 없음</div>'
    )

    return (
        '<div class="section wm-section" id="worldmap-section">'
        '<div class="section-title">'
        '🌍 글로벌 신호 지도'
        '<span class="section-sub">마커 클릭 → 해당국 기사 상세</span>'
        '</div>'
        '<div class="worldmap-container">'
        '<canvas id="worldmap-canvas"></canvas>'
        '<div id="worldmap-tooltip" class="wm-tooltip"></div>'
        '<div class="wm-legend-overlay">'
        '<div class="wm-lo-row">'
        '<span class="wm-lo-item wm-lo-high">● HIGH</span>'
        '<span class="wm-lo-item wm-lo-med">● MED</span>'
        '<span class="wm-lo-item wm-lo-low">● LOW</span>'
        '</div>'
        '<div class="wm-lo-size">● 점 크기 = HIGH 기사 수</div>'
        '<div class="wm-lo-size">○ glow 크기 = 전체 기사 수</div>'
        '<div class="wm-lo-hint">🖱 휠 확대 · 드래그 이동 · 더블클릭 초기화</div>'
        '</div>'
        '</div>'
        '<div class="wm-alert-strip">'
        '<div class="wm-alert-head">⚡ 실시간 HIGH 신호</div>'
        f'<div class="wm-alert-grid">{strip_inner}</div>'
        '</div>'
        '</div>'
    )


def _build_worldmap_script(country_stats: dict) -> str:
    stats_json = json.dumps(country_stats or {}, ensure_ascii=False)
    land_json  = json.dumps(_NE_LAND_POLYS)
    return f"""
// ── World Map (Full Canvas — Intel Dashboard) ──
(function() {{
  var container = document.querySelector('.worldmap-container');
  var canvas    = document.getElementById('worldmap-canvas');
  if (!canvas || !container) return;

  var STATS = {stats_json};
  window._wmSetStats = function(ns) {{
    STATS = ns;
    rebuildActive();
    drawStaticLayer();
  }};

  var COORDS = {{
    US:[38,-97],  CA:[56,-96], GB:[54,-2],  DE:[51,10],  FR:[46,2],
    PL:[52,20],   JP:[36,138], KR:[37,128], CN:[35,105], TH:[15,101],
    SG:[1.3,104], MY:[4,109],  ID:[-5,120], VN:[14,108], AU:[-27,133],
    IT:[42,13],   PH:[13,122], IN:[22,79],  AE:[24,54],  SA:[24,45],
    BR:[-10,-52], MX:[23,-102], ZA:[-29,24],
    RU:[56,60],   KZ:[48,67],  UZ:[41,64],  BY:[53,28]
  }};
  var CNAMES = {{
    US:'미국', CA:'캐나다', GB:'영국', DE:'독일', FR:'프랑스',
    PL:'폴란드', JP:'일본', KR:'한국', CN:'중국', TH:'태국',
    SG:'싱가포르', MY:'말레이시아', ID:'인도네시아', VN:'베트남', AU:'호주',
    IT:'이탈리아', PH:'필리핀', IN:'인도', AE:'UAE', SA:'사우디',
    BR:'브라질', MX:'멕시코', ZA:'남아공',
    RU:'러시아', KZ:'카자흐스탄', UZ:'우즈베키스탄', BY:'벨라루스'
  }};
  var LAND = {land_json};

  var DPR  = window.devicePixelRatio || 1;
  var W = 0, H = 0, tick = 0, animId = null;
  var ctx  = canvas.getContext('2d');
  var off  = document.createElement('canvas').getContext('2d');
  var activeCC = [];

  // ── 뷰 변환(줌·팬) — 밀집 지역(유럽) 겹침을 마우스휠 확대로 해소 ──
  var VIEW = {{ s: 1, x: 0, y: 0 }};
  var MINS = 1, MAXS = 5.5;
  var EU = {{ GB:1, DE:1, FR:1, PL:1, IT:1 }};   // 겹치는 유럽권 → 축소 시 클러스터
  var CLUSTER_ZOOM = 1.6;                          // 이 배율 미만이면 유럽 마커를 하나로 묶음
  var clusterBox = null;                           // {{x,y,r,n}} 화면좌표 — 히트테스트용
  function clampView() {{
    if (VIEW.s < MINS) VIEW.s = MINS;
    if (VIEW.s > MAXS) VIEW.s = MAXS;
    // 팬 한계: 지도가 화면 밖으로 완전히 빠지지 않도록
    var minX = W - W * VIEW.s, minY = H - H * VIEW.s;
    if (VIEW.x > 0) VIEW.x = 0; if (VIEW.x < minX) VIEW.x = minX;
    if (VIEW.y > 0) VIEW.y = 0; if (VIEW.y < minY) VIEW.y = minY;
  }}
  function euCentroid() {{
    var xs = 0, ys = 0, n = 0, tot = 0, hi = 0;
    Object.keys(EU).forEach(function(cc) {{
      var st = STATS[cc]; if (!st || !st.total) return;
      var co = COORDS[cc]; xs += pX(co[1]); ys += pY(co[0]); n++;
      tot += st.total; hi += (st.high || 0);
    }});
    if (!n) return null;
    return {{ x: xs / n, y: ys / n, n: n, total: tot, high: hi }};
  }}

  // 크롭 + 상단 페이드로 북극권 가로 이음새를 어둠에 녹임. 국가는 전부 위도 56 이하.
  var LAT_TOP = 74, LAT_BOT = -56;
  function pX(lon) {{ return (lon + 180) / 360 * W; }}
  function pY(lat) {{ return (LAT_TOP - lat) / (LAT_TOP - LAT_BOT) * H; }}
  // 부드러운 곡선 대륙 path — 거친 정점을 곡선으로 라운딩(중점 경유 2차 베지어) → 매끄러운 해안
  function landPath(c2, poly) {{
    var n = poly.length; if (n < 3) return;
    var f = poly[0], l = poly[n - 1];
    c2.moveTo((pX(l[0]) + pX(f[0])) / 2, (pY(l[1]) + pY(f[1])) / 2);
    for (var i = 0; i < n; i++) {{
      var c = poly[i], x2 = poly[(i + 1) % n];
      c2.quadraticCurveTo(pX(c[0]), pY(c[1]),
                          (pX(c[0]) + pX(x2[0])) / 2, (pY(c[1]) + pY(x2[1])) / 2);
    }}
    c2.closePath();
  }}

  function rebuildActive() {{
    activeCC = Object.keys(COORDS).filter(function(cc) {{
      var s = STATS[cc]; return s && s.total > 0;
    }});
  }}

  /* ── Static layer (ocean + grid + land + country glows) ── */
  function drawStaticLayer() {{
    var oc = off.canvas;
    oc.width  = W * DPR;
    oc.height = H * DPR;
    off.setTransform(DPR, 0, 0, DPR, 0, 0);

    // Ocean (은은한 방사형 — 가운데가 살짝 밝은 딥네이비)
    var bg = off.createRadialGradient(W*0.5, H*0.42, H*0.1, W*0.5, H*0.5, W*0.65);
    bg.addColorStop(0, '#0c1526'); bg.addColorStop(1, '#060a14');
    off.fillStyle = bg; off.fillRect(0, 0, W, H);

    // 대륙 = 매끄러운 곡선 채우기 + 소프트 글로우 (부드럽고 이쁘게)
    var lg = off.createLinearGradient(0, 0, 0, H);
    lg.addColorStop(0, '#2a4661'); lg.addColorStop(1, '#1a2f47');
    off.save();
    off.shadowColor = 'rgba(70,125,200,0.45)';
    off.shadowBlur = 18;
    off.fillStyle = lg;
    LAND.forEach(function(poly) {{ off.beginPath(); landPath(off, poly); off.fill(); }});
    off.restore();
    // 부드러운 해안선 하이라이트
    off.strokeStyle = 'rgba(140,180,235,0.30)'; off.lineWidth = 0.9;
    LAND.forEach(function(poly) {{ off.beginPath(); landPath(off, poly); off.stroke(); }});

    // 상·하단 페이드 — 북극권 해안선 '가로 이음새'와 크롭 경계를 어둠으로 부드럽게 녹임
    var topf = off.createLinearGradient(0, 0, 0, H * 0.22);
    topf.addColorStop(0, '#060a14'); topf.addColorStop(1, 'rgba(6,10,20,0)');
    off.fillStyle = topf; off.fillRect(0, 0, W, H * 0.22);
    var botf = off.createLinearGradient(0, H * 0.80, 0, H);
    botf.addColorStop(0, 'rgba(6,10,20,0)'); botf.addColorStop(1, '#060a14');
    off.fillStyle = botf; off.fillRect(0, H * 0.80, W, H * 0.20);

    // Country signal glows (radial blobs on land)
    Object.keys(COORDS).forEach(function(cc) {{
      var st = STATS[cc]; if (!st || !st.total) return;
      var co = COORDS[cc], x = pX(co[1]), y = pY(co[0]);
      var r   = 55 + Math.min(st.total * 4, 70);
      var col = st.high > 0 ? 'rgba(248,113,113,' : st.medium > 0 ? 'rgba(251,191,36,' : 'rgba(34,211,238,';
      var al  = st.high > 0 ? 0.28  : st.medium > 0 ? 0.18 : 0.12;
      var grd = off.createRadialGradient(x, y, 0, x, y, r);
      grd.addColorStop(0, col + al + ')'); grd.addColorStop(1, col + '0)');
      off.beginPath(); off.arc(x, y, r, 0, Math.PI*2);
      off.fillStyle = grd; off.fill();
    }});
  }}

  /* ── Dynamic: signal arcs from KR ── */
  function drawArcs() {{
    var kr = STATS['KR']; if (!kr || !kr.total) return;
    var krx = pX(COORDS['KR'][1]), kry = pY(COORDS['KR'][0]);
    activeCC.forEach(function(cc) {{
      if (cc === 'KR') return;
      var st = STATS[cc], co = COORDS[cc];
      var tx = pX(co[1]), ty = pY(co[0]);
      var mx = (krx + tx) / 2, my = (kry + ty) / 2 - Math.abs(tx - krx) * 0.28;
      var g = ctx.createLinearGradient(krx, kry, tx, ty);
      g.addColorStop(0,   'rgba(99,179,237,0.55)');
      g.addColorStop(0.6, 'rgba(99,179,237,0.2)');
      g.addColorStop(1,   st.high > 0 ? 'rgba(248,113,113,0.4)' : 'rgba(251,191,36,0.3)');
      ctx.beginPath(); ctx.moveTo(krx, kry);
      ctx.quadraticCurveTo(mx, my, tx, ty);
      ctx.strokeStyle = g;
      ctx.lineWidth = st.high > 0 ? 1.1 : 0.7;
      ctx.setLineDash([4, 6]); ctx.stroke(); ctx.setLineDash([]);
    }});
  }}

  /* ── Dynamic: scan sweep ── */
  function drawScan() {{
    var y = ((tick * 0.35) % (H + 100)) - 50;
    var sg = ctx.createLinearGradient(0, y-50, 0, y+50);
    sg.addColorStop(0,   'rgba(80,180,255,0)');
    sg.addColorStop(0.5, 'rgba(80,180,255,0.045)');
    sg.addColorStop(1,   'rgba(80,180,255,0)');
    ctx.fillStyle = sg; ctx.fillRect(0, y-50, W, 100);
  }}

  /* ── Dynamic: markers ── */
  function drawMarkers() {{
    var labels = [];
    var clustered = VIEW.s < CLUSTER_ZOOM;
    clusterBox = null;
    Object.keys(COORDS).forEach(function(cc) {{
      if (clustered && EU[cc]) return;   // 축소 상태 → 유럽 개별마커 생략(클러스터로 대체)
      var co = COORDS[cc], st = STATS[cc] || {{total:0,high:0,medium:0}};
      var x = pX(co[1]), y = pY(co[0]);
      if (!st.total) {{
        ctx.beginPath(); ctx.arc(x, y, 2, 0, Math.PI*2);
        ctx.fillStyle = '#1d3550'; ctx.fill(); return;
      }}
      var isH = st.high > 0, isM = st.medium > 0;
      var col  = isH ? '#f87171' : isM ? '#fbbf24' : '#22d3ee';
      var glC  = isH ? 'rgba(248,113,113,' : isM ? 'rgba(251,191,36,' : 'rgba(34,211,238,';
      var base = isH ? 5 + Math.min(Math.sqrt(st.high)*1.6, 7) : 4;

      // 3 staggered pulse rings (반경 축소 → 밀집 지역 뭉침 방지)
      [0, 0.33, 0.66].forEach(function(off2) {{
        var ph = ((tick / 65) + off2) % 1;
        var pr = base + ph * 17, pa = (1 - ph) * (isH ? 0.6 : 0.4);
        ctx.beginPath(); ctx.arc(x, y, pr, 0, Math.PI*2);
        ctx.strokeStyle = glC + pa + ')';
        ctx.lineWidth = isH ? 1.5 : 1.0; ctx.stroke();
      }});

      // Outer glow halo (축소)
      var grd = ctx.createRadialGradient(x, y, 0, x, y, base*3.4);
      grd.addColorStop(0, glC+'0.5)'); grd.addColorStop(1, glC+'0)');
      ctx.beginPath(); ctx.arc(x, y, base*3.4, 0, Math.PI*2);
      ctx.fillStyle = grd; ctx.fill();

      // Core — bright highlight + color
      var cg = ctx.createRadialGradient(x-base*0.3, y-base*0.3, 0, x, y, base);
      cg.addColorStop(0, '#ffffff'); cg.addColorStop(0.45, col); cg.addColorStop(1, glC+'0.7)');
      ctx.beginPath(); ctx.arc(x, y, base, 0, Math.PI*2);
      ctx.fillStyle = cg; ctx.fill();

      labels.push({{cc: cc, x: x, y: y, base: base, isH: isH, isM: isM,
                    pri: isH ? st.high * 10 : (isM ? st.medium : 0)}});
    }});

    // ── 유럽 클러스터 마커 (축소 상태) — 숫자 배지 + 클릭 시 확대 ──
    if (clustered) {{
      var eu = euCentroid();
      if (eu) {{
        var isH2 = eu.high > 0;
        var col2 = isH2 ? '#f87171' : '#fbbf24';
        var glC2 = isH2 ? 'rgba(248,113,113,' : 'rgba(251,191,36,';
        var cr   = 12;
        // halo
        var gg = ctx.createRadialGradient(eu.x, eu.y, 0, eu.x, eu.y, cr*2.6);
        gg.addColorStop(0, glC2+'0.45)'); gg.addColorStop(1, glC2+'0)');
        ctx.beginPath(); ctx.arc(eu.x, eu.y, cr*2.6, 0, Math.PI*2); ctx.fillStyle = gg; ctx.fill();
        // ring
        ctx.beginPath(); ctx.arc(eu.x, eu.y, cr, 0, Math.PI*2);
        ctx.fillStyle = 'rgba(10,16,22,0.92)'; ctx.fill();
        ctx.lineWidth = 2; ctx.strokeStyle = col2; ctx.stroke();
        // count badge (국가 수)
        var fs2 = 12;
        ctx.font = 'bold ' + fs2 + 'px monospace';
        ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
        ctx.fillStyle = col2; ctx.fillText(String(eu.n), eu.x, eu.y + 0.5);
        ctx.textBaseline = 'alphabetic';
        // label
        ctx.font = 'bold 10px monospace';
        ctx.fillStyle = 'rgba(0,0,0,0.7)'; ctx.fillText('유럽', eu.x + 0.5, eu.y + cr + 12.5);
        ctx.fillStyle = isH2 ? '#fca5a5' : '#fde68a'; ctx.fillText('유럽', eu.x, eu.y + cr + 12);
        clusterBox = {{ x: eu.x, y: eu.y, r: cr + 4 }};
      }}
    }}

    // ── 라벨: 겹침 회피 (HIGH 우선, 겹치면 아래/위로 비켜서 배치) ──
    if (W > 440) {{
      var fs = Math.max(9, Math.round(W * 0.010));
      ctx.font = 'bold ' + fs + 'px monospace';
      ctx.textAlign = 'center'; ctx.textBaseline = 'alphabetic';
      labels.sort(function(a, b) {{ return b.pri - a.pri; }});
      var placed = [];
      labels.forEach(function(L) {{
        var w = ctx.measureText(L.cc).width;
        var above = L.y - L.base - 4, below = L.y + L.base + fs + 2;
        // 상단 가까운 마커는 라벨을 아래로(윗잘림 방지)
        var cands = (L.y < fs + L.base + 10)
          ? [below, below + (fs + 3), above]
          : [above, below, above - (fs + 3), below + (fs + 3)];
        var ly = cands[0];
        for (var ci = 0; ci < cands.length; ci++) {{
          var yy = cands[ci];
          var box = [L.x - w/2 - 2, yy - fs, L.x + w/2 + 2, yy + 3];
          var hit = false;
          for (var pi = 0; pi < placed.length; pi++) {{
            var p = placed[pi];
            if (box[0] < p[2] && box[2] > p[0] && box[1] < p[3] && box[3] > p[1]) {{ hit = true; break; }}
          }}
          if (!hit || ci === cands.length - 1) {{ ly = yy; placed.push(box); break; }}
        }}
        ctx.fillStyle = 'rgba(0,0,0,0.7)';
        ctx.fillText(L.cc, L.x + 0.5, ly + 0.5);
        ctx.fillStyle = L.isH ? '#fca5a5' : L.isM ? '#fde68a' : '#a5f3fc';
        ctx.fillText(L.cc, L.x, ly);
      }});
    }}
  }}

  /* ── 은은한 비네트 (깊이감만, 코너·좌표라벨 제거) ── */
  function drawHUD() {{
    var vig = ctx.createRadialGradient(W/2, H/2, H*0.35, W/2, H/2, H*0.95);
    vig.addColorStop(0, 'rgba(0,0,0,0)'); vig.addColorStop(1, 'rgba(0,0,0,0.35)');
    ctx.fillStyle = vig; ctx.fillRect(0, 0, W, H);
  }}

  /* ── Main loop ── */
  function loop() {{
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    ctx.clearRect(0, 0, W, H);
    // 뷰 변환(줌·팬) 적용 → 정적 레이어 + 마커 함께 스케일
    ctx.save();
    ctx.translate(VIEW.x, VIEW.y);
    ctx.scale(VIEW.s, VIEW.s);
    ctx.drawImage(off.canvas, 0, 0, W, H);
    drawMarkers();
    ctx.restore();
    drawHUD();
    tick++;
    animId = requestAnimationFrame(loop);
  }}

  function resize() {{
    W = container.clientWidth || 800;
    H = Math.round(W * (LAT_TOP - LAT_BOT) / 360);
    canvas.width  = W * DPR; canvas.height = H * DPR;
    canvas.style.width = W + 'px'; canvas.style.height = H + 'px';
    container.style.height = H + 'px';
    clampView();
    drawStaticLayer();
  }}

  rebuildActive(); resize();
  if (animId) cancelAnimationFrame(animId);
  loop();

  window.addEventListener('resize', function() {{
    if (animId) {{ cancelAnimationFrame(animId); animId = null; }}
    resize(); loop();
  }});

  // Tooltip + click + 줌/팬
  var tooltip = document.getElementById('worldmap-tooltip');
  function toWorld(mx, my) {{ return [(mx - VIEW.x) / VIEW.s, (my - VIEW.y) / VIEW.s]; }}
  function overCluster(mx, my) {{
    if (!clusterBox) return false;
    var wx = (mx - VIEW.x) / VIEW.s, wy = (my - VIEW.y) / VIEW.s;
    var d = Math.sqrt((wx-clusterBox.x)*(wx-clusterBox.x)+(wy-clusterBox.y)*(wy-clusterBox.y));
    return d < (clusterBox.r + 8);
  }}
  function hitTest(mx, my) {{
    var wpt = toWorld(mx, my), wx = wpt[0], wy = wpt[1];
    var clustered = VIEW.s < CLUSTER_ZOOM;
    var hit = null, minD = 24;
    Object.keys(COORDS).forEach(function(cc) {{
      if (clustered && EU[cc]) return;   // 클러스터에 묶인 개별국은 히트 제외
      var co = COORDS[cc], x = pX(co[1]), y = pY(co[0]);
      var d = Math.sqrt((wx-x)*(wx-x)+(wy-y)*(wy-y));
      if (d < minD) {{ minD = d; hit = cc; }}
    }});
    return hit;
  }}
  function zoomToEurope() {{
    var eu = euCentroid(); if (!eu) return;
    VIEW.s = 3.0;
    VIEW.x = W/2 - eu.x * VIEW.s;
    VIEW.y = H/2 - eu.y * VIEW.s;
    clampView();
  }}

  var dragging = false, dragMoved = false, lastX = 0, lastY = 0;
  canvas.addEventListener('mousedown', function(e) {{
    dragging = true; dragMoved = false;
    lastX = e.clientX; lastY = e.clientY;
  }});
  window.addEventListener('mouseup', function() {{ dragging = false; }});
  canvas.addEventListener('mousemove', function(e) {{
    var r = canvas.getBoundingClientRect();
    var mx = e.clientX - r.left, my = e.clientY - r.top;
    if (dragging) {{
      var dx = e.clientX - lastX, dy = e.clientY - lastY;
      if (Math.abs(dx) + Math.abs(dy) > 2) dragMoved = true;
      VIEW.x += dx; VIEW.y += dy; lastX = e.clientX; lastY = e.clientY;
      clampView();
      tooltip.style.display = 'none';
      canvas.style.cursor = 'grabbing';
      return;
    }}
    if (overCluster(mx, my)) {{
      var eu = euCentroid();
      tooltip.style.display = 'block';
      tooltip.style.left = (mx+16)+'px'; tooltip.style.top = (my-16)+'px';
      tooltip.innerHTML = '<strong style="color:#e2e8f0">유럽 ' + (eu?eu.n:0) + '개국</strong><br>'
        + '<span style="color:#94a3b8">클릭 → 확대</span>';
      canvas.style.cursor = 'pointer';
      return;
    }}
    var hit = hitTest(mx, my);
    if (hit) {{
      var st = STATS[hit] || {{total:0,high:0,medium:0}};
      var sig = st.high   > 0 ? '<span style="color:#f87171;font-weight:700">● HIGH '   + st.high   + '건</span>'
              : st.medium > 0 ? '<span style="color:#fbbf24">● MED '  + st.medium + '건</span>'
              : st.total  > 0 ? '<span style="color:#22d3ee">● LOW '  + st.total  + '건</span>'
              :                  '<span style="color:#475569">● 신호 없음</span>';
      tooltip.style.display = 'block';
      tooltip.style.left = (mx+16)+'px'; tooltip.style.top = (my-16)+'px';
      tooltip.innerHTML = '<strong style="color:#e2e8f0">' + (CNAMES[hit]||hit) + '</strong><br>' + sig;
      canvas.style.cursor = 'pointer';
    }} else {{
      tooltip.style.display = 'none';
      canvas.style.cursor = VIEW.s > 1 ? 'grab' : 'default';
    }}
  }});
  canvas.addEventListener('mouseleave', function() {{ if (tooltip) tooltip.style.display='none'; dragging = false; }});
  canvas.addEventListener('click', function(e) {{
    if (dragMoved) return;   // 드래그(팬)였으면 클릭 무시
    var r = canvas.getBoundingClientRect();
    var mx = e.clientX - r.left, my = e.clientY - r.top;
    if (overCluster(mx, my)) {{ zoomToEurope(); return; }}
    var hit = hitTest(mx, my);
    if (hit) openHeatmapDrilldown('all', hit);
  }});
  canvas.addEventListener('dblclick', function(e) {{
    e.preventDefault();
    VIEW.s = 1; VIEW.x = 0; VIEW.y = 0;   // 더블클릭 → 초기화
  }});
  canvas.addEventListener('wheel', function(e) {{
    e.preventDefault();
    var r = canvas.getBoundingClientRect();
    var mx = e.clientX - r.left, my = e.clientY - r.top;
    var wx = (mx - VIEW.x) / VIEW.s, wy = (my - VIEW.y) / VIEW.s;
    var factor = e.deltaY < 0 ? 1.18 : 1/1.18;
    VIEW.s *= factor;
    if (VIEW.s < MINS) VIEW.s = MINS;
    if (VIEW.s > MAXS) VIEW.s = MAXS;
    // 커서 지점을 고정점으로 유지
    VIEW.x = mx - wx * VIEW.s;
    VIEW.y = my - wy * VIEW.s;
    clampView();
    tooltip.style.display = 'none';
  }}, {{ passive: false }});
}})();"""


def _build_chart_scripts(trend: dict, distribution: list) -> str:
    """Chart.js 초기화 스크립트 (Chart.js 로드 후 실행될 코드)."""
    scripts = []

    if trend["weeks"]:
        data_json = json.dumps({
            "labels": trend["weeks"],
            "high":   trend["high"],
            "medium": trend["medium"],
            "low":    trend["low"],
        })
        scripts.append(f"""
(function() {{
  var d = {data_json};
  var ctx = document.getElementById('trendChart');
  if (!ctx || typeof Chart === 'undefined') return;
  new Chart(ctx, {{
    type: 'line',
    data: {{
      labels: d.labels,
      datasets: [
        {{ label: 'HIGH',   data: d.high,   borderColor:'#e05353', backgroundColor:'rgba(224,83,83,0.08)',  tension:0.35, fill:true,  pointRadius:3, borderWidth:2 }},
        {{ label: 'MEDIUM', data: d.medium, borderColor:'#d4943a', backgroundColor:'rgba(212,148,58,0.05)', tension:0.35, fill:false, pointRadius:2, borderWidth:1.5 }},
        {{ label: 'LOW',    data: d.low,    borderColor:'#9aa3b5', backgroundColor:'transparent',            tension:0.35, fill:false, pointRadius:2, borderWidth:1 }}
      ]
    }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      plugins: {{ legend: {{ position: 'top', labels: {{ font: {{ size: 11 }}, color:'#9aa4a6', boxWidth:12 }} }} }},
      scales: {{
        y: {{ beginAtZero: true, grid: {{ color:'rgba(255,255,255,0.06)' }}, ticks: {{ color:'#9aa4a6', precision: 0, font: {{ size: 10 }} }} }},
        x: {{ grid: {{ color:'rgba(255,255,255,0.06)' }}, ticks: {{ color:'#9aa4a6', font: {{ size: 10 }}, maxRotation: 45 }} }}
      }}
    }}
  }});
}})();""")

    if distribution:
        labels = [ACTIVITY_LABELS.get(d["activity_type"], d["activity_type"]) for d in distribution]
        totals = [d["total"] for d in distribution]
        colors = ["#4a8fd4","#8b95ff","#9b7fe8","#4ab884","#e05353","#6f7aa0","#d4943a"][:len(distribution)]
        data_json = json.dumps({"labels": labels, "data": totals, "colors": colors})
        scripts.append(f"""
(function() {{
  var d = {data_json};
  var ctx = document.getElementById('actChart');
  if (!ctx || typeof Chart === 'undefined') return;
  new Chart(ctx, {{
    type: 'doughnut',
    data: {{
      labels: d.labels,
      datasets: [{{ data: d.data, backgroundColor: d.colors, borderWidth: 2, borderColor: '#0f1118' }}]
    }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      plugins: {{
        legend: {{ position: 'right', labels: {{ font: {{ size: 10 }}, color:'#4c5468', boxWidth: 12 }} }},
        tooltip: {{ callbacks: {{ label: function(c) {{ return c.label + ': ' + c.parsed + '건'; }} }} }}
      }}
    }}
  }});
}})();""")

    return "\n".join(scripts)


# ---------------------------------------------------------------------------
# 커맨드센터 (개편) 렌더러
# ---------------------------------------------------------------------------

def _delta_html(cur: int, prev: int, cap: str = "직전 동기") -> str:
    """직전 동일기간 대비 증감 → ▲/▼/– + % (.d.pos/.neg/.neu). prev 없으면 캡션만."""
    if not prev:
        return f'<div class="d neu"><span class="cap">{cap} 데이터 없음</span></div>'
    pct = (cur - prev) / prev * 100
    if pct >= 0.5:
        cls, arw = "pos", "▲"
    elif pct <= -0.5:
        cls, arw = "neg", "▼"
    else:
        cls, arw = "neu", "–"
    return (f'<div class="d {cls}">{arw} {abs(pct):.0f}%'
            f'<span class="cap">vs {cap}</span></div>')


def _sparkline_svg(series: list, w: int = 96, h: int = 20) -> str:
    """일자별 수치 → 면적형 스파크라인 SVG. 데이터 < 2개면 빈 문자열."""
    vals = [float(v) for v in (series or [])]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    n = len(vals)
    def _pt(i, v):
        x = i / (n - 1) * w
        y = h - ((v - lo) / rng) * (h - 2) - 1
        return f"{x:.1f},{y:.1f}"
    pts = " ".join(_pt(i, v) for i, v in enumerate(vals))
    line = "M" + " L".join(pts.split(" "))
    area = f"M0,{h} L" + " L".join(pts.split(" ")) + f" L{w},{h} Z"
    ex, ey = _pt(n - 1, vals[-1]).split(",")
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" preserveAspectRatio="none" aria-hidden="true">'
            f'<path class="fill" d="{area}"/><path d="{line}"/>'
            f'<circle class="end" cx="{ex}" cy="{ey}" r="1.7"/></svg>')


def _render_metric_rail(stats: dict, growth_story: dict, spikes: list, days: int) -> str:
    """상단 지표 레일 6종 (kpi-* id 보존 → setPeriod 연동). 직전 동기 증감 + 수집 스파크라인."""
    o = (growth_story or {}).get("overall") or {}
    yoy = o.get("yoy_pct")
    yoy_txt = (f'{"+" if yoy >= 0 else ""}{yoy:.0f}%') if yoy is not None else "—"
    nsp = len(spikes or [])
    spark = _sparkline_svg(stats.get("spark") or [])
    d_total = _delta_html(stats.get("total", 0), stats.get("prev_total", 0))
    d_high  = _delta_html(stats.get("high", 0), stats.get("prev_high", 0))
    d_brand = _delta_html(stats.get("brands_active", 0), stats.get("prev_brands_active", 0))
    d_ctry  = _delta_html(stats.get("countries_active", 0), stats.get("prev_countries_active", 0))
    return (
        '<div class="rail">'
        f'<div class="met"><div class="l">수집 {days}D</div><div class="v tnum"><span id="kpi-total">{stats.get("total",0):,}</span></div><div id="kpi-d-total">{d_total}</div><div id="kpi-spark">{spark}</div></div>'
        f'<div class="met"><div class="l">HIGH 신호</div><div class="v tnum" style="color:var(--coral)"><span id="kpi-high">{stats.get("high",0)}</span></div><div id="kpi-d-high">{d_high}</div></div>'
        f'<div class="met"><div class="l">활성 브랜드</div><div class="v tnum"><span id="kpi-brands">{stats.get("brands_active",0)}</span></div><div id="kpi-d-brands">{d_brand}</div></div>'
        f'<div class="met"><div class="l">커버 국가</div><div class="v tnum"><span id="kpi-countries">{stats.get("countries_active",0)}</span></div><div id="kpi-d-countries">{d_ctry}</div></div>'
        f'<div class="met"><div class="l">주요국 수출 YoY</div><div class="v tnum" style="color:var(--teal)">{yoy_txt}</div><div class="d pos">{o.get("growers",0)}개국↑<span class="cap">전년동기</span></div></div>'
        f'<div class="met"><div class="l">검색 급등</div><div class="v tnum" style="color:var(--amber)">{nsp}</div><div class="d neu"><span class="cap">글로벌·해외</span></div></div>'
        '</div>'
    )


def _render_move_stream(high_articles: list, demand_tri: list) -> str:
    """핵심 무브 스트림 — HIGH 중복제거(브랜드·국가·활동) + 수요 verdict 라벨."""
    verdict_by_brand = {t["brand"]: t.get("verdict") for t in (demand_tri or [])}
    _VB = {"real": ("실질", "vb-real"), "pr": ("PR우세", "vb-pr"),
           "latent": ("숨은수요", "vb-latent"), "stable": ("안정", "vb-latent")}
    seen, rows = set(), []
    for a in sorted(high_articles or [], key=lambda x: -(x.get("score") or 0)):
        k = (a.get("brand"), a.get("country"), a.get("activity_type"))
        if k in seen:
            continue
        seen.add(k)
        cc = a.get("country", "")
        flag = COUNTRY_FLAGS.get(cc, "🌐")
        act = ACTIVITY_LABELS.get(a.get("activity_type", ""), a.get("activity_type", ""))
        title = _esc((a.get("title_ko") or a.get("title") or "")[:76])
        v = verdict_by_brand.get(a.get("brand"))
        vb = (f'<span class="vb {_VB[v][1]}">{_VB[v][0]}</span>' if v in _VB else "")
        dcls = "dot-h" if a.get("importance") == "high" else "dot-m"
        rows.append(
            f'<div class="ev" onclick="openHeatmapDrilldown(\'{_esc(a.get("brand",""))}\',\'{cc}\',\'all\')">'
            f'<div class="m"><span class="{dcls}">●</span>{flag}{_esc(cc)} · {_esc(act)}{vb}</div>'
            f'<div class="t">{title}</div></div>'
        )
        if len(rows) >= 7:
            break
    body = "".join(rows) or '<div class="ev"><div class="t" style="color:var(--lo)">이번 기간 핵심 무브 없음</div></div>'
    return f'<div class="stream">{body}</div>'


def _render_brand_signals(summary: list) -> str:
    """브랜드 신호 요약(직관형) — 불투명 막대 대신 실수치+라벨 칩. '왜 강한지' 바로 보이게."""
    if not summary:
        return ('<div class="bsig"><div class="bsig-empty">신호 데이터 축적 중</div></div>')
    rows = []
    for i, b in enumerate(summary, 1):
        chips = "".join(
            f'<span class="bsig-chip t-{sg["tone"]}">{sg["icon"]} {_esc(sg["text"])}</span>'
            for sg in b["signals"])
        tier = '<span class="bsig-tier">1군</span>' if b.get("tier") == 1 else ""
        rows.append(
            f'<div class="bsig-row" onclick="openHeatmapDrilldown(\'{_esc(b["brand"])}\',\'all\',\'all\')">'
            f'<span class="bsig-rank">{i}</span>'
            f'<span class="bsig-brand">{_esc(b["brand"])}{tier}</span>'
            f'<div class="bsig-chips">{chips}</div></div>'
        )
    note = ('<div class="bsig-note">📰 기사=최근 4주 보도량(괄호=직전 4주 대비 배수) · '
            '🔍 검색=구글 급등비 · 💰 매출=최신 연간 YoY · 🪧 상표=US·JP 출원 · 🛒 리테일=아마존 순위. '
            '초록=상승·강세.</div>')
    return f'<div class="bsig">{"".join(rows)}</div>{note}'


def _render_composite_lb(composite: list, trend: dict = None) -> str:
    """브랜드 종합 스코어 리더보드 (21개 전부, 4색 서브신호 바). 추세 있으면 ▲▼."""
    if not composite:
        return ('<div class="lb"><div class="ev"><div class="t" style="color:var(--lo);padding:6px">'
                '종합 스코어 데이터 축적 중</div></div></div>')
    trend = trend or {}
    rows = []
    for o in composite:
        subs = o["subs"]
        bars = ""
        for key, cls in (("momentum", "sub-mom"), ("financial", "sub-fin"),
                         ("trademark", "sub-tm"), ("demand", "sub-dem")):
            v = subs.get(key)
            h = int(round((v or 0) * 100)) if v is not None else 3
            op = "" if v is not None else "opacity:.25"
            bars += f'<i class="{cls}" style="height:{max(h,3)}%;{op}"></i>'
        sccol = "var(--champ2)" if o["rank"] <= 2 else "var(--hi)"
        # 시계열 추세(주간 스냅샷 ≥2점일 때만): 첫 대비 증감
        tr = trend.get(o["brand"]) or {}
        dl = tr.get("delta")
        trend_html = ""
        if tr.get("points", 0) >= 2 and dl is not None and abs(dl) >= 0.5:
            tc = "var(--teal)" if dl > 0 else "var(--coral)"
            arw = "▲" if dl > 0 else "▼"
            trend_html = f'<span class="lb-tr" style="color:{tc}" title="지난 스냅샷 대비">{arw}{abs(dl):.0f}</span>'
        rows.append(
            f'<div class="r" onclick="openHeatmapDrilldown(\'{_esc(o["brand"])}\',\'all\',\'all\')">'
            f'<span class="rk">{o["rank"]}</span><span class="nm">{_esc(o["brand"])}</span>'
            f'<span class="subs" title="4축 신호 상대강도(모멘텀·재무·상표·수요)">{bars}</span>'
            f'{trend_html}</div>'
        )
    key = ('<div class="lb-key">'
           '<span title="최근 4주 뉴스 활동량·중요도 (가중 35%)"><i class="sub-mom"></i>모멘텀</span>'
           '<span title="매출·영업이익 등 재무 실적 (가중 25%)"><i class="sub-fin"></i>재무</span>'
           '<span title="US·JP 화장품 상표 출원 = 진출 선행신호 (가중 15%)"><i class="sub-tm"></i>상표</span>'
           '<span title="네이버·구글 검색 수요 (가중 25%)"><i class="sub-dem"></i>수요</span></div>')
    return f'<div class="lb">{"".join(rows)}</div>{key}'


def _parse_market_sections(market_text: str) -> dict:
    """시장요약 텍스트(### 대응/기회/점검) → {respond,opportunity,check: [bullets]}."""
    import re as _re
    out = {"respond": [], "opportunity": [], "check": []}
    for part in _re.split(r"###\s+", market_text or ""):
        part = part.strip()
        if not part:
            continue
        nl = part.find("\n")
        label = (part if nl < 0 else part[:nl]).strip()
        seg = "" if nl < 0 else part[nl + 1:]
        bullets = [ln.strip().lstrip("-•").strip().replace("**", "")
                   for ln in seg.split("\n") if ln.strip().startswith(("-", "•"))]
        if not bullets:
            continue
        if "대응" in label:
            out["respond"] += bullets
        elif "기회" in label or "확장" in label:
            out["opportunity"] += bullets
        else:
            out["check"] += bullets
    return out


def _urgency_badge(text: str) -> tuple:
    """'[시급:높음/중간/낮음]' 패턴 추출 → (본문에서 제거된 텍스트, 기존 insight-badge span)."""
    import re as _re
    m = _re.search(r"\[?\s*시급\s*[:\-]?\s*(높음|중간|낮음|high|medium|low)\s*\]?", text, _re.I)
    if not m:
        return text, ""
    lvl = m.group(1).lower()
    cls, lab = {
        "높음": ("insight-badge-high-hot", "시급 높음"), "high": ("insight-badge-high-hot", "시급 높음"),
        "중간": ("insight-badge-high-warm", "시급 중간"), "medium": ("insight-badge-high-warm", "시급 중간"),
        "낮음": ("insight-badge-high-low", "시급 낮음"), "low": ("insight-badge-high-low", "시급 낮음"),
    }.get(lvl, ("insight-badge-high-warm", "시급"))
    clean = _re.sub(r"\s*\[?\s*시급\s*[:\-]?\s*(높음|중간|낮음|high|medium|low)\s*\]?\s*", "", text, flags=_re.I).strip(" ·-—")
    return clean, f'<span class="insight-badge {cls}">{lab}</span>'


def _urgency_li(bullet: str) -> str:
    """시장 인사이트 bullet → <li>본문 + 시급 배지</li>. [시급:X] 없으면 배지 생략."""
    clean, badge = _urgency_badge(bullet)
    return f"<li>{_esc(clean)}{badge}</li>"


def _clean_product_name(name: str, brand: str) -> str:
    """제품명 정리 — 프로모 대괄호·선행 브랜드명 트림. (헤더에 브랜드 있으니 중복 제거)"""
    import re as _re
    n = name or ""
    n = _re.sub(r"\[[^\]]*\]", "", n)                    # [Hudson's Pick] 등 프로모 태그 제거
    n = _re.sub(r"^\s*(?:" + _re.escape(brand) + r"|d'?alba|skin1004|anua|cosrx)[\s\W]*", "",
                n, flags=_re.I)                          # 선행 브랜드명 트림
    n = n.split(",")[0]                                  # 첫 구절만(장황한 SEO 꼬리 제거)
    return n.strip(" -–—·")[:48] or (name or "")[:48]


def _retail_rank_line(rt: dict) -> str:
    """리테일 순위 한 줄 — 광역/전문 라벨 + 국기 + 별점·리뷰. 순위 해석 명확화."""
    rflag = COUNTRY_FLAGS.get(rt.get("country", ""), "")
    cat = _esc(rt.get("category", ""))
    scope = "뷰티 전체" if rt.get("is_broad") else cat   # 광역 노드면 '전체' 명시
    rate = f" · ⭐{rt['rating']}" if rt.get("rating") else ""
    rev = rt.get("reviews")
    rev_txt = f" · {rev // 1000}K리뷰" if rev and rev >= 1000 else (f" · {rev}리뷰" if rev else "")
    return f'🛒 {rflag}아마존 {scope} <b>#{rt["rank"]}</b>{rate}{rev_txt}'


def _render_opportunity_stories(stories: list) -> str:
    """핵심 서사 카드(재구성) — 판독→제품(순위)→왜→시장포지션(우리/확장)→액션. BLUF·5W."""
    if not stories:
        return ""
    _ACT = {"신시장_진출": "신시장 진출", "유통_채널": "유통 채널", "신제품_런칭": "신제품 런칭",
            "인플루언서_협업": "인플루언서 협업", "투자_BD": "투자·BD", "브랜드_마케팅": "브랜드 마케팅",
            "실적_공시": "실적·공시", "가격_프로모션": "가격·프로모션", "기타": "기타"}

    def _pos_line(items, limit=3):
        seen, out = set(), []
        for z in items:                                  # 이미 rank asc 정렬 → (국가,카테고리)별 최고만
            key = (z["country"], z["category"])
            if key in seen:
                continue
            seen.add(key)
            lbl = "뷰티 전체" if z.get("is_broad") else z["category"]
            out.append(f'{COUNTRY_FLAGS.get(z["country"], "")}{_esc(lbl)} #{z["rank"]}')
            if len(out) >= limit:
                break
        return " · ".join(out) or "—"

    cards = []
    for s in stories[:6]:
        brand = s.get("brand", "")
        cc = s.get("country", "")
        flag = COUNTRY_FLAGS.get(cc, "🌐")
        neg = '<span class="os-neg">⚠️ 악재</span>' if s.get("has_negative") else ""
        # BLUF: 판독(결론)
        sr = s.get("signal_read") or {}
        verdict = (f'<div class="os-verdict os-read-{sr.get("tone","grow")}" title="{_esc(sr.get("why",""))}">'
                   f'{_esc(sr.get("label",""))}</div>') if sr.get("label") else ""
        # 🏆 잘 나가는 제품 = 순위를 낸 리테일 제품
        perf = s.get("perf", {})
        rt = perf.get("retail")
        mv = s.get("move", {})
        if rt and rt.get("rank"):
            prod = _clean_product_name(rt.get("product") or "", brand) or "(제품명 미상)"
            prod_block = (f'<div class="os-sec"><div class="os-k">🏆 잘 나가는 제품</div>'
                          f'<div class="os-prod">{_esc(prod)}</div>'
                          f'<div class="os-rank">{_retail_rank_line(rt)}</div></div>')
        else:
            # 리테일 매칭 없음 → 뉴스 신호 기반임을 명시(오해 방지)
            npd = (s.get("products") or [None])[0]
            prod_block = ('<div class="os-sec"><div class="os-k">🏆 잘 나가는 제품</div>'
                          f'<div class="os-prod os-dim">{_esc(npd) if npd else "리테일 순위 매칭 없음"}</div>'
                          '<div class="os-rank os-dim">뉴스·검색 신호 기반 (아마존 순위권 밖)</div></div>')
        # 💡 왜 높나
        why_chips = "".join(f'<span class="os-wchip">{_esc(w)}</span>' for w in (s.get("why") or []))
        ing_extra = "".join(f'<span class="os-ing">{_esc(i)}</span>' for i in (s.get("ingredients") or [])[:4])
        why_block = (f'<div class="os-sec"><div class="os-k">💡 왜 높나</div>'
                     f'<div class="os-why">{why_chips}{ing_extra}</div></div>')
        # 🗺 시장 포지션 — 우리영역 / 확장후보 (동등)
        areas = s.get("retail_areas") or {"our": [], "expansion": []}
        pos_block = (f'<div class="os-sec"><div class="os-k">🗺 시장 포지션</div>'
                     f'<div class="os-pos"><span class="os-tag our">우리영역</span> {_pos_line(areas.get("our", []))}</div>'
                     f'<div class="os-pos"><span class="os-tag exp">확장후보</span> {_pos_line(areas.get("expansion", []))}</div></div>')
        # 무브(맥락) — 접힘성 한 줄 + 원문
        url = mv.get("url") or ""
        src = (f'<a class="os-src" href="{_esc(url)}" target="_blank" rel="noopener">원문 ↗</a>'
               if url.startswith("http") else "")
        move_line = (f'<div class="os-move"><span class="os-act">{_esc(_ACT.get(mv.get("activity_type",""), ""))}</span> '
                     f'{_esc((mv.get("title") or "")[:70])} {src}</div>')
        # 👉 우리가 할 것
        action = _esc(s.get("action") or "")
        action_block = (f'<div class="os-action"><span class="os-ac-k">👉 우리가 할 것</span>'
                        f'<span class="os-ac-v">{action}</span></div>') if action else ""
        cards.append(
            f'<div class="ostory" onclick="openHeatmapDrilldown(\'{_esc(brand)}\',\'{_esc(cc)}\',\'all\')">'
            f'<div class="os-head"><span class="os-loc">{flag} {_esc(s.get("country_name",""))} · '
            f'<b>{_esc(brand)}</b></span>{neg}</div>'
            f'{verdict}{prod_block}{why_block}{pos_block}{move_line}{action_block}'
            f'</div>'
        )
    return f'<div class="ostory-grid">{"".join(cards)}</div>'


def _render_action_banner(market_text: str) -> str:
    """'지금 대응' 액션 배너 — 최상단·코럴 강조. [시급:X]은 기존 insight-badge로 통일."""
    resp = _parse_market_sections(market_text)["respond"][:3]
    if not resp:
        return ""
    items = []
    for b in resp:
        clean, badge = _urgency_badge(b)
        items.append(f"<li>{_esc(clean)}{badge}</li>")
    lis = "".join(items)
    return (
        '<div class="action-banner">'
        '<div class="ab-label">⚑ 지금 대응해야 할 것</div>'
        f'<ul class="ab-list">{lis}</ul>'
        '</div>'
    )


def _render_synth(stats7: dict, market_text: str, growth_story: dict, composite: list) -> str:
    """AI 종합 인사이트 — 데이터 리드 + 합성문단 + 기회/점검(대응은 상단 배너로 분리)."""
    o = (growth_story or {}).get("overall") or {}
    mkts = (growth_story or {}).get("markets") or []
    top_mkt = mkts[0] if mkts else None
    top_brand = composite[0] if composite else None
    yoy = o.get("yoy_pct")
    high7 = (stats7 or {}).get("high", 0)
    growers = o.get("growers")
    top3 = ", ".join(_esc(b["brand"]) for b in composite[:3]) if composite else ""
    top_mkt_name = _esc(_COUNTRY_KO_LBL.get(top_mkt["country_code"], top_mkt["country_name"])) if top_mkt else ""

    bits = []
    if yoy is not None:
        bits.append(f'주요국 화장품 수출 <b>{"+" if yoy>=0 else ""}{yoy:.0f}%</b>')
    if top_mkt:
        bits.append(f'최고 성장 <b>{top_mkt_name} +{top_mkt["yoy_pct"]:.0f}%</b>')
    if top_brand:
        bits.append(f'종합 스코어 1위 <b>{_esc(top_brand["brand"])}</b>')
    lead = " · ".join(bits) if bits else "최근 7일 경쟁 신호를 5축(뉴스·검색·수출·재무·상표)으로 교차검증했습니다."

    sents = []
    if high7:
        sents.append(f'최근 7일 HIGH 신호 <b>{high7}건</b>이 포착됐습니다.')
    if top_mkt and growers is not None:
        sents.append(f'주요국 화장품 수출은 <b>{growers}개국</b>에서 늘었고, 그중 <b>{top_mkt_name}</b>가 전년 대비 <b>+{top_mkt["yoy_pct"]:.0f}%</b>로 가장 가팔랐습니다.')
    if top3:
        sents.append(f'브랜드 종합 스코어는 <b>{top3}</b> 순으로 높습니다.')
    para = " ".join(sents) or "관세청·DART·KIPRIS·검색 신호를 뉴스와 교차검증해 정리했습니다."

    facts = []
    if high7:
        facts.append(f'<span class="sf"><i>HIGH</i>{high7}건</span>')
    if growers is not None:
        facts.append(f'<span class="sf"><i>수출 성장국</i>{growers}개</span>')
    if composite:
        facts.append(f'<span class="sf"><i>종합 스코어</i>{len(composite)}브랜드</span>')
    facts_html = f'<div class="synth-facts">{"".join(facts)}</div>' if facts else ""

    sec = _parse_market_sections(market_text)
    cols = []
    for kind, klabel in (("opportunity", "선점할 기회"), ("check", "확인·점검")):
        b = sec[kind][:3]
        if not b:
            continue
        lis = "".join(_urgency_li(x) for x in b)
        cols.append(f'<div class="scol {kind}"><div class="h">{klabel}</div><ul>{lis}</ul></div>')
    cols_html = "".join(cols) or '<div class="scol check"><div class="h">점검</div><ul><li>시장 인사이트 생성 중</li></ul></div>'

    return (
        '<div class="synth hud">'
        '<div class="lead"><div class="tl">Weekly Synthesis · AI 종합</div>'
        f'<h2>{lead}</h2>'
        f'<p class="synth-body">{para}</p>'
        f'{facts_html}'
        '<div class="by">최근 7일 · 뉴스·검색·수출·재무·상표 5축 교차검증</div></div>'
        f'<div class="cols">{cols_html}</div>'
        '</div>'
    )


def _render_legend() -> str:
    """통합 범례 — 페이지의 모든 배지/색 의미를 한 곳에서(접이식). 처음 보는 사람도 해석 가능."""
    def grp(title, items):
        chips = "".join(
            f'<span class="lg-item"><span class="lg-dot" style="background:{c}"></span>{_esc(lab)}</span>'
            for lab, c in items)
        return f'<div class="lg-grp"><span class="lg-t">{title}</span>{chips}</div>'
    groups = [
        grp("검증(뉴스vs수요)", [("실질", "var(--teal)"), ("PR우세", "var(--coral)"),
                                ("숨은수요", "var(--champ)"), ("안정", "var(--mid)")]),
        grp("중요도", [("HIGH", "var(--coral)"), ("MED", "var(--amber)")]),
        grp("모멘텀", [("▲ 급상승", "var(--teal)"), ("▶ 안정", "var(--mid)"), ("▼ 둔화", "var(--coral)")]),
        grp("종합 스코어 축", [("모멘텀", "var(--violet)"), ("재무", "var(--teal)"),
                              ("상표", "var(--champ)"), ("수요", "var(--amber)")]),
        grp("수출 YoY", [("성장 +15%↑", "var(--teal)"), ("둔화 -10%↓", "var(--coral)")]),
    ]
    return (
        '<details class="legend"><summary>범례 · 배지/색상 의미 <span class="lg-hint">(클릭)</span></summary>'
        '<div class="lg-body">' + "".join(groups) + '</div></details>'
    )


# ---------------------------------------------------------------------------
# HTML 조립
# ---------------------------------------------------------------------------

def _build_full_html(
    stats: dict,
    high_articles: list,
    matrix: dict,
    trend: dict,
    distribution: list,
    brand_act: list,
    brand_high: list,
    brand_insights: dict,
    chartjs_src: str,
    days: int,
    country_stats: dict = None,
    period_data: dict = None,
    brand_radar: list = None,
    category_battle: list = None,
    expansion_playbook: list = None,
    briefing_archive: list = None,
    momentum: list = None,
    market_text: str = "",
    digest: dict = None,
    demand_tri: list = None,
    export_growth: list = None,
    growth_story: dict = None,
    financials: list = None,
    nice_financials: list = None,
    trademark_sig: dict = None,
    search_spikes: list = None,
    composite: list = None,
    brand_signals: list = None,
    stories: list = None,
    score_trend: dict = None,
) -> str:
    has_chartjs = bool(chartjs_src)
    generated = datetime.utcnow() + timedelta(hours=9)
    generated_str = generated.strftime("%Y-%m-%d %H:%M KST")

    kpi_html          = _render_kpi_cards(stats)
    brands_list       = matrix.get("brands", [])
    act_types_list    = sorted({d["activity_type"] for d in distribution})
    filter_bar_html   = _render_filter_bar(brands_list, act_types_list)
    high_html         = _render_high_table(high_articles)
    heatmap_html      = _render_heatmap(matrix)
    brand_high_html   = _render_brand_high_ratio(brand_high)
    brand_act_html    = _render_brand_activity_bar(brand_act)
    category_battle_html = _render_category_battle(category_battle or [])
    expansion_playbook_html = _render_expansion_playbook(expansion_playbook or [])
    briefing_archive_html = _render_briefing_archive(briefing_archive or [])
    _dg = digest or {}
    overview_digest_html = _render_overview_digest(
        _dg.get("stats") or stats, momentum or [], _dg.get("cat") or [],
        _dg.get("expansion") or [], _dg.get("high") or [], _dg.get("market") or "",
        _dg.get("ref_date") or "")
    radar_html        = _render_brand_radar(brand_radar or [])
    demand_html       = _render_demand_signal(demand_tri or [])
    export_growth_html = _render_export_growth(export_growth or [])
    growth_story_html  = _render_growth_story(growth_story or {})
    growth_headline_html = _render_growth_headline(growth_story or {})
    financials_html   = _render_financials(financials or [])
    financials_nice_html = _render_financials_nice(nice_financials or [])
    trademark_html    = _render_trademark(trademark_sig or {})
    search_spikes_html = _render_search_spikes(search_spikes or [])
    # ── 커맨드센터(브리핑 탭) 신규 블록 ──
    metric_rail_html  = _render_metric_rail(stats, growth_story or {}, search_spikes or [], days)
    move_stream_html  = _render_move_stream(_dg.get("high") or high_articles, demand_tri or [])
    composite_lb_html = _render_composite_lb(composite or [], score_trend or {})
    brand_signals_html = _render_brand_signals(brand_signals or [])
    _market7 = _dg.get("market") or market_text
    action_banner_html = _render_action_banner(_market7)
    stories_html      = _render_opportunity_stories(stories or [])
    legend_html       = _render_legend()
    synth_html        = _render_synth(_dg.get("stats") or stats,
                                      _market7, growth_story or {}, composite or [])
    _mk = (growth_story or {}).get("markets") or []
    market_list_html = "".join(
        f'<div class="mk-row" onclick="switchTab(\'strategy\')">'
        f'<span class="flag">{COUNTRY_FLAGS.get(m["country_code"], "🌐")}</span>'
        f'<div><div class="nm">{_esc(_COUNTRY_KO_LBL.get(m["country_code"], m["country_name"]))}</div>'
        f'<div class="why">{_esc((m["moves"][0]["brand"] + " · " + ACTIVITY_LABELS.get(m["moves"][0]["activity_type"], "")) if m.get("moves") else "활동 축적 중")}</div></div>'
        f'<span class="yoy">{"+" if m["yoy_pct"] >= 0 else ""}{m["yoy_pct"]:.0f}%</span></div>'
        for m in _mk[:7]
    ) or '<div class="mk-row"><div class="why" style="padding:10px">수출 데이터 축적 중 (관세청 수집 후)</div></div>'
    insights_script   = _build_insights_script(brand_insights)
    market_script     = _build_market_script()
    trend_html        = _canvas_or_table_trend(trend, has_chartjs)
    activity_html     = _canvas_or_table_activity(distribution, has_chartjs)
    chart_scripts     = _build_chart_scripts(trend, distribution) if has_chartjs else ""
    stacked_script    = _build_stacked_bar_script(brand_act)

    chartjs_tag = f"<script>{chartjs_src}</script>" if has_chartjs else ""

    worldmap_css     = _WORLDMAP_CSS
    worldmap_section = _render_worldmap_section(high_articles)
    worldmap_script  = _build_worldmap_script(country_stats or {})

    # Pre-compute JSON outside f-string to avoid {{...}} dict-in-set TypeError
    high_data_json = json.dumps(
        [_fmt_art_for_js(a) for a in high_articles], ensure_ascii=False
    )

    # Period data for client-side switching (30/60/90일 presets)
    def _esc_s(s: str) -> str:
        return html_lib.escape(str(s or ""), quote=True)

    _pd = period_data or {}
    period_data_for_js = {
        str(p): {
            "kpi": {
                "total":     v["kpi"]["total"],
                "high":      v["kpi"]["high"],
                "brands":    v["kpi"]["brands"],
                "countries": v["kpi"]["countries"],
                "prev_total":     v["kpi"].get("prev_total", 0),
                "prev_high":      v["kpi"].get("prev_high", 0),
                "prev_brands":    v["kpi"].get("prev_brands", 0),
                "prev_countries": v["kpi"].get("prev_countries", 0),
                "spark":          v["kpi"].get("spark", []),
            },
            "articles":      v["articles"],
            "country_stats": v["country_stats"],
            "market":        _esc_s(v.get("market", "")),
            "insights": {
                brand: {
                    "top_act":       _esc_s(ins["top_act"]),
                    "top_pct":       ins["top_pct"],
                    "high_pct":      ins["high_pct"],
                    "strategy":      _esc_s(ins["strategy"]),
                    "top_countries": ins["top_countries"],
                    "key_articles":  [
                        {
                            "imp":      a.get("imp", "low"),
                            "date":     a.get("date", ""),
                            "act":      _esc_s(a.get("act", "")),
                            "title_ko": _esc_s(a.get("title_ko", "")),
                            "url":      a.get("url", ""),
                        }
                        for a in ins.get("key_articles", [])
                    ],
                }
                for brand, ins in v.get("insights", {}).items()
            },
        }
        for p, v in _pd.items()
    }
    period_data_json = json.dumps(period_data_for_js, ensure_ascii=False)

    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>K-뷰티 경쟁사 인텔리전스 — 최근 {days}일</title>
{chartjs_tag}
<style>{_DASHBOARD_CSS}{worldmap_css}</style>
</head>
<body>
<div class="mast">
  <div class="mk"><span class="sq"></span><h1>CELLFUSION <span>INTEL</span></h1></div>
  <span class="live"><span class="bl"></span>LIVE</span>
  <span class="sp"></span>
  <span class="clk">최근 <span id="period-label">{days}</span>D · {_esc(generated_str)}</span>
</div>

<div class="tabbar">
  <div class="tabbar-tabs">
    <button class="tab-btn active" data-tab="overview" onclick="switchTab('overview')">브리핑</button>
    <button class="tab-btn" data-tab="brands" onclick="switchTab('brands')">경쟁사</button>
    <button class="tab-btn" data-tab="strategy" onclick="switchTab('strategy')">시장</button>
    <button class="tab-btn" data-tab="finance" onclick="switchTab('finance')">재무</button>
    <button class="tab-btn" data-tab="feed" onclick="switchTab('feed')">기록</button>
    <button class="tab-btn" data-tab="search" onclick="switchTab('search')">검색</button>
  </div>
  <div class="period-row">
    <span class="period-row-label">기간<span class="period-basis-hint" title="상단 기간은 뉴스 수집 범위(KPI·기사·지도·브랜드동향)에 적용됩니다. 검색=주간, 수출=월간, 재무=연간처럼 데이터 성격상 자체 기준을 쓰는 모듈은 각 섹션에 기준을 표기합니다.">ⓘ 뉴스 기준</span></span>
    <div class="period-presets" id="pb-presets">
      <button class="period-btn{"" if days != 30 else " active"}" data-days="30" onclick="setPeriod(30)">30일</button>
      <button class="period-btn{"" if days != 60 else " active"}" data-days="60" onclick="setPeriod(60)">60일</button>
      <button class="period-btn{"" if days != 90 else " active"}" data-days="90" onclick="setPeriod(90)">90일</button>
    </div>
    <div class="period-vsep"></div>
    <div class="period-range">
      <input type="date" id="from-date" class="period-date-input" />
      <span class="period-date-sep">~</span>
      <input type="date" id="to-date" class="period-date-input" />
      <button class="period-apply-btn" onclick="applyDateRange()">조회</button>
    </div>
    <span id="period-msg" class="period-msg" style="display:none"></span>
  </div>
</div>

<div class="page-body">

  <!-- ===== 탭: 브리핑 (심플 종합 — 지금 대응→오늘→이번주→스토리) ===== -->
  <div class="tab-panel active" id="tab-overview">
    <!-- 1) 지금 대응이 필요한 것 (최우선) -->
    <div class="eyebrow"><span class="lab">지금 대응이 필요한 것</span><span class="rule"></span><span class="rt">이번 주 최우선</span></div>
    {action_banner_html}

    <!-- 2) 오늘 핵심 지표 -->
    {metric_rail_html}

    <!-- 3) 이번 주 동향 — 가장 활발한 시장 | 핵심 무브 -->
    <div class="eyebrow"><span class="lab">이번 주 동향</span><span class="rule"></span><span class="rt jump" onclick="switchTab('strategy')">시장 탭 전체 →</span></div>
    <div class="cmd cmd-2">
      <div class="box"><div class="ph">가장 활발한 시장 <span class="c">수출 YoY</span></div><div class="mkl">{market_list_html}</div></div>
      <div class="box"><div class="ph">핵심 무브 <span class="c">기간 연동</span></div><div id="move-stream-wrap">{move_stream_html}</div></div>
    </div>

    <!-- 4) 주간 AI 종합 + 브랜드 신호 강도 -->
    <div class="eyebrow"><span class="lab">주간 종합</span><span class="rule"></span><span class="rt">최근 7일 AI 인사이트</span></div>
    <div id="synth-wrap">{synth_html}</div>
    <div class="section">
      <div class="section-title">브랜드 신호 요약
        <span class="section-sub">각 브랜드가 왜 강한지 — 기사·검색·매출·상표·리테일 실수치로 (클릭 시 상세)</span>
      </div>
      {brand_signals_html}
    </div>

    <!-- 5) 기회 스토리 (딥다이브 — 맨 아래) -->
    <div class="eyebrow"><span class="lab">Opportunity Stories</span><span class="rule"></span><span class="rt">나라·브랜드·무브·제품 → 우리가 할 것 (상세)</span></div>
    {stories_html}
    {legend_html}
  </div>

  <!-- ===== 탭: 경쟁사 ===== -->
  <div class="tab-panel" id="tab-brands">
    <!-- Brand Radar — 모멘텀 기반 티어 신호 -->
    <div class="section">
      <div class="section-title">
        Brand Radar
        <span class="section-sub">최근 4주 vs 직전 4주 기사량 비율 · ▲Rising / ▶Stable / ▼Cooling</span>
      </div>
      {radar_html}
    </div>

    <!-- 수요 검증 — 뉴스(공급/PR) vs 네이버 검색(수요) 삼각검증 -->
    <div class="section">
      <div class="section-title">
        📡 수요 검증 <span class="section-sub">보도량(공급) vs 네이버 검색량(수요) 대조 — "진짜 무브인가, PR 노이즈인가"</span>
      </div>
      {demand_html}
    </div>

    <!-- 글로벌 검색 급등 (구글 트렌드) — 네이버(국내) 보완 -->
    <div class="section">
      <div class="section-title">
        🔺 글로벌 검색 급등 <span class="section-sub">구글 트렌드 글로벌·미국·일본 — 최근7일 vs 직전28일 급증(네이버=국내 보완, 해외 수요 조기신호)</span>
      </div>
      {search_spikes_html}
    </div>

    <!-- 해외 상표 출원 (브랜드 선행신호 — 시장 탭에서 이동) -->
    <div class="section">
      <div class="section-title">
        🪧 해외 상표 출원 = 진출 선행신호 <span class="section-sub">경쟁사가 미국·일본에 낸 상표(자기출원·화장품류) — 뉴스보다 먼저 잡히는 진출·신제품 조짐</span>
      </div>
      {trademark_html}
    </div>

    <div class="section">
      <div class="section-title">브랜드별 HIGH 비중</div>
      {brand_high_html}
    </div>

    <div class="section">
      <div class="section-title">
        브랜드별 활동 유형 구성
        <span class="section-sub">전략 포지셔닝 비교</span>
      </div>
      {brand_act_html}
      <div class="legend-row" id="stacked-legend"></div>
    </div>

    <!-- Brand Insight Cards (Claude API 자동생성) -->
    <div class="section" id="insight-section">
      <div class="section-title">
        브랜드별 전략 인사이트
        <span class="section-sub">스택바 클릭 시 해당 브랜드로 이동</span>
      </div>
      <div class="insight-grid" id="insight-grid"></div>
    </div>
  </div>

  <!-- ===== 탭: 우리 관점 ===== -->
  <div class="tab-panel" id="tab-strategy">
    <!-- 글로벌 신호 지도 (브리핑에서 이동 — 공간 여유 있는 시장 탭 상단) -->
    {worldmap_section}

    {category_battle_html}

    <!-- 시장 성장 스토리 — 수출 성장(성과) x 그 시장 경쟁사 활동(뉴스) -->
    <div class="section">
      <div class="section-title">
        🔥 뜨는 시장, 왜 크는가 <span class="section-sub">관세청 실수출 성장(YoY) + 같은 시장에서 경쟁사가 한 진출·입점·마케팅 — 발표(뉴스)·성과(수출)를 한눈에 대조</span>
      </div>
      {growth_story_html}
    </div>

    <!-- 브랜드 × 국가 분포 (시장 관점 — 어느 시장에 경쟁이 몰리나) -->
    <div class="section">
      <div class="section-title">
        브랜드 × 국가 분포 <span class="section-sub">어느 시장에 경쟁이 집중되나 · 셀 클릭 시 HIGH/MED 기사 목록</span>
      </div>
      {heatmap_html}
    </div>

    <!-- 화장품 수출 규모·성장 전체 랭킹 (스킨케어 330499) -->
    <div class="section">
      <div class="section-title">
        🌍 화장품 수출 시장 랭킹 <span class="section-sub">관세청 실수출액(스킨케어·기초 HS 330499) 규모순 + YoY · 진출 우선순위 하드데이터</span>
      </div>
      {export_growth_html}
    </div>

    {expansion_playbook_html}

    <!-- 시장 종합 인사이트 + 셀퓨전씨 맞춤 조언 -->
    <div class="section" id="market-section">
      <div class="section-title">
        🧭 시장 종합 인사이트 &amp; 셀퓨전씨 전략 제언
        <span class="section-sub">전 경쟁사 종합 분석 → 우리(씨엠에스랩) 관점 조언</span>
      </div>
      <div class="market-body" id="market-body"></div>
    </div>
  </div>

  <!-- ===== 탭: 재무 (NICE BizLine · 연 단위 · 비상장 포함) ===== -->
  <div class="tab-panel" id="tab-finance">
    <div class="section">
      <div class="section-title">
        💰 재무 현황 <span class="section-sub">NICE BizLine 산업경쟁현황 · 매출·영업이익·광고비 (2023~2025, 연 단위) · 상장·비상장 통합</span>
      </div>
      {financials_nice_html}
    </div>
  </div>

  <!-- ===== 탭: 검색 (MCP+챗봇 자연어 질의) ===== -->
  <div class="tab-panel" id="tab-search">
    <div class="section">
      <div class="section-title">기간·브랜드·국가 검색
        <span class="section-sub">자연어로 물어보면 데이터(뉴스·수출·재무·상표·검색·리테일)를 조회해 답합니다</span>
      </div>
      <div class="search-examples">
        <button class="se-chip" onclick="askExample(this)">아누아가 최근 미국에서 뭐 했어?</button>
        <button class="se-chip" onclick="askExample(this)">최근 30일 브라질에서 활발한 브랜드는?</button>
        <button class="se-chip" onclick="askExample(this)">토리든 재무 실적 알려줘</button>
        <button class="se-chip" onclick="askExample(this)">조선미녀가 유럽에서 한 활동은?</button>
      </div>
      <div class="search-box">
        <input id="search-q" class="search-input" placeholder="예: 스킨1004가 일본에서 최근 뭐 하고 있어?" onkeydown="if(event.key==='Enter')runSearch()"/>
        <button class="search-send" onclick="runSearch()">검색</button>
      </div>
      <div id="search-chat" class="search-chat"></div>
    </div>
  </div>
  <script>
  function askExample(el){{ document.getElementById('search-q').value=el.textContent; runSearch(); }}
  async function runSearch(){{
    var inp=document.getElementById('search-q'); var q=(inp.value||'').trim(); if(!q) return;
    var chat=document.getElementById('search-chat');
    var id='m'+Date.now();
    var qe=q.replace(/&/g,'&amp;').replace(/</g,'&lt;');
    chat.insertAdjacentHTML('afterbegin','<div class="sc-pair"><div class="sc-q">'+qe+'</div><div class="sc-a" id="'+id+'">답변 생성 중…</div></div>');
    inp.value='';
    try{{
      var r=await fetch('/api/ask',{{method:'POST',headers:{{'Content-Type':'application/json'}},body:JSON.stringify({{question:q}})}});
      var d=await r.json();
      var ans=(d.answer||'답을 찾지 못했어요.').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/\\n/g,'<br>');
      document.getElementById(id).innerHTML=ans;
    }}catch(e){{ document.getElementById(id).innerHTML='오류: '+e; }}
  }}
  </script>

  <script>
  window.rebuildMoveStream = function(){{
    var wrap=document.getElementById('move-stream-wrap');
    if(!wrap || !window.HIGH_DATA) return;
    var arr=HIGH_DATA.slice().sort(function(a,b){{return (b.score||0)-(a.score||0);}});
    var seen={{}}, rows=[], n=0;
    for(var i=0;i<arr.length && n<7;i++){{
      var a=arr[i];
      if(a.imp!=='high' && a.imp!=='medium') continue;
      var k=(a.brand||'')+'|'+(a.country||'')+'|'+(a.act||'');
      if(seen[k]) continue; seen[k]=1;
      var dot=(a.imp==='high')?'dot-h':'dot-m';
      var t=String(a.title||'').slice(0,76).replace(/&/g,'&amp;').replace(/</g,'&lt;');
      var ac=String(a.act||'').replace(/</g,'&lt;');
      var bb=String(a.brand||'').replace(/"/g,'');
      rows.push('<div class="ev" data-b="'+bb+'" data-c="'+(a.country||'')+'"><div class="m"><span class="'+dot+'">●</span>'+(a.country||'')+' · '+ac+'</div><div class="t">'+t+'</div></div>');
      n++;
    }}
    wrap.innerHTML='<div class="stream">'+(rows.join('')||'<div class="ev"><div class="t" style="color:var(--lo)">이 기간 핵심 무브 없음</div></div>')+'</div>';
    wrap.onclick=function(e){{ var el=e.target.closest('.ev'); if(el && el.dataset.b) openHeatmapDrilldown(el.dataset.b, el.dataset.c||'all','all'); }};
  }};
  </script>

  <!-- ===== 탭: 기록 ===== -->
  <div class="tab-panel" id="tab-feed">
    <div class="section">
      <div class="section-title">
        HIGH/MED 기사 목록
        <span class="section-sub" id="high-count-label">{len(high_articles)}건</span>
        <button class="collapse-btn" id="articles-toggle" onclick="toggleArticlesSection()">▲ 접기</button>
      </div>
      <div id="articles-content">
        {filter_bar_html}
        {high_html}
      </div>
    </div>

    {briefing_archive_html}

    <div class="chart-section">
      <div class="section-title">주별 수집 트렌드<span class="section-sub">전체 브랜드 주간 수집량 추이</span></div>
      {trend_html}
    </div>
  </div>

</div>

<!-- Drilldown panel -->
<div class="dd-overlay" id="dd-overlay" onclick="closeDrilldown()"></div>
<div class="dd-panel" id="dd-panel">
  <div class="dd-header">
    <div><h3 id="dd-title">—</h3><p id="dd-subtitle">HIGH importance 기사</p></div>
    <button class="dd-close" onclick="closeDrilldown()">✕</button>
  </div>
  <div class="dd-summary" id="dd-summary" style="display:none;"></div>
  <div class="dd-body" id="dd-body"></div>
</div>

<script>
// ── Period data (client-side switching) ──
var PERIOD_DATA = {period_data_json};
var _currentPeriod = {days};

// HIGH articles for current period (drilldown)
var HIGH_DATA = {high_data_json};

function escH(s) {{
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}}

var _FLAGS2 = {{US:'🇺🇸',JP:'🇯🇵',KR:'🇰🇷',CN:'🇨🇳',GB:'🇬🇧',PL:'🇵🇱',
               SG:'🇸🇬',TH:'🇹🇭',CA:'🇨🇦',AU:'🇦🇺',DE:'🇩🇪',FR:'🇫🇷',
               ID:'🇮🇩',MY:'🇲🇾',VN:'🇻🇳',PH:'🇵🇭',IT:'🇮🇹'}};

function renderArticlesTable(arts) {{
  var tbody = document.getElementById('articles-tbody');
  if (!tbody) return;
  if (!arts || !arts.length) {{
    tbody.innerHTML = '<tr><td colspan="7" style="padding:20px;color:#a0aec0;font-style:italic">HIGH/MEDIUM 기사 없음</td></tr>';
    var lbl = document.getElementById('high-count-label');
    if (lbl) lbl.textContent = '0건 — 행 클릭 시 상세 펼침';
    return;
  }}
  var rows = '';
  arts.forEach(function(a, i) {{
    var flag = _FLAGS2[a.country] || '🌐';
    var impB = a.imp === 'high'
      ? '<span class="imp-badge imp-high">HIGH</span>'
      : '<span class="imp-badge imp-med">MED</span>';
    var urlCell = a.url ? '<a href="' + escH(a.url) + '" target="_blank" onclick="event.stopPropagation()">원문↗</a>' : '';
    var t = String(a.title||''); if(t.length > 160) t = t.substring(0,160)+'…';
    rows += '<tr class="main-row" data-brand="' + escH(a.brand) + '" data-act="' + escH(a.act) + '" onclick="toggleRow(' + i + ')">'
      + '<td class="date-cell">' + escH(a.date) + '</td>'
      + '<td>' + impB + ' <span class="brand-tag">' + escH(a.brand) + '</span></td>'
      + '<td class="flag-cell">' + flag + ' ' + escH(a.country) + '</td>'
      + '<td><span class="act-tag">' + escH(a.act) + '</span></td>'
      + '<td class="title-cell">' + escH(t) + '</td>'
      + '<td class="conf-cell">' + escH(a.conf||'') + '</td>'
      + '<td>' + urlCell + '</td>'
      + '</tr>'
      + '<tr id="dr-' + i + '" class="detail-row hidden"><td colspan="7">'
      + '<div class="detail-box">'
      + '<p><strong>요약(한):</strong> ' + escH(a.details||'') + '</p>'
      + (a.source ? '<p class="src-info">출처: ' + escH(a.source) + '</p>' : '')
      + '</div></td></tr>';
  }});
  tbody.innerHTML = rows;
  applyFilter();
}}

function _setDelta(elId, cur, prev) {{
  var el = document.getElementById(elId);
  if (!el) return;
  cur = cur || 0; prev = prev || 0;
  if (!prev) {{ el.innerHTML = '<div class="d neu"><span class="cap">직전 동기 데이터 없음</span></div>'; return; }}
  var pct = (cur - prev) / prev * 100;
  var cls = 'neu', arw = '–';
  if (pct >= 0.5) {{ cls = 'pos'; arw = '▲'; }}
  else if (pct <= -0.5) {{ cls = 'neg'; arw = '▼'; }}
  el.innerHTML = '<div class="d ' + cls + '">' + arw + ' ' + Math.abs(pct).toFixed(0) +
                 '%<span class="cap">vs 직전 동기</span></div>';
}}
function _sparkSvg(series) {{
  series = series || [];
  if (series.length < 2) return '';
  var w = 96, h = 20, n = series.length;
  var lo = Math.min.apply(null, series), hi = Math.max.apply(null, series);
  var rng = (hi - lo) || 1;
  function pt(i, v) {{
    var x = i / (n - 1) * w;
    var y = h - ((v - lo) / rng) * (h - 2) - 1;
    return x.toFixed(1) + ',' + y.toFixed(1);
  }}
  var pts = []; for (var i = 0; i < n; i++) pts.push(pt(i, series[i]));
  var line = 'M' + pts.join(' L');
  var area = 'M0,' + h + ' L' + pts.join(' L') + ' L' + w + ',' + h + ' Z';
  var last = pt(n - 1, series[n - 1]).split(',');
  return '<svg class="spark" viewBox="0 0 ' + w + ' ' + h + '" preserveAspectRatio="none" aria-hidden="true">' +
         '<path class="fill" d="' + area + '"/><path d="' + line + '"/>' +
         '<circle class="end" cx="' + last[0] + '" cy="' + last[1] + '" r="1.7"/></svg>';
}}
function setPeriod(days) {{
  var key = String(days);
  var d = PERIOD_DATA[key];
  var msgEl = document.getElementById('period-msg');
  if (!d) {{
    if (msgEl) {{
      msgEl.style.display = '';
      msgEl.textContent = days + '일 데이터가 없습니다. 재생성: python cli.py report --days ' + days;
    }}
    return;
  }}
  if (msgEl) msgEl.style.display = 'none';
  _currentPeriod = days;

  // Button active state
  document.querySelectorAll('.period-btn').forEach(function(b) {{
    b.classList.toggle('active', +b.dataset.days === days);
  }});

  // Update date pickers
  _initDatePicker(days);

  // Header label
  var lbl = document.getElementById('period-label');
  if (lbl) lbl.textContent = days;

  // KPI cards
  var k = d.kpi;
  var totEl = document.getElementById('kpi-total');   if (totEl) totEl.textContent = (k.total||0).toLocaleString();
  var hiEl  = document.getElementById('kpi-high');    if (hiEl)  hiEl.textContent  = (k.high||0).toLocaleString();
  var brEl  = document.getElementById('kpi-brands');  if (brEl)  brEl.textContent  = (k.brands||0).toLocaleString();
  var coEl  = document.getElementById('kpi-countries'); if (coEl) coEl.textContent = (k.countries||0).toLocaleString();
  _setDelta('kpi-d-total', k.total, k.prev_total);
  _setDelta('kpi-d-high', k.high, k.prev_high);
  _setDelta('kpi-d-brands', k.brands, k.prev_brands);
  _setDelta('kpi-d-countries', k.countries, k.prev_countries);
  var spEl = document.getElementById('kpi-spark'); if (spEl) spEl.innerHTML = _sparkSvg(k.spark);

  // Articles table + drilldown data
  HIGH_DATA = d.articles;
  renderArticlesTable(d.articles);
  if (window.rebuildMoveStream) window.rebuildMoveStream();

  // Reset filters + collapse
  _fBrand = 'all'; _fAct = 'all';
  document.querySelectorAll('#brand-filters .filter-pill').forEach(function(p) {{
    p.classList.toggle('active', p.dataset.brand === 'all');
  }});
  document.querySelectorAll('#act-filters .filter-pill').forEach(function(p) {{
    p.classList.remove('active', 'act-active');
    if (p.dataset.act === 'all') p.classList.add('active');
  }});

  // World map
  if (window._wmSetStats) window._wmSetStats(d.country_stats);

  // Insight cards + 시장 종합
  if (d.insights && window._renderInsights) window._renderInsights(d.insights);
  if (window._renderMarket) window._renderMarket(d.market || '');
}}

// ── Date picker helpers ──
function _isoDate(d) {{
  return d.getFullYear() + '-' +
    String(d.getMonth()+1).padStart(2,'0') + '-' +
    String(d.getDate()).padStart(2,'0');
}}
function _initDatePicker(days) {{
  var today = new Date(); today.setHours(0,0,0,0);
  var from  = new Date(today.getTime() - days * 86400000);
  var fEl   = document.getElementById('from-date');
  var tEl   = document.getElementById('to-date');
  if (fEl) fEl.value = _isoDate(from);
  if (tEl) tEl.value = _isoDate(today);
}}
var BASE_ARTICLES = (PERIOD_DATA['90'] && PERIOD_DATA['90'].articles) ? PERIOD_DATA['90'].articles : HIGH_DATA;

function applyDateRange() {{
  var fEl   = document.getElementById('from-date');
  var tEl   = document.getElementById('to-date');
  var msgEl = document.getElementById('period-msg');
  if (!fEl || !fEl.value || !tEl || !tEl.value) return;
  var fromStr = fEl.value, toStr = tEl.value;
  if (fromStr > toStr) {{
    if (msgEl) {{ msgEl.style.display=''; msgEl.textContent='시작일이 종료일보다 늦습니다.'; }}
    return;
  }}
  if (msgEl) {{ msgEl.style.display = ''; msgEl.textContent = '구간 조회 중…'; }}
  document.querySelectorAll('.period-btn').forEach(function(b) {{ b.classList.remove('active'); }});
  var lbl = document.getElementById('period-label'); if (lbl) lbl.textContent = fromStr + ' ~ ' + toStr;

  // 서버 조회 — 임의 구간(과거 포함, 90일 제한 없음). DB에서 KPI·기사·synth 계산.
  fetch('/api/period?from=' + encodeURIComponent(fromStr) + '&to=' + encodeURIComponent(toStr))
    .then(function(r) {{ return r.json(); }})
    .then(function(d) {{
      if (d.error) {{ if (msgEl) {{ msgEl.style.display=''; msgEl.textContent=d.error; }} return; }}
      if (msgEl) msgEl.style.display = 'none';
      var arts = d.articles || [];
      HIGH_DATA = arts; renderArticlesTable(arts);
      if (window.rebuildMoveStream) window.rebuildMoveStream();
      var s = d.stats || {{}};
      var set = function(id, v) {{ var e=document.getElementById(id); if(e) e.textContent=(v||0).toLocaleString(); }};
      set('kpi-total', s.total); set('kpi-high', s.high);
      set('kpi-brands', s.brands_active); set('kpi-countries', s.countries_active);
      ['kpi-d-total','kpi-d-high','kpi-d-brands','kpi-d-countries'].forEach(function(id) {{
        var de=document.getElementById(id); if(de) de.innerHTML='<div class="d neu"><span class="cap">사용자 지정 구간</span></div>';
      }});
      var byDay={{}}; arts.forEach(function(a){{ if(a.date) byDay[a.date]=(byDay[a.date]||0)+1; }});
      var ds=Object.keys(byDay).sort();
      var spEl=document.getElementById('kpi-spark'); if(spEl) spEl.innerHTML=_sparkSvg(ds.map(function(x){{return byDay[x];}}));
      var cStats={{}}; arts.forEach(function(a){{ if(!cStats[a.country])cStats[a.country]={{total:0,high:0,medium:0}}; cStats[a.country].total++; if(a.imp==='high')cStats[a.country].high++; else cStats[a.country].medium++; }});
      if(window._wmSetStats) window._wmSetStats(cStats);
      if(d.synth_html){{ var sw=document.getElementById('synth-wrap'); if(sw) sw.innerHTML=d.synth_html; }}
      _fetchInsights(fromStr, toStr);
    }})
    .catch(function(e) {{ if (msgEl) {{ msgEl.style.display=''; msgEl.textContent='구간 조회 실패: '+e; }} }});
}}

function _fetchInsights(fromStr, toStr) {{
  var grid = document.getElementById('insight-grid');
  if (!grid) return;
  grid.innerHTML = '<div style="padding:32px;text-align:center;color:#9ca3af;font-size:13px;">인사이트 생성 중...</div>';
  fetch('/api/insights?from_date=' + encodeURIComponent(fromStr) + '&to_date=' + encodeURIComponent(toStr))
    .then(function(r) {{ return r.json(); }})
    .then(function(data) {{
      if (window._renderInsights) window._renderInsights(data);
    }})
    .catch(function() {{
      grid.innerHTML = '<div style="padding:32px;text-align:center;color:#dc2626;font-size:13px;">인사이트 로드 실패 — 서버 연결 확인</div>';
    }});
}}

var COLLAPSE_LIMIT = 10;
var _articlesCollapsed = true;
function toggleArticlesSection() {{
  _articlesCollapsed = !_articlesCollapsed;
  _applyCollapseAndFilter();
}}

function toggleStrat(id) {{
  var el = document.getElementById('strat-' + id);
  if (!el) return;
  var exp = el.classList.toggle('expanded');
  el.classList.toggle('clamp', !exp);
  var btn = el.nextElementSibling;
  if (btn && btn.classList.contains('insight-more')) btn.textContent = exp ? '접기 ▴' : '더보기 ▾';
}}
function switchTab(name) {{
  document.querySelectorAll('.tab-btn').forEach(function(b) {{
    b.classList.toggle('active', b.dataset.tab === name);
  }});
  document.querySelectorAll('.tab-panel').forEach(function(p) {{
    p.classList.toggle('active', p.id === 'tab-' + name);
  }});
  // 숨겨진 탭에서 width=0으로 그려진 캔버스 차트 재그리기
  if (name === 'brands' && window._drawStacked) {{ window._drawStacked(); }}
  try {{ window.dispatchEvent(new Event('resize')); }} catch (e) {{}}
  window.scrollTo({{ top: 0, behavior: 'smooth' }});
}}

function toggleRow(i) {{
  var row = document.getElementById('dr-' + i);
  if (row) row.classList.toggle('hidden');
}}

// ── Heatmap drilldown ──
var _CN2 = {{US:'미국',JP:'일본',KR:'한국',CN:'중국',PL:'폴란드',SG:'싱가포르',TH:'태국',GB:'영국',CA:'캐나다',AU:'호주',DE:'독일',FR:'프랑스',ID:'인도네시아',MY:'말레이시아',VN:'베트남',PH:'필리핀',IT:'이탈리아',BR:'브라질',MX:'멕시코',IN:'인도',AE:'UAE',SA:'사우디',ZA:'남아공',RU:'러시아',KZ:'카자흐스탄',UZ:'우즈베키스탄',BY:'벨라루스'}};

function _currentRange() {{
  var fEl = document.getElementById('from-date');
  var tEl = document.getElementById('to-date');
  if (fEl && fEl.value && tEl && tEl.value) return {{from: fEl.value, to: tEl.value}};
  var today = new Date(); today.setHours(0,0,0,0);
  var from = new Date(today.getTime() - (_currentPeriod || 30) * 86400000);
  return {{from: _isoDate(from), to: _isoDate(today)}};
}}

var _ACT_META = {{
  '유통_채널':{{l:'유통 채널',c:'#4a8fd4'}},
  '신시장_진출':{{l:'신시장 진출',c:'#9b7fe8'}},
  '신제품_런칭':{{l:'신제품 런칭',c:'#4ab884'}},
  '인플루언서_협업':{{l:'인플루언서',c:'#8b95ff'}},
  '투자_BD':{{l:'투자·BD',c:'#e05353'}},
  '브랜드_마케팅':{{l:'브랜드 마케팅',c:'#e0894a'}},
  '실적_공시':{{l:'실적·공시',c:'#46b0b0'}},
  '가격_프로모션':{{l:'가격·프로모션',c:'#d64f8f'}},
  '기타':{{l:'기타',c:'#6f7aa0'}}
}};

function _renderActChips(acts) {{
  if (!acts || !acts.length) return '';
  var chips = acts.map(function(a) {{
    var m = _ACT_META[a.act] || {{l:a.act, c:'#6f7aa0'}};
    return '<span class="dd-act-focus" style="border-color:' + m.c + '55;color:' + m.c + '">'
      + m.l + ' <b>' + a.pct + '%</b></span>';
  }}).join('');
  return '<div class="dd-act-row"><div class="dd-act-row-h">주력 활동</div><div class="dd-act-chips">' + chips + '</div></div>';
}}

// 마크다운 인라인 변환: **굵게** → <strong>, 앞머리 '- '·'1.' 불릿 정리
function _mdBold(s) {{
  return String(s || '').replace(/\\*\\*([^*]+?)\\*\\*/g, '<strong>$1</strong>');
}}

function _renderSummarySections(raw) {{
  // 서버가 html-escape한 '### 라벨\\n본문' 텍스트를 소제목 블록으로 분할
  var parts = raw.split(/###\\s+/).filter(function(s) {{ return s.trim(); }});
  if (parts.length === 0) return '<div class="dd-sum-body">' + _mdBold(raw) + '</div>';
  return parts.map(function(chunk) {{
    var nl = chunk.indexOf('\\n');
    var label = nl === -1 ? chunk.trim() : chunk.slice(0, nl).trim();
    var body  = nl === -1 ? '' : chunk.slice(nl + 1).trim();
    body = _mdBold(body).replace(/\\n/g, '<br>');
    return '<div class="dd-sum-sec"><div class="dd-sum-sec-h">' + _mdBold(label) + '</div>'
      + '<div class="dd-sum-sec-b">' + body + '</div></div>';
  }}).join('');
}}

var _ddToken = 0;
function _renderCellSummary(brand, country) {{
  var sumEl = document.getElementById('dd-summary');
  if (!sumEl) return;
  if (brand === 'all' || country === 'all') {{ sumEl.style.display = 'none'; sumEl.innerHTML = ''; return; }}
  var myToken = ++_ddToken;
  sumEl.style.display = '';
  sumEl.innerHTML = '<div class="dd-sum-label">전략 인사이트</div>'
    + '<div class="dd-sum-body"><span class="dd-sum-spin"></span>분석 생성 중… (최초 1회 수 초 소요)</div>';
  var rng = _currentRange();
  fetch('/api/cell-insight?brand=' + encodeURIComponent(brand)
        + '&country=' + encodeURIComponent(country)
        + '&from_date=' + encodeURIComponent(rng.from)
        + '&to_date=' + encodeURIComponent(rng.to))
    .then(function(r) {{ return r.json(); }})
    .then(function(data) {{
      if (myToken !== _ddToken) return;  // 다른 셀로 이동함 — 무시
      if (data && (data.summary || (data.activities && data.activities.length))) {{
        sumEl.innerHTML = '<div class="dd-sum-label">전략 인사이트</div>'
          + _renderActChips(data.activities)
          + _renderSummarySections(data.summary || '');
      }} else {{
        sumEl.style.display = 'none';
      }}
    }})
    .catch(function() {{
      if (myToken !== _ddToken) return;
      sumEl.style.display = 'none';
    }});
}}

function openHeatmapDrilldown(brand, country, total) {{
  var _CN = _CN2;
  var arts = HIGH_DATA.filter(function(a) {{ return (brand === 'all' || a.brand === brand) && (country === 'all' || a.country === country); }});
  document.getElementById('dd-title').textContent = brand === 'all' ? (_CN[country] || country) + ' 전체' : brand + ' · ' + (country === 'all' ? '전체 시장' : (_CN[country] || country));
  var highCount = arts.filter(function(a){{ return a.imp === 'high'; }}).length;
  var medCount  = arts.length - highCount;
  var countText = (typeof total === 'number' ? '전체 ' + total + '건 · ' : '') + 'HIGH ' + highCount + '건' + (medCount > 0 ? ' · MED ' + medCount + '건' : '');
  document.getElementById('dd-subtitle').textContent = countText;
  _renderCellSummary(brand, country);
  var body = document.getElementById('dd-body');
  if (!arts.length) {{
    body.innerHTML = '<div class="dd-empty">이 셀에 HIGH/MEDIUM 기사 없음</div>';
  }} else {{
    body.innerHTML = arts.map(function(a) {{
      var link = a.url ? '<a class="dd-link" href="' + a.url + '" target="_blank" rel="noopener">원문 보기 ↗</a>' : '';
      var badge = a.imp === 'high'
        ? '<span style="background:rgba(239,83,83,0.15);color:#d0322b;padding:1px 6px;border-radius:3px;font-size:12.5px;font-weight:700;margin-right:5px">HIGH</span>'
        : '<span style="background:rgba(224,160,64,0.18);color:#a86a12;padding:1px 6px;border-radius:3px;font-size:12.5px;font-weight:700;margin-right:5px">MED</span>';
      var scoreBadge = a.score ? '<span style="background:rgba(200,169,110,0.16);color:var(--gold);padding:1px 6px;border-radius:3px;font-size:12.5px;font-weight:700;margin-left:auto">' + a.score + '점</span>' : '';
      var meta = [];
      if (a.channel) meta.push('🏪 ' + a.channel);
      if (a.city) meta.push('📍 ' + a.city);
      if (a.price) meta.push('💰 ' + a.price);
      if (a.evidence) meta.push(a.evidence);
      var metaLine = meta.length ? '<div style="font-size:11.5px;color:var(--lo);margin-top:5px;">' + meta.join(' · ') + '</div>' : '';
      return '<div class="dd-item">'
        + '<div class="dd-item-top"><span class="dd-date">' + a.date + '</span>'
        + badge + '<span class="dd-act-chip">' + a.act + '</span>' + scoreBadge + '</div>'
        + '<div class="dd-title">' + a.title + '</div>'
        + (a.details ? '<div style="font-size:13px;color:var(--mid);margin-top:4px;line-height:1.5;">' + a.details + '</div>' : '')
        + metaLine + link + '</div>';
    }}).join('');
  }}
  document.getElementById('dd-panel').classList.add('open');
  document.getElementById('dd-overlay').style.display = 'block';
}}

function closeDrilldown() {{
  document.getElementById('dd-panel').classList.remove('open');
  document.getElementById('dd-overlay').style.display = 'none';
}}

// ── Filter + Collapse ──
var _fBrand = 'all', _fAct = 'all';

function _applyCollapseAndFilter() {{
  var tbody = document.getElementById('articles-tbody');
  if (!tbody) return;
  var mainRows = Array.from(tbody.querySelectorAll('tr.main-row'));
  var shown = 0, total = 0;
  mainRows.forEach(function(tr, idx) {{
    var filterOk = tr._filterVisible !== false;
    if (filterOk) total++;
    var show = filterOk && (!_articlesCollapsed || shown < COLLAPSE_LIMIT);
    if (show) shown++;
    tr.style.display = show ? '' : 'none';
    var detailRow = document.getElementById('dr-' + idx);
    if (detailRow) detailRow.style.display = (show && !detailRow.classList.contains('hidden')) ? '' : 'none';
  }});
  var suffix = (_fBrand !== 'all' || _fAct !== 'all') ? ' (필터됨)' : '';
  var countText = _articlesCollapsed ? (shown + '/' + total + '건') : (total + '건');
  var lbl = document.getElementById('high-count-label');
  if (lbl) lbl.textContent = countText + suffix;
  var btn = document.getElementById('articles-toggle');
  if (btn) btn.textContent = _articlesCollapsed ? ('▼ 전체보기 (+' + (total - shown) + '건)') : '▲ 접기';
}}

function applyFilter() {{
  var tbody = document.getElementById('articles-tbody');
  if (!tbody) return;
  tbody.querySelectorAll('tr.main-row').forEach(function(tr) {{
    tr._filterVisible = (_fBrand === 'all' || tr.dataset.brand === _fBrand)
                     && (_fAct   === 'all' || tr.dataset.act   === _fAct);
  }});
  _applyCollapseAndFilter();
  document.querySelectorAll('.heatmap-table tbody tr').forEach(function(tr) {{
    var bc = tr.querySelector('.brand-name');
    if (!bc) return;
    tr.style.opacity = (_fBrand === 'all' || bc.textContent.trim() === _fBrand || bc.textContent === '합계') ? '1' : '0.35';
  }});
}}
document.addEventListener('DOMContentLoaded', function() {{
  // Init collapsed state
  _applyCollapseAndFilter();
  // Init date pickers
  _initDatePicker(_currentPeriod);
  // from-date / to-date: apply on Enter key
  ['from-date','to-date'].forEach(function(id) {{
    var el = document.getElementById(id);
    if (el) el.addEventListener('keydown', function(e) {{ if(e.key==='Enter') applyDateRange(); }});
  }});

  var bf = document.getElementById('brand-filters');
  if (bf) bf.addEventListener('click', function(e) {{
    var pill = e.target.closest('.filter-pill');
    if (!pill) return;
    bf.querySelectorAll('.filter-pill').forEach(function(p) {{ p.classList.remove('active'); }});
    pill.classList.add('active');
    _fBrand = pill.dataset.brand;
    applyFilter();
  }});
  var af = document.getElementById('act-filters');
  if (af) af.addEventListener('click', function(e) {{
    var pill = e.target.closest('.filter-pill');
    if (!pill) return;
    af.querySelectorAll('.filter-pill').forEach(function(p) {{ p.classList.remove('active', 'act-active'); }});
    pill.classList.add(pill.dataset.act === 'all' ? 'active' : 'act-active');
    _fAct = pill.dataset.act;
    applyFilter();
  }});
}});

{chart_scripts}
{stacked_script}
{market_script}
{insights_script}
{worldmap_script}
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# 공개 API
# ---------------------------------------------------------------------------

def build_period_payload(from_date: str, to_date: str) -> dict:
    """임의 구간(from~to, 과거 포함) 브리핑 데이터 서버 조회 — /api/period용.

    반환 {stats:{total,high,brands,countries}, articles:[js], synth_html}.
    클라 90일 캐시로 못 보는 과거 구간(예: 26년 1분기)을 DB에서 직접 조회.
    """
    from storage.models import get_session
    session = get_session()
    try:
        stats = get_collection_stats_range(session, from_date, to_date)
        arts = get_high_articles(session, from_date=from_date, to_date=to_date)
        arts_js = [_fmt_art_for_js(a) for a in arts]
        # 구간 synth (범위 인사이트 → 시장종합 LLM → 렌더)
        market_text = ""
        try:
            raw = get_brand_insights_raw_by_range(session, from_date, to_date)
            if raw:
                market_text = generate_market_overview(raw)
        except Exception as e:
            logger.warning("구간 synth 실패: %s", e)
        try:
            growth = get_market_growth_story(session)
        except Exception:
            growth = {}
        try:
            composite = get_brand_composite_score(session)
        except Exception:
            composite = []
        stats_kpi = {"total": stats["total"], "high": stats["high"],
                     "brands_active": stats["brands_active"], "countries_active": stats["countries_active"]}
        synth_html = _render_synth(stats_kpi, market_text, growth, composite)
        return {"stats": stats, "articles": arts_js, "synth_html": synth_html,
                "from": from_date, "to": to_date}
    finally:
        session.close()


def generate_report(output_path: str = "rival_report.html", days: int = 30) -> str:
    """
    DB 조회 → self-contained HTML 대시보드 생성.

    Returns:
        생성된 파일의 절대 경로
    """
    session = get_session()
    try:
        stats         = get_collection_stats(session, days=days)
        matrix        = get_brand_country_matrix(session, days=days)
        trend         = get_weekly_trend(session, weeks=12)
        distribution  = get_activity_distribution(session, days=days)
        brand_act     = get_brand_activity_matrix(session, days=days)
        brand_high    = get_brand_high_ratio(session, days=days)
        insights_raw  = get_brand_insights_raw(session, days=days)
        country_stats = get_country_signal_stats(session, days=days)
        category_battle = get_category_battle(session, days=days)
        # 해외 진출 플레이북은 진입 이벤트가 드물어 윈도우를 넓게(최소 90일) 잡아 밀도 확보
        expansion_playbook = get_expansion_playbook(session, days=max(days, 90))
        briefing_archive = get_briefings_list(session, limit=24)
        try:
            brand_radar = get_brand_radar(session)
        except Exception:
            brand_radar = []
        # 수요 검증(뉴스 vs 네이버 검색 트렌드) — search_trends 없으면 빈 리스트
        try:
            demand_tri = get_demand_triangulation(session)
        except Exception:
            demand_tri = []
        # 화장품 수출 성장(관세청, 스킨케어 330499) — export_stats 없으면 빈 리스트
        try:
            export_growth = get_market_export_growth(session, hs_like="330499", trailing=3)
        except Exception:
            export_growth = []
        # 시장 성장 스토리(수출 YoY x 그 시장 경쟁사 활동) — 삼각검증 통합 뷰
        try:
            growth_story = get_market_growth_story(session)
        except Exception:
            growth_story = {"overall": None, "markets": []}
        # 경쟁사 실적(DART) — competitor_financials 없으면 빈 리스트
        try:
            financials = get_competitor_financials(session)
        except Exception:
            financials = []
        # NICE BizLine 재무(비상장 포함, 연 단위) — 재무 탭
        try:
            nice_financials = get_nice_financials(session)
        except Exception:
            nice_financials = []
        # 해외 상표 출원 선행신호(KIPRIS) — trademark_filings 없으면 빈 dict
        try:
            trademark_sig = get_trademark_signals(session)
        except Exception:
            trademark_sig = {"feed": [], "brands": []}
        # 글로벌 검색 급등(구글 트렌드) — google_trends 없으면 빈 리스트
        try:
            search_spikes = get_google_spikes(session)
        except Exception:
            search_spikes = []
        # 브랜드 종합 스코어(모멘텀·재무·상표·수요 통합) — 실패 시 빈 리스트
        try:
            composite = get_brand_composite_score(session)
        except Exception:
            composite = []
        # 브랜드 신호 요약(직관형 — 실수치 라벨) : #2 재설계
        try:
            brand_signals = get_brand_signal_summary(session, limit=12)
        except Exception:
            brand_signals = []
        # 핵심 서사 '기회 스토리' 합성 + AI 액션('우리가 할 것') — 실패해도 대시보드 무해
        try:
            stories = get_opportunity_stories(session, days=days, limit=6)
            if stories:
                _acts = generate_opportunity_actions(stories)
                for _s in stories:
                    _s["action"] = _acts.get(f"{_s.get('brand','')}|{_s.get('country','')}", "")
        except Exception as _e:
            logger.warning("기회 스토리 생성 실패: %s", _e)
            stories = []
        # 시계열 추세(주간 스냅샷 누적분) — 없으면 빈 dict(무해)
        try:
            from analytics.history import get_score_trend
            score_trend = get_score_trend(session)
        except Exception:
            score_trend = {}
        # 시장 인사이트 정량 근거: 브랜드 모멘텀(최근4주 속도) — 기간 무관 단일 계산
        try:
            market_momentum = compute_brand_momentum(session)
        except Exception:
            market_momentum = []

        # ── 개요 '이번 주 종합 요약' 전용 데이터 (기간 토글과 무관, 최근 7일) ──
        DG_DAYS = 7
        dg_stats = get_collection_stats(session, days=DG_DAYS)
        dg_cat   = get_category_battle(session, days=DG_DAYS)
        dg_raw   = get_brand_insights_raw(session, days=DG_DAYS)

        # 기간 선택기용 멀티 기간 데이터 (30/60/90일 + 현재 days)
        preset_periods = sorted(set([30, 60, 90, days]))
        _today = datetime.utcnow().date()
        max_period = max(preset_periods)

        # 기사 최대 기간(90일) 1회만 로드 — 작은 기간은 날짜 필터링으로 재사용
        # article_body(2000자 영문 원문)는 SELECT에서 제외 — OOM 방지 (LIMIT 400)
        _all_articles = get_high_articles(session, days=max_period)
        high_articles = [
            a for a in _all_articles
            if _fmt_date(a.get("published_date", "")) >= (_today - timedelta(days=days)).isoformat()
        ] if days < max_period else _all_articles

        # 다이제스트용 최근 7일 기사 (score순은 _all_articles 정렬 유지)
        _dg_cut = (_today - timedelta(days=DG_DAYS)).isoformat()
        dg_high = [a for a in _all_articles if _fmt_date(a.get("published_date", "")) >= _dg_cut]

        period_data: dict = {}
        period_insights_raw: dict = {}
        period_cache: dict = {}
        period_date_ranges: dict = {}
        period_cat_battle: dict = {}
        for p in preset_periods:
            p_cutoff_str = (_today - timedelta(days=p)).isoformat()
            p_arts = [
                a for a in _all_articles
                if _fmt_date(a.get("published_date", "")) >= p_cutoff_str
            ] if p < max_period else _all_articles
            p_stats  = get_collection_stats(session, days=p)
            p_cstats = get_country_signal_stats(session, days=p)
            period_insights_raw[p] = get_brand_insights_raw(session, days=p)
            period_cat_battle[p] = get_category_battle(session, days=p)
            _from = p_cutoff_str
            _to   = _today.isoformat()
            period_date_ranges[p]  = (_from, _to)
            # 정확 날짜 대신 기간 길이 기준 최근(7일) 캐시 재사용 → 매일 재생성 방지
            period_cache[p]        = get_insights_cache_by_period(session, p, max_age_days=7)
            period_data[p] = {
                "kpi": {
                    "total":     p_stats["total"],
                    "high":      p_stats["high"],
                    "brands":    p_stats["brands_active"],
                    "countries": p_stats["countries_active"],
                    "prev_total":      p_stats.get("prev_total", 0),
                    "prev_high":       p_stats.get("prev_high", 0),
                    "prev_brands":     p_stats.get("prev_brands_active", 0),
                    "prev_countries":  p_stats.get("prev_countries_active", 0),
                    "spark":           p_stats.get("spark", []),
                },
                "articles":      [_fmt_art_for_js(a) for a in p_arts],
                "country_stats": p_cstats,
            }
    finally:
        session.close()

    # 기간별 AI 인사이트 생성 (캐시 히트 → DB, 캐시 미스 → OpenAI API → DB 저장)
    insight_session = get_session()
    try:
        for p in preset_periods:
            p_raw    = period_insights_raw[p]
            cached_p = period_cache[p]
            p_brand_insights: dict = {}
            for brand, data in p_raw.items():
                if brand in cached_p and cached_p[brand].get("summary"):
                    summary = cached_p[brand]["summary"]
                else:
                    summary = generate_brand_strategy_summary(brand, data.get("articles", []))
                    _from, _to = period_date_ranges[p]
                    upsert_insight_cache(insight_session, brand, _from, _to, {
                        "summary":  summary,
                        "top_act":  data["top_act"],
                        "top_pct":  data["top_pct"],
                        "high_pct": data["high_pct"],
                    })
                p_brand_insights[brand] = {
                    "top_act":       data["top_act"],
                    "top_pct":       data["top_pct"],
                    "high_pct":      data["high_pct"],
                    "strategy":      summary,
                    "top_countries": data["top_countries"],
                    "key_articles":  data.get("articles", [])[:3],
                }
            period_data[p]["insights"] = p_brand_insights

            # 시장 종합 인사이트 + 셀퓨전씨 맞춤 조언 (__MARKET__ 센티넬로 캐시)
            m_cached = cached_p.get("__MARKET__", {}).get("summary")
            if m_cached:
                market = m_cached
            else:
                market = generate_market_overview(
                    p_raw, momentum=market_momentum,
                    category_battle=period_cat_battle.get(p),
                )
                if market:
                    _from, _to = period_date_ranges[p]
                    upsert_insight_cache(insight_session, "__MARKET__", _from, _to, {
                        "summary": market, "top_act": None, "top_pct": 0, "high_pct": 0.0,
                    })
            period_data[p]["market"] = market

        # ── 개요 다이제스트용 7일 시장 내러티브 (하루 1회 캐시) ──
        dg_market = get_digest_cache(insight_session, "__DIGEST7__")
        if not dg_market:
            try:
                dg_market = generate_market_overview(
                    dg_raw, momentum=market_momentum, category_battle=dg_cat)
            except Exception:
                dg_market = ""
            if dg_market:
                _dg_from = datetime.utcnow() - timedelta(days=DG_DAYS)
                _dg_to = datetime.utcnow()
                upsert_insight_cache(insight_session, "__DIGEST7__", _dg_from, _dg_to, {
                    "summary": dg_market, "top_act": None, "top_pct": 0, "high_pct": 0.0,
                })
    finally:
        insight_session.close()

    # 현재 기간 brand_insights (하위 호환용)
    brand_insights = period_data.get(days, {}).get("insights", {})

    chartjs_src  = _get_chartjs()
    html_content = _build_full_html(
        stats, high_articles, matrix, trend, distribution,
        brand_act, brand_high, brand_insights, chartjs_src, days,
        country_stats=country_stats,
        period_data=period_data,
        brand_radar=brand_radar,
        demand_tri=demand_tri,
        export_growth=export_growth,
        growth_story=growth_story,
        financials=financials,
        nice_financials=nice_financials,
        trademark_sig=trademark_sig,
        search_spikes=search_spikes,
        composite=composite,
        brand_signals=brand_signals,
        stories=stories,
        score_trend=score_trend,
        category_battle=category_battle,
        expansion_playbook=expansion_playbook,
        briefing_archive=briefing_archive,
        momentum=market_momentum,
        market_text=period_data.get(days, {}).get("market", ""),
        digest={
            "stats": dg_stats, "cat": dg_cat, "high": dg_high,
            "expansion": expansion_playbook, "market": dg_market,
            "ref_date": (datetime.utcnow() + timedelta(hours=9)).strftime("%-m/%-d")
                        if os.name != "nt" else (datetime.utcnow() + timedelta(hours=9)).strftime("%#m/%#d"),
        },
    )

    abs_path = os.path.abspath(output_path)
    with open(abs_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    logger.info("보고서 생성 완료: %s (%.1f KB)", abs_path, len(html_content) / 1024)
    return abs_path
