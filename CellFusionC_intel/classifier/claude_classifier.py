"""
2단계 OpenAI GPT 분류 파이프라인 (최적화)

Stage 1 — gpt-4o-mini: 관련성 필터 (배치, 저비용)
Stage 2 — gpt-4o: 배치 structured output 분류
  - 기사 N건을 1 API 콜로 처리 → 비용·속도 대폭 절감
  - 429 rate limit 시 지수 백오프 자동 재시도
"""

import json
import logging
import time
from typing import Optional

from openai import OpenAI, RateLimitError

from collectors.base_collector import RawArticle
from config.settings import OPENAI_API_KEY, CLASSIFIER_MODEL_FILTER, CLASSIFIER_MODEL_DETAIL
from classifier.schemas import NewsClassification
from classifier.prompts import (
    FILTER_SYSTEM_PROMPT,
    CLASSIFICATION_SYSTEM_PROMPT,
    build_filter_prompt,
    build_classification_prompt,
)

logger = logging.getLogger(__name__)

BATCH_SIZE = 8          # gpt-4o 1콜당 최대 기사 수
MAX_RETRIES = 4
RETRY_BASE_DELAY = 20   # 첫 재시도 대기 (초)

_client: Optional[OpenAI] = None

# ── 토큰 사용량 집계 (비용 가시화) ──────────────────────────────────────────
# gpt-4o-mini 단가 (USD / 1M tokens)
_PRICE_IN = 0.15
_PRICE_OUT = 0.60
_token_usage = {"in": 0, "out": 0, "calls": 0}


def reset_token_usage() -> None:
    _token_usage.update({"in": 0, "out": 0, "calls": 0})


def get_token_usage() -> dict:
    """{in, out, calls, cost_usd} 반환."""
    cost = _token_usage["in"] / 1e6 * _PRICE_IN + _token_usage["out"] / 1e6 * _PRICE_OUT
    return {**_token_usage, "cost_usd": round(cost, 4)}


def _track_usage(response) -> None:
    try:
        u = getattr(response, "usage", None)
        if u:
            _token_usage["in"] += getattr(u, "prompt_tokens", 0) or 0
            _token_usage["out"] += getattr(u, "completion_tokens", 0) or 0
            _token_usage["calls"] += 1
    except Exception:
        pass


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=OPENAI_API_KEY)
    return _client


def _is_quota_error(err) -> bool:
    """429 중 '잔액 소진'인지 — 속도 제한과 구분한다.

    OpenAI는 둘 다 429로 준다. 속도 제한은 기다리면 풀리지만 잔액 소진은
    기다려도 안 풀린다. 그런데 예전엔 구분 없이 20·40·80초를 쉬고 재시도해서,
    실제로 하루 90분을 허비하고도 결국 전부 실패했다(2026-11-03, 268건).
    """
    # 에러 모양이 SDK 버전·경로마다 다르다(body가 error를 감싸기도, 아니기도).
    # 한 군데만 보면 놓치므로 가능한 출처를 전부 긁어 한 문자열로 합쳐 본다.
    parts = [str(err), str(getattr(err, "message", "") or "")]
    body = getattr(err, "body", None)
    if isinstance(body, dict):
        inner = body.get("error") if isinstance(body.get("error"), dict) else body
        parts += [str(inner.get("code") or ""), str(inner.get("type") or ""),
                  str(inner.get("message") or "")]
    txt = " ".join(parts).lower()
    return "insufficient_quota" in txt or "exceeded your current quota" in txt


class QuotaExhausted(RuntimeError):
    """OpenAI 크레딧 소진 — 재시도해도 소용없다. 충전이 필요하다."""


def _with_retry(fn, *args, **kwargs):
    """429 시 지수 백오프 재시도. 단 '잔액 소진'이면 즉시 중단한다."""
    delay = RETRY_BASE_DELAY
    for attempt in range(MAX_RETRIES):
        try:
            return fn(*args, **kwargs)
        except RateLimitError as e:
            if _is_quota_error(e):
                logger.error("OpenAI 크레딧 소진 — 재시도하지 않고 중단합니다. "
                             "결제/크레딧을 확인하세요. (%s)", str(e)[:140])
                raise QuotaExhausted(str(e)[:200]) from e
            if attempt == MAX_RETRIES - 1:
                raise
            logger.warning("Rate limit — %d초 대기 (%d/%d)", delay, attempt + 1, MAX_RETRIES)
            time.sleep(delay)
            delay *= 2
    return None


