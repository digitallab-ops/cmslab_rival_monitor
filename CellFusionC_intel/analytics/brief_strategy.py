"""
브리핑 탭 전략 콘텐츠 생성 + 캐시 — 나라별 전략 해석 / 왜 잘나가나 / 브랜드 종합 / 주간 총평.

brand_insights 테이블을 재사용해 (from_date,to_date) 창 단위로 캐시(주1회성 자동 갱신,
날짜 롤링이라 하루 단위로 재생성). brand 컬럼에 네임스페이스 키를 넣어 종류를 구분한다:
  BS|{brand}|{cc}   전략 해석 한 문장(관찰→의도)
  BW|{brand}|{cc}   그 나라 주력제품이 왜 잘나가나
  BSUM|{brand}      브랜드 종합(펼치기 전 미리보기)
  BTONGP            주간 종합 총평(1건)
summary 컬럼에 텍스트를 저장. Render 빌드 중 미존재분만 생성(부분 갱신).

노이즈(골프·가품·M&A·소송·상장 등)는 LLM이 'SKIP' 반환 → 렌더러가 필터.
"""

import os
import logging
from collections import defaultdict

from analytics.queries import get_insights_cache, upsert_insight_cache

logger = logging.getLogger(__name__)

_MODEL_LINE = os.getenv("BRIEF_MODEL", "gpt-4o-mini")   # 전략해석·왜·종합
_MODEL_TONGP = os.getenv("INSIGHT_MODEL", "gpt-4o")     # 총평(품질)

_COUNTRY_KO = {
    "US": "미국", "JP": "일본", "KR": "한국", "CN": "중국", "GB": "영국", "PL": "폴란드",
    "SG": "싱가포르", "TH": "태국", "CA": "캐나다", "AU": "호주", "DE": "독일", "FR": "프랑스",
    "ID": "인도네시아", "MY": "말레이시아", "VN": "베트남", "PH": "필리핀", "IT": "이탈리아",
    "IN": "인도", "MX": "멕시코", "RU": "러시아", "ES": "스페인", "PT": "포르투갈", "KE": "케냐",
    "EU": "유럽", "UZ": "우즈베키스탄", "AE": "아랍에미리트", "UAE": "아랍에미리트", "BR": "브라질",
    "SA": "사우디", "NL": "네덜란드", "SE": "스웨덴", "TR": "튀르키예",
}
def _cty(c):
    c = (c or "").upper(); return _COUNTRY_KO.get(c, c)

TAC_KO = {"신시장_진출": "신흥시장 선점", "유통_채널": "유통망 확대", "신제품_런칭": "신제품 출시",
          "브랜드_마케팅": "브랜드 마케팅", "인플루언서_협업": "인플루언서", "가격_프로모션": "프로모션",
          "투자_BD": "투자·제휴"}

_NOISE = ("위조품", "가품", "소송", "피소", "고소", "M&A", "인수합병", "우승", "수상",
          "시상", "상장", "공시", "횡령", "발암")


def is_meaningful_line(v: str) -> bool:
    """전략 해석문이 실제 시장 전략인지(SKIP·노이즈 아님)."""
    if not v or v.strip().upper().startswith("SKIP"):
        return False
    return not any(k in v for k in _NOISE)


def _llm(model: str, prompt: str, max_tokens: int, temperature: float = 0.4) -> str:
    from openai import OpenAI
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    resp = client.chat.completions.create(
        model=model, max_tokens=max_tokens, temperature=temperature,
        messages=[{"role": "user", "content": prompt}],
    )
    return (resp.choices[0].message.content or "").strip()


