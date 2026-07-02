# K-Beauty Competitive Intelligence Monitor

> K-뷰티 경쟁 브랜드의 글로벌 동향을 **자동 수집 → AI 분류 → 전략 인사이트**로 변환하는 인텔리전스 파이프라인

![Python](https://img.shields.io/badge/Python-3.11-3776AB?style=flat-square&logo=python&logoColor=white)
![Claude API](https://img.shields.io/badge/Claude_API-Haiku_4.5-D97706?style=flat-square)
![OpenAI](https://img.shields.io/badge/OpenAI-GPT--4o-412991?style=flat-square&logo=openai&logoColor=white)
![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Supabase-3ECF8E?style=flat-square&logo=supabase&logoColor=white)
![SQLAlchemy](https://img.shields.io/badge/SQLAlchemy-2.0-red?style=flat-square)

---

## What It Does

경쟁 브랜드(Anua, Mediheal, By Wishtrend 등) 기사를 **하루에 수백 건** 자동 수집하고, AI가 활동 유형(인플루언서·신시장·신제품 등)과 중요도(HIGH/MEDIUM/LOW)를 분류한 뒤, 전략 담당자가 즉시 활용할 수 있는 HTML 대시보드로 제공합니다.

```
Google News RSS ─┐
BeautyMatter     ├─▶  Deduplicate  ─▶  Claude Classify  ─▶  PostgreSQL
장업신문 / WWD   ┘        ↓                                      ↓
PRTimes JP       ─────  URL Hash                          Dashboard HTML
                         Title Sim                        + Slack Alert
```

---

## Dashboard

| 섹션 | 설명 |
|------|------|
| **KPI 바** | 총 기사 수 · HIGH 비중 · 주요 국가 · 주력 활동유형 |
| **국가 히트맵** | 브랜드 × 국가 기사 수 히트맵 — 셀 클릭 시 HIGH/MEDIUM/LOW 분류 드릴다운 |
| **활동유형 스택바** | 브랜드별 활동 포트폴리오 (인플루언서·유통·신제품 등 6종) |
| **HIGH 비중 차트** | 브랜드별 고중요도 기사 비율 비교 |
| **전략 인사이트 카드** | Claude API가 생성한 브랜드별 2줄 전략 요약 + 근거 기사 3건 |
| **주간 트렌드** | 8주 HIGH/MEDIUM/LOW 건수 추이 |
| **기간 토글** | 최근 7일 / 30일 / 90일 전환 — 전체 대시보드 실시간 갱신 |

> 모든 차트는 **Chart.js 없이 Canvas API로 직접 구현** (자체 렌더러, 외부 CDN 의존 없음)

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│  Collectors (feedparser + requests + BeautifulSoup)             │
│  Google RSS · BeautyMatter · WWD · 장업신문 · PRTimes JP        │
└──────────────────────────┬──────────────────────────────────────┘
                           │ raw articles
┌──────────────────────────▼──────────────────────────────────────┐
│  Deduplication                                                  │
│  1) URL SHA-256 hash  2) 제목 자카드 유사도(≥ 0.85)             │
└──────────────────────────┬──────────────────────────────────────┘
                           │ unique articles
┌──────────────────────────▼──────────────────────────────────────┐
│  Classifier  (OpenAI GPT-4o)                                    │
│  · 활동유형 7종 분류     · 중요도 3단계(HIGH/MEDIUM/LOW)         │
│  · 한국어 제목 번역      · 핵심 세부사항 추출                    │
└──────────────────────────┬──────────────────────────────────────┘
                           │ structured records
┌──────────────────────────▼──────────────────────────────────────┐
│  Storage  (SQLAlchemy 2.0 + Supabase PostgreSQL)                │
│  rival_intel schema · brand · country · importance · activity   │
└────────────┬─────────────────────────────────┬──────────────────┘
             │                                 │
┌────────────▼─────────────┐    ┌──────────────▼──────────────────┐
│  Dashboard Generator      │    │  Notifications                  │
│  · Canvas API 커스텀 차트 │    │  Slack Webhook (HIGH only)      │
│  · Claude API 전략 요약   │    └─────────────────────────────────┘
│  · 기간별 필터링          │
└──────────────────────────┘
```

---

## Monitoring Coverage

**Tier 1** (매일 수집) — 핵심 경쟁 브랜드

| 브랜드 | 주력 시장 |
|--------|----------|
| Anua | US · JP · KR · SG · PL · TH · CA · GB |
| Mediheal | US · JP · KR · SG |
| By Wishtrend | US · JP · KR · SG |
| Cos de Baha | US · JP · SG |
| Dalba | JP · KR |

**Tier 2** (주 1회) — 16개 브랜드 추가 모니터링 (Dr.Jart+, Skin1004, Beauty of Joseon 등)

총 **21개 브랜드 × 14개 국가** 조합 모니터링

---

## Tech Stack

| 레이어 | 기술 |
|--------|------|
| **언어** | Python 3.11 |
| **AI 분류** | OpenAI GPT-4o (활동유형 · 중요도 · 번역) |
| **AI 인사이트** | Anthropic Claude Haiku 4.5 (전략 요약, ~$0.02/리포트) |
| **수집** | feedparser · requests · BeautifulSoup4 |
| **중복 제거** | SHA-256 URL 해시 + 자카드 유사도 |
| **DB** | PostgreSQL (Supabase) · SQLAlchemy 2.0 ORM |
| **스케줄링** | APScheduler |
| **알림** | Slack Incoming Webhook |
| **대시보드** | Vanilla JS + HTML5 Canvas API (커스텀 렌더러) |
| **CLI** | Click |

---

## Quick Start

```bash
# 1. 설치
cd CellFusionC_intel
pip install -r requirements.txt
cp .env.example .env   # API 키 · DB 정보 입력

# 2. DB 초기화
python -c "from storage.models import create_tables; create_tables()"

# 3. 즉시 수집
python cli.py collect --brand Anua --country JP

# 4. 전체 수집 + 리포트 생성
python cli.py collect-all
python cli.py report --days 30 --output report.html

# 5. 24시간 자동 스케줄러
python main.py
```

<details>
<summary>CLI 전체 명령어</summary>

```bash
python cli.py collect       --brand <BRAND> --country <CC>  # 단일 수집
python cli.py collect-all                                    # 전체 브랜드 × 국가
python cli.py report        --days 30 --output out.html      # HTML 리포트 생성
python cli.py stats                                          # DB 현황 요약
python cli.py high          --days 7                         # HIGH 기사 목록
```

</details>

---

## Project Structure

```
CellFusionC_intel/
├── collectors/
│   ├── google_rss.py       # Google News RSS 수집기
│   ├── media_rss.py        # BeautyMatter · WWD RSS
│   ├── jangup.py           # 장업신문 (한국어 미디어)
│   ├── prtimes.py          # PRTimes JP (일본어 미디어)
│   └── body_fetcher.py     # 기사 본문 추출
├── classifier/
│   ├── claude_classifier.py # GPT-4o 분류 파이프라인
│   ├── prompts.py           # 분류 프롬프트 관리
│   └── schemas.py           # Pydantic 응답 스키마
├── deduplication/
│   └── url_hasher.py        # URL 해시 + 제목 유사도
├── analytics/
│   ├── queries.py           # SQLAlchemy 집계 쿼리
│   └── summarizer.py        # Claude API 전략 요약 생성
├── dashboard/
│   └── generate.py          # HTML 대시보드 생성기 (Canvas 차트 포함)
├── storage/
│   ├── models.py            # SQLAlchemy ORM 모델
│   └── repository.py        # CRUD 레포지토리
├── scheduler/
│   ├── pipeline.py          # 수집 → 분류 → 저장 파이프라인
│   ├── runner.py            # APScheduler 실행기
│   └── briefing.py          # 일일 브리핑 생성
├── notifications/
│   └── slack.py             # Slack HIGH 기사 알림
├── config/
│   ├── brands.py            # 브랜드 · 국가 · 활동유형 설정
│   └── settings.py          # 환경변수 로드
├── cli.py                   # Click CLI 진입점
├── main.py                  # 스케줄러 진입점
├── requirements.txt
└── .env.example
```

---

## Key Design Decisions

**왜 Chart.js를 쓰지 않았나?**
대시보드는 단일 HTML 파일로 배포되며 외부 CDN 접근이 없는 환경에서도 동작해야 합니다. Canvas API로 직접 구현해 의존성 0, 번들 크기 최소화, 히트맵·스택바·라인차트의 인터랙션(클릭·드릴다운·스크롤)을 완전히 제어할 수 있습니다.

**왜 GPT-4o + Claude를 함께 쓰나?**
GPT-4o는 구조화된 JSON 분류(활동유형·중요도)에, Claude Haiku는 자연스러운 한국어 전략 문장 생성에 각각 더 적합합니다. 비용 최적화: 분류(대량·반복)는 GPT-4o Mini 로 전환 가능, 요약(저빈도·고품질)은 Haiku 유지.

**왜 Supabase를 선택했나?**
팀 공유 DB + REST API + 실시간 구독을 PostgreSQL 위에서 즉시 사용할 수 있어 인프라 관리 없이 프로덕션 수준의 DB를 확보할 수 있습니다.

---

## Environment Variables

```env
# .env.example 참고
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...

DB_HOST=...
DB_PORT=5432
DB_USER=...
DB_PASSWORD=...
DB_NAME=postgres

SLACK_WEBHOOK_URL=        # 선택사항
```

---

*Built for CellFusion C — K-뷰티 글로벌 전략팀 내부 인텔리전스 도구*
