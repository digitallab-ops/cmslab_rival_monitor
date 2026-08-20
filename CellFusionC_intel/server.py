"""
K-뷰티 경쟁사 인텔리전스 — FastAPI 서버

실행:
    uvicorn server:app --reload          # 개발
    uvicorn server:app --host 0.0.0.0 --port 8000  # 배포
"""

import asyncio
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
from fastapi import FastAPI, Query, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse

logger = logging.getLogger(__name__)

# 생성된 HTML 캐시 (메모리)
_dashboard_html: str = ""
_dashboard_built_at: float = 0.0        # 마지막 생성 시각(epoch)
_regenerating: bool = False             # 중복 재생성 방지
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
    """백그라운드 재생성 (조회 TTL·수동 refresh 공용). 중복 방지 플래그."""
    global _dashboard_html, _dashboard_built_at, _regenerating
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


@app.post("/api/refresh")
async def refresh(background_tasks: BackgroundTasks):
    """대시보드 재생성 (백그라운드). 로컬 수집 직후 ping_dashboard_refresh가 호출."""
    background_tasks.add_task(_regen_async)
    return {"status": "ok", "message": "재생성 중 — 잠시 후 새로고침하세요"}


# ── 인사이트 API ─────────────────────────────────────────────────────────────

@app.get("/api/insights")
async def api_insights(
    from_date: str = Query(..., description="시작일 YYYY-MM-DD"),
    to_date: str   = Query(..., description="종료일 YYYY-MM-DD"),
):
    """날짜 범위 기반 브랜드 전략 인사이트.

    DB 캐시 히트 → 즉시 반환.
    캐시 미스 → OpenAI 생성 → DB 저장 → 반환.
    동일 날짜 범위는 영구 캐시 (기사가 변하지 않으므로 결과도 동일).
    """
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


# ── 로컬 실행 ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
