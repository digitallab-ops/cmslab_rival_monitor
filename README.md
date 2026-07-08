# K-Beauty Intel — 경쟁사 인텔리전스 시스템

K-뷰티 주요 경쟁 브랜드의 글로벌 동향을 자동으로 수집·분류·시각화하는 모니터링 시스템.  
국내외 15개 미디어 소스를 매일 스캔하고, AI가 기사를 분류·한국어 번역하여 대시보드에 제공합니다.

**대시보드:** https://cmslab-rival-monitor.onrender.com

---

## 수집 소스 (15개)

수집은 6가지 수집기 모듈로 구성됩니다. 인증 불필요 — 전부 공개 RSS / 공개 API 기반입니다.

### 1. Google News RSS (`collectors/google_rss.py`)
Google News 검색 RSS를 브랜드명 × 국가 조합으로 쿼리합니다.

```
https://news.google.com/rss/search?q="{brand}" beauty&hl={hl}&gl={gl}&ceid={ceid}
```

국가별로 언어·지역 파라미터(`hl`, `gl`, `ceid`)를 다르게 설정해 현지 기사를 수집합니다.  
예) `hl=en-US&gl=US` (미국), `hl=ja&gl=JP` (일본), `hl=ko&gl=KR` (한국)

### 2. 글로벌 뷰티 전문 미디어 RSS (`collectors/media_rss.py`)
15개 뷰티 전문지·보도자료 서비스 RSS를 전부 수집한 뒤, 브랜드명 언급 여부로 필터링합니다.

| 분류 | 매체 |
|------|------|
| 미국 뷰티 업계지 | BeautyMatter, WWD Beauty, Glossy |
| 글로벌 뷰티 전문 | Global Cosmetics News, CosmeticsDesign Asia, CosmeticsDesign Europe |
| 보도자료 서비스 | PR Newswire, BusinessWire Cosmetics |
| 지역 미디어 | WWD Japan, Korea Herald, SCMP Lifestyle, Nikkei Asia |

### 3. 장업신문 (`collectors/jangup.py`)
국내 뷰티 전문지 장업신문 RSS 수집. KR 파이프라인에서 동작.  
브랜드 한국어명(`BRAND_KO_NAMES`)으로 추가 매칭.

### 4. PRTimes Japan (`collectors/prtimes.py`)
일본 PR 배포 서비스 PRTimes RSS 수집. JP 파이프라인에서만 동작.  
일본 시장 공식 발표문을 직접 수집합니다.

### 5. Naver News API (`collectors/naver_news.py`)
네이버 뉴스 검색 API (`openapi.naver.com/v1/search/news.json`) 사용.  
한국어 브랜드명으로 검색. KR 파이프라인 전용. 무료 API, 일 25,000 호출 한도.

### 6. Reddit RSS (`collectors/reddit_collector.py`)
Reddit 서브레딧 검색 RSS 사용. API 키 불필요.  
대상 서브레딧: `r/AsianBeauty`, `r/SkincareAddiction`, `r/KoreanBeauty`  
소비자 커뮤니티 반응(입점 소식·바이럴·리뷰 트렌드)을 수집합니다.

---

## 파이프라인 흐름

```
수집기 6종
  ↓
URL 해시 중복 제거 (SHA-256)
  ↓
제목 유사도 중복 제거 (자카드 유사도 ≥ 0.85)
  ↓
기사 본문 fetch (BeautifulSoup — Google News 제외)
  ↓
AI 분류 — 2단계 (gpt-4o-mini)
  Stage 1: 관련성 필터 (브랜드 언급 여부)
  Stage 2: 배치 구조화 분류 (8건/콜)
    - 브랜드, 시장 국가, 활동유형, 중요도
    - 한국어 제목 번역 (title_ko)
    - 한국어 본문 요약 (article_body_ko, 최대 500자)
    - brand_focus (primary / secondary / incidental)
    - source_country vs country (크로스마켓 분류)
  ↓
PostgreSQL 저장 (Supabase, rival_intel 스키마)
  ↓
대시보드 (FastAPI)          Slack 알림 (HIGH 즉시 발송)
```

