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
    "HK": "홍콩", "TW": "대만", "UK": "영국", "GT": "과테말라", "LA": "라오스",
    "KZ": "카자흐스탄", "BY": "벨라루스", "ZA": "남아공",
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
    return f"""이 브랜드({brand_ko})가 {_cty(cc)}에서 이번 주 보인 움직임을 '전략 해석' 1~2문장으로 써라.
- 반드시 **구체적 사실**을 담아라: 채널명·제품·수치·행사 등(아래 본문요지에서 근거). 뭉뚱그리기 금지.
- 그리고 '그게 무슨 의미인지/왜 주목할 만한지'를 한 마디. 단 아래 **상투어는 절대 금지**(성의없어 보임):
  '입지 강화', '프리미엄 포지셔닝 강화', '인지도 확보', '존재감 강화', '확장하려는 움직임', '경쟁력 강화', '주목된다'.
- 매번 다른 각도로: 대형유통 침투 / 앰배서더·인플루언서 팬덤 / 틈새·면세·팝업 채널 / 신카테고리 진입 /
  신흥시장 선점 / 가격 공세 / 성분·기능 차별화 등.
- 60~95자. 이 브랜드의 움직임으로만 서술('K뷰티 브랜드들이'처럼 일반화 금지). 관찰·추정형(~보인다/~읽힌다).
  단정·과장·우리 회사 언급 금지.
- 아래면 시장전략과 무관 → 정확히 'SKIP'만: 실적/투자·펀드·지분·상장·M&A·합병, 골프·스포츠·시상,
  소송·가품·분쟁, 단순 브랜드 나열.

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
    """'이번 주의 한 방' — 뾰족한 헤드라인 + 근거 한 줄. 슬랙·대시보드 공용(단일 출처)."""
    return f"""K뷰티 경쟁 인텔리전스 브리핑의 '이번 주의 한 방'을 딱 2문장(합쳐 90자 이내)으로 써라.
- **브랜드명·수치를 하나도 쓰지 마라**(개별 브랜드는 목록에서 따로 보여주므로 겹치면 안 됨).
- 첫 문장 = 이번 주를 규정하는 뾰족한 헤드라인 한 방(35자 이내, 단정적).
  예: '이번 주 진짜 뉴스는 브랜드가 아니라 채널이다' / 'K뷰티가 아마존 밖으로 나가기 시작했다' /
      '팝업이 신제품을 이긴 한 주' / '틱톡숍이 새 격전지가 됐다'
- 둘째 문장 = 그게 지금 왜 중요한지 근거 한 줄(55자 이내).
- 금지어(상투어): '다변화', '모색', '전환점', '입지 강화', '시장 점유율 확대', '긍정적', '기대'.
  예측·당위 금지, 관찰형(~보인다/~읽힌다). 우리 회사 언급·우열 단정 금지.
데이터: 신호 {n}건 / 주요 브랜드: {topb} / 급성장 시장: {mktxt}
브랜드 움직임: {' | '.join(strat_examples[:8])}"""


def _p_watch(strat_examples, mktxt):
    """'지켜볼 것' — 다음에 판가름날 지점(내일 또 열어보게 하는 훅). 줄바꿈 구분."""
    return f"""아래 K뷰티 경쟁 브랜드 움직임 중 '다음에 판가름날' 지점 2개를 뽑아라.
- 각 한 줄(30~45자), 궁금증을 유발하게. 브랜드명 포함 OK.
  예: '스킨1004 일본 팝업이 실제 판매로 이어지는지' / '메디큐브 틱톡숍 매출이 지속되는지'
- 예측·단정 금지. 확인 관점으로. 우리 회사 언급 금지.
- 출력은 딱 2줄, 각 줄에 하나씩. 번호·불릿·따옴표 없이 문장만.
브랜드 움직임: {' | '.join(strat_examples[:8])}
급성장 시장: {mktxt}"""


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

    # 4) '이번 주의 한 방'(BTONGP) + '지켜볼 것'(BWATCH) — 슬랙·대시보드 공용 단일 출처
    mk_g = sorted([m for m in mkt if m.get("yoy_pct") and m["exp_usd_3m"] >= 8e6],
                  key=lambda z: -z["yoy_pct"])[:4]
    mktxt = ", ".join(f"{m['country_name']} +{m['yoy_pct']:.0f}%" for m in mk_g)
    topb = ", ".join(bko(b) for b in sorted(active, key=lambda b: -sum(r.get("attn", 0) for r in by_brand[b]))[:5])
    examples = [v for v in strat.values() if is_meaningful_line(v)]
    tongp = _get_or_gen("BTONGP", lambda: _p_tongp(len(records), topb, mktxt, examples),
                        _MODEL_TONGP, 200)
    watch_raw = _get_or_gen("BWATCH", lambda: _p_watch(examples, mktxt), _MODEL_TONGP, 200)
    watch = [w.strip(" -•\t") for w in (watch_raw or "").split("\n") if w.strip()][:2]

    return {"strat": strat, "why": why, "bsum": bsum, "tongp": tongp,
            "watch": watch, "active": active}


# ── 발표 → 안착 매칭(LLM) ────────────────────────────────────────────────────
# 한글 발표명과 영문 아마존명은 음차 관계라 문자열 매칭이 불가(레티날 샷=Retinal Shot).
# 실험 결과 mid 신뢰도는 오매칭(마이크로니들링 세럼→Retinal Shot)이라 high만 채택한다.
def _p_match(brand_ko, announced, retail_names):
    return f"""'{brand_ko}'의 발표 신제품명(한글)과 아마존 실제 판매 제품명(영문)을 대조해 같은 제품끼리 짝지어라.
