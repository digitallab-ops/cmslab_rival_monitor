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
from notifications.slack import (send_weekly_briefing, send_daily_briefing,
                                 send_afternoon_digest)
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
          AND is_self IS NOT TRUE                    -- 자사(셀퓨전씨)는 경쟁 브리핑서 제외
          AND activity_type NOT IN ('실적_공시')     -- 실적·공시 store-only
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
    pr = f" 제품:{r[5]}" if r[5] else ""
    url = f" URL:{r[6]}" if r[6] else ""
    return (f"[score {r[8]}][{str(r[3]).upper()}] {r[0]}/{r[1]} - {r[2]}{pr}{ch}: "
            f"{(r[4] or '')[:detail_len]}{url}")


def _build_prompt_by_brand(rows, limit: int, detail_len: int) -> str:
    """브랜드별로 묶은 데이터 프롬프트 (슬랙 일간 — 활동 있는 브랜드만, 스코어순)."""
    if not rows:
        return "수집된 기사가 없습니다."
    buckets: dict = {}
    for r in rows[:limit]:
        buckets.setdefault(r[0] or "?", []).append(r)
    # 브랜드 정렬: 그 브랜드 최고 스코어 desc
    order = sorted(buckets, key=lambda b: -max((x[8] or 0) for x in buckets[b]))
    lines = []
    for b in order:
        lines.append(f"\n=== {b} ===")
        for r in buckets[b]:
            lines.append(_fmt_line(r, detail_len))
    return "\n".join(lines)


def _build_prompt_by_region(rows, limit: int, detail_len: int) -> str:
    """권역별로 묶은 데이터 프롬프트 (주간용)."""
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
            rt = perf.get("retail")
            if rt and rt.get("rank"): pf.append(f"아마존{rt.get('category','')}#{rt['rank']}")
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


# ── 데이터 리치 본문 구성 (대시보드 신호 재사용: 수치 배지 + 야무진 해석) ──────────

def _bko(b):
    from config.brands import BRAND_KO_NAMES
    v = BRAND_KO_NAMES.get(b)
    return (v[0] if isinstance(v, list) and v else (v or b))


def _cty_ko(c):
    from analytics.brief_strategy import _COUNTRY_KO
    return _COUNTRY_KO.get((c or "").upper(), c)


def _badges(r) -> list:
    """레코드의 정제 신호 → 수치 배지(데이터 리치)."""
    b = []
    if r.get("retail_rank_solid"):
        cat = r.get("retail_category") or ""
        b.append(f"🛒 아마존 {cat} #{r['retail_rank_solid']}")
    if r.get("google_spike"):
        b.append(f"🔍 검색 ▲{r['google_spike']:.1f}배")
    elif r.get("search_up"):
        b.append("🔍 검색↑")
    if r.get("export_yoy") is not None:
        b.append(f"📦 수출 {'+' if r['export_yoy'] >= 0 else ''}{r['export_yoy']:.0f}%")
    if r.get("oy_rank"):
        b.append(f"🇰🇷 올영 #{r['oy_rank']}")
    if r.get("trademark_cnt"):
        b.append(f"🪧 상표 {r['trademark_cnt']}")
    return b


def _prod_short(p):
    """상표·제품명 정리 — 브랜드 접두·괄호 제거, 짧게."""
    import re
    if not p:
        return ""
    p = re.sub(r"\[[^\]]*\]", "", p)
    p = re.sub(r"\s+", " ", p).strip(" ,·-")
    for pre in [r"^d[\'’]?alba\s*(piedmont)?", r"^SKIN1004", r"^Beauty of Joseon", r"^Abib",
                r"^Anua", r"^ANUA\([^)]*\)", r"^CENTELLIAN\s*24", r"^MEDIHEAL", r"^medicube",
                r"^COSRX", r"^VT", r"^Numbuzin", r"^numbuzin", r"^Mixsoon", r"^mixsoon"]:
        p = re.sub(pre, "", p, flags=re.I).strip(" ,·-")
    return p[:26]


def _sig(r):
    """핵심 신호 — 브랜드별 실측(수출은 국가값이라 제외, 아래 급성장시장에). 최대 2개."""
    out = []
    if r.get("retail_rank_solid"):
        out.append(f"아마존 {r.get('retail_category','')} {r['retail_rank_solid']}위")
    if r.get("google_spike"):
        out.append(f"검색 {r['google_spike']:.1f}배↑")
    elif r.get("oy_rank") and r["oy_rank"] <= 10:
        out.append(f"올영 {r['oy_rank']}위")
    return " · ".join(out[:2])


