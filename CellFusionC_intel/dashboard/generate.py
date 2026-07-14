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
    get_high_articles,
    get_insights_cache,
    get_insights_cache_by_period,
    get_weekly_trend,
    upsert_insight_cache,
)
from analytics.summarizer import generate_brand_strategy_summary
from storage.models import get_session

logger = logging.getLogger(__name__)

CHARTJS_URL = "https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"
_CACHE_PATH = Path(__file__).parent / "chartjs_cache.min.js"

COUNTRY_FLAGS = {
    "US": "🇺🇸", "JP": "🇯🇵", "KR": "🇰🇷", "CN": "🇨🇳",
    "PL": "🇵🇱", "SG": "🇸🇬", "TH": "🇹🇭", "GB": "🇬🇧",
    "CA": "🇨🇦", "AU": "🇦🇺", "DE": "🇩🇪", "FR": "🇫🇷",
    "ID": "🇮🇩", "MY": "🇲🇾", "VN": "🇻🇳", "PH": "🇵🇭",
    "IT": "🇮🇹",
}

ACTIVITY_LABELS = {
    "신시장_진출":    "신시장 진출",
    "유통_채널":      "유통 채널",
    "신제품_런칭":    "신제품 런칭",
    "인플루언서_협업": "인플루언서 협업",
    "투자_BD":        "투자·BD",
    "브랜드_마케팅":  "브랜드 마케팅",
    "실적_공시":      "실적·공시",
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
    }


def _cell_color(value: int, max_value: int) -> str:
    """히트맵 셀 배경색. 0=빈 셀, max=골드, 가독성 우선."""
    if max_value == 0 or value == 0:
        return "background:#191e2e;color:#7a82a8;"
    norm = min(value / max_value, 1.0)
    # 딥블루 → 골드 그라데이션 (명확한 구분)
    r = int(28  + norm * (200 - 28))
    g = int(40  + norm * (169 - 40))
    b = int(65  + norm * (60  - 65))
    text = "#f0f2f8" if norm > 0.2 else "#a0aabb"
    return f"background:rgb({r},{g},{b});color:{text};"


# ---------------------------------------------------------------------------
# 섹션별 HTML 렌더러
# ---------------------------------------------------------------------------

