"""
분석 쿼리 함수 모음 — 대시보드 / CLI 드릴다운용

모든 함수는 SQLAlchemy Session을 받아 순수 Python dict/list를 반환.
"""

from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from config.settings import DB_SCHEMA

try:
    from config.brands import COUNTRIES as _COUNTRIES
    _CC_NAME = {cc: (cfg.get("name") or cc) for cc, cfg in _COUNTRIES.items()}
except Exception:
    _CC_NAME = {}


def _cutoff_iso(days: int) -> str:
    return (datetime.utcnow() - timedelta(days=days)).isoformat()


def get_category_battle(session: Session, days: int = 30) -> list[dict]:
    """자사 카테고리 × 경쟁 활동 '대결 뷰'.

    우리가 파는 카테고리(CATEGORY_KEYWORDS)별로, 경쟁사 HIGH/MED 활동(중복·incidental 제외)이
    제품명·details·제목에서 매칭되는 건수 + 상위 무브먼트 반환.
    """
    from config.brands import OUR_CATEGORIES, CATEGORY_KEYWORDS
    cutoff = _cutoff_iso(days)
    rows = session.execute(text(f"""
        SELECT brand, country, activity_type, importance,
               COALESCE(strategic_score,0),
               COALESCE(product_name,''), COALESCE(details,''),
               COALESCE(NULLIF(title_ko,''), title), source_url, channel
        FROM {DB_SCHEMA}.news_articles
        WHERE (is_duplicate IS NOT TRUE)
          AND importance IN ('high','medium')
          AND (brand_focus != 'incidental' OR brand_focus IS NULL)
          AND published_date >= :cutoff
    """), {"cutoff": cutoff}).fetchall()

    result = {c: {"category": c, "total": 0, "high": 0, "moves": []} for c in OUR_CATEGORIES}
    for r in rows:
        hay = f"{r[5]} {r[6]} {r[7]}".lower()
        for cat in OUR_CATEGORIES:
            if any(kw.lower() in hay for kw in CATEGORY_KEYWORDS[cat]):
                b = result[cat]
                b["total"] += 1
                if r[3] == "high":
                    b["high"] += 1
                b["moves"].append({
                    "brand": r[0], "country": r[1], "activity_type": r[2],
                    "importance": r[3], "score": r[4] or 0,
                    "title": r[7] or "", "url": r[8] or "", "channel": r[9] or "",
                })
    # 무브먼트 스코어순 상위 5, 카테고리는 total 많은 순
    out = []
    for c in OUR_CATEGORIES:
        b = result[c]
        b["moves"].sort(key=lambda m: -m["score"])
        b["moves"] = b["moves"][:5]
        out.append(b)
    out.sort(key=lambda x: -x["total"])
    return out


# 잡음 국가코드(권역명·전역·비대상) — 플레이북에서 제외
_NON_MARKET_CC = {"", "?", "EU", "GLOBAL", "global", "NA", "LATAM", "SEA", "APAC", "ME",
                  "AF", "IN?", "other", "OTHER", "null", "None"}


def get_expansion_playbook(session: Session, days: int = 90) -> list[dict]:
    """해외 진출 플레이북 — '경쟁사는 이 시장에 이렇게 들어갔다'.

    신시장_진출·유통_채널 활동(중복·incidental 제외)을 시장(국가)별로 묶어,
    그 시장 진입에 쓰인 채널(리테일러)과 대표 무브먼트를 반환.
    우리가 해당 시장에 갈 때의 참고서로 읽는 뷰. 최근일수록 channel 필드가 풍부.
    """
    cutoff = _cutoff_iso(days)
    rows = session.execute(text(f"""
        SELECT country, brand, activity_type,
               COALESCE(channel,''), COALESCE(strategic_score,0),
               COALESCE(NULLIF(title_ko,''), title),
               source_url, published_date::date::text, importance
        FROM {DB_SCHEMA}.news_articles
        WHERE (is_duplicate IS NOT TRUE)
          AND activity_type IN ('신시장_진출','유통_채널')
          AND (brand_focus != 'incidental' OR brand_focus IS NULL)
          AND published_date >= :cutoff
        ORDER BY published_date DESC
    """), {"cutoff": cutoff}).fetchall()

    markets: dict = {}
    for r in rows:
        cc = (r[0] or "").strip()
        if cc in _NON_MARKET_CC:
            continue
        m = markets.setdefault(cc, {"country": cc, "moves": 0, "high": 0,
                                    "channels": {}, "brands": set(), "items": []})
        m["moves"] += 1
        if r[8] == "high":
            m["high"] += 1
        m["brands"].add(r[1])
        # 채널 필드(리테일러) 집계 — 진입 경로
        ch = (r[3] or "").strip()
        if ch:
            for part in ch.replace("·", ",").replace("/", ",").split(","):
                p = part.strip()
                if p:
                    m["channels"][p] = m["channels"].get(p, 0) + 1
        m["items"].append({
            "brand": r[1], "activity_type": r[2], "channel": ch,
            "score": r[4] or 0, "title": r[5] or "", "url": r[6] or "",
            "date": r[7] or "",
        })

    out = []
    for cc, m in markets.items():
        chans = sorted(m["channels"].items(), key=lambda x: -x[1])
        items = sorted(m["items"], key=lambda x: (-x["score"], x["date"]))[:5]
        out.append({
            "country": cc,
            "moves": m["moves"],
            "high": m["high"],
            "brand_count": len(m["brands"]),
            "channels": [c for c, _ in chans[:8]],
            "items": items,
        })
    out.sort(key=lambda x: (-x["moves"], -x["high"]))
    return out


def get_digest_cache(session: Session, key: str = "__DIGEST7__") -> str:
    """오늘 생성된 7일 다이제스트 내러티브 캐시 조회(없거나 오래되면 None → 재생성)."""
    try:
        row = session.execute(text(f"""
            SELECT summary FROM {DB_SCHEMA}.brand_insights
            WHERE brand = :k AND generated_at::date = CURRENT_DATE
            ORDER BY generated_at DESC LIMIT 1
        """), {"k": key}).fetchone()
        return row[0] if row else ""
    except Exception:
        return ""


def get_briefings_list(session: Session, limit: int = 24) -> list[dict]:
    """보관된 브리핑(주간/일간) 목록 — 대시보드 아카이브용. 최신순. 테이블 없으면 빈 리스트."""
    try:
        rows = session.execute(text(f"""
            SELECT kind, generated_at::text, period_from::date::text, period_to::date::text,
                   content, total, high, brands, countries, model
            FROM {DB_SCHEMA}.briefings
            ORDER BY generated_at DESC
            LIMIT :lim
        """), {"lim": max(1, min(limit, 60))}).fetchall()
    except Exception:
        return []
    return [{
        "kind": r[0], "generated_at": r[1], "period_from": r[2], "period_to": r[3],
        "content": r[4] or "", "total": r[5], "high": r[6],
        "brands": r[7], "countries": r[8], "model": r[9],
    } for r in rows]


def get_collection_stats(session: Session, days: int = 30) -> dict:
    """KPI 요약 통계 반환. 직전 동일기간(prev_*) 대비 증감 + 일자별 스파크라인 포함."""
    cutoff = _cutoff_iso(days)
    row = session.execute(
        text(f"""
            SELECT
                COUNT(*)                                              AS total,
                COUNT(*) FILTER (WHERE importance = 'high')          AS high,
                COUNT(*) FILTER (WHERE importance = 'medium')        AS medium,
                COUNT(*) FILTER (WHERE importance = 'low')           AS low,
                COUNT(DISTINCT brand)                                AS brands_active,
                COUNT(DISTINCT country)                              AS countries_active
            FROM {DB_SCHEMA}.news_articles
            WHERE (is_duplicate IS NOT TRUE) AND published_date >= :cutoff
        """),
        {"cutoff": cutoff},
    ).fetchone()

    # 직전 동일기간(이번 기간 시작 이전의 같은 길이) — 증감 화살표용 실제 baseline
    prev_start = _cutoff_iso(days * 2)
    prow = session.execute(
        text(f"""
            SELECT
                COUNT(*)                                        AS total,
                COUNT(*) FILTER (WHERE importance = 'high')     AS high,
                COUNT(DISTINCT brand)                           AS brands_active,
                COUNT(DISTINCT country)                         AS countries_active
            FROM {DB_SCHEMA}.news_articles
            WHERE (is_duplicate IS NOT TRUE)
              AND published_date >= :prev_start AND published_date < :cutoff
        """),
        {"prev_start": prev_start, "cutoff": cutoff},
    ).fetchone()

    # 일자별 수집량 스파크라인 (기간 내, 날짜 오름차순)
    srows = session.execute(
        text(f"""
            SELECT published_date::date::text AS d, COUNT(*) AS n
            FROM {DB_SCHEMA}.news_articles
            WHERE (is_duplicate IS NOT TRUE) AND published_date >= :cutoff
            GROUP BY published_date::date
            ORDER BY published_date::date
        """),
        {"cutoff": cutoff},
    ).fetchall()
    spark = [int(r[1] or 0) for r in srows]

    return {
        "total":            row[0] or 0,
        "high":             row[1] or 0,
        "medium":           row[2] or 0,
        "low":              row[3] or 0,
        "brands_active":    row[4] or 0,
        "countries_active": row[5] or 0,
        "prev_total":            (prow[0] or 0) if prow else 0,
        "prev_high":             (prow[1] or 0) if prow else 0,
        "prev_brands_active":    (prow[2] or 0) if prow else 0,
        "prev_countries_active": (prow[3] or 0) if prow else 0,
        "spark":            spark,
        "days":             days,
        "generated_at":     datetime.utcnow().isoformat(),
    }


