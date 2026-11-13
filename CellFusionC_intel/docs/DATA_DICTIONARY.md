# rival_intel 데이터 사전

K-뷰티 경쟁사 모니터링 데이터베이스. 외부 연동·통합 적재를 맡은 분을 위한 안내서입니다.

- 기준일: **2026-11-13**
- 스키마: `rival_intel` (PostgreSQL / Supabase)
- 규모: 테이블 **35개 · 약 179 MB · 최대 테이블 17,448행**
- 모든 시간 컬럼은 `timestamptz`(UTC 저장)입니다. 운영 기준 시각은 **KST(UTC+9)** 입니다.

---

## 1. 읽기 전에 반드시 알아야 할 다섯 가지

이 다섯 개를 모르고 적재하면 수치가 틀립니다.

### ① `news_articles`를 그대로 세면 과대계상됩니다

기사 1건이 여러 매체에 중복 게재됩니다. 두 개의 컬럼이 이를 표시합니다.

| 컬럼 | 뜻 | 집계 시 |
|---|---|---|
| `is_duplicate` | 중복 여부 | — |
| `dup_of` | 대표 기사의 `id` | **`dup_of IS NOT NULL`인 4,032행은 제외** |

제외하지 않으면 전체 17,448행 중 **23%가 중복 계상**됩니다.

### ② `brand_focus`로 걸러야 합니다

수집은 넓게 하고 판단은 나중에 하는 구조라, 테이블에는 **관련 없는 기사도 그대로 들어 있습니다.**

| `brand_focus` | 행수 | 뜻 | 분석 사용 |
|---|---|---|---|
| `primary` | 8,980 | 그 브랜드가 기사의 주제 | 사용 |
| `secondary` | 3,674 | 비중 있게 등장 | 사용 |
| `incidental` | 2,764 | 스쳐 지나감 | 제외 |
| `unrelated` | 1,907 | 무관 (동명이인·증시 스침 등) | 제외 |
| `NULL` | 123 | 미분류(구버전) | 제외 |

**사내 분석 쿼리는 모두 `brand_focus IN ('primary','secondary')` 로 필터합니다.** 통합 DB도 같은 기준을 쓰셔야 숫자가 맞습니다.

### ③ 원천 데이터와 LLM 생성물이 같은 DB에 있습니다

`brand_insights`, `brand_country_insights`, `briefings`, `respond_feed` 는 **LLM이 생성한 해석문**입니다. 사실 데이터가 아닙니다. 아래 표의 "구분" 열에서 `파생(LLM)` 으로 표시했습니다. 통합 DB에 사실 테이블로 올리지 마십시오.

### ④ 다시 수집할 수 없는 데이터가 있습니다

`news_articles` 는 Google News RSS에서 모읍니다. **RSS는 최근 30일 창만 제공하므로 과거 기사는 재수집이 불가능합니다.** 삭제 시 복구는 Supabase 일일 백업(7일 보관)에만 의존합니다.

### ⑤ 지금 멈춰 있는 소스가 있습니다

7절 "알려진 문제"를 먼저 보십시오. 일부 테이블은 고장으로 갱신이 멈춘 상태이고, 일부는 원래 월 단위라 오래된 것이 정상입니다. **둘을 구분해야 합니다.**

---

## 2. 테이블 분류

| 구분 | 뜻 | 통합 DB 적재 |
|---|---|---|
| **원천** | 외부에서 수집한 사실 | 적재 대상 |
| **파생(계산)** | 원천에서 코드가 계산 | 재계산 가능 — 선택 |
| **파생(LLM)** | LLM이 생성한 문장 | 사실로 쓰지 말 것 |
| **운영메타** | 파이프라인 자체 기록 | 보통 불필요 |
| **설정** | 사람이 관리하는 기준표 | 조인용으로 필요 |

---

## 3. 전체 테이블

갱신 주기는 KST 기준입니다. "최종"은 2026-11-13 기준 마지막 데이터 날짜입니다.

### 원천 — 뉴스·언론

| 테이블 | 행수 | 구분 | 시간 컬럼 | 갱신 | 최종 | 비고 |
|---|---|---|---|---|---|---|
| `news_articles` | 17,448 | 원천 | `collected_at` / `published_date` | 매일 09·18시 + 주간 풀스캔 | 11-13 | **핵심 테이블.** 1·2절 필터 필수 |
| `collection_runs` | 65,053 | 운영메타 | `run_at` | 수집마다 | 11-13 | 브랜드×국가별 수집 이력 |
| `dedup_candidates` | 277 | 파생(계산) | `created_at` | 주간 | 11-08 | 중복 판정 후보 |

