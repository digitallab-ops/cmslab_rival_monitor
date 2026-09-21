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

from sqlalchemy import text as _sqltext
from storage.models import get_session
from config.settings import DB_SCHEMA

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
logger = logging.getLogger("slack_bot")

SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN", "")
MCP_SERVER_URL  = os.getenv("MCP_SERVER_URL", "")
MCP_API_KEY     = os.getenv("MCP_API_KEY", "").strip()
MODEL           = os.getenv("SLACK_BOT_MODEL", "gpt-4o-mini")
STREAM_INTERVAL = 1.0          # Slack rate limit 고려 update 최소 간격(초)
MAX_ROUNDS      = 6            # tool-calling 최대 라운드
HISTORY_TURNS   = 10

SYSTEM_PROMPT = (
    "당신은 씨엠에스랩(더마 선케어 브랜드 '셀퓨전씨' 운영)의 경쟁사 인텔리전스 애널리스트입니다. "
    "K-뷰티 경쟁 브랜드(Anua·조선미녀·Skin1004·달바·VT·Rejuran 등)의 해외 활동 데이터를 MCP 툴로 조회해 "
    "질문에 답합니다.\n\n"
    "원칙:\n"
    "1) 반드시 툴로 실제 데이터를 조회해 사실 기반으로 답하라. 모르면 지어내지 말고 툴을 호출하라.\n"
    "2) 답 끝에 '그래서 셀퓨전씨는?' 시사점을 연결하되, 억지로 붙이지 말고 관련 있을 때만. "
    "'주의 깊게 살펴볼 필요가 있다' 같은 관용구·뻔한 말 반복 금지 — 구체적·상황특정 제안만.\n"
    "3) 간결하고 뾰족하게. 숫자(건수·모멘텀 배수·채널)를 인용하라.\n"
    "4) **근거 표기**: 기사 데이터를 근거로 썼으면 답 하단에 '📎 출처'로 근거 기사 제목과 "
    "URL을 1~3개 붙여라. 데이터 없이 답하지 마라.\n"
    "   ⚠️ URL은 **툴 결과에 실제로 있던 url/source_url만** 그대로 복사하라. "
    "URL을 지어내는 것은 절대 금지(example.com 같은 가짜·추정 링크 금지). "
    "툴 결과에 URL이 없으면 '📎 출처' 줄 자체를 쓰지 마라.\n"
    "5) 슬랙용이므로 강조는 별표 하나 *굵게*, 목록은 • 로. 마크다운 헤더(###)·별표 두 개(**)·"
    "마크다운 링크([텍스트](url)) 쓰지 마라. 링크는 <url|텍스트> 형식.\n"
    "6) 한국어로 답하라."
)

_client = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
_tools_cache = None                              # OpenAI tools 포맷 캐시
_history: dict = defaultdict(lambda: deque(maxlen=HISTORY_TURNS * 2))


class _InProcMCP:
    """인프로세스 MCP 어댑터 — HTTP를 타지 않고 툴 함수를 직접 호출.

    MCP_SERVER_URL이 '우리 자신'을 가리키면(웹 서버가 자기 /mcp를 HTTP로 호출) 워커가
    1개인 배포에서 자기 요청을 자기가 기다리는 교착이 생긴다(실측: /health 0.4초인데
    /api/ask는 60초+ 무응답, MCP를 외부에서 직접 부르면 0.3초 정상).
    같은 프로세스 안에 이미 FastMCP 객체가 있으므로 그대로 호출하면 교착도, 왕복 비용도 없다.
    """

    def __init__(self, mcp):
        self._mcp = mcp

    async def list_tools(self):
        tools = await self._mcp.list_tools()
        return type("R", (), {"tools": tools})()

    async def call_tool(self, name, args):
        res = await self._mcp.call_tool(name, args or {})
        content = res[0] if isinstance(res, tuple) else res
        return type("R", (), {"content": content})()


