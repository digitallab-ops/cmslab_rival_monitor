"""질문에 답하기 전에 "우리 데이터가 이걸 커버하는가"를 먼저 확인한다.

슬랙봇은 툴을 호출해 답한다. 그런데 질문이 우리가 아예 모으지 않는 것을
물으면(광고비·시장점유율·틱톡), 툴은 뭐라도 돌려주고 모델은 그걸로 그럴듯한
답을 만든다. 받는 사람은 그게 추정인지 실측인지 알 수 없다.

그래서 답하기 전에 한 단계를 둔다 — **없는 건 없다고 말한다.**
NVIDIA auto-ontology의 `check_answerable`에서 가져온 생각이다.

설계에서 가장 조심한 것: **막는 쪽으로 기울면 안 된다.** 답할 수 있는 질문을
막는 비용이, 애매한 답을 하는 비용보다 크다. 그래서
  · 판정이 애매하면 통과(full)
  · LLM이 실패하면 통과(full)
  · 'none'은 질문의 핵심이 통째로 없을 때만
  · 'partial'은 막지 않는다 — 빠진 부분을 답에 명시하게만 한다

명세(무엇을 갖고 있는가)는 **DB에서 실측해 만든다.** 손으로 적어 두면 데이터가
늘거나 줄 때 어긋난다 — 국가 용어에서 겪은 것과 같은 문제다([[config/vocab.py]]).
의미 레이블(이 표가 무슨 질문에 답하는가)만 코드에 있다.
"""

import json
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

_MODEL = os.getenv("ANSWERABLE_MODEL", "gpt-4o-mini")
_TTL = 3600          # 명세는 자주 안 변한다. 1시간 캐시.
_lock = threading.Lock()
_cache: dict = {}
_cached_at = 0.0


# ── 우리가 모으지 않는 것 ───────────────────────────────────────────────────
# 실제로 들어온 질문에서 늘려 갈 것. 여기 적힌 것만 막는 게 아니라, 명세에 없는
# 주제를 LLM이 판단한다 — 이 목록은 자주 나오는 것을 못 박아 두는 용도다.
NOT_COLLECTED = [
    "경쟁사 광고비·마케팅 예산·매체 집행액",
    "시장점유율(M/S)",
    # '매출이 없다'가 아니다 — 공시 기업의 회사 단위 매출은 보유 6번에 있다.
    # 없는 건 국가로 쪼갠 판매액이다. 길게 풀어 썼더니 모델이 헷갈려 정방향이
    # 무너졌고, 짧게 쓰니 '메디큐브 매출'이 막혔다. 그래서 문구가 아니라
    # 판정 규칙(_SYS의 '보유 목록 우선')으로 풀었다.
    "국가로 쪼갠 판매액·판매수량 (예: 아누아의 일본 매출)",
    "틱톡·인스타그램 지표 (유튜브만 모은다)",
    "중국 플랫폼(티몰·타오바오·샤오홍슈·더우인)",
    "유통 재고·물류·공급망",
    "소비자 설문·NPS·브랜드 인지도 조사",
    "오프라인 매장 수·점포별 실적",
    "임직원 수·조직도·인사",
    "경쟁사 원가·마진 (공시 재무가 있는 상장사 제외)",
]


