from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy import text
from sqlalchemy.orm import Session

from storage.models import NewsArticle, CollectionRun, DedupCandidate, get_session
from config.settings import DB_SCHEMA


def get_active_brand_names(session: Optional[Session] = None) -> list[str]:
    """활성 모니터 브랜드명(monitored_brands DB, 티어 무관). 실패·빈 결과 시 config fallback.

    신호계층(검색·수출·재무·상표)과 뉴스 수집이 같은 브랜드 소스를 쓰도록 공용화.
    → monitored_brands에 브랜드 1회 추가하면 전 파이프라인에 자동 반영.
    """
    from config.brands import ALL_BRANDS
    own = session is None
    if own:
        session = get_session()
    try:
        rows = session.execute(text(
            f"SELECT name FROM {DB_SCHEMA}.monitored_brands "
            f"WHERE is_active = TRUE ORDER BY tier, name"
        )).fetchall()
        return [r[0] for r in rows] or list(ALL_BRANDS)
    except Exception:
        return list(ALL_BRANDS)
    finally:
        if own:
            session.close()


def article_exists(session: Session, url_hash: str) -> bool:
    return session.query(NewsArticle).filter_by(url_hash=url_hash).first() is not None


def save_article(session: Session, article: NewsArticle) -> NewsArticle:
    session.add(article)
    session.commit()
    session.refresh(article)
    return article


def get_recent_titles(session: Session, days: int = 3) -> list[tuple[int, str]]:
    cutoff = datetime.utcnow() - timedelta(days=days)
    rows = (
        session.query(NewsArticle.id, NewsArticle.title)
        .filter(NewsArticle.published_date >= cutoff)
        .all()
    )
    return [(r.id, r.title) for r in rows]


def save_dedup_candidate(session: Session, id1: int, id2: int, similarity: float):
    cand = DedupCandidate(article_id_1=id1, article_id_2=id2, similarity=similarity)
    session.add(cand)
    session.commit()


def save_collection_run(session: Session, run: CollectionRun) -> CollectionRun:
    session.add(run)
    session.commit()
    return run


# ── HIGH 속보 중복 발송 방지 ────────────────────────────────────────────────
#  같은 사건이 출처만 달리 여러 기사로 들어오면(번역 후 거의 동일) 속보가 여러 번
#  발송됨. 의미 병합(임베딩)은 밤에만 돌아 속보보다 늦음 → 발송 시점에 최근 발송
#  로그와 대조해 (브랜드·국가·활동유형 동일 + 제목/내용 유사)면 억제한다.
_ALERT_LOG_READY = False