### 원천 — 유통·채널

| 테이블 | 행수 | 구분 | 시간 컬럼 | 갱신 | 최종 | 비고 |
|---|---|---|---|---|---|---|
| `retail_rankings` | 16,400 | 원천 | `capture_date` | 매일 06:21 | 11-13 | 해외 리테일러 랭킹 |
| `oliveyoung_rankings` | 11,431 | 원천 | `capture_date` | 매일 06:30 | 11-13 | 11-07~12 **6일 공백** 있음 (7절) |
| `oliveyoung_reviews` | 159 | 원천 | `captured_at` | 주 1회 | 11-13 | 같은 공백 |
| `self_channel_products` | 96 | 원천 | `captured_at` | 매일 06:40 | 11-13 | 자사 올영 성과. 같은 공백 |
| `self_channel_meta` | 1 | 원천 | `captured_at` | 매일 | 11-13 | 같은 공백 |

### 원천 — 수요·관심도

| 테이블 | 행수 | 구분 | 시간 컬럼 | 갱신 | 최종 | 비고 |
|---|---|---|---|---|---|---|
| `google_trends` | 22,157 | 원천 | `period` | 주 1회 | 11-12 | `ratio` 는 상대값(0~100) |
| `search_trends` | 1,709 | 원천 | `period` | 주 1회 | 11-09 | 네이버 검색 트렌드 |
| `social_metrics` | 13,048 | 원천 | `captured_date` | 매일 | 11-13 | 플랫폼×브랜드 지표 |
| `youtube_lang` | 2,383 | 원천 | `captured_date` | 매일 11:00 | 11-13 | 유튜브 언어별 버즈 |

### 원천 — 재무·공시·지식재산

| 테이블 | 행수 | 구분 | 시간 컬럼 | 갱신 | 최종 | 비고 |
|---|---|---|---|---|---|---|
| `nice_financials` | 46,719 | 원천 | `loaded_at` | **연 1회** | 08-20 | NICE 산업·기업 재무. 오래된 것이 정상 |
| `nice_company_brands` | 7,222 | 설정 | (없음) | 수동 | — | 기업↔브랜드 매핑 |
| `competitor_financials` | 174 | 원천 | `fetched_at` | **월 2회**(4·19일) | 11-03 | DART 공시 |
| `consensus_financials` | 104 | 원천 | `fetched_at` | 주간 | 11-11 | 증권사 컨센서스 |
| `export_stats` | 3,310 | 원천 | `period` | **월 1회**(3일) | 09-01 | 수출 통계. 발표 자체가 2개월 지연 |
| `trademark_filings` | 1,222 | 원천 | `app_date` | **월 1회**(4일) | 11-04 | 국내·해외 상표 출원 |
| `trademark_goods` | 13,496 | 원천 | `collected_at` | 일간 백필 | 11-11 | ⚠️ 백필 정체 (7절) |
| `brand_applicants` | 42 | 설정 | `updated_at` | 수시 | 11-11 | 브랜드↔출원인 매핑 |

### 파생 — 계산 결과

| 테이블 | 행수 | 구분 | 시간 컬럼 | 갱신 | 최종 | 비고 |
|---|---|---|---|---|---|---|
| `brand_score_history` | 522 | 파생(계산) | `capture_date` | 주간(월 07:40) | 11-09 | 종합 점수 스냅샷 |
| `watch_items` | 99 | 파생(계산) | `updated_at` | 매일 08:30 | 11-12 | 주시 항목 스코어보드 |
| `product_ingredients` | 56 | 파생(계산) | `collected_at` | 수시 | 11-02 | 성분 분석 |
| `device_watch_log` | 1 | 파생(계산) | `created_at` | 수시 | 11-09 | 디바이스 세그먼트 로그 |

### 파생 — LLM 생성물 (⚠️ 사실 데이터 아님)

| 테이블 | 행수 | 구분 | 시간 컬럼 | 최종 | 비고 |
|---|---|---|---|---|---|
| `brand_insights` | 5,816 | 파생(LLM) | `generated_at` | 11-12 | 브랜드×기간 요약문 캐시 |
| `brand_country_insights` | 69 | 파생(LLM) | `generated_at` | **08-03** | 사실상 미사용 |
| `briefings` | 161 | 파생(LLM) | `created_at` | 11-13 | 일간·주간 브리핑 본문 |
| `respond_feed` | 14 | 파생(LLM) | `created_at` | 11-13 | 대응 제안 |

### 설정 · 운영메타