def _probe(session) -> dict:
    """DB에서 실제 보유량을 잰다."""
    from sqlalchemy import text
    from config.settings import DB_SCHEMA as S

    def one(sql, default=None):
        try:
            r = session.execute(text(sql)).first()
            return tuple(r) if r else default
        except Exception as e:
            logger.debug("명세 조회 실패: %s", e)
            return default

    def lst(sql):
        try:
            return [r[0] for r in session.execute(text(sql)).fetchall() if r[0]]
        except Exception:
            return []

    m: dict = {}
    m["news"] = one(
        f"SELECT min(collected_at)::date::text, max(collected_at)::date::text, count(*), "
        f"count(*) FILTER (WHERE published_date > now() - interval '90 days') "
        f"FROM {S}.news_articles", ("", "", 0, 0))
    m["brands"] = lst(f"SELECT name FROM {S}.monitored_brands WHERE is_active IS NOT FALSE ORDER BY name")
    # 국가는 코드가 아니라 한국어명으로 넘긴다. '몽골 진출한 브랜드 있어?'를
    # 물었을 때 명세에 'MN'만 있으면 모델이 없는 줄 알고 막아 버린다.
    raw_cc = lst(
        f"SELECT country FROM {S}.news_articles "
        f"WHERE published_date > now() - interval '180 days' AND country IS NOT NULL "
        f"GROUP BY country ORDER BY count(*) DESC")
    try:
        from config.vocab import country_ko, is_country
        m["countries"] = [country_ko(c) for c in raw_cc if is_country(c)]
    except Exception:
        m["countries"] = raw_cc
    # 어느 브랜드에 재무가 있는지 — 개수만 주면 '메디큐브 매출'을 못 답한다고 막는다.
    m["fin_brands"] = lst(f"SELECT DISTINCT brand FROM {S}.competitor_financials WHERE brand IS NOT NULL")
    m["amazon"] = (lst(f"SELECT DISTINCT country FROM {S}.retail_rankings"),
                   one(f"SELECT min(capture_date)::text, max(capture_date)::text, count(*) "
                       f"FROM {S}.retail_rankings", ("", "", 0)))
    m["oliveyoung"] = one(
        f"SELECT min(capture_date)::text, max(capture_date)::text, count(*) "
        f"FROM {S}.oliveyoung_rankings", ("", "", 0))
    m["youtube"] = one(
        f"SELECT min(captured_date)::text, max(captured_date)::text, count(DISTINCT brand) "
        f"FROM {S}.social_metrics", ("", "", 0))
    m["trends"] = (one(f"SELECT min(period)::text, max(period)::text FROM {S}.google_trends", ("", "")),
                   one(f"SELECT min(period)::text, max(period)::text FROM {S}.search_trends", ("", "")))
    m["financials"] = one(
        f"SELECT count(DISTINCT brand), min(bsns_year)::text, max(bsns_year)::text "
        f"FROM {S}.competitor_financials", (0, "", ""))
    m["export"] = one(
        f"SELECT min(period)::text, max(period)::text, count(DISTINCT country_code) "
        f"FROM {S}.export_stats", ("", "", 0))
    # 어느 나라인지까지 적는다. '2개국'만 적었더니 모델이 데이터가 빈약하다고
    # 보고 '상표 출원 뭐 들어왔어?'를 간헐적으로 막았다(역검증에서 잡음).
    m["trademark"] = one(
        f"SELECT count(*), count(DISTINCT brand), "
        f"count(*) FILTER (WHERE app_date > now() - interval '1 year') "
        f"FROM {S}.trademark_filings", (0, 0, 0))
    m["tm_countries"] = lst(f"SELECT DISTINCT country FROM {S}.trademark_filings WHERE country IS NOT NULL")
    m["ingredients"] = one(f"SELECT count(*) FROM {S}.product_ingredients", (0,))
    # 주제별 보유 행수 — 확정 해제(_topic_backed) 판정에 쓴다.
    for key, tbl in (("tm_rows", "trademark_filings"), ("az_rows", "retail_rankings"),
                     ("oy_rows", "oliveyoung_rankings"), ("yt_rows", "social_metrics"),
                     ("tr_rows", "google_trends"), ("ex_rows", "export_stats"),
                     ("ing_rows", "product_ingredients"), ("bf_rows", "briefings"),
                     ("news_rows", "news_articles")):
        m[key] = (one(f"SELECT count(*) FROM {S}.{tbl}", (0,)) or (0,))[0]
    m["price_cov"] = one(
        f"SELECT count(*) FILTER (WHERE price_info IS NOT NULL AND price_info <> ''), count(*) "
        f"FROM {S}.news_articles", (0, 1))
    return m


def manifest(session=None, force: bool = False) -> dict:
    """보유 데이터 명세(캐시). DB를 못 읽으면 빈 dict — 그때는 게이트가 통과시킨다."""
    global _cache, _cached_at
    with _lock:
        if not force and _cache and time.time() - _cached_at < _TTL:
            return _cache
        own = session is None
        try:
            if own:
                from storage.models import get_session
                session = get_session()
            _cache = _probe(session)
            _cached_at = time.time()
            return _cache
        except Exception as e:
            logger.warning("보유 명세 생성 실패(게이트 통과로 처리): %s", e)
            _cached_at = time.time()
            return {}
        finally:
            if own and session is not None:
                try:
                    session.close()
                except Exception:
                    pass