def _render_kpi_cards(stats: dict) -> str:
    items = [
        ("총 수집",   stats["total"],             "건", "#c8a96e", "kpi-total"),
        ("HIGH",     stats["high"],              "건", "#e05353", "kpi-high"),
        ("활성 브랜드", stats["brands_active"],   "개", "#4a8fd4", "kpi-brands"),
        ("커버 국가",  stats["countries_active"], "개", "#aab3cc", "kpi-countries"),
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
                f' &nbsp;<span style="color:var(--gold);font-size:10px">↗ 크로스마켓 '
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
    SIGNAL_COLOR = {"rising": "#4ab884", "stable": "#aab3cc", "cooling": "#e05353"}
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
                "투자_BD", "브랜드_마케팅", "실적_공시", "기타"]
    act_labels_list = [ACTIVITY_LABELS.get(k, k) for k in act_keys]
    act_colors = ["#4a8fd4", "#9b7fe8", "#4ab884", "#c8a96e",
                  "#e05353", "#e0894a", "#46b0b0", "#6f7aa0"]

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
  var canvas = document.getElementById('stackedBar');
  if (!canvas) return;
  var dpr = window.devicePixelRatio || 1;
  var BAR_H = 34, GAP = 14, LABEL_W = 110, PAD_R = 16, PAD_T = 8, PAD_B = 28;
  var n = d.brands.length;
  var totalH = PAD_T + n * BAR_H + (n - 1) * GAP + PAD_B;
  var W = canvas.parentElement.getBoundingClientRect().width;
  canvas.width = W * dpr; canvas.height = totalH * dpr;
  canvas.style.width = W + 'px'; canvas.style.height = totalH + 'px';
  var ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  var chartW = W - LABEL_W - PAD_R;
  d.brands.forEach(function(brand, ri) {{
    var acts = d.rows[ri];
    var total = acts.reduce(function(s, v) {{ return s + v; }}, 0);
    var y = PAD_T + ri * (BAR_H + GAP);
    ctx.fillStyle = '#aab3cc';
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
    ctx.fillStyle = '#6f7aa0';
    ctx.font = '11px system-ui';
    ctx.textAlign = 'left';
    ctx.fillText(total + '건', x + 5, y + BAR_H / 2);
  }});
  // Legend
  var leg = document.getElementById('stacked-legend');
  if (leg) {{
    d.act_labels.forEach(function(lb, i) {{
      var el = document.createElement('div');
      el.className = 'legend-item';
      el.innerHTML = '<span class="legend-dot" style="background:' + d.act_colors[i] + '"></span>' + lb;
      leg.appendChild(el);
    }});
  }}
}})();"""


def _build_insights_script(brand_insights: dict) -> str:
    """window._renderInsights(data) 함수 정의 + 초기 렌더링 스크립트."""
    if not brand_insights:
        return ""

    flag_json = json.dumps({"US":"🇺🇸","JP":"🇯🇵","KR":"🇰🇷","SG":"🇸🇬","PL":"🇵🇱","TH":"🇹🇭","CA":"🇨🇦","GB":"🇬🇧"})
    imp_json  = json.dumps({"high": "#e05353", "medium": "#d4943a", "low": "#3e465c"})

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
    body = body.replace(/\\n/g, ' ');
    var watch = label.indexOf('관전') !== -1;
    return '<div class="insight-strat-sec"><div class="insight-strat-h' + (watch ? ' watch' : '') + '">'
      + label + '</div><div class="insight-strat-body">' + body + '</div></div>';
  }}).join('');
}}

window._renderInsights = function(data) {{
  var FLAGS = {flag_json};
  var IMP_C = {imp_json};
  var ACT_COLORS_MAP = {{"유통_채널":"#4a8fd4","신시장_진출":"#9b7fe8","신제품_런칭":"#4ab884","인플루언서_협업":"#c8a96e","투자_BD":"#e05353","브랜드_마케팅":"#e0894a","실적_공시":"#46b0b0","기타":"#6f7aa0"}};
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
    var arts = (ins.key_articles || []).map(function(a) {{
      var dot = IMP_C[a.imp] || '#9ca3af';
      var lnk = (a.url && a.url.indexOf('http') === 0)
        ? '<a class="insight-art-link" href="' + a.url + '" target="_blank" rel="noopener">↗</a>'
        : '';
      return '<div class="insight-art-row">' +
        '<span class="insight-art-imp" style="background:' + dot + '"></span>' +
        '<span class="insight-art-title">' + (a.title_ko || '') + '</span>' +
        '<span class="insight-art-meta">' + (a.date || '').slice(5) + '</span>' +
        lnk + '</div>';
    }}).join('');
    var safeId = 'insight-' + brand.replace(/\\s/g, '_');
    html += '<div class="insight-card" id="' + safeId + '">' +
      '<div class="insight-hdr">' +
        '<span class="insight-brand">' + brand + '</span>' +
        '<span class="insight-badge insight-badge-act" style="background:' + actColor + '">' + ins.top_act + ' ' + ins.top_pct + '%</span>' +
        '<span class="insight-badge ' + highCls + '">HIGH ' + ins.high_pct + '%</span>' +
      '</div>' +
      '<div class="insight-strategy">' + _fmtInsightStrategy(ins.strategy || '') + '</div>' +
      '<div class="insight-markets">' + mkts + '</div>' +
      '<div class="insight-articles-hdr">핵심 근거 기사</div>' +
      arts +
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
}})();"""


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
  --bg:      #0c0e18;
  --surface: #131620;
  --elevated:#1a1e2e;
  --deep:    #20243a;
  --border:  #262b40;
  --bhi:     #303660;
  --gold:    #d4b87e;
  --blue:    #6fb0ec;
  --hi:      #f4f6fb;
  --mid:     #bcc4d8;
  --lo:      #9ba5cc;
  --high:    #ef5353;
  --med:     #e0a040;
}
body {
  font-family: system-ui, -apple-system, "Segoe UI", "Malgun Gothic", "Noto Sans KR", sans-serif;
  background: var(--bg);
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
  position: sticky;
  top: 0;
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
  font-size: 11px;
  font-weight: 800;
  letter-spacing: 0.22em;
  text-transform: uppercase;
  color: var(--hi);
}
.page-header .meta {
  font-size: 11px;
  color: var(--mid);
  letter-spacing: 0.02em;
}
.page-header .meta span { color: var(--gold); }

/* ── Layout ── */
.page-body { max-width: 1500px; margin: 0 auto; padding: 20px 24px 64px; }
.section {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 3px;
  padding: 18px 20px;
  margin-bottom: 16px;
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
  font-size: 10px;
  color: var(--mid);
  cursor: pointer;
  font-family: inherit;
  white-space: nowrap;
  letter-spacing: 0.04em;
  transition: all 0.15s;
}
.collapse-btn:hover { border-color: var(--gold); color: var(--gold); }

