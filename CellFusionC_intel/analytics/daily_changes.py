"""오늘 달라진 것 — 어제와 비교해 새로 생긴 일만.

첫 화면이 누적 합계('수집 1,785건')로 시작하면 어제와 거의 같은 숫자라 다시 볼
이유가 없다. 사람이 궁금한 건 합계가 아니라 **변화**다.

재료는 이미 다 있었는데 쓰지 않고 있었다 — 올리브영은 prev_rank·delta를 같이
저장하고(최근 30일 5계단 이상 변동만 1,214건), 아마존은 일별 스냅샷이 쌓여 있다.

우리 카테고리(더마·선케어)의 변화를 먼저 올린다. 셀퓨전씨가 선케어 브랜드라
'닥터지 선케어 6위 → 17위'는 같은 폭의 바디케어 변동보다 훨씬 중요하다.
"""

import logging

from sqlalchemy import text

from config.settings import DB_SCHEMA

logger = logging.getLogger(__name__)

# 셀퓨전씨가 뛰는 판. 이 카테고리 변화는 같은 폭이라도 위로 올린다.
_OUR_CATS = ("선케어", "더모", "스킨케어", "클렌징", "마스크팩")

_MIN_JUMP = 5          # 몇 계단부터 '달라졌다'고 볼 것인가


def _is_ours(cat: str) -> bool:
    return any(k in (cat or "") for k in _OUR_CATS)


def _oliveyoung(session, days: int) -> list:
    """올영 순위 급변. prev_rank·delta가 이미 저장돼 있어 그대로 쓴다."""
    try:
        rows = session.execute(text(f"""
            SELECT capture_date, brand, category, rank_position, prev_rank, delta
            FROM {DB_SCHEMA}.oliveyoung_rankings
            WHERE is_monitored AND is_ours IS NOT TRUE
              AND delta IS NOT NULL AND abs(delta) >= :j
              AND capture_date >= (SELECT max(capture_date)
                                   FROM {DB_SCHEMA}.oliveyoung_rankings) - :d
            ORDER BY abs(delta) DESC
        """), {"j": _MIN_JUMP, "d": days}).fetchall()
    except Exception as e:
        session.rollback()
        logger.warning("올영 변동 조회 실패: %s", e)
        return []
    out = []
    for d, brand, cat, now, prev, delta in rows:
        up = delta > 0
        out.append({
            "when": str(d), "brand": brand, "where": f"올리브영 {cat}",
            "cat": cat, "ours": _is_ours(cat), "up": up, "size": abs(delta),
            "text": f"{prev}위 → {now}위",
            "why": f"{abs(delta)}계단 {'올라섰다' if up else '내려갔다'}",
        })
    return out


def _retail(session, days: int) -> list:
    """아마존 등 해외 리테일. prev가 저장돼 있지 않아 직전 스냅샷과 직접 비교한다."""
    try:
        rows = session.execute(text(f"""
            WITH snap AS (
              SELECT capture_date, retailer, country, category, brand,
                     min(rank) AS rk
              FROM {DB_SCHEMA}.retail_rankings
              WHERE is_monitored AND rank IS NOT NULL
                AND capture_date >= (SELECT max(capture_date)
                                     FROM {DB_SCHEMA}.retail_rankings) - :d - 3
              GROUP BY 1,2,3,4,5
            ), paired AS (
              SELECT s.*, lag(rk) OVER (
                       PARTITION BY retailer, country, category, brand
                       ORDER BY capture_date) AS prev
              FROM snap s
            )
            SELECT capture_date, brand, country, category, rk, prev
            FROM paired
            WHERE prev IS NOT NULL AND abs(prev - rk) >= :j
              AND capture_date >= (SELECT max(capture_date)
                                   FROM {DB_SCHEMA}.retail_rankings) - :d
            ORDER BY abs(prev - rk) DESC
        """), {"j": _MIN_JUMP, "d": days}).fetchall()
    except Exception as e:
        session.rollback()
        logger.warning("해외 순위 변동 조회 실패: %s", e)
        return []
    out = []
    for d, brand, cc, cat, now, prev in rows:
        delta = prev - now                     # 순위는 작을수록 좋다
        up = delta > 0
        out.append({
            "when": str(d), "brand": brand, "where": f"{cc} 아마존 {cat}",
            "cat": cat, "ours": _is_ours(cat), "up": up, "size": abs(delta),
            "text": f"{prev}위 → {now}위",
            "why": f"{abs(delta)}계단 {'올라섰다' if up else '내려갔다'}",
        })
    return out