def _theme_and_watch(moves, market_line):
    """'매일 읽고 싶은' 편집물용 — 뾰족한 한 방(theme) + 지켜볼 것(watch). 1 LLM(json)."""
    import json as _json
    facts = []
    for r, rd in moves:
        ch = ", ".join(list((r.get("channels") or {}).keys())[:2])
        facts.append(f"- {_bko(r['brand'])}/{_cty_ko(r['country'])} · 채널={ch or '-'} · {rd}")
    prompt = (
        "너는 K뷰티 경쟁 인텔리전스 애널리스트다. 아래 이번 주 무브로 '매일 아침 꼭 읽고 싶은' "
        "브리핑의 두 요소를 써라.\n\n"
        "1) theme('이번 주의 한 방') — 딱 2문장, 짧게(합쳐 90자 이내). **브랜드명·수치 하나도 쓰지 마라**"
        "(아래 목록과 겹치면 안 됨).\n"
        "   · 첫 문장 = 이번 주를 규정하는 뾰족한 헤드라인 한 방(35자 이내, 단정적). "
        "예: '이번 주 진짜 뉴스는 브랜드가 아니라 채널이다' / 'K뷰티가 아마존 밖으로 나가기 시작했다' / "
        "'팝업이 신제품을 이긴 한 주'.\n"
        "   · 둘째 문장 = 그게 왜 지금 중요한지 근거 한 줄(55자 이내).\n"
        "   · 금지어(상투어): '다변화', '모색', '전환점', '입지 강화', '시장 점유율 확대', '긍정적', '기대'. "
        "예측·당위 금지, 관찰형.\n\n"
        "2) watch('지켜볼 것') — 1~2개. 이 무브 중 '다음에 판가름날' 지점을 궁금증 유발하게 짧게. "
        "예: '스킨1004 일본 팝업이 실제 판매로 이어지는지', '리쥬란 세포라가 반짝인지 안착인지'.\n\n"
        "움직임:\n" + "\n".join(facts)
        + (f"\n급성장 시장: {market_line}" if market_line else "")
        + '\n\n반드시 JSON: {"theme":"...", "watch":["...","..."]}')
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
        resp = client.chat.completions.create(
            model="gpt-4o", max_tokens=360, temperature=0.4,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}])
        d = _json.loads(resp.choices[0].message.content or "{}")
        return (d.get("theme") or "").strip(), [w.strip() for w in (d.get("watch") or []) if w.strip()][:2]
    except Exception as e:
        logger.warning("테마/지켜볼것 생성 실패: %s", e)
        return "", []


def _compose_brief_body(session, weekly: bool):
    """슬랙 브리핑 본문 — 대시보드와 '무브 해석'은 동일(build_brief_strategy)하되,
    상단 '큰 그림'은 목록과 겹치지 않는 통찰(별도 생성). 흥미·밀도 위해 4~6건.
    반환: (body_markdown, kpi_stats)."""
    from datetime import datetime, timedelta
    from collections import defaultdict
    from analytics.queries import (get_brief_records, get_retail_performance,
                                   get_market_export_growth)
    from analytics.brief_strategy import build_brief_strategy, is_meaningful_line

    recs = get_brief_records(session, days=7)          # 대시보드와 동일 창
    if not recs:
        return "", {}
    try:
        rp = get_retail_performance(session)
    except Exception:
        rp = {}
    try:
        mkt = get_market_export_growth(session, hs_like="330499", trailing=3)
    except Exception:
        mkt = []
    to7 = datetime.utcnow().date().isoformat()
    from7 = (datetime.utcnow() - timedelta(days=7)).date().isoformat()
    # 무브 해석은 대시보드와 동일 캐시(BS|…) — 클릭해 들어가도 같은 문구
    sd = build_brief_strategy(session, recs, rp, mkt, from7, to7, bko=_bko)
    strat = sd.get("strat", {})

    # 대시보드 브랜드 카드와 동일 정렬(브랜드 attn 합), 각 브랜드 top 국가(의미있는 해석)
    g = defaultdict(list)
    for r in recs:
        g[r["brand"]].append(r)
    brands_sorted = sorted(g.items(), key=lambda x: -sum(rr["attn"] for rr in x[1]))
    topN = 6 if weekly else 4
    moves = []
    for b, rs in brands_sorted:
        cand = [r for r in sorted(rs, key=lambda x: -x["attn"])
                if is_meaningful_line(strat.get((b, r["country"]), ""))]
        if not cand:
            continue
        r = cand[0]
        moves.append((r, strat[(b, r["country"])].strip()))
        if len(moves) >= topN:
            break
    if not moves:
        return "", {}

    # 급성장 시장(성장률 순)
    mk = sorted([m for m in mkt if m.get("yoy_pct") is not None and m.get("exp_usd_3m", 0) >= 8e6],
                key=lambda z: -z["yoy_pct"])[:3]
    market_line = " · ".join(f"*{_cty_ko(m['country_code'])}* +{m['yoy_pct']:.0f}%" for m in mk)

    # 편집물 훅 — 뾰족한 한 방 + 지켜볼 것(목록과 겹치지 않음)
    theme, watch = _theme_and_watch(moves, market_line)

    # 무브 라인(번호 + 제품·수치 + 해석). 해석 앞 브랜드명 중복 제거.
    move_lines = []
    for idx, (r, line_read) in enumerate(moves, 1):
        bn = _bko(r["brand"])
        if line_read.startswith(bn):
            line_read = line_read[len(bn):].lstrip("가이은는을를 ·,").strip()
        sig = _sig(r)
        head = f"*{idx}. {bn}* · {_cty_ko(r['country'])}"
        if sig:
            head += f"  —  {sig}"
        move_lines.append(head + (f"\n{line_read}" if line_read else ""))

    parts = []
    if theme:
        parts.append(f"*📌 이번 주의 한 방*\n{theme}")
    parts.append("*🎯 주목할 움직임*\n" + "\n\n".join(move_lines))
    if market_line:
        parts.append(f"*📈 다음 격전지*  {market_line}")
    if watch:
        parts.append("*👀 지켜볼 것*\n" + "\n".join(f"• {w}" for w in watch))
    body = "\n\n".join(parts)

    kpi = {
        "total": len(recs),
        "high": sum(1 for r in recs if r.get("importance") == "high" or (r.get("high_cnt") or 0) > 0),
        "brands": len({r["brand"] for r in recs if r["label"] == "verified"}),
        "countries": len({r["country"] for r in recs if r["label"] == "market"}),
    }
    return body, kpi


