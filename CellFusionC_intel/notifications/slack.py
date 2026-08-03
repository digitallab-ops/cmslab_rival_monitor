"""
Slack Incoming Webhook 알림

- high importance 기사 수집 즉시 알림
- 주간 브리핑 전송
- SLACK_WEBHOOK_URL 미설정 시 조용히 스킵
"""

import json
import logging
import os

import requests

logger = logging.getLogger(__name__)

COUNTRY_FLAGS = {
    "US": "🇺🇸", "JP": "🇯🇵", "KR": "🇰🇷", "CN": "🇨🇳",
    "PL": "🇵🇱", "SG": "🇸🇬", "TH": "🇹🇭", "GB": "🇬🇧",
    "CA": "🇨🇦", "AU": "🇦🇺", "DE": "🇩🇪", "FR": "🇫🇷",
    "ID": "🇮🇩", "MY": "🇲🇾", "VN": "🇻🇳", "PH": "🇵🇭",
    "IT": "🇮🇹",
}

ACTIVITY_EMOJI = {
    "신시장_진출":   "🌏",
    "유통_채널":     "🏪",
    "신제품_런칭":   "✨",
    "인플루언서_협업": "📱",
    "투자_BD":       "💰",
    "브랜드_마케팅": "📣",
    "실적_공시":     "📊",
    "기타":          "📌",
}


def _get_webhook_url() -> str:
    return os.getenv("SLACK_WEBHOOK_URL", "")


def _post_to(url: str, payload: dict) -> bool:
    if not url:
        return False
    try:
        resp = requests.post(url, json=payload, timeout=8)
        return resp.status_code == 200
    except Exception as e:
        logger.warning("Slack 전송 실패: %s", e)
        return False


def _post(payload: dict, secondary: bool = False) -> bool:
    """기본 채널로 전송. secondary=True면 두 번째 채널(SLACK_WEBHOOK_URL_2)에도 함께 전송.

    두 번째 웹훅은 브리핑·HIGH 속보만 받고, 수집·시스템 요약은 기본 채널만.
    """
    primary = _get_webhook_url()
    if not primary:
        logger.debug("SLACK_WEBHOOK_URL 미설정 — 알림 스킵")
    ok = _post_to(primary, payload)
    if secondary:
        url2 = os.getenv("SLACK_WEBHOOK_URL_2", "").strip()
        if url2:
            _post_to(url2, payload)
    return ok


def notify_high_importance(article) -> bool:
    """high importance 기사 즉시 알림."""
    flag = COUNTRY_FLAGS.get(article.country, "🌐")
    act_emoji = ACTIVITY_EMOJI.get(article.activity_type, "📌")
    product_line = f"\n> *제품:* {article.product_name}" if article.product_name else ""
    source_line = f"\n<{article.source_url}|원문 보기>" if article.source_url else ""

    payload = {
        "text": f"🚨 *[HIGH]* {article.brand} · {flag} {article.country}",
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"🚨 *[HIGH IMPORTANCE]* {article.brand} · {flag} {article.country}\n"
                        f"{act_emoji} *{article.activity_type}*{product_line}\n\n"
                        f"{article.details}"
                        f"{source_line}"
                    ),
                },
            },
            {"type": "divider"},
        ],
    }
    return _post(payload, secondary=True)


def notify_collection_summary(label: str, agg: dict) -> bool:
    """수집 잡 완료 후 요약 리포트 전송.

    agg: {found, saved, classified, errors, brands, countries,
          duration, high, top_saved:[(brand,cnt),...]}
    """
    saved     = agg.get("saved", 0)
    found     = agg.get("found", 0)
    classified = agg.get("classified", 0)
    high      = agg.get("high", 0)
    errors    = agg.get("errors", 0)
    brands    = agg.get("brands", 0)
    countries = agg.get("countries", 0)
    duration  = agg.get("duration", 0)

    top_saved = agg.get("top_saved", [])
    top_line = "\n".join(f"• {b} — {c}건" for b, c in top_saved[:8]) or "• 신규 저장 없음"

    mins = int(duration // 60)
    secs = int(duration % 60)
    dur_str = f"{mins}분 {secs}초" if mins else f"{secs}초"
    err_line = f"  ·  ⚠️ 오류 {errors}건" if errors else ""

    cost = agg.get("cost_usd", 0)
    calls = agg.get("api_calls", 0)
    cost_line = f"  ·  🪙 OpenAI {calls}콜 ≈ ${cost:.3f}" if calls else ""

    payload = {
        "text": f"📥 수집 완료 — {label}  (신규 {saved}건)",
        "blocks": [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"📥 수집 완료 — {label}"},
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*신규 저장*\n{saved}건"},
                    {"type": "mrkdwn", "text": f"*그중 HIGH*\n{high}건"},
                    {"type": "mrkdwn", "text": f"*수집/분류*\n{found} → {classified}"},
                    {"type": "mrkdwn", "text": f"*브랜드×국가*\n{brands}×{countries}"},
                ],
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*신규 저장 브랜드*\n{top_line}"},
            },
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"⏱ 소요 {dur_str}{err_line}{cost_line}"},
                ],
            },
            {"type": "divider"},
        ],
    }
    return _post(payload)