def _ensure_high_alert_log(session: Session) -> None:
    global _ALERT_LOG_READY
    if _ALERT_LOG_READY:
        return
    session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.high_alert_log (
            id BIGSERIAL PRIMARY KEY,
            brand VARCHAR(100),
            country VARCHAR(8),
            activity_type VARCHAR(40),
            sig TEXT,
            sent_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """))
    session.execute(text(
        f"CREATE INDEX IF NOT EXISTS ix_high_alert_log_key "
        f"ON {DB_SCHEMA}.high_alert_log (brand, country, activity_type, sent_at DESC)"
    ))
    session.commit()
    _ALERT_LOG_READY = True


def _alert_sig(article) -> str:
    """중복 비교용 텍스트 — 번역본(details/title_ko) 우선."""
    return (getattr(article, "details", None)
            or getattr(article, "title_ko", None)
            or getattr(article, "title", None) or "").strip()


def _char_ngrams(s: str, n: int = 3) -> set:
    """공백·기호 제거 후 문자 n-gram 집합. 한국어 조사·어순 변화에 강건."""
    import re
    t = re.sub(r"[^가-힣A-Za-z0-9]", "", s or "")
    return {t[i:i + n] for i in range(len(t) - n + 1)} if len(t) >= n else ({t} if t else set())


def _same_event(a: str, b: str, seq_thr: float = 0.50, gram_thr: float = 0.20) -> bool:
    """같은 사건 판정 — 문자 유사도(번역 어투 유사) 또는 3-gram Jaccard(핵심 구절 겹침)."""
    from deduplication.url_hasher import title_similarity
    if title_similarity(a, b) >= seq_thr:
        return True
    ga, gb = _char_ngrams(a), _char_ngrams(b)
    if not (ga and gb):
        return False
    return len(ga & gb) / len(ga | gb) >= gram_thr


def high_alert_is_duplicate(session: Session, article, window_hours: int = 72) -> bool:
    """최근 window 내 같은 (브랜드·국가·활동유형)로 같은 사건 속보를 이미 보냈으면 True."""
    _ensure_high_alert_log(session)
    sig = _alert_sig(article)
    if not sig:
        return False
    cutoff = datetime.utcnow() - timedelta(hours=window_hours)
    rows = session.execute(text(f"""
        SELECT sig FROM {DB_SCHEMA}.high_alert_log
        WHERE brand = :b AND country = :c AND activity_type = :a AND sent_at >= :cut
        ORDER BY sent_at DESC LIMIT 40
    """), {"b": article.brand, "c": article.country,
           "a": article.activity_type, "cut": cutoff}).fetchall()
    return any(_same_event(sig, r[0] or "") for r in rows)


def record_high_alert(session: Session, article) -> None:
    """발송한 HIGH 속보를 로그에 기록(이후 중복 판단 기준)."""
    _ensure_high_alert_log(session)
    session.execute(text(f"""
        INSERT INTO {DB_SCHEMA}.high_alert_log (brand, country, activity_type, sig)
        VALUES (:b, :c, :a, :s)
    """), {"b": article.brand, "c": article.country,
           "a": article.activity_type, "s": _alert_sig(article)})
    session.commit()


def query_articles(
    session: Session,
    brand: Optional[str] = None,
    country: Optional[str] = None,
    activity_type: Optional[str] = None,
    importance: Optional[str] = None,
    days: Optional[int] = None,
    limit: int = 20,
) -> list[NewsArticle]:
    q = session.query(NewsArticle)
    if brand:
        q = q.filter(NewsArticle.brand.ilike(f"%{brand}%"))
    if country:
        q = q.filter(NewsArticle.country == country.upper())
    if activity_type:
        q = q.filter(NewsArticle.activity_type == activity_type)
    if importance:
        q = q.filter(NewsArticle.importance == importance)
    if days:
        cutoff = datetime.utcnow() - timedelta(days=days)
        q = q.filter(NewsArticle.published_date >= cutoff)
    return q.order_by(NewsArticle.published_date.desc()).limit(limit).all()


# ── 값 매핑(드리프트 승인→반영) ──────────────────────────────────────────────
# 파수꾼이 미매핑 국가코드·채널을 감지→제안(pending), 슬랙 `매핑 승인`으로 active,
# 대시보드가 런타임에 읽어 코드배포 없이 반영. kind='country'|'channel'.
_VALUE_MAP_READY = False


def _ensure_value_mappings(session: Session) -> None:
    global _VALUE_MAP_READY
    if _VALUE_MAP_READY:
        return
    session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.value_mappings (
            id BIGSERIAL PRIMARY KEY,
            kind VARCHAR(20) NOT NULL,
            code VARCHAR(80) NOT NULL,
            value VARCHAR(120),
            suggested VARCHAR(120),
            status VARCHAR(12) DEFAULT 'pending',
            added_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            UNIQUE (kind, code)
        )
    """))
    session.commit()
    _VALUE_MAP_READY = True


