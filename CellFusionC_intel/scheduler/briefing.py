"""
브리핑 자동 생성 (GPT + Supabase DB)

- 주간(월 08:00 KST): 최근 7일 심층 분석 → gpt-4o
- 일간(매일 08:00 KST): 전날 수집분 요약 → gpt-4o-mini
- Slack 전송
"""

import logging
from datetime import datetime, timedelta

from openai import OpenAI

from config.settings import OPENAI_API_KEY, DB_SCHEMA
from config.brands import REGION_MAP
from notifications.slack import send_weekly_briefing, send_daily_briefing
from storage.models import get_session, save_briefing
from sqlalchemy import text

logger = logging.getLogger(__name__)

# 대표 기사만(의미 중복 제외).
_DUP_FILTER = "AND is_duplicate IS NOT TRUE"


def _fetch_rows(session, hours: int) -> list:
    """최근 N시간 수집 기사 (스코어순, incidental·중복 제외)."""
    since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    return session.execute(text(f"""
        SELECT brand, country, activity_type, importance,
               details, product_name, source_url, collected_at,
               COALESCE(strategic_score, 0) AS score, channel, evidence_level
        FROM {DB_SCHEMA}.news_articles
        WHERE collected_at >= :since
          AND (brand_focus != 'incidental' OR brand_focus IS NULL)
          {_DUP_FILTER}
        ORDER BY COALESCE(strategic_score,0) DESC, importance DESC, collected_at DESC
    """), {"since": since}).fetchall()


def _stats(session, hours: int) -> dict:
    since = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    r = session.execute(text(f"""
        SELECT COUNT(*), COUNT(*) FILTER (WHERE importance='high'),
               COUNT(DISTINCT brand), COUNT(DISTINCT country)
        FROM {DB_SCHEMA}.news_articles
        WHERE collected_at >= :since {_DUP_FILTER}
    """), {"since": since}).fetchone()
    return {"total": r[0], "high": r[1], "brands": r[2], "countries": r[3]}


def _fmt_line(r, detail_len: int) -> str:
    # r: brand0 country1 activity2 importance3 details4 product5 url6 collected7 score8 channel9 evidence10
    ch = f" 채널:{r[9]}" if r[9] else ""
    ev = f" 근거:{r[10]}" if r[10] else ""
    pr = f" 제품:{r[5]}" if r[5] else ""
    return (f"[score {r[8]}][{str(r[3]).upper()}] {r[0]}/{r[1]} - {r[2]}{pr}{ch}{ev}: "
            f"{(r[4] or '')[:detail_len]}")


def _build_prompt_by_region(rows, limit: int, detail_len: int) -> str:
    """권역별로 묶은 데이터 프롬프트."""
    if not rows:
        return "수집된 기사가 없습니다."
    buckets: dict = {}
    for r in rows[:limit]:
        region = REGION_MAP.get((r[1] or "").upper(), "기타")
        buckets.setdefault(region, []).append(r)
    order = ["KR", "APAC", "SEA", "NA", "EU", "ME", "LATAM", "AF", "IN", "기타"]
    lines = []
    for reg in sorted(buckets, key=lambda x: order.index(x) if x in order else 99):
        lines.append(f"\n=== [{reg}] ===")
        for r in buckets[reg]:
            lines.append(_fmt_line(r, detail_len))
    return "\n".join(lines)


def _save(kind: str, content: str, stats: dict, hours: int, model: str) -> None:
    """생성된 브리핑을 DB에 보관 (실패해도 발송엔 지장 없음)."""
    if not content or content.startswith("브리핑 생성 오류"):
        return
    now = datetime.utcnow()
    session = get_session()
    try:
        save_briefing(session, kind=kind, content=content, stats=stats,
                      period_from=now - timedelta(hours=hours), period_to=now, model=model)
    except Exception as e:
        logger.warning("브리핑 DB 저장 실패(%s): %s", kind, e)
    finally:
        session.close()


def _openai(model: str, system: str, user: str, max_tokens: int) -> str:
    client = OpenAI(api_key=OPENAI_API_KEY)
    resp = client.chat.completions.create(
        model=model,
        messages=[{"role": "system", "content": system},
                  {"role": "user", "content": user}],
        max_tokens=max_tokens,
        temperature=0.3,
    )
    return (resp.choices[0].message.content or "").strip()


def _cms_profile() -> str:
    try:
        from analytics.summarizer import CMS_PROFILE
        return CMS_PROFILE
    except Exception:
        return "우리=씨엠에스랩/셀퓨전씨(더마 선케어 스페셜리스트)."


