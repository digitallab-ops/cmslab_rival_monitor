# cmslab_rival_monitor — 운영 인수인계 문서

K-뷰티 경쟁사 인텔리전스 자동 수집 시스템. 이 문서는 인수자가 시스템을 그대로 이어받아 운영할 수 있도록 작성되었습니다.

---

## 1. 시스템 개요

경쟁 브랜드 관련 기사를 매일 자동 수집하고, GPT-4o로 활동유형·중요도를 분류한 뒤 PostgreSQL에 저장합니다. HTML 대시보드와 Slack 알림으로 팀에 전달됩니다.

```
수집 (4개 소스)  →  중복 제거  →  GPT 분류  →  DB 저장  →  대시보드 / Slack
```

---

## 2. 환경 설정

### 2-1. 필수 패키지 설치

```bash
cd CellFusionC_intel
pip install -r requirements.txt
```

### 2-2. 환경변수 설정

```bash
cp .env.example .env
```

`.env` 파일에 아래 값을 채웁니다. **이 파일은 절대 git에 커밋하지 마세요.**

| 변수 | 설명 | 비고 |
|------|------|------|
| `OPENAI_API_KEY` | GPT-4o 분류용 | platform.openai.com |
| `ANTHROPIC_API_KEY` | Claude 전략 요약용 | console.anthropic.com |
| `DB_HOST` | Supabase PostgreSQL 호스트 | Supabase 프로젝트 설정 |
| `DB_PORT` | 포트 (기본 5432) | |
| `DB_USER` | DB 유저명 | |
| `DB_PASSWORD` | DB 비밀번호 | |
| `DB_NAME` | DB 이름 (기본 `postgres`) | |
| `SLACK_WEBHOOK_URL` | HIGH 기사 알림 Webhook | 선택사항 |

### 2-3. DB 초기화 (최초 1회)

```bash
cd CellFusionC_intel
python -c "from storage.models import create_tables, migrate_tables; create_tables(); migrate_tables()"
```

> 이미 테이블이 존재하면 `migrate_tables()`는 무시하고 넘어갑니다 (idempotent).

---

## 3. 스케줄 구조

`python main.py` 로 스케줄러를 실행하면 아래 4개 잡이 자동 동작합니다.

| 잡 | 실행 시각 (KST) | 내용 |
|----|----------------|------|
| `daily_tier1` | **매일 06:00** | Tier1 브랜드 5개 × Tier1 국가 6개 수집 |
| `weekly_full` | **매주 월 03:00** | 전체 21개 브랜드 × 14개 국가 풀스캔 |
| `weekly_dedup` | **매주 일 02:00** | 제목 유사도 중복 후보 DB 기록 |
| `weekly_briefing` | **매주 월 09:00** | 주간 브리핑 Slack 전송 |

**스케줄러 실행:**

```bash
cd CellFusionC_intel
python main.py
# Ctrl+C 로 종료
```

> 서버에서 백그라운드로 돌릴 경우:
> ```bash
> nohup python main.py >> ../scheduler.log 2>> ../scheduler_err.log &
> ```

---

## 4. 수집 대상

### Tier 1 — 매일 수집 (핵심 경쟁 브랜드)

| 브랜드 | 국가 |
|--------|------|
| Anua | US · JP · KR · SG · PL · TH |
| Mediheal | US · JP · KR · SG · PL · TH |
| By Wishtrend | US · JP · KR · SG · PL · TH |
| Cos de Baha | US · JP · KR · SG · PL · TH |
| Dalba | US · JP · KR · SG · PL · TH |

### Tier 2 — 주 1회 수집 (모니터링 브랜드 16개)

Dr.Jart+, Skin1004, Roundlab, Centellian24, VT Cosmetics, Numbuzin, b.plain, Goodal, Torriden, Abib, Rejuran, Mixsoon, Aestura, Zeroid, Beauty of Joseon, Celimax

### 수집 소스

| 소스 | 대상 국가 | 모듈 |
|------|----------|------|
| Google News RSS | 전체 | `collectors/google_rss.py` |
| BeautyMatter · WWD RSS | 전체 | `collectors/media_rss.py` |
| 장업신문 | KR | `collectors/jangup.py` |
| PRTimes Japan | JP | `collectors/prtimes.py` |

---

## 5. 주요 CLI 명령어

```bash
cd CellFusionC_intel

# 단일 브랜드·국가 즉시 수집 (테스트용)
python cli.py collect --brand Anua --country JP

# 전체 수집 (스케줄러 없이 한 번에)
python cli.py collect-all

# HTML 대시보드 생성
python cli.py report --days 30 --output report.html

# DB 현황 확인
python cli.py stats

# HIGH 기사 목록 출력
python cli.py high --days 7
```

---

## 6. DB 스키마

데이터베이스: Supabase PostgreSQL, 스키마: `rival_intel`

### 테이블: `news_articles` (핵심 테이블)

