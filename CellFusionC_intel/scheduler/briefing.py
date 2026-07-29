"""
브리핑 자동 생성 (GPT + Supabase DB)

- 주간(월 08:00 KST): 최근 7일 심층 분석 → gpt-4o
- 일간(매일 08:00 KST): 전날 수집분 요약 → gpt-4o-mini
- Slack 전송
"""

import logging
from datetime import datetime, timedelta

from openai import OpenAI

from config.settings import OPENAI_API_KEY, DB_SCHEMA
from config.brands import REGION_MAP
from notifications.slack import send_weekly_briefing, send_daily_briefing
from storage.models import get_session
from sqlalchemy import text

logger = logging.getLogger(__name__)

# 대표 기사만(의미 중복 제외).
_DUP_FILTER = "AND is_duplicate IS NOT TRUE"


def _fetch_rows(session, hours: int) -> list:
    """최근 N시간 수집 기사 (스코어순, incidental·중복 제외)."""
    since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    return session.execute(text(f"""
        SELECT brand, country, activity_type, importance,
               details, product_name, source_url, collected_at,
               COALESCE(strategic_score, 0) AS score, channel, evidence_level
        FROM {DB_SCHEMA}.news_articles
        WHERE collected_at >= :since
          AND (brand_focus != 'incidental' OR brand_focus IS NULL)
          {_DUP_FILTER}
        ORDER BY COALESCE(strategic_score,0) DESC, importance DESC, collected_at DESC
    """), {"since": since}).fetchall()


def _stats(session, hours: int) -> dict:
    since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    r = session.execute(text(f"""
        SELECT COUNT(*), COUNT(*) FILTER (WHERE importance='high'),
               COUNT(DISTINCT brand), COUNT(DISTINCT country)
        FROM {DB_SCHEMA}.news_articles
        WHERE collected_at >= :since {_DUP_FILTER}
    """), {"since": since}).fetchone()
    return {"total": r[0], "high": r[1], "brands": r[2], "countries": r[3]}


def _fmt_line(r, detail_len: int) -> str:
    # r: brand0 country1 activity2 importance3 details4 product5 url6 collected7 score8 channel9 evidence10
    ch = f" 채널:{r[9]}" if r[9] else ""
    ev = f" 근거:{r[10]}" if r[10] else ""
    pr = f" 제품:{r[5]}" if r[5] else ""
    return (f"[score {r[8]}][{str(r[3]).upper()}] {r[0]}/{r[1]} - {r[2]}{pr}{ch}{ev}: "
            f"{(r[4] or '')[:detail_len]}")


def _build_prompt_by_region(rows, limit: int, detail_len: int) -> str:
    """권역별로 묶은 데이터 프롬프트."""
    if not rows:
        return "수집된 기사가 없습니다."
    buckets: dict = {}
    for r in rows[:limit]:
        region = REGION_MAP.get((r[1] or "").upper(), "기타")
        buckets.setdefault(region, []).append(r)
    order = ["KR", "APAC", "SEA", "NA", "EU", "ME", "LATAM", "AF", "IN", "기타"]
    lines = []
    for reg in sorted(buckets, key=lambda x: order.index(x) if x in order else 99):
        lines.append(f"\n=== [{reg}] ===")
        for r in buckets[reg]:
            lines.append(_fmt_line(r, detail_len))
    return "\n".join(lines)


def _openai(model: str, system: str, user: str, max_tokens: int) -> str:
    client = OpenAI(api_key=OPENAI_API_KEY)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        max_tokens=max_tokens,
        temperature=0.3,
    )
    return (resp.choices[0].message.content or "").strip()


def _cms_profile() -> str:
    try:
        from analytics.summarizer import CMS_PROFILE
        return CMS_PROFILE
    except Exception:
        return "우리=씨엠에스랩/셀퓨전씨(더마 선케어 스페셜리스트)."


# ── 주간 브리핑 (심층) ────────────────────────────────────────────────────────