def get_high_articles(
    session: Session,
    days: int = 30,
    brand: "str | None" = None,
    country: "str | None" = None,
    limit: int = 2500,
) -> list:
    """HIGH/MEDIUM 기사 목록 반환 (드릴다운용).

    limit: 상한. 기본 2500 — 지도 마커(전체 카운트)와 드릴다운 목록이
    어긋나지 않도록 전 국가 커버가 목표(작은 시장이 상위국에 밀려 잘리는 것 방지).
    """
    cutoff = _cutoff_iso(days)

    where_extras = ""
    params: dict = {"cutoff": cutoff, "lim": int(limit)}
    if brand:
        where_extras += " AND LOWER(brand) = :brand"
        params["brand"] = brand.lower()
    if country:
        where_extras += " AND country = :country"
        params["country"] = country.upper()

    rows = session.execute(
        text(f"""
            SELECT id, title, brand, country, activity_type,
                   details, product_name, source_url, source_name,
                   published_date, note, classification_confidence,
                   title_ko, article_body_ko, importance,
                   brand_focus, source_country,
                   COALESCE(strategic_score, 0), channel, city, price_info, evidence_level
            FROM {DB_SCHEMA}.news_articles
            WHERE (is_duplicate IS NOT TRUE) AND importance IN ('high', 'medium')
              AND (
                  brand_focus IS NULL           -- 구기사: 필터 미적용
                  OR brand_focus != 'incidental' -- 신기사: incidental 제외
                  OR importance = 'high'         -- HIGH는 incidental이어도 표시
              )
              AND published_date >= :cutoff
              {where_extras}
            ORDER BY
                CASE importance WHEN 'high' THEN 0 ELSE 1 END,
                COALESCE(strategic_score, 0) DESC,
                published_date DESC
            LIMIT :lim
        """),
        params,
    ).fetchall()

    return [
        {
            "id":               r[0],
            "title":            r[1] or "",
            "brand":            r[2] or "",
            "country":          r[3] or "",
            "activity_type":    r[4] or "",
            "details":          r[5] or "",
            "product_name":     r[6],
            "source_url":       r[7] or "",
            "source_name":      r[8] or "",
            "published_date":   r[9].isoformat() if r[9] else "",
            "note":             r[10],
            "confidence":       float(r[11]) if r[11] is not None else None,
            "title_ko":         r[12],
            "article_body_ko":  r[13],
            "importance":       r[14] or "high",
            "brand_focus":      r[15],
            "source_country":   r[16],
            "score":            r[17] or 0,
            "channel":          r[18],
            "city":             r[19],
            "price_info":       r[20],
            "evidence_level":   r[21],
        }
        for r in rows
    ]


def get_brand_country_matrix(
    session: Session, days: int = 30, top_n: int = 12, top_n_countries: int = 14
) -> dict:
    """brand × country 크로스탭 카운트 매트릭스 반환.

    컬럼(국가)은 카운트 상위 top_n_countries개만 노출 → 가로 스크롤 방지.
    NULL·빈 국가코드는 제외. 행 합계는 전체 시장 기준(노출 컬럼 합과 다를 수 있음).
    """
    cutoff = _cutoff_iso(days)

    rows = session.execute(
        text(f"""
            SELECT brand, country, COUNT(*) AS cnt
            FROM {DB_SCHEMA}.news_articles
            WHERE (is_duplicate IS NOT TRUE) AND published_date >= :cutoff
            GROUP BY brand, country
            ORDER BY brand, country
        """),
        {"cutoff": cutoff},
    ).fetchall()

    _JUNK_CC = {"", "NULL", "NONE", "N/A", "??", "XX"}

    brand_totals: dict = defaultdict(int)
    country_totals: dict = defaultdict(int)
    raw_matrix: dict = defaultdict(lambda: defaultdict(int))

    for brand_val, country_val, cnt in rows:
        cc = (country_val or "").strip().upper()
        if cc in _JUNK_CC:
            continue
        raw_matrix[brand_val][cc] += cnt
        brand_totals[brand_val] += cnt
        country_totals[cc] += cnt

    top_brands = sorted(brand_totals, key=lambda b: brand_totals[b], reverse=True)[:top_n]
    top_countries = sorted(
        country_totals, key=lambda c: country_totals[c], reverse=True
    )[:top_n_countries]

    return {
        "brands":         top_brands,
        "countries":      top_countries,
        "matrix":         {b: dict(raw_matrix[b]) for b in top_brands},
        "brand_totals":   dict(brand_totals),
        "country_totals": dict(country_totals),
        "grand_total":    sum(brand_totals.values()),
    }


def get_weekly_trend(session: Session, weeks: int = 12) -> dict:
    """주별 importance 카운트 반환 (시계열 트렌드)."""
    cutoff = (datetime.utcnow() - timedelta(weeks=weeks)).isoformat()

    rows = session.execute(
        text(f"""
            SELECT
                TO_CHAR(DATE_TRUNC('week', published_date AT TIME ZONE 'UTC'), 'IYYY"-W"IW') AS week_label,
                importance,
                COUNT(*) AS cnt
            FROM {DB_SCHEMA}.news_articles
            WHERE (is_duplicate IS NOT TRUE) AND published_date >= :cutoff
            GROUP BY week_label, importance
            ORDER BY week_label
        """),
        {"cutoff": cutoff},
    ).fetchall()

    week_set: set = set()
    raw: dict = defaultdict(lambda: defaultdict(int))
    for week_label, importance_val, cnt in rows:
        week_set.add(week_label)
        raw[week_label][importance_val] = cnt

    all_weeks = sorted(week_set)
    return {
        "weeks":  all_weeks,
        "high":   [raw[w].get("high", 0)   for w in all_weeks],
        "medium": [raw[w].get("medium", 0) for w in all_weeks],
        "low":    [raw[w].get("low", 0)    for w in all_weeks],
    }


def get_activity_distribution(session: Session, days: int = 30) -> list:
    """activity_type별 카운트 반환 (중요도 breakdown 포함)."""
    cutoff = _cutoff_iso(days)

    rows = session.execute(
        text(f"""
            SELECT
                activity_type,
                COUNT(*)                                             AS total,
                COUNT(*) FILTER (WHERE importance = 'high')         AS high,
                COUNT(*) FILTER (WHERE importance = 'medium')       AS medium,
                COUNT(*) FILTER (WHERE importance = 'low')          AS low
            FROM {DB_SCHEMA}.news_articles
            WHERE (is_duplicate IS NOT TRUE) AND published_date >= :cutoff
            GROUP BY activity_type
            ORDER BY total DESC
        """),
        {"cutoff": cutoff},
    ).fetchall()

    grand_total = sum(r[1] for r in rows) or 1
    return [
        {
            "activity_type": r[0] or "기타",
            "total":         r[1] or 0,
            "high":          r[2] or 0,
            "medium":        r[3] or 0,
            "low":           r[4] or 0,
            "pct":           round((r[1] or 0) / grand_total * 100, 1),
        }
        for r in rows
    ]


def get_brand_activity_matrix(session: Session, days: int = 30) -> list:
    """brand × activity_type 크로스탭 (브랜드별 전략 포지셔닝 차트용)."""
    cutoff = _cutoff_iso(days)

    rows = session.execute(
        text(f"""
            SELECT
                brand,
                activity_type,
                COUNT(*)                                             AS total,
                COUNT(*) FILTER (WHERE importance = 'high')         AS high,
                COUNT(*) FILTER (WHERE importance = 'medium')       AS medium,
                COUNT(*) FILTER (WHERE importance = 'low')          AS low
            FROM {DB_SCHEMA}.news_articles
            WHERE (is_duplicate IS NOT TRUE) AND published_date >= :cutoff
            GROUP BY brand, activity_type
            ORDER BY brand, total DESC
        """),
        {"cutoff": cutoff},
    ).fetchall()

    # brand → {act_type: {total, high, medium, low}}
    brand_map: dict = defaultdict(lambda: defaultdict(lambda: {"total": 0, "high": 0, "medium": 0, "low": 0}))
    for brand_val, act, total, high, med, low in rows:
        brand_map[brand_val][act or "기타"] = {
            "total":  total or 0,
            "high":   high  or 0,
            "medium": med   or 0,
            "low":    low   or 0,
        }

    return [
        {"brand": b, "activities": dict(acts)}
        for b, acts in brand_map.items()
    ]


def get_brand_high_ratio(session: Session, days: int = 30) -> list:
    """브랜드별 HIGH 비중 비교 (시그널 강도 차트용)."""
    cutoff = _cutoff_iso(days)

    rows = session.execute(
        text(f"""
            SELECT
                brand,
                COUNT(*)                                         AS total,
                COUNT(*) FILTER (WHERE importance = 'high')     AS high
            FROM {DB_SCHEMA}.news_articles
            WHERE (is_duplicate IS NOT TRUE) AND published_date >= :cutoff
            GROUP BY brand
            ORDER BY high DESC, total DESC
        """),
        {"cutoff": cutoff},
    ).fetchall()

    return [
        {
            "brand": r[0] or "",
            "total": r[1] or 0,
            "high":  r[2] or 0,
            "pct":   round((r[2] or 0) / (r[1] or 1) * 100, 1),
        }
        for r in rows
    ]