import re

# 섹션 라벨 → 이모지 (부분일치)
_SECTION_EMOJI = [
    ("Executive", "🎯"), ("권역", "🌍"), ("무브먼트", "🔥"), ("Watchlist", "👀"),
    ("Implication", "🧭"), ("어제의 핵심", "📰"), ("셀퓨전씨", "🧴"), ("종합", "🧠"),
]


def _sec_emoji(label: str) -> str:
    for k, e in _SECTION_EMOJI:
        if k in label:
            return e
    return "▪️"


def _clean_body(body: str) -> str:
    """마크다운 본문 → 슬랙 mrkdwn. `- `→`• `, `**`→`*`, 빈 줄 정리, 분석 줄 들여쓰기 유지."""
    out = []
    for ln in body.split("\n"):
        s = ln.rstrip()
        if not s.strip():
            continue
        s = s.replace("**", "*")               # 슬랙 볼드는 별표 하나
        st = s.lstrip()
        indent = len(s) - len(st)
        if st.startswith("- "):
            out.append("• " + st[2:])
        elif st.startswith(("→", "↳")) or indent >= 2:
            out.append("    " + st)             # 무브먼트 2번째 줄(분석) 들여쓰기
        else:
            out.append(st)
    return "\n".join(out)


def _chunk(text: str, limit: int) -> list:
    """줄 단위로 limit 이하 청크 분할."""
    chunks, buf = [], ""
    for ln in text.split("\n"):
        if len(buf) + len(ln) + 1 > limit:
            if buf:
                chunks.append(buf)
            buf = ln
        else:
            buf = f"{buf}\n{ln}" if buf else ln
    if buf:
        chunks.append(buf)
    return chunks


def _briefing_blocks(text: str, limit: int = 2900) -> list:
    """`### 섹션` 마크다운 → 섹션별 (구분선 + 볼드 헤더 + 본문) 슬랙 블록."""
    parts = re.split(r"###\s+", text or "")
    blocks = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        nl = part.find("\n")
        label = (part if nl == -1 else part[:nl]).strip()
        body = "" if nl == -1 else part[nl + 1:].strip()
        header = f"*{_sec_emoji(label)}  {label}*"
        body_clean = _clean_body(body)
        content = header + (f"\n{body_clean}" if body_clean else "")
        blocks.append({"type": "divider"})
        for c in _chunk(content, limit):
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": c}})
    return blocks


def _kst_date_label() -> str:
    """KST 기준 'M/D(요일)' 라벨."""
    from datetime import datetime, timedelta
    now = datetime.utcnow() + timedelta(hours=9)
    wd = ["월", "화", "수", "목", "금", "토", "일"][now.weekday()]
    return f"{now.month}/{now.day}({wd})"


def send_weekly_briefing(briefing_text: str, stats: dict) -> bool:
    """주간 브리핑 Slack 전송 (긴 본문 자동 분할)."""
    d = _kst_date_label()
    payload = {
        "text": f"📊 위클리 심층 브리핑 · {d}",
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": f"📊 위클리 심층 브리핑 · {d}"}},
            {"type": "context", "elements": [
                {"type": "mrkdwn",
                 "text": (f"🗓 *지난 7일 종합* · 매주 월요일 아침  ·  📥 총 *{stats.get('total',0)}*건  ·  "
                          f"🔴 HIGH *{stats.get('high',0)}*  ·  🏷 브랜드 *{stats.get('brands',0)}*  ·  "
                          f"🌐 국가 *{stats.get('countries',0)}*")}]},
            {"type": "divider"},
            *_briefing_blocks(briefing_text),
        ],
    }
    return _post(payload, secondary=True)


def send_daily_briefing(briefing_text: str, stats: dict) -> bool:
    """일간 브리핑 Slack 전송."""
    d = _kst_date_label()
    payload = {
        "text": f"🌅 데일리 브리핑 · {d}",
        "blocks": [
            {"type": "header", "text": {"type": "plain_text", "text": f"🌅 데일리 브리핑 · {d}"}},
            {"type": "context", "elements": [
                {"type": "mrkdwn", "text": f"📅 *어제 수집분 요약* · 매일 아침  ·  📥 신규 *{stats.get('total',0)}*건  ·  "
                                           f"🔴 HIGH *{stats.get('high',0)}*  ·  "
                                           f"🏷 브랜드 *{stats.get('brands',0)}*  ·  🌐 국가 *{stats.get('countries',0)}*"}]},
            {"type": "divider"},
            *_briefing_blocks(briefing_text),
        ],
    }
    return _post(payload, secondary=True)
