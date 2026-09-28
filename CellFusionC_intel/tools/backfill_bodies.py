"""과거 기사 본문 백필 — 구글뉴스 리디렉션 URL을 풀어 원문을 채운다.

구글RSS로 들어온 기사는 본문이 비어 있고 source_url이 news.google.com 리디렉션이다
(실측 6,564건). 수집기는 2026-09-28부터 본문을 받지만 그 이전 것은 그대로다.
이 스크립트는 그 과거분을 채운다. LLM을 쓰지 않으므로 API 비용은 들지 않는다.

분류값(strategic_score·importance·brand_focus)은 **건드리지 않는다**. 본문이
채워져도 재분류하지 않으면 지표는 그대로다 — 과거 통계가 흔들리지 않게 의도한 것이다.

중단해도 안전하다. 다시 실행하면 아직 비어 있는 것만 다시 집는다.

실행:
    python -m tools.backfill_bodies                 # 최근 것부터 전부
    python -m tools.backfill_bodies --limit 500     # 500건만
    python -m tools.backfill_bodies --months 3      # 최근 3개월만
"""

import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from sqlalchemy import text

from collectors.google_rss import fetch_article_body
from config.settings import DB_SCHEMA
from storage.models import get_session

logger = logging.getLogger(__name__)

_WORKERS = 6        # 수집기와 같은 값 — 발행사 서버 부담과 속도의 절충
_COMMIT_EVERY = 50  # 중간 커밋 간격(중단돼도 여기까지는 남는다)


def _targets(session, limit: int, months: int) -> list:
    """본문이 비어 있고 URL을 복원할 수 있는 기사 — 최근 것부터."""
    where = ["COALESCE(article_body, '') = ''", "source_url LIKE '%news.google.com%'"]
    if months:
        where.append(f"published_date >= now() - interval '{int(months)} months'")
    sql = (f"SELECT id, source_url FROM {DB_SCHEMA}.news_articles "
           f"WHERE {' AND '.join(where)} ORDER BY published_date DESC")
    if limit:
        sql += f" LIMIT {int(limit)}"
    return [(r[0], r[1]) for r in session.execute(text(sql)).fetchall()]


def run(limit: int = 0, months: int = 0) -> dict:
    session = get_session()
    sess_http = requests.Session()
    try:
        rows = _targets(session, limit, months)
        total = len(rows)
        if not total:
            logger.info("백필 대상 없음")
            return {"total": 0, "filled": 0, "url_only": 0, "failed": 0}
        logger.info("백필 시작 — 대상 %d건 (워커 %d)", total, _WORKERS)

        filled = url_only = failed = done = 0
        with ThreadPoolExecutor(max_workers=_WORKERS) as ex:
            futs = {ex.submit(fetch_article_body, sess_http, url): aid
                    for aid, url in rows}
            for fut in as_completed(futs):
                aid = futs[fut]
                done += 1
                try:
                    real, body = fut.result()
                except Exception as e:
                    failed += 1
                    logger.debug("백필 실패 id=%s: %s", aid, e)
                    continue
                if not real:
                    failed += 1
                    continue
                # URL만 복원돼도 저장한다 — 출처 링크가 구글이 아닌 실제 기사로 바뀐다
                if body:
                    session.execute(text(
                        f"UPDATE {DB_SCHEMA}.news_articles "
                        f"SET article_body = :b, source_url = :u WHERE id = :i"),
                        {"b": body, "u": real, "i": aid})
                    filled += 1
                else:
                    session.execute(text(
                        f"UPDATE {DB_SCHEMA}.news_articles "
                        f"SET source_url = :u WHERE id = :i"), {"u": real, "i": aid})
                    url_only += 1
                if done % _COMMIT_EVERY == 0:
                    session.commit()
                    logger.info("  %d/%d — 본문 %d · URL만 %d · 실패 %d",
                                done, total, filled, url_only, failed)
        session.commit()
        logger.info("백필 완료 — 대상 %d · 본문 %d · URL만 %d · 실패 %d",
                    total, filled, url_only, failed)
        return {"total": total, "filled": filled, "url_only": url_only, "failed": failed}
    finally:
        session.close()


if __name__ == "__main__":
    import argparse

    from dotenv import load_dotenv

    sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="처리 건수 상한(0=전부)")
    ap.add_argument("--months", type=int, default=0, help="최근 N개월만(0=전부)")
    a = ap.parse_args()
    print(run(limit=a.limit, months=a.months))