def get_brand_insights_raw(session: Session, days: int = 30) -> dict:
    """브랜드별 전략 인사이트 카드용 원자료 수집.

    반환 형식:
    {
      brand: {
        top_act: str, top_pct: float, high_pct: float,
        top_countries: [[country, count], ...],  # top-3
        articles: [{imp, date, act, title_ko, url}, ...]  # HIGH+MEDIUM top-5
      }
    }
    """
    cutoff = _cutoff_iso(days)

    # 1) brand × activity_type 카운트
    act_rows = session.execute(
        text(f"""
            SELECT brand, activity_type, COUNT(*) AS cnt
            FROM {DB_SCHEMA}.news_articles
            WHERE (is_duplicate IS NOT TRUE) AND published_date >= :cutoff
            GROUP BY brand, activity_type
            ORDER BY brand, cnt DESC
        """),
        {"cutoff": cutoff},
    ).fetchall()

    # 2) 브랜드별 총계 + HIGH 카운트
    high_rows = session.execute(
        text(f"""
            SELECT brand,
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE importance = 'high') AS high
            FROM {DB_SCHEMA}.news_articles
            WHERE (is_duplicate IS NOT TRUE) AND published_date >= :cutoff
            GROUP BY brand
        """),
        {"cutoff": cutoff},
    ).fetchall()

    # 3) 브랜드별 주력 시장 top-3
    country_rows = session.execute(
        text(f"""
            SELECT brand, country, COUNT(*) AS cnt
            FROM {DB_SCHEMA}.news_articles
            WHERE (is_duplicate IS NOT TRUE) AND published_date >= :cutoff
            GROUP BY brand, country
            ORDER BY brand, cnt DESC
        """),
        {"cutoff": cutoff},
    ).fetchall()

    # 4) HIGH+MEDIUM 기사 상위 5건 per brand (Claude 요약용)
    art_rows = session.execute(
        text(f"""
            SELECT brand, importance, activity_type,
                   COALESCE(NULLIF(title_ko,''), LEFT(NULLIF(details,''),70), title) AS title_ko,
                   source_url, published_date::date::text AS pub_date,
                   details, country, product_name
            FROM {DB_SCHEMA}.news_articles
            WHERE (is_duplicate IS NOT TRUE) AND importance IN ('high', 'medium')
              AND published_date >= :cutoff
            ORDER BY brand,
                     CASE importance WHEN 'high' THEN 0 ELSE 1 END,
                     published_date DESC
        """),
        {"cutoff": cutoff},
    ).fetchall()

    # ── 조합 ──────────────────────────────────────────────
    brand_totals: dict = {}
    for r in high_rows:
        brand_totals[r[0]] = {"total": r[1] or 0, "high": r[2] or 0}

    # top activity per brand
    brand_acts: dict = defaultdict(list)
    for r in act_rows:
        brand_acts[r[0]].append((r[1] or "기타", r[2] or 0))

    # top countries per brand
    brand_countries: dict = defaultdict(list)
    for r in country_rows:
        brand_countries[r[0]].append([r[1], r[2] or 0])

    # articles per brand (max 5)
    brand_arts: dict = defaultdict(list)
    for r in art_rows:
        b = r[0]
        if len(brand_arts[b]) < 5:
            brand_arts[b].append({
                "imp":      r[1] or "",
                "act":      r[2] or "기타",
                "title_ko": r[3] or "",
                "url":      r[4] or "",
                "details":  r[6] or "",
                "date":     r[5] or "",
                "country":  r[7] or "",
                "product":  r[8] or "",
            })

    result: dict = {}
    for brand in brand_totals:
        acts = brand_acts.get(brand, [])
        top_act, top_cnt = acts[0] if acts else ("기타", 0)
        total = brand_totals[brand]["total"] or 1
        high  = brand_totals[brand]["high"]
        result[brand] = {
            "top_act":       top_act,
            "top_pct":       round(top_cnt / total * 100),
            "high_pct":      round(high / total * 100, 1),
            "top_countries": brand_countries.get(brand, [])[:3],
            "articles":      brand_arts.get(brand, []),
        }
    return result


def get_insights_cache_by_period(
    session: Session, days: int, max_age_days: int = 7
) -> dict:
    """기간 길이(days) 기준 최근 캐시 재사용 — 정확 날짜 매칭 대신 근사.

    대시보드 프리빌드가 매일 to_date=오늘로 캐시 미스 나서 20개 브랜드×3기간
    요약을 매일 재생성하던 낭비 방지. 같은 기간 길이(±2일)로 max_age_days 내
    생성된 요약을 브랜드별 최신 1건 재사용.
    """
    rows = session.execute(
        text(f"""
            SELECT DISTINCT ON (brand)
                   brand, summary, top_act, top_pct, high_pct
            FROM {DB_SCHEMA}.brand_insights
            WHERE (to_date::date - from_date::date) BETWEEN :dmin AND :dmax
              AND generated_at >= NOW() - (:age || ' days')::interval
              AND summary IS NOT NULL AND summary != ''
            ORDER BY brand, generated_at DESC
        """),
        {"dmin": days - 2, "dmax": days + 2, "age": max_age_days},
    ).fetchall()
    return {
        r[0]: {
            "summary":  r[1] or "",
            "top_act":  r[2] or "기타",
            "top_pct":  r[3] or 0,
            "high_pct": float(r[4]) if r[4] is not None else 0.0,
        }
        for r in rows
    }


def get_insights_cache(session: Session, from_date: str, to_date: str) -> dict:
    """날짜 범위 기준 캐시 조회. {brand: {summary, top_act, top_pct, high_pct}}"""
    rows = session.execute(
        text(f"""
            SELECT brand, summary, top_act, top_pct, high_pct
            FROM {DB_SCHEMA}.brand_insights
            WHERE from_date::date = :from_date
              AND to_date::date = :to_date
        """),
        {"from_date": from_date, "to_date": to_date},
    ).fetchall()
    return {
        r[0]: {
            "summary":  r[1] or "",
            "top_act":  r[2] or "기타",
            "top_pct":  r[3] or 0,
            "high_pct": float(r[4]) if r[4] is not None else 0.0,
        }
        for r in rows
    }


def upsert_insight_cache(
    session: Session, brand: str, from_date: str, to_date: str, data: dict
) -> None:
    """브랜드 인사이트 DB에 UPSERT (brand, from_date, to_date 기준)."""
    session.execute(
        text(f"""
            INSERT INTO {DB_SCHEMA}.brand_insights
                (brand, from_date, to_date, summary, top_act, top_pct, high_pct, generated_at)
            VALUES (:brand, :from_date, :to_date, :summary, :top_act, :top_pct, :high_pct, NOW())
            ON CONFLICT (brand, from_date, to_date)
            DO UPDATE SET
                summary      = EXCLUDED.summary,
                top_act      = EXCLUDED.top_act,
                top_pct      = EXCLUDED.top_pct,
                high_pct     = EXCLUDED.high_pct,
                generated_at = EXCLUDED.generated_at
        """),
        {
            "brand":     brand,
            "from_date": from_date,
            "to_date":   to_date,
            "summary":   data.get("summary", ""),
            "top_act":   data.get("top_act", "기타"),
            "top_pct":   int(data.get("top_pct", 0)),
            "high_pct":  float(data.get("high_pct", 0.0)),
        },
    )
    session.commit()


def get_brand_insights_raw_by_range(session: Session, from_date: str, to_date: str) -> dict:
    """명시적 날짜 범위 기반 브랜드 인사이트 원자료 (API 엔드포인트용)."""
    params = {"from_date": from_date, "to_date": to_date}
    date_filter = "published_date::date >= :from_date AND published_date::date <= :to_date"

    act_rows = session.execute(
        text(f"""
            SELECT brand, activity_type, COUNT(*) AS cnt
            FROM {DB_SCHEMA}.news_articles
            WHERE {date_filter}
            GROUP BY brand, activity_type
            ORDER BY brand, cnt DESC
        """), params,
    ).fetchall()

    high_rows = session.execute(
        text(f"""
            SELECT brand,
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE importance = 'high') AS high
            FROM {DB_SCHEMA}.news_articles
            WHERE {date_filter}
            GROUP BY brand
        """), params,
    ).fetchall()

    country_rows = session.execute(
        text(f"""
            SELECT brand, country, COUNT(*) AS cnt
            FROM {DB_SCHEMA}.news_articles
            WHERE {date_filter}
            GROUP BY brand, country
            ORDER BY brand, cnt DESC
        """), params,
    ).fetchall()

    art_rows = session.execute(
        text(f"""
            SELECT brand, importance, activity_type,
                   COALESCE(NULLIF(title_ko,''), LEFT(NULLIF(details,''),70), title) AS title_ko,
                   source_url, published_date::date::text AS pub_date, details,
                   country, product_name
            FROM {DB_SCHEMA}.news_articles
            WHERE (is_duplicate IS NOT TRUE) AND importance IN ('high', 'medium')
              AND {date_filter}
            ORDER BY brand,
                     CASE importance WHEN 'high' THEN 0 ELSE 1 END,
                     published_date DESC
        """), params,
    ).fetchall()

    brand_totals: dict = {r[0]: {"total": r[1] or 0, "high": r[2] or 0} for r in high_rows}
    brand_acts: dict = defaultdict(list)
    for r in act_rows:
        brand_acts[r[0]].append((r[1] or "기타", r[2] or 0))
    brand_countries: dict = defaultdict(list)
    for r in country_rows:
        brand_countries[r[0]].append([r[1], r[2] or 0])
    brand_arts: dict = defaultdict(list)
    for r in art_rows:
        b = r[0]
        if len(brand_arts[b]) < 5:
            brand_arts[b].append({
                "imp": r[1] or "", "act": r[2] or "기타",
                "title_ko": r[3] or "", "url": r[4] or "",
                "details": r[6] or "", "date": r[5] or "",
                "country": r[7] or "", "product": r[8] or "",
            })

    result: dict = {}
    for brand in brand_totals:
        acts = brand_acts.get(brand, [])
        top_act, top_cnt = acts[0] if acts else ("기타", 0)
        total = brand_totals[brand]["total"] or 1
        result[brand] = {
            "top_act":       top_act,
            "top_pct":       round(top_cnt / total * 100),
            "high_pct":      round(brand_totals[brand]["high"] / total * 100, 1),
            "top_countries": brand_countries.get(brand, [])[:3],
            "articles":      brand_arts.get(brand, []),
        }
    return result


