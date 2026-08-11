from typing import Literal, Optional

from pydantic import BaseModel, Field


class NewsClassification(BaseModel):
    brand: str = Field(description="브랜드명 (원본 영문)")
    country: str = Field(description="기사 관련 ISO 국가 코드 (예: US, PL, TH)")
    brand_focus: Literal["primary", "secondary", "incidental"] = Field(
        description="기사에서 브랜드의 비중: primary=주인공, secondary=주요 언급 중 하나, incidental=예시로 잠깐 언급"
    )
    activity_type: Literal[
        "신시장_진출",
        "유통_채널",
        "신제품_런칭",
        "인플루언서_협업",
        "투자_BD",
        "브랜드_마케팅",
        "실적_공시",
        "가격_프로모션",
        "기타",
    ] = Field(description="활동 유형")
    importance: Literal["high", "medium", "low"] = Field(description="중요도")
    strategic_score: int = Field(ge=0, le=100, description="전략적 중요도 100점 스코어(대략적 순위 신호). 75+ high, 55~74 medium, 이하 low와 정합")
    details: str = Field(description="핵심 내용 2-3문장 (한국어)")
    product_name: Optional[str] = Field(default=None, description="기사에서 언급된 특정 제품명 (없으면 null)")
    channel: Optional[str] = Field(default=None, description="입점·유통 채널/리테일러명 (예: Sephora, Watsons, Nykaa; 없으면 null)")
    price_info: Optional[str] = Field(default=None, description="가격·프로모션 정보 (예: 'THB 690 / 15% 할인'; 없으면 null)")
    city: Optional[str] = Field(default=None, description="구체 도시명 (기사에 명시된 경우만; 없으면 null)")
    evidence_level: Literal["official", "editorial", "pr", "rehash"] = Field(
        description="근거 수준: official=브랜드/리테일러 공식, editorial=독립 편집기사, pr=보도자료/PR와이어, rehash=재게재·재가공"
    )
    title_ko: Optional[str] = Field(default=None, description="기사 제목의 한국어 번역 (원문이 이미 한국어면 null)")
    article_body_ko: Optional[str] = Field(default=None, description="기사 본문의 한국어 번역 요약 (최대 500자, 본문 없으면 null)")
    key_ingredients: Optional[str] = Field(default=None, description="기사에 언급된 핵심 성분/포뮬러를 쉼표로 구분 (예: 'PDRN,센텔라,나이아신아마이드'). 성분 언급 없으면 null")
    sentiment: Literal["positive", "neutral", "negative"] = Field(default="neutral", description="해당 브랜드 관점의 기사 톤: positive=호재, neutral=중립, negative=악재(리콜·품질이슈·논란·규제·소송 등)")
    confidence: float = Field(ge=0.0, le=1.0, description="분류 신뢰도 0.0~1.0")
    note: Optional[str] = Field(default=None, description="추가 메모 또는 불확실 사항")