def _self_hosted_mcp() -> bool:
    """MCP_SERVER_URL이 이 서비스 자신인지(=자기 호출이라 인프로세스로 우회해야 하는지).

    server.py가 기동 시 MCP_SERVER_URL을 http://127.0.0.1:{PORT}/mcp로 강제하므로
    루프백 주소가 곧 '자기 자신'이다. 공개 URL로 설정된 경우도 함께 본다.
    """
    if os.getenv("MCP_INPROC", "").strip() == "1":
        return True
    url = (os.getenv("MCP_SERVER_URL") or MCP_SERVER_URL or "").strip()
    if not url:
        return False
    host = url.replace("https://", "").replace("http://", "").split("/")[0].lower()
    hostname = host.split(":")[0]
    if hostname in ("127.0.0.1", "localhost", "0.0.0.0", "::1"):
        return True
    own = (os.getenv("RENDER_EXTERNAL_URL") or "").strip()
    if own:
        own_host = own.replace("https://", "").replace("http://", "").split("/")[0].lower()
        return own_host.split(":")[0] == hostname
    return False


@asynccontextmanager
async def _mcp_session():
    if _self_hosted_mcp():
        try:
            from mcp_server import rival_mcp
            yield _InProcMCP(rival_mcp)
            return
        except Exception as e:      # 임포트 실패 시 기존 HTTP 경로로 폴백
            logger.warning("인프로세스 MCP 실패 → HTTP 폴백: %s", e)
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


_FAKE_URL = re.compile(r"https?://(?:www\.)?(?:example\.(?:com|org)|test\.com|localhost|foo\.bar)\S*", re.I)


def _slackify(t: str) -> str:
    """LLM 출력 → 슬랙 mrkdwn 교정(프롬프트만으론 모델이 어김).
    ** 볼드 → *, 마크다운 링크 → <url|text>, ### 헤더 → *헤더*, 지어낸 URL 제거."""
    if not t:
        return t
    # 지어낸 예시 URL이 든 링크/각주는 통째로 제거(가짜 출처 방지)
    t = re.sub(r"\[([^\]]+)\]\(\s*" + _FAKE_URL.pattern + r"\s*\)", r"\1", t)
    t = _FAKE_URL.sub("", t)
    t = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", r"<\2|\1>", t)   # [텍스트](url) → <url|텍스트>
    t = re.sub(r"\*\*(.+?)\*\*", r"*\1*", t, flags=re.S)                # **볼드** → *볼드*
    t = re.sub(r"^\s*#{1,6}\s*(.+)$", r"*\1*", t, flags=re.M)           # ### 헤더 → *헤더*
    # 실제 URL이 남지 않은 '📎 출처' 줄은 통째로 제거(가짜 출처 흔적 방지)
    t = "\n".join(ln for ln in t.split("\n")
                  if not (re.match(r"\s*📎\s*출처", ln) and "<http" not in ln))
    return re.sub(r"\n{3,}", "\n\n", t).strip()