# ── 브랜드×국가 인사이트 (히트맵 셀 드릴다운) ────────────────────────────────

def get_brand_country_insight_cache(
    session: Session, brand: str, country: str, from_date: str, to_date: str
) -> "dict | None":
    """브랜드×국가×날짜범위 캐시 조회. 없으면 None."""
    row = session.execute(
        text(f"""
            SELECT summary, high_count, med_count
            FROM {DB_SCHEMA}.brand_country_insights
            WHERE brand = :brand
              AND country = :country
              AND from_date::date = :from_date
              AND to_date::date = :to_date
        """),
        {"brand": brand, "country": country, "from_date": from_date, "to_date": to_date},
    ).fetchone()
    if not row or not (row[0] or "").strip():
        return None
    return {
        "summary":    row[0],
        "high_count": row[1] or 0,
        "med_count":  row[2] or 0,
    }


def upsert_brand_country_insight(
    session: Session, brand: str, country: str,
    from_date: str, to_date: str, data: dict
) -> None:
    """브랜드×국가 인사이트 UPSERT (brand, country, from_date, to_date 기준)."""
    session.execute(
        text(f"""
            INSERT INTO {DB_SCHEMA}.brand_country_insights
                (brand, country, from_date, to_date, summary, high_count, med_count, generated_at)
            VALUES (:brand, :country, :from_date, :to_date, :summary, :high_count, :med_count, NOW())
            ON CONFLICT (brand, country, from_date, to_date)
            DO UPDATE SET
                summary      = EXCLUDED.summary,
                high_count   = EXCLUDED.high_count,
                med_count    = EXCLUDED.med_count,
                generated_at = EXCLUDED.generated_at
        """),
        {
            "brand":      brand,
            "country":    country,
            "from_date":  from_date,
            "to_date":    to_date,
            "summary":    data.get("summary", ""),
            "high_count": int(data.get("high_count", 0)),
            "med_count":  int(data.get("med_count", 0)),
        },
    )
    session.commit()


def get_brand_country_articles(
    session: Session, brand: str, country: str, from_date: str, to_date: str
) -> list:
    """해당 브랜드×국가의 HIGH/MED 기사 (요약 입력용). [{imp, act, title_ko, details, date}]"""
    rows = session.execute(
        text(f"""
            SELECT importance, activity_type,
                   COALESCE(NULLIF(title_ko,''), LEFT(NULLIF(details,''),70), title) AS title_ko,
                   published_date::date::text AS pub_date, details
            FROM {DB_SCHEMA}.news_articles
            WHERE (is_duplicate IS NOT TRUE) AND importance IN ('high', 'medium')
              AND LOWER(brand) = :brand
              AND country = :country
              AND published_date::date >= :from_date
              AND published_date::date <= :to_date
            ORDER BY CASE importance WHEN 'high' THEN 0 ELSE 1 END,
                     published_date DESC
            LIMIT 12
        """),
        {"brand": brand.lower(), "country": country.upper(),
         "from_date": from_date, "to_date": to_date},
    ).fetchall()
    return [
        {
            "imp":      r[0] or "",
            "act":      r[1] or "기타",
            "title_ko": r[2] or "",
            "date":     r[3] or "",
            "details":  r[4] or "",
        }
        for r in rows
    ]


def get_country_signal_stats(session: Session, days: int = 30) -> dict:
    """국가별 신호 통계 반환 (세계지도용). {CC: {total, high, medium}}

    드릴다운(get_high_articles)과 동일 기준으로 집계 → '마커는 있는데 눌러도 없음'
    불일치 제거. HIGH/MEDIUM만, incidental medium 제외, 정상 2자리 국가코드만.
    total = high + medium(클릭 가능한 건수).
    """
    cutoff = _cutoff_iso(days)
    rows = session.execute(
        text(f"""
            SELECT country,
                   COUNT(*) FILTER (WHERE importance = 'high') AS high,
                   COUNT(*) FILTER (WHERE importance = 'medium'
                       AND (brand_focus IS NULL OR brand_focus != 'incidental')) AS medium
            FROM {DB_SCHEMA}.news_articles
            WHERE (is_duplicate IS NOT TRUE) AND published_date >= :cutoff
              AND importance IN ('high', 'medium')
              AND country ~ '^[A-Z]{{2}}$'          -- null·global·LATAM 등 오분류 코드 제외
              AND (brand_focus IS NULL OR brand_focus != 'incidental' OR importance = 'high')
            GROUP BY country
        """),
        {"cutoff": cutoff},
    ).fetchall()
    out = {}
    for r in rows:
        hi, med = r[1] or 0, r[2] or 0
        if hi + med == 0:
            continue
        out[r[0]] = {"total": hi + med, "high": hi, "medium": med}
    return out


def get_ingredient_trends(session: Session, days: int = 30, limit: int = 15) -> list[dict]:
    """경쟁사 성분·포뮬러 지형 — 최근 N일 언급 성분 집계.

    반환: [{ingredient, mentions, brand_cnt, brands:[...]}], 언급수 desc.
    key_ingredients(쉼표구분)를 unnest → 성분별 언급수·주도 브랜드. incidental 제외.
    """
    cutoff = _cutoff_iso(days)
    try:
        rows = session.execute(text(f"""
            WITH ing AS (
                SELECT brand,
                       btrim(unnest(string_to_array(key_ingredients, ','))) AS ingredient
                FROM {DB_SCHEMA}.news_articles
                WHERE (is_duplicate IS NOT TRUE) AND published_date >= :cutoff
                  AND key_ingredients IS NOT NULL AND key_ingredients <> ''
                  AND (brand_focus IS NULL OR brand_focus != 'incidental')
            )
            SELECT ingredient, COUNT(*) AS mentions,
                   COUNT(DISTINCT brand) AS brand_cnt,
                   array_agg(DISTINCT brand) AS brands
            FROM ing
            WHERE char_length(ingredient) >= 2
            GROUP BY ingredient
            ORDER BY mentions DESC, brand_cnt DESC
            LIMIT :lim
        """), {"cutoff": cutoff, "lim": limit}).fetchall()
    except Exception:
        return []
    return [{"ingredient": r[0], "mentions": r[1] or 0,
             "brand_cnt": r[2] or 0, "brands": list(r[3] or [])} for r in rows]


def get_negative_signals(session: Session, days: int = 30, limit: int = 20) -> list[dict]:
    """경쟁사 악재(negative sentiment) 목록 — '기회 신호'. incidental 제외, HIGH·MED만."""
    cutoff = _cutoff_iso(days)
    try:
        rows = session.execute(text(f"""
            SELECT brand, country, activity_type,
                   COALESCE(NULLIF(title_ko,''), title) AS title,
                   details, source_url, published_date::date::text, importance
            FROM {DB_SCHEMA}.news_articles
            WHERE (is_duplicate IS NOT TRUE) AND sentiment = 'negative'
              AND published_date >= :cutoff
              AND (brand_focus IS NULL OR brand_focus != 'incidental')
              AND importance IN ('high', 'medium')
            ORDER BY CASE importance WHEN 'high' THEN 0 ELSE 1 END, published_date DESC
            LIMIT :lim
        """), {"cutoff": cutoff, "lim": limit}).fetchall()
    except Exception:
        return []
    return [{"brand": r[0], "country": r[1], "activity_type": r[2], "title": r[3] or "",
             "details": r[4] or "", "source_url": r[5] or "", "date": r[6] or "",
             "importance": r[7] or ""} for r in rows]


def get_retail_performance(session: Session, days: int = 21) -> dict:
    """브랜드별 아마존 성과 — 서사의 '잘 나간다' 실측. 핵심영역(선·BB·베이스) 특화 강조.

    반환: {brand: {overall best fields.., is_core, core:{category,rank,rating,reviews,product}|None}}.
    overall=전 카테고리 최고순위, core=셀퓨전씨 핵심영역 내 최고순위(있으면). 없으면 {}.
    """
    cutoff = _cutoff_iso(days)
    try:
        rows = session.execute(text(f"""
            SELECT brand, country, category, product_name, rank, rating,
                   review_count, product_url, COALESCE(is_core, FALSE)
            FROM {DB_SCHEMA}.retail_rankings
            WHERE brand IS NOT NULL AND rank IS NOT NULL
              AND capture_date = (SELECT MAX(capture_date) FROM {DB_SCHEMA}.retail_rankings
                                  WHERE capture_date >= :cutoff)
        """), {"cutoff": cutoff}).fetchall()
    except Exception:
        return {}
    by_brand: dict = {}
    for r in rows:
        item = {"country": r[1], "category": r[2], "product": r[3], "rank": r[4],
                "rating": float(r[5]) if r[5] is not None else None,
                "review_count": r[6], "url": r[7] or "", "is_core": bool(r[8])}
        by_brand.setdefault(r[0], []).append(item)
    out: dict = {}
    for brand, items in by_brand.items():
        best = min(items, key=lambda x: x["rank"])
        cores = [x for x in items if x["is_core"]]
        core = min(cores, key=lambda x: x["rank"]) if cores else None
        by_cc: dict = {}
        for it in items:
            cc = it["country"]
            if cc not in by_cc or it["rank"] < by_cc[cc]["rank"]:
                by_cc[cc] = it
        out[brand] = {**best, "core": core, "by_country": by_cc, "by_category": items}
    return out