한글은 영문의 음차인 경우가 많다(예: 레티날 샷 = Retinal Shot, 마데카 크림 = Madeca Cream).
- **확실한 것만**. 성분·제형이 다르면 짝짓지 마라(마이크로니들링 세럼 ≠ Retinal Shot).
- 애매하면 아예 넣지 마라. 없으면 빈 배열.
[발표] {announced}
[리테일] {retail_names}
JSON만: {{"matches":[{{"announced":"...","retail":"...","confidence":"high|mid"}}]}}"""


# 제형·부위가 다르면 같은 제품일 수 없다 — LLM이 high로 붙여도 결정적으로 걸러낸다
# (실측 오매칭: 'PDRN 아이크림' → 'PDRN Moisturizing Cream'은 아이크림≠페이스크림).
_TYPE_GUARD = [
    ("eye", ("아이크림", "아이 크림", "아이패치", "아이 패치", "eye ", "eye_", "eye cream", "eye patch")),
    ("sun", ("선크림", "선스틱", "썬크림", "자외선", "sunscreen", "sunstick", "sun ", "spf")),
    ("mask", ("마스크", "시트팩", "mask", "sheet", "マスク", "パック")),
    ("toner", ("토너", "패드", "toner", "pad")),
    ("serum", ("세럼", "앰플", "serum", "ampoule", "essence", "セラム", "美容液")),
    ("cream", ("크림", "cream", "moisturizer", "balm", "로션", "lotion", "クリーム")),
    ("cleanser", ("클렌저", "클렌징", "폼", "cleanser", "cleansing", "foam")),
]
# 부위·용도가 특정되는 유형 — 한쪽에만 있으면 다른 제품으로 본다(아이크림 vs 페이스크림).
_EXCLUSIVE_TYPES = ("eye", "sun", "mask", "cleanser")


def _type_conflict(announced: str, retail: str) -> bool:
    """발표명과 리테일명의 제형/부위가 어긋나면 True(매칭 기각).

    실측 오매칭 차단: 'PDRN 아이크림'→'PDRN Moisturizing Cream'(아이≠페이스),
    '레티날 세럼'→'Retinal Booster Cream'(세럼≠크림).
    """
    a, r = (announced or "").lower(), (retail or "").lower()
    at = {t for t, kws in _TYPE_GUARD if any(k in a for k in kws)}
    rt = {t for t, kws in _TYPE_GUARD if any(k in r for k in kws)}
    if not at or not rt:
        return False              # 한쪽이 분류 불가(미커버 언어 등) → 판단 보류
    # 부위 특정 유형은 양쪽이 일치해야 함(한쪽만 아이크림 → 기각)
    for t in _EXCLUSIVE_TYPES:
        if (t in at) != (t in rt):
            return True
    return not (at & rt)          # 공통 유형이 하나도 없으면 다른 제품


def build_launch_hits(session, candidates, from_date, to_date, bko=None):
    """발표→안착 사례를 LLM 매칭으로 확정(브랜드당 1콜, high만). brand_insights에 캐시.
    반환: [{brand, announced, product, country, category, rank, reviews}]"""
    import json as _json
    bko = bko or (lambda b: b)
    try:
        cache = get_insights_cache(session, from_date, to_date)
    except Exception:
        cache = {}
    cached = {k: v.get("summary", "") for k, v in cache.items()}

    hits = []
    for c in candidates:
        brand = c["brand"]
        ann = sorted(set(c["announced"]))[:12]
        ret = c["retail"][:15]
        ret_names = [r["product"][:70] for r in ret]
        if not ann or not ret_names:
            continue
        key = f"BHIT|{brand}"
        raw = cached.get(key)
        if raw is None:
            try:
                raw = _llm(_MODEL_LINE, _p_match(bko(brand), ann, ret_names), 500, 0.0)
            except Exception as e:
                logger.warning("발표-안착 매칭 실패 [%s]: %s", brand, e)
                raw = ""
            if raw:
                try:
                    upsert_insight_cache(session, key, from_date, to_date,
                                         {"summary": raw, "top_act": "hit", "top_pct": 0, "high_pct": 0.0})
                except Exception:
                    session.rollback()
            cached[key] = raw
        if not raw:
            continue
        try:
            data = _json.loads(raw[raw.find("{"):raw.rfind("}") + 1] or "{}")
        except Exception:
            continue
        for m in (data.get("matches") or []):
            if (m.get("confidence") or "").lower() != "high":
                continue          # mid는 오매칭 실증 — 채택 안 함
            rname = m.get("retail") or ""
            row = next((r for r in ret if r["product"][:70] == rname[:70]), None)
            if not row:
                row = next((r for r in ret if rname[:28] and rname[:28] in r["product"]), None)
            if not row:
                continue
            if _type_conflict(m.get("announced", ""), row["product"]):
                logger.debug("제형 불일치로 매칭 기각: %s ↔ %s", m.get("announced"), row["product"][:40])
                continue
            hits.append({"brand": brand, "announced": m.get("announced", ""),
                         "product": row["product"], "country": row["country"],
                         "category": row["category"], "rank": row["rank"],
                         "reviews": row["reviews"]})
    # 브랜드×제품 중복 제거(순위 좋은 것 우선)
    seen, out = set(), []
    for h in sorted(hits, key=lambda x: (x["rank"] or 999)):
        k = (h["brand"], h["product"][:40])
        if k in seen:
            continue
        seen.add(k)
        out.append(h)
    return out