def _entries(session, days: int) -> list:
    """랭킹 신규 진입 — 그 브랜드가 그 판에 처음 이름을 올린 것."""
    # 주의: 브랜드를 늦게 등록하면 그 브랜드의 데이터도 늦게부터 쌓인다. 그걸
    # '새로 진입'으로 읽으면 거짓말이 된다 — 센텔리안24가 올영 스킨케어에 처음
    # 올랐다고 뜬 적이 있다. 그 카테고리를 충분히 오래 수집한 뒤에 나타난
    # 브랜드만 진입으로 본다.
    try:
        rows = session.execute(text(f"""
            WITH cat_start AS (
              SELECT category, min(capture_date) AS c0
              FROM {DB_SCHEMA}.oliveyoung_rankings GROUP BY category
            ), first_seen AS (
              SELECT brand, category, min(capture_date) AS d, min(rank_position) AS rk
              FROM {DB_SCHEMA}.oliveyoung_rankings
              WHERE is_monitored AND is_ours IS NOT TRUE
              GROUP BY brand, category
            ), brand_start AS (
              SELECT brand, min(capture_date) AS b0
              FROM {DB_SCHEMA}.oliveyoung_rankings GROUP BY brand
            )
            SELECT f.d, f.brand, f.category, f.rk
            FROM first_seen f
            JOIN cat_start c ON c.category = f.category
            JOIN brand_start b ON b.brand = f.brand
            WHERE f.d >= (SELECT max(capture_date)
                          FROM {DB_SCHEMA}.oliveyoung_rankings) - :d
              AND f.d >= c.c0 + 14      -- 그 카테고리를 2주 넘게 본 뒤에 나타났고
              AND f.d >= b.b0 + 14      -- 그 브랜드도 2주 넘게 추적한 뒤여야 한다
            ORDER BY f.rk
        """), {"d": days}).fetchall()
    except Exception as e:
        session.rollback()
        logger.warning("신규 진입 조회 실패: %s", e)
        return []
    return [{
        "when": str(d), "brand": brand, "where": f"올리브영 {cat}",
        "cat": cat, "ours": _is_ours(cat), "up": True, "size": 99,
        "text": f"{rk}위로 진입", "why": "수집 이후 이 판에서 처음 잡혔다",
    } for d, brand, cat, rk in rows]