| 컬럼 | 타입 | 설명 |
|------|------|------|
| `id` | bigint PK | 자동 증가 |
| `url_hash` | varchar(64) UNIQUE | SHA-256 해시 (중복 방지) |
| `published_date` | timestamptz | 기사 발행일 |
| `brand` | varchar(100) | 브랜드명 |
| `country` | varchar(10) | 국가 코드 (US/JP/KR 등) |
| `activity_type` | varchar(50) | 활동유형 (아래 참고) |
| `importance` | varchar(10) | HIGH / MEDIUM / LOW |
| `details` | text | GPT 추출 핵심 내용 |
| `title_ko` | varchar(400) | 한국어 번역 제목 |
| `source_url` | text | 원문 URL |
| `source_name` | varchar(200) | 출처 미디어명 |
| `collected_at` | timestamptz | 수집 시각 |

### 활동유형 7종

`신시장_진출` / `유통_채널` / `신제품_런칭` / `인플루언서_협업` / `투자_BD` / `브랜드_마케팅` / `기타`

### 테이블: `collection_runs`

수집 실행 이력. 브랜드×국가별 수집 건수, 중복 건수, 소요시간 기록. 오류 추적용.

### 테이블: `dedup_candidates`

제목 유사도 0.85 이상인 기사 쌍 목록. 주간 중복 정리 잡이 채웁니다.

---

## 7. 브랜드·국가 추가 방법

`CellFusionC_intel/config/brands.py` 파일 수정:

```python
# 매일 수집 브랜드에 추가
TIER1_BRANDS = [
    "Anua",
    "새브랜드",   # ← 여기 추가
    ...
]

# 한국어 검색명 추가 (장업신문 수집에 필요)
BRAND_KO_NAMES = {
    "새브랜드": ["한국어브랜드명"],
    ...
}
```

국가 추가 시에는 `COUNTRIES` 딕셔너리에 Google News 파라미터(`hl`, `gl`, `ceid`)도 함께 등록해야 합니다.

---

## 8. API 비용 참고

| 작업 | 모델 | 기사당 비용 |
|------|------|------------|
| 분류 (필터링) | GPT-4o-mini | ~$0.0003 |
| 분류 (상세) | GPT-4o | ~$0.003 |
| 전략 요약 생성 | Claude Haiku 4.5 | ~$0.004/브랜드 |

일 평균 수집량 기준 **월 $5~15** 수준. `DAILY_TOKEN_BUDGET = 500_000` 설정으로 일일 토큰 상한 관리 중 (`config/settings.py`).

---

## 9. 트러블슈팅

**수집은 됐는데 DB에 저장이 안 됨**
→ `.env`의 DB 접속 정보 확인. `python cli.py stats`로 연결 테스트.

**분류가 안 됨 / OpenAI 오류**
→ `OPENAI_API_KEY` 확인. 키 잔액 확인 (platform.openai.com → Usage).

**장업신문 수집 0건**
→ 장업신문 RSS URL이 변경됐을 수 있음. `collectors/jangup.py` 상단 URL 확인.

**Slack 알림 안 옴**
→ `.env`의 `SLACK_WEBHOOK_URL` 값 확인. `None` 또는 빈 값이면 알림 미발송 (오류 아님).

**`migrate_tables` 오류**
→ Supabase DB 접속이 되는지 먼저 확인 후, 오류 메시지 보고 해당 컬럼이 이미 존재하는지 체크.

---

## 10. 파일 구조

```
CellFusionC_intel/
├── config/
│   ├── brands.py        # 브랜드 목록, 국가, 활동유형 정의
│   └── settings.py      # 환경변수 로드, 모델명, 임계값 설정
├── collectors/          # 수집기 4종 (google_rss, media_rss, jangup, prtimes)
├── classifier/          # GPT-4o 분류 파이프라인, 프롬프트, 스키마
├── deduplication/       # URL 해시, 제목 유사도 중복 제거
├── analytics/
│   ├── queries.py       # 대시보드용 집계 쿼리
│   └── summarizer.py    # Claude API 전략 요약 생성
├── dashboard/
│   └── generate.py      # HTML 대시보드 생성 (Canvas 차트 포함)
├── storage/
│   ├── models.py        # SQLAlchemy ORM 모델, 테이블 생성/마이그레이션
│   └── repository.py    # CRUD
├── scheduler/
│   ├── pipeline.py      # 수집→분류→저장 파이프라인 (CLI·스케줄러 공용)
│   ├── runner.py        # APScheduler 잡 정의
│   └── briefing.py      # 주간 브리핑 생성
├── notifications/
│   └── slack.py         # Slack Webhook 발송
├── cli.py               # Click CLI
├── main.py              # 스케줄러 진입점
├── requirements.txt
└── .env.example         # 환경변수 템플릿 (실제 값 없음)
```

---

## 11. 인수인계 체크리스트

- [ ] `.env` 파일 값 전달받음 (DB 접속 정보, API 키)
- [ ] Supabase 프로젝트 접근 권한 확인
- [ ] OpenAI 계정 접근 권한 확인
- [ ] Anthropic Console 접근 권한 확인
- [ ] Slack Webhook URL 확인
- [ ] `python cli.py stats` 실행해서 DB 연결 확인
- [ ] `python cli.py collect --brand Anua --country US` 로 수집 1회 테스트
- [ ] 스케줄러 서버 (현재 운영 서버 위치 확인 필요)