def _signal_digest(session, weekly: bool = True) -> str:
    """5축 신호(종합스코어·삼각검증·상표·검색급등·수출)를 Slack 포맷으로 결정적 생성.

    뉴스 LLM 서술 뒤에 붙는 '하드 데이터' 섹션 — 환각 없이 정확한 수치/랭킹.
    weekly=False면 실무 액션 위주로 압축(진출 선행·검색 급등·스코어 top3).
    """
    from analytics.queries import (
        get_brand_composite_score, get_demand_triangulation,
        get_trademark_signals, get_google_spikes, get_market_growth_story,
        get_ingredient_trends, get_negative_signals, get_opportunity_stories,
    )
    L: list[str] = []

    # 핵심 서사 '기회 스토리' — 나라·브랜드·무브·제품·성과 (결정적, 브리핑 최상단)
    try:
        stories = get_opportunity_stories(session, days=(30 if weekly else 14), limit=5)
    except Exception:
        stories = []
    if stories:
        L.append("### 🎯 기회 스토리 — 어느 나라·브랜드·무브·성과")
        for s in stories[:5]:
            mv = s.get("move", {})
            perf = s.get("perf", {})
            pf = []
            if perf.get("search_spike"): pf.append(f"검색{perf['search_spike']}배")
            if perf.get("export_yoy") is not None: pf.append(f"수출{perf['export_yoy']:+.0f}%")
            if perf.get("momentum"): pf.append(f"모멘텀{perf['momentum']}")
            prod = (s.get("products") or [""])[0]
            ings = ", ".join(s.get("ingredients", [])[:3])
            tail = " · ".join([x for x in [prod, ings, " ".join(pf)] if x])
            L.append(f"- *{s.get('country_name','')}* {s.get('brand','')} — {mv.get('activity_type','')}"
                     + (f" · {tail}" if tail else ""))

    # 핵심 무브 (원문 링크) — 브리핑에서 바로 원문으로 클릭 연결
    try:
        since = (datetime.utcnow() - timedelta(hours=(24 * 7 if weekly else 28))).isoformat()
        mv = session.execute(text(f"""
            SELECT brand, country, activity_type, title_ko, title, source_url,
                   COALESCE(strategic_score,0) sc
            FROM {DB_SCHEMA}.news_articles
            WHERE collected_at >= :since AND importance='high'
              AND (brand_focus != 'incidental' OR brand_focus IS NULL) {_DUP_FILTER}
            ORDER BY COALESCE(strategic_score,0) DESC
        """), {"since": since}).fetchall()
    except Exception:
        mv = []
    seen, mv_lines = set(), []
    for r in mv:
        k = (r[0], r[1], r[2])
        if k in seen:
            continue
        seen.add(k)
        title = (r[3] or r[4] or "")[:60]
        url = r[5] or ""
        link = f" <{url}|원문 ↗>" if url.startswith("http") else ""
        mv_lines.append(f"- *{r[0]}* ({r[1]}) {r[2]} · {title}{link}")
        if len(mv_lines) >= (6 if weekly else 4):
            break
    if mv_lines:
        L.append("### 🔴 핵심 무브 (원문 링크)")
        L += mv_lines

    # 진출 선행신호 — 최근 해외 상표 출원 (가장 액션어블 → 항상 최상단)
    try:
        feed = get_trademark_signals(session, months=(3 if weekly else 2), limit=8).get("feed", [])
    except Exception:
        feed = []
    if feed:
        L.append("### 🪧 진출 선행신호 — 최근 해외 상표 출원")
        for f in feed[:(6 if weekly else 4)]:
            L.append(f"- {f['date']} *{f['brand']}* ({f['country']}) — {f['mark']}")

    # 글로벌 검색 급등
    try:
        sp = get_google_spikes(session)
    except Exception:
        sp = []
    if sp:
        L.append("### 🔺 글로벌 검색 급등 (최근7일 vs 직전28일)")
        for x in sp[:5]:
            L.append(f"- *{x['brand']}* ({x['geo']}) 검색 {x['spike_ratio']}배↑")

    # 종합 스코어 (모멘텀·재무·상표·수요 통합)
    try:
        comp = get_brand_composite_score(session)
    except Exception:
        comp = []
    if comp:
        L.append("### 🏆 브랜드 종합 스코어 (0~100)")
        for o in comp[:(6 if weekly else 3)]:
            drv = "·".join(o.get("drivers", []))
            L.append(f"- *{o['brand']}* {o['score']}점" + (f" — {drv}↑" if drv else ""))

    if weekly:
        # 발표 vs 검색 수요 검증 (삼각)
        try:
            tri = get_demand_triangulation(session)
        except Exception:
            tri = []
        if tri:
            real = [t["brand"] for t in tri if t.get("verdict") == "real"][:5]
            latent = [t["brand"] for t in tri if t.get("verdict") == "latent"][:5]
            pr = [t["brand"] for t in tri if t.get("verdict") == "pr"][:5]
            vs = []
            if real:   vs.append(f"- 실질(뉴스↑·검색↑): {', '.join(real)}")
            if latent: vs.append(f"- 숨은수요(검색↑·보도적음): {', '.join(latent)}")
            if pr:     vs.append(f"- PR우세(보도↑·검색식음): {', '.join(pr)}")
            if vs:
                L.append("### 🔍 발표 vs 검색 수요 검증")
                L += vs
        # 뜨는 시장 (수출 YoY)
        try:
            mkts = get_market_growth_story(session).get("markets", [])
        except Exception:
            mkts = []
        if mkts:
            L.append("### 🌍 뜨는 시장 — 실수출 성장 (관세청)")
            for m in mkts[:5]:
                lead = (m["moves"][0]["brand"] if m.get("moves") else "")
                L.append(f"- {m['country_name']} 수출 +{m['yoy_pct']:.0f}%"
                         + (f" · 그 시장 경쟁사: {lead}" if lead else ""))

    # 경쟁사 악재 = 기회 신호 (일·주간 공통)
    try:
        negs = get_negative_signals(session, days=(7 if weekly else 2), limit=6)
    except Exception:
        negs = []
    if negs:
        L.append("### ⚠️ 경쟁사 악재 — 반사 기회")
        for n in negs[:5]:
            link = f" <{n['source_url']}|원문 ↗>" if str(n.get("source_url","")).startswith("http") else ""
            L.append(f"- *{n['brand']}* ({n['country']}) {n['activity_type']} · {n['title'][:56]}{link}")

    # 성분·포뮬러 지형 (주간만 — 축적 필요)
    if weekly:
        try:
            ings = get_ingredient_trends(session, days=30, limit=8)
        except Exception:
            ings = []
        if ings:
            L.append("### 🧪 경쟁사 성분 지형 (최근 30일)")
            for it in ings[:6]:
                who = ", ".join(it["brands"][:3]) + ("…" if it["brand_cnt"] > 3 else "")
                L.append(f"- *{it['ingredient']}* — {it['mentions']}건 / {it['brand_cnt']}개 브랜드"
                         + (f" ({who})" if who else ""))

    if not L:
        return ""
    header = "\n\n---\n\n📊 *신호 검증 (뉴스 외 4축: 검색·수출·재무·상표)*\n\n"
    return header + "\n".join(L)