def get_retail_landscape(session: Session, days: int = 21, limit: int = 20) -> list[dict]:
    """아마존 리테일 지형 — 카테고리별 상위 K뷰티 제품(모니터링+메이저). 대시보드용."""
    cutoff = _cutoff_iso(days)
    try:
        rows = session.execute(text(f"""
            WITH recent AS (
                SELECT category, brand, is_monitored, product_name, rank, rating, review_count, product_url,
                       ROW_NUMBER() OVER (PARTITION BY category, brand, product_name
                           ORDER BY capture_date DESC) rn
                FROM {DB_SCHEMA}.retail_rankings
                WHERE capture_date >= :cutoff AND rank IS NOT NULL
            )
            SELECT category, brand, is_monitored, product_name, rank, rating, review_count, product_url
            FROM recent WHERE rn = 1
            ORDER BY rank ASC
            LIMIT :lim
        """), {"cutoff": cutoff, "lim": limit}).fetchall()
    except Exception:
        return []
    return [{"category": r[0], "brand": r[1], "is_monitored": bool(r[2]), "product": r[3],
             "rank": r[4], "rating": float(r[5]) if r[5] is not None else None,
             "review_count": r[6], "url": r[7] or ""} for r in rows]


_OUR_AREA = {"선케어", "BB크림", "CC크림", "파운데이션", "틴티드모이스처", "DD크림"}
_BROAD_CATS = {"뷰티"}   # 광역 노드(전문 카테고리 아님 — 순위 해석 주의)
_TREND_ING = {"PDRN", "엑소좀", "콜라겐", "레티놀", "센텔라", "나이아신아마이드",
              "시카", "펩타이드", "히알루론산"}


def _why_drivers(move: dict, perf: dict, ingredients: list) -> list:
    """순위/화제가 '왜 높은지'를 다른 신호로 설명 — 퍼즐 시너지. 최대 3개."""
    d = []
    act = (move or {}).get("activity_type", "")
    rt = (perf or {}).get("retail") or {}
    _act_label = {"신제품_런칭": "🆕 신제품 출시", "인플루언서_협업": "📱 인플루언서 협업",
                  "유통_채널": "🏪 신규 유통 입점", "신시장_진출": "🌏 신시장 진출",
                  "가격_프로모션": "🏷 프로모션·가격", "투자_BD": "💰 투자·BD"}
    if act in _act_label:
        d.append(_act_label[act])
    if perf.get("search_spike"):
        d.append(f"🔍 검색 {perf['search_spike']}배↑")
    for i in (ingredients or []):
        if any(t in i for t in _TREND_ING):
            d.append(f"🧪 트렌드성분 {i}")
            break
    rev, rate = rt.get("reviews"), rt.get("rating")
    if rev and rev >= 10000 and rate and rate >= 4.5:
        d.append("⭐ 리뷰 축적 스테디셀러")
    elif rev is not None and rev < 1500 and rt.get("rank") and rt["rank"] <= 20:
        d.append("🚀 신상 급부상")
    if perf.get("export_yoy") is not None and perf["export_yoy"] >= 15:
        d.append(f"📦 수출 +{perf['export_yoy']:.0f}%")
    if perf.get("momentum") and perf["momentum"] >= 2:
        d.append("📈 모멘텀 급등")
    return d[:3] or ["관찰 초기 신호"]


def _signal_read(sc, n_arts, spike, mom, retail) -> dict:
    """리테일↔뉴스↔검색 교차 판독 — 신호 연결에서 나오는 한 줄 인사이트.

    반환: {label, tone, why}. tone: hot(3박자)/opp(PR우세=기회)/stealth(숨은강자)/grow(성장중).
    """
    strong_news = (sc or 0) >= 60 or (n_arts or 0) >= 4
    hot_demand = (spike and spike >= 1.5) or (mom and mom >= 1.5)
    has_retail = bool(retail and retail.get("rank") and retail["rank"] <= 50)
    buzz = strong_news or hot_demand
    if buzz and has_retail:
        return {"tone": "hot", "label": "🔥 실판매까지 검증된 급상승",
                "why": "뉴스·검색·리테일 3박자 — 즉시 대응"}
    if buzz and not has_retail:
        return {"tone": "opp", "label": "📢 화제성 대비 실판매 약함(PR 우세)",
                "why": "발표·검색은 뜨나 아마존 순위권 밖 — 우리가 실판매로 추월할 기회"}
    if has_retail and not buzz:
        return {"tone": "stealth", "label": "🥷 조용한 실판매 강자",
                "why": "보도 잠잠한데 리테일 상위 — 과소평가, 경계 필요"}
    return {"tone": "grow", "label": "🌱 성장 초기 신호",
            "why": "단일 축 신호 — 추이 관찰"}


def get_opportunity_stories(session: Session, days: int = 30, limit: int = 8) -> list[dict]:
    """핵심 서사 체인 합성 — (브랜드×국가)별 '무브→제품/성분→성과프록시'를 한 카드로.

    체인: 어느 나라 · 어느 브랜드 · 어떻게(무브) · 무슨 제품/성분 · 잘 나가나(성과프록시).
    '우리가 할 것'(action)은 상위 레이어(S2, AI)에서 채움. 여기선 결정적 데이터만.
    반환: [{brand, country, country_name, move:{...}, products:[...], ingredients:[...],
            has_negative, perf:{search_spike, export_yoy, momentum}, opp_score}]
    """
    cutoff = _cutoff_iso(days)
    try:
        rows = session.execute(text(f"""
            WITH arts AS (
                SELECT brand, country, activity_type, importance,
                       COALESCE(strategic_score, 0) AS sc,
                       COALESCE(NULLIF(title_ko,''), title) AS title,
                       details, source_url,
                       published_date::date::text AS pub_date,
                       product_name, key_ingredients,
                       ROW_NUMBER() OVER (PARTITION BY brand, country
                           ORDER BY (importance='high') DESC,
                                    COALESCE(strategic_score,0) DESC,
                                    published_date DESC) AS rn,
                       SUM(COALESCE(strategic_score,0)) OVER (PARTITION BY brand, country) AS cell_score,
                       COUNT(*) OVER (PARTITION BY brand, country) AS n_arts,
                       BOOL_OR(sentiment='negative') OVER (PARTITION BY brand, country) AS has_neg
                FROM {DB_SCHEMA}.news_articles
                WHERE (is_duplicate IS NOT TRUE) AND published_date >= :cutoff
                  AND importance IN ('high','medium')
                  AND (brand_focus IS NULL OR brand_focus != 'incidental')
                  AND country ~ '^[A-Z]{{2}}$'
            )
            SELECT brand, country, activity_type, importance, sc, title, details,
                   source_url, pub_date, product_name, key_ingredients,
                   cell_score, n_arts, has_neg
            FROM arts WHERE rn = 1
            ORDER BY cell_score DESC
            LIMIT :lim
        """), {"cutoff": cutoff, "lim": limit}).fetchall()
    except Exception:
        return []
    if not rows:
        return []

    cells = [(r[0], r[1]) for r in rows]
    # 셀별 제품·성분 집계(대표 기사 외 포함)
    prod_map: dict = {}
    ing_map: dict = {}
    try:
        for b, c in cells:
            agg = session.execute(text(f"""
                SELECT
                  array_agg(DISTINCT product_name) FILTER (WHERE product_name IS NOT NULL
                      AND product_name<>'' AND lower(product_name) NOT IN ('null','none','n/a')),
                  string_agg(DISTINCT key_ingredients, ',') FILTER (WHERE key_ingredients IS NOT NULL AND key_ingredients<>'')
                FROM {DB_SCHEMA}.news_articles
                WHERE (is_duplicate IS NOT TRUE) AND published_date >= :cutoff
                  AND brand=:b AND country=:c AND importance IN ('high','medium')
                  AND (brand_focus IS NULL OR brand_focus!='incidental')
            """), {"cutoff": cutoff, "b": b, "c": c}).fetchone()
            prod_map[(b, c)] = list(agg[0] or [])[:4] if agg else []
            ings = []
            if agg and agg[1]:
                seen = set()
                for tok in str(agg[1]).split(","):
                    t = tok.strip()
                    if t and t.lower() not in seen:
                        seen.add(t.lower()); ings.append(t)
            ing_map[(b, c)] = ings[:6]
    except Exception:
        pass

    # 성과 프록시 인덱싱 (있으면 붙이고 없으면 생략)
    export_by_cc: dict = {}
    try:
        for g in get_market_export_growth(session, hs_like="3304%", trailing=3):
            export_by_cc[g["country_code"]] = g.get("yoy_pct")
    except Exception:
        pass
    spike_by_brand: dict = {}
    try:
        for sp in get_google_spikes(session):
            # 브랜드별 최대 급등비 보관(지역 무관 대표값)
            b = sp.get("brand"); r = sp.get("spike_ratio")
            if b and (b not in spike_by_brand or (r or 0) > spike_by_brand[b]):
                spike_by_brand[b] = r
    except Exception:
        pass
    mom_by_brand: dict = {}
    try:
        for m in compute_brand_momentum(session):
            mom_by_brand[m["brand"]] = m.get("momentum")
    except Exception:
        pass
    retail_by_brand = get_retail_performance(session)   # 실측 '잘 나간다'

    stories = []
    for r in rows:
        (brand, country, act, imp, sc, title, details, url, date,
         product_name, key_ing, cell_score, n_arts, has_neg) = r
        cc = country
        spike = spike_by_brand.get(brand)
        exp = export_by_cc.get(cc)
        mom = mom_by_brand.get(brand)
        _rb = retail_by_brand.get(brand)
        # 스토리 국가에 리테일 데이터 있으면 그 시장 순위 우선, 없으면 전체 최고(프록시)
        retail = None
        if _rb:
            retail = dict((_rb.get("by_country") or {}).get(cc) or _rb)
            retail["core"] = _rb.get("core")   # 핵심영역 신호는 브랜드 단위 유지
        # 기회 스코어: 무브 강도(대표 스코어) + 활동 폭(캡) + 수요(검색) + 성과(수출) + 모멘텀 + 리테일 실순위
        # cell_score 합계는 KR 대량기사에 쏠려서 대표스코어+활동폭으로 재균형
        opp = (sc or 0) * 1.0 + min(n_arts or 0, 6) * 6
        if spike and spike >= 1.5:  opp += min((spike - 1) * 20, 40)
        if exp and exp >= 15:       opp += min(exp * 0.4, 30)
        if mom and mom >= 1.3:      opp += min((mom - 1) * 25, 25)
        if retail and retail.get("rank"):   # 실판매 순위 = 가장 강한 성과신호
            opp += max(0, 45 - retail["rank"])   # 아마존 카테고리 1위 근접일수록 큰 가점
            _rc = retail.get("core")
            if _rc and _rc.get("rank"):          # 핵심영역(선·BB) 순위 = 우리 텃밭 → 추가 가중
                opp += max(0, 30 - _rc["rank"]) * 1.4
        # 시장 포지션: 우리영역 vs 확장후보 (브랜드 전 카테고리 순위)
        areas = {"our": [], "expansion": []}
        for it in ((_rb or {}).get("by_category") or []):
            tgt = "our" if it["category"] in _OUR_AREA else "expansion"
            areas[tgt].append({"country": it["country"], "category": it["category"],
                               "rank": it["rank"], "is_broad": it["category"] in _BROAD_CATS})
        areas["our"].sort(key=lambda z: z["rank"])
        areas["expansion"].sort(key=lambda z: z["rank"])

        perf = {"search_spike": round(spike, 1) if spike else None,
                "export_yoy": round(exp, 0) if exp is not None else None,
                "momentum": round(mom, 2) if mom else None,
                "retail": ({"category": retail["category"], "rank": retail["rank"],
                            "country": retail.get("country", ""),
                            "is_broad": retail.get("category", "") in _BROAD_CATS,
                            "rating": retail["rating"], "reviews": retail["review_count"],
                            "product": retail["product"], "url": retail["url"],
                            "is_core": retail.get("is_core", False),
                            "core": ({"category": retail["core"]["category"],
                                      "rank": retail["core"]["rank"],
                                      "rating": retail["core"]["rating"],
                                      "reviews": retail["core"]["review_count"]}
                                     if retail.get("core") else None)}
                           if retail and retail.get("rank") else None)}
        story = {
            "brand": brand, "country": cc,
            "country_name": _CC_NAME.get(cc, cc),
            "move": {"activity_type": act, "importance": imp, "title": title or "",
                     "details": details or "", "url": url or "", "date": date or "",
                     "score": sc or 0},
            "products": prod_map.get((brand, cc), []),
            "ingredients": ing_map.get((brand, cc), []),
            "has_negative": bool(has_neg),
            "n_moves": n_arts or 0,
            "signal_read": _signal_read(sc, n_arts, spike, mom, retail),
            "perf": perf,
            "retail_areas": areas,
            "opp_score": round(opp, 1),
        }
        story["why"] = _why_drivers(story["move"], perf, story["ingredients"])
        stories.append(story)
    stories.sort(key=lambda s: s["opp_score"], reverse=True)
    return stories


