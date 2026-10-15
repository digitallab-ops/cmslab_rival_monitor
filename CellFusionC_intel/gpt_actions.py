"""ChatGPT 커스텀 GPT '작업(Actions)'용 REST 계층.

왜 따로 만드나 — ChatGPT 커스텀 커넥터는 MCP를 붙일 때 OAuth 2.1 + 동적
클라이언트 등록을 요구한다. 우리처럼 고정 키를 쓰는 서버는 못 붙는다.
반면 '나만의 GPT'의 작업(Actions)은 OpenAPI + API 키(Bearer)를 그대로 받는다.
그래서 MCP 툴 19개를 그대로 감싼 GET 엔드포인트를 열고, 스키마를 가져가게 한다.

메인 앱(server.py)에 /gpt 로 마운트한다. 서브앱으로 분리한 이유는 스키마
때문이다 — 메인 앱에 붙이면 /api/explore 같은 내부 라우트까지 스키마에 딸려
들어가고, ChatGPT 작업 개수 상한(30개)에 걸린다.

인증은 MCP와 분리된 GPT_API_KEY를 쓴다. 외부에 나가는 키라서, 끊고 싶을 때
이것만 갈면 우리 MCP는 그대로 둘 수 있다.
"""

import functools
import inspect
import json
import logging
import os

from fastapi import Depends, FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool

import mcp_server as M

logger = logging.getLogger(__name__)

PUBLIC_BASE = (os.getenv("RENDER_EXTERNAL_URL")
               or "https://cmslab-rival-monitor.onrender.com").rstrip("/")

# ChatGPT는 응답이 길면 통째로 잘라버리고 모델이 엉뚱하게 답한다.
# 넘치면 리스트를 줄여서 "구조는 남기고 건수를 줄인" 형태로 돌려준다.
_MAX_CHARS = 45_000

gpt_api = FastAPI(
    title="K-뷰티 경쟁사 인텔리전스",
    version="1.0.0",
    description=(
        "씨엠에스랩(더마 선케어 브랜드 '셀퓨전씨')의 K-뷰티 경쟁사 모니터링 데이터. "
        "경쟁 브랜드의 해외 활동(신시장 진출·유통 채널·신제품·인플루언서·투자), "
        "수요·성과 신호(검색 트렌드·관세청 수출·상표 출원), 재무, "
        "올리브영 국내 랭킹·리뷰를 조회한다. "
        "답변은 항상 셀퓨전씨 관점의 시사점으로 연결할 것."
    ),
    servers=[{"url": f"{PUBLIC_BASE}/gpt"}],
    # 마운트하면 FastAPI가 상대경로 "/gpt"를 servers 맨 앞에 끼워 넣는다.
    # ChatGPT는 첫 항목을 집어서 호출하므로 그대로 두면 주소가 깨진다.
    root_path_in_servers=False,
    docs_url=None, redoc_url=None,          # 문서 UI는 필요 없다. 스키마만 쓴다.
)


def _require_key(request: Request) -> None:
    """Bearer 인증. 키가 없으면 전부 거절(fail-closed)."""
    key = (os.getenv("GPT_API_KEY") or "").strip()
    if not key:
        raise HTTPException(503, "GPT_API_KEY 미설정 — 이 엔드포인트는 비활성입니다.")
    auth = (request.headers.get("authorization") or "").strip()
    if auth != f"Bearer {key}":
        raise HTTPException(401, "unauthorized")


def _shrink(obj):
    """_MAX_CHARS 안에 들어올 때까지 리스트 길이를 절반씩 줄인다.

    통째로 자르면 JSON이 깨져서 모델이 아예 못 읽는다. 구조는 보존하고
    건수만 줄이는 쪽이 낫다 — 상위 N건이 대개 중요한 것들이기도 하다.
    """
    s = json.dumps(obj, ensure_ascii=False, default=str)
    if len(s) <= _MAX_CHARS:
        return obj

    def cut(o, ratio):
        if isinstance(o, list):
            keep = max(1, int(len(o) * ratio))
            return [cut(x, ratio) for x in o[:keep]]
        if isinstance(o, dict):
            return {k: cut(v, ratio) for k, v in o.items()}
        return o

    ratio = 0.5
    for _ in range(8):
        trimmed = cut(obj, ratio)
        s = json.dumps(trimmed, ensure_ascii=False, default=str)
        if len(s) <= _MAX_CHARS:
            if isinstance(trimmed, dict):
                trimmed["_truncated"] = (
                    "응답이 길어 목록을 상위 일부만 남겼습니다. "
                    "더 좁은 조건(기간·브랜드·국가)으로 다시 물어보세요.")
            return trimmed
        ratio *= 0.5
    return {"_truncated": "응답이 너무 큽니다. 조건을 좁혀 주세요."}


