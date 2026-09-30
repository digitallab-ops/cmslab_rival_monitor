# 인수인계 문서 (HANDOVER) — K-Beauty 경쟁사 인텔리전스

> 이 시스템을 **처음 넘겨받은 사람**이 운영·유지보수·확장할 수 있도록 정리한 실무 문서.
> 시스템 개요·설계 배경은 [README.md](README.md) 참고. 여기서는 **"어떻게 돌리고 고치는가"** 만 다룬다.

---

## 0. 30초 요약

- **하는 일:** 경쟁 K-뷰티 21개 브랜드 × 23개국 동향을 자동 수집·AI분류하고, 외부 신호(검색·수출·재무·상표) 5축으로 교차검증해 대시보드·Slack으로 전달.
- **수집·분석 = 로컬 PC의 스케줄러**(Windows 작업)에서만 돈다.
- **Render = 대시보드·봇 조회 전용**(DB만 읽음). ← **이 분리를 절대 깨지 말 것.**
- **DB = Supabase PostgreSQL** (스키마 `rival_intel`). 로컬과 Render가 같은 DB를 공유.

---

## 1. 아키텍처 — 어디서 뭐가 도는가 (가장 중요)

```
로컬 PC (수집·분석 엔진)                     Render (조회 전용, 상시가동)
├─ APScheduler (Windows 작업                 ├─ FastAPI 대시보드 (HTML)
│   "CMSLab_RivalScheduler")                 ├─ /mcp (MCP 서버)
├─ 뉴스 수집·AI분류·중복병합                 └─ Slack Q&A 봇 (web 프로세스 내 in-process)
├─ 신호 수집(검색·수출·재무·상표)                        │
├─ 브리핑 생성·발송                                       │ 둘 다 같은 DB를 읽고/쓴다
└─ 모멘텀·티어 자동조정                                    ▼
                    └──────────▶ Supabase PostgreSQL (rival_intel) ◀──────────┘
```

### ⚠️ 반드시 지킬 원칙
1. **`server.py`(Render)에 스케줄러/수집 코드를 절대 넣지 말 것.** Render는 조회 전용. 과거 무료티어 시절 흔적이 있으나 복원 금지. 수집은 로컬 단독.
2. **`.env`는 절대 git 커밋 금지** (실 크리덴셜 포함, gitignore됨). 새 키는 `.env`에만.
3. **git push는 두 리모트에 모두**: `handover`(운영·digitallab-ops) + `origin`(포폴·lsmlub99).

---

## 2. 실행 & 배포

### 로컬 스케줄러 (수집 엔진)
- Windows 작업 스케줄러의 **`CMSLab_RivalScheduler`** 작업이 `pythonw.exe cli.py run`을 **창 없이 백그라운드**로 실행. 10분마다 self-heal(죽으면 재시작).
- **수동 실행:** `start_scheduler.bat` 또는 `python cli.py run`
- **로그:** `CellFusionC_intel/logs/scheduler.log`

### 스케줄러 재시작 (코드 변경 반영 시 필수)
PowerShell:
```powershell
Stop-ScheduledTask -TaskName 'CMSLab_RivalScheduler'
# 혹시 남은 pythonw 프로세스가 있으면 종료 후
Start-ScheduledTask -TaskName 'CMSLab_RivalScheduler'
# 확인
Get-Content CellFusionC_intel\logs\scheduler.log -Tail 20
```
> 스케줄러는 잡 코드를 **프로세스 시작 시 로드**하므로, `signals/`·`scheduler/` 등을 고치면 **재시작해야 반영**된다.

### Render (대시보드·봇)
- `main` 브랜치 push 시 **자동 재배포**. 유료 플랜(상시가동, 유휴 슬립 없음).
- 대시보드는 서버 메모리에 캐시됨 → 갱신: `POST /api/refresh` 호출 또는 재배포.

---

## 3. 환경변수 (`.env`)

