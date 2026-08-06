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
| `RENDER_EXTERNAL_URL` | (Render) | |

> 키 없는 신호 모듈은 **자동 스킵**(로그만 남김) — 시스템이 죽지 않는다.

---

## 4. 스케줄 (KST, 로컬 스케줄러)

| 시각 | 잡 id | 작업 |
|---|---|---|
| 매일 09:00, 18:00 | `daily_tier1` | Tier1 브랜드×Tier1 국가 수집 |
| 매일 23:00 | `semantic_dedup` | 의미 임베딩 중복 병합 |
| 매일 08:00 | `daily_briefing` | 일간 브리핑 발송 |
| 월·목 07:00 | `search_trends` | 네이버 검색트렌드(국내 수요) |
| 월·수·금 07:20 | `google_trends` | 구글 트렌드(글로벌) + 검색급등 알림 |
| 매월 3일 06:30 | `export_stats` | 관세청 수출통계 |
| 매월 4일 06:40 | `dart_financials` | DART 재무 |
| 매월 4일 06:50 | `trademark` | KIPRIS 해외상표 |
| 매주 월 17:00 | `profile_sync` | Cafe24 자사 제품 프로필 |
| 매주 월 19:00 | `weekly_momentum` | 모멘텀 재계산 + 티어 자동조정 |
| 매주 월 20:00 | `weekly_full` | 전체 브랜드×국가 풀스캔 |
| 매주 월 08:00 | `weekly_briefing` | 심층 주간 브리핑 |
| 매주 일 19:00 | `weekly_dedup` | 제목 유사도 중복 후보 기록 |

> 신호 수집 주기는 **원본 갱신 주기에 맞춤**(관세청·KIPRIS 월1회, 검색 상시). 정의: `scheduler/runner.py::create_scheduler`.

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
| `competitor_financials` | DART 재무 |
| `trademark_filings` | KIPRIS 해외상표 |

신호 테이블은 각 모듈의 `_ensure_table`이 **없으면 자동 생성**(비파괴 `CREATE TABLE IF NOT EXISTS`).

---

## 6. 자주 하는 운영 작업

### 브랜드 추가
1. `monitored_brands` DB에 INSERT (`name`, `tier`, `is_active=TRUE`). → 뉴스·네이버·구글·수출은 **자동 반영**.
2. (선택) 정밀 매핑이 필요한 곳만 수동 추가:
   - `config/brands.py` `BRAND_KO_NAMES` — 한국어명(네이버 검색 정확도↑)
   - `signals/dart_financials.py` `BRAND_CORP` — 운영사명(재무 매칭)
   - `signals/trademark.py` `SEARCH_TERMS`·`OWN_APPLICANTS` — 상표 검색어·출원인
   > 매핑 없으면 그 브랜드는 DART·상표에서 "미매칭"으로 조용히 스킵(에러 아님).
3. 스케줄러 재시작.

### 국가 추가
- `config/brands.py` `COUNTRIES`(+`TIER1/TIER2_COUNTRIES`)에 추가 → 뉴스·수출 자동. (구글 GEOS·상표는 API 한정이라 별도.)

### 키 갱신 (특히 KIPRIS는 연 단위 만료)
- `.env`의 해당 키 교체 → 스케줄러 재시작. 만료돼도 해당 신호만 스킵되고 나머지는 정상.

### 신호 수동 1회 실행 (테스트)
```bash
cd CellFusionC_intel
python -m signals.export_stats      # / naver_trends / google_trends / dart_financials / trademark
```

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