/* ── Period row ── */
.period-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  background: var(--elevated);
  border: 1px solid var(--border);
  border-radius: 3px;
  padding: 8px 12px;
  margin-bottom: 16px;
}
.period-row-label {
  font-size: 9px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  color: var(--lo);
  white-space: nowrap;
}
.period-presets { display: flex; gap: 4px; }
.period-btn {
  background: transparent;
  border: 1px solid var(--border);
  border-radius: 2px;
  padding: 3px 12px;
  font-size: 11px;
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
  font-size: 11px;
  font-family: inherit;
  color: var(--hi);
  background: var(--bg);
  width: 126px;
  cursor: pointer;
}
.period-date-input:focus { outline: none; border-color: var(--gold); box-shadow: 0 0 0 2px rgba(200,169,110,0.15); }
.period-date-sep { font-size: 11px; color: var(--lo); }
.period-apply-btn {
  background: rgba(74,143,212,0.12);
  color: var(--blue);
  border: 1px solid rgba(74,143,212,0.35);
  border-radius: 2px;
  padding: 3px 12px;
  font-size: 11px;
  font-weight: 600;
  cursor: pointer;
  font-family: inherit;
  white-space: nowrap;
  transition: all 0.15s;
}
.period-apply-btn:hover { background: rgba(74,143,212,0.22); }
.period-msg { width: 100%; font-size: 10px; color: #e07e40; font-weight: 500; margin-top: 2px; }

/* ── KPI ── */
.kpi-grid { display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; }
@media (max-width: 680px) { .kpi-grid { grid-template-columns: repeat(2, 1fr); } }
.kpi-card {
  background: var(--elevated);
  border: 1px solid var(--border);
  border-radius: 3px;
  padding: 20px 18px 16px;
  position: relative;
  overflow: hidden;
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
  font-size: 11px;
  color: var(--mid);
  margin-top: 8px;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.1em;
}

/* ── Tables ── */
.table-wrap { overflow-x: auto; }
.data-table { width: 100%; border-collapse: collapse; font-size: 13px; }
.data-table th {
  background: var(--deep);
  color: var(--mid);
  font-weight: 700;
  font-size: 9px;
  text-transform: uppercase;
  letter-spacing: 0.1em;
  padding: 9px 10px;
  text-align: left;
  white-space: nowrap;
  position: sticky;
  top: 0;
  z-index: 2;
  border-bottom: 1px solid var(--border);
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
.detail-box .src-info { color: var(--mid); font-size: 10px; margin-top: 6px; }
.detail-box .title-ko-line { color: var(--blue); font-size: 11px; margin-bottom: 4px; }
.body-orig { margin-top: 8px; }
.body-orig summary { font-size: 10px; color: var(--mid); cursor: pointer; }
.body-text {
  font-size: 10px; line-height: 1.5; color: var(--mid);
  background: var(--bg); padding: 8px 10px; border-radius: 2px;
  margin-top: 4px; white-space: pre-wrap; word-break: break-word;
  max-height: 180px; overflow-y: auto;
}

/* ── Tags / badges ── */
.imp-badge {
  display: inline-block;
  padding: 1px 5px; border-radius: 2px;
  font-size: 9px; font-weight: 700; white-space: nowrap;
  vertical-align: middle; margin-right: 2px;
  letter-spacing: 0.06em;
}
.imp-high { background: rgba(224,83,83,0.15); color: #e05353; }
.imp-med  { background: rgba(212,148,58,0.15); color: #d4943a; }
.brand-tag {
  background: rgba(74,143,212,0.1);
  color: var(--blue);
  padding: 1px 7px; border-radius: 2px;
  font-size: 10px; font-weight: 700; white-space: nowrap;
  letter-spacing: 0.04em;
}
.act-tag {
  background: rgba(200,169,110,0.1);
  color: var(--gold);
  padding: 1px 7px; border-radius: 2px;
  font-size: 10px; white-space: nowrap;
}
.date-cell { color: var(--mid); font-size: 11px; white-space: nowrap; font-variant-numeric: tabular-nums; }
.flag-cell { white-space: nowrap; }
.conf-cell { color: var(--mid); font-size: 11px; text-align: right; white-space: nowrap; font-variant-numeric: tabular-nums; }
.title-cell { max-width: 480px; word-break: break-word; }

/* ── Heatmap ── */
.heatmap-wrap { overflow-x: auto; }
.heatmap-table th { position: sticky; top: 0; z-index: 2; }
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
  border-radius: 3px;
  padding: 18px 20px;
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
  font-size: 9px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.1em; color: var(--lo); margin-right: 2px; white-space: nowrap;
}
.filter-pill {
  background: transparent;
  border: 1px solid var(--border);
  border-radius: 2px;
  padding: 2px 10px;
  font-size: 10px; font-weight: 500; color: var(--mid);
  cursor: pointer; transition: all 0.12s; white-space: nowrap; font-family: inherit;
  letter-spacing: 0.02em;
}
.filter-pill:hover { border-color: var(--gold); color: var(--gold); }
.filter-pill.active { background: var(--gold); color: var(--bg); border-color: var(--gold); font-weight: 700; }
.filter-pill.act-active { background: rgba(74,143,212,0.15); color: var(--blue); border-color: var(--blue); }
.filter-count { font-size: 10px; color: var(--lo); margin-left: 4px; white-space: nowrap; }

/* ── Lower 2-col ── */
.lower-row { display: grid; grid-template-columns: minmax(0, 1fr) 300px; gap: 16px; margin-bottom: 16px; }
.lower-row > * { min-width: 0; }
@media (max-width: 900px) { .lower-row { grid-template-columns: 1fr; } }

/* ── Brand HIGH ratio ── */
.high-ratio-wrap { display: flex; flex-direction: column; gap: 10px; }
.hr-row { display: flex; align-items: center; gap: 8px; }
.hr-brand { font-size: 11px; font-weight: 600; color: var(--hi); min-width: 86px; white-space: nowrap; }
.hr-bar-bg { flex: 1; height: 12px; background: var(--elevated); border-radius: 1px; overflow: hidden; }
.hr-bar-fill {
  height: 100%;
  background: linear-gradient(90deg, #a83838, #e05353);
  border-radius: 1px;
  transition: width 0.5s ease;
}
.hr-badge { font-size: 10px; font-weight: 700; color: #e05353; min-width: 36px; text-align: right; font-variant-numeric: tabular-nums; }
.hr-meta { font-size: 10px; color: var(--mid); white-space: nowrap; min-width: 55px; font-variant-numeric: tabular-nums; }

/* ── Stacked bar ── */
.stacked-wrap { position: relative; }
.legend-row { display: flex; gap: 12px; flex-wrap: wrap; margin-top: 10px; }
.legend-item { display: flex; align-items: center; gap: 4px; font-size: 10px; color: var(--mid); }
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
.dd-date { font-size: 11px; color: var(--mid); white-space: nowrap; font-variant-numeric: tabular-nums; }
.dd-act-chip {
  font-size: 10.5px; font-weight: 700; padding: 1px 8px;
  border-radius: 2px; white-space: nowrap;
  background: rgba(111,176,236,0.14); color: var(--blue);
  letter-spacing: 0.04em;
}
.dd-title { font-size: 13.5px; color: var(--hi); line-height: 1.5; font-weight: 500; }
.dd-link { display: inline-block; margin-top: 5px; font-size: 11px; color: var(--blue); }
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
  font-size: 11px; font-weight: 800; letter-spacing: 0.14em; text-transform: uppercase;
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
  font-size: 10px; font-weight: 700; letter-spacing: 0.12em; text-transform: uppercase;
  color: var(--lo); margin-bottom: 6px;
}
.dd-act-chips { display: flex; flex-wrap: wrap; gap: 6px; }
.dd-act-focus {
  font-size: 12px; font-weight: 600; padding: 3px 9px;
  border: 1px solid; border-radius: 12px; white-space: nowrap;
  background: rgba(255,255,255,0.03);
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
.radar-icon { font-size: 10px; font-weight: 700; flex-shrink: 0; width: 12px; text-align: center; }
.radar-brand { font-size: 12px; font-weight: 600; color: var(--hi); min-width: 130px; white-space: nowrap; }
.radar-tier1 {
  font-size: 9px; font-weight: 700; padding: 1px 6px; border-radius: 2px;
  background: rgba(200,169,110,0.12); color: var(--gold);
  letter-spacing: 0.06em; white-space: nowrap; flex-shrink: 0;
}
.radar-tier2 {
  font-size: 9px; font-weight: 700; padding: 1px 6px; border-radius: 2px;
  background: rgba(78,88,112,0.3); color: var(--mid);
  letter-spacing: 0.06em; white-space: nowrap; flex-shrink: 0;
}
.radar-bar-bg { flex: 1; height: 6px; background: var(--deep); border-radius: 1px; overflow: hidden; min-width: 60px; }
.radar-bar-fill { height: 100%; border-radius: 1px; transition: width 0.5s ease; }
.radar-score { font-size: 11px; font-weight: 700; min-width: 36px; text-align: right; font-variant-numeric: tabular-nums; }
.radar-meta { font-size: 10px; color: var(--lo); white-space: nowrap; min-width: 80px; font-variant-numeric: tabular-nums; }
.radar-promo {
  font-size: 9px; font-weight: 700; padding: 1px 6px; border-radius: 2px; white-space: nowrap;
  background: rgba(74,184,132,0.15); color: #4ab884; letter-spacing: 0.04em; flex-shrink: 0;
}
.radar-demote {
  font-size: 9px; font-weight: 700; padding: 1px 6px; border-radius: 2px; white-space: nowrap;
  background: rgba(224,83,83,0.12); color: #e05353; letter-spacing: 0.04em; flex-shrink: 0;
}

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
  font-size: 9px; font-weight: 700; padding: 2px 7px;
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
  font-size: 10.5px; font-weight: 800; letter-spacing: 0.04em;
  color: var(--blue); margin-bottom: 3px;
}
.insight-strat-h.watch { color: var(--gold); }
.insight-strat-body { font-size: 12.5px; color: var(--hi); line-height: 1.6; }
.insight-markets { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; }
.insight-market-item { font-size: 11px; color: var(--mid); display: flex; align-items: center; gap: 3px; }
.insight-market-item.mkt-click {
  cursor: pointer; padding: 1px 6px; border-radius: 3px;
  border: 1px solid transparent; transition: border-color 0.15s, background 0.15s;
}
.insight-market-item.mkt-click:hover {
  border-color: rgba(200,169,110,0.4); background: rgba(200,169,110,0.08);
}
.insight-market-cnt { font-weight: 700; color: var(--hi); font-variant-numeric: tabular-nums; }
.insight-articles-hdr {
  font-size: 9px; font-weight: 700; text-transform: uppercase;
  letter-spacing: 0.1em; color: var(--lo); margin-bottom: 6px;
  padding-top: 8px; border-top: 1px solid var(--border);
}
.insight-art-row {
  display: flex; align-items: flex-start; gap: 6px;
  padding: 4px 0; border-top: 1px solid var(--border); font-size: 11px;
}
.insight-art-imp { width: 5px; height: 5px; border-radius: 50%; flex-shrink: 0; margin-top: 4px; }
.insight-art-title { flex: 1; color: var(--hi); line-height: 1.4; }
.insight-art-meta  { color: var(--lo); white-space: nowrap; font-size: 10px; font-variant-numeric: tabular-nums; }
.insight-art-link  { color: var(--blue); font-size: 10px; white-space: nowrap; }
.insight-art-link:hover { color: var(--gold); }
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
  display: flex; flex-direction: column; gap: 4px; z-index: 5;
  background: rgba(5,12,24,0.80);
  padding: 6px 10px;
  border-radius: 4px;
  border: 1px solid rgba(30,80,160,0.25);
}
.wm-lo-row { display: flex; gap: 10px; }
.wm-lo-item {
  font-size: 10px; font-family: monospace; font-weight: 700;
  letter-spacing: 0.5px; opacity: 0.85;
}
.wm-lo-size {
  font-size: 9px; font-family: monospace; opacity: 0.7;
  color: #8890b8; letter-spacing: 0.3px;
}
.wm-lo-high { color: #f87171; }
.wm-lo-med  { color: #fbbf24; }
.wm-lo-low  { color: #22d3ee; }
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
  background: var(--elevated);
  border: 1px solid var(--border);
  border-left: 3px solid var(--high);
  border-radius: 4px;
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s, transform 0.1s;
}
.wm-alert-card:hover {
  background: #1f2438;
  border-color: var(--bhi);
  border-left-color: var(--high);
  transform: translateY(-1px);
}
.wm-alert-badges { display: flex; align-items: center; gap: 6px; margin-bottom: 6px; flex-wrap: wrap; }
.wm-alert-brand {
  font-size: 11px; font-weight: 700; color: var(--gold);
  background: rgba(212,184,126,0.12); border-radius: 3px; padding: 2px 8px;
  letter-spacing: 0.02em;
}
.wm-alert-cc {
  font-size: 11px; font-weight: 600; color: var(--mid);
  background: rgba(255,255,255,0.05); border-radius: 3px; padding: 2px 8px;
}
.wm-alert-title {
  font-size: 13.5px; color: var(--hi); line-height: 1.5; font-weight: 500;
  overflow: hidden; display: -webkit-box;
  -webkit-line-clamp: 2; -webkit-box-orient: vertical;
}
.wm-alert-meta { font-size: 10.5px; color: var(--lo); margin-top: 6px; font-variant-numeric: tabular-nums; }
.wm-alert-empty { padding: 16px; color: var(--lo); font-size: 12px; grid-column: 1 / -1; }
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
    SG:[1.3,104], MY:[4,109],  ID:[-5,120], VN:[14,108], AU:[-27,133]
  }};
  var CNAMES = {{
    US:'미국', CA:'캐나다', GB:'영국', DE:'독일', FR:'프랑스',
    PL:'폴란드', JP:'일본', KR:'한국', CN:'중국', TH:'태국',
    SG:'싱가포르', MY:'말레이시아', ID:'인도네시아', VN:'베트남', AU:'호주'
  }};
  var LAND = {land_json};

  var DPR  = window.devicePixelRatio || 1;
  var W = 0, H = 0, tick = 0, animId = null;
  var ctx  = canvas.getContext('2d');
  var off  = document.createElement('canvas').getContext('2d');
  var activeCC = [];

  function pX(lon) {{ return (lon + 180) / 360 * W; }}
  function pY(lat) {{ return (90  - lat) / 180 * H; }}

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

    // Ocean
    var bg = off.createLinearGradient(0, 0, 0, H);
    bg.addColorStop(0, '#060c1a'); bg.addColorStop(1, '#030810');
    off.fillStyle = bg; off.fillRect(0, 0, W, H);

    // Dot intersections at grid nodes
    off.fillStyle = 'rgba(80,140,230,0.2)';
    for (var glon = -180; glon <= 180; glon += 30)
      for (var glat = -90; glat <= 90; glat += 30) {{
        off.beginPath(); off.arc(pX(glon), pY(glat), 0.9, 0, Math.PI*2); off.fill();
      }}

    // Grid
    off.strokeStyle = 'rgba(50,110,210,0.07)'; off.lineWidth = 0.5;
    [-60,-30,30,60].forEach(function(lat) {{
      off.beginPath(); off.moveTo(0,pY(lat)); off.lineTo(W,pY(lat)); off.stroke();
    }});
    [-120,-60,60,120].forEach(function(lon) {{
      off.beginPath(); off.moveTo(pX(lon),0); off.lineTo(pX(lon),H); off.stroke();
    }});
    // Equator + tropics
    off.strokeStyle = 'rgba(60,130,220,0.22)'; off.lineWidth = 0.9;
    off.beginPath(); off.moveTo(0,pY(0)); off.lineTo(W,pY(0)); off.stroke();
    off.strokeStyle = 'rgba(60,130,220,0.1)'; off.lineWidth = 0.5;
    [23.4,-23.4].forEach(function(lat) {{
      off.beginPath(); off.moveTo(0,pY(lat)); off.lineTo(W,pY(lat)); off.stroke();
    }});

    // Land polygons
    LAND.forEach(function(poly) {{
      off.beginPath();
      poly.forEach(function(pt, i) {{
        i === 0 ? off.moveTo(pX(pt[0]),pY(pt[1])) : off.lineTo(pX(pt[0]),pY(pt[1]));
      }});
      off.closePath();
      off.fillStyle = '#0d1c2e'; off.fill();
      off.strokeStyle = '#17304d'; off.lineWidth = 0.7; off.stroke();
    }});

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
    Object.keys(COORDS).forEach(function(cc) {{
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

      // 3 staggered pulse rings
      [0, 0.33, 0.66].forEach(function(off2) {{
        var ph = ((tick / 65) + off2) % 1;
        var pr = base + ph * 28, pa = (1 - ph) * (isH ? 0.7 : 0.45);
        ctx.beginPath(); ctx.arc(x, y, pr, 0, Math.PI*2);
        ctx.strokeStyle = glC + pa + ')';
        ctx.lineWidth = isH ? 1.6 : 1.0; ctx.stroke();
      }});

      // Outer glow halo
      var grd = ctx.createRadialGradient(x, y, 0, x, y, base*5.5);
      grd.addColorStop(0, glC+'0.55)'); grd.addColorStop(1, glC+'0)');
      ctx.beginPath(); ctx.arc(x, y, base*5.5, 0, Math.PI*2);
      ctx.fillStyle = grd; ctx.fill();

      // Core — bright highlight + color
      var cg = ctx.createRadialGradient(x-base*0.3, y-base*0.3, 0, x, y, base);
      cg.addColorStop(0, '#ffffff'); cg.addColorStop(0.45, col); cg.addColorStop(1, glC+'0.7)');
      ctx.beginPath(); ctx.arc(x, y, base, 0, Math.PI*2);
      ctx.fillStyle = cg; ctx.fill();

      // Label
      if (W > 440) {{
        var fs = Math.max(9, Math.round(W * 0.010));
        ctx.font = 'bold ' + fs + 'px monospace';
        ctx.textAlign = 'center';
        ctx.fillStyle = 'rgba(0,0,0,0.65)';
        ctx.fillText(cc, x+0.5, y - base - 3.5);
        ctx.fillStyle = isH ? '#fca5a5' : isM ? '#fde68a' : '#a5f3fc';
        ctx.fillText(cc, x, y - base - 4);
      }}
    }});
  }}

  /* ── HUD frame ── */
  function drawHUD() {{
    var L = 16;
    ctx.strokeStyle = 'rgba(99,179,237,0.35)'; ctx.lineWidth = 1.5;
    [[0,0,1,1],[W,0,-1,1],[0,H,1,-1],[W,H,-1,-1]].forEach(function(c) {{
      ctx.beginPath();
      ctx.moveTo(c[0], c[1]+c[3]*L); ctx.lineTo(c[0], c[1]); ctx.lineTo(c[0]+c[2]*L, c[1]);
      ctx.stroke();
    }});
    // Vignette
    var vig = ctx.createRadialGradient(W/2, H/2, H*0.28, W/2, H/2, H*0.9);
    vig.addColorStop(0, 'rgba(0,0,0,0)'); vig.addColorStop(1, 'rgba(0,0,0,0.55)');
    ctx.fillStyle = vig; ctx.fillRect(0, 0, W, H);
    // Coord label bottom-left
    ctx.fillStyle = 'rgba(60,110,180,0.45)';
    ctx.font = '9px monospace'; ctx.textAlign = 'left';
    ctx.fillText('EQUIRECT / WGS84', 6, H - 5);
  }}

  /* ── Main loop ── */
  function loop() {{
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    ctx.clearRect(0, 0, W, H);
    // Blit static layer
    ctx.save(); ctx.setTransform(1,0,0,1,0,0);
    ctx.drawImage(off.canvas, 0, 0); ctx.restore();
    drawScan(); drawArcs(); drawMarkers(); drawHUD();
    tick++;
    animId = requestAnimationFrame(loop);
  }}

  function resize() {{
    W = container.clientWidth || 800;
    H = Math.round(W * 0.50);
    canvas.width  = W * DPR; canvas.height = H * DPR;
    canvas.style.width = W + 'px'; canvas.style.height = H + 'px';
    container.style.height = H + 'px';
    drawStaticLayer();
  }}

  rebuildActive(); resize();
  if (animId) cancelAnimationFrame(animId);
  loop();

  window.addEventListener('resize', function() {{
    if (animId) {{ cancelAnimationFrame(animId); animId = null; }}
    resize(); loop();
  }});

  // Tooltip + click
  var tooltip = document.getElementById('worldmap-tooltip');
  function hitTest(mx, my) {{
    var hit = null, minD = 24;
    Object.keys(COORDS).forEach(function(cc) {{
      var co = COORDS[cc], x = pX(co[1]), y = pY(co[0]);
      var d = Math.sqrt((mx-x)*(mx-x)+(my-y)*(my-y));
      if (d < minD) {{ minD = d; hit = cc; }}
    }});
    return hit;
  }}
  canvas.addEventListener('mousemove', function(e) {{
    var r = canvas.getBoundingClientRect();
    var mx = e.clientX - r.left, my = e.clientY - r.top;
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
      tooltip.style.display = 'none'; canvas.style.cursor = 'default';
    }}
  }});
  canvas.addEventListener('mouseleave', function() {{ if (tooltip) tooltip.style.display='none'; }});
  canvas.addEventListener('click', function(e) {{
    var r = canvas.getBoundingClientRect();
    var hit = hitTest(e.clientX-r.left, e.clientY-r.top);
    if (hit) openHeatmapDrilldown('all', hit);
  }});
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
        {{ label: 'LOW',    data: d.low,    borderColor:'#3e465c', backgroundColor:'transparent',            tension:0.35, fill:false, pointRadius:2, borderWidth:1 }}
      ]
    }},
    options: {{
      responsive: true, maintainAspectRatio: false,
      plugins: {{ legend: {{ position: 'top', labels: {{ font: {{ size: 11 }}, color:'#aab3cc', boxWidth:12 }} }} }},
      scales: {{
        y: {{ beginAtZero: true, grid: {{ color:'rgba(30,34,53,0.8)' }}, ticks: {{ color:'#aab3cc', precision: 0, font: {{ size: 10 }} }} }},
        x: {{ grid: {{ color:'rgba(30,34,53,0.8)' }}, ticks: {{ color:'#aab3cc', font: {{ size: 10 }}, maxRotation: 45 }} }}
      }}
    }}
  }});
}})();""")

    if distribution:
        labels = [ACTIVITY_LABELS.get(d["activity_type"], d["activity_type"]) for d in distribution]
        totals = [d["total"] for d in distribution]
        colors = ["#4a8fd4","#c8a96e","#9b7fe8","#4ab884","#e05353","#6f7aa0","#d4943a"][:len(distribution)]
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
        legend: {{ position: 'right', labels: {{ font: {{ size: 10 }}, color:'#aab3cc', boxWidth: 12 }} }},
        tooltip: {{ callbacks: {{ label: function(c) {{ return c.label + ': ' + c.parsed + '건'; }} }} }}
      }}
    }}
  }});
}})();""")

    return "\n".join(scripts)


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
    radar_html        = _render_brand_radar(brand_radar or [])
    insights_script   = _build_insights_script(brand_insights)
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
            },
            "articles":      v["articles"],
            "country_stats": v["country_stats"],
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
<header class="page-header">
  <div class="page-header-brand">
    <div class="page-header-accent"></div>
    <h1>K-BEAUTY INTEL</h1>
  </div>
  <div class="meta">최근 <span id="period-label">{days}</span>일 집계 &nbsp;·&nbsp; <span>{_esc(generated_str)}</span></div>
