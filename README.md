# K-Beauty Intel — 경쟁사 인텔리전스 시스템

셀퓨전씨(씨엠에스랩)의 글로벌 경쟁 브랜드 동향을 **자동으로 수집·AI 분류·한국어 번역·시각화**하는 모니터링 시스템.
매일 저녁 국내외 주요 미디어를 스캔하고, 매일 아침·매주 월요일 아침 Slack으로 브리핑을 발송합니다.

**대시보드:** https://cmslab-rival-monitor.onrender.com

---

## 무엇을 해주는가 (한눈에)

| | 내용 |
|---|---|
| 🌍 **수집** | 21개 브랜드 × 23개국 × 6종 수집기 — 매일 저녁 자동 |
| 🤖 **AI 분류** | 활동유형·중요도·전략스코어·근거수준 판정 + 한국어 번역 |
| 🧹 **중복 병합** | 같은 사건을 다른 제목으로 보도한 기사를 하나로 묶음 (의미 임베딩) |
| 📊 **대시보드** | 브랜드 모멘텀·국가 히트맵·세계 신호지도·카테고리 대결 뷰 |
| 📨 **브리핑** | 매일 아침 8시 일간 + 매주 월 아침 8시 심층 주간 (Slack) |

---

## 수집 소스 (6종 수집기)

전부 공개 RSS / 공개 API 기반 — 인증 불필요.

| # | 수집기 | 소스 | 범위 |
|---|--------|------|------|
| 1 | `google_rss.py` | Google News RSS (브랜드 × 국가) | 전 국가. 국가별 언어·지역 파라미터로 현지 기사 수집 |
| 2 | `media_rss.py` | 글로벌 뷰티 전문지 RSS **30개 피드** | 국가 비종속 — 기사 내용으로 시장 판정 |
| 3 | `jangup.py` | 장업신문 (국내 뷰티 전문지) | KR |
| 4 | `prtimes.py` | PRTimes Japan (일본 PR 배포) | JP |
| 5 | `naver_news.py` | 네이버 뉴스 검색 API | KR |
| 6 | `reddit_collector.py` | Reddit (r/AsianBeauty 등) | 소비자 커뮤니티 반응 |

**전문지 30개 피드 (media_rss):** BeautyMatter, WWD Beauty, Glossy, Global Cosmetics News, Cosmetics Business, Premium Beauty News, CosmeticsDesign Asia/Europe, PR Newswire, BusinessWire, WWD Japan, Korea Herald, SCMP Lifestyle, Nikkei Asia, TheIndustry.beauty(UK), RetailDetail(Benelux), Inside Retail Asia, ET BrandEquity(IN), Campaign ME, Bizcommunity(AF) 등.
- **확장시장 보강(2026-07):** Pambianco Beauty(IT), Wiadomości Kosmetyczne(PL), Mercado & Consumo·Cosmetic Innovation(BR), Communicate·Arabian Business(ME), afaqs!(IN), Brand Communicator(NG), Female Daily(ID) — RSS 실응답 검증된 것만 채택. Atualidade Cosmética·IndiaRetailing 등은 RSS 미제공/차단으로 제외(Google News가 간접 커버).

### 현지어 심층 수집
주간 풀스캔 시 브랜드명을 **현지어 활동 키워드**와 결합해 recall을 높입니다.
예) 브라질 `"cosméticos coreanos" "lançamento"`, 태국 `"เครื่องสำอางเกาหลี" "เปิดตัว"`, 사우디 아랍어 키워드 등.

---

## 파이프라인 흐름

```
수집기 6종
  ↓  URL 해시 중복 제거 (SHA-256)
  ↓  제목 유사도 1차 필터 (SequenceMatcher ≥ 0.85)
  ↓  기사 본문 fetch (BeautifulSoup)
  ↓  AI 분류 — 2단계 (gpt-4o-mini, 배치 8건/콜)
  │    Stage 1: 관련성 필터 (브랜드 실제 언급 여부)
  │    Stage 2: 구조화 분류 (아래 필드)
  ↓  PostgreSQL 저장 (Supabase · rival_intel 스키마)
  ↓  [23:00] 의미 임베딩 중복 병합 (같은 사건 → 대표 1건)
  ↓
대시보드 (FastAPI)      Slack 브리핑 (일간/주간)
```