| 테이블 | 행수 | 구분 | 비고 |
|---|---|---|---|
| `monitored_brands` | 59 | **설정** | 모니터링 대상 기준표. 조인 기준 |
| `value_mappings` | 11 | 설정 | 국가·권역 등 용어 단일 출처 |
| `brand_candidates` | 43 | 운영메타 | 신흥 브랜드 후보(승인 대기) |
| `job_runs` | 604 | 운영메타 | 스케줄러 실행 이력 |
| `high_alert_log` | 461 | 운영메타 | 알림 발송 이력 |
| `bot_conversations` | 22 | 운영메타 | Slack 봇 대화 |
| `bot_user_memory` | 3 | 운영메타 | Slack 봇 장기기억 |

---

## 4. 핵심 테이블 상세

### `news_articles` — 기사 원본 (17,448행 / 119 MB)

기본키 `id`. 중복 방지 키는 `url_hash` (중복 0건 확인).

**식별·분류**

| 컬럼 | 설명 |
|---|---|
| `brand` | 모니터링 브랜드명. `monitored_brands.name` 과 조인 |
| `country` | 수집 대상 국가 코드 |
| `source_country` | 매체 소재 국가 (7.2% NULL) |
| `activity_type` | 활동 유형 (진출·제품출시·실적 등) |
| `brand_focus` | **필수 필터.** 1·2절 참조 |
| `importance` / `evidence_level` | 중요도 / 근거 강도 |
| `is_self` | 자사(CellFusionC) 기사 여부 |

**본문**

| 컬럼 | NULL 비율 | 비고 |
|---|---|---|
| `title` | 0% | 원문 제목 |
| `title_ko` | 1.8% | 한국어 번역 |
| `article_body` | 11.8% | 원문 본문 |
| `article_body_ko` | 31.0% | 번역 본문 |

**부분적으로만 채워지는 컬럼** — 통합 DB에서 필수 필드로 두지 마십시오.

| 컬럼 | NULL 비율 |
|---|---|
| `note` | 99.8% |
| `price_info` | 93.0% |
| `city` | 91.1% |
| `key_ingredients` | 89.8% |
| `embedding` | 80.6% |
| `product_name` | 76.8% |
| `channel` | 72.2% |
| `sentiment` | 32.4% |

**옮기지 않기를 권합니다**

- `embedding` — 중복 판정용 벡터. 분석 가치 없음, 용량만 큼
- `strategic_score` — **폐기 예정.** 7.7%만 채워져 있고 산식이 신뢰되지 않아 사내에서도 사용 중단

### `monitored_brands` — 브랜드 기준표 (59행)

`name`(PK), `tier`(1=일별 수집, 2=주간), `ko_names`(**Postgres 배열 `text[]`** — 일부 ETL 도구가 처리 못 하므로 평탄화 필요), `is_active`, `momentum_score`, `segment`.

이 DB에서 **배열 타입은 이 컬럼 하나뿐**입니다.

### `retail_rankings` — 해외 리테일 랭킹 (16,400행)

`retailer`, `country`, `category`, `brand`, `product_name`, `rank`, `rating`, `review_count`, `capture_date`, `is_monitored`, `is_core`.

`is_monitored = false` 는 모니터링 대상 외 브랜드로, 순위 맥락 확보용입니다. 집계 시 주의하십시오.

### `oliveyoung_rankings` — 국내 올리브영 랭킹 (11,271행)

`category`, `rank_position`, `prev_rank`, `delta`, `goods_no`, `goods_name`, `brand`, `capture_date`, `is_ours`.

이 데이터는 **외부 MCP 서비스를 경유**해 수집합니다. 현재 중단 상태입니다(7절).

### `nice_financials` — 기업 재무 (46,719행)

`industry_code`, `industry_name`, `company`, `metric`, `year`, `amount`, `rank`, `is_cosmetic`.

**long 포맷**(metric별 1행)입니다. wide로 피벗하려면 `metric` 을 전개하십시오. 연 1회 갱신이라 `loaded_at` 이 오래된 것은 정상입니다.

---

## 5. 증분 적재 기준

전체를 매번 읽지 마십시오. Supabase 트래픽 요금은 **조직 단위로 합산**되며, `news_articles` 하나가 119 MB입니다.

| 테이블 | 증분 기준 컬럼 |
|---|---|
| `news_articles` | `collected_at` |
| `retail_rankings` / `oliveyoung_rankings` | `capture_date` |
| `social_metrics` / `youtube_lang` | `captured_date` |
| `google_trends` / `search_trends` / `export_stats` | `period` |
| `competitor_financials` / `consensus_financials` | `fetched_at` |
| `trademark_goods` | `collected_at` |
| `brand_insights` | `generated_at` |
| `nice_financials` | `loaded_at` |

