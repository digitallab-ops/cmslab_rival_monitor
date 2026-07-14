"""
분석 쿼리 함수 모음 — 대시보드 / CLI 드릴다운용

모든 함수는 SQLAlchemy Session을 받아 순수 Python dict/list를 반환.
"""

from collections import defaultdict
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from config.settings import DB_SCHEMA


def _cutoff_iso(days: int) -> str:
    return (datetime.utcnow() - timedelta(days=days)).isoformat()


def get_collection_stats(session: Session, days: int = 30) -> dict:
    """KPI 요약 통계 반환."""
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
            WHERE published_date >= :cutoff
        """),
        {"cutoff": cutoff},
    ).fetchone()

    return {
        "total":            row[0] or 0,
        "high":             row[1] or 0,
        "medium":           row[2] or 0,
        "low":              row[3] or 0,
        "brands_active":    row[4] or 0,
        "countries_active": row[5] or 0,
        "days":             days,
        "generated_at":     datetime.utcnow().isoformat(),
    }


def get_high_articles(
    session: Session,
    days: int = 30,
    brand: "str | None" = None,
    country: "str | None" = None,
) -> list:
    """HIGH importance 기사 전체 목록 반환 (드릴다운용)."""
    cutoff = _cutoff_iso(days)

    where_extras = ""
    params: dict = {"cutoff": cutoff}
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
                   brand_focus, source_country
            FROM {DB_SCHEMA}.news_articles
            WHERE importance IN ('high', 'medium')
              AND (
                  brand_focus IS NULL           -- 구기사: 필터 미적용
                  OR brand_focus != 'incidental' -- 신기사: incidental 제외
                  OR importance = 'high'         -- HIGH는 incidental이어도 표시
              )
              AND published_date >= :cutoff
              {where_extras}
            ORDER BY
                CASE importance WHEN 'high' THEN 0 ELSE 1 END,
                published_date DESC
            LIMIT 400
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
            WHERE published_date >= :cutoff
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
            WHERE published_date >= :cutoff
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
            WHERE published_date >= :cutoff
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
            WHERE published_date >= :cutoff
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
            WHERE published_date >= :cutoff
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
            WHERE published_date >= :cutoff
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
            WHERE published_date >= :cutoff
            GROUP BY brand
        """),
        {"cutoff": cutoff},
    ).fetchall()

    # 3) 브랜드별 주력 시장 top-3
    country_rows = session.execute(
        text(f"""
            SELECT brand, country, COUNT(*) AS cnt
            FROM {DB_SCHEMA}.news_articles
            WHERE published_date >= :cutoff
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
                   details
            FROM {DB_SCHEMA}.news_articles
            WHERE importance IN ('high', 'medium')
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
                   source_url, published_date::date::text AS pub_date, details
            FROM {DB_SCHEMA}.news_articles
            WHERE importance IN ('high', 'medium')
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
            WHERE importance IN ('high', 'medium')
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
    """국가별 신호 통계 반환 (세계지도용). {CC: {total, high, medium}}"""
    cutoff = _cutoff_iso(days)
    rows = session.execute(
        text(f"""
            SELECT country,
                   COUNT(*) AS total,
                   COUNT(*) FILTER (WHERE importance = 'high') AS high,
                   COUNT(*) FILTER (WHERE importance = 'medium') AS medium
            FROM {DB_SCHEMA}.news_articles
            WHERE published_date >= :cutoff
            GROUP BY country
        """),
        {"cutoff": cutoff},
    ).fetchall()
    return {r[0]: {"total": r[1] or 0, "high": r[2] or 0, "medium": r[3] or 0} for r in rows}


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