### 국가 분류 방식

`country` 필드는 **기사 언어·출처가 아닌 기사가 다루는 실제 시장**을 기준으로 분류합니다.  
한국어 기사라도 내용이 "미국 세포라 입점"이면 → `US`.  
`source_country`는 수집 파이프라인이 어느 국가 조합으로 실행됐는지를 별도 저장합니다.

---

## 모니터링 대상

**Tier 1 브랜드 (매일 수집)**  
Anua · Beauty of Joseon · Dr.Jart+ · Skin1004 · Mediheal · Torriden · Dalba · By Wishtrend · Cos de Baha

**커버 국가 (16개)**  
`US` `JP` `KR` `GB` `DE` `FR` `PL` `SG` `TH` `ID` `MY` `VN` `PH` `AU` `CA` `CN`

---

## 대시보드 구성

| 섹션 | 내용 |
|------|------|
| Brand Radar | 최근 4주 vs 직전 4주 기사량 모멘텀 순위 |
| 브랜드 × 국가 히트맵 | 어느 브랜드가 어느 시장에서 활발한지 매트릭스 |
| 글로벌 신호 지도 | 국가별 HIGH/MED/LOW 기사 수 세계 지도 시각화 |
| HIGH/MED 기사 목록 | 중요도 기준 필터, 브랜드·활동유형 필터 |
| 전략 인사이트 카드 | 브랜드별 AI 요약 (gpt-4o-mini, 캐시 적용) |
| 기간 토글 | 30일 / 60일 / 90일 클라이언트 사이드 전환 |

기사 제목·본문은 영어·일본어 포함 전량 한국어 번역 제공. 원문 링크 함께 제공.

---

## 갱신 주기

| 시각 (KST) | 작업 |
|------------|------|
| 매일 18:00 | Tier1 브랜드 × 전체 국가 일별 수집 |
| 매주 월 19:00 | 브랜드 모멘텀 스코어 재계산 |
| 매주 월 20:00 | 전체 브랜드 × 전체 국가 풀스캔 |
| 매주 화 09:00 | 주간 브리핑 Slack 자동 발송 |
| 매 14분 | Render 슬립 방지 keep-alive 핑 |

---

## 기술 스택

| 항목 | 내용 |
|------|------|
| 서버 | FastAPI + uvicorn, Render Free |
| 스케줄러 | APScheduler BackgroundScheduler (FastAPI lifespan 통합) |
| DB | Supabase PostgreSQL (`rival_intel` 스키마) |
| ORM | SQLAlchemy + psycopg2 |
| AI 분류·번역 | OpenAI gpt-4o-mini (배치 8건/콜) |
| 주간 브리핑 | OpenAI gpt-4o |
| HTML 파싱 | BeautifulSoup4 |
| RSS 파싱 | feedparser |
| 대시보드 | 서버사이드 HTML 생성 (Chart.js + Canvas API) |

---

## 개발자 설치

```bash
cd CellFusionC_intel
pip install -r requirements.txt
cp .env.example .env   # .env에 아래 값 입력
```

**필수 환경변수**

```
OPENAI_API_KEY=...
DB_HOST=...
DB_USER=...
DB_PASSWORD=...
DB_NAME=postgres
SLACK_WEBHOOK_URL=...        # 선택
NAVER_CLIENT_ID=...          # 선택 (KR 네이버 수집용)
NAVER_CLIENT_SECRET=...
```

> `.env`는 절대 git 커밋 금지 — 실서버 크리덴셜 포함.

**DB 초기화 (최초 1회)**

```bash
python -c "from storage.models import create_tables, migrate_tables; create_tables(); migrate_tables()"
```

**로컬 실행**

```bash
uvicorn server:app --reload --port 8000
# http://localhost:8000
```