**초기 적재 1회는 덤프 파일로 받아 가시기를 권합니다.**

주의: `news_articles` 는 분류 결과가 나중에 갱신될 수 있어, `collected_at` 기준 증분만으로는 **이미 적재한 행의 `brand_focus` 변경을 놓칩니다.** 주 1회 최근 30일 구간을 재적재하시면 안전합니다.

---

## 6. 접속

| 항목 | 값 |
|---|---|
| 호스트 | Supabase 커넥션 풀러 주소 (별도 전달) |
| 포트 | **5432** (세션 모드) |
| 데이터베이스 | `postgres` |
| 스키마 | `rival_intel` |
| 사용자 | `<계정명>.<프로젝트ref>` — **풀러는 계정명 뒤에 프로젝트 ref가 붙어야 인증됩니다** |
| SSL | `sslmode=require` |

🔴 **6543 포트는 사용하지 마십시오.** 트랜잭션 모드라 prepared statement가 깨져 ETL 도구가 간헐적으로 실패합니다.

---

## 7. 알려진 문제 (2026-11-13 기준)

### 올리브영 데이터 — 11-07~11-12 공백 (복구 완료)

대상: `oliveyoung_rankings`, `oliveyoung_reviews`, `self_channel_products`, `self_channel_meta`

외부 MCP 서비스가 2026-11-07부터 인증을 요구하기 시작해 HTTP 401로 막혔고, 클라이언트 쪽에 해당 토큰 설정이 없어 **6일간 수집이 중단**됐습니다. **2026-11-13에 복구**되어 현재는 정상 수집 중입니다.

⚠️ **다만 11-07 ~ 11-12 구간 데이터는 존재하지 않습니다.** 지나간 날짜의 랭킹은 소급 수집이 불가능합니다. 시계열 분석 시 이 구간을 결측으로 처리하십시오. 0으로 채우면 순위 급락으로 오독됩니다.

당시 스케줄러 작업은 매일 실행되어 "성공"으로 기록됐습니다. 작업 이력만으로는 알 수 없었던 유형입니다.

### ⚠️ 국내 상표 백필 정체

대상: `trademark_goods`, `trademark_filings`

58개 브랜드 중 51개가 미처리 상태이며, 외부 API 호출 한도가 매일 첫 호출에서 소진되어 **2일 연속 0건 전진**했습니다. 국내 상표 데이터는 **불완전한 상태**로 간주하십시오.

### 브랜드 표기 (정리 완료)

`TIRTIR` / `Tirtir` 대소문자 불일치 1행은 2026-11-13에 `Tirtir` 로 통일했습니다. **현재 대소문자만 다른 중복 표기는 없습니다.**

`news_articles.brand` 에는 `monitored_brands` 에 없는 값이 두 종 남아 있습니다. 둘 다 의도된 것이거나 영향이 없습니다.

| 값 | 행수 | 설명 |
|---|---|---|
| `CellFusionC` | 35 | **자사.** `is_self = true` 로 구분됩니다. 경쟁사 집계에서 제외하십시오 |
| `올리브영` | 1 | 브랜드가 아닌 유통사명이 들어간 1행. `brand_focus = unrelated` 라 분석에서 이미 제외됩니다 |

`monitored_brands` 와 조인하실 때는 **내부 조인이 이 두 값을 떨어뜨린다는 점**만 인지하시면 됩니다.

### 정상이지만 오래되어 보이는 것

아래는 고장이 아닙니다. 원래 주기가 깁니다.

| 테이블 | 최종 | 주기 |
|---|---|---|
| `nice_financials` | 08-20 | 연 1회 |
| `export_stats` | 09-01 | 월 1회 + 통계 발표 자체가 2개월 지연 |
| `competitor_financials` | 11-03 | 월 2회 |
| `trademark_filings` | 11-04 | 월 1회 |
| `brand_country_insights` | 08-03 | 기능 미사용 |

---

## 8. 스키마 상태 점검 결과

| 항목 | 결과 |
|---|---|
| 기본키 없는 테이블 | **없음** |
| 시간 컬럼 타입 | **42개 컬럼 전부 `timestamptz` 로 통일** |
| 배열 컬럼 | `monitored_brands.ko_names` 1개 |
| JSON 컬럼 | 없음 |
| `news_articles` URL 중복 | **0건** |