| 변수 | 용도 | 발급/비고 |
|---|---|---|
| `OPENAI_API_KEY` | AI 분류·번역·브리핑·임베딩 | 유료 |
| `DB_HOST`/`PORT`/`USER`/`PASSWORD`/`NAME` | Supabase PostgreSQL | |
| `SLACK_WEBHOOK_URL` | 브리핑·수집요약·티어변경 알림 | |
| `SLACK_WEBHOOK_URL_2` | 주간/일간/HIGH/검색급등 알림(별도 채널) | |
| `SLACK_BOT_TOKEN`/`APP_TOKEN` | Slack Q&A 봇(Socket Mode) | `docs/SLACK_BOT_SETUP.md` |
| `SLACK_BOT_MODEL` | 봇 모델 (기본 gpt-4o-mini) | |
| `MCP_SERVER_URL`/`MCP_API_KEY` | 봇↔MCP 인증 | |
| `NAVER_CLIENT_ID`/`SECRET` | 네이버 **뉴스 검색** 수집 | developers.naver.com |
| `NAVER_HUB_KEY_ID`/`NAVER_HUB_KEY` | 네이버 **데이터랩 검색트렌드** | NAVER API HUB (뉴스검색 키와 별개) |
| `DATA_GO_KR_KEY` | 관세청 수출통계 | data.go.kr (무료, 디코딩키) |
| `OPENDART_KEY` | DART 재무 | opendart.fss.or.kr (무료) |
| `KIPRIS_KEY` | KIPRIS 해외상표 accessKey | plus.kipris.or.kr (무료, 월1000콜, **연 단위 갱신**) |
| `EUIPO_KEY`/`EUIPO_SECRET` | 유럽 상표(EUIPO) | euipo.europa.eu |
| `YOUTUBE_API_KEY` | 유튜브 버즈·언어권 분석 | 무료 1일 10,000유닛 (search.list=100, videos.list=1) |
| `ADMIN_KEY` | 🔒 관리자 게이팅 — 기간 조회·CSV 내려받기·재생성 | 임의 문자열 |
| `SLACK_MENTION_IDS` | 알림에 붙일 멘션 대상 | 채널 음소거 시에도 알림이 뜨게 |
| `RENDER_EXTERNAL_URL` | (Render) | |

> 키 없는 신호 모듈은 **자동 스킵**(로그만 남김) — 시스템이 죽지 않는다.
>
> **키가 없는 수집도 있다** — 네이버 증권 컨센서스(`naver_consensus`)와 올리브영은
> 공식 오픈API가 아니라 화면이 쓰는 내부 엔드포인트다. 예고 없이 바뀔 수 있으므로
> 실패해도 기존 재무·랭킹은 건드리지 않게 분리해 뒀다.

---

## 4. 스케줄 (KST, 로컬 스케줄러) — 잡 24종

**수집**
| 시각 | 작업 |
|---|---|
| 매일 05:30 / 06:40 | 자사(셀퓨전씨) 수집 · 자사 올영 성과 |
| 매일 06:20 / 06:30 | 아마존 리테일 9개국 · 올리브영 국내 랭킹 |
| 매일 09:00, 18:00 | Tier1 브랜드×Tier1 국가 뉴스 수집 |
| 매일 11:00 | 유튜브 버즈 보정(전 브랜드 커버) |
| 매주 월 20:00 | 전체 브랜드×국가 풀스캔 |
| 월·목 07:00 | 네이버 검색 트렌드 |
| 월·수·금 07:20 | 구글 트렌드 + 검색급등 알림 |
| 매월 3일 06:30 | 관세청 수출통계 |
| 매월 4·19일 06:40 | DART 재무(분기보고서 법정기한 분기말+45일 대응) |
| 매월 4일 06:50 | KIPRIS 해외상표 |
| 매주 목 07:10 | 네이버 증권 컨센서스(추정 실적) |

**분석·정리**
| 시각 | 작업 |
|---|---|
| 매일 08:30 | **적중표 추출·채점** (아침 브리핑 직후) |
| 매일 09:10 | 신흥 브랜드 자동발견 |
| 매일 23:00 | 의미 임베딩 중복 병합 |
| 매주 화 06:00 | 브랜드 모멘텀 + 티어 자동조정 |
| 매주 화 07:20 | 제품 전성분 인텔 |
| 매주 월 07:40 / 17:00 | 브랜드 스코어 스냅샷 · 자사 제품 프로필 동기화 |
| 매주 일 19:00 | 제목 유사도 중복 후보 기록 |

**발신**
| 시각 | 작업 |
|---|---|
| 화~금 08:00 | 일간 브리핑 Slack |
| 매주 월 08:00 | 심층 주간 브리핑 Slack |
| 평일 13:00 | 신규 HIGH 다이제스트(있을 때만) |

> 신호 수집 주기는 **원본 갱신 주기에 맞춘다**(관세청·KIPRIS 월1회, 검색 상시).
> 정의: `scheduler/runner.py::create_scheduler`.
> **잡 실행 이력은 `job_runs`에 남는다** — 월간 잡이 조용히 걸러도 파수꾼이 잡아낸다.

---

## 5. DB 스키마 (`rival_intel`) 주요 테이블

