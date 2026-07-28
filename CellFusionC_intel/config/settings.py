import os
from dotenv import load_dotenv
from sqlalchemy.engine import URL

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# Naver News Search API (https://developers.naver.com)
NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET", "")

# YouTube Data API v3 (https://console.cloud.google.com — 무료 1만 유닛/일)
# 미설정 시 YouTube 수집기는 자동 스킵
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")

DB_SCHEMA = "rival_intel"

# DB 연결 — 비밀번호 특수문자 문제를 피하기 위해 SQLAlchemy URL.create() 사용
DATABASE_URL = URL.create(
    drivername="postgresql+psycopg2",
    username=os.getenv("DB_USER", "postgres"),
    password=os.getenv("DB_PASSWORD", "postgres"),
    host=os.getenv("DB_HOST", "localhost"),
    port=int(os.getenv("DB_PORT", "5432")),
    database=os.getenv("DB_NAME", "postgres"),
)

CLASSIFIER_MODEL_FILTER = "gpt-4o-mini"
CLASSIFIER_MODEL_DETAIL = "gpt-4o-mini"

# 의미 중복 병합
EMBED_MODEL = "text-embedding-3-small"
DEDUP_COSINE_THRESHOLD = 0.60   # 전이적(union-find) 병합 임계값. 브랜드 내 같은 사건 체인
                                # (실측: 같은 사건 0.42~0.85 체인연결, 무관 기사 0.17~0.30 → 안전)

COLLECTION_INTERVAL_PRIORITY = 3600
COLLECTION_INTERVAL_ALL = 21600

RSS_REQUEST_DELAY = 3

TITLE_SIMILARITY_THRESHOLD = 0.85
DEDUP_WINDOW_DAYS = 3

DAILY_TOKEN_BUDGET = 500_000