def _with_ko(brands: list) -> list:
    """영문 등록명에 한글 표기를 붙인다 — 사람은 한글로 묻는다."""
    try:
        from config.brands import BRAND_KO_NAMES
    except Exception:
        return brands
    out = []
    for b in brands:
        ko = [k for k in (BRAND_KO_NAMES.get(b) or []) if k and k != b][:2]
        out.append(f"{b}({'·'.join(ko)})" if ko else b)
    return out


def _self_names() -> list:
    try:
        from config.brands import SELF_BRANDS, BRAND_KO_NAMES
        out = []
        for b in SELF_BRANDS:
            out.append(b)
            out += [k for k in (BRAND_KO_NAMES.get(b) or []) if k != b][:2]
        return out or ["CellFusionC", "셀퓨전씨"]
    except Exception:
        return ["CellFusionC", "셀퓨전씨"]


def manifest_text(session=None) -> str:
    """LLM에 넣을 명세 요약. 숫자는 DB 실측, 설명은 코드."""
    m = manifest(session)
    if not m:
        return ""
    n = m.get("news") or ("", "", 0, 0)
    az_c, az_r = m.get("amazon") or ([], ("", "", 0))
    oy = m.get("oliveyoung") or ("", "", 0)
    yt = m.get("youtube") or ("", "", 0)
    gt, st = m.get("trends") or (("", ""), ("", ""))
    fin = m.get("financials") or (0, "", "")
    ex = m.get("export") or ("", "", 0)
    tm = m.get("trademark") or (0, 0)
    pc = m.get("price_cov") or (0, 1)
    brands = m.get("brands") or []
    ccs = m.get("countries") or []

    return "\n".join([
        "[우리가 가진 데이터]",
        f"1. 경쟁사 뉴스 활동 — {n[2]:,}건 ({n[0]}~{n[1]} 수집, 최근 90일 {n[3]:,}건). "
        f"활동유형 9종(신시장 진출·유통 채널·신제품 런칭·인플루언서 협업·투자/BD·"
        f"브랜드 마케팅·실적 공시·가격 프로모션·기타), 전략점수 0~100.",
        f"   기사가 쌓인 나라 {len(ccs)}곳: {', '.join(ccs)}",
        f"2. 아마존 랭킹 — {az_c and ','.join(sorted(az_c))} {len(az_c)}개국, "
        f"{az_r[2]:,}행 ({az_r[0]}~{az_r[1]}). 순위·평점·리뷰수이며 판매수량은 아니다.",
        f"3. 올리브영 국내 랭킹 — {oy[2]:,}행 ({oy[0]}~{oy[1]}) + 리뷰 키워드(주간).",
        f"4. 유튜브 버즈 — 브랜드 {yt[2]}개 ({yt[0]}~{yt[1]}). 조회수·영상수·참여율. "
        f"유튜브만이며 틱톡·인스타는 없다.",
        f"5. 검색 수요 — 구글 트렌드 글로벌({gt[0]}~{gt[1]}), 네이버 검색트렌드 국내({st[0]}~{st[1]}).",
        f"6. 재무 — DART 공시 경쟁사 {fin[0]}개 브랜드({fin[1]}~{fin[2]}년): "
        f"{', '.join(m.get('fin_brands') or []) or '없음'}. "
        f"여기에 더해 네이버 컨센서스(추정 실적), NICE 업계 통계. 상장·공시 기업만이다.",
        f"7. 관세청 화장품 수출통계 — {ex[0]}~{ex[1]}, {ex[2]}개국. "
        f"품목(HS코드)·국가 단위이고 브랜드 단위가 아니다.",
        f"8. 해외 상표출원(KIPRIS) — {tm[0]:,}건, 브랜드 {tm[1]}개, "
        f"최근 1년 {tm[2]}건. 출원국은 "
        f"{', '.join(m.get('tm_countries') or []) or '미상'}. 진출 선행지표로 쓴다.",
        f"9. 제품 전성분 — {m.get('ingredients', (0,))[0]}개 제품.",
        f"10. 가격 — 기사에 가격이 적힌 {pc[0]:,}건({pc[0]/max(pc[1],1)*100:.0f}%)뿐. 전수 가격표는 없다.",
        "",
        f"[추적 중인 경쟁 브랜드 {len(brands)}개 — 괄호 안이 한글 표기]",
        # 한글명을 빼먹으면 '조선미녀'·'코스알엑스'·'메디큐브'를 물었을 때 모델이
        # "추적 안 하는 브랜드"로 보고 막아 버린다. 역검증에서 22개 중 3개가
        # 이 이유로 잘못 막혔다. 수집기·검증계층에서도 같은 문제를 겪었다.
        ", ".join(_with_ko(brands)),
        "",
        f"[우리(자사) 브랜드] {', '.join(_self_names())} — 경쟁사가 아니라 우리다. "
        f"자사 올리브영 성과·제품 리뷰 데이터를 따로 갖고 있고, 모든 답의 "
        f"'그래서 우리는?' 관점이 여기에 선다. 추적 목록에 없다고 막지 마라.",
        "",
        "[모으지 않는 것]",
        "\n".join(f"- {x}" for x in NOT_COLLECTED),
    ])