def get_market_export_growth(session: Session, hs_like: str = "330499",
                             trailing: int = 3) -> list[dict]:
    """
    관세청 화장품 수출 YoY — 국가별 최근 trailing개월 합 vs 전년 동기.

    hs_like: '330499'(스킨케어·기타, 기본) / '3304%'(화장품 전체). export_stats 없으면 [].
    반환: [{country_code, country_name, exp_usd_3m, prev_usd_3m, yoy_pct}], 수출액 desc.
    """
    try:
        rows = session.execute(text(f"""
            WITH mx AS (SELECT MAX(period) m FROM {DB_SCHEMA}.export_stats),
            cur AS (
                SELECT country_code, MAX(country_name) country_name, SUM(exp_usd)::float e
                FROM {DB_SCHEMA}.export_stats, mx
                WHERE hs_cd LIKE :hs AND period > (m - make_interval(months => :t))
                GROUP BY country_code),
            prv AS (
                SELECT country_code, SUM(exp_usd)::float e
                FROM {DB_SCHEMA}.export_stats, mx
                WHERE hs_cd LIKE :hs
                  AND period > (m - make_interval(months => :t12))
                  AND period <= (m - make_interval(months => 12))
                GROUP BY country_code)
            SELECT cur.country_code, cur.country_name, cur.e, COALESCE(prv.e, 0)
            FROM cur LEFT JOIN prv USING (country_code)
            ORDER BY cur.e DESC
        """), {"hs": hs_like, "t": trailing, "t12": trailing + 12}).fetchall()
    except Exception:
        return []

    out = []
    for cc, name, cur_e, prv_e in rows:
        cur_e = float(cur_e or 0)
        prv_e = float(prv_e or 0)
        yoy = round((cur_e / prv_e - 1) * 100, 1) if prv_e > 0 else None
        out.append({
            "country_code": cc, "country_name": name,
            "exp_usd_3m": cur_e, "prev_usd_3m": prv_e, "yoy_pct": yoy,
        })
    return out


# 성장 '왜'를 설명하는 전략 활동 유형 (실적공시·기타 제외)
_STORY_ACTS = ("신시장_진출", "유통_채널", "신제품_런칭", "브랜드_마케팅",
               "인플루언서_협업", "투자_BD", "가격_프로모션")


def get_google_spikes(session: Session, spike_ratio: float = 1.5,
                      floor: float = 20.0) -> list[dict]:
    """
    구글 트렌드 검색 급등 감지 — (브랜드×지역)별 최근 7일 평균 vs 직전 28일 평균.

    글로벌·US·JP 시장별 검색 급등 = 뭔가 터지는 조짐(수출·진출 선행). google_trends 없으면 [].
    반환: spike_ratio 이상 & 최근값 floor 이상만. 급등 강도순.
    """
    try:
        rows = session.execute(text(f"""
            WITH ranked AS (
                SELECT brand, geo, period, ratio,
                       ROW_NUMBER() OVER (PARTITION BY brand, geo ORDER BY period DESC) rn
                FROM {DB_SCHEMA}.google_trends
                WHERE brand IS NOT NULL
            )
            SELECT brand, geo,
                   AVG(ratio) FILTER (WHERE rn <= 7)                AS recent,
                   AVG(ratio) FILTER (WHERE rn BETWEEN 8 AND 35)    AS baseline
            FROM ranked GROUP BY brand, geo
        """)).fetchall()
    except Exception:
        return []

    out = []
    for brand, geo, recent, baseline in rows:
        recent = float(recent or 0)
        baseline = float(baseline or 0)
        if recent < floor or baseline < 1:
            continue
        ratio = round(recent / baseline, 2)
        if ratio >= spike_ratio:
            out.append({"brand": brand, "geo": geo, "recent": round(recent, 1),
                        "baseline": round(baseline, 1), "spike_ratio": ratio})
    out.sort(key=lambda x: x["spike_ratio"], reverse=True)
    return out


def get_trademark_signals(session: Session, months: int = 18, limit: int = 24) -> dict:
    """
    해외 상표 출원 = 진출 선행신호(KIPRIS). 자기출원(is_own)·화장품류(is_cosmetic)만.

    trademark_filings 없으면 {feed:[], brands:[]}.
    반환: {feed:[{brand,country,mark,date}], brands:[{brand,country,recent,total,latest}]}
    """
    try:
        feed_rows = session.execute(text(f"""
            SELECT brand, country, mark_name, app_date
            FROM {DB_SCHEMA}.trademark_filings
            WHERE is_own AND is_cosmetic AND app_date IS NOT NULL
              AND app_date >= (CURRENT_DATE - make_interval(months => :m))
            ORDER BY app_date DESC LIMIT :lim
        """), {"m": months, "lim": limit}).fetchall()
        brand_rows = session.execute(text(f"""
            SELECT brand, country,
                   COUNT(*) FILTER (WHERE app_date >= (CURRENT_DATE - make_interval(months => :m))) recent,
                   COUNT(*) total, MAX(app_date) latest
            FROM {DB_SCHEMA}.trademark_filings
            WHERE is_own AND is_cosmetic
            GROUP BY brand, country
            HAVING MAX(app_date) IS NOT NULL
            ORDER BY recent DESC, latest DESC
        """), {"m": months}).fetchall()
    except Exception:
        return {"feed": [], "brands": []}

    feed = [{"brand": r[0], "country": r[1], "mark": r[2], "date": str(r[3])}
            for r in feed_rows]
    brands = [{"brand": r[0], "country": r[1], "recent": r[2] or 0,
               "total": r[3] or 0, "latest": str(r[4]) if r[4] else None}
              for r in brand_rows]
    return {"feed": feed, "brands": brands}


