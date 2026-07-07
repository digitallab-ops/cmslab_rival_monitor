# K-Beauty Intel — 경쟁사 인텔리전스 시스템

K-뷰티 주요 경쟁 브랜드의 글로벌 동향을 자동으로 수집·분류·시각화하는 모니터링 시스템입니다.  
구글 뉴스, 네이버, 장업신문, Reddit 등 국내외 미디어를 매일 스캔하고 AI가 한국어로 정리해 대시보드에 제공합니다.

---

## 대시보드

**접속 URL:** https://cmslab-rival-monitor.onrender.com

> 첫 접속 시 20~30초 로딩이 걸릴 수 있습니다 (Render 무료 플랜 특성상).

---

## 무엇을 보여주나

| 화면 | 내용 |
|------|------|
| **Brand Radar** | 최근 4주 vs 직전 4주 기사량 비교 — 급부상·모멘텀 감소 브랜드 한눈에 파악 |
| **브랜드 × 국가 히트맵** | 어느 브랜드가 어느 시장에서 활발한지 매트릭스로 표시 |
| **HIGH 기사 목록** | M&A, 신시장 진출, 대형 파트너십 등 중요도 HIGH 기사만 필터 |
| **전략 인사이트 카드** | 브랜드별 최근 활동 AI 요약 (2문장) |
| **기간 토글** | 최근 7일 / 30일 / 90일 선택해서 트렌드 확인 |

기사 제목과 내용은 영어·일본어 포함 **전량 한국어 번역** 제공. 원문 링크도 함께 제공합니다.

---

## 모니터링 대상

### 수집 브랜드 (9개)

| Tier | 브랜드 | 수집 주기 |
|------|--------|----------|
| **Tier 1** | Anua, Beauty of Joseon, Dr.Jart+, Skin1004, Mediheal, Torriden, Dalba, By Wishtrend, Cos de Baha | **매일** |
| **Tier 2** | DB 기반 관리 (별도 목록) | 주 1회 |

### 커버 국가 (16개)

`US` `JP` `KR` `CN` `GB` `DE` `FR` `PL` `SG` `TH` `ID` `MY` `VN` `PH` `AU` `CA`

### 수집 소스

| 소스 | 특징 |
|------|------|
| Google News RSS | 브랜드명 × 국가 조합 검색, 전체 언어 커버 |
| BeautyMatter / WWD / Glossy | 글로벌 뷰티 전문 미디어 |
| 장업신문 | 국내 뷰티 전문지 |
| PRTimes Japan | 일본 PR 배포 기사 |
| Naver News | 네이버 뉴스 검색 |
| Reddit | 글로벌 소비자 커뮤니티 반응 |

---

## 데이터 갱신 주기

| 시각 (KST) | 작업 |
|------------|------|
| **매일 18:00** | Tier1 브랜드 × 전체 국가 일별 수집 |
| **매주 월요일 19:00** | 브랜드 모멘텀 스코어 갱신 |
| **매주 월요일 20:00** | 전체 브랜드 × 전체 국가 풀스캔 |
| **매주 화요일 09:00** | 주간 브리핑 Slack 발송 |

---

## 기사 분류 기준

수집된 기사는 AI가 아래 기준으로 자동 분류합니다.

**활동유형 7종**

| 유형 | 설명 |
|------|------|
| `신시장_진출` | 신규 국가/시장 공식 진출, 현지 미디어 최초 등장 |
| `유통_채널` | Sephora·Amazon·Ulta·올리브영 글로벌 등 채널 입점·확장 |
| `신제품_런칭` | 신규 성분·포뮬라 제품, 카테고리 확장 (색조·바디 등) |
| `인플루언서_협업` | KOL·유튜버·TikToker 바이럴, 앰배서더, 협찬 캠페인 |
| `투자_BD` | 투자 유치, 해외 법인, 유통 파트너십, M&A |
| `브랜드_마케팅` | 포지셔닝 변경, 수상·인증, 팝업스토어, PR 캠페인 |
| `기타` | 위 유형에 해당하지 않는 관련 뉴스 |

**중요도 3단계**

- `HIGH` — 신규 시장·채널 진출, 대규모 투자·M&A, 주요 글로벌 파트너십
- `MEDIUM` — 신제품 해외 출시, 인플루언서 협업, 지역 캠페인
- `LOW` — 단순 제품 언급, 소규모 프로모션

**brand_focus** (기사 내 브랜드 비중)

- `primary` — 기사의 주인공이 해당 브랜드
- `secondary` — 여러 브랜드 중 하나로 의미있게 다뤄짐
- `incidental` — 업계 동향 기사에서 예시로 잠깐 언급

**국가 분류** — 기사 출처 언어가 아닌 **기사가 다루는 실제 시장** 기준.  
한국어 기사라도 내용이 "미국 세포라 입점"이면 `US`로 분류.

---

## 시스템 구조

```
수집 (6개 소스)
  ↓
중복 제거 (URL 해시 + 제목 유사도)
  ↓
AI 분류 (gpt-4o-mini)
  브랜드 / 국가 / 활동유형 / 중요도 / 한국어 번역
  ↓
PostgreSQL 저장 (Supabase)
  ↓
대시보드 (FastAPI + HTML)      Slack 알림 (HIGH 기사 즉시)
```