_SYS = """너는 K-뷰티 경쟁사 모니터링 시스템의 '답변 가능성' 판정기다.
질문이 우리 보유 데이터로 답할 수 있는지만 판정한다. 답을 만들지 마라.

coverage 중 하나:
  full    — 보유 데이터로 답할 수 있다
  partial — 일부는 답할 수 있고 일부는 없다
  none    — 질문의 핵심이 통째로 우리 데이터 밖이다

**기본값은 full이다.** 애매하면 full로 답하라. 답할 수 있는 질문을 막는 것이
애매하게 답하는 것보다 나쁘다. 아래는 full이다:
  · 인사·잡담·시스템 사용법·이전 대화에 대한 질문
  · "요즘 어때", "뭐 있어", "정리해줘" 같은 열린 질문 (가진 걸로 답하면 된다)
  · 해석·의견·전략 제안을 요구하는 질문 (데이터 위에 올리면 된다)
  · 추적 중인 브랜드에 대한 거의 모든 질문

**[우리가 가진 데이터] 목록이 [모으지 않는 것]보다 우선한다.** 두 쪽에 비슷한
낱말이 보이면, 보유 목록에 그 항목이 **이름으로 적혀 있는지**를 보고 판정하라.
예: 재무(6번)에 이름이 적힌 브랜드의 매출·영업이익은 **답할 수 있다**(full).
'모으지 않는 것'의 매출 항목은 국가로 쪼갠 판매액을 말한다.

**브랜드 이름은 한글로 들어온다.** 목록의 괄호 안이 한글 표기다 —
'조선미녀'는 Beauty of Joseon, '코스알엑스'는 COSRX, '메디큐브'는 Medicube로
전부 추적 중이다. 괄호에 없어도 한글 음차가 목록의 영문명과 같은 브랜드를
가리키면 추적 중인 것이다. 자사 브랜드(셀퓨전씨)도 막지 마라.

**나라도 한글로 들어온다.** 위 '기사가 쌓인 나라' 목록에 있으면 답할 수 있다.

none은 이럴 때만:
  · 모으지 않는 항목이 질문의 핵심일 때 (광고비·시장점유율·틱톡·중국 플랫폼 등)
  · 추적 목록에도 자사에도 없는 **완전히 다른 브랜드**가 질문의 주인공일 때
    (예: 설화수·헤라·라네즈·이니스프리 같은 대기업 브랜드는 목록에 없으면 none)
  · 뷰티 경쟁사 모니터링과 아무 상관 없는 질문일 때 (날씨·일정·번역 등)

missing에는 '질문이 요구하는데 우리에게 없는 것'을 사람 말로 적어라.
alternative에는 '대신 답해 줄 수 있는 것'을 구체적으로 적어라 — 없으면 "".
untracked_brand에는 질문에 나왔지만 추적 목록에 없는 브랜드명을 적어라 — 없으면 "".

JSON으로만:
{"coverage":"full|partial|none","missing":["..."],"alternative":"...",
 "untracked_brand":"...","reason":"한 문장"}"""


