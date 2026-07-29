"""
Slack 봇 — 셀퓨전씨 경쟁 인텔리전스 Q&A (Socket Mode)

@멘션 또는 DM으로 질문 → gpt-4o가 MCP 툴(list_brands/get_brand_intel/…)을
함수호출로 조회 → 셀퓨전씨 관점으로 스트리밍 답변.

구조: slack-bolt(async) + AsyncOpenAI(tool calling) + MCP streamable-http 클라이언트.

환경변수:
  SLACK_BOT_TOKEN=xoxb-...     # Bot Token
  SLACK_APP_TOKEN=xapp-...     # Socket Mode App-Level Token
  OPENAI_API_KEY=sk-...
  MCP_SERVER_URL=https://.../mcp
  MCP_API_KEY=                 # (MCP 서버 Bearer 키. 없으면 생략)
  SLACK_BOT_MODEL=gpt-4o       # (선택)

실행: python slack_bot.py
"""

import asyncio
import json
import logging
import os
import re
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager

from dotenv import load_dotenv

_HERE = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(_HERE, ".env"))

from openai import AsyncOpenAI
from mcp.client.streamable_http import streamablehttp_client
from mcp import ClientSession
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.socket_mode.async_handler import AsyncSocketModeHandler

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger("slack_bot")

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN", "")
MCP_SERVER_URL  = os.getenv("MCP_SERVER_URL", "")
MCP_API_KEY     = os.getenv("MCP_API_KEY", "").strip()
MODEL           = os.getenv("SLACK_BOT_MODEL", "gpt-4o")
STREAM_INTERVAL = 1.0          # Slack rate limit 고려 update 최소 간격(초)
MAX_ROUNDS      = 6            # tool-calling 최대 라운드
HISTORY_TURNS   = 10

SYSTEM_PROMPT = (
    "당신은 씨엠에스랩(더마 선케어 브랜드 '셀퓨전씨' 운영)의 경쟁사 인텔리전스 애널리스트입니다. "
    "K-뷰티 경쟁 브랜드(Anua·조선미녀·Skin1004·달바·VT·Rejuran 등)의 해외 활동 데이터를 MCP 툴로 조회해 "
    "질문에 답합니다.\n\n"
    "원칙:\n"
    "1) 반드시 툴로 실제 데이터를 조회해 사실 기반으로 답하라. 모르면 지어내지 말고 툴을 호출하라.\n"
    "2) 답은 항상 '그래서 셀퓨전씨는?' 관점의 시사점으로 연결하라(우리 선케어·더마·주력시장 베트남/중국/일본/올영 기준).\n"
    "3) 간결하고 뾰족하게. 숫자(건수·모멘텀 배수·채널)를 인용하라. 뻔한 말 금지.\n"
    "4) 슬랙용이므로 강조는 별표 하나 *굵게*, 목록은 • 로. 마크다운 헤더(###)·별표 두 개(**) 쓰지 마라.\n"
    "5) 한국어로 답하라."
)

_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
_tools_cache = None                              # OpenAI tools 포맷 캐시
_history: dict = defaultdict(lambda: deque(maxlen=HISTORY_TURNS * 2))


@asynccontextmanager
async def _mcp_session():
    headers = {"Authorization": f"Bearer {MCP_API_KEY}"} if MCP_API_KEY else None
    async with streamablehttp_client(MCP_SERVER_URL, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            yield session


async def _get_openai_tools(session) -> list:
    """MCP 툴 목록 → OpenAI function 포맷 (프로세스 레벨 캐시)."""
    global _tools_cache
    if _tools_cache is None:
        listed = await session.list_tools()
        _tools_cache = [{
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description or "",
                "parameters": t.inputSchema or {"type": "object", "properties": {}},
            },
        } for t in listed.tools]
        logger.info("MCP 툴 %d개 로드: %s", len(_tools_cache),
                    [t["function"]["name"] for t in _tools_cache])
    return _tools_cache


async def _call_mcp_tool(session, name: str, args: dict) -> str:
    try:
        result = await session.call_tool(name, args)
        if result.content:
            return "\n".join(c.text for c in result.content if hasattr(c, "text"))
        return "(빈 결과)"
    except Exception as e:
        logger.warning("MCP 툴 호출 실패 [%s]: %s", name, e)
        return f"(툴 {name} 호출 오류: {e})"


