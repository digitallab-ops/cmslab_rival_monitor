"""
기존 DB 기사 국가(country) 재분류 도구

대상: country='KR' 이면서 실제 내용이 다른 시장인 기사
방법: gpt-4o-mini로 경량 국가 판단 (country 필드만 업데이트)

사용:
    cd CellFusionC_intel
    python tools/reclassify_country.py --days 90 --dry-run   # 대상 목록만 출력
    python tools/reclassify_country.py --days 90             # 실제 업데이트
    python tools/reclassify_country.py --days 90 --brand Anua
"""

import argparse
import json
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

from sqlalchemy import text
from openai import OpenAI, RateLimitError

from storage.models import get_session, DB_SCHEMA
from config.settings import OPENAI_API_KEY
from classifier.prompts import CLASSIFICATION_SYSTEM_PROMPT

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# 크로스마켓 시그널 키워드 — 이 단어가 KR 기사 제목/details에 있으면 재분류 후보
CROSS_MARKET_KW = [
    "미국", "美", "US ", "Ulta", "Sephora", "Target", "CVS", "Walmart",
    "Amazon", "아마존", "일본", "Japan", "JP ", "유럽", "Europe", "EU ",
    "영국", "UK ", "프랑스", "France", "독일", "Germany", "호주", "Australia",
    "캐나다", "Canada", "싱가포르", "Singapore", "태국", "Thailand",
    "중국", "China", "동남아", "베트남", "인도",
    "글로벌 진출", "해외 입점", "해외 출시", "해외 오프라인",
]

SYSTEM = CLASSIFICATION_SYSTEM_PROMPT

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = OpenAI(api_key=OPENAI_API_KEY)
    return _client


def _judge_country(brand: str, source_country: str, title: str, details: str, lang: str = "") -> str | None:
    """gpt-4o-mini 경량 호출 — country 코드만 반환."""
    lang_hint = f"\n출처 언어: {lang}" if lang else ""
    user_msg = (
        f"브랜드: {brand}\n"
        f"수집 파이프라인 국가: {source_country}  (수집 경로, 내용에 따라 다른 시장으로 분류할 것)\n"
        f"출처 언어: {lang or '불명'}\n"
        f"제목: {title}\n"
        f"요약: {details or '(없음)'}\n\n"
        f"위 기사가 실제로 다루는 시장의 ISO 국가 코드만 JSON으로 반환하세요.\n"
        f"형식: {{\"country\": \"US\"}}"
    )
    delay = 10
    for attempt in range(4):
        try:
            resp = _get_client().chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": SYSTEM},
                    {"role": "user",   "content": user_msg},
                ],
                response_format={"type": "json_object"},
                temperature=0,
                max_tokens=20,
            )
            raw = resp.choices[0].message.content or ""
            return json.loads(raw).get("country")
        except RateLimitError:
            if attempt == 3:
                raise
            time.sleep(delay)
            delay *= 2
        except Exception as e:
            logger.error("GPT 오류: %s", e)
            return None
    return None


def _has_cross_market_signal(title: str, details: str) -> bool:
    text_target = (title or "") + " " + (details or "")
    return any(kw.lower() in text_target.lower() for kw in CROSS_MARKET_KW)


def run(days: int = 90, brand_filter: str = None, dry_run: bool = False, limit: int = 500):
    session = get_session()
    try:
        brand_clause = "AND brand = :brand" if brand_filter else ""
        rows = session.execute(text(f"""
            SELECT id, brand, country, source_country, title, details, language
            FROM {DB_SCHEMA}.news_articles
            WHERE country = 'KR'
              AND published_date >= NOW() - INTERVAL '{days} days'
              {brand_clause}
            ORDER BY published_date DESC
            LIMIT :limit
        """), {"brand": brand_filter, "limit": limit} if brand_filter
              else {"limit": limit}).fetchall()

        candidates = [
            r for r in rows
            if _has_cross_market_signal(r[4] or "", r[5] or "")
        ]

        logger.info("전체 KR 기사: %d건 | 크로스마켓 후보: %d건", len(rows), len(candidates))

        if dry_run:
            for r in candidates:
                print(f"  [{r[0]}] {r[1]:20} | {r[4][:80]}")
            return

        updated = 0
        for r in candidates:
            art_id, brand, old_country, src_country, title, details, lang = r
            new_country = _judge_country(
                brand=brand,
                source_country=src_country or "KR",
                title=title or "",
                details=details or "",
                lang=lang or "",
            )
            if new_country and new_country != old_country:
                session.execute(text(f"""
                    UPDATE {DB_SCHEMA}.news_articles
                    SET country = :country
                    WHERE id = :id
                """), {"country": new_country, "id": art_id})
                session.commit()
                logger.info("  [%s] %-20s  KR → %s  |  %s", art_id, brand, new_country, (title or "")[:60])
                updated += 1
            else:
                logger.debug("  [%s] %-20s  유지: %s", art_id, brand, old_country)
            time.sleep(0.3)   # rate limit 여유

        logger.info("완료: %d건 업데이트", updated)

    finally:
        session.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="기존 KR 기사 국가 재분류")
    parser.add_argument("--days",     type=int,  default=90,    help="최근 N일 대상 (기본 90)")
    parser.add_argument("--brand",    type=str,  default=None,  help="특정 브랜드만")
    parser.add_argument("--limit",    type=int,  default=500,   help="최대 처리 건수")
    parser.add_argument("--dry-run",  action="store_true",      help="실제 업데이트 없이 대상만 출력")
    args = parser.parse_args()

    run(days=args.days, brand_filter=args.brand, dry_run=args.dry_run, limit=args.limit)