_FIN_WORDS = ("매출", "영업이익", "실적", "순이익", "영업익", "revenue", "매출액",
              "적자", "흑자", "영업손실", "컨센서스", "분기")

# 낱말만으로 확실히 없는 주제 — 다른 뜻으로 쓰일 여지가 없다.
_NO_DATA_TOPICS = ("광고비", "마케팅예산", "매체집행", "매체비", "점유율", "시장점유",
                   "재고", "물류", "공급망", "nps", "인지도조사", "브랜드인지도",
                   "직원수", "임직원", "조직도", "원가율")
# 출처는 맞는데 지표를 물으면 없다. '틱톡에서 뜬 뉴스'는 기사로 답할 수 있으므로
# 지표 낱말이 같이 있을 때만 막는다.
_NO_DATA_SOURCES = ("틱톡", "tiktok", "인스타", "instagram", "티몰", "tmall",
                    "타오바오", "taobao", "샤오홍슈", "더우인", "douyin",
                    "매장", "점포", "오프라인스토어")
_METRIC_WORDS = ("몇", "얼마", "수치", "지표", "팔로워", "조회수", "개야", "명이야",
                 "건수", "매출", "판매량", "순위는")


# 낱말 → 그 질문이 기대는 데이터 묶음. 그 묶음에 행이 있으면 'none'으로 막지
# 않는다. 조회하면 아는 사실을 LLM 판정에 맡기면 흔들린다 — '경쟁사 상표 출원
# 뭐 들어왔어?'가 714건을 갖고도 4회 중 3회 막혔다. 메디큐브 매출과 같은 패턴.
_TOPIC_DATA = {
    "trademark": (("상표", "출원", "trademark", "등록출원"), "tm_rows"),
    "amazon":    (("아마존", "amazon", "랭킹", "순위"), "az_rows"),
    "oliveyoung": (("올리브영", "올영", "oliveyoung"), "oy_rows"),
    "youtube":   (("유튜브", "youtube", "조회수", "버즈"), "yt_rows"),
    "trends":    (("검색량", "검색트렌드", "트렌드", "구글트렌드", "네이버검색"), "tr_rows"),
    "export":    (("수출", "관세청", "수출액"), "ex_rows"),
    "ingredient": (("전성분", "성분", "포뮬라"), "ing_rows"),
    "briefing":  (("브리핑", "주간보고", "요약해"), "bf_rows"),
    "news":      (("기사", "뉴스", "진출", "입점", "런칭", "신제품", "프로모션",
                   "인플루언서", "채널", "활동"), "news_rows"),
}


def _topic_backed(question: str) -> str:
    """질문이 기대는 데이터를 우리가 실제로 갖고 있으면 그 이름을 돌려준다."""
    q = (question or "").lower().replace(" ", "")
    m = manifest()
    for name, (words, key) in _TOPIC_DATA.items():
        if any(w in q for w in words) and (m.get(key) or 0) > 0:
            return name
    return ""


def _deterministic_block(question: str) -> str:
    """낱말만 보고 확실히 못 답하는 질문 — LLM 판정보다 세게 막는다.

    이것만 확정으로 막고, 나머지는 LLM 판단에 맡긴다. 여기 넣을 낱말은
    '다른 뜻으로 쓰일 여지가 없는가'를 기준으로 고른다.
    """
    q = (question or "").lower().replace(" ", "")
    for w in _NO_DATA_TOPICS:
        if w in q:
            return w
    if any(w in q for w in _NO_DATA_SOURCES) and any(w in q for w in _METRIC_WORDS):
        return next(w for w in _NO_DATA_SOURCES if w in q)
    return ""


