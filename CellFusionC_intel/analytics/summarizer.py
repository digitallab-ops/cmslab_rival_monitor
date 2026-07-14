"""
브랜드 전략 인사이트 요약 — OpenAI API (gpt-4o-mini)
"""

import logging
import os

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)


def generate_brand_strategy_summary(brand: str, articles: list) -> str:
    """HIGH+MEDIUM 기사 → 분석적 전략 인사이트 (2섹션, 한국어).

    ### 전략 요약 / ### 관전 포인트 형식. 프론트가 '### 라벨'로 분할 렌더.
    articles: [{imp, act, title_ko, details, date}, ...]
    """
    if not articles:
        return f"### 전략 요약\n{brand}의 최근 주목할 만한 활동이 없습니다."

    article_lines = "\n".join(
        f"- [{a['imp'].upper()}] {a.get('title_ko','')} / {a.get('details','')[:140]} ({a.get('act','')}, {a.get('date','')})"
        for a in articles
        if a.get("title_ko") or a.get("details")
    )
    if not article_lines:
        return _fallback_from_data(brand, articles)

    prompt = f"""당신은 K-뷰티 경쟁사 인텔리전스 분석가입니다. 아래는 브랜드 **{brand}**의 최근 기사(여러 시장 종합)입니다:

{article_lines}

이 브랜드가 지금 무엇을 하고 있고, 그것이 **경쟁 관점에서 무엇을 의미하는지** 분석하세요.
단순 사실 나열("~하고 있다")이 아니라 **패턴과 의도, 시사점**을 읽어내야 합니다.
아래 2개 섹션 형식을 **정확히** 지키세요 (머리말은 반드시 `### `로 시작):

### 전략 요약
여러 시장·활동을 관통하는 {brand}의 핵심 전략 패턴을 2문장으로. 구체적 사실(채널명·국가·파트너·수치)로 뒷받침. "글로벌 공략 중" 같은 뻔한 말 금지.

### 관전 포인트
분석가의 한 줄 인사이트 — 이 움직임이 **왜 중요한가 / 무엇을 노리는가 / 다음에 무엇을 주시해야 하는가**. 경쟁사(우리) 관점에서 시사점이 드러나게 1~2문장. 기사에 없는 추측은 "가능성" 수준으로만."""

    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=450,
            temperature=0.35,
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
    """특정 브랜드가 특정 국가에서 벌이는 활동 → 구조화된 전략 리딩.

    3개 섹션(### 핵심 행보 / ### 근거 / ### 전략적 의도)으로 반환.
    프론트가 '### 라벨' 기준으로 분할해 소제목 블록으로 렌더링.

    articles: [{imp, act, title_ko, details, date}, ...] (해당 브랜드×국가만)
    """
    country_ko = _COUNTRY_KO.get(country, country)
    if not articles:
        return f"### 핵심 행보\n{brand}의 {country_ko} 관련 주목할 만한 활동이 아직 없습니다."

    article_lines = "\n".join(
        f"- [{a['imp'].upper()}] {a.get('title_ko','')} / {a.get('details','')[:160]} ({a.get('act','')}, {a.get('date','')})"
        for a in articles
        if a.get("title_ko") or a.get("details")
    )
    if not article_lines:
        return _fallback_from_data(brand, articles)

    prompt = f"""당신은 K-뷰티 경쟁사 인텔리전스 분석가입니다.
다음은 브랜드 **{brand}**의 **{country_ko}** 시장 관련 최근 기사입니다:

{article_lines}

위 기사들을 종합해 {brand}가 **{country_ko}에서** 무엇을 어떻게 하고 있으며 그 속셈(전략적 의도)이 무엇인지 분석하세요.
아래 3개 섹션 형식을 **정확히** 지켜서 작성하세요 (각 섹션 머리말은 반드시 `### `로 시작):

### 핵심 행보
{country_ko} 시장에서의 구체적 움직임을 2~3문장으로. 반드시 기사의 **구체적 사실**(유통 채널명, 파트너·인플루언서 이름, 진출 방식, 제품, 수치·시점)을 명시. 여러 건이면 흐름/순서로 엮을 것.

### 근거
위 판단의 핵심 근거가 된 기사 1~3건을 "- 제목 요지 (날짜)" 형식으로 나열. 각 줄에 왜 중요한지 한 구절 덧붙일 것.

### 전략적 의도
{brand}가 {country_ko}에서 노리는 것 — 사업 확장 방식(유통 확대? 브랜드 인지도? 특정 세그먼트 공략?)과 다음 수순 예측을 2~3문장으로. "글로벌 공략 중" 같은 뻔한 말 금지, 이 브랜드·이 시장에 특정된 해석만."""

    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=600,
            temperature=0.35,
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