async def answer(question: str, history: list, on_delta, memory: dict | None = None) -> str:
    """gpt-4o tool-calling 루프. on_delta(text): 스트리밍 부분답변 콜백.
    memory: {키:값} 이 사용자에 대해 기억하는 지속 사실 → 시스템 프롬프트에 주입(개인화)."""
    sys_prompt = SYSTEM_PROMPT
    if memory:
        mem_txt = "\n".join(f"- {k}: {v}" for k, v in list(memory.items())[:12])
        sys_prompt += (
            "\n\n[이 사용자에 대해 기억하는 것 — 답변을 이 맥락에 맞춰 개인화하라]\n" + mem_txt +
            "\n(기억이 질문과 무관하면 무시. 기억 내용을 굳이 되뇌지 말고 자연스럽게 반영만 하라.)")
    async with _mcp_session() as session:
        tools = await _get_openai_tools(session)
        messages = [{"role": "system", "content": sys_prompt}, *history,
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
        return _slackify(final) or "죄송해요, 답변을 만들지 못했어요. 질문을 조금 더 구체적으로 주실래요?"


app = AsyncApp(token=SLACK_BOT_TOKEN)
_BOT_MENTION = re.compile(r"<@[A-Z0-9]+>")


_APPROVE_VERBS = ("추가", "승인", "등록")
_REJECT_VERBS = ("제외", "거절", "무시")
# 브랜드 등록/제외(쓰기) 허용 사용자 — 슬랙 user ID 콤마구분. 비면 아무도 못 함(조회는 누구나).
_BRAND_ADMINS = {x.strip() for x in os.getenv("SLACK_BRAND_ADMINS", "").split(",") if x.strip()}


def _pending_candidates():
    s = get_session()
    try:
        return s.execute(_sqltext(
            f"SELECT name, ko_name, mention_count FROM {DB_SCHEMA}.brand_candidates "
            f"WHERE status='pending' ORDER BY mention_count DESC, proposed_at DESC LIMIT 20")).fetchall()
    finally:
        s.close()


def _brand_command(text: str, user_id: str = ""):
    """신흥 브랜드 후보 명령. 명령이면 응답 문자열, 아니면 None(→ 일반 Q&A로).
    조회(후보)는 누구나, 등록/제외(쓰기)는 SLACK_BRAND_ADMINS만."""
    t = (text or "").strip()
    # 정확히 '내 아이디'만 받다 보니 '내 ID', '내 슬랙 id 뭐냐고'가 LLM으로 새서
    # "개인 정보는 확인할 수 없습니다"라고 거절당했다. 표현 변주를 폭넓게 받는다.
    _t = re.sub(r"[?？!！.\s]+", "", t.lower())
    if re.fullmatch(r"(내|나의|제)?(슬랙|slack)?(아이디|id)(뭐야|뭐냐고|뭐임|알려줘|좀|은|는)*", _t) \
            or _t in ("myid", "myslackid", "whatsmyid"):
        return (f"당신의 Slack ID: `{user_id}`\n"
                f"_알림을 받으려면 이 ID를 `SLACK_MENTION_IDS`에, "
                f"브랜드 승인 권한이 필요하면 `SLACK_BRAND_ADMINS`에 넣으세요._")

    def _can_write():
        return bool(_BRAND_ADMINS) and user_id in _BRAND_ADMINS

    def _denied():
        if not _BRAND_ADMINS:
            return ("🔒 브랜드 등록 권한자가 아직 설정되지 않았어요. 서버 환경변수 "
                    "`SLACK_BRAND_ADMINS`에 관리자 Slack ID를 넣어주세요. "
                    f"(당신 ID: `{user_id}` — `내 아이디`로도 확인)")
        return f"🔒 브랜드 등록/제외는 관리자만 가능해요. (당신 ID: `{user_id}`)"

    if t in ("후보", "브랜드 후보", "후보 목록"):
        try:
            rows = _pending_candidates()
        except Exception as e:
            return f"⚠️ 후보 조회 실패: {e}"
        if not rows:
            return "대기 중인 신흥 브랜드 후보가 없어요."
        out = ["*신흥 브랜드 후보(대기)* — `승인 <브랜드>`로 등록 · `제외 <브랜드>`로 무시"]
        for n, ko, c in rows:
            lbl = n + (f" ({ko})" if ko and ko != n else "")
            out.append(f"• {lbl} — 언급 {c}건")
        return "\n".join(out)
    # 짧은 명령만 인식(일반 질문 오탐 방지): "승인 아뮤즈" / "아뮤즈 승인" / "브랜드 승인 아뮤즈" / "승인"
    toks = [x for x in t.split() if x != "브랜드"]
    if not (1 <= len(toks) <= 3):
        return None
    kind, vpos = None, None
    for i, tok in enumerate(toks):
        if tok in _APPROVE_VERBS:
            kind, vpos = "approve", i; break
        if tok in _REJECT_VERBS:
            kind, vpos = "reject", i; break
    if not kind:
        return None
    if not _can_write():
        return _denied()
    name = " ".join(tok for j, tok in enumerate(toks) if j != vpos).strip()
    if name:
        return _apply_brand(name, kind)
    # 브랜드명 없이 동사만 → 대기 후보가 딱 1개면 그걸로
    try:
        pend = _pending_candidates()
    except Exception as e:
        return f"⚠️ 후보 조회 실패: {e}"
    if not pend:
        return "대기 중인 후보가 없어요."
    if len(pend) == 1:
        return _apply_brand(pend[0][0], kind)
    names = ", ".join(p[0] for p in pend)
    return f"후보가 여러 개예요 — 브랜드명을 붙여주세요. 예) `{toks[vpos]} {pend[0][0]}`\n대기: {names}"


def _apply_brand(name: str, kind: str) -> str:
    s = get_session()
    try:
        row = s.execute(_sqltext(
            f"SELECT name, ko_name FROM {DB_SCHEMA}.brand_candidates "
            f"WHERE lower(name)=lower(:n) OR ko_name=:n LIMIT 1"), {"n": name}).fetchone()
        cand_name = row[0] if row else name
        cand_ko = row[1] if row else None
        if kind == "reject":
            s.execute(_sqltext(
                f"UPDATE {DB_SCHEMA}.brand_candidates SET status='rejected' "
                f"WHERE lower(name)=lower(:n) OR ko_name=:n"), {"n": name})
            s.commit()
            return f"🚫 *{cand_name}* 제외했어요. 다시 제안하지 않습니다."
        ko_arr = [cand_ko] if cand_ko else None
        s.execute(_sqltext(f"""
            INSERT INTO {DB_SCHEMA}.monitored_brands (name, tier, ko_names, is_active)
            VALUES (:n, 2, :ko, TRUE)
            ON CONFLICT (name) DO UPDATE SET is_active=TRUE,
                tier=LEAST({DB_SCHEMA}.monitored_brands.tier, 2)
        """), {"n": cand_name, "ko": ko_arr})
        s.execute(_sqltext(
            f"UPDATE {DB_SCHEMA}.brand_candidates SET status='approved' "
            f"WHERE lower(name)=lower(:n) OR ko_name=:n"), {"n": name})
        s.commit()
        return (f"✅ *{cand_name}* 모니터링에 등록했어요(Tier2·주간). "
                f"다음 수집 주기부터 뉴스·신호가 쌓입니다.")
    except Exception as e:
        s.rollback()
        return f"⚠️ 처리 실패: {e}"
    finally:
        s.close()


def _mapping_command(text: str, user_id: str = ""):
    """파수꾼 드리프트 매핑 승인 명령('매핑'으로 시작). 명령이면 응답, 아니면 None.
    조회는 누구나, 승인/제외(쓰기)는 SLACK_BRAND_ADMINS만."""
    t = (text or "").strip()
    if not t.startswith("매핑"):
        return None
    rest = t[2:].strip()   # '매핑' 뒤
    from storage.repository import (list_pending_mappings, approve_mapping,
                                    approve_all_pending, reject_mapping)

    def _can_write():
        return bool(_BRAND_ADMINS) and user_id in _BRAND_ADMINS

    def _fmt_pending(s):
        rows = list_pending_mappings(s)
        if not rows:
            return "대기 중인 매핑 제안이 없어요."
        out = ["*미매핑 제안(대기)* — `매핑 승인`(전체 반영) · `매핑 <코드>=<값>`(개별) · `매핑 제외 <코드>`"]
        for kind, code, sug in rows:
            out.append(f"• [{kind}] `{code}` → 제안: {sug or '(없음)'}")
        return "\n".join(out)

    s = get_session()
    try:
        # 조회
        if rest in ("", "목록", "리스트", "확인"):
            return _fmt_pending(s)
        toks = rest.split()
        # 제외
        if toks and toks[0] in _REJECT_VERBS:
            if not _can_write():
                return f"🔒 매핑 승인/제외는 관리자만 가능해요. (당신 ID: `{user_id}`)"
            if len(toks) < 2:
                return "제외할 코드를 붙여주세요. 예) `매핑 제외 HK`"
            n = reject_mapping(s, toks[1].upper())
            return f"🚫 `{toks[1].upper()}` 제안 제외({n}건). 다시 제안하지 않아요." if n else "해당 코드 대기 제안이 없어요."
        # 승인(전체)
        if rest in ("승인", "전체 승인", "모두 승인") or toks[:1] == ["승인"] and len(toks) == 1:
            if not _can_write():
                return f"🔒 매핑 승인은 관리자만 가능해요. (당신 ID: `{user_id}`)"
            n = approve_all_pending(s)
            return f"✅ 대기 제안 {n}건 반영했어요. 다음 대시보드 갱신부터 한국어로 표시됩니다." if n else "반영할(제안값 있는) 대기 항목이 없어요."
        # 개별: `매핑 HK=홍콩` 또는 `매핑 승인 HK` 또는 `매핑 HK`
        if not _can_write():
            return f"🔒 매핑 승인은 관리자만 가능해요. (당신 ID: `{user_id}`)"
        target = rest
        if toks and toks[0] in _APPROVE_VERBS:
            target = rest[len(toks[0]):].strip()
        val = None
        code = target
        if "=" in target:
            code, val = [x.strip() for x in target.split("=", 1)]
        code = code.upper()
        if not code:
            return "코드를 지정해주세요. 예) `매핑 HK=홍콩` 또는 `매핑 승인 HK`"
        n = approve_mapping(s, code, val)
        if n:
            return f"✅ `{code}` → {val or '제안값'} 반영({n}건). 다음 갱신부터 표시돼요."
        return f"`{code}` 대기 제안이 없어요. `매핑`으로 목록 확인하세요."
    except Exception as e:
        return f"⚠️ 매핑 처리 실패: {e}"
    finally:
        s.close()


def _memory_command(text: str, user_id: str = ""):
    """사용자 기억 제어 — `기억`(조회) / `기억해 <내용>`(추가) / `잊어`·`기억 삭제 <키>`(삭제).
    본인 기억만 다루므로 관리자 권한 불필요. 명령이면 응답 문자열, 아니면 None."""
    t = (text or "").strip()
    if not (t.startswith("기억") or t.startswith("잊어")):
        return None
    from storage.repository import (get_user_memory, upsert_user_memory, delete_user_memory)
    s = get_session()
    try:
        # 삭제: "잊어" / "잊어 일본" / "기억 삭제 일본"
        if t.startswith("잊어") or t.startswith("기억 삭제") or t.startswith("기억삭제"):
            key = t.replace("기억 삭제", "").replace("기억삭제", "").replace("잊어", "").strip()
            n = delete_user_memory(s, user_id, key or None)
            if not n:
                return "지울 기억이 없어요."
            return (f"🧹 기억 {n}건 지웠어요." if key else f"🧹 당신에 대한 기억 {n}건 전부 지웠어요.")
        # 추가: "기억해 나는 일본 담당이야"
        if t.startswith("기억해"):
            val = t[3:].strip(" :·,")
            if not val:
                return "무엇을 기억할까요? 예) `기억해 나는 일본 시장 담당`"
            key = ("직접 입력 " + val[:20])
            upsert_user_memory(s, user_id, key, val, source="user")
            return f"🧠 기억했어요 — _{val}_\n(`기억`으로 확인, `잊어`로 삭제)"
        # 조회: "기억"
        mem = get_user_memory(s, user_id)
        if not mem:
            return ("아직 당신에 대해 기억한 게 없어요. 대화를 나누면 관심 브랜드·시장 같은 걸 "
                    "자동으로 기억합니다.\n직접 알려주려면 `기억해 나는 일본 시장 담당`")
        lines = ["*🧠 당신에 대해 기억하는 것* — `기억해 <내용>`로 추가 · `잊어`로 전체 삭제"]
        lines += [f"• {v}" for v in mem.values()]
        return "\n".join(lines)
    except Exception as e:
        return f"⚠️ 기억 처리 실패: {e}"
    finally:
        s.close()


_MEM_EXTRACT_PROMPT = (
    "아래는 사내 경쟁 인텔리전스 봇과 한 직원의 대화다. 이 '사용자'에 대해 앞으로도 계속 유효할 "
    "지속적 사실만 뽑아라(담당 시장·관심 브랜드/카테고리·업무 역할·선호하는 답변 형식 등).\n"
    "- 일회성 질문 내용, 봇이 답한 데이터는 기억이 아니다. 뽑지 마라.\n"
    "- 확실하지 않으면 뽑지 마라. 없으면 빈 배열.\n"
    "- 각 항목은 key(짧은 라벨)와 value(한 줄 사실). 최대 2개.\n"
    '반드시 JSON: {"facts":[{"key":"담당 시장","value":"일본 시장을 담당한다"}]}'
)


def _extract_memory(user_id: str, question: str, reply: str) -> None:
    """대화에서 지속될 사실을 추출해 저장(백그라운드·실패 무해)."""
    try:
        import json as _json
        from openai import OpenAI
        from storage.repository import get_user_memory, upsert_user_memory
        s = get_session()
        try:
            known = get_user_memory(s, user_id)
            known_txt = "\n".join(f"- {k}: {v}" for k, v in known.items()) or "(없음)"
            cli = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
            resp = cli.chat.completions.create(
                model="gpt-4o-mini", max_tokens=200, temperature=0,
                response_format={"type": "json_object"},
                messages=[{"role": "user", "content":
                           f"{_MEM_EXTRACT_PROMPT}\n\n이미 아는 것:\n{known_txt}\n\n"
                           f"[사용자] {question[:600]}\n[봇] {reply[:600]}"}])
            facts = (_json.loads(resp.choices[0].message.content or "{}").get("facts") or [])[:2]
            for f in facts:
                k, v = (f.get("key") or "").strip(), (f.get("value") or "").strip()
                if k and v and v not in known.values():
                    upsert_user_memory(s, user_id, k, v, source="auto")
                    logger.info("사용자 기억 저장 [%s] %s=%s", user_id, k, v)
        finally:
            s.close()
    except Exception as e:
        logger.debug("기억 추출 스킵: %s", e)


async def _handle(event: dict, client, in_thread: bool):
    user = event.get("user", "?")
    channel = event["channel"]
    text = _BOT_MENTION.sub("", event.get("text", "")).strip()
    thread_ts = event.get("thread_ts") or (event["ts"] if in_thread else None)
    if not text:
        await client.chat_postMessage(channel=channel, thread_ts=thread_ts,
                                      text="무엇을 물어볼까요? 예) `아누아 최근 미국 동향`, `베트남 시장 경쟁 상황`, `앰플 카테고리 압박`\n브랜드 관리: `후보` · `추가 <브랜드>` · `제외 <브랜드>`")
        return

    _memcmd = _memory_command(text, user)
    if _memcmd is not None:
        await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=_memcmd)
        return

    _mcmd = _mapping_command(text, user)
    if _mcmd is not None:
        await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=_mcmd)
        return

    _cmd = _brand_command(text, user)
    if _cmd is not None:
        await client.chat_postMessage(channel=channel, thread_ts=thread_ts, text=_cmd)
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

    # 대화 맥락·사용자 기억을 DB에서(재시작해도 이어짐). 실패 시 인메모리 폴백.
    hist, mem = list(_history[user]), {}
    try:
        from storage.repository import load_bot_history, get_user_memory
        _s = get_session()
        try:
            db_hist = load_bot_history(_s, user, turns=HISTORY_TURNS)
            if db_hist:
                hist = db_hist
            mem = get_user_memory(_s, user)
        finally:
            _s.close()
    except Exception as e:
        logger.debug("대화/기억 로드 스킵(인메모리 사용): %s", e)

    try:
        result = await answer(text, hist, on_delta, memory=mem)
    except Exception as e:
        logger.exception("답변 생성 오류")
        result = f"⚠️ 처리 중 오류가 났어요: {e}"

    await _safe_update(client, channel, ts, result)
    _history[user].append({"role": "user", "content": text})
    _history[user].append({"role": "assistant", "content": result})
    # 영속 저장 + 지속 사실 추출(응답 후 백그라운드 — 사용자 대기 없음)
    try:
        from storage.repository import save_bot_turn
        _s = get_session()
        try:
            save_bot_turn(_s, user, "user", text)
            save_bot_turn(_s, user, "assistant", result)
        finally:
            _s.close()
    except Exception as e:
        logger.debug("대화 저장 스킵: %s", e)
    if not result.startswith("⚠️"):
        asyncio.get_running_loop().run_in_executor(None, _extract_memory, user, text, result)


