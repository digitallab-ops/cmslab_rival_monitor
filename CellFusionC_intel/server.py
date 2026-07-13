"""
K-뷰티 경쟁사 인텔리전스 — FastAPI 서버

실행:
    uvicorn server:app --reload          # 개발
    uvicorn server:app --host 0.0.0.0 --port 8000  # 배포
"""

import html as html_lib
import logging
import os
import sys
import threading

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
    global _dashboard_html
    try:
        logger.info("대시보드 사전 생성 시작")
        _dashboard_html = _build_dashboard()
        logger.info("대시보드 사전 생성 완료")
    except Exception as e:
        logger.error("대시보드 사전 생성 실패: %s", e)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 수집 스케줄러는 로컬 PC에서만 실행 (cli.py run).
    # Render는 대시보드 조회 전용(Supabase 읽기) — 여기서 스케줄러를 돌리면
    # 로컬과 이중 수집되어 OpenAI 토큰이 두 배로 소모됨.
    t = threading.Thread(target=_prebuild, daemon=True)
    t.start()
    yield


app = FastAPI(title="K-뷰티 경쟁사 인텔리전스", docs_url="/docs", lifespan=lifespan)


# ── 대시보드 ────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "ready": bool(_dashboard_html)}


@app.get("/", response_class=HTMLResponse)
async def dashboard():
    """메인 대시보드."""
    if not _dashboard_html:
        return HTMLResponse(_LOADING_PAGE, status_code=200)
    return _dashboard_html


@app.post("/api/refresh")
async def refresh(background_tasks: BackgroundTasks):
    """대시보드 재생성 (백그라운드). 기사 수집 후 호출."""
    def _regen():
        global _dashboard_html
        _dashboard_html = _build_dashboard()
        logger.info("대시보드 재생성 완료")

    background_tasks.add_task(_regen)
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
    from analytics.queries import (
        get_brand_country_insight_cache,
        upsert_brand_country_insight,
        get_brand_country_articles,
    )
    from analytics.summarizer import generate_brand_country_summary
    from storage.models import get_session

    session = get_session()
    try:
        cached = get_brand_country_insight_cache(session, brand, country, from_date, to_date)
        if cached:
            return JSONResponse({
                "summary": html_lib.escape(cached["summary"]),
                "stats":   {"high": cached["high_count"], "med": cached["med_count"]},
                "cached":  True,
            })

        articles = get_brand_country_articles(session, brand, country, from_date, to_date)
    finally:
        session.close()

    high = sum(1 for a in articles if a["imp"] == "high")
    med  = len(articles) - high

    summary = generate_brand_country_summary(brand, country, articles)

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
        "summary": html_lib.escape(summary),
        "stats":   {"high": high, "med": med},
        "cached":  False,
    })


# ── 로컬 실행 ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=True)
