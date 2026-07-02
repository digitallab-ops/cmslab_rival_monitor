import os
from dotenv import load_dotenv
from sqlalchemy.engine import URL

load_dotenv()

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# Naver News Search API (https://developers.naver.com)
NAVER_CLIENT_ID = os.getenv("NAVER_CLIENT_ID", "")
NAVER_CLIENT_SECRET = os.getenv("NAVER_CLIENT_SECRET", "")

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
CLASSIFIER_MODEL_DETAIL = "gpt-4o"

COLLECTION_INTERVAL_PRIORITY = 3600
COLLECTION_INTERVAL_ALL = 21600

RSS_REQUEST_DELAY = 3

TITLE_SIMILARITY_THRESHOLD = 0.85
DEDUP_WINDOW_DAYS = 3

DAILY_TOKEN_BUDGET = 500_000
