# Slack 경쟁 인텔 봇 — 설정 가이드

@멘션·DM으로 질문하면 gpt-4o가 MCP 툴로 데이터를 조회해 셀퓨전씨 관점으로 답하는 봇.

구조: **Slack 봇(Socket Mode)** → **gpt-4o 함수호출** → **MCP 서버(web `/mcp`)** → Supabase.

---

## 1단계 · Slack 앱 생성 (워크스페이스 관리자가 1회, 약 5분)

1. https://api.slack.com/apps → **Create New App** → **From scratch** → 이름(예: `셀퓨전씨 인텔`) + 워크스페이스 선택.
2. **Socket Mode** (좌측 메뉴) → **Enable Socket Mode** 켜기 → App-Level Token 생성:
   - Token Name 아무거나, Scope **`connections:write`** 추가 → 생성.
   - 나온 **`xapp-...`** 토큰 = `SLACK_APP_TOKEN`.
3. **OAuth & Permissions** → **Bot Token Scopes**에 아래 추가:
   - `app_mentions:read`, `chat:write`, `im:history`, `im:read`, `im:write`
4. **Event Subscriptions** → Enable → **Subscribe to bot events**에 추가:
   - `app_mention`, `message.im`
   - (Socket Mode면 Request URL 입력 불필요)
5. **App Home** (Features > App Home) → **Messages Tab ON** → "Allow users to send Slash commands and messages from the messages tab" 체크.
   - ⚠️ 이걸 켜야 봇에게 **DM**이 작동함.
6. **Install to Workspace** (OAuth & Permissions 상단) → 설치 승인.
   - 나온 **`xoxb-...`** 토큰 = `SLACK_BOT_TOKEN`.
7. 봇을 쓸 채널에 초대: 채널에서 `/invite @봇이름`. (DM은 초대 불필요)

> 발급된 `xapp-...`, `xoxb-...` 두 토큰을 아래 환경변수에 넣으면 끝. (git엔 절대 커밋 금지)

---

## 2단계 · Render 배포 (render.yaml Blueprint)

`render.yaml`에 이미 두 서비스가 정의돼 있음:
- **web (`kbeauty-intel`)** — 대시보드 + MCP 서버(`/mcp`)
- **worker (`kbeauty-slack-bot`)** — 봇 (Socket Mode, HTTP 미서빙)

Render 대시보드 → Blueprint 재적용(또는 수동으로 worker 서비스 추가) 후 **환경변수 설정**:

**web 서비스에 추가**
| 키 | 값 |
|---|---|
| `MCP_API_KEY` | 아무 긴 랜덤 문자열 (예: `openssl rand -hex 24`) |

**worker 서비스에 설정**
| 키 | 값 |
|---|---|
| `SLACK_BOT_TOKEN` | `xoxb-...` |
| `SLACK_APP_TOKEN` | `xapp-...` |
| `OPENAI_API_KEY` | 기존과 동일 |
| `MCP_SERVER_URL` | `https://<web서비스 도메인>/mcp` (예: `https://kbeauty-intel.onrender.com/mcp`) |
| `MCP_API_KEY` | **web에 넣은 값과 동일** |
| `SLACK_BOT_MODEL` | `gpt-4o` (기본) |

> `MCP_API_KEY`는 web·worker 양쪽 동일해야 함(봇이 그 키로 /mcp 인증). 미설정 시 /mcp 무인증 공개되므로 반드시 설정 권장.

---

## 3단계 · 확인

- worker 로그에 `Slack 봇 시작 (model=gpt-4o, mcp=...)` + `MCP 툴 7개 로드` 뜨면 정상.
- 채널에서 `@봇 아누아 최근 미국 동향` 또는 봇에게 DM으로 `베트남 시장 경쟁 상황`.

### 예시 질문
- `아누아 요즘 어때? 미국 위주로`
- `브라질 시장에 경쟁사들 어떻게 들어가고 있어?`
- `앰플 카테고리 경쟁 압박 어때?`
- `지금 급상승하는 브랜드 알려줘`
- `선크림 관련 최근 뉴스 찾아줘`

---

## 로컬에서 돌리려면 (선택)

`.env`에 `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN`, `OPENAI_API_KEY`,
`MCP_SERVER_URL`(로컬이면 `http://127.0.0.1:8000/mcp`), `MCP_API_KEY` 넣고:

```bash
# 터미널 1 — MCP 붙은 대시보드 서버
uvicorn server:app --port 8000
# 터미널 2 — 봇
python slack_bot.py
```

## MCP 툴 목록 (봇이 자동 선택 호출)
`list_brands` · `get_brand_intel(brand)` · `get_market_intel(country)` ·
`search_news(query)` · `get_category_battle_view` · `get_expansion_playbook_view(country?)` ·
`get_brand_momentum_view`
