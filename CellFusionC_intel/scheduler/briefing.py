"""
주간 브리핑 자동 생성 (GPT + Supabase DB)

- 최근 7일 수집 데이터를 GPT-4o로 요약
- 브랜드별 주요 활동 + 시장 패턴 인사이트 생성
- Slack으로 전송
"""

import logging
from datetime import datetime, timedelta

from openai import OpenAI

from config.settings import OPENAI_API_KEY, DB_SCHEMA
from storage.models import get_session
from notifications.slack import send_weekly_briefing
from sqlalchemy import text

logger = logging.getLogger(__name__)


def _fetch_week_data(session) -> dict:
    """최근 7일 데이터 집계 (전략 스코어순)."""
    since = (datetime.utcnow() - timedelta(days=7)).isoformat()

    rows = session.execute(text(f"""
        SELECT brand, country, activity_type, importance,
               details, product_name, source_url, collected_at,
               COALESCE(strategic_score, 0) AS score,
               channel, evidence_level
        FROM {DB_SCHEMA}.news_articles
        WHERE collected_at >= :since
          AND (brand_focus != 'incidental' OR brand_focus IS NULL)
        ORDER BY COALESCE(strategic_score,0) DESC, importance DESC, collected_at DESC
    """), {"since": since}).fetchall()

    stats = session.execute(text(f"""
        SELECT
            COUNT(*) as total,
            COUNT(*) FILTER (WHERE importance='high') as high,
            COUNT(DISTINCT brand) as brands,
            COUNT(DISTINCT country) as countries
        FROM {DB_SCHEMA}.news_articles
        WHERE collected_at >= :since
    """), {"since": since}).fetchone()

    return {
        "rows": rows,
        "stats": {
            "total":     stats[0],
            "high":      stats[1],
            "brands":    stats[2],
            "countries": stats[3],
        },
    }


def _build_gpt_prompt(rows) -> str:
    if not rows:
        return "이번 주 수집된 기사가 없습니다."

    # r: brand0 country1 activity2 importance3 details4 product5 url6 collected7 score8 channel9 evidence10
    lines = ["=== 최근 7일 수집 데이터 (전략 스코어순) ===\n"]
    for r in rows[:60]:  # 최대 60건 (토큰 절약)
        ch = f" 채널:{r[9]}" if r[9] else ""
        ev = f" 근거:{r[10]}" if r[10] else ""
        lines.append(
            f"[score {r[8]}][{str(r[3]).upper()}] {r[0]}/{r[1]} - {r[2]}{ch}{ev}: "
            f"{(r[4] or '')[:160]}"
        )

    return "\n".join(lines)


def generate_weekly_briefing() -> str:
    """GPT-4o로 주간 브리핑 생성 후 Slack 전송."""
    session = get_session()
    try:
        data = _fetch_week_data(session)
    finally:
        session.close()

    if not data["rows"]:
        logger.info("주간 브리핑: 수집 데이터 없음")
        return ""

    data_prompt = _build_gpt_prompt(data["rows"])

    try:
        from analytics.summarizer import CMS_PROFILE
    except Exception:
        CMS_PROFILE = "우리=씨엠에스랩/셀퓨전씨(더마 선케어 스페셜리스트)."

    client = OpenAI(api_key=OPENAI_API_KEY)
    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "당신은 씨엠에스랩의 글로벌 경쟁 인텔리전스 분석가입니다. "
                        "아래 1주치 경쟁사 활동 데이터로 의사결정용 주간 보고를 작성하세요.\n\n"
                        f"{CMS_PROFILE}\n\n"
                        "다음 4개 섹션 형식을 정확히 지키세요 (마크다운 볼드 ** 쓰지 말 것):\n"
                        "### Executive Takeaway\n- 이번 주 가장 중요한 국가·채널·경쟁 변화 3줄\n\n"
                        "### Top 5 Activity\n- 각 줄: 브랜드 / 국가 / 활동유형 / 채널 / 근거수준 / score / 한줄 시사점 "
                        "(strategic score 높은 순 5건. 75점↑은 '[즉시공유]' 표시)\n\n"
                        "### Watchlist\n- 아직 공식 확인이 약한(근거 pr·rehash) 또는 후속 확인 필요한 3건\n\n"
                        "### Implication (셀퓨전씨)\n- 우리 유통·상품·마케팅 관점 검토 액션 1~3개. 우리 실제 제품/시장에 매칭.\n\n"
                        "한국어. 근거는 데이터에 있는 사실만."
                    ),
                },
                {"role": "user", "content": data_prompt},
            ],
            max_tokens=1100,
            temperature=0.3,
        )
        briefing_text = response.choices[0].message.content
    except Exception as e:
        logger.error("브리핑 GPT 생성 오류: %s", e)
        briefing_text = f"브리핑 생성 오류: {e}"

    logger.info("주간 브리핑 생성 완료 (%d자)", len(briefing_text))
    send_weekly_briefing(briefing_text, data["stats"])
    return briefing_text