def get_competitor_financials(session: Session) -> list[dict]:
    """
    경쟁사 실적(DART) — 브랜드별 최신 매출·영업이익·영업이익률 + 매출 YoY.

    competitor_financials 없으면 []. is_brand_level=False면 수치는 '회사 전체'.
    반환: 최신연도 매출 desc. [{brand, corp_name, stock_code, is_brand_level,
          year, revenue, op_income, opm, prev_revenue, rev_yoy_pct}]
    """
    try:
        rows = session.execute(text(f"""
            WITH ranked AS (
                SELECT brand, corp_name, stock_code, is_brand_level, bsns_year,
                       revenue, op_income, net_income,
                       ROW_NUMBER() OVER (PARTITION BY brand ORDER BY bsns_year DESC) rn
                FROM {DB_SCHEMA}.competitor_financials
                WHERE revenue IS NOT NULL
            )
            SELECT c.brand, c.corp_name, c.stock_code, c.is_brand_level, c.bsns_year,
                   c.revenue, c.op_income, p.revenue
            FROM ranked c
            LEFT JOIN ranked p ON p.brand = c.brand AND p.rn = 2
            WHERE c.rn = 1
            ORDER BY c.revenue DESC
        """)).fetchall()
    except Exception:
        return []

    out = []
    for r in rows:
        rev = float(r[5] or 0)
        op = float(r[6] or 0)
        prev = float(r[7] or 0)
        out.append({
            "brand": r[0], "corp_name": r[1], "stock_code": r[2] or "",
            "is_brand_level": bool(r[3]), "year": r[4],
            "revenue": rev, "op_income": op,
            "opm": round(op / rev * 100, 1) if rev else None,
            "prev_revenue": prev,
            "rev_yoy_pct": round((rev / prev - 1) * 100, 1) if prev > 0 else None,
        })
    return out


def get_market_growth_story(session: Session, top_n: int = 6,
                            window_days: int = 150, trailing: int = 3) -> dict:
    """
    시장 성장 스토리 — 국가별 화장품 수출 YoY(성과) + 같은 시장의 경쟁사 활동(뉴스).

    "이 시장이 왜 크는가"를 삼각으로 엮음: 실제 수출 성장 + 그 시장에서 경쟁사가
    한 진출·입점·신제품·마케팅 활동을 함께 제시(인과 아님, 동반 맥락).

    반환: {overall: {yoy_pct, cur_musd, prev_musd, growers, decliners},
           markets: [{country_code, country_name, exp_musd, yoy_pct, delta_musd,
                      moves: [{brand, activity_type, title, url, date, importance}]}]}
    export_stats 없으면 markets=[], overall=None.
    """
    growth = get_market_export_growth(session, hs_like="3304%", trailing=trailing)
    if not growth:
        return {"overall": None, "markets": []}

    # 전체(주요국 합) YoY
    cur_tot = sum(g["exp_usd_3m"] for g in growth)
    prv_tot = sum(g["prev_usd_3m"] for g in growth)
    overall = {
        "yoy_pct": round((cur_tot / prv_tot - 1) * 100, 1) if prv_tot > 0 else None,
        "cur_musd": round(cur_tot / 1e6, 1),
        "prev_musd": round(prv_tot / 1e6, 1),
        "growers": sum(1 for g in growth if (g["yoy_pct"] or 0) >= 15),
        "decliners": sum(1 for g in growth if (g["yoy_pct"] or 0) <= -10),
    }

    # 성장 시장 선별: 규모 하한(월 $3M↑=3M누적 $9M↑)으로 미세시장 노이즈 제거 후 YoY순
    sizable = [g for g in growth if g["exp_usd_3m"] >= 9_000_000 and g["yoy_pct"] is not None]
    sizable.sort(key=lambda g: g["yoy_pct"], reverse=True)
    picked = sizable[:top_n]

    cutoff = _cutoff_iso(window_days)
    acts_ph = ", ".join(f"'{a}'" for a in _STORY_ACTS)
    markets = []
    for g in picked:
        cc = g["country_code"]
        rows = session.execute(text(f"""
            SELECT brand, activity_type, title_ko, title, source_url,
                   published_date, importance, strategic_score
            FROM {DB_SCHEMA}.news_articles
            WHERE country = :cc
              AND published_date >= :cutoff
              AND is_duplicate IS NOT TRUE
              AND (brand_focus != 'incidental' OR brand_focus IS NULL)
              AND activity_type IN ({acts_ph})
            ORDER BY (importance = 'high') DESC,
                     COALESCE(strategic_score, 0) DESC,
                     published_date DESC
            LIMIT 5
        """), {"cc": cc, "cutoff": cutoff}).fetchall()
        moves = [{
            "brand": r[0], "activity_type": r[1],
            "title": (r[2] or r[3] or "")[:90],
            "url": r[4] or "",
            "date": str(r[5])[:10] if r[5] else "",
            "importance": r[6],
        } for r in rows]
        markets.append({
            "country_code": cc, "country_name": g["country_name"],
            "exp_musd": round(g["exp_usd_3m"] / 1e6, 1),
            "yoy_pct": g["yoy_pct"],
            "delta_musd": round((g["exp_usd_3m"] - g["prev_usd_3m"]) / 1e6, 1),
            "moves": moves,
        })
    return {"overall": overall, "markets": markets}


def get_search_momentum(session: Session) -> dict:
    """
    네이버 검색 트렌드(수요 신호) 모멘텀 — 브랜드별 최근4주 vs 직전4주 평균 검색지수.

    search_trends 테이블(kind='brand')이 없거나 비면 빈 dict 반환(비파괴).
    반환: {brand: {recent, prev, momentum, signal}}  signal ∈ rising|stable|cooling
    """
    try:
        rows = session.execute(text(f"""
            WITH ranked AS (
                SELECT term, ratio,
                       ROW_NUMBER() OVER (PARTITION BY term ORDER BY period DESC) rn
                FROM {DB_SCHEMA}.search_trends
                WHERE kind = 'brand'
            )
            SELECT term,
                   AVG(ratio) FILTER (WHERE rn <= 4)              AS recent,
                   AVG(ratio) FILTER (WHERE rn BETWEEN 5 AND 8)   AS prev
            FROM ranked GROUP BY term
        """)).fetchall()
    except Exception:
        return {}

    out = {}
    for term, recent, prev in rows:
        recent = float(recent or 0)
        prev = float(prev or 0)
        momentum = round(recent / prev, 2) if prev >= 1 else 1.0
        if momentum > 1.3:
            signal = "rising"
        elif momentum < 0.77:
            signal = "cooling"
        else:
            signal = "stable"
        out[term] = {"recent": round(recent, 1), "prev": round(prev, 1),
                     "momentum": momentum, "signal": signal}
    return out


def get_demand_triangulation(session: Session) -> list[dict]:
    """
    뉴스(공급/PR) vs 검색(수요) 삼각검증. 브랜드별로 두 모멘텀을 대조해 라벨링:

      - 뉴스↑ & 검색↑   → '실질'    (real: PR과 실수요 동반 — 진짜 무브)
      - 뉴스↑ & 검색↓   → 'PR우세'  (pr: 보도는 뜨는데 검색 수요는 식음 — 노이즈 의심)
      - 검색↑ & 뉴스 정체 → '숨은수요' (latent: 검색은 느는데 보도 적음 — 선제 주목)
      - 그 외           → '안정'

    주의: 뉴스 모멘텀은 수집빈도 변화에 민감(최근 부풀림)하므로 판별의 핵심은 검색 방향.
    'PR우세'는 검색이 실제로 *식을 때*만(단순 정체 아님) 붙여 오탐을 줄인다.
    검색 데이터 없으면(테이블 부재) 각 브랜드 verdict=None. 대시보드/브리핑 공용.
    """
    news = compute_brand_momentum(session)
    search = get_search_momentum(session)

    out = []
    for n in news:
        b = n["brand"]
        s = search.get(b)
        news_up = n["signal"] == "rising"
        verdict = None
        if s is not None:
            search_up = s["signal"] == "rising"
            search_down = s["signal"] == "cooling"
            if news_up and search_up:
                verdict = "real"
            elif news_up and search_down:
                verdict = "pr"
            elif search_up and not news_up:
                verdict = "latent"
            else:
                verdict = "stable"
        out.append({
            "brand":          b,
            "tier":           n["tier"],
            "news_momentum":  n["momentum"],
            "news_signal":    n["signal"],
            "news_recent_4w": n["recent_4w"],
            "search_momentum": s["momentum"] if s else None,
            "search_signal":   s["signal"] if s else None,
            "search_recent":   s["recent"] if s else None,
            "verdict":         verdict,
        })
    # 검증된 실질 무브 우선 정렬: real > latent > pr > stable, 그 안에서 뉴스 활동량순
    rank = {"real": 0, "latent": 1, "pr": 2, "stable": 3, None: 4}
    out.sort(key=lambda x: (rank.get(x["verdict"], 4), -x["news_recent_4w"]))
    return out