def _p_strat(brand_ko, cc, rec):
    chs = list((rec.get("channels") or {}).keys())[:4]
    tac = [TAC_KO.get(t, t) for t in (rec.get("tactics") or {}).keys()]
    return f"""이 브랜드({brand_ko})가 {_cty(cc)}에서 이번 주 보인 움직임을 읽고, '전략 해석' 한 문장을 써라.
형식: [관찰된 구체적 사실]하며, [이 브랜드가 무엇을 노리는지 전략 해석]하려는 움직임으로 보인다.
- 60~80자, 한 문장. 반드시 이 브랜드({brand_ko})의 움직임으로 서술('K뷰티 브랜드들이'처럼 일반화 금지).
- 해석은 채널·수단에 근거해 구체적으로, 서로 다르게(대형유통 침투/앰배서더 팬덤/틈새채널 선점/카테고리 확장/신흥시장 선점/프리미엄 포지셔닝).
- '대중 인지도 확보' 같은 뻔한 표현 반복 금지.
- 톤: 관찰·추정형(~보인다/~읽힌다). 단정·과장·우리 회사 언급 금지.
- 아래면 시장전략과 무관 → 정확히 'SKIP'만: 실적/투자공시, 상장·M&A, 소송·분쟁, 가품·위조품, 스포츠선수·시상, 단순 브랜드나열.

헤드라인: {rec.get('headline')}
본문요지: {rec.get('summary') or '(없음)'}
판매채널: {', '.join(chs) or '(없음)'}
활동분류: {', '.join(tac) or '(없음)'}"""


def _p_why(cc, it, ns):
    return f"""아마존 {_cty(cc)}에서 {it.get('category','')} 카테고리 {it['rank']}위인 제품이 왜 그 나라에서 잘 팔리는지 한 줄(28~40자)로 설명해라.
- 근거는 아래 실제 지표에서만: 리뷰 {it.get('review_count') or 0:,}개, 별점 {it.get('rating')}, 순위 {it['rank']}위, 제품명 속 성분/기능.
- 리뷰 많으면 '검증된 스테디셀러', 상위 순위면 '카테고리 주력' 식으로 근거를 밝혀라. 리뷰 적으면 그렇게 부르지 마라.
- 추정형(~로 보인다). 없는 사실 지어내기 금지. 브랜드명 생략.
제품명: {it.get('product')}
관련 뉴스요지: {(ns or '(없음)')[:120]}"""


def _p_sum(brand_ko, strat_snips, prods):
    return f"""'{brand_ko}' 브랜드의 이번 주 글로벌 움직임을 1~2문장(55~85자)으로 종합해라.
- 어느 시장들에서 어떤 방식(대형유통 입점·앰배서더·신흥시장 선점 등)으로 움직이는지 큰 틀만.
- 펼치기 전 '미리보기'용이니 핵심만. 관찰형(~보인다). 우리 회사 언급·우열 단정 금지. 브랜드명으로 시작.
나라별 움직임: {' / '.join(strat_snips[:6])}
리테일 상위: {', '.join(prods) or '(없음)'}"""


def _p_tongp(n, topb, mktxt, strat_examples):
    return f"""K뷰티 경쟁 인텔리전스 주간 브리핑의 '종합 총평'을 3~4문장으로 써라.
- 이번 주 전반 흐름: 어느 시장이 급성장하고, 브랜드들이 대체로 어떤 방식(대형유통·앰배서더·신흥시장 선점 등)으로 움직이는지 큰 그림.
- 구체 브랜드/국가 1~2개 예시 언급. 관찰·추정형(~보인다). 우리 회사 언급·우열 단정 금지.
데이터: 신호 {n}건 / 주요 브랜드: {topb} / 급성장 시장: {mktxt}
브랜드 움직임 예시: {' | '.join(strat_examples[:8])}"""


