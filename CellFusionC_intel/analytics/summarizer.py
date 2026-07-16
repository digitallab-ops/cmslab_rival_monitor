"""
브랜드 전략 인사이트 요약 — OpenAI API (gpt-4o-mini)
"""

import logging
import os

from dotenv import load_dotenv
load_dotenv()

logger = logging.getLogger(__name__)

# 자사 프로필 — 인사이트를 우리(씨엠에스랩) 관점으로 튜닝하기 위한 컨텍스트
CMS_PROFILE = """[우리 회사 = 씨엠에스랩 / 브랜드 = 셀퓨전씨(CellFusionC)]
- 정체성: 병의원 기반 20년 노하우의 더마/임상 '선케어 스페셜리스트'. 매스 K-뷰티 트렌드 추종이 아니라 전문성·임상 근거가 무기.
- 핵심 제품군:
  ① 선케어(자외선차단) — 레이저 선스크린(고보호 SPF50+), 톤업/틴티드 선크림, 데일리 선크림  ← 우리의 심장
  ② 더마 스킨케어 — 진정·장벽 케어
  ③ 이너뷰티 — 건강기능식품(이너 선케어)
- 주력 시장/채널: 한국(올리브영 핵심), 베트남(자외선차단 1위), 중국(선케어 1위), 일본(버라이어티샵). 미국은 확장 과제(아직 약함).
- 우리와 무관한 영역(색조 메이크업, 향수, 아이크림, 헤어 등)은 셀퓨전씨 사업과 직접 관련 없음.

[관련성 판단 기준 — 매우 중요]
경쟁사 움직임이 (a) 선케어·자외선차단·톤업/틴티드, (b) 더마/진정/장벽 스킨케어, (c) 우리 주력시장·채널(한국 올리브영·베트남·중국·일본·미국 진출) 중 하나라도 건드리면 "우리와 관련". 그 외(색조·향수·아이크림 순위 등)는 우리와 무관하니 인사이트에서 제외한다."""

_ACT_LABEL = {
    "신시장_진출": "신시장 진출", "유통_채널": "유통 채널", "신제품_런칭": "신제품 런칭",
    "인플루언서_협업": "인플루언서 협업", "투자_BD": "투자·BD", "브랜드_마케팅": "브랜드 마케팅",
    "실적_공시": "실적·공시", "기타": "기타",
}


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

    prompt = f"""당신은 씨엠에스랩(더마 선케어 브랜드 '셀퓨전씨' 운영)의 경쟁사 인텔리전스 분석가입니다.
아래는 경쟁 브랜드 **{brand}**의 최근 기사(여러 시장 종합)입니다:

{article_lines}

{CMS_PROFILE}

이 브랜드의 움직임을 **날카롭게** 분석하세요. 뭉툭한 서술("~하고 있다", "경쟁력을 강화 중") 절대 금지.
반드시 아래 2개 섹션 형식으로 (머리말은 `### `로 시작):

### 전략 요약
{brand}의 핵심 전략을 관통하는 **한 문장 결론 + 근거 1문장**. 구체 사실(채널명·국가·파트너·수치)로 못박을 것. 여러 활동이면 그 밑에 깔린 하나의 의도로 꿰어라.

### 관전 포인트
셀퓨전씨 입장에서의 **날 선 시사점** 1~2문장. 다음 중 최소 하나를 명시:
(a) 우리와 겹치는 지점(선케어·더마·해외시장 특히 베트남/중국/일본)이 있으면 위협 강도,
(b) 우리가 취할 구체적 대응/선점 포인트,
(c) 다음에 반드시 주시할 시그널.
일반론 말고 이 브랜드·이 상황에 특정된 조언만."""

    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=450,
            temperature=0.4,
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

    prompt = f"""당신은 씨엠에스랩(더마 선케어 브랜드 '셀퓨전씨' 운영)의 경쟁사 인텔리전스 분석가입니다.
다음은 경쟁 브랜드 **{brand}**의 **{country_ko}** 시장 관련 최근 기사입니다:

{article_lines}

{CMS_PROFILE}

위 기사들을 종합해 {brand}가 **{country_ko}에서** 무엇을 어떻게 하고 있으며 그 속셈(전략적 의도)이 무엇인지 분석하세요.
아래 3개 섹션 형식을 **정확히** 지켜서 작성하세요 (각 섹션 머리말은 반드시 `### `로 시작, 마크다운 볼드 ** 쓰지 말 것):

### 핵심 행보
{country_ko} 시장에서의 구체적 움직임을 2~3문장으로. 반드시 기사의 구체적 사실(유통 채널명, 파트너·인플루언서 이름, 진출 방식, 제품명, 수치·시점)을 명시. 여러 건이면 흐름/순서로 엮을 것.

### 근거
핵심 근거 기사 1~3건을 "- 제목 요지 (날짜)"로 나열. 각 줄에 왜 중요한지 한 구절.

### 셀퓨전씨 시사점
{brand}의 {country_ko} 행보가 우리(셀퓨전씨)에게 갖는 의미를 1~2문장으로. 우리 선케어/더마 영역 또는 이 시장({country_ko})과 겹치면 위협/기회로 못박고 대응 힌트까지. 안 겹치면 "직접 경쟁 접점은 낮음"이라고 솔직히."""

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