# ── Stage 1: gpt-4o-mini 필터 ─────────────────────────────────────────────────

def _filter_relevant(articles: list[RawArticle]) -> list[int]:
    """관련 기사 인덱스 반환. 오류 시 전체 통과."""
    if not articles:
        return []

    client = _get_client()
    try:
        response = _with_retry(
            client.chat.completions.create,
            model=CLASSIFIER_MODEL_FILTER,
            messages=[
                {"role": "system", "content": FILTER_SYSTEM_PROMPT},
                {"role": "user", "content": build_filter_prompt(articles)},
            ],
            response_format={"type": "json_object"},
            max_completion_tokens=512,
            temperature=0,
        )
        _track_usage(response)
        data = json.loads(response.choices[0].message.content)
        indices = data.get("relevant", [])
        return [i for i in indices if isinstance(i, int) and 0 <= i < len(articles)]

    except QuotaExhausted:
        # '전체 통과'로 넘기면 분류 없이 저장돼 지표가 오염된다. 위로 올려 중단시킨다.
        raise
    except Exception as e:
        logger.warning("Stage1 오류 → 전체 통과: %s", e)
        return list(range(len(articles)))


# ── Stage 2: gpt-4o 배치 분류 ─────────────────────────────────────────────────

def _make_batch_schema() -> dict:
    """NewsClassification + index 필드를 포함한 배치 JSON 스키마.
    OpenAI strict 모드: 모든 properties가 required에 있어야 하고,
    Optional 필드는 anyOf[type, null] 패턴으로 표현.
    """
    item_schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "index":         {"type": "integer", "description": "기사 인덱스 (0-based)"},
            "brand":         {"type": "string",  "description": "브랜드명 (원본 영문)"},
            "country":       {"type": "string",  "description": "ISO 국가 코드"},
            "activity_type": {
                "type": "string",
                "enum": ["신시장_진출", "유통_채널", "신제품_런칭",
                         "인플루언서_협업", "투자_BD", "브랜드_마케팅", "실적_공시",
                         "가격_프로모션", "기타"],
                "description": "활동 유형",
            },
            "importance":    {"type": "string", "enum": ["high", "medium", "low"]},
            "strategic_score": {"type": "integer", "description": "전략 중요도 100점 스코어(대략적). 75+ high/55~74 medium/이하 low"},
            "details":       {"type": "string", "description": "핵심 내용 2-3문장 (한국어)"},
            "product_name":  {"anyOf": [{"type": "string"}, {"type": "null"}],
                              "description": "언급된 특정 제품명 (없으면 null)"},
            "channel":       {"anyOf": [{"type": "string"}, {"type": "null"}],
                              "description": "입점·유통 채널/리테일러명 (예: Sephora, Watsons, Nykaa; 없으면 null)"},
            "price_info":    {"anyOf": [{"type": "string"}, {"type": "null"}],
                              "description": "가격·프로모션 정보 (예: 'THB 690 / 15% 할인'; 없으면 null)"},
            "city":          {"anyOf": [{"type": "string"}, {"type": "null"}],
                              "description": "구체 도시명 (기사에 명시된 경우만; 없으면 null)"},
            "evidence_level": {
                "type": "string",
                "enum": ["official", "editorial", "pr", "rehash"],
                "description": "근거 수준: official=공식, editorial=편집기사, pr=보도자료/PR와이어, rehash=재게재",
            },
            "title_ko":      {"anyOf": [{"type": "string"}, {"type": "null"}],
                              "description": "기사 제목의 한국어 번역. 원문이 한국어(ko)인 경우에만 null. 영어·일본어·기타 언어는 반드시 한국어로 번역"},
            "article_body_ko": {"anyOf": [{"type": "string"}, {"type": "null"}],
                              "description": "기사 본문의 한국어 번역 요약 최대 500자 (본문 없으면 null)"},
            "key_ingredients": {"anyOf": [{"type": "string"}, {"type": "null"}],
                              "description": "기사에 언급된 핵심 성분/포뮬러를 쉼표로 구분 (예: 'PDRN,센텔라'; 성분 언급 없으면 null)"},
            "sentiment":     {"type": "string", "enum": ["positive", "neutral", "negative"],
                              "description": "브랜드 관점 톤: positive=호재, neutral=중립, negative=악재(리콜·품질이슈·논란·규제·소송)"},
            "brand_focus":   {
                "type": "string",
                "enum": ["primary", "secondary", "incidental", "unrelated"],
                "description": ("기사에서 해당 브랜드의 비중: primary=주인공, secondary=주요 언급, "
                                "incidental=예시로 잠깐 언급, unrelated=이 브랜드 기사가 아님"
                                "(기사 어디에도 브랜드가 안 나옴 — 억지로 고르지 말고 이 값을 쓸 것)"),
            },
            "confidence":    {"type": "number", "description": "분류 신뢰도 0.0~1.0"},
            "note":          {"anyOf": [{"type": "string"}, {"type": "null"}],
                              "description": "추가 메모"},
        },
        "required": ["index", "brand", "country", "activity_type",
                     "importance", "strategic_score", "details", "product_name",
                     "channel", "price_info", "city", "evidence_level",
                     "title_ko", "article_body_ko", "key_ingredients", "sentiment",
                     "brand_focus", "confidence", "note"],
    }
    return {
        "type": "object",
        "properties": {"results": {"type": "array", "items": item_schema}},
        "required": ["results"],
        "additionalProperties": False,
    }


