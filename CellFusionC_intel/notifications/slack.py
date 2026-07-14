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


def _post(payload: dict) -> bool:
    url = _get_webhook_url()
    if not url:
        logger.debug("SLACK_WEBHOOK_URL 미설정 — 알림 스킵")
        return False
    try:
        resp = requests.post(url, json=payload, timeout=8)
        return resp.status_code == 200
    except Exception as e:
        logger.warning("Slack 전송 실패: %s", e)
        return False


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
    return _post(payload)


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
                    {"type": "mrkdwn", "text": f"⏱ 소요 {dur_str}{err_line}"},
                ],
            },
            {"type": "divider"},
        ],
    }
    return _post(payload)


def send_weekly_briefing(briefing_text: str, stats: dict) -> bool:
    """주간 브리핑 Slack 전송."""
    total   = stats.get("total", 0)
    high    = stats.get("high", 0)
    brands  = stats.get("brands", 0)
    countries = stats.get("countries", 0)

    payload = {
        "text": "📊 CellFusionC 경쟁사 주간 인텔리전스 브리핑",
        "blocks": [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "📊 경쟁사 주간 인텔리전스 브리핑",
                },
            },
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*총 수집*\n{total}건"},
                    {"type": "mrkdwn", "text": f"*High Importance*\n{high}건"},
                    {"type": "mrkdwn", "text": f"*활성 브랜드*\n{brands}개"},
                    {"type": "mrkdwn", "text": f"*커버 국가*\n{countries}개"},
                ],
            },
            {"type": "divider"},
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": briefing_text},
            },
        ],
    }
    return _post(payload)