def propose_mapping(session: Session, kind: str, code: str, suggested: str = "") -> None:
    """미매핑 값을 pending 제안으로 등록(이미 있으면 무시)."""
    _ensure_value_mappings(session)
    session.execute(text(f"""
        INSERT INTO {DB_SCHEMA}.value_mappings (kind, code, suggested, status)
        VALUES (:k, :c, :s, 'pending')
        ON CONFLICT (kind, code) DO NOTHING
    """), {"k": kind, "c": code, "s": suggested})
    session.commit()


def list_pending_mappings(session: Session, kind: Optional[str] = None) -> list[tuple]:
    """대기(pending) 제안 목록 → [(kind, code, suggested), ...]."""
    _ensure_value_mappings(session)
    q = f"SELECT kind, code, COALESCE(suggested,'') FROM {DB_SCHEMA}.value_mappings WHERE status='pending'"
    p: dict = {}
    if kind:
        q += " AND kind = :k"; p["k"] = kind
    q += " ORDER BY kind, code"
    return [(r[0], r[1], r[2]) for r in session.execute(text(q), p).fetchall()]


def approve_mapping(session: Session, code: str, value: Optional[str] = None,
                    kind: Optional[str] = None) -> int:
    """특정 code의 pending 제안을 active로(값=지정값 또는 제안값). 반환: 반영 건수."""
    _ensure_value_mappings(session)
    q = (f"UPDATE {DB_SCHEMA}.value_mappings "
         f"SET value = COALESCE(:v, NULLIF(suggested,''), value), status='active' "
         f"WHERE code = :c AND status='pending'")
    p: dict = {"v": value, "c": code}
    if kind:
        q += " AND kind = :k"; p["k"] = kind
    r = session.execute(text(q), p)
    session.commit()
    return r.rowcount or 0


def approve_all_pending(session: Session, kind: Optional[str] = None) -> int:
    """대기 제안 전부 active로(값=제안값 있는 것만). 반환: 반영 건수."""
    _ensure_value_mappings(session)
    q = (f"UPDATE {DB_SCHEMA}.value_mappings SET value = suggested, status='active' "
         f"WHERE status='pending' AND COALESCE(suggested,'') <> ''")
    p: dict = {}
    if kind:
        q += " AND kind = :k"; p["k"] = kind
    r = session.execute(text(q), p)
    session.commit()
    return r.rowcount or 0


def reject_mapping(session: Session, code: str, kind: Optional[str] = None) -> int:
    """제안 거절(status='rejected' — 다시 제안 안 함)."""
    _ensure_value_mappings(session)
    q = f"UPDATE {DB_SCHEMA}.value_mappings SET status='rejected' WHERE code = :c AND status='pending'"
    p: dict = {"c": code}
    if kind:
        q += " AND kind = :k"; p["k"] = kind
    r = session.execute(text(q), p)
    session.commit()
    return r.rowcount or 0


def get_active_mappings(session: Session, kind: str) -> dict:
    """반영된(active) 매핑 {code: value} — 대시보드 런타임 병합용."""
    _ensure_value_mappings(session)
    rows = session.execute(text(
        f"SELECT code, value FROM {DB_SCHEMA}.value_mappings "
        f"WHERE kind = :k AND status='active' AND value IS NOT NULL"), {"k": kind}).fetchall()
    return {r[0]: r[1] for r in rows}


def known_mapping_codes(session: Session, kind: str) -> set:
    """이미 제안됐거나(pending) 반영된(active) code 집합 — 파수꾼 재제안 방지용."""
    _ensure_value_mappings(session)
    rows = session.execute(text(
        f"SELECT code FROM {DB_SCHEMA}.value_mappings "
        f"WHERE kind = :k AND status IN ('pending','active')"), {"k": kind}).fetchall()
    return {r[0] for r in rows}


# ── 슬랙봇 개인화: 대화 영속 + 사용자 장기 기억 ──────────────────────────────
# 슬랙 user_id별로 (a) 대화를 DB에 남겨 재시작해도 맥락이 이어지고,
# (b) '지속될 사실'(담당 시장·관심 브랜드·선호 형식)을 기억해 답변을 개인화한다.
# 사용자는 `기억`으로 조회, `기억해 ~`로 추가, `잊어`로 삭제할 수 있다(제어권 보장).
_BOT_MEM_READY = False