def _brands_in(question: str) -> list:
    """질문에 나온 추적 브랜드 — 영문명·한글 별칭 둘 다로 찾는다.

    오직 **막힌 걸 풀어 주는 데만** 쓴다. 과하게 잡혀도 통과시키는 방향이라
    안전하다(막는 쪽으로는 절대 쓰지 않는다).
    """
    q = (question or "").lower().replace(" ", "")
    if not q:
        return []
    try:
        from config.brands import BRAND_KO_NAMES
    except Exception:
        BRAND_KO_NAMES = {}
    hits = []
    for b in (manifest().get("brands") or []):
        names = [b] + list(BRAND_KO_NAMES.get(b) or [])
        for nm in names:
            n = (nm or "").lower().replace(" ", "")
            if len(n) >= 2 and n in q:
                hits.append(b)
                break
    return hits


def _hard_facts(question: str, chk: dict) -> dict:
    """조회하면 아는 사실로 LLM 판정을 덮는다.

    '메디큐브 매출 얼마야?'가 3회 연속 막혔다 — 에이피알 공시가 DB에 있는데도
    모델이 '모으지 않는 것'의 매출 항목에 끌려갔다. 프롬프트를 두 번 고쳐도
    안 되길래, 조회로 답이 나오는 건 조회로 정한다. LLM은 판단만 한다.
    """
    hits = _brands_in(question)

    # (1) 추적 중인 브랜드를 '추적 안 함'으로 본 것부터 지운다. **확정 차단보다
    #     먼저** 해야 한다 — 안 그러면 '아누아 광고비'를 막으면서 "아누아는
    #     추적 대상이 아니다"라는 엉뚱한 답이 나간다(끝단 검증에서 잡았다).
    #     이유만 지우고 판정은 안 바꾼다. 다른 이유로 막힌 것까지 풀면
    #     '조선미녀 틱톡 팔로워'가 통과해 버린다.
    if hits and chk.get("untracked_brand"):
        chk["untracked_brand"] = ""
        if chk.get("coverage") == "none" and not chk.get("missing"):
            chk["coverage"] = "full"
            chk["reason"] = (chk.get("reason") or "") + f" (실제로는 추적 중: {', '.join(hits)})"

    # (2) 확실히 없는 주제면 LLM이 뭐라 했든 막는다.
    blocked_by = _deterministic_block(question)
    if blocked_by:
        chk["coverage"] = "none"
        if not chk.get("missing"):
            chk["missing"] = [f"'{blocked_by}' 관련 데이터"]
        chk["reason"] = f"'{blocked_by}'는 모으지 않는 항목(확정)"
        return chk

    # (3) 질문이 기대는 데이터를 실제로 갖고 있으면 'none'으로 막지 않는다.
    #     확정 차단(2)을 이미 통과했으므로 여기 오는 건 없는 주제가 아니다.
    if chk.get("coverage") == "none":
        backed = _topic_backed(question)
        if backed:
            chk["coverage"] = "partial" if chk.get("missing") else "full"
            chk["reason"] = f"{backed} 데이터 보유 — 답변 가능(확정)"

    if not hits:
        return chk
    m = manifest()
    # (4) 공시 재무가 있는 브랜드의 매출·실적 질문은 답할 수 있다.
    #     위에서 확정 차단에 안 걸렸으므로 티몰·국가별 판매 질문은 여기 안 온다.
    fin = set(m.get("fin_brands") or [])
    if (chk.get("coverage") == "none" and fin & set(hits)
            and any(w in (question or "") for w in _FIN_WORDS)):
        chk["coverage"] = "full"
        chk["missing"] = []
        chk["reason"] = f"DART 공시 재무 보유({', '.join(sorted(fin & set(hits)))}) — 답변 가능"
    return chk