# ── 주간 브리핑 (심층) ────────────────────────────────────────────────────────

def generate_weekly_briefing() -> str:
    """최근 7일 심층 주간 보고 → Slack (gpt-4o)."""
    session = get_session()
    try:
        rows = _fetch_rows(session, hours=24 * 7)
        stats = _stats(session, hours=24 * 7)
        signal_digest = _signal_digest(session, weekly=True)
    finally:
        session.close()

    if not rows:
        logger.info("주간 브리핑: 수집 데이터 없음")
        return ""

    data_prompt = _build_prompt_by_region(rows, limit=100, detail_len=240)
    if signal_digest:
        data_prompt += ("\n\n=== [정량 신호: 검색수요·수출성과·상표선행·종합스코어] ===\n"
                        "(뉴스와 교차해 반드시 해석에 반영)\n" + signal_digest)
    system = (
        "당신은 씨엠에스랩의 글로벌 경쟁 인텔리전스 수석 분석가입니다. "
        "아래 1주치 경쟁사 활동 데이터(권역별 정리)로 의사결정용 심층 주간 보고를 작성하세요.\n\n"
        f"{_cms_profile()}\n\n"
        "요구: 표면적 요약 금지. 구체 사실(채널·수치·제품·도시)과 '왜 중요한지'를 파고들 것.\n"
        "출력 규칙(슬랙 전송용): 섹션 머리말은 '### '로 시작. 강조는 별표 하나 *굵게* 만 사용(별표 두 개 ** 금지). "
        "번호목록·표 금지. 각 항목은 '- '로 시작.\n\n"
        "### Executive Takeaway\n- 이번 주 판을 바꾸는 핵심 변화 3~4개. 각 줄 앞에 *브랜드* 강조 + 무엇을·왜.\n\n"
        "### 권역별 핵심 움직임\n- 권역(EU·ME·SEA·IN·NA 등)별 가장 중요한 움직임을 "
        "'- [권역] *브랜드*: 무엇을 어디서(채널) → 왜 중요' 형식으로. 활동 있는 권역만.\n\n"
        "### 주요 무브먼트 상세 (Top 5~7)\nstrategic score 높은 순. 각 항목을 정확히 '두 줄'로:\n"
        "- *브랜드* · 국가 · 활동 · 채널 · score점  (75점↑면 맨 앞에 🔴[즉시공유])\n"
        "  → 무엇을·왜 중요·우리(셀퓨전씨) 접점 1~2문장.\n\n"
        "### Watchlist\n- 공식확인 약함(pr·rehash) 또는 후속 확인 필요한 3건 + 무엇을 확인해야 하나.\n\n"
        "### Implication (셀퓨전씨)\n- 우리 유통·상품·마케팅 관점 실행 액션 2~3개. 우리 실제 제품(레이저UV썬스크린·"
        "콜라겐PDRN앰플 등)/주력시장(올영·베트남·중국·일본·미국)에 구체 매칭.\n\n"
        "중요: 뉴스뿐 아니라 위 '정량 신호'(검색수요·수출성과·상표선행·종합스코어)를 Executive Takeaway와 "
        "Implication에서 반드시 함께 해석할 것 — 예: 발표는 있으나 검색·수출 미동반이면 'PR 노이즈'로 평가, "
        "신규 해외 상표는 '진출 임박'으로, 수출 급증 시장은 '실질 성장'으로 근거를 붙일 것.\n"
        "한국어. 데이터에 있는 사실만."
    )
    try:
        text_out = _openai("gpt-4o", system, data_prompt, max_tokens=3000)
    except Exception as e:
        logger.error("주간 브리핑 GPT 오류: %s", e)
        text_out = f"브리핑 생성 오류: {e}"

    if signal_digest and not text_out.startswith("브리핑 생성 오류"):
        text_out += signal_digest

    logger.info("주간 브리핑 생성 완료 (%d자)", len(text_out))
    _save(kind="weekly", content=text_out, stats=stats, hours=24 * 7, model="gpt-4o")
    send_weekly_briefing(text_out, stats)
    return text_out


