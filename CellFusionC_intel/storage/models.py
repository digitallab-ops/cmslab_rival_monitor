from datetime import datetime
from sqlalchemy import (
    BigInteger, Boolean, Column, Float, Index, Integer, ARRAY, String, Text, TIMESTAMP, create_engine
)
from sqlalchemy.orm import DeclarativeBase, Session

from config.settings import DATABASE_URL, DB_SCHEMA


class Base(DeclarativeBase):
    pass


class NewsArticle(Base):
    __tablename__ = "news_articles"

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    url_hash = Column(String(64), unique=True, nullable=False)

    # PRD 8개 핵심 필드
    published_date = Column(TIMESTAMP(timezone=True), nullable=False)
    brand = Column(String(100), nullable=False)
    country = Column(String(10), nullable=False)
    activity_type = Column(String(50), nullable=False)
    details = Column(Text, nullable=False)
    source_url = Column(Text, nullable=False)
    importance = Column(String(10), nullable=False)
    note = Column(Text)

    # 보조 필드
    title = Column(Text, nullable=False)
    source_name = Column(String(200))
    language = Column(String(10))

    # 제품 정보
    product_name = Column(String(200))   # GPT가 추출한 언급 제품명

    # 가이드 확장 필드 (Phase 3)
    channel = Column(String(200))          # 입점·유통 채널/리테일러명
    price_info = Column(String(300))       # 가격·프로모션 정보
    city = Column(String(100))             # 구체 도시명
    evidence_level = Column(String(20))    # official / editorial / pr / rehash
    strategic_score = Column(Integer)      # 100점 전략 중요도 스코어

    # 의미 중복 병합 (semantic dedup)
    is_duplicate = Column(Boolean, default=False)   # 대표 아닌 중복 기사
    dup_of = Column(BigInteger)                     # 대표 기사 id
    embedding = Column(Text)                        # title+details 임베딩 캐시(JSON)

    # 기사 본문 (원문 + 한국어 번역)
    article_body = Column(Text)          # URL fetch로 가져온 원문 본문 (최대 2000자)
    title_ko = Column(String(400))       # GPT가 번역한 제목 한국어
    article_body_ko = Column(Text)       # GPT가 번역·요약한 본문 한국어 (최대 500자)

    # 분류 메타데이터
    brand_focus = Column(String(20))        # primary / secondary / incidental (NULL=구기사)
    classification_confidence = Column(Float)
    classifier_model = Column(String(50))
    key_ingredients = Column(Text)          # 기사에 언급된 핵심 성분/포뮬러 (쉼표구분, 예: "PDRN,센텔라")
    sentiment = Column(String(10))          # positive / neutral / negative (브랜드 관점 톤)

    # 수집 메타데이터
    source_country = Column(String(10))    # 파이프라인 수집 국가 (country와 다를 수 있음)
    collected_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    collector_type = Column(String(50))

    __table_args__ = (
        Index("idx_news_brand", "brand"),
        Index("idx_news_country", "country"),
        Index("idx_news_activity", "activity_type"),
        Index("idx_news_date", "published_date"),
        Index("idx_news_brand_country", "brand", "country"),
        {"schema": DB_SCHEMA},
    )


class CollectionRun(Base):
    __tablename__ = "collection_runs"
    __table_args__ = {"schema": DB_SCHEMA}

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    run_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    collector_type = Column(String(50))
    brand = Column(String(100))
    country = Column(String(10))
    articles_found = Column(BigInteger, default=0)
    articles_new = Column(BigInteger, default=0)
    articles_duped = Column(BigInteger, default=0)
    error_message = Column(Text)
    duration_secs = Column(Float)


class DedupCandidate(Base):
    __tablename__ = "dedup_candidates"
    __table_args__ = {"schema": DB_SCHEMA}

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    article_id_1 = Column(BigInteger, nullable=False)
    article_id_2 = Column(BigInteger, nullable=False)
    similarity = Column(Float)
    reviewed = Column(Boolean, default=False)
    created_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)


class BrandInsight(Base):
    """브랜드별 날짜 범위 AI 인사이트 캐시."""
    __tablename__ = "brand_insights"
    __table_args__ = (
        Index("uq_brand_insight_range", "brand", "from_date", "to_date", unique=True),
        {"schema": DB_SCHEMA},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    brand = Column(String(100), nullable=False)
    from_date = Column(TIMESTAMP(timezone=True), nullable=False)
    to_date = Column(TIMESTAMP(timezone=True), nullable=False)
    summary = Column(Text, nullable=False)
    top_act = Column(String(50))
    top_pct = Column(Integer)
    high_pct = Column(Float)
    generated_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)


