from datetime import datetime, timedelta
from typing import Optional
from sqlalchemy import text
from sqlalchemy.orm import Session

from storage.models import NewsArticle, CollectionRun, DedupCandidate
from config.settings import DB_SCHEMA


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