### AI가 뽑아내는 필드
| 필드 | 설명 |
|------|------|
| `activity_type` | **9종**: 신시장_진출 · 유통_채널 · 신제품_런칭 · 인플루언서_협업 · 투자_BD · 브랜드_마케팅 · 실적_공시 · 가격_프로모션 · 기타 |
| `importance` | high / medium / low |
| `strategic_score` | **0~100 전략 중요도** (75+ high · 55~74 medium 정합) |
| `evidence_level` | **근거 수준**: official(공식) · editorial(편집기사) · pr(보도자료) · rehash(재게재) |
| `brand_focus` | primary(주인공) · secondary · incidental(스쳐 언급) |
| `channel` | 입점·유통 채널/리테일러 (Sephora, Watsons, Nykaa …) |
| `price_info` `city` `product_name` | 가격·프로모션 / 도시 / 제품명 (있을 때만) |
| `country` vs `source_country` | 기사가 **다루는 시장** vs 수집이 실행된 국가 조합 (크로스마켓 분류) |
| `title_ko` `article_body_ko` | 제목·본문 한국어 번역 (영·일 포함 전량) |

> **국가 분류:** `country`는 기사 언어·출처가 아니라 **기사가 다루는 실제 시장** 기준. 한국어 기사라도 "미국 세포라 입점"이면 → `US`.

### 의미 임베딩 중복 병합 (`semantic_dedup.py`)
제목 문자열 유사도로는 **같은 사건을 다른 제목으로 보도한 기사**(매체별 재보도)를 못 잡습니다.
→ `title+details` 임베딩(text-embedding-3-small) 코사인 유사도로 **브랜드 내 같은 사건을 클러스터링**, 대표 1건(최고 strategic_score)만 남기고 나머지는 `is_duplicate` 플래그(삭제 아님 — 되돌리기 가능).
- 전이적(union-find) 클러스터링 + **같은 활동유형만** 병합 → 교차토픽 오병합 방지.
- incidental(다이제스트·나열형) 제외 → 클러스터 오염 방지.
- 매일 23:00 자동 실행. (실측: 최근분 약 31% 중복 병합, 오병합 0)

---

## 모니터링 대상

**Tier 1 브랜드 (매일 수집, 7개)**
Anua · Mediheal · Dalba · Beauty of Joseon · Skin1004 · Dr.Jart+ · Torriden

**Tier 2 브랜드 (주간 수집, 14개)**
Cos de Baha · By Wishtrend · Roundlab · Centellian24 · VT Cosmetics · Numbuzin · b.plain · Goodal · Abib · Rejuran · Mixsoon · Aestura · Zeroid · Celimax

> 티어는 `monitored_brands` 테이블에서 **모멘텀 기반으로 자동 승급/강등**됩니다 (쿨다운 14일, 플립플롭 방지).

**커버 국가 (23개)**
- **Tier1 (매일):** `US` `PL` `JP` `TH` `SG` `CN` `KR` `GB` `CA` `AU` `ID` `MY` `VN`
- **Tier2 (주간):** `DE` `FR` `IT` `AE` `SA` `BR` `MX` `IN` `PH` `ZA`

권역 그룹: KR · APAC · SEA · NA · EU · ME(중동) · LATAM · AF · IN

---

## 대시보드 구성