| 테이블 | 내용 |
|---|---|
| `news_articles` | 수집·분류된 기사(핵심) |
| `monitored_brands` | 모니터 브랜드 + tier + momentum (**브랜드 추가는 여기**) |
| `collection_runs` | 수집 실행 로그 |
| `briefings` | 생성된 브리핑 |
| `high_alert_log` | HIGH 속보 발송 이력(중복 억제용) |
| `search_trends` | 네이버 검색지수 |
| `google_trends` | 구글 검색지수(GLOBAL/US/JP) |
| `export_stats` | 관세청 수출액 |
| `competitor_financials` | DART 재무 — **분기 누적**(1Q/반기/3Q/연간). 단독분기가 아니다 |
| `nice_financials` · `nice_company_brands` | NICE BizLine 재무(46,719행·7,222개사, 비상장 포함) + 브랜드 매칭 |
| `consensus_financials` | 네이버 증권 확정·추정 실적. `is_estimate`로 구분 |
| `trademark_filings` | KIPRIS·EUIPO 해외상표. `is_own`=자사 출원 |
| `retail_rankings` | 아마존 9개국 제품 랭킹(일별) |
| `oliveyoung_rankings` · `oliveyoung_reviews` | 올리브영 카테고리별 Top20 + 리뷰 감성 |
| `social_metrics` · `youtube_lang` | 유튜브 버즈(브랜드 합계) + **언어권별** 조회수·대표영상 |
| `watch_items` | **적중표** — 브리핑이 짚은 것과 채점 결과 |
| `brand_candidates` | 신흥 브랜드 자동발견 후보(슬랙 승인 대기) |
| `job_runs` | 스케줄러 잡 실행 이력(파수꾼이 미실행 감지에 씀) |
| `bot_conversations` · `bot_user_memory` | Slack 봇 대화·개인화 기억 |

신호 테이블은 각 모듈의 `_ensure_table`이 **없으면 자동 생성**(비파괴 `CREATE TABLE IF NOT EXISTS`).

**단위 함정 — 여기서 자주 틀린다**
- `nice_financials.amount` = **천원** / `competitor_financials.revenue` = **원** /
  `consensus_financials.revenue` = **억원**. 화면 포맷터가 셋 다 다르다.
- DART 분기보고서는 **연초부터 누적**이다. 단독분기는 차분해야 한다
  (3Q = 3Q누적 − 반기). `analytics/queries.get_quarterly_series` 참고.

---

## 6. 자주 하는 운영 작업

### 브랜드 추가 ⚠️ 체크리스트 전부 확인

DB에만 넣으면 **기능마다 조용히 빠진다.** 승인은 DB로 들어가는데 코드 곳곳은
`config/brands.py`를 보기 때문이다. 실제 피해 사례: 메디큐브가 아마존 8개국
1위인데 '미등록'으로 찍혔고(1,914행), 재무 탭에서는 통째로 빠져 있었다.

| # | 할 일 | 안 하면 |
|---|---|---|
| 1 | `monitored_brands` INSERT (`name`·`tier`·`ko_names`·`is_active`) | 아무것도 안 됨 |
| 2 | `config/brands.py` `TIER1_BRANDS`/`TIER2_BRANDS` | 수집 주기에서 빠짐 (※ `_merge_db_ko_names()`가 DB 이름을 들여오므로 보통 자동) |
| 3 | `config/brands.py` `BRAND_KO_NAMES` | 네이버·장업신문이 **국내 기사를 못 긁음** |
| 4 | `signals/dart_financials.py` `BRAND_CORP` — **회사명**(브랜드명 아님) | DART 재무 미매칭. 제로이드에 '제로이드'를 적어둬 계속 실패했다(실제 회사는 네오팜) |
| 5 | `signals/trademark.py` `OWN_APPLICANTS` | 자사 상표를 남의 것으로 봐 **화면에서 브랜드가 통째로 사라짐**(피드가 `is_own`만 태움) |
| 6 | 상장사면 `signals/naver_consensus.py` `LISTED_PARENTS` | 추정 실적 안 나옴 |
| 7 | `python -c "from signals.nice_financials import recompute_matches; recompute_matches()"` | **재무 탭에 행 자체가 안 생김**(매칭이 엑셀 적재 때 한 번만 계산됨) |
| 8 | 과거 데이터 소급 — `retail_rankings.is_monitored`, `trademark_filings.is_own`, `oliveyoung_rankings.brand` | 수집 당시 값이라 등록해도 안 바뀜 |
| 9 | 스케줄러 재시작 | config 변경이 반영 안 됨 |

**회사명을 모르면 4·5·6은 비워 둔다.** 추측해서 넣으면 엉뚱한 회사에 붙는다
(`오브제`로 종목검색하면 `오브젠`이라는 다른 상장사가 나온다). 재무가 안 붙을 뿐
뉴스·순위 수집은 정상 동작한다.