def _news(session, days: int, limit: int) -> list:
    """새로 들어온 HIGH 기사 — 전략점수 높은 것부터."""
    try:
        # 같은 사건을 매체 5곳이 쓰면 5건이 올라온다(믹순 버블보블 협업이 그랬다).
        # is_duplicate가 못 잡는 유사 기사라 브랜드 + 제목 앞머리로 한 번 더 묶는다.
        rows = session.execute(text(f"""
            WITH pick AS (
              SELECT DISTINCT ON (brand, left(regexp_replace(
                       COALESCE(NULLIF(title_ko,''), title),
                       '[^가-힣A-Za-z0-9]', '', 'g'), 14))
                     published_date, brand, country,
                     COALESCE(NULLIF(title_ko,''), title) AS t,
                     strategic_score, source_url
              FROM {DB_SCHEMA}.news_articles
              WHERE importance = 'high' AND is_duplicate IS NOT TRUE
                AND is_self IS NOT TRUE
                AND (brand_focus NOT IN ('incidental','unrelated') OR brand_focus IS NULL)
                AND published_date >= CURRENT_DATE - :d
              ORDER BY brand, left(regexp_replace(
                         COALESCE(NULLIF(title_ko,''), title),
                         '[^가-힣A-Za-z0-9]', '', 'g'), 14),
                       strategic_score DESC NULLS LAST
            )
            SELECT published_date::date, brand, country, t, strategic_score, source_url
            FROM pick
            ORDER BY strategic_score DESC NULLS LAST, published_date DESC
            LIMIT :n
        """), {"d": days, "n": limit * 5}).fetchall()   # 유사 제거로 줄 것을 감안
    except Exception as e:
        session.rollback()
        logger.warning("신규 기사 조회 실패: %s", e)
        return []
    # 같은 사건을 매체 다섯 곳이 쓰면 다섯 건이 올라온다(믹순 버블보블 협업이 그랬다).
    # 제목 앞머리로 묶으려니 '믹순 버블-레트로 게임…'과 '믹순, 레트로 게임…'처럼
    # 어순이 달라 안 잡혔다. 같은 브랜드 안에서 제목 유사도로 한 번 더 걸러낸다.
    import re as _re
    from difflib import SequenceMatcher as _SM

    def _key(t):
        return _re.sub(r"[^가-힣A-Za-z0-9]", "", t or "")

    # 유사도만으로는 어순이 크게 다른 같은 사건을 못 잡는다('믹순 버블-레트로 게임…'
    # vs '믹순, 버블보블 협업으로…'). 이건 오늘 요약이지 기사 목록이 아니므로
    # **브랜드당 하루 한 건**으로 자른다 — 점수가 가장 높은 것만 남는다.
    out, kept, per_brand = [], [], set()
    for d, brand, country, title, sc, url in rows:
        if (brand, str(d)) in per_brand:
            continue
        k = _key(title)
        if any(_SM(None, k, kk).ratio() >= 0.6 for _b, kk in kept):
            continue
        per_brand.add((brand, str(d)))
        kept.append((brand, k))
        out.append({
            "when": str(d), "brand": brand, "where": country or "",
            "cat": "", "ours": False, "up": True, "size": int(sc or 0),
            "text": (title or "")[:90], "why": "새 소식", "url": url or "",
            "kind": "news",
        })
    return out[:limit]


def get_daily_changes(session, days: int = 1, limit: int = 9) -> dict:
    """반환 {rank:[...], news:[...], since:str}.

    rank = 순위 변화(우리 카테고리 먼저, 그다음 낙폭·상승폭 순).
    days=1이면 최신 스냅샷과 그 직전만. 주말·수집 공백을 감안해 호출부에서 넓힌다.
    """
    moves = _oliveyoung(session, days) + _retail(session, days) + _entries(session, days)
    # 우리 판의 변화를 먼저. 같은 판 안에서는 폭이 큰 것부터.
    moves.sort(key=lambda x: (not x["ours"], -x["size"]))
    seen, rank = set(), []
    for m in moves:
        key = (m["brand"], m["where"])
        if key in seen:
            continue                      # 같은 브랜드·같은 판은 한 번만
        seen.add(key)
        rank.append(m)
        if len(rank) >= limit:
            break
    return {
        "rank": rank,
        "news": _news(session, days, limit),
        "since": (rank[0]["when"] if rank else ""),
    }


if __name__ == "__main__":
    import sys

    from dotenv import load_dotenv

    from storage.models import get_session

    sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    se = get_session()
    try:
        d = get_daily_changes(se, days=1)
        print(f"순위 변화 {len(d['rank'])}건")
        for m in d["rank"]:
            mark = "★" if m["ours"] else " "
            print(f"  {mark} {m['brand']:16} {m['where']:22} {m['text']:14} {m['why']}")
        print(f"\n새 소식 {len(d['news'])}건")
        for n in d["news"][:5]:
            print(f"    {n['brand']:14} {n['where']:3} [{n['size']:3}] {n['text'][:52]}")
    finally:
        se.close()