def check_answerable(question: str, session=None) -> dict:
    """질문 → {coverage, missing, alternative, untracked_brand, reason}.

    판정 못 하면 coverage='full' — 막지 않는다.
    """
    q = (question or "").strip()
    out = {"coverage": "full", "missing": [], "alternative": "",
           "untracked_brand": "", "reason": ""}
    if len(q) < 2:
        return out
    mtxt = manifest_text(session)
    if not mtxt:
        # 명세를 못 읽어도 게이트를 통째로 끄지는 않는다. 광고비·점유율·재고처럼
        # DB를 안 봐도 확실히 없는 주제는 그대로 막는다 — 예전엔 여기서 바로
        # 돌아가는 바람에 명세 조회가 한 번 실패하면 게이트가 사라졌다.
        blocked_by = _deterministic_block(q)
        if blocked_by:
            out.update({"coverage": "none", "missing": [f"'{blocked_by}' 관련 데이터"],
                        "reason": f"'{blocked_by}'는 모으지 않는 항목(확정, 명세 없이 판정)"})
        else:
            out["reason"] = "보유 명세를 읽지 못해 판정 생략"
        return out
    try:
        from openai import OpenAI
        r = OpenAI(api_key=os.getenv("OPENAI_API_KEY")).chat.completions.create(
            model=_MODEL, max_tokens=320, temperature=0,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": _SYS},
                      {"role": "user", "content": mtxt + "\n\n[질문]\n" + q}])
        got = json.loads(r.choices[0].message.content or "{}")
    except Exception as e:
        # 명세 실패 때와 같다 — LLM이 죽어도 확정 규칙은 살아 있어야 한다.
        logger.warning("답변가능성 LLM 판정 실패(확정 규칙만 적용): %s: %s",
                       type(e).__name__, e)
        blocked_by = _deterministic_block(q)
        if blocked_by:
            out.update({"coverage": "none", "missing": [f"'{blocked_by}' 관련 데이터"],
                        "reason": f"'{blocked_by}'는 모으지 않는 항목(확정, LLM 없이 판정)"})
        else:
            out["reason"] = f"판정 실패({type(e).__name__})"
        return out

    cov = str(got.get("coverage") or "full").lower()
    if cov not in ("full", "partial", "none"):
        cov = "full"
    miss = got.get("missing") or []
    if isinstance(miss, str):
        miss = [miss]
    # 'none'인데 빠진 것을 대지 못하면 믿을 수 없는 판정이다 — 통과시킨다.
    if cov == "none" and not miss and not (got.get("untracked_brand") or "").strip():
        cov = "full"
    out.update({"coverage": cov, "missing": [str(x) for x in miss][:5],
                "alternative": str(got.get("alternative") or "").strip(),
                "untracked_brand": str(got.get("untracked_brand") or "").strip(),
                "reason": str(got.get("reason") or "").strip()})
    # 조회로 아는 사실이 LLM 판정보다 세다. 푸는 방향으로만 작동한다.
    before = out["coverage"]
    try:
        out = _hard_facts(question, out)
    except Exception as e:
        # 여기서 터지면 확정 규칙이 통째로 날아간다 — debug로 묻어 두는 바람에
        # Render에서 게이트가 안 걸리는 원인을 한참 못 찾았다.
        logger.warning("확정 사실 보정 실패(%s): %s", type(e).__name__, e, exc_info=True)
    if before != out["coverage"]:
        logger.info("확정 보정: %s → %s (%s)", before, out["coverage"],
                    (out.get("reason") or "")[:60])
    return out


def decline_message(chk: dict) -> str:
    """coverage='none'일 때 사람에게 보낼 답. 못 한다고만 하지 말고 길을 준다."""
    miss = chk.get("missing") or []
    brand = (chk.get("untracked_brand") or "").strip()
    alt = (chk.get("alternative") or "").strip()
    if brand:
        lines = [f"*{brand}* 은(는) 아직 추적 대상이 아니라 쌓인 데이터가 없어요.",
                 f"추가하시려면 `추가 {brand}` 라고 해주세요 — 다음 수집부터 들어갑니다."]
    else:
        lines = ["이건 우리가 모으지 않는 데이터라 답을 만들 수 없어요."]
        if miss:
            lines.append("없는 것: " + " · ".join(miss[:3]))
    if alt:
        lines.append(f"대신 이건 됩니다 — {alt}")
    lines.append("_추측으로 답하지 않으려고 먼저 확인했어요._")
    return "\n".join(lines)


def partial_note(chk: dict) -> str:
    """coverage='partial'일 때 시스템 프롬프트에 끼울 한 줄."""
    miss = chk.get("missing") or []
    if not miss:
        return ""
    return ("\n\n[이 질문에 대해 우리 데이터에 없는 것]\n"
            + "\n".join(f"- {x}" for x in miss[:4])
            + "\n위 항목은 **없다고 분명히 적고** 나머지만 데이터로 답하라. "
              "없는 항목을 추정·일반론으로 메우지 마라.")