def generate_market_overview(brand_insights_raw: dict) -> str:
    """전 브랜드 데이터 종합 → 시장 인사이트 + 셀퓨전씨 맞춤 조언 (구조화).

    brand_insights_raw: {brand: {top_act, high_pct, articles:[{imp,act,title_ko,details,date}], ...}}
    반환: ### 지금 대응해야 할 것 / ### 선점할 기회 / ### 확인·점검할 것
    """
    if not brand_insights_raw:
        return "### 지금 대응해야 할 것\n- 최근 종합할 만한 경쟁사 활동이 없습니다."

    # 활동유형 집계 + 경쟁사 기사 digest (국가·제품 포함 → 우리 제품/시장 매칭 근거)
    act_tally: dict = {}
    lines: list = []
    for brand, d in brand_insights_raw.items():
        arts = d.get("articles") or []
        for a in arts:
            act = a.get("act", "")
            if act:
                act_tally[act] = act_tally.get(act, 0) + 1
        # HIGH 우선 + 부족하면 MED 보충, 브랜드당 최대 3건
        picked = [a for a in arts if a.get("imp") == "high"][:3]
        if len(picked) < 3:
            picked += [a for a in arts if a.get("imp") != "high"][: 3 - len(picked)]
        for a in picked:
            t = a.get("title_ko") or (a.get("details") or "")[:80]
            if not t:
                continue
            cc = a.get("country", "") or "?"
            prod = a.get("product", "")
            meta = f"{_ACT_LABEL.get(a.get('act',''), a.get('act',''))}, {cc}"
            if prod:
                meta += f", 제품:{prod}"
            lines.append(f"- [{brand}/{cc}] {t} ({meta}, {a.get('date','')})")
    lines = lines[:26]
    if not lines:
        return "### 지금 대응해야 할 것\n- 최근 종합할 만한 경쟁 활동이 없습니다."

    act_rank = sorted(act_tally.items(), key=lambda x: -x[1])
    act_str = ", ".join(f"{_ACT_LABEL.get(k,k)} {v}건" for k, v in act_rank[:6])

    prompt = f"""당신은 씨엠에스랩의 수석 경쟁 전략 애널리스트입니다.
아래는 최근 모니터링된 K-뷰티 경쟁 브랜드들의 주요 활동입니다. 각 줄 형식: [브랜드/국가] 제목 (활동유형, 국가, 제품, 날짜)

{chr(10).join(lines)}

[활동유형 분포] {act_str}

{CMS_PROFILE}

당신의 임무: 위 경쟁 동향을 **셀퓨전씨(더마 선케어)의 눈으로** 걸러내고 매칭해서, 우리가 실제로 움직일 근거가 되는 인사이트만 뽑는다.

절대 규칙:
1) 관련성 필터 — 위 [관련성 판단 기준]에 안 맞는 항목(색조·향수·아이크림 순위 등 우리와 무관)은 **버려라**. 억지로 채우지 마라.
2) 매칭 — 남긴 항목은 반드시 **우리의 특정 제품/라인(레이저선스크린·톤업선크림·데일리선크림·더마스킨케어·이너뷰티) 또는 특정 시장(한국올영·베트남·중국·일본·미국)**에 연결해라. "누가 어디서 무엇을 → 그게 우리 무엇에 어떻게" 형태.
3) 뻔한 말·추상어("경쟁 심화", "글로벌 공략") 금지. 경쟁사 실명과 구체 행동을 반드시 포함.
4) 출력은 마크다운 볼드(**)·번호목록 쓰지 말 것. 각 항목은 "- "로 시작. 머리말은 `### `.

아래 3개 섹션으로:

### 지금 대응해야 할 것
우리 선케어/더마 영역이나 주력시장(베트남·중국·일본·올영)을 직접 건드리는 경쟁 위협. 각 줄 = [경쟁사가 무엇을 어디서] → [우리 어떤 제품/시장이 어떻게 위협받나 + 어떻게 대응] [시급:높음/중간].

### 선점할 기회
우리 강점(임상 더마 선케어, 자외선차단 전문)으로 먼저 먹을 수 있는 빈틈/트렌드. 각 줄 = [어떤 트렌드·빈틈] → [우리 어떤 제품으로 + 어느 시장·채널에서 선점].

### 확인·점검할 것
아직 불확실하지만 반드시 주시·검증할 시그널. 각 줄 = [무엇을 왜 지켜봐야 하나](예: 경쟁사가 우리 1위 시장에 진입 조짐, 특정 채널 동향, 데이터가 부족한 영역).

각 섹션 2~3개. 정말 해당 항목이 없으면 그 섹션에 "- 현재 특이사항 없음"이라고만 적어라."""

    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=900,
            temperature=0.4,
            messages=[{"role": "user", "content": prompt}],
        )
        content = (response.choices[0].message.content or "").strip()
        if not content:
            raise ValueError("empty market overview")
        return content
    except Exception as e:
        logger.warning("시장 종합 인사이트 생성 실패: %s", e)
        return ""


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
