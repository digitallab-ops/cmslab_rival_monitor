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


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=OPENAI_API_KEY)
    return _client


def _with_retry(fn, *args, **kwargs):
    """429 RateLimitError 시 지수 백오프로 재시도."""
    delay = RETRY_BASE_DELAY
    for attempt in range(MAX_RETRIES):
        try:
            return fn(*args, **kwargs)
        except RateLimitError:
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
        data = json.loads(response.choices[0].message.content)
        indices = data.get("relevant", [])
        return [i for i in indices if isinstance(i, int) and 0 <= i < len(articles)]

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
                         "인플루언서_협업", "투자_BD", "브랜드_마케팅", "기타"],
                "description": "활동 유형",
            },
            "importance":    {"type": "string", "enum": ["high", "medium", "low"]},
            "details":       {"type": "string", "description": "핵심 내용 2-3문장 (한국어)"},
            "product_name":  {"anyOf": [{"type": "string"}, {"type": "null"}],
                              "description": "언급된 특정 제품명 (없으면 null)"},
            "title_ko":      {"anyOf": [{"type": "string"}, {"type": "null"}],
                              "description": "기사 제목의 한국어 번역. 원문이 한국어(ko)인 경우에만 null. 영어·일본어·기타 언어는 반드시 한국어로 번역"},
            "article_body_ko": {"anyOf": [{"type": "string"}, {"type": "null"}],
                              "description": "기사 본문의 한국어 번역 요약 최대 500자 (본문 없으면 null)"},
            "confidence":    {"type": "number", "description": "분류 신뢰도 0.0~1.0"},
            "note":          {"anyOf": [{"type": "string"}, {"type": "null"}],
                              "description": "추가 메모"},
        },
        "required": ["index", "brand", "country", "activity_type",
                     "importance", "details", "product_name",
                     "title_ko", "article_body_ko", "confidence", "note"],
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