class BrandCountryInsight(Base):
    """브랜드×국가 단위 날짜 범위 AI 인사이트 캐시 (히트맵 셀 드릴다운용)."""
    __tablename__ = "brand_country_insights"
    __table_args__ = (
        Index("uq_brand_country_insight_range",
              "brand", "country", "from_date", "to_date", unique=True),
        {"schema": DB_SCHEMA},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    brand = Column(String(100), nullable=False)
    country = Column(String(10), nullable=False)
    from_date = Column(TIMESTAMP(timezone=True), nullable=False)
    to_date = Column(TIMESTAMP(timezone=True), nullable=False)
    summary = Column(Text, nullable=False)
    high_count = Column(Integer)
    med_count = Column(Integer)
    generated_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)


class MonitoredBrand(Base):
    """동적 브랜드 티어 + 모멘텀 관리."""
    __tablename__ = "monitored_brands"
    __table_args__ = {"schema": DB_SCHEMA}

    name            = Column(String(100), primary_key=True)
    tier            = Column(Integer, default=2)       # 1=daily, 2=weekly
    ko_names        = Column(ARRAY(String))
    is_active       = Column(Boolean, default=True)
    momentum_score  = Column(Float, default=0.0)
    last_scored     = Column(TIMESTAMP(timezone=True))
    tier_changed_at = Column(TIMESTAMP(timezone=True))
    note            = Column(Text)


class Briefing(Base):
    """생성된 브리핑 보관 (주간/일간). 기간별 조회·봇 리트리브용."""
    __tablename__ = "briefings"
    __table_args__ = (
        Index("ix_briefings_kind_time", "kind", "generated_at"),
        {"schema": DB_SCHEMA},
    )

    id = Column(BigInteger, primary_key=True, autoincrement=True)
    kind = Column(String(20), nullable=False)            # 'weekly' | 'daily'
    generated_at = Column(TIMESTAMP(timezone=True), default=datetime.utcnow)
    period_from = Column(TIMESTAMP(timezone=True))
    period_to = Column(TIMESTAMP(timezone=True))
    content = Column(Text, nullable=False)
    total = Column(Integer)
    high = Column(Integer)
    brands = Column(Integer)
    countries = Column(Integer)
    model = Column(String(40))