def generate_weekly_briefing() -> str:
    """최근 7일 심층 주간 보고 → Slack (gpt-4o)."""
    session = get_session()
    try:
        rows = _fetch_rows(session, hours=24 * 7)
        stats = _stats(session, hours=24 * 7)
    finally:
        session.close()

    if not rows:
        logger.info("주간 브리핑: 수집 데이터 없음")
        return ""

    data_prompt = _build_prompt_by_region(rows, limit=100, detail_len=240)
    system = (
        "당신은 씨엠에스랩의 글로벌 경쟁 인텔리전스 수석 분석가입니다. "
        "아래 1주치 경쟁사 활동 데이터(권역별 정리)로 의사결정용 심층 주간 보고를 작성하세요.\n\n"
        f"{_cms_profile()}\n\n"
        "요구: 표면적 요약 금지. 구체 사실(채널·수치·제품·도시)과 '왜 중요한지'를 파고들 것.\n"
        "출력 규칙(슬랙 전송용): 섹션 머리말은 '### '로 시작. 강조는 별표 하나 *굵게* 만 사용(별표 두 개 ** 금지). "
        "번호목록·표 금지. 각 항목은 '- '로 시작.\n\n"
        "### Executive Takeaway\n- 이번 주 판을 바꾸는 핵심 변화 3~4개. 각 줄 앞에 *브랜드* 강조 + 무엇을·왜.\n\n"
        "### 권역별 핵심 움직임\n- 권역(EU·ME·SEA·IN·NA 등)별 가장 중요한 움직임을 "
        "'- [권역] *브랜드*: 무엇을 어디서(채널) → 왜 중요' 형식으로. 활동 있는 권역만.\n\n"
        "### 주요 무브먼트 상세 (Top 5~7)\nstrategic score 높은 순. 각 항목을 정확히 '두 줄'로:\n"
        "- *브랜드* · 국가 · 활동 · 채널 · score점  (75점↑면 맨 앞에 🔴[즉시공유])\n"
        "  → 무엇을·왜 중요·우리(셀퓨전씨) 접점 1~2문장.\n\n"
        "### Watchlist\n- 공식확인 약함(pr·rehash) 또는 후속 확인 필요한 3건 + 무엇을 확인해야 하나.\n\n"
        "### Implication (셀퓨전씨)\n- 우리 유통·상품·마케팅 관점 실행 액션 2~3개. 우리 실제 제품(레이저UV썬스크린·"
        "콜라겐PDRN앰플 등)/주력시장(올영·베트남·중국·일본·미국)에 구체 매칭.\n\n"
        "한국어. 데이터에 있는 사실만."
    )
    try:
        text_out = _openai("gpt-4o", system, data_prompt, max_tokens=3000)
    except Exception as e:
        logger.error("주간 브리핑 GPT 오류: %s", e)
        text_out = f"브리핑 생성 오류: {e}"

    logger.info("주간 브리핑 생성 완료 (%d자)", len(text_out))
    send_weekly_briefing(text_out, stats)
    return text_out


# ── 일간 브리핑 (간결) ────────────────────────────────────────────────────────

def generate_daily_briefing() -> str:
    """전날 수집분 요약 → Slack (gpt-4o-mini)."""
    session = get_session()
    try:
        rows = _fetch_rows(session, hours=28)   # 전날 저녁 수집분 커버
        stats = _stats(session, hours=28)
    finally:
        session.close()

    if not rows:
        logger.info("일간 브리핑: 전날 수집 없음")
        send_daily_briefing("어제 새로 잡힌 주목할 경쟁 활동이 없습니다.", stats)
        return ""

    data_prompt = _build_prompt_by_region(rows, limit=45, detail_len=160)
    system = (
        "당신은 씨엠에스랩의 경쟁 인텔리전스 분석가입니다. 어제 수집된 경쟁사 활동을 아침 브리핑으로 "
        "간결히 정리하세요.\n\n"
        f"{_cms_profile()}\n\n"
        "마크다운 볼드(**) 쓰지 말 것. 아래 형식(머리말 '### '):\n\n"
        "### 어제의 핵심 (3~5건)\n- 각 줄: 브랜드/국가 - 무엇을(채널·제품 포함) → 한줄 시사점. score 높은 순.\n\n"
        "### 셀퓨전씨 관련\n- 우리 선케어·더마·주력시장과 겹치는 건이 있으면 1~2건 콕 집어 대응 포인트. 없으면 '특이사항 없음'.\n\n"
        "한국어, 총 400자 내외. 데이터에 있는 사실만."
    )
    try:
        text_out = _openai("gpt-4o-mini", system, data_prompt, max_tokens=700)
    except Exception as e:
        logger.error("일간 브리핑 GPT 오류: %s", e)
        text_out = f"브리핑 생성 오류: {e}"

    logger.info("일간 브리핑 생성 완료 (%d자)", len(text_out))
    send_daily_briefing(text_out, stats)
    return text_out