def compute_brand_momentum(session: Session) -> list[dict]:
    """
    브랜드별 모멘텀 스코어 계산.

    momentum = recent_4w_count / max(prev_4w_count, 1)
    - > 1.5  → Rising  (인디 브랜드 급부상 / Tier2→1 승급 후보)
    - 0.7~1.5 → Stable
    - < 0.7  → Cooling (기존 브랜드 침체 / Tier1→2 강등 후보)

    Returns list of dicts sorted by momentum desc.
    """
    now = datetime.utcnow()
    recent_start = (now - timedelta(weeks=4)).isoformat()
    prev_start   = (now - timedelta(weeks=8)).isoformat()
    prev_end     = recent_start

    rows = session.execute(text(f"""
        SELECT
            brand,
            COUNT(*) FILTER (WHERE published_date >= :recent_start)               AS recent_4w,
            COUNT(*) FILTER (WHERE published_date >= :prev_start
                              AND  published_date <  :prev_end)                    AS prev_4w,
            COUNT(*) FILTER (WHERE published_date >= :recent_start
                              AND  importance = 'high')                            AS recent_high,
            COUNT(*)                                                               AS total
        FROM {DB_SCHEMA}.news_articles
        WHERE published_date >= :prev_start
          AND (brand_focus != 'incidental' OR brand_focus IS NULL)
        GROUP BY brand
        ORDER BY brand
    """), {
        "recent_start": recent_start,
        "prev_start":   prev_start,
        "prev_end":     prev_end,
    }).fetchall()

    # monitored_brands에서 현재 tier 가져오기
    tier_rows = session.execute(text(
        f"SELECT name, tier FROM {DB_SCHEMA}.monitored_brands WHERE is_active = TRUE"
    )).fetchall()
    tier_map = {r[0]: r[1] for r in tier_rows}

    import math
    result = []
    for r in rows:
        brand, recent, prev, recent_high, total = r[0], r[1] or 0, r[2] or 0, r[3] or 0, r[4] or 0
        # prev_4w가 3건 미만이면 이전 기간 데이터 부족 → momentum neutral 처리
        if prev < 3:
            momentum = 1.0
        else:
            momentum = round(recent / prev, 2)
        if momentum > 1.5:
            signal = "rising"
        elif momentum < 0.7:
            signal = "cooling"
        else:
            signal = "stable"
        # HIGH 기사 1건 = 일반 기사 2건 가중치: 전략적 활동이 많은 브랜드가 상위에 위치
        sort_score = (recent + recent_high * 2) * math.log1p(momentum)
        result.append({
            "brand":        brand,
            "tier":         tier_map.get(brand, 2),
            "momentum":     momentum,
            "signal":       signal,
            "recent_4w":    recent,
            "prev_4w":      prev,
            "recent_high":  recent_high,
            "total_8w":     total,
            "_sort_score":  sort_score,
        })

    result.sort(key=lambda x: x["_sort_score"], reverse=True)
    return result


def upsert_brand_momentum(session: Session, brand: str, momentum: float) -> None:
    """monitored_brands 테이블의 momentum_score + last_scored 갱신."""
    session.execute(text(f"""
        UPDATE {DB_SCHEMA}.monitored_brands
        SET momentum_score = :momentum,
            last_scored    = NOW()
        WHERE name = :brand
    """), {"brand": brand, "momentum": momentum})
    session.commit()


def days_since_tier_change(session: Session, brand: str) -> "float | None":
    """마지막 tier 변경 후 경과일. 기록 없으면 None(제약 없음)."""
    row = session.execute(text(f"""
        SELECT EXTRACT(EPOCH FROM (NOW() - tier_changed_at)) / 86400.0
        FROM {DB_SCHEMA}.monitored_brands
        WHERE name = :brand AND tier_changed_at IS NOT NULL
    """), {"brand": brand}).fetchone()
    return float(row[0]) if row and row[0] is not None else None


def update_brand_tier(session: Session, brand: str, new_tier: int) -> None:
    """monitored_brands 테이블의 tier 승급/강등 + tier_changed_at 기록."""
    session.execute(text(f"""
        UPDATE {DB_SCHEMA}.monitored_brands
        SET tier = :tier,
            tier_changed_at = NOW()
        WHERE name = :brand
    """), {"brand": brand, "tier": new_tier})
    session.commit()


def get_brand_radar(session: Session) -> list[dict]:
    """대시보드 Brand Radar용: momentum + tier 정보 반환."""
    scores = compute_brand_momentum(session)

    # DB에 없는 브랜드(아직 기사 없는 Tier2) 보완
    all_brands_rows = session.execute(text(
        f"SELECT name, tier, momentum_score FROM {DB_SCHEMA}.monitored_brands WHERE is_active = TRUE"
    )).fetchall()
    scored_names = {s["brand"] for s in scores}
    for r in all_brands_rows:
        if r[0] not in scored_names:
            scores.append({
                "brand":       r[0],
                "tier":        r[1],
                "momentum":    r[2] or 0.0,
                "signal":      "stable",
                "recent_4w":   0,
                "prev_4w":     0,
                "recent_high": 0,
                "total_8w":    0,
            })

    import math
    for s in scores:
        if "_sort_score" not in s:
            s["_sort_score"] = (s["recent_4w"] + s["recent_high"] * 2) * math.log1p(s["momentum"])
    scores.sort(key=lambda x: (x["tier"], -x["_sort_score"]))
    return scores


# 서브신호 → 한국어 라벨
_COMPOSITE_DRV = {"momentum": "모멘텀", "financial": "실적", "trademark": "상표", "demand": "수요"}
_VERDICT_DEMAND = {"real": 1.0, "latent": 0.7, "stable": 0.4, "pr": 0.2}


def get_brand_composite_score(session: Session) -> list[dict]:
    """
    브랜드 종합 스코어 — 모멘텀·재무·상표선행·수요 4축을 0~100으로 통합.

    기존 쿼리 조합(새 SQL 없음). 각 서브신호 0~1 정규화 후, 결측은 제외하고
    존재하는 축의 가중치로 재정규화. 전 활성 브랜드 커버. 전체 실패 시 [].
    반환: [{brand,tier,score,rank,subs{4:0~1|None},present{4:bool},verdict,drivers[상위2]}]
    """
    try:
        mo_list = compute_brand_momentum(session)
        mo = {m["brand"]: m for m in mo_list}
        rows = session.execute(text(
            f"SELECT name, tier FROM {DB_SCHEMA}.monitored_brands WHERE is_active = TRUE ORDER BY tier, name"
        )).fetchall()
        active = [(r[0], r[1]) for r in rows] or [(m["brand"], m.get("tier", 2)) for m in mo_list]
        fins = {f["brand"]: f for f in get_competitor_financials(session)}
        tm_sum: dict = {}
        for b in get_trademark_signals(session).get("brands", []):
            tm_sum[b["brand"]] = tm_sum.get(b["brand"], 0) + (b.get("recent") or 0)
        demand = {d["brand"]: d for d in get_demand_triangulation(session)}
        spikes: dict = {}
        for s in get_google_spikes(session):
            spikes[s["brand"]] = max(spikes.get(s["brand"], 0), s["spike_ratio"])
    except Exception:
        return []

    recent_vals = sorted(m["recent_4w"] for m in mo_list) or [1]
    p90 = recent_vals[max(0, int(len(recent_vals) * 0.9) - 1)] or 1  # 볼륨 정규화 분모
    tm_max = max(tm_sum.values()) if tm_sum else 0
    W = {"momentum": 0.35, "financial": 0.25, "trademark": 0.15, "demand": 0.25}

    out = []
    for brand, tier in active:
        m = mo.get(brand)
        subs: dict = {}
        present: dict = {}

        # 모멘텀 — 방향 + 볼륨 (항상 존재)
        mom = (m or {}).get("momentum", 1.0) or 1.0
        rec = (m or {}).get("recent_4w", 0) or 0
        direction = (min(max(mom, 0.5), 2.0) - 0.5) / 1.5
        volume = min(rec / p90, 1.0) if p90 else 0.0
        subs["momentum"] = round(0.7 * direction + 0.3 * volume, 3)
        present["momentum"] = True

        # 재무 YoY
        f = fins.get(brand)
        if f and f.get("rev_yoy_pct") is not None:
            x = min(max(f["rev_yoy_pct"], -20.0), 40.0)
            subs["financial"] = round((x + 20.0) / 60.0, 3)
            present["financial"] = True
        else:
            subs["financial"] = None
            present["financial"] = False

        # 상표 선행 (테이블 데이터 있을 때만)
        if tm_max > 0:
            subs["trademark"] = round(min(tm_sum.get(brand, 0) / tm_max, 1.0), 3)
            present["trademark"] = True
        else:
            subs["trademark"] = None
            present["trademark"] = False

        # 수요 — verdict + 검색 급등 보너스
        d = demand.get(brand)
        sp = spikes.get(brand)
        verdict = (d or {}).get("verdict")
        if verdict or sp:
            base = _VERDICT_DEMAND.get(verdict, 0.4) if verdict else 0.4
            bonus = min(0.2 * ((sp or 1.0) - 1.0), 0.2) if sp else 0.0
            subs["demand"] = round(min(base + bonus, 1.0), 3)
            present["demand"] = True
        else:
            subs["demand"] = None
            present["demand"] = False

        # 가변 가중합
        num = den = 0.0
        contrib: dict = {}
        for k, w in W.items():
            if present[k] and subs[k] is not None:
                num += w * subs[k]
                den += w
                contrib[k] = w * subs[k]
        score = round(100 * num / den) if den > 0 else None

        drivers = [k for k, _ in sorted(contrib.items(), key=lambda kv: kv[1], reverse=True)[:2]]
        out.append({
            "brand": brand, "tier": tier, "score": score,
            "subs": subs, "present": present, "verdict": verdict,
            "drivers": [_COMPOSITE_DRV[k] for k in drivers],
        })

    out = [o for o in out if o["score"] is not None]
    out.sort(key=lambda x: x["score"], reverse=True)
    for i, o in enumerate(out):
        o["rank"] = i + 1
    return out