def _register(fn, summary: str) -> None:
    """MCP 툴 함수를 그대로 GET 라우트로 연다.

    functools.wraps 가 __wrapped__ 를 달아줘서 FastAPI가 원본 시그니처를 읽는다.
    덕분에 brand/days 같은 인자가 쿼리 파라미터로 스키마에 그대로 실린다.
    """
    is_async = inspect.iscoroutinefunction(fn)

    @functools.wraps(fn)
    async def route(**kw):
        try:
            out = await fn(**kw) if is_async else await run_in_threadpool(fn, **kw)
        except Exception as e:                       # noqa: BLE001
            logger.warning("GPT 작업 %s 실패: %s", fn.__name__, e)
            raise HTTPException(500, f"조회 실패: {type(e).__name__}") from e
        return _shrink(out)

    route.__name__ = fn.__name__
    gpt_api.add_api_route(
        f"/{fn.__name__}", route, methods=["GET"],
        operation_id=fn.__name__, summary=summary,
        description=(inspect.getdoc(fn) or summary),
        dependencies=[Depends(_require_key)],
    )


# MCP와 같은 19개. 설명은 GPT가 어느 툴을 쓸지 고르는 근거라 한 줄로 또렷하게.
for _fn, _summary in [
    (M.list_brands, "추적 중인 경쟁 브랜드 목록(영문명·Tier). 다른 툴의 brand 인자로 이 영문명을 쓴다"),
    (M.get_brand_intel, "특정 경쟁 브랜드의 최근 활동 + 전략 요약"),
    (M.get_market_intel, "특정 국가에서 경쟁사들이 무엇을 하고 있는지 요약"),
    (M.search_news, "경쟁사 뉴스 자유 검색(키워드가 제목·본문에 포함된 기사)"),
    (M.get_category_battle_view, "카테고리별 경쟁사 활동 집중도(선케어·크림·앰플 등)"),
    (M.get_expansion_playbook_view, "경쟁사가 각 해외 시장에 어떤 채널로 진입했는지"),
    (M.get_briefings, "저장된 브리핑(주간·일간) 전문"),
    (M.get_brand_momentum_view, "최근 4주 vs 직전 4주 활동량 — 급상승·식는 브랜드"),
    (M.get_demand_signal, "수요 신호(네이버 검색 트렌드) vs 공급 신호(보도량) 삼각검증"),
    (M.get_export_growth, "관세청 화장품 수출 성장 — 국가별 최근 3개월 vs 전년 동기"),
    (M.get_growth_story, "어느 시장이 왜 크는가 — 수출 성장과 그 시장 경쟁사 활동을 엮음"),
    (M.get_financials, "경쟁사 실적(DART) — 매출·영업이익·영업이익률 + 매출 YoY"),
    (M.get_trademark_signals_view, "해외 상표 출원 = 진출 선행신호(미국·일본). 뉴스보다 먼저 잡힌다"),
    (M.get_search_spikes, "글로벌 검색 급등(구글 트렌드) — 최근 7일 vs 직전 28일"),
    (M.get_oliveyoung_rankings, "올리브영 카테고리별 베스트 순위(오늘 Top20)"),
    (M.get_oliveyoung_movers, "올리브영 최근 7일 순위 변동 큰 상품 Top20"),
    (M.get_oliveyoung_review_alerts, "올리브영 최근 7일 부정 리뷰 급증 상품 + 주요 키워드"),
    (M.get_oliveyoung_review_compare, "올리브영 리뷰 키워드로 본 경쟁사 vs 셀퓨전씨 강약점"),
    (M.get_oliveyoung_daily_brief, "올리브영 오늘 종합 인사이트 브리핑"),
]:
    _register(_fn, _summary)