</header>

<div class="page-body">

  <div class="section">
    <div class="section-title">요약 통계</div>
    <div class="period-row">
      <span class="period-row-label">기간</span>
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
    {kpi_html}
  </div>

  {worldmap_section}

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

  <div class="lower-row">
    <div class="section">
      <div class="section-title">
        브랜드 &times; 국가 분포 히트맵
        <span class="section-sub">셀 클릭 시 HIGH/MED 기사 목록</span>
      </div>
      {heatmap_html}
    </div>
    <div class="section">
      <div class="section-title">브랜드별 HIGH 비중</div>
      {brand_high_html}
    </div>
  </div>

  <div class="section">
    <div class="section-title">
      브랜드별 활동 유형 구성
      <span class="section-sub">전략 포지셔닝 비교</span>
    </div>
    {brand_act_html}
    <div class="legend-row" id="stacked-legend"></div>
  </div>

  <!-- Brand Radar — 모멘텀 기반 티어 신호 -->
  <div class="section">
    <div class="section-title">
      Brand Radar
      <span class="section-sub">최근 4주 vs 직전 4주 기사량 비율 · ▲Rising / ▶Stable / ▼Cooling</span>
    </div>
    {radar_html}
  </div>

  <!-- Brand Insight Cards (Claude API 자동생성) -->
  <div class="section" id="insight-section">
    <div class="section-title">
      브랜드별 전략 인사이트
      <span class="section-sub">스택바 클릭 시 해당 브랜드로 이동</span>
    </div>
    <div class="insight-grid" id="insight-grid"></div>
  </div>

  <div class="chart-section">
    <div class="section-title">주별 수집 트렌드<span class="section-sub">전체 브랜드 주간 수집량 추이</span></div>
    {trend_html}
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

  // Articles table + drilldown data
  HIGH_DATA = d.articles;
  renderArticlesTable(d.articles);

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

  // Insight cards
  if (d.insights && window._renderInsights) window._renderInsights(d.insights);
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
  if (msgEl) msgEl.style.display = 'none';

  // Check coverage — base dataset covers 90 days back from generation date
  var today  = new Date(); today.setHours(0,0,0,0);
  var fromDt = new Date(fromStr + 'T00:00:00');
  var daysBack = Math.round((today - fromDt) / 86400000);
  if (daysBack > 90) {{
    if (msgEl) {{
      msgEl.style.display = '';
      msgEl.textContent = '90일 이전 데이터는 재생성 필요: python cli.py report --days ' + (daysBack + 5);
    }}
    return;
  }}

  // Deactivate preset buttons
  document.querySelectorAll('.period-btn').forEach(function(b) {{ b.classList.remove('active'); }});

  // Client-side filter on 90-day base dataset
  var filtered = BASE_ARTICLES.filter(function(a) {{ return a.date >= fromStr && a.date <= toStr; }});
  HIGH_DATA = filtered;
  renderArticlesTable(filtered);

  // Recompute KPIs
  var brands = {{}}, countries = {{}}, high = 0;
  filtered.forEach(function(a) {{
    brands[a.brand] = 1; countries[a.country] = 1;
    if (a.imp === 'high') high++;
  }});
  var kpi = {{ total: filtered.length, high: high, brands: Object.keys(brands).length, countries: Object.keys(countries).length }};
  var el;
  el = document.getElementById('kpi-total');     if (el) el.textContent = kpi.total.toLocaleString();
  el = document.getElementById('kpi-high');      if (el) el.textContent = kpi.high.toLocaleString();
  el = document.getElementById('kpi-brands');    if (el) el.textContent = kpi.brands.toLocaleString();
  el = document.getElementById('kpi-countries'); if (el) el.textContent = kpi.countries.toLocaleString();

  // Recompute country stats for world map
  var cStats = {{}};
  filtered.forEach(function(a) {{
    if (!cStats[a.country]) cStats[a.country] = {{total:0, high:0, medium:0}};
    cStats[a.country].total++;
    if (a.imp === 'high') cStats[a.country].high++;
    else cStats[a.country].medium++;
  }});
  if (window._wmSetStats) window._wmSetStats(cStats);

  // Update header label
  var lbl = document.getElementById('period-label');
  if (lbl) lbl.textContent = fromStr + ' ~ ' + toStr;

  // Fetch insights for this custom date range
  _fetchInsights(fromStr, toStr);
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

