"""
K-뷰티 경쟁사 인텔리전스 — FastAPI 서버

실행:
    uvicorn server:app --reload          # 개발
    uvicorn server:app --host 0.0.0.0 --port 8000  # 배포
"""

import asyncio
import hashlib
import hmac
import html as html_lib
import logging
import os
import sys
import threading
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

# .env를 명시적 경로로 로드 (CWD 무관)
from dotenv import load_dotenv
load_dotenv(os.path.join(_HERE, ".env"))

from contextlib import asynccontextmanager
from fastapi import FastAPI, Query, BackgroundTasks, Request
from fastapi.responses import HTMLResponse, JSONResponse

logger = logging.getLogger(__name__)

# 생성된 HTML 캐시 (메모리)
_dashboard_html: str = ""
_dashboard_built_at: float = 0.0        # 마지막 생성 시각(epoch)
_regenerating: bool = False             # 중복 재생성 방지
_regen_lock = threading.Lock()          # 체크-앤-셋 원자화(동시요청·refresh 레이스 방지)
_STALE_TTL = 3 * 3600                    # 이 시간 지나면 조회 시 자가 재생성(초)

_LOADING_PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><meta http-equiv="refresh" content="5">
<title>K-BEAUTY INTEL</title>
<style>
*{box-sizing:border-box;margin:0;padding:0;}
body{display:flex;align-items:center;justify-content:center;height:100vh;
font-family:system-ui,-apple-system,"Segoe UI","Malgun Gothic",sans-serif;
background:#08090f;color:#8891ab;}
.wrap{text-align:center;}
.brand{font-size:11px;font-weight:800;letter-spacing:0.22em;text-transform:uppercase;
color:#eceef5;margin-bottom:32px;}
.bar{width:3px;height:22px;background:linear-gradient(180deg,#c8a96e,transparent);
border-radius:1px;margin:0 auto 10px;}
.spinner{width:32px;height:32px;border:2px solid #1e2235;
border-top-color:#c8a96e;border-radius:50%;
animation:spin 0.9s linear infinite;margin:0 auto 18px;}
@keyframes spin{to{transform:rotate(360deg);}}
.msg{font-size:12px;color:#3e465c;letter-spacing:0.04em;}
.sub{font-size:10px;color:#2a2f42;margin-top:6px;}
</style></head>
<body><div class="wrap">
<div class="bar"></div>
<div class="brand">K-Beauty Intel</div>
<div class="spinner"></div>
<div class="msg">데이터 처리 중</div>
<div class="sub">5초마다 자동 새로고침</div>
</div></body></html>"""


def _build_dashboard() -> str:
    from dashboard.generate import generate_report
    path = generate_report("_server_cache.html")
    with open(path, encoding="utf-8") as f:
        return f.read()


def _prebuild():
    global _dashboard_html, _dashboard_built_at
    try:
        logger.info("대시보드 사전 생성 시작")
        _dashboard_html = _build_dashboard()
        _dashboard_built_at = time.time()
        logger.info("대시보드 사전 생성 완료")
    except Exception as e:
        logger.error("대시보드 사전 생성 실패: %s", e)


def _regen_async():
    """백그라운드 재생성 (조회 TTL·수동 refresh 공용). 락으로 중복 재생성 방지."""
    global _dashboard_html, _dashboard_built_at, _regenerating
    # 체크-앤-셋을 락 안에서 원자적으로 — 동시 요청/refresh가 둘 다 통과하는 레이스 차단.
    with _regen_lock:
        if _regenerating:
            return
        _regenerating = True
    try:
        _dashboard_html = _build_dashboard()
        _dashboard_built_at = time.time()
        logger.info("대시보드 재생성 완료")
    except Exception as e:
        logger.error("대시보드 재생성 실패: %s", e)
    finally:
        with _regen_lock:
            _regenerating = False


def _maybe_regen():
    """조회 시 TTL 경과 + 재생성 중 아니면 백그라운드 재생성 킥(자가치유 폴백)."""
    if _dashboard_html and not _regenerating and (time.time() - _dashboard_built_at) > _STALE_TTL:
        threading.Thread(target=_regen_async, daemon=True).start()


# ── MCP 서버 (Slack 봇 등 LLM이 데이터를 툴로 조회) ──────────────────────────
#  기존 쿼리를 감싼 조회 전용 툴을 streamable-http로 /mcp에 노출. stateless 모드.
from mcp_server import rival_mcp

_mcp_asgi = rival_mcp.streamable_http_app()


def _start_slack_bot_inprocess():
    """Slack 봇을 web 프로세스 내 백그라운드 태스크로 기동 (토큰 있을 때만).

    별도 워커/추가 비용 없이 web 상시가동에 얹혀 24/7. 봇은 같은 프로세스의
    MCP 마운트를 localhost로 호출(공개 URL 왕복 회피). 토큰 없으면 조용히 스킵.
    """
    bot_token = os.getenv("SLACK_BOT_TOKEN", "").strip()
    app_token = os.getenv("SLACK_APP_TOKEN", "").strip()
    if not (bot_token and app_token):
        logger.info("Slack 봇 토큰 미설정 → 봇 미기동 (대시보드/MCP만 실행)")
        return None
    try:
        port = os.getenv("PORT", "8000")
        # 같은 프로세스의 MCP를 localhost로 조회하도록 강제 (import 전에 설정)
        os.environ["MCP_SERVER_URL"] = f"http://127.0.0.1:{port}/mcp"
        import slack_bot
        from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler
        handler = AsyncSocketModeHandler(slack_bot.app, app_token)
        task = asyncio.create_task(handler.start_async())
        logger.info("Slack 봇 in-process 기동 (model=%s, mcp=%s)",
                    slack_bot.MODEL, os.environ["MCP_SERVER_URL"])
        return task
    except Exception as e:
        logger.error("Slack 봇 기동 실패(무시하고 web 계속): %s", e)
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 수집 스케줄러는 로컬 PC에서만 실행 (cli.py run).
    # Render는 대시보드 조회 전용(Supabase 읽기) — 여기서 스케줄러를 돌리면
    # 로컬과 이중 수집되어 OpenAI 토큰이 두 배로 소모됨.
    # 검색 탭 챗봇도 같은 프로세스의 MCP를 localhost로 호출 → 기본 URL 보장(봇 미기동이어도).
    _port = os.getenv("PORT", "8000")
    os.environ.setdefault("MCP_SERVER_URL", f"http://127.0.0.1:{_port}/mcp")
    t = threading.Thread(target=_prebuild, daemon=True)
    t.start()
    # 마운트한 MCP 앱의 세션 매니저 task group을 부모 lifespan에서 기동해야 함.
    async with rival_mcp.session_manager.run():
        bot_task = _start_slack_bot_inprocess()   # Slack 봇 in-process 기동
        try:
            yield
        finally:
            if bot_task and not bot_task.done():
                bot_task.cancel()


app = FastAPI(title="K-뷰티 경쟁사 인텔리전스", docs_url="/docs", lifespan=lifespan)


# MCP 엔드포인트 Bearer 인증 (MCP_API_KEY 설정 시에만). 공개 URL 보호.
_MCP_API_KEY = os.getenv("MCP_API_KEY", "").strip()

# 관리자 전용 게이팅(기간 조정·실시간 조회·대량 내려받기).
#
# 예전엔 비밀번호를 GET ?key=... 로 받았다. 주소창·브라우저 기록·Render 접근
# 로그에 평문으로 남아서(실제로 공유된 스크린샷 주소창에 찍혔다) 지금은
# 로그인만 POST 본문으로 받고, 이후 호출은 HttpOnly 쿠키로 인증한다.
# 쿠키에 비밀번호를 그대로 담지 않고 만료시각에 HMAC 서명한 토큰을 담는다.
_ADMIN_KEY = os.getenv("ADMIN_KEY", "").strip()
_ADMIN_COOKIE = "rv_admin"
_ADMIN_TTL = 12 * 3600          # 쿠키 유효기간(초)

if not _ADMIN_KEY:
    logger.warning("ADMIN_KEY 미설정 — 관리자 기능을 전부 차단합니다(fail-closed). "
                   "예전엔 통과시켰으나 환경변수 누락 시 조용히 열리는 문제가 있었습니다.")


def _admin_token(exp: int) -> str:
    """만료시각 + HMAC 서명. 키 자체를 쿠키에 싣지 않으려고."""
    sig = hmac.new(_ADMIN_KEY.encode(), str(exp).encode(), hashlib.sha256).hexdigest()[:32]
    return f"{exp}.{sig}"


def _token_ok(tok: str) -> bool:
    try:
        exp_s, sig = (tok or "").split(".", 1)
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < int(time.time()):
        return False
    return hmac.compare_digest(sig, _admin_token(exp).split(".", 1)[1])


def _admin_ok(key: str = "", request: Request = None) -> bool:
    """쿠키 토큰 > X-Admin-Key 헤더 > ?key= 쿼리 순으로 확인.

    쿼리 방식은 로컬 스케줄러 핑의 하위호환으로만 남겨 둔다 — 브라우저는
    더 이상 쓰지 않는다. ADMIN_KEY가 없으면 전부 거절(fail-closed).
    """
    if not _ADMIN_KEY:
        return False
    if request is not None:
        if _token_ok(request.cookies.get(_ADMIN_COOKIE, "")):
            return True
        hdr = (request.headers.get("x-admin-key") or "").strip()
        if hdr and hmac.compare_digest(hdr, _ADMIN_KEY):
            return True
    key = (key or "").strip()
    return bool(key) and hmac.compare_digest(key, _ADMIN_KEY)


def _client_ip(request: Request) -> str:
    """Render는 프록시 뒤라 request.client.host가 전부 같은 값으로 찍힌다.
    X-Forwarded-For 첫 홉을 쓴다(위조 가능하지만 IP별 구분엔 이게 최선)."""
    xff = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    return xff or (request.client.host if request.client else "?")


# 비밀번호를 사람이 고른 값으로 쓰면 사전 대입이 현실적인 위협이 된다.
# 로그인만 조인다 — IP당 실패 5회/15분이면 잠그고, 실패마다 1초를 쉰다.
# (XFF는 위조할 수 있으니 지연이 실질 방어다. 근본 방어는 긴 무작위 키.)
_LOGIN_MAX_FAIL = 5
_LOGIN_WINDOW = 15 * 60
_LOGIN_FAIL_DELAY = 1.0
_login_fails: dict = {}


def _login_locked(ip: str) -> int:
    """남은 잠금 시간(초). 0이면 통과."""
    now = time.time()
    fails = [t for t in _login_fails.get(ip, []) if now - t < _LOGIN_WINDOW]
    _login_fails[ip] = fails
    if len(fails) < _LOGIN_MAX_FAIL:
        return 0
    return int(_LOGIN_WINDOW - (now - fails[0])) + 1


def _login_record(ip: str, ok: bool) -> None:
    now = time.time()
    if ok:
        _login_fails.pop(ip, None)
        return
    _login_fails.setdefault(ip, []).append(now)
    if len(_login_fails) > 2000:        # 메모리 상한(오래된 IP 정리)
        for k in [k for k, v in _login_fails.items()
                  if not any(now - t < _LOGIN_WINDOW for t in v)]:
            _login_fails.pop(k, None)


class _MCPAuthASGI:
    """마운트된 MCP 앱 앞단 Bearer 인증 래퍼 (키 미설정 시 통과)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and _MCP_API_KEY:
            headers = dict(scope.get("headers") or [])
            auth = headers.get(b"authorization", b"").decode()
            if auth != f"Bearer {_MCP_API_KEY}":
                await send({"type": "http.response.start", "status": 401,
                            "headers": [(b"content-type", b"text/plain")]})
                await send({"type": "http.response.body", "body": b"unauthorized"})
                return
        await self.app(scope, receive, send)


app.mount("/mcp", _MCPAuthASGI(_mcp_asgi))

# ChatGPT 커스텀 GPT의 '작업(Actions)'용 REST 계층. ChatGPT 커넥터는 MCP에
# OAuth 2.1 + 동적 클라이언트 등록을 요구해서 고정 키로는 못 붙는다.
# Actions는 OpenAPI + Bearer를 받으므로 같은 툴을 이쪽으로도 연다.
try:
    from gpt_actions import gpt_api
    app.mount("/gpt", gpt_api)
except Exception as e:                      # noqa: BLE001
    logger.warning("GPT Actions 계층 마운트 실패(건너뜀): %s", e)


# ── 대시보드 ────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "ready": bool(_dashboard_html)}


# 브라우저가 옛 대시보드를 캐시해 갱신이 안 보이는 문제 방지 — 항상 최신 제공.
_NO_CACHE = {"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
             "Pragma": "no-cache", "Expires": "0"}


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """메인 대시보드. 조회 시 TTL 경과했으면 백그라운드 재생성(자가치유)."""
    if not _dashboard_html:
        return HTMLResponse(_LOADING_PAGE, status_code=200, headers=_NO_CACHE)
    _maybe_regen()   # 오래됐으면 다음 조회를 위해 미리 갱신(현재 요청은 즉시 응답)
    return HTMLResponse(_dashboard_html, headers=_NO_CACHE)


@app.post("/api/admin-check")
async def admin_check(request: Request):
    """관리자 로그인 — 성공 시 HttpOnly 쿠키를 내린다.

    예전엔 GET ?key=... 였다. 비밀번호가 주소창·브라우저 기록·서버 접근 로그에
    평문으로 남아서 POST 본문으로 옮겼다. 이후 관리자 호출은 쿠키로 인증한다.
    """
    ip = _client_ip(request)
    wait = _login_locked(ip)
    if wait:
        return JSONResponse(
            {"ok": False, "error": f"시도가 너무 많습니다. {wait // 60 + 1}분 후 다시 시도하세요."},
            status_code=429)
    try:
        payload = await request.json()
    except Exception:
        payload = {}
    key = str((payload or {}).get("key") or "").strip()
    ok = bool(_ADMIN_KEY) and bool(key) and hmac.compare_digest(key, _ADMIN_KEY)
    _login_record(ip, ok)
    if not ok:
        await asyncio.sleep(_LOGIN_FAIL_DELAY)      # 자동 대입 속도를 떨어뜨린다
        logger.warning("관리자 로그인 실패 — ip=%s", ip)
        return JSONResponse({"ok": False}, status_code=401)
    exp = int(time.time()) + _ADMIN_TTL
    res = JSONResponse({"ok": True, "ttl": _ADMIN_TTL})
    res.set_cookie(_ADMIN_COOKIE, _admin_token(exp), max_age=_ADMIN_TTL,
                   httponly=True, samesite="lax",
                   secure=request.url.scheme == "https", path="/")
    return res


@app.post("/api/admin-logout")
async def admin_logout():
    """공용 PC에서 쿠키를 즉시 버릴 수 있게."""
    res = JSONResponse({"ok": True})
    res.delete_cookie(_ADMIN_COOKIE, path="/")
    return res


@app.post("/api/refresh")
async def refresh(request: Request, background_tasks: BackgroundTasks,
                  key: str = Query("")):
    """대시보드 재생성 (백그라운드). 로컬 수집 직후 ping_dashboard_refresh가 호출.
    ADMIN_KEY 설정 시 관리자 키 필요(로컬 ping도 ADMIN_KEY 동봉)."""
    if not _admin_ok(key, request):
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    background_tasks.add_task(_regen_async)
    return {"status": "ok", "message": "재생성 중 — 잠시 후 새로고침하세요"}


# /api/ask 남용 방지 — 공개 챗봇이라 인증은 못 걸지만(대시보드 JS가 호출),
# 길이 상한 + 간단한 인메모리 IP 레이트리밋으로 비용/남용 폭주만 차단.
_ASK_MAX_LEN = 500
_ASK_TIMEOUT_SEC = int(os.getenv("ASK_TIMEOUT_SEC", "55"))   # 무한 대기 방지 상한
_ASK_RATE_MAX = 12          # 분당 IP별 허용 횟수
_ask_hits: dict = {}


def _ask_rate_ok(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _ask_hits.get(ip, []) if now - t < 60]
    if len(hits) >= _ASK_RATE_MAX:
        _ask_hits[ip] = hits
        return False
    hits.append(now)
    _ask_hits[ip] = hits
    if len(_ask_hits) > 2000:      # 메모리 상한(오래된 IP 정리)
        for k in [k for k, v in _ask_hits.items() if not any(now - t < 60 for t in v)]:
            _ask_hits.pop(k, None)
    return True


@app.post("/api/ask")
async def api_ask(request: Request):
    """통합검색 챗봇 — 자연어 질문(기간·브랜드·국가)을 MCP 툴로 조회해 답변.
    Slack 봇과 동일한 LLM tool-calling 로직(slack_bot.answer) 재사용."""
    ip = request.client.host if request.client else "?"
    if not _ask_rate_ok(ip):
        return JSONResponse({"answer": "요청이 너무 잦습니다. 잠시 후 다시 시도해 주세요."}, status_code=429)
    try:
        body = await request.json()
    except Exception:
        body = {}
    q = (body.get("question") or "").strip()
    if not q:
        return JSONResponse({"answer": "질문을 입력해 주세요."}, status_code=400)
    if len(q) > _ASK_MAX_LEN:
        return JSONResponse({"answer": f"질문이 너무 깁니다({_ASK_MAX_LEN}자 이내)."}, status_code=400)
    if not os.getenv("OPENAI_API_KEY"):
        return JSONResponse({"answer": "검색 기능 미설정(OPENAI_API_KEY 필요)."}, status_code=503)
    try:
        import slack_bot
        # /api/ask는 단발 JSON 응답 — 스트리밍 콜백 불필요(slack_bot은 on_delta를
        # 동기 호출하므로 no-op 동기 콜백을 넘긴다). 예전의 async _collect는 await되지
        # 않아 코루틴이 버려지고 parts가 항상 비어 무의미했음.
        # 서버측 상한 — 무한 대기 대신 안내 응답. 프론트도 별도 타임아웃을 건다.
        ans = await asyncio.wait_for(slack_bot.answer(q, [], lambda t: None),
                                     timeout=_ASK_TIMEOUT_SEC)
        return {"answer": ans or "관련 데이터를 찾지 못했어요."}
    except asyncio.TimeoutError:
        logger.warning("검색 응답 타임아웃(%ds): %s", _ASK_TIMEOUT_SEC, q[:60])
        return JSONResponse(
            {"answer": f"조회가 {_ASK_TIMEOUT_SEC}초를 넘겨 중단했어요. "
                       "질문을 더 좁혀서(브랜드·국가·기간) 다시 시도해 주세요.",
             "error": "timeout"}, status_code=504)
    except Exception as e:
        logger.warning("검색 응답 실패: %s", e)
        return JSONResponse({"answer": "검색 처리 중 오류가 발생했어요. 잠시 후 다시 시도해 주세요.",
                             "error": "server"}, status_code=500)


# ── 인사이트 API ─────────────────────────────────────────────────────────────

@app.get("/api/period")
async def api_period(request: Request,
                     from_date: str = Query(..., alias="from"),
                     to_date: str = Query(..., alias="to")):
    """임의 구간(과거 포함) 브리핑 데이터 — KPI·기사·synth를 DB에서 직접 조회.
    클라 90일 캐시로 못 보는 구간(예: 26년 1분기)을 서버가 계산해 반환. 관리자 전용."""
    if not _admin_ok(request=request):
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    import re as _re
    if not (_re.match(r"^\d{4}-\d{2}-\d{2}$", from_date or "") and _re.match(r"^\d{4}-\d{2}-\d{2}$", to_date or "")):
        return JSONResponse({"error": "날짜 형식 오류(YYYY-MM-DD)"}, status_code=400)
    if from_date > to_date:
        return JSONResponse({"error": "시작일이 종료일보다 늦습니다"}, status_code=400)
    try:
        import asyncio as _a
        from dashboard.generate import build_period_payload
        payload = await _a.to_thread(build_period_payload, from_date, to_date)
        return payload
    except Exception as e:
        logger.warning("구간 조회 실패: %s", e)
        return JSONResponse({"error": "구간 조회 중 오류가 발생했습니다."}, status_code=500)


@app.get("/api/articles")
async def api_articles(days: int = Query(30)):
    """기간별 기사 목록 — 대시보드가 기간을 바꿀 때 받아간다.

    30·60·90일 기사를 전부 HTML에 박아두니 3.8MB였고 95%가 이 배열이었다.
    기본 기간만 싣고 나머지는 여기서 준다.
    """
    if days not in (30, 60, 90):
        return JSONResponse({"error": "지원하지 않는 기간"}, status_code=400)
    try:
        from dashboard.generate import build_period_articles
        arts = await asyncio.to_thread(build_period_articles, days)
        return JSONResponse({"days": days, "articles": arts})
    except Exception as e:
        logger.warning("기간 기사 조회 실패(%s일): %s", days, e)
        return JSONResponse({"error": "기사를 불러오지 못했습니다."}, status_code=500)


@app.get("/api/insights")
async def api_insights(
    request: Request,
    from_date: str = Query(..., description="시작일 YYYY-MM-DD"),
    to_date: str   = Query(..., description="종료일 YYYY-MM-DD"),
):
    """날짜 범위 기반 브랜드 전략 인사이트. 관리자 전용.

    DB 캐시 히트 → 즉시 반환.
    캐시 미스 → OpenAI 생성 → DB 저장 → 반환.
    동일 날짜 범위는 영구 캐시 (기사가 변하지 않으므로 결과도 동일).
    """
    if not _admin_ok(request=request):
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    from analytics.queries import (
        get_insights_cache,
        upsert_insight_cache,
        get_brand_insights_raw_by_range,
    )
    from analytics.summarizer import generate_brand_strategy_summary
    from storage.models import get_session

    session = get_session()
    try:
        cached = get_insights_cache(session, from_date, to_date)
        raw    = get_brand_insights_raw_by_range(session, from_date, to_date)
    finally:
        session.close()

    if not raw:
        return JSONResponse({})

    result: dict = {}
    save_session = get_session()
    try:
        for brand, data in raw.items():
            if brand in cached and cached[brand].get("summary"):
                ins = cached[brand]
            else:
                summary = generate_brand_strategy_summary(brand, data.get("articles", []))
                ins = {
                    "summary":  summary,
                    "top_act":  data["top_act"],
                    "top_pct":  data["top_pct"],
                    "high_pct": data["high_pct"],
                }
                if summary:  # 빈 문자열은 캐시 저장 안 함
                    upsert_insight_cache(save_session, brand, from_date, to_date, ins)

            result[brand] = {
                "top_act":       html_lib.escape(ins.get("top_act", "기타")),
                "top_pct":       ins.get("top_pct", 0),
                "high_pct":      ins.get("high_pct", 0.0),
                "strategy":      html_lib.escape(ins.get("summary", "")),
                "top_countries": data["top_countries"],
                "key_articles": [
                    {
                        "imp":      a.get("imp", "low"),
                        "date":     a.get("date", ""),
                        "act":      html_lib.escape(a.get("act", "")),
                        "title_ko": html_lib.escape(a.get("title_ko", "")),
                        "url":      a.get("url", ""),
                    }
                    for a in data.get("articles", [])[:3]
                ],
            }
    finally:
        save_session.close()

    return JSONResponse(result)


@app.get("/api/cell-insight")
async def api_cell_insight(
    brand: str    = Query(..., description="브랜드명"),
    country: str  = Query(..., description="국가 코드 (예: US)"),
    from_date: str = Query(..., description="시작일 YYYY-MM-DD"),
    to_date: str   = Query(..., description="종료일 YYYY-MM-DD"),
):
    """히트맵 셀 드릴다운용 브랜드×국가 전략 요약.

    캐시 히트 → 즉시 반환. 미스 → OpenAI 생성 → DB 저장 → 반환.
    사용자 클릭 시에만 호출되며 (brand, country, from_date, to_date)로 영구 캐시.
    """
    from collections import Counter
    from analytics.queries import (
        get_brand_country_insight_cache,
        upsert_brand_country_insight,
        get_brand_country_articles,
    )
    from analytics.summarizer import generate_brand_country_summary
    from storage.models import get_session

    # 기사는 항상 조회 — 활동유형 분포(1차 직관성 칩)는 캐시 히트에도 최신 제공.
    # AI 요약만 캐시로 절약.
    session = get_session()
    try:
        articles = get_brand_country_articles(session, brand, country, from_date, to_date)
        cached = get_brand_country_insight_cache(session, brand, country, from_date, to_date)
    finally:
        session.close()

    high = sum(1 for a in articles if a["imp"] == "high")
    med  = len(articles) - high

    # 주력 활동유형 top 3 (HIGH/MED 기사 기준)
    act_cnt = Counter(a["act"] for a in articles if a.get("act"))
    act_total = sum(act_cnt.values()) or 1
    activities = [
        {"act": k, "count": v, "pct": round(v / act_total * 100)}
        for k, v in act_cnt.most_common(3)
    ]

    if cached:
        summary = cached["summary"]
        is_cached = True
    else:
        summary = generate_brand_country_summary(brand, country, articles)
        is_cached = False
        if summary:
            save_session = get_session()
            try:
                upsert_brand_country_insight(
                    save_session, brand, country, from_date, to_date,
                    {"summary": summary, "high_count": high, "med_count": med},
                )
            finally:
                save_session.close()

    return JSONResponse({
        "summary":    html_lib.escape(summary),
        "activities": activities,
        "stats":      {"high": high, "med": med},
        "cached":     is_cached,
    })


# ── 데이터 탐색 API ──────────────────────────────────────────────────────────
# 기획팀이 누적 데이터 전체를 기간·브랜드·국가로 직접 뒤져보고 엑셀로 받아가는 통로.
# 조회 대상 테이블·컬럼은 analytics.queries.EXPLORE_DATASETS 화이트리스트에만 있고
# 사용자 입력은 전부 바인딩 파라미터로 들어가므로 임의 SQL은 실행되지 않는다.

_EXPLORE_MAX = 5000        # 화면 표시 상한
_EXPLORE_CSV_MAX = 50000   # 내려받기 상한 — 엑셀이 감당하는 선
# 본문을 붙이면 한 행이 평균 3,300자다. 5만 행이면 결과를 담는 것만으로 167MB라
# Render 인스턴스가 버티지 못한다. 본문 포함일 때만 상한을 따로 둔다.
_EXPLORE_CSV_BODY_MAX = 8000


def _explore_fetch(dataset: str, date_from: str, date_to: str,
                   brand: str, country: str, limit: int, offset: int,
                   max_rows: int = _EXPLORE_MAX, with_body: bool = False) -> dict:
    from analytics.queries import explore_query
    from storage.models import get_session
    session = get_session()
    try:
        return explore_query(session, dataset, date_from=date_from, date_to=date_to,
                             brand=brand, country=country, limit=limit, offset=offset,
                             max_rows=max_rows, with_body=with_body)
    finally:
        session.close()


@app.get("/api/explore")
async def api_explore(dataset: str = Query("news"),
                      from_date: str = Query("", alias="from"),
                      to_date: str = Query("", alias="to"),
                      brand: str = Query(""), country: str = Query(""),
                      limit: int = Query(200), offset: int = Query(0)):
    """데이터 탐색 — 화이트리스트 데이터셋을 기간·브랜드·국가로 조회."""
    import re as _re
    for d in (from_date, to_date):
        if d and not _re.match(r"^\d{4}-\d{2}-\d{2}$", d):
            return JSONResponse({"error": "날짜 형식 오류(YYYY-MM-DD)"}, status_code=400)
    if from_date and to_date and from_date > to_date:
        return JSONResponse({"error": "시작일이 종료일보다 늦습니다"}, status_code=400)
    try:
        data = await asyncio.to_thread(
            _explore_fetch, dataset, from_date, to_date, brand[:60], country[:40],
            max(1, min(limit, _EXPLORE_MAX)), max(0, offset))
        return JSONResponse(data)
    except Exception as e:
        logger.warning("데이터 탐색 실패: %s", e)
        return JSONResponse({"error": "조회 중 오류가 발생했습니다."}, status_code=500)


@app.get("/api/explore/summary")
async def api_explore_summary():
    """데이터셋별 적재 현황(건수·기간) — 무엇이 얼마나 쌓였는지 한눈에."""
    def _run():
        from analytics.queries import explore_summary
        from storage.models import get_session
        session = get_session()
        try:
            return explore_summary(session)
        finally:
            session.close()
    try:
        return JSONResponse({"datasets": await asyncio.to_thread(_run)})
    except Exception as e:
        logger.warning("적재 현황 조회 실패: %s", e)
        return JSONResponse({"datasets": []})


@app.get("/api/explore/csv")
async def api_explore_csv(request: Request,
                          dataset: str = Query("news"),
                          from_date: str = Query("", alias="from"),
                          to_date: str = Query("", alias="to"),
                          brand: str = Query(""), country: str = Query(""),
                          body: int = Query(0)):
    """지금 보고 있는 조건 그대로 CSV로 내려받기(최대 5만 행). 관리자 전용.

    화면 조회는 열어두되 대량 추출만 막는다 — 무인증으로 6만여 행 원본을 반복해서
    받아갈 수 있으면 데이터 유출이자 부하 공격 경로가 된다.
    """
    import csv
    import io
    import re as _re
    from fastapi.responses import StreamingResponse

    from analytics.queries import EXPLORE_DATASETS

    if not _admin_ok(request=request):
        return JSONResponse({"error": "unauthorized"}, status_code=403)
    # 파일명에 쓰이므로 화이트리스트 검증을 여기서 먼저 한다. 검증 없이 보간하면
    # 비ASCII 데이터셋명이 Content-Disposition(latin-1)에 들어가 500이 되고,
    # 따옴표가 섞이면 filename 파라미터가 쪼개진다.
    if dataset not in EXPLORE_DATASETS:
        return JSONResponse({"error": "알 수 없는 데이터셋"}, status_code=400)
    for d in (from_date, to_date):
        if d and not _re.match(r"^\d{4}-\d{2}-\d{2}$", d):
            return JSONResponse({"error": "날짜 형식 오류(YYYY-MM-DD)"}, status_code=400)
    if from_date and to_date and from_date > to_date:
        return JSONResponse({"error": "시작일이 종료일보다 늦습니다"}, status_code=400)
    from analytics.queries import EXPLORE_DATASETS as _DS
    want_body = bool(body) and bool(_DS[dataset].get("body"))
    cap = _EXPLORE_CSV_BODY_MAX if want_body else _EXPLORE_CSV_MAX
    try:
        data = await asyncio.to_thread(
            _explore_fetch, dataset, from_date, to_date, brand[:200], country[:200],
            cap, 0, cap, want_body)
    except Exception as e:
        logger.warning("CSV 내려받기 실패: %s", e)
        return JSONResponse({"error": "내려받기 중 오류가 발생했습니다."}, status_code=500)

    rows = data.get("rows") or []
    total = data.get("total") or 0
    ignored = data.get("ignored") or []

    def _stream():
        # 5만 행을 통째로 메모리에 담지 않고 흘려보낸다(재무 CSV 1건당 35MB 점유였다).
        buf = io.StringIO()
        w = csv.writer(buf)

        def _flush():
            out = buf.getvalue()
            buf.seek(0); buf.truncate(0)
            return out

        yield "﻿".encode("utf-8")      # 엑셀이 UTF-8로 읽게 하는 BOM. 없으면 한글이 깨진다
        w.writerow(data.get("cols") or [])
        yield _flush().encode("utf-8")
        for i, row in enumerate(rows, 1):
            w.writerow(row)
            if i % 500 == 0:
                yield _flush().encode("utf-8")
        tail = _flush()
        if tail:
            yield tail.encode("utf-8")

    # 파일명은 '실제로 적용된' 조건만 담는다. 무시된 축을 파일명에 쓰면
    # 2일치를 요청했는데 전체가 담긴 파일이 기간이 걸린 것처럼 보인다.
    parts = [dataset]
    if from_date or to_date:
        if "기간" not in ignored:
            parts.append(f"{from_date or 'all'}_{to_date or 'all'}")
        else:
            parts.append("nodatefilter")
    if want_body:
        parts.append("withbody")
    if len(rows) < total:
        parts.append(f"partial{len(rows)}of{total}")   # ASCII만 — 헤더는 latin-1이다
    fname = "-".join(parts) + ".csv"
    headers = {"Content-Disposition": f'attachment; filename="{fname}"'}
    if ignored:
        headers["X-Explore-Ignored"] = ",".join(
            {"기간": "date", "브랜드": "brand", "국가": "country"}.get(x, x) for x in ignored)
    return StreamingResponse(_stream(), media_type="text/csv; charset=utf-8", headers=headers)


# ── 재무 탭 회사 목록 ─────────────────────────────────────────────────────────
# 7,209개사를 대시보드 HTML에 통째로 박으면 979KB가 재무 탭을 열지 않는 사람에게도
# 매번 나가고, 구매 데이터(NICE BizLine)가 인증 없이 그대로 노출된다. HTML에는
# 모니터링 브랜드만 싣고 '화장품업 전체'·'전체 기업'을 누를 때 여기서 받아간다.

# 'all'(7,200개사)은 뺐다. 화면에서 토글만 감추면 URL을 아는 사람은 그대로 받아갈
# 수 있어 NICE 구매 데이터가 통째로 열린 경로가 된다. 화장품업까지만 연다.
_COMPANY_SCOPES = {"monitored", "cosmetic"}


@app.get("/api/finance/csv")
async def api_finance_csv(request: Request):
    """재무 전체 내려받기 — 엑셀 파일(.xlsx).

    화면엔 기획팀 요청대로 매출·영업이익률·광고비율만 둔다. 영업이익 금액·
    광고비 금액·분기별 수치는 여기서 본다. 범위는 **화장품업 전체**다.

    CSV로 주면 열이 31개라 엑셀에서 폭이 다 좁고 숫자가 raw로 찍혀 읽기 어렵다.
    두 줄 머리글·천단위·퍼센트·틀고정을 넣은 xlsx로 만든다.
    """
    from fastapi.responses import Response

    if not _admin_ok(request=request):
        return JSONResponse({"error": "unauthorized"}, status_code=403)

    def _build():
        from analytics.finance_xlsx import build_workbook
        from analytics.queries import get_financial_export
        from storage.models import get_session
        se = get_session()
        try:
            d = get_financial_export(se)
        finally:
            se.close()
        y = d.get("years") or {}
        return build_workbook(d.get("rows") or [],
                              y.get("base", 2024), y.get("prev", 2025),
                              y.get("est", 2026)), len(d.get("rows") or [])

    try:
        blob, n = await asyncio.to_thread(_build)
    except Exception as e:
        logger.warning("재무 엑셀 생성 실패: %s", e)
        return JSONResponse({"error": "내려받기 중 오류가 발생했습니다."}, status_code=500)

    logger.info("재무 엑셀 내려받기 — %d개사 · %d바이트", n, len(blob))
    return Response(
        content=blob,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": 'attachment; filename="financials.xlsx"'})


@app.get("/api/companies")
async def api_companies(scope: str = Query("cosmetic")):
    """재무 탭 회사 행 데이터. scope: monitored | cosmetic."""
    if scope not in _COMPANY_SCOPES:
        return JSONResponse({"error": "알 수 없는 범위"}, status_code=400)
    try:
        from dashboard.generate import build_companies_payload
        rows = await asyncio.to_thread(build_companies_payload, scope)
        return JSONResponse({"scope": scope, "rows": rows})
    except Exception as e:
        logger.warning("회사 목록 조회 실패 [%s]: %s", scope, e)
        return JSONResponse({"error": "조회 중 오류가 발생했습니다."}, status_code=500)


# ── 로컬 실행 ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