_BATCH_SCHEMA = None


def _get_batch_schema() -> dict:
    global _BATCH_SCHEMA
    if _BATCH_SCHEMA is None:
        _BATCH_SCHEMA = _make_batch_schema()
    return _BATCH_SCHEMA


def _classify_batch(
    articles: list[RawArticle],
    brand: str,
    country: str,
) -> list[tuple[int, NewsClassification]]:
    """여러 기사를 1 API 콜로 배치 분류."""
    client = _get_client()

    items_text = "\n\n".join(
        f"[{i}]\n{build_classification_prompt(a, brand, country)}"
        for i, a in enumerate(articles)
    )
    user_prompt = (
        f"다음 {len(articles)}개 기사를 각각 분류하세요. "
        f'JSON 배열로 반환: [{{"index":0,...}}, {{"index":1,...}}, ...]\n\n{items_text}'
    )

    try:
        response = _with_retry(
            client.chat.completions.create,
            model=CLASSIFIER_MODEL_DETAIL,
            messages=[
                {"role": "system", "content": CLASSIFICATION_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "BatchClassification",
                    "schema": _get_batch_schema(),
                    "strict": True,
                },
            },
            max_tokens=4096,
        )
        _track_usage(response)

        raw = json.loads(response.choices[0].message.content)
        results = []
        for item in raw.get("results", []):
            idx = item.pop("index", None)
            if idx is None or not (0 <= idx < len(articles)):
                continue
            try:
                results.append((idx, NewsClassification(**item)))
            except Exception as e:
                logger.warning("배치 항목 파싱 오류 (idx=%s): %s", idx, e)
        return results

    except QuotaExhausted:
        raise
    except Exception as e:
        logger.error("배치 분류 오류: %s", e)
        return []


# ── 공개 API ───────────────────────────────────────────────────────────────────

def classify_articles(
    articles: list[RawArticle],
    brand: str,
    country: str,
) -> list[tuple[RawArticle, NewsClassification]]:
    """
    수집 기사 2단계 분류.
    Stage1: gpt-4o-mini 필터 → Stage2: gpt-4o 배치 (BATCH_SIZE건씩 1콜)
    """
    if not articles:
        return []

    relevant_indices = _filter_relevant(articles)
    logger.info("Stage1: %d/%d건 통과 [%s/%s]",
                len(relevant_indices), len(articles), brand, country)
    if not relevant_indices:
        return []

    relevant = [articles[i] for i in relevant_indices]

    results: list[tuple[RawArticle, NewsClassification]] = []
    for batch_start in range(0, len(relevant), BATCH_SIZE):
        batch = relevant[batch_start:batch_start + BATCH_SIZE]
        for local_idx, clf in _classify_batch(batch, brand, country):
            results.append((batch[local_idx], clf))

    logger.info("Stage2: %d/%d건 완료 [%s/%s]",
                len(results), len(relevant), brand, country)
    return results