function toggleRow(i) {{
  var row = document.getElementById('dr-' + i);
  if (row) row.classList.toggle('hidden');
}}

// ── Heatmap drilldown ──
var _CN2 = {{US:'미국',JP:'일본',KR:'한국',CN:'중국',PL:'폴란드',SG:'싱가포르',TH:'태국',GB:'영국',CA:'캐나다',AU:'호주',DE:'독일',FR:'프랑스',ID:'인도네시아',MY:'말레이시아',VN:'베트남',PH:'필리핀',IT:'이탈리아'}};

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
  '인플루언서_협업':{{l:'인플루언서',c:'#c8a96e'}},
  '투자_BD':{{l:'투자·BD',c:'#e05353'}},
  '브랜드_마케팅':{{l:'브랜드 마케팅',c:'#e0894a'}},
  '실적_공시':{{l:'실적·공시',c:'#46b0b0'}},
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

function _renderSummarySections(raw) {{
  // 서버가 html-escape한 '### 라벨\\n본문' 텍스트를 소제목 블록으로 분할
  var parts = raw.split(/###\\s+/).filter(function(s) {{ return s.trim(); }});
  if (parts.length === 0) return '<div class="dd-sum-body">' + raw + '</div>';
  return parts.map(function(chunk) {{
    var nl = chunk.indexOf('\\n');
    var label = nl === -1 ? chunk.trim() : chunk.slice(0, nl).trim();
    var body  = nl === -1 ? '' : chunk.slice(nl + 1).trim();
    body = body.replace(/\\n/g, '<br>');
    return '<div class="dd-sum-sec"><div class="dd-sum-sec-h">' + label + '</div>'
      + '<div class="dd-sum-sec-b">' + body + '</div></div>';
  }}).join('');
}}

var _ddToken = 0;
function _renderCellSummary(brand, country) {{
  var sumEl = document.getElementById('dd-summary');
  if (!sumEl) return;
  if (brand === 'all') {{ sumEl.style.display = 'none'; sumEl.innerHTML = ''; return; }}
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
  var arts = HIGH_DATA.filter(function(a) {{ return (brand === 'all' || a.brand === brand) && a.country === country; }});
  document.getElementById('dd-title').textContent = brand === 'all' ? (_CN[country] || country) + ' 전체' : brand + ' · ' + (_CN[country] || country);
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
        ? '<span style="background:rgba(239,83,83,0.18);color:#ff8a8a;padding:1px 6px;border-radius:3px;font-size:11px;font-weight:700;margin-right:5px">HIGH</span>'
        : '<span style="background:rgba(224,160,64,0.18);color:#f0be6e;padding:1px 6px;border-radius:3px;font-size:11px;font-weight:700;margin-right:5px">MED</span>';
      return '<div class="dd-item">'
        + '<div class="dd-item-top"><span class="dd-date">' + a.date + '</span>'
        + badge + '<span class="dd-act-chip">' + a.act + '</span></div>'
        + '<div class="dd-title">' + a.title + '</div>'
        + (a.details ? '<div style="font-size:13px;color:var(--mid);margin-top:4px;line-height:1.5;">' + a.details + '</div>' : '')
        + link + '</div>';
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
{insights_script}
{worldmap_script}
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# 공개 API
# ---------------------------------------------------------------------------

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
        try:
            brand_radar = get_brand_radar(session)
        except Exception:
            brand_radar = []

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

        period_data: dict = {}
        period_insights_raw: dict = {}
        period_cache: dict = {}
        period_date_ranges: dict = {}
        for p in preset_periods:
            p_cutoff_str = (_today - timedelta(days=p)).isoformat()
            p_arts = [
                a for a in _all_articles
                if _fmt_date(a.get("published_date", "")) >= p_cutoff_str
            ] if p < max_period else _all_articles
            p_stats  = get_collection_stats(session, days=p)
            p_cstats = get_country_signal_stats(session, days=p)
            period_insights_raw[p] = get_brand_insights_raw(session, days=p)
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
    )

    abs_path = os.path.abspath(output_path)
    with open(abs_path, "w", encoding="utf-8") as f:
        f.write(html_content)

    logger.info("보고서 생성 완료: %s (%.1f KB)", abs_path, len(html_content) / 1024)
    return abs_path
