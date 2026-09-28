"""본문이 새로 생긴 기사 재분류 — 제목만 보고 매긴 분류를 내용 기반으로 고친다.

구글RSS 기사는 오랫동안 본문 없이 **제목 한 줄로만** 분류됐다. 2026-09-28에 본문을
백필(tools/backfill_bodies)해 89%까지 채웠으므로, 그 기사들을 다시 분류하면
brand_focus·strategic_score·importance·product_name이 내용 기반으로 바뀐다.

대상은 **본문이 새로 생긴 것만**이다. 네이버 기사는 처음부터 본문이 있었으니
다시 돌려도 입력이 같아 돈만 나간다(최근 3개월 기준 2,372건 제외).

비용 최적화(실측 기반):
  · 대상 축소 1,278 → 939건 — 네이버 제외
  · 배치 8 → 16건 — 시스템 프롬프트가 입력의 54%였다. 콜 수를 줄이는 게 본문
    자르는 것보다 효과가 크다. 32까지 올렸더니 응답 JSON이 max_tokens에서
    잘려 배치 통째로 날아갔다 — 16이 상한이다
  · 본문 1,000 → 600자 — 기사 핵심은 남기면서 입력을 줄인다
  · Stage1 필터 생략 — 이미 수집 때 통과한 기사들이다. 다시 돌릴 이유가 없다

주의: strategic_score가 바뀌면 티어 승급 기준(60점+ N건)이 움직인다. 좋아지는
방향이지만 승급·강등이 생길 수 있다.

실행:
    python -m tools.reclassify --months 3 --dry-run   # 대상·비용만 확인
    python -m tools.reclassify --months 3             # 실제 수행
"""

import logging
import sys

from sqlalchemy import text

from classifier.claude_classifier import _classify_batch, get_token_usage, reset_token_usage
from collectors.base_collector import RawArticle
from config.settings import DB_SCHEMA
from storage.models import get_session

logger = logging.getLogger(__name__)

_BATCH = 16          # 콜당 고정비를 낮추되, 응답이 max_tokens(4096)을 넘어 잘리지 않는 선
_BODY_CHARS = 600    # 분류 입력에 넣을 본문 길이
_FIELDS = ("brand_focus", "strategic_score", "importance",
           "activity_type", "product_name", "channel")


def _targets(session, months: int, limit: int) -> list:
    """본문이 있는 구글RSS 기사 — 제목만으로 분류됐던 것들."""
    sql = (f"SELECT id, brand, country, title, article_body "
           f"FROM {DB_SCHEMA}.news_articles "
           f"WHERE collector_type = 'google_rss' "
           f"  AND COALESCE(article_body, '') <> '' "
           f"  AND is_duplicate IS NOT TRUE "
           f"  AND published_date >= now() - interval '{int(months)} months' "
           f"ORDER BY published_date DESC")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return session.execute(text(sql)).fetchall()


def run(months: int = 3, limit: int = 0, dry_run: bool = False) -> dict:
    session = get_session()
    try:
        rows = _targets(session, months, limit)
        if not rows:
            logger.info("재분류 대상 없음")
            return {"total": 0}
        logger.info("재분류 대상 %d건 (배치 %d · 본문 %d자)", len(rows), _BATCH, _BODY_CHARS)
        if dry_run:
            calls = (len(rows) + _BATCH - 1) // _BATCH
            logger.info("[dry-run] 예상 API 콜 %d회 — 실제 호출하지 않음", calls)
            return {"total": len(rows), "calls": calls, "dry_run": True}

        reset_token_usage()
        changed = failed = done = 0
        diffs: dict = {f: 0 for f in _FIELDS}

        # 같은 (브랜드, 국가)끼리 묶어야 분류 프롬프트의 맥락이 맞는다
        buckets: dict = {}
        for r in rows:
            buckets.setdefault((r[1], r[2]), []).append(r)

        for (brand, country), items in buckets.items():
            for i in range(0, len(items), _BATCH):
                chunk = items[i:i + _BATCH]
                arts = [RawArticle(
                    title=r[3] or "", url="", published=None, summary="",
                    source_name="", language="", body=(r[4] or "")[:_BODY_CHARS],
                ) for r in chunk]
                try:
                    results = _classify_batch(arts, brand, country)
                except Exception as e:
                    failed += len(chunk)
                    logger.warning("배치 실패 %s/%s: %s", brand, country, str(e)[:100])
                    continue
                for idx, clf in results:
                    if not (0 <= idx < len(chunk)):
                        continue
                    aid = chunk[idx][0]
                    vals, sets = {"i": aid}, []
                    for f in _FIELDS:
                        v = getattr(clf, f, None)
                        if v is not None:
                            sets.append(f"{f} = :{f}")
                            vals[f] = v
                    if not sets:
                        continue
                    try:
                        before = session.execute(text(
                            f"SELECT {', '.join(_FIELDS)} FROM {DB_SCHEMA}.news_articles "
                            f"WHERE id = :i"), {"i": aid}).fetchone()
                        session.execute(text(
                            f"UPDATE {DB_SCHEMA}.news_articles SET {', '.join(sets)} "
                            f"WHERE id = :i"), vals)
                        for n, f in enumerate(_FIELDS):
                            if f in vals and before and before[n] != vals[f]:
                                diffs[f] += 1
                        changed += 1
                    except Exception as e:
                        session.rollback()
                        failed += 1
                        logger.warning("저장 실패 id=%s: %s", aid, str(e)[:80])
                done += len(chunk)
                session.commit()
                if done % (_BATCH * 4) == 0:
                    u = get_token_usage()
                    logger.info("  %d/%d — 갱신 %d · 실패 %d · $%.2f",
                                done, len(rows), changed, failed, u["cost_usd"])

        u = get_token_usage()
        logger.info("재분류 완료 — 대상 %d · 갱신 %d · 실패 %d · %d콜 $%.2f",
                    len(rows), changed, failed, u["calls"], u["cost_usd"])
        logger.info("  바뀐 필드: %s",
                    ", ".join(f"{f} {n}건" for f, n in diffs.items() if n))
        return {"total": len(rows), "changed": changed, "failed": failed,
                "cost_usd": u["cost_usd"], "diffs": diffs}
    finally:
        session.close()


if __name__ == "__main__":
    import argparse

    from dotenv import load_dotenv

    sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--months", type=int, default=3)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    print(run(months=a.months, limit=a.limit, dry_run=a.dry_run))