def build_brief_strategy(session, records, rp, mkt, from_date, to_date, bko=None):
    """브리핑 전략 콘텐츠 생성/캐시. 반환: {strat, why, bsum, tongp}.
    bko: 영문브랜드→한글 변환 함수(없으면 원문)."""
    bko = bko or (lambda b: b)
    try:
        cache = get_insights_cache(session, from_date, to_date)
    except Exception:
        cache = {}
    cached = {k: v.get("summary", "") for k, v in cache.items()}
    dirty = False

    def _get_or_gen(key, gen_fn, model, max_tokens, temp=0.4):
        nonlocal dirty
        if key in cached:
            return cached[key]
        try:
            txt = _llm(model, gen_fn(), max_tokens, temp)
        except Exception as e:
            logger.warning("브리핑 전략 생성 실패 [%s]: %s", key, e)
            txt = ""
        if txt:
            try:
                upsert_insight_cache(session, key, from_date, to_date,
                                     {"summary": txt, "top_act": "brief", "top_pct": 0, "high_pct": 0.0})
                dirty = True
            except Exception:
                session.rollback()
        cached[key] = txt
        return txt

    by_brand = defaultdict(list)
    for r in records:
        by_brand[r["brand"]].append(r)

    # 1) 나라별 전략 해석(BS)
    strat = {}
    for r in records:
        k = f"BS|{r['brand']}|{r['country']}"
        strat[(r["brand"], r["country"])] = _get_or_gen(
            k, lambda r=r: _p_strat(bko(r["brand"]), r["country"], r), _MODEL_LINE, 160)

    # 활동 있는 브랜드(=의미있는 라인 1개+) 판정
    active = set()
    for (b, c), v in strat.items():
        if is_meaningful_line(v):
            active.add(b)

    # 2) 왜 잘나가나(BW) — 활동 브랜드의 리테일 리뷰100+ 상위 국가
    why = {}
    newsum = {}
    for r in records:
        kk = (r["brand"], r["country"])
        if len(r.get("summary") or "") > len(newsum.get(kk, "")):
            newsum[kk] = r.get("summary") or ""
    for b in active:
        bc = (rp.get(b) or {}).get("by_country") or {}
        solids = sorted([(cc, it) for cc, it in bc.items() if (it.get("review_count") or 0) >= 100],
                        key=lambda x: x[1]["rank"])[:4]
        for cc, it in solids:
            k = f"BW|{b}|{cc}"
            ns = newsum.get((b, cc)) or newsum.get((b, "US")) or ""
            why[(b, cc)] = _get_or_gen(k, lambda cc=cc, it=it, ns=ns: _p_why(cc, it, ns), _MODEL_LINE, 90, 0.3)

    # 3) 브랜드 종합(BSUM)
    bsum = {}
    for b in active:
        snips = [f"{_cty(r['country'])}: {strat[(b, r['country'])]}"
                 for r in sorted(by_brand[b], key=lambda x: -x.get("attn", 0))
                 if is_meaningful_line(strat.get((b, r["country"]), ""))]
        bc = (rp.get(b) or {}).get("by_country") or {}
        prods = [f"{_cty(cc)} 아마존 {it.get('category','')} {it['rank']}위"
                 for cc, it in sorted(bc.items(), key=lambda x: x[1]["rank"])
                 if (it.get("review_count") or 0) >= 100][:3]
        bsum[b] = _get_or_gen(f"BSUM|{b}", lambda b=b, snips=snips, prods=prods: _p_sum(bko(b), snips, prods),
                              _MODEL_LINE, 140)

    # 4) 주간 총평(BTONGP)
    mk_g = sorted([m for m in mkt if m.get("yoy_pct") and m["exp_usd_3m"] >= 8e6],
                  key=lambda z: -z["yoy_pct"])[:4]
    mktxt = ", ".join(f"{m['country_name']} +{m['yoy_pct']:.0f}%" for m in mk_g)
    topb = ", ".join(bko(b) for b in sorted(active, key=lambda b: -sum(r.get("attn", 0) for r in by_brand[b]))[:5])
    examples = [v for v in strat.values() if is_meaningful_line(v)]
    tongp = _get_or_gen("BTONGP", lambda: _p_tongp(len(records), topb, mktxt, examples),
                        _MODEL_TONGP, 320)

    return {"strat": strat, "why": why, "bsum": bsum, "tongp": tongp, "active": active}