async def answer(question: str, history: list, on_delta) -> str:
    """gpt-4o tool-calling 루프. on_delta(text): 스트리밍 부분답변 콜백."""
    async with _mcp_session() as session:
        tools = await _get_openai_tools(session)
        messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history,
                    {"role": "user", "content": question}]
        final = ""
        for _round in range(MAX_ROUNDS):
            stream = await _client.chat.completions.create(
                model=MODEL, messages=messages, tools=tools,
                tool_choice="auto", temperature=0.3, stream=True,
            )
            content = ""
            tool_bufs: dict = {}
            async for chunk in stream:
                if not chunk.choices:
                    continue
                delta = chunk.choices[0].delta
                if delta.content:
                    content += delta.content
                    on_delta(content)
                for tc in (delta.tool_calls or []):
                    buf = tool_bufs.setdefault(tc.index, {"id": "", "name": "", "args": ""})
                    if tc.id:
                        buf["id"] = tc.id
                    if tc.function and tc.function.name:
                        buf["name"] = tc.function.name
                    if tc.function and tc.function.arguments:
                        buf["args"] += tc.function.arguments

            if not tool_bufs:
                final = content
                break

            # 어시스턴트의 tool_call 요청을 대화에 추가
            messages.append({
                "role": "assistant",
                "content": content or None,
                "tool_calls": [{
                    "id": b["id"], "type": "function",
                    "function": {"name": b["name"], "arguments": b["args"] or "{}"},
                } for _, b in sorted(tool_bufs.items())],
            })
            # 각 툴 실행 → 결과 추가
            for _, b in sorted(tool_bufs.items()):
                try:
                    args = json.loads(b["args"]) if b["args"].strip() else {}
                except Exception:
                    args = {}
                logger.info("툴 호출: %s(%s)", b["name"], args)
                out = await _call_mcp_tool(session, b["name"], args)
                messages.append({"role": "tool", "tool_call_id": b["id"], "content": out})
        return final or "죄송해요, 답변을 만들지 못했어요. 질문을 조금 더 구체적으로 주실래요?"


app = AsyncApp(token=SLACK_BOT_TOKEN)
_BOT_MENTION = re.compile(r"<@[A-Z0-9]+>")


async def _handle(event: dict, client, in_thread: bool):
    user = event.get("user", "?")
    channel = event["channel"]
    text = _BOT_MENTION.sub("", event.get("text", "")).strip()
    thread_ts = event.get("thread_ts") or (event["ts"] if in_thread else None)
    if not text:
        await client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                      text="무엇을 물어볼까요? 예) `아누아 최근 미국 동향`, `베트남 시장 경쟁 상황`, `앰플 카테고리 압박`")
        return

    ph = await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text="🔎 조회 중…")
    ts = ph["ts"]
    state = {"last": 0.0, "text": ""}

    def on_delta(cur: str):
        state["text"] = cur
        now = time.monotonic()
        if now - state["last"] >= STREAM_INTERVAL:
            state["last"] = now
            asyncio.create_task(_safe_update(client, channel, ts, cur + " ▌"))

    hist = list(_history[user])
    try:
        result = await answer(text, hist, on_delta)
    except Exception as e:
        logger.exception("답변 생성 오류")
        result = f"⚠️ 처리 중 오류가 났어요: {e}"

    await _safe_update(client, channel, ts, result)
    _history[user].append({"role": "user", "content": text})
    _history[user].append({"role": "assistant", "content": result})


async def _safe_update(client, channel, ts, text):
    try:
        await client.chat_update(channel=channel, ts=ts, text=text[:3900])
    except Exception as e:
        logger.debug("chat_update 실패: %s", e)


@app.event("app_mention")
async def on_mention(event, client):
    if event.get("bot_id"):
        return
    await _handle(event, client, in_thread=True)


@app.event("message")
async def on_message(event, client):
    # DM만 처리 (채널 일반 메시지는 멘션으로만). 봇 자신·수정 이벤트 무시.
    if event.get("bot_id") or event.get("subtype"):
        return
    if event.get("channel_type") == "im":
        await _handle(event, client, in_thread=False)


async def _main():
    missing = [k for k, v in {
        "SLACK_BOT_TOKEN": SLACK_BOT_TOKEN, "SLACK_APP_TOKEN": SLACK_APP_TOKEN,
        "MCP_SERVER_URL": MCP_SERVER_URL,
    }.items() if not v]
    if missing:
        raise SystemExit(f"환경변수 누락: {', '.join(missing)}")
    logger.info("Slack 봇 시작 (model=%s, mcp=%s)", MODEL, MCP_SERVER_URL)
    handler = AsyncSocketModeHandler(app, SLACK_APP_TOKEN)
    await handler.start_async()


if __name__ == "__main__":
    asyncio.run(_main())