def ensure_briefings_table(session) -> None:
    """briefings 테이블 idempotent 생성 (비파괴적). 저장 전 1회 호출."""
    from sqlalchemy import text
    session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.briefings (
            id BIGSERIAL PRIMARY KEY,
            kind VARCHAR(20) NOT NULL,
            generated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            period_from TIMESTAMP WITH TIME ZONE,
            period_to TIMESTAMP WITH TIME ZONE,
            content TEXT NOT NULL,
            total INTEGER, high INTEGER, brands INTEGER, countries INTEGER,
            model VARCHAR(40)
        )
    """))
    session.execute(text(
        f"CREATE INDEX IF NOT EXISTS ix_briefings_kind_time "
        f"ON {DB_SCHEMA}.briefings (kind, generated_at DESC)"
    ))
    session.commit()


def save_briefing(session, kind: str, content: str, stats: dict,
                  period_from=None, period_to=None, model: str = "") -> None:
    """브리핑 1건 저장 (테이블 없으면 생성)."""
    from sqlalchemy import text
    if not (content or "").strip():
        return
    ensure_briefings_table(session)
    session.execute(text(f"""
        INSERT INTO {DB_SCHEMA}.briefings
            (kind, period_from, period_to, content, total, high, brands, countries, model)
        VALUES (:kind, :pf, :pt, :content, :total, :high, :brands, :countries, :model)
    """), {
        "kind": kind, "pf": period_from, "pt": period_to, "content": content,
        "total": stats.get("total"), "high": stats.get("high"),
        "brands": stats.get("brands"), "countries": stats.get("countries"),
        "model": model,
    })
    session.commit()


_engine = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(
            DATABASE_URL,
            pool_size=3,
            max_overflow=2,
            pool_pre_ping=True,
            connect_args={"sslmode": "require"},
        )
    return _engine


def create_tables():
    from sqlalchemy import text
    engine = get_engine()
    with engine.connect() as conn:
        conn.execute(text(f"CREATE SCHEMA IF NOT EXISTS {DB_SCHEMA}"))
        conn.commit()
    Base.metadata.create_all(engine)
    print(f"DB 테이블 생성 완료 (schema: {DB_SCHEMA})")
    return engine


def migrate_tables():
    """기존 테이블에 신규 컬럼 추가 (idempotent — 이미 있으면 무시)."""
    from sqlalchemy import text
    engine = get_engine()
    migrations = [
        f"ALTER TABLE {DB_SCHEMA}.news_articles ADD COLUMN IF NOT EXISTS product_name VARCHAR(200)",
        f"ALTER TABLE {DB_SCHEMA}.news_articles ADD COLUMN IF NOT EXISTS article_body TEXT",
        f"ALTER TABLE {DB_SCHEMA}.news_articles ADD COLUMN IF NOT EXISTS title_ko VARCHAR(400)",
        f"ALTER TABLE {DB_SCHEMA}.news_articles ADD COLUMN IF NOT EXISTS article_body_ko TEXT",
        f"ALTER TABLE {DB_SCHEMA}.news_articles ADD COLUMN IF NOT EXISTS source_country VARCHAR(10)",
        f"ALTER TABLE {DB_SCHEMA}.news_articles ADD COLUMN IF NOT EXISTS brand_focus VARCHAR(20)",
        f"ALTER TABLE {DB_SCHEMA}.news_articles ADD COLUMN IF NOT EXISTS channel VARCHAR(200)",
        f"ALTER TABLE {DB_SCHEMA}.news_articles ADD COLUMN IF NOT EXISTS price_info VARCHAR(300)",
        f"ALTER TABLE {DB_SCHEMA}.news_articles ADD COLUMN IF NOT EXISTS city VARCHAR(100)",
        f"ALTER TABLE {DB_SCHEMA}.news_articles ADD COLUMN IF NOT EXISTS evidence_level VARCHAR(20)",
        f"ALTER TABLE {DB_SCHEMA}.news_articles ADD COLUMN IF NOT EXISTS strategic_score INTEGER",
        f"ALTER TABLE {DB_SCHEMA}.news_articles ADD COLUMN IF NOT EXISTS is_duplicate BOOLEAN DEFAULT FALSE",
        f"ALTER TABLE {DB_SCHEMA}.news_articles ADD COLUMN IF NOT EXISTS dup_of BIGINT",
        f"ALTER TABLE {DB_SCHEMA}.news_articles ADD COLUMN IF NOT EXISTS embedding TEXT",
        # 성분·포뮬러 트렌드(①) + 감성·논란 탐지(②)
        f"ALTER TABLE {DB_SCHEMA}.news_articles ADD COLUMN IF NOT EXISTS key_ingredients TEXT",
        f"ALTER TABLE {DB_SCHEMA}.news_articles ADD COLUMN IF NOT EXISTS sentiment VARCHAR(10)",
    ]
    with engine.connect() as conn:
        for sql in migrations:
            try:
                conn.execute(text(sql))
                conn.commit()
                print(f"마이그레이션 완료: {sql.split('ADD')[1].strip()}")
            except Exception as e:
                print(f"마이그레이션 스킵 ({e})")
        # brand_insights 테이블 — from_date/to_date 기반으로 재생성
        try:
            conn.execute(text(f"DROP TABLE IF EXISTS {DB_SCHEMA}.brand_insights"))
            conn.execute(text(f"""
                CREATE TABLE {DB_SCHEMA}.brand_insights (
                    id BIGSERIAL PRIMARY KEY,
                    brand VARCHAR(100) NOT NULL,
                    from_date TIMESTAMP WITH TIME ZONE NOT NULL,
                    to_date TIMESTAMP WITH TIME ZONE NOT NULL,
                    summary TEXT NOT NULL,
                    top_act VARCHAR(50),
                    top_pct INTEGER,
                    high_pct FLOAT,
                    generated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
                    UNIQUE(brand, from_date, to_date)
                )
            """))
            conn.commit()
            print("brand_insights 테이블 재생성 완료")
        except Exception as e:
            print(f"brand_insights 테이블 생성 스킵 ({e})")
    return engine


def get_session(engine=None) -> Session:
    from sqlalchemy.orm import sessionmaker
    if engine is None:
        engine = get_engine()
    SessionLocal = sessionmaker(bind=engine)
    return SessionLocal()