# ── 주간 브리핑 (심층) ────────────────────────────────────────────────────────

def generate_weekly_briefing() -> str:
    """최근 7일 심층 주간 보고 → Slack (gpt-4o)."""
    session = get_session()
    try:
        body, kpi = _compose_brief_body(session, weekly=True)
    finally:
        session.close()
    if not body:
        logger.info("주간 브리핑: 데이터 없음")
        return ""
    logger.info("주간 브리핑 생성 완료 (%d자)", len(body))
    _save(kind="weekly", content=body, stats=kpi, hours=24 * 7, model="gpt-4o-mini")
    send_weekly_briefing(body, kpi)
    return body


# ── 일간 브리핑 (간결) ────────────────────────────────────────────────────────

def generate_daily_briefing() -> str:
    """어제 활동 → Slack. 대시보드 신호(수치 배지) + 야무진 해석."""
    session = get_session()
    try:
        body, kpi = _compose_brief_body(session, weekly=False)
    finally:
        session.close()
    if not body:
        logger.info("일간 브리핑: 데이터 없음")
        send_daily_briefing("어제 새로 잡힌 주목할 경쟁 활동이 없습니다.", {})
        return ""
    logger.info("아침 브리핑 생성 완료 (%d자)", len(body))
    _save(kind="daily", content=body, stats=kpi, hours=28, model="gpt-4o-mini")
    send_daily_briefing(body, kpi)
    return body


# ── 오후 다이제스트 (오전 신규 HIGH 있을 때만) ────────────────────────────────

def generate_afternoon_digest() -> str:
    """오전에 새로 수집된 HIGH만 짧게 → Slack. 없으면 발송 안 함(소음 최소)."""
    from analytics.summarizer import _TONE_GUIDE
    session = get_session()
    try:
        rows = _fetch_rows(session, hours=6)     # 오늘 오전 수집분
        stats = _stats(session, hours=6)
    finally:
        session.close()

    high = [r for r in rows if str(r[3]).lower() == "high"]
    if not high:
        logger.info("오후 다이제스트: 새 HIGH 없음 — 발송 스킵")
        return ""

    data_prompt = "\n".join(_fmt_line(r, 180) for r in high[:12])
    system = (
        "당신은 씨엠에스랩 경쟁 인텔리전스 분석가입니다. 오늘 오전 새로 잡힌 '중요(HIGH)' 활동만 "
        "점심 후 훑어볼 수 있게 아주 짧게 정리하세요.\n\n"
        f"{_cms_profile()}\n\n"
        "규칙: 3~6줄, 각 줄 '- *브랜드* (국가): 무엇을 — 무슨 의미/어떤 방향(객관) 한 줄'. 제품명·채널 구체. "
        "뭉뚱그림·추측 금지, 데이터 사실만. 마크다운 볼드(**)·번호목록 금지, 강조는 *별표 하나*, 머리말 '### '.\n\n"
        "### 오후 업데이트 · 오전 신규 HIGH\n- ...\n\n"
        f"{_TONE_GUIDE}"
    )
    try:
        text_out = _openai("gpt-4o-mini", system, data_prompt, max_tokens=700)
    except Exception as e:
        logger.error("오후 다이제스트 GPT 오류: %s", e)
        return ""

    logger.info("오후 다이제스트 생성 (%d자, HIGH %d건)", len(text_out), len(high))
    _save(kind="afternoon", content=text_out, stats=stats, hours=6, model="gpt-4o-mini")
    send_afternoon_digest(text_out, stats)
    return text_out