| 섹션 | 내용 |
|------|------|
| Brand Radar | 최근 4주 vs 직전 4주 기사량 모멘텀 순위 |
| 브랜드 × 국가 히트맵 | 어느 브랜드가 어느 시장에서 활발한지 매트릭스 |
| 글로벌 신호 지도 | 국가별 HIGH/MED/LOW 세계지도 시각화 |
| 🥊 **우리 카테고리 vs 경쟁 활동** | Cafe24 자사 카테고리(선케어·크림·앰플/세럼 등)별 **경쟁 압박 강도** + 최고 위협 무브먼트 |
| HIGH/MED 기사 목록 | 중요도·브랜드·활동유형 필터, 채널·근거·스코어 배지 |
| 전략 인사이트 카드 | 브랜드별 AI 요약 (캐시 적용) |
| 시장 종합 인사이트 | 전 경쟁사 종합 → 셀퓨전씨 관점 전략 제언 |
| 기간 토글 | 30 / 60 / 90일 전환 |

기사 제목·본문은 전량 한국어 번역 + 원문 링크 제공. **중복 병합된 기사는 대표 1건만 노출.**

---

## 자동 브리핑 (Slack)

| 브리핑 | 발송 | 모델 | 내용 |
|--------|------|------|------|
| **일간** | 매일 08:00 | gpt-4o-mini | 전날 수집분 핵심 3~5건 + 셀퓨전씨 관련 대응 포인트 (간결) |
| **주간** | 매주 월 08:00 | gpt-4o | 지난주 심층: Executive Takeaway / 권역별 핵심 움직임 / 주요 무브먼트 상세(Top 5~7) / Watchlist / 셀퓨전씨 실행 액션 |

---

## 갱신 주기 (KST)

| 시각 | 작업 |
|------|------|
| 매일 08:00 | 일간 브리핑 발송 |
| 매일 18:00 | Tier1 브랜드 × Tier1 국가 일별 수집 |
| 매일 23:00 | 의미 임베딩 중복 병합 |
| 매주 월 08:00 | 심층 주간 브리핑 발송 |
| 매주 월 17:00 | Cafe24 자사 제품 프로필 동기화 |
| 매주 월 19:00 | 브랜드 모멘텀 재계산 + 티어 자동 조정 |
| 매주 월 20:00 | 전체 브랜드 × 전체 국가 풀스캔 (현지어 심층) |
| 매주 일 19:00 | 제목 유사도 중복 후보 기록 |

> 운영: 로컬 APScheduler(Windows 작업 `CMSLab_RivalScheduler`, 10분마다 self-heal, KST). Render는 유료플랜 상시가동이라 keep-alive 핑 불필요.

---

## 기술 스택

| 항목 | 내용 |
|------|------|
| 서버 | FastAPI + uvicorn (Render 유료플랜) |
| 스케줄러 | APScheduler (로컬 상시 실행) |
| DB | Supabase PostgreSQL (`rival_intel` 스키마) |
| ORM | SQLAlchemy + psycopg2 |
| AI 분류·번역·일간브리핑 | OpenAI gpt-4o-mini (배치 8건/콜) |
| 주간 브리핑 | OpenAI gpt-4o |
| 의미 중복 dedup | OpenAI text-embedding-3-small |
| 자사 제품 연동 | Cafe24 카탈로그 API (프록시) |
| 대시보드 | 서버사이드 HTML 생성 (Chart.js + Canvas) |

---

## 개발자 설치

```bash
cd CellFusionC_intel
pip install -r requirements.txt
cp .env.example .env   # 아래 값 입력
```

**필수 환경변수**
```
OPENAI_API_KEY=...
DB_HOST=...  DB_USER=...  DB_PASSWORD=...  DB_NAME=postgres
SLACK_WEBHOOK_URL=...        # 선택 (브리핑·알림)
NAVER_CLIENT_ID=...          # 선택 (KR 네이버 수집)
NAVER_CLIENT_SECRET=...
```

> `.env`는 **절대 git 커밋 금지** — 실서버 크리덴셜 포함 (gitignore됨).

**DB 초기화 (최초 1회)**
```bash
python -c "from storage.models import create_tables, migrate_tables; create_tables(); migrate_tables()"
```

**로컬 실행**
```bash
uvicorn server:app --reload --port 8000   # 대시보드
python cli.py run                          # 스케줄러 단독 실행
```