def _ensure_bot_tables(session: Session) -> None:
    global _BOT_MEM_READY
    if _BOT_MEM_READY:
        return
    session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.bot_conversations (
            id BIGSERIAL PRIMARY KEY,
            user_id VARCHAR(32) NOT NULL,
            role VARCHAR(12) NOT NULL,
            content TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """))
    session.execute(text(
        f"CREATE INDEX IF NOT EXISTS ix_bot_conv_user "
        f"ON {DB_SCHEMA}.bot_conversations (user_id, created_at DESC)"))
    session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.bot_user_memory (
            id BIGSERIAL PRIMARY KEY,
            user_id VARCHAR(32) NOT NULL,
            mem_key VARCHAR(80) NOT NULL,
            mem_value TEXT,
            source VARCHAR(12) DEFAULT 'auto',
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            UNIQUE (user_id, mem_key)
        )
    """))
    session.commit()
    _BOT_MEM_READY = True


def save_bot_turn(session: Session, user_id: str, role: str, content: str) -> None:
    """대화 한 턴 저장(실패해도 대화엔 지장 없게 호출부에서 예외 흡수)."""
    _ensure_bot_tables(session)
    session.execute(text(f"""
        INSERT INTO {DB_SCHEMA}.bot_conversations (user_id, role, content)
        VALUES (:u, :r, :c)"""), {"u": user_id, "r": role, "c": (content or "")[:6000]})
    session.commit()


def load_bot_history(session: Session, user_id: str, turns: int = 10) -> list:
    """최근 대화를 OpenAI messages 형식으로(오래된 순). turns=주고받은 쌍 수."""
    _ensure_bot_tables(session)
    rows = session.execute(text(f"""
        SELECT role, content FROM {DB_SCHEMA}.bot_conversations
        WHERE user_id = :u ORDER BY created_at DESC, id DESC LIMIT :n
    """), {"u": user_id, "n": turns * 2}).fetchall()
    return [{"role": r[0], "content": r[1] or ""} for r in reversed(rows)]


def get_user_memory(session: Session, user_id: str) -> dict:
    """{키: 값} — 이 사용자에 대해 기억하는 지속 사실."""
    _ensure_bot_tables(session)
    rows = session.execute(text(f"""
        SELECT mem_key, mem_value FROM {DB_SCHEMA}.bot_user_memory
        WHERE user_id = :u ORDER BY updated_at DESC LIMIT 25
    """), {"u": user_id}).fetchall()
    return {r[0]: r[1] for r in rows}


def upsert_user_memory(session: Session, user_id: str, key: str,
                       value: str, source: str = "auto") -> None:
    _ensure_bot_tables(session)
    session.execute(text(f"""
        INSERT INTO {DB_SCHEMA}.bot_user_memory (user_id, mem_key, mem_value, source, updated_at)
        VALUES (:u, :k, :v, :s, NOW())
        ON CONFLICT (user_id, mem_key) DO UPDATE
        SET mem_value = EXCLUDED.mem_value, source = EXCLUDED.source, updated_at = NOW()
    """), {"u": user_id, "k": key[:80], "v": (value or "")[:500], "s": source})
    session.commit()


def delete_user_memory(session: Session, user_id: str, key: Optional[str] = None) -> int:
    """key 지정 시 그 항목만, 없으면 이 사용자 기억 전체 삭제. 반환: 삭제 건수."""
    _ensure_bot_tables(session)
    if key:
        r = session.execute(text(
            f"DELETE FROM {DB_SCHEMA}.bot_user_memory WHERE user_id=:u AND mem_key ILIKE :k"),
            {"u": user_id, "k": f"%{key}%"})
    else:
        r = session.execute(text(
            f"DELETE FROM {DB_SCHEMA}.bot_user_memory WHERE user_id=:u"), {"u": user_id})
    session.commit()
    return r.rowcount or 0