# ── 일간 브리핑 (간결) ────────────────────────────────────────────────────────

def generate_daily_briefing() -> str:
    """전날 수집분 요약 → Slack (gpt-4o-mini)."""
    session = get_session()
    try:
        rows = _fetch_rows(session, hours=28)   # 전날 저녁 수집분 커버
        stats = _stats(session, hours=28)
        signal_digest = _signal_digest(session, weekly=False)
    finally:
        session.close()

    if not rows:
        logger.info("일간 브리핑: 전날 수집 없음")
        send_daily_briefing("어제 새로 잡힌 주목할 경쟁 활동이 없습니다." + (signal_digest or ""), stats)
        return ""

    data_prompt = _build_prompt_by_region(rows, limit=45, detail_len=160)
    if signal_digest:
        data_prompt += "\n\n=== [정량 신호 요약] ===\n" + signal_digest
    system = (
        "당신은 씨엠에스랩의 경쟁 인텔리전스 분석가입니다. 어제 수집된 경쟁사 활동을 아침 브리핑으로 "
        "간결히 정리하세요.\n\n"
        f"{_cms_profile()}\n\n"
        "마크다운 볼드(**) 쓰지 말 것. 아래 형식(머리말 '### '):\n\n"
        "### 어제의 핵심 (3~5건)\n- 각 줄: 브랜드/국가 - 무엇을(채널·제품 포함) → 한줄 시사점. score 높은 순.\n\n"
        "### 셀퓨전씨 관련\n- 우리 선케어·더마·주력시장과 겹치는 건이 있으면 1~2건 콕 집어 대응 포인트. 없으면 '특이사항 없음'.\n\n"
        "위 '정량 신호'(신규 상표=진출 임박, 검색 급등, 종합 스코어)에서 눈에 띄는 게 있으면 '어제의 핵심'에 한 줄 반영.\n"
        "한국어, 총 500자 내외. 데이터에 있는 사실만."
    )
    try:
        text_out = _openai("gpt-4o-mini", system, data_prompt, max_tokens=900)
    except Exception as e:
        logger.error("일간 브리핑 GPT 오류: %s", e)
        text_out = f"브리핑 생성 오류: {e}"

    if signal_digest and not text_out.startswith("브리핑 생성 오류"):
        text_out += signal_digest

    logger.info("일간 브리핑 생성 완료 (%d자)", len(text_out))
    _save(kind="daily", content=text_out, stats=stats, hours=28, model="gpt-4o-mini")
    send_daily_briefing(text_out, stats)
    return text_out