---

## 알림

- **HIGH 기사 발생 즉시** — Slack 채널 자동 전송
- **매주 화요일 09:00** — 지난주 경쟁사 동향 주간 브리핑 Slack 발송

---

---

## 개발자용 — 로컬 설치 및 운영

### 환경 설정

```bash
cd CellFusionC_intel
pip install -r requirements.txt
cp .env.example .env   # .env 파일에 아래 값 입력
```

**필수 환경변수**

| 변수 | 설명 |
|------|------|
| `OPENAI_API_KEY` | AI 분류·번역용 (platform.openai.com) |
| `DB_HOST` | Supabase PostgreSQL 호스트 |
| `DB_USER` / `DB_PASSWORD` | DB 인증 정보 |
| `DB_NAME` | DB 이름 (기본 `postgres`) |
| `SLACK_WEBHOOK_URL` | Slack 알림 Webhook (선택) |
| `RENDER_EXTERNAL_URL` | Render 자동 주입 — keep-alive 핑용 |

> **⚠️ `.env` 파일은 절대 git에 커밋하지 마세요.** 실서버 크리덴셜이 포함되어 있습니다.

### DB 초기화 (최초 1회)

```bash
python -c "from storage.models import create_tables, migrate_tables; create_tables(); migrate_tables()"
```

### 로컬 서버 실행

```bash
uvicorn server:app --host 0.0.0.0 --port 8000 --reload
# http://localhost:8000 → 대시보드
# http://localhost:8000/health → 헬스체크
```

### 주요 CLI 명령어

```bash
# 단일 수집 테스트
python cli.py collect --brand Anua --country JP

# HTML 리포트 파일 생성
python cli.py report --days 30 --output report.html

# DB 현황 확인
python cli.py stats

# HIGH 기사 목록
python cli.py high --days 7
```

### 브랜드·국가 추가

`monitored_brands` DB 테이블에서 직접 관리 (코드 재배포 없이 반영).  
또는 `config/brands.py`의 `TIER1_BRANDS` / `ALL_BRANDS` 수정 후 재배포.

국가 추가 시 `config/brands.py`의 `COUNTRIES` 딕셔너리에 Google News 파라미터(`hl`, `gl`, `ceid`)도 함께 등록.

### API 비용

| 작업 | 모델 | 단가 |
|------|------|------|
| 관련성 필터 (Stage 1) | gpt-4o-mini | ~$0.0001/기사 |
| 상세 분류 (Stage 2) | gpt-4o-mini | ~$0.0005/기사 |
| 전략 요약 | gpt-4o-mini | ~$0.0003/브랜드 |
| 주간 브리핑 | gpt-4o | ~$0.005/회 |

일 수집량 기준 **월 $1~3** 수준.

### 파일 구조

```
CellFusionC_intel/
├── config/
│   ├── brands.py        # 브랜드 목록, 국가 정의
│   └── settings.py      # 환경변수, 모델명, 임계값
├── collectors/          # 수집기 6종
├── classifier/          # AI 분류 파이프라인, 프롬프트, 스키마
├── deduplication/       # URL 해시, 제목 유사도 중복 제거
├── analytics/
│   ├── queries.py       # 대시보드용 집계 쿼리
│   └── summarizer.py    # AI 전략 요약 생성
├── dashboard/
│   └── generate.py      # HTML 대시보드 렌더링
├── storage/
│   ├── models.py        # SQLAlchemy ORM, 마이그레이션
│   └── repository.py    # CRUD
├── scheduler/
│   ├── pipeline.py      # 수집→분류→저장 파이프라인
│   ├── runner.py        # APScheduler 잡 정의 (BackgroundScheduler)
│   └── briefing.py      # 주간 브리핑 생성
├── notifications/
│   └── slack.py         # Slack Webhook 발송
├── server.py            # FastAPI 앱 + lifespan 스케줄러
├── cli.py               # Click CLI
└── requirements.txt
```

### 트러블슈팅

**수집은 됐는데 저장 안 됨** → `.env` DB 접속 정보 확인, `python cli.py stats`로 연결 테스트

**분류 오류 / OpenAI 에러** → `OPENAI_API_KEY` 확인, platform.openai.com → Usage 잔액 확인

**장업신문 0건** → RSS URL 변경 가능성, `collectors/jangup.py` 상단 URL 확인

**Slack 알림 없음** → `.env`의 `SLACK_WEBHOOK_URL` 값 확인 (빈 값이면 알림 미발송, 오류 아님)

**첫 접속 느림** → Render 무료 플랜 특성. 서버가 15분 비활성 후 슬립 → keep-alive 핑(14분 주기)으로 방지 중이나 배포 직후엔 느릴 수 있음

---

## 운영 환경

- **서버:** Render (Free tier) — https://cmslab-rival-monitor.onrender.com
- **DB:** Supabase PostgreSQL (`rival_intel` 스키마)
- **스케줄러:** APScheduler BackgroundScheduler (FastAPI lifespan에 통합)
- **리포지토리:** GitHub (`digitallab-ops/cmslab_rival_monitor`)