def purge_bot_conversations(session: Session, keep_days: int = 90) -> int:
    """오래된 대화 로그 정리(무한 증가 방지)."""
    try:
        r = session.execute(text(f"""
            DELETE FROM {DB_SCHEMA}.bot_conversations
            WHERE created_at < NOW() - (:d || ' days')::interval"""), {"d": keep_days})
        session.commit()
        return r.rowcount or 0
    except Exception:
        session.rollback()
        return 0


# ── 소셜 지표(유튜브·인스타·틱톡 공용) ──────────────────────────────────────
# 뉴스(공급)·검색(수요)·리테일(실판매)에 없는 '소셜 버즈' 축. 플랫폼 무관 스키마라
# 유튜브 조회수, IG 팔로워, 틱톡 조회수/GMV를 같은 테이블에 담는다.
_SOCIAL_READY = False


def _ensure_social_metrics(session: Session) -> None:
    global _SOCIAL_READY
    if _SOCIAL_READY:
        return
    session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.social_metrics (
            id BIGSERIAL PRIMARY KEY,
            platform VARCHAR(16) NOT NULL,
            brand VARCHAR(100) NOT NULL,
            metric VARCHAR(32) NOT NULL,
            value DOUBLE PRECISION,
            meta TEXT,
            captured_date DATE NOT NULL DEFAULT CURRENT_DATE,
            UNIQUE (platform, brand, metric, captured_date)
        )
    """))
    session.execute(text(
        f"CREATE INDEX IF NOT EXISTS ix_social_metrics_key "
        f"ON {DB_SCHEMA}.social_metrics (platform, brand, captured_date DESC)"))
    session.commit()
    _SOCIAL_READY = True


def upsert_social_metric(session: Session, platform: str, brand: str, metric: str,
                         value: float, meta: Optional[str] = None) -> None:
    """당일 지표 upsert(같은 날 재수집 시 갱신)."""
    _ensure_social_metrics(session)
    session.execute(text(f"""
        INSERT INTO {DB_SCHEMA}.social_metrics (platform, brand, metric, value, meta)
        VALUES (:p, :b, :m, :v, :meta)
        ON CONFLICT (platform, brand, metric, captured_date)
        DO UPDATE SET value = EXCLUDED.value, meta = EXCLUDED.meta
    """), {"p": platform, "b": brand, "m": metric, "v": float(value or 0),
           "meta": (meta or "")[:300]})
    session.commit()


def get_social_buzz(session: Session, platform: str = "youtube", days: int = 21) -> dict:
    """브랜드별 최신 소셜 지표 + 직전 대비 변화.
    반환: {brand: {metric: {'latest':v, 'prev':v, 'delta_pct':float|None, 'meta':str}}}"""
    _ensure_social_metrics(session)
    try:
        rows = session.execute(text(f"""
            SELECT brand, metric, value, meta, captured_date
            FROM {DB_SCHEMA}.social_metrics
            WHERE platform = :p AND captured_date >= CURRENT_DATE - :d
            ORDER BY brand, metric, captured_date DESC
        """), {"p": platform, "d": days}).fetchall()
    except Exception:
        return {}
    out: dict = {}
    for brand, metric, value, meta, _cd in rows:
        slot = out.setdefault(brand, {}).setdefault(metric, {"latest": None, "prev": None,
                                                            "delta_pct": None, "meta": ""})
        if slot["latest"] is None:
            slot["latest"] = value
            slot["meta"] = meta or ""
        elif slot["prev"] is None:
            slot["prev"] = value
    for _b, mm in out.items():
        for _m, s in mm.items():
            if s["latest"] is not None and s["prev"]:
                s["delta_pct"] = (s["latest"] - s["prev"]) / s["prev"] * 100
    return out