async def _safe_update(client, channel, ts, text):
    try:
        await client.chat_update(channel=channel, ts=ts, text=text[:3900])
    except Exception as e:
        logger.debug("chat_update 실패: %s", e)


@app.event("app_home_opened")
async def on_home(event, client):
    """App Home 탭 — 앱 설명 + 대시보드 바로가기 버튼 + 사용법."""
    url = os.getenv("RENDER_EXTERNAL_URL") or "https://cmslab-rival-monitor.onrender.com"
    try:
        await client.views_publish(user_id=event["user"], view={
            "type": "home",
            "blocks": [
                {"type": "header", "text": {"type": "plain_text", "text": "🛰️  CELLFUSION INTEL"}},
                {"type": "section", "text": {"type": "mrkdwn", "text":
                    "K-뷰티 경쟁 브랜드 *21곳 · 27개국*을 매일 대신 지켜봅니다.\n"
                    "'발표'와 '실제 성과'를 가려서 — *지금 알아야 할 것만* 추려드려요."}},
                {"type": "actions", "elements": [
                    {"type": "button", "text": {"type": "plain_text", "text": "📊  대시보드 열기"},
                     "url": url, "style": "primary"}]},
                {"type": "divider"},
                {"type": "section", "text": {"type": "mrkdwn", "text": "*🔎  이런 걸 볼 수 있어요*"}},
                {"type": "section", "fields": [
                    {"type": "mrkdwn", "text": "*📌 이번 주 핵심*\n지금 대응할 것 · 선점 기회 · 점검할 것"},
                    {"type": "mrkdwn", "text": "*🏆 브랜드 스코어*\n21개 브랜드, 누가 뜨고 지나 순위로"},
                    {"type": "mrkdwn", "text": "*🌍 신호 지도*\n어느 나라에서 어떤 경쟁사가 움직이나"},
                    {"type": "mrkdwn", "text": "*🔥 뜨는 시장*\n실제 수출이 크는 나라 + 그 이유"},
                    {"type": "mrkdwn", "text": "*🪧 진출 임박*\n경쟁사 해외 상표 = 다음 진출지 예고"},
                    {"type": "mrkdwn", "text": "*🔍 진짜 vs 홍보*\n발표가 검색·수출로 이어지는지 검증"},
                ]},
                {"type": "divider"},
                {"type": "section", "text": {"type": "mrkdwn", "text":
                    "*💬  이렇게 물어보세요* — 멘션하거나 DM으로"}},
                {"type": "section", "fields": [
                    {"type": "mrkdwn", "text": "`아누아 요즘 어때?`"},
                    {"type": "mrkdwn", "text": "`미국에서 뜨는 브랜드는?`"},
                    {"type": "mrkdwn", "text": "`브랜드 종합 스코어 top5`"},
                    {"type": "mrkdwn", "text": "`진출 임박 있어?`"},
                    {"type": "mrkdwn", "text": "`폴란드 수출 왜 늘어?`"},
                    {"type": "mrkdwn", "text": "`조선미녀 최근 무브`"},
                ]},
                {"type": "divider"},
                {"type": "context", "elements": [{"type": "mrkdwn", "text":
                    "🗓  데일리 브리핑 매일 08:00  ·  위클리 심층 매주 월 08:00 (KST)"
                    "      ·      씨엠에스랩 디지털랩"}]},
            ],
        })
    except Exception as e:
        logger.debug("app_home 발행 실패(App Home 탭 활성화 필요할 수 있음): %s", e)


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