### 국가 추가
- `config/brands.py` `COUNTRIES`(+`TIER1/TIER2_COUNTRIES`)에 추가 → 뉴스·수출 자동. (구글 GEOS·상표는 API 한정이라 별도.)

### 키 갱신 (특히 KIPRIS는 연 단위 만료)
- `.env`의 해당 키 교체 → 스케줄러 재시작. 만료돼도 해당 신호만 스킵되고 나머지는 정상.

### 신호 수동 1회 실행 (테스트)
```bash
cd CellFusionC_intel
python -m signals.export_stats       # 관세청 수출
python -m signals.naver_trends       # 네이버 검색
python -m signals.google_trends      # 구글 검색
python -m signals.dart_financials    # DART 재무(분기 포함)
python -m signals.naver_consensus    # 네이버 증권 추정 실적
python -m signals.trademark          # KIPRIS 상표
python -m signals.brand_discovery    # 신흥 브랜드 발견
python -m analytics.watch_scoreboard # 적중표 추출·채점
```

### 일회성 도구 (`tools/`)
```bash
python -m tools.backfill_bodies --months 3   # 과거 기사 본문 백필(구글 리디렉션 해제)
python -m tools.reclassify --months 3 --dry-run  # 본문 기준 재분류(비용 먼저 확인)
```
> 재분류는 `strategic_score`·`importance`가 바뀌어 **과거 통계가 소급 변동**한다.
> 지난 브리핑에서 말한 수치와 지금 화면이 달라질 수 있다는 뜻이다.

### HIGH 속보 문턱 조정
- `.env` `HIGH_ALERT_MIN_SCORE`(기본 85). 높이면 알림↓.

---

## 7. 트러블슈팅

| 증상 | 확인 | 해결 |
|---|---|---|
| 수집이 안 돔 | 작업 스케줄러 `CMSLab_RivalScheduler` 상태, `logs/scheduler.log` | 재시작(§2) |
| 대시보드 옛날 데이터 | Render 캐시 | `POST /api/refresh` 또는 재배포 |
| 특정 신호 비어있음 | 해당 `.env` 키, 로그의 "스킵/미매칭" | 키 확인 / 매핑 추가 |
| 구글 트렌드 429 실패 | `logs`의 429 | 정상(부분수집). UA는 이미 적용. sleep↑는 `google_trends._PAYLOAD_SLEEP` |
| DART 비상장 데이터 없음 | status 013 | **정상 한계** — 표준 API는 상장사만 |
| Slack 답변 2번 | 로컬에서 봇 중복 실행 | 로컬 봇 종료(운영 봇은 Render in-process) |
| 검색/구글 "브랜드 순위" 이상 | 배치 상대정규화 | 브랜드 간 비교 무효 — 급등/모멘텀만 유효(의도된 동작) |
| 새 브랜드가 화면에 안 보임 | 재무 탭·상표 섹션 | §6 브랜드 체크리스트 7·8번(NICE 재계산·과거 데이터 소급) 누락 |
| 배포했는데 화면이 그대로 | 생성 시각(우상단) vs 푸시 시각 | Render가 새 커밋을 아직 안 올렸을 수 있다. Render → Events에서 확인 후 Manual Deploy |
| 적중표가 전부 '대기' | `watch_items.due_on` | 정상 — 기한(보통 한 달) 전이다. 그 사이 잠정 경과만 매일 갱신된다 |
| 적중표 '판단불가'가 많음 | `metric='other'` | 확인 방법을 안 적은 옛 브리핑이거나 수집하지 않는 지표(틱톡샵 등). 맞힌 척하지 않는 게 의도 |
| 수출로 확인하는 항목이 안 끝남 | `export_stats` 최신 period | 관세청 확정분이 **두 달가량 늦다**. 정상 |

---

## 8. 확장 가이드

- **새 수집기:** `BaseCollector` 상속 → `collect(brand,country)` 구현 → `scheduler/pipeline.py`에 등록.
- **새 신호 모듈:** `signals/*.py`에 `run()` + `_ensure_table()` + 전용 테이블. `get_active_brand_names()`로 브랜드 조회(자동 확장). `analytics/queries.py`에 조회 함수, `scheduler/runner.py`에 잡, 대시보드/`mcp_server.py`에 노출. **키 없으면 스킵**하게 방어적으로.
- **테이블 마이그레이션:** 컬럼 추가는 `_ensure_table`에 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS`로 비파괴 처리(예: `trademark_filings.is_own`).

---

## 9. 연락·리소스
- 대시보드: https://cmslab-rival-monitor.onrender.com
- Slack 봇 설정: `CellFusionC_intel/docs/SLACK_BOT_SETUP.md`
- 리모트: `handover`(digitallab-ops, 운영) · `origin`(lsmlub99, 포폴)
