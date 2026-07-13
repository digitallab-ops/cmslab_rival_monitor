"""
브랜드 전략 인사이트 요약 — OpenAI API (gpt-4o-mini)
"""

import logging
import os

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)


def generate_brand_strategy_summary(brand: str, articles: list) -> str:
    """HIGH+MEDIUM 기사 → 구체적 전략 인사이트 2문장 (한국어).

    articles: [{imp, act, title_ko, details, date}, ...]
    """
    if not articles:
        return f"{brand}의 최근 주목할 만한 활동이 없습니다."

    article_lines = "\n".join(
        f"- [{a['imp'].upper()}] {a.get('title_ko','')} / {a.get('details','')[:120]} ({a.get('act','')}, {a.get('date','')})"
        for a in articles
        if a.get("title_ko") or a.get("details")
    )
    if not article_lines:
        return _fallback_from_data(brand, articles)

    prompt = f"""다음은 K-뷰티 브랜드 {brand}의 최근 경쟁 인텔리전스 기사입니다:

{article_lines}

위 기사를 바탕으로 {brand}의 현재 전략을 **2문장**으로 요약하세요.
- 반드시 기사에 나온 **구체적 사실**(파트너십, 진출국가, 인수합병, 채널명, 수치 등)을 포함할 것
- "글로벌 시장을 공략 중입니다" 같은 뻔한 표현 금지
- 첫 문장: 가장 중요한 최근 움직임 (무엇을, 어디서, 어떻게)
- 둘째 문장: 그것이 시사하는 전략 방향 또는 다음 시장"""

    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=300,
            temperature=0.3,
            messages=[{"role": "user", "content": prompt}],
        )
        content = (response.choices[0].message.content or "").strip()
        if not content:
            raise ValueError("empty response from gpt-4o-mini")
        return content
    except Exception as e:
        logger.warning("브랜드 요약 생성 실패 [%s]: %s", brand, e)
        return _fallback_from_data(brand, articles)


_COUNTRY_KO = {
    "US": "미국", "JP": "일본", "KR": "한국", "CN": "중국", "GB": "영국",
    "PL": "폴란드", "SG": "싱가포르", "TH": "태국", "CA": "캐나다", "AU": "호주",
    "DE": "독일", "FR": "프랑스", "ID": "인도네시아", "MY": "말레이시아",
    "VN": "베트남", "PH": "필리핀", "IT": "이탈리아",
}


def generate_brand_country_summary(brand: str, country: str, articles: list) -> str:
    """특정 브랜드가 특정 국가에서 벌이는 활동 → 2문장 요약 (무엇을·왜).

    articles: [{imp, act, title_ko, details, date}, ...] (해당 브랜드×국가만)
    """
    country_ko = _COUNTRY_KO.get(country, country)
    if not articles:
        return f"{brand}의 {country_ko} 관련 주목할 만한 활동이 없습니다."

    article_lines = "\n".join(
        f"- [{a['imp'].upper()}] {a.get('title_ko','')} / {a.get('details','')[:120]} ({a.get('act','')}, {a.get('date','')})"
        for a in articles
        if a.get("title_ko") or a.get("details")
    )
    if not article_lines:
        return _fallback_from_data(brand, articles)

    prompt = f"""다음은 K-뷰티 브랜드 {brand}의 **{country_ko}** 시장 관련 최근 기사입니다:

{article_lines}

위 기사를 바탕으로 {brand}가 **{country_ko}에서** 지금 무엇을 하고 있는지 **2문장**으로 요약하세요.
- 반드시 기사에 나온 **구체적 사실**(파트너십, 유통 채널명, 진출 방식, 캠페인, 수치 등)을 포함할 것
- "글로벌 시장을 공략 중입니다" 같은 뻔한 표현 금지
- 첫 문장: {country_ko}에서의 가장 중요한 최근 움직임 (무엇을, 어떻게)
- 둘째 문장: 그것이 왜 중요한가 (전략적 의미 또는 시사점)"""

    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=300,
            temperature=0.3,
            messages=[{"role": "user", "content": prompt}],
        )
        content = (response.choices[0].message.content or "").strip()
        if not content:
            raise ValueError("empty response from gpt-4o-mini")
        return content
    except Exception as e:
        logger.warning("브랜드×국가 요약 생성 실패 [%s/%s]: %s", brand, country, e)
        return _fallback_from_data(brand, articles)


def _fallback_from_data(brand: str, articles: list) -> str:
    """AI 실패 시 실제 기사 내용 기반 fallback."""
    # HIGH 우선, 없으면 MEDIUM
    key = next((a for a in articles if a.get("imp") == "high" and (a.get("details") or a.get("title_ko"))), None)
    if not key:
        key = next((a for a in articles if a.get("details") or a.get("title_ko")), None)
    if not key:
        return f"{brand}의 최근 주목할 만한 활동이 없습니다."

    first = (key.get("details") or key.get("title_ko") or "").strip()
    # 두 번째 다른 기사
    second = next(
        (a for a in articles if a is not key and (a.get("details") or a.get("title_ko"))),
        None,
    )
    second_text = ""
    if second:
        s = (second.get("details") or second.get("title_ko") or "").strip()
        if s:
            second_text = f" 아울러 {s}"

    return f"{first}{second_text}"
