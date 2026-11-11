"""상표 판독 — 근거를 먼저 모으고, 그 근거로만 읽는다.

기존 판독(`summarizer.generate_trademark_reads`)은 상표명만 보고 LLM에 물었다.
그래서 모르는 걸 모른다고 하지 않고 지어냈다. 실측으로:

  Pation  `DAILY ANTI-CIPATION` → "일상적인 **안티에이징** 케어"
          anticipation(기대)을 쪼갠 말장난인데 'ANTI'만 보고 안티에이징이라 했다.
  Thome   `COMFRTHOME`          → "**홈 케어** 중심 스킨케어"
          Thome는 톰더글로우 — 뷰티 디바이스 브랜드다.
  Dr.G    DR.GOMGOM·DR.GOBI     → "**의사와 엄마의 비전**을 담은 제품 라인"
  Eum     COSMETICMUSEUM        → "뮤지엄 컨셉" (한 단어 보고)

고치는 방법은 모델을 키우는 게 아니다. 이건 추론력 문제가 아니라 **사실을
모르는** 문제다. DR.GOBI가 고비사막인지, AZ COMPLEX가 뭔지는 찾아봐야 안다.

그래서 순서를 뒤집는다 — **근거를 먼저 모으고, 근거에 없으면 '확인 필요'로 남긴다.**

근거 우선순위:
  1. 국내 지정상품 명세  출원인이 직접 쓴 1차 자료. 추측이 아니다.
  2. 우리 뉴스 코퍼스    이미 수집한 16,851건. 실측으로 단서어의 60%가 여기 있다.
  3. 네이버 검색        위 둘로도 모를 때만.

확신도는 **모델이 아니라 근거가 정한다**. 모델에게 "확신도를 매겨라"라고 하면
근거 없이도 '높음'이라고 쓴다.
"""

import os
import re
import logging
from datetime import date, timedelta

from sqlalchemy import text

from config.settings import DB_SCHEMA

logger = logging.getLogger(__name__)

_MODEL = os.getenv("TM_READ_MODEL", os.getenv("INSIGHT_MODEL_MARKET", "gpt-4o"))

# 상표명에 흔히 붙는 군더더기 — 분절 전에 떼어낸다.
_STOP = {"THE", "AND", "FOR", "BY", "OF", "WITH", "NEW", "PRO", "PLUS", "CO", "LAB",
         "INC", "LTD", "KOREA", "SEOUL", "COSMETIC", "COSMETICS", "BEAUTY"}

# 분절용 씨앗 어휘. 뉴스 코퍼스에서 뽑은 단어와 합쳐 쓴다.
_SEED_WORDS = {
    "GLOW", "SKIN", "DERMA", "CICA", "CENTELLA", "HEARTLEAF", "MUGWORT", "RICE",
    "SNAIL", "MUCIN", "PEPTIDE", "EXOSOME", "COLLAGEN", "RETINOL", "RETINAL",
    "NIACINAMIDE", "VITAMIN", "CERAMIDE", "HYALURON", "HYALURONIC", "PANTHENOL",
    "AMPOULE", "SERUM", "TONER", "CREAM", "ESSENCE", "CLEANSER", "CLEANSING",
    "MASK", "PATCH", "PAD", "SUN", "SUNSCREEN", "SUNSTICK", "SHIELD", "FENCE",
    "BARRIER", "REBOOT", "REBOOST", "BOOST", "BOOSTER", "REPAIR", "SOOTHE",
    "SOOTHING", "CALMING", "HYDRA", "HYDRATING", "MOIST", "MOISTURE", "WATER",
    "AQUA", "OIL", "BALM", "STICK", "GEL", "FOAM", "MILK", "LOTION", "MIST",
    "LIFT", "LIFTING", "FIRMING", "WRINKLE", "AGE", "ANTI", "BRIGHT", "TONE",
    "TONING", "WHITE", "CLEAR", "PORE", "ACNE", "TROUBLE", "SEBUM", "MILD",
    "GENTLE", "DEEP", "DAILY", "NIGHT", "MORNING", "FRESH", "COOL", "COOLING",
    "ICE", "WARM", "THERMA", "THERMO", "LASER", "LED", "DEVICE", "CARE",
    "HOME", "SALON", "CLINIC", "MEDI", "MEDIC", "DERM", "BIO", "PURE", "NATURAL",
    "NATURE", "GREEN", "TEA", "HONEY", "PROPOLIS", "CERA", "ZINC", "UV",
    "SHOT", "PULSE", "WAVE", "SONIC", "MASSAGE", "ROLLER", "GUASHA", "COMB",
    "BRUSH", "HAIR", "SCALP", "LIP", "EYE", "BODY", "HAND", "FOOT", "BABY",
    "KIDS", "MEN", "COMPLEX", "CAPSULE", "LAYER", "SMART", "RECHARGE",
    "SIGNATURE", "PREMIUM", "ORIGIN", "ORIGINAL", "FIRST", "MIRACLE", "MAGIC",
    "SECRET", "ANSWER", "SOLUTION", "THERAPY", "TREATMENT", "RESCUE", "RECOVERY",
    # 짧지만 업계에서 뜻이 분명한 말 — 코퍼스 4자 제한에 걸려 빠지므로 씨앗에 둔다.
    "POP", "DEW", "FIT", "BAR", "SPF", "PDRN", "EGF", "AHA", "BHA", "PHA",
    "EMS", "MTS", "RF", "LHA", "TXA", "SOS", "SPA", "SET", "KIT", "DUO",
}

# 혼자 나오면 안 되는 접두사. 'DAILY ANTI-CIPATION'의 ANTI를 단서로 내보내면
# 안티에이징으로 오독된다 — 뒤 조각과 붙여서 본다.
_PREFIXES = {"ANTI", "PRE", "POST", "NON", "ULTRA", "SUPER", "MULTI",
             "INTRA", "TRANS", "MICRO", "NANO", "HYPER", "RE", "DE"}

_vocab_cache: set = set()

# 코퍼스에서 끌어온 짧은 단어는 쓰레기가 많다 — 'VER'·'UNE'·'ARA'가 뉴스 본문에
# 있다고 분절에 쓰면 'VERANUA'가 'VER'+'ANUA'로 쪼개진다. 씨앗 어휘는 길이에
# 상관없이 쓰고(SUN·GEL·OIL·LED·PAD), 코퍼스 단어는 4자 이상만 쓴다.
_CORPUS_MIN = 4


def _vocab(session) -> set:
    """분절용 어휘 = 씨앗 + 우리 뉴스에 실제로 나온 영문 단어(4자 이상)."""
    global _vocab_cache
    if _vocab_cache:
        return _vocab_cache
    words = set(_SEED_WORDS)
    try:
        rows = session.execute(text(f"""
            SELECT COALESCE(product_name,'') || ' ' || COALESCE(title,'')
            FROM {DB_SCHEMA}.news_articles
            WHERE published_date >= CURRENT_DATE - INTERVAL '24 months'
            LIMIT 20000
        """)).fetchall()
        for (blob,) in rows:
            for t in re.findall(r"[A-Za-z]{%d,}" % _CORPUS_MIN, blob or ""):
                words.add(t.upper())
    except Exception as e:
        logger.debug("어휘 적재 실패(씨앗만 사용): %s", e)
    _vocab_cache = words
    return words


def split_mark(mark: str, brand_tokens: set, vocab: set) -> dict:
    """상표명을 쓸 수 있는 단서어로 쪼갠다.

    `ANUABARRIERREBOOST`처럼 붙여 쓴 상표가 많아 통째로 한 단어가 된다. 그러면
    BARRIER·REBOOST를 못 보고 '해석 불가'로 떨어진다 — 실측으로 외부 검색이
    필요하다고 본 24개 중 13개가 사실 이 전처리 문제였다.

    반환 {'words': [...], 'unknown': [...]} — unknown은 어휘에 없는 조각(조어).
    """
    words, unknown = [], []
    raw = [t for t in re.split(r"[^A-Za-z0-9가-힣]+", (mark or "").upper()) if t]
    # 쪼갠 조각 중 하나가 조어면 옆 조각과 붙여서 다시 본다. 'DAILY ANTI-CIPATION'을
    # 그대로 두면 'ANTI'가 단서로 나가 **안티에이징으로 오독**된다(실측 사고).
    # 붙이면 'ANTICIPATION' 한 덩어리가 되어 '모르는 말'로 정직하게 남는다.
    merged, i = [], 0
    while i < len(raw):
        cur = raw[i]
        if i + 1 < len(raw) and (
                cur in _PREFIXES
                or (not _splits(cur, vocab, brand_tokens)
                    and not _splits(raw[i + 1], vocab, brand_tokens))):
            cur = cur + raw[i + 1]
            i += 1
        merged.append(cur)
        i += 1

    for tok in merged:
        if tok in _STOP or len(tok) < 2:
            continue
        if re.search(r"[가-힣]", tok):
            if tok not in brand_tokens:
                words.append(tok)
            continue
        parts = _strip_and_split(tok, vocab, brand_tokens)
        if parts:
            words += [p for p in parts if p not in _STOP and p not in brand_tokens]
        elif tok not in brand_tokens:
            unknown.append(tok)
    # 순서 유지 중복 제거
    seen = set()
    words = [w for w in words if not (w in seen or seen.add(w))]
    seen = set()
    unknown = [w for w in unknown if not (w in seen or seen.add(w))]
    return {"words": words, "unknown": unknown}


def _strip_and_split(tok: str, vocab: set, brand_tokens: set) -> list:
    """브랜드 접두를 떼고 쪼갠다. **떼는 쪽이 더 잘 쪼개질 때만 뗀다.**

    무턱대고 떼면 망가진다 — Dr.G의 브랜드 토큰에 'G'가 있어서 `DR.GOMGOM`이
    'OMGOM'이 됐고, GlowM의 'GLOWM'을 떼서 `GLOWMARA`가 'ARA'가 됐다
    (바른 독법은 GLOW+MARA다). 그래서 뗀 것과 안 뗀 것을 둘 다 쪼개 보고
    더 나은 쪽을 쓴다. 한 글자 토큰은 아예 떼지 않는다.
    """
    best = _dp_split(tok, vocab)
    for bt in sorted((b for b in brand_tokens if len(b) >= 3), key=len, reverse=True):
        if len(tok) > len(bt) and tok.startswith(bt):
            alt = _dp_split(tok[len(bt):], vocab)
            if alt and (not best or len("".join(alt)) > len("".join(best))):
                best = alt
            break
    return best


def _splits(tok: str, vocab: set, brand_tokens: set) -> bool:
    return bool(_strip_and_split((tok or "").upper(), vocab, brand_tokens))


def _dp_split(tok: str, vocab: set, min_len: int = 3) -> list:
    """사전 기반 최장일치 분절. 전부 사전 단어로 쪼개질 때만 결과를 준다.

    일부만 쪼개지면 안 쪼갠 것으로 본다 — 억지로 쪼개면 'GOMGOM'이
    'GO'+'MGOM'처럼 엉뚱해지고, 그 조각으로 판독하면 더 틀린다.
    """
    n = len(tok)
    if n <= 4 and tok in vocab:
        return [tok]
    best = [None] * (n + 1)
    best[0] = []
    for i in range(1, n + 1):
        for j in range(max(0, i - 14), i):
            if best[j] is None:
                continue
            piece = tok[j:i]
            if len(piece) >= min_len and piece in vocab:
                cand = best[j] + [piece]
                if best[i] is None or len(cand) < len(best[i]):
                    best[i] = cand
    return best[n] or ([tok] if tok in vocab else [])


# ── 근거 수집 ────────────────────────────────────────────────────────────────

# 3·35류는 이 바닥의 기본값이고, 나머지는 '하던 일과 다른 일'이다.
# 29·30·32류(식품·음료)까지 실제로 나왔다 — 라벨이 없으면 "29류 29류"로 찍힌다.
CLASS_LABEL = {
    1: "화장품 원료·화학", 3: "화장품", 5: "의약·기능성", 9: "전자기기",
    10: "의료기기", 11: "가전·LED", 14: "장신구", 16: "인쇄물", 18: "가방",
    20: "가구·용기", 21: "미용도구", 24: "직물", 25: "의류", 28: "완구",
    29: "식품", 30: "식품·커피·차", 32: "음료", 33: "주류", 35: "판매·유통",
    38: "통신", 41: "교육·엔터", 42: "연구·소프트웨어", 43: "음식점·숙박",
    44: "미용·의료서비스",
}


# 3류(화장품)·35류(판매업)는 이 바닥의 기본값이다 — 실측 14개 브랜드 중 각각
# 14곳·11곳이 낸다. "화장품을 만들려 한다"는 결론은 아무것도 말해주지 않는다.
#
# 처음엔 '또래가 적게 쓰는 상품'으로 걸러봤는데, 지정상품 문구가 회사마다 달라
# (스킨케어제 / 스킨케어용 화장품 / 비의료용 스킨케어제) 거의 다 희소로 잡혔다.
# 그래서 특이의 정의를 둘로 좁혔다 — 둘 다 문구가 아니라 사실에 기댄다.
#   ① 류 이탈: 3·35류 밖은 '하던 일과 다른 일'이다(의료기기·전자·식품·의약).
#   ② 그 브랜드의 새 상품: 최근 _NEW_MONTHS 안에 처음 낸 3·35류 상품.
#      과거 출원이 없는 브랜드는 새것인지 알 수 없으므로 적용하지 않는다.
_BASELINE_CLASSES = {3, 35}
_NEW_MONTHS = 6
# 한 출원이 류 전체를 긁어 적는 일이 흔하다('살아있지 않은 식용벌레'까지 들어간다).
# 길게 보내면 그 나열이 상표명을 덮어버려서, 판독에 넘길 때만 앞쪽을 자른다.
_GOODS_CAP = 12


def _cls_int(*vals) -> int:
    for v in vals:
        digits = re.sub(r"[^0-9]", "", v or "")[:2]
        if digits:
            try:
                return int(digits)
            except ValueError:
                pass
    return 0


_prev_cache: dict = {}


def _class_prevalence(session, months: int) -> dict:
    """상품류 -> 그 류에 출원한 브랜드 수. 10류는 절반이 내지만 29류(식품)는
    한 곳뿐이다 — 드문 류가 더 큰 뉴스이므로 근거를 그 순서로 세운다."""
    hit = _prev_cache.get(months)
    if hit is not None:
        return hit
    try:
        rows = session.execute(text(f"""
            SELECT g.cls, COUNT(DISTINCT f.brand)
            FROM {DB_SCHEMA}.trademark_goods g
            JOIN {DB_SCHEMA}.trademark_filings f
              ON f.country = 'KR' AND f.app_number = g.app_number
            WHERE g.country = 'KR'
              AND f.app_date >= CURRENT_DATE - make_interval(months => :m)
            GROUP BY 1
        """), {"m": months}).fetchall()
    except Exception as e:
        logger.debug("류 집계 실패: %s", e)
        return {}
    out = {_cls_int(c): int(n) for c, n in rows}
    _prev_cache[months] = out
    return out


def goods_evidence(session, brand: str, months: int = 18, cap: int = 40) -> dict:
    """국내 지정상품 명세 — 가장 강한 근거(출원인이 직접 쓴 문서).

    중복이 많다(한 출원에 '마사지기'가 두 번 들어간다 — 실측 평균 43%가 중복).
    **저장은 원문 그대로 두고 여기서만 줄인다.** 요약을 LLM에 맡기면 뭘 빠뜨렸는지
    확인할 길이 없는데, 원문이 남아 있으면 언제든 맞춰볼 수 있다.

    근거는 **출원 한 건**을 한 줄로 묶는다. 상표명과 상품을 떼어놓으면
    '무쿠미(むくみ=부기)를 5·29·30·32류에 냈다'가 그냥 '식품 상품 목록'이 된다.
    """
    try:
        rows = session.execute(text(f"""
            SELECT f.app_number, f.app_date, f.cls_code, f.mark_name, g.cls, g.name_ko
            FROM {DB_SCHEMA}.trademark_filings f
            JOIN {DB_SCHEMA}.trademark_goods g
              ON g.country = 'KR' AND g.app_number = f.app_number
            WHERE f.country = 'KR' AND f.brand = :b
              AND f.app_date >= CURRENT_DATE - make_interval(months => :m)
            ORDER BY f.app_date DESC
        """), {"b": brand, "m": months}).fetchall()
    except Exception as e:
        logger.debug("지정상품 조회 실패(%s): %s", brand, e)
        return {"lines": [], "routine_lines": [], "classes": [], "distinct_classes": [],
                "n_filings": 0, "n_goods": 0, "n_distinct": 0, "n_routine": 0,
                "has_prior": False, "marks": []}

    cutoff = date.today() - timedelta(days=int(_NEW_MONTHS * 30.5))
    prior, recent = set(), set()
    for apn, ad, clsc, mark, gcls, ko in rows:
        if not ko:
            continue
        (recent if (ad and ad >= cutoff) else prior).add((_cls_int(gcls, clsc), ko))
    has_prior = bool(prior)

    prev = _class_prevalence(session, months)
    filings: dict = {}        # app_number -> {date, mark, cls -> [상품]}
    routine: dict = {}        # cls -> [상품]
    marks, seen = set(), set()
    n_routine = 0
    for apn, ad, clsc, mark, gcls, ko in rows:
        if not ko:
            continue
        c = _cls_int(gcls, clsc)
        key = (c, ko)
        if key in seen:
            continue
        seen.add(key)
        if c not in _BASELINE_CLASSES:
            rare = True                                   # ① 류 이탈
        else:
            rare = has_prior and key in recent and key not in prior   # ② 새 상품
        if mark:
            marks.add(mark)
        if not rare:
            routine.setdefault(c, []).append(ko)
            n_routine += 1
            continue
        f = filings.setdefault(apn, {"date": ad, "mark": mark or "", "cls": {}})
        f["cls"].setdefault(c, []).append(ko)

    def _lab(c):
        return f"{c}류 {CLASS_LABEL.get(c, '')}".strip()

    def _rank_cls(c: int) -> tuple:
        # 기본 류(3·35)는 맨 뒤로. 나머지는 '그 류에 출원한 브랜드가 적을수록' 앞.
        return (1 if c in _BASELINE_CLASSES else 0, prev.get(c, 1))

    lines = []
    for apn, f in sorted(filings.items(),
                         key=lambda kv: (min(_rank_cls(c) for c in kv[1]["cls"]),
                                         str(kv[1]["date"] or ""))):
        cls_sorted = sorted(f["cls"], key=_rank_cls)
        new_tag = " · 최근 처음 낸 것" if all(c in _BASELINE_CLASSES for c in cls_sorted) else ""
        items, room = [], cap
        for c in cls_sorted:
            take = f["cls"][c][:max(1, room)]
            items.append(f"{_lab(c)}: " + ", ".join(take))
            room -= len(take)
            if room <= 0:
                break
        head_txt = f"{f['date'] or ''} 「{f['mark'] or '(도형상표)'}」"
        lines.append(f"{head_txt} [{' + '.join(_lab(c) for c in cls_sorted)}{new_tag}] "
                     + " / ".join(items))

    routine_lines = []
    for c, items in sorted(routine.items(), key=lambda kv: -len(kv[1])):
        routine_lines.append(f"[{_lab(c)}] " + ", ".join(items[:cap]))

    distinct_classes = sorted({c for f in filings.values() for c in f["cls"]})
    return {"lines": lines, "routine_lines": routine_lines,
            "classes": sorted(set(distinct_classes) | set(routine)),
            "distinct_classes": distinct_classes,
            "n_filings": len({r[0] for r in rows}),
            "n_goods": len(seen), "n_distinct": len(seen) - n_routine,
            "n_routine": n_routine, "has_prior": has_prior,
            "marks": sorted(marks)}


def corpus_evidence(session, terms: list, brand: str, limit: int = 6) -> list:
    """우리 뉴스에서 그 단어가 실제로 쓰인 문장. 외부 검색보다 먼저 본다."""
    out = []
    for t in terms[:8]:
        if len(t) < 3:
            continue
        try:
            rows = session.execute(text(f"""
                SELECT title, COALESCE(product_name,''), published_date
                FROM {DB_SCHEMA}.news_articles
                WHERE (title ILIKE :p OR product_name ILIKE :p OR article_body ILIKE :p)
                  AND published_date >= CURRENT_DATE - INTERVAL '24 months'
                ORDER BY (brand = :b) DESC, published_date DESC
                LIMIT 2
            """), {"p": f"%{t}%", "b": brand}).fetchall()
        except Exception:
            continue
        for ti, pn, pd in rows:
            out.append(f"[뉴스 {pd}] {t} — {(pn or ti or '')[:90]}")
        if len(out) >= limit:
            break
    return out[:limit]


def web_evidence(terms: list, brand_label: str, limit: int = 6) -> list:
    """위 둘로도 모르는 조어만 검색한다. 호출을 아끼려 상위 3개만."""
    if not terms:
        return []
    try:
        from signals.web_verify import search as wsearch
    except Exception as e:
        logger.debug("검색 모듈 없음: %s", e)
        return []
    out = []
    for t in terms[:3]:
        try:
            out += wsearch(f"{brand_label} {t}", kinds=("webkr", "news"), each=2)
        except Exception as e:
            logger.debug("검색 실패(%s): %s", t, e)
        if len(out) >= limit:
            break
    return out[:limit]


# ── 판독 ─────────────────────────────────────────────────────────────────────

_PROMPT = """당신은 씨엠에스랩(더마 선케어 '셀퓨전씨')의 경쟁 인텔리전스 분석가입니다.
아래는 경쟁 브랜드 **{label}**의 최근 상표 출원과, 그 뜻을 확인하려고 모은 근거입니다.

[최근 상표명]
{marks}

[지정상품 — 출원인이 특허청에 직접 써낸 1차 자료]
{goods}

[참고 — 뉴스·검색(보조 설명용)]
{notes}

이 브랜드가 **무엇을 만들려 하는지** 한 줄로 읽어주세요.

규칙:
- **근거에 있는 내용만 쓰세요.** 근거에 없는 성분·카테고리·컨셉을 지어내지 마세요.
- 지정상품 명세가 있으면 그게 가장 확실한 근거입니다 — 출원인이 직접 쓴 문서입니다.
- **[지정상품]이 비어 있지 않으면 결론은 거기서 나와야 합니다.** [참고]의 뉴스는
  성분명·제품명을 거드는 용도일 뿐, 그것만으로 결론을 쓰면 안 됩니다.
- 지정상품 줄은 「상표명」[상품류] 상품... 꼴입니다. **상표명과 상품류를 묶어서**
  읽으세요 — 「MUKUMI」가 5류(건기식)·29·30·32류(식품·음료)에 걸쳐 있으면
  '먹는 제품으로 넘어간다'는 뜻입니다.
- **[지정상품]은 평소와 다른 것만 추려 보낸 것입니다.** 그 브랜드가 늘 내던
  일반 화장품·판매업 출원은 빼고 보냈습니다. 그러니 "화장품을 만들려 한다",
  "스킨케어 제품을 개발한다" 같은, 화장품 회사면 다 해당되는 문장은 **금지**입니다.
  읽는 사람이 **이 브랜드만의 움직임**으로 받아들일 내용을 쓰세요.
- 류가 말해주는 것: 10류=의료기기·홈디바이스, 9류=앱·소프트웨어, 11류=가전,
  5류=의약·건강기능식품, 21류=미용도구, 1류=화장품 원료, 29·30·32류=식품·음료,
  44류=병원·약국 등 서비스업. 3류·35류 줄이 올라왔다면 그건 **최근에 처음 낸**
  품목이라는 뜻이니, 늘 하던 것이 아니라 새로 손댄 쪽을 짚으세요.
- 상표명을 우리말로 옮겨 적기만 한 판독은 금지합니다. 'SKINREADY'를 '스킨케어 준비
  제품군'이라고 쓰는 식은 아무 정보가 없습니다. 여러 단서를 **묶어서** 방향을 쓰세요.
- 근거로 읽히는 **제품군·성분·폼팩터**가 있으면 그 단어를 그대로 넣으세요.
- 근거가 모자라면 **지어내지 말고** read를 빈 문자열("")로 두세요. 그게 정답입니다.
- 브랜드명은 화면에 따로 나오니 판독문에 다시 쓰지 말고, 방향(동사구)으로 시작하세요.
- 45자 내외, 한국어.
- basis에는 판독의 근거가 된 문구를 근거에서 **그대로 인용**해 넣으세요(30자 내외).

JSON만: {{"read":"...", "basis":"..."}}"""


def _llm(prompt: str, max_tokens: int = 300) -> dict:
    import json
    from openai import OpenAI
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    resp = client.chat.completions.create(
        model=_MODEL, max_tokens=max_tokens, temperature=0.2,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": prompt}],
    )
    return json.loads(resp.choices[0].message.content or "{}")


def read_brand(session, brand: str, label: str, marks: list,
               brand_tokens: set, use_web: bool = True) -> dict:
    """한 브랜드의 상표를 판독. 확신도는 **근거가 정한다**(모델이 아니라)."""
    vocab = _vocab(session)
    words, unknown = [], []
    for m in marks[:20]:
        sp = split_mark(m, brand_tokens, vocab)
        words += sp["words"]
        unknown += sp["unknown"]
    words = list(dict.fromkeys(words))
    unknown = list(dict.fromkeys(unknown))

    g = goods_evidence(session, brand, cap=_GOODS_CAP)
    notes = corpus_evidence(session, words + unknown, brand)
    if use_web and unknown and not g["lines"]:
        notes += web_evidence(unknown, label)
    ev = list(g["lines"]) + notes

    # 확신도 — 근거의 종류로 결정한다.
    if g["lines"]:
        conf, basis_kind = "high", f"국내 지정상품 {g['n_distinct']}종({g['n_filings']}건 출원)"
    elif g["n_goods"]:
        # 3류 화장품만 잔뜩 낸 건 '화장품을 만들려 한다'는 뜻밖에 없다. 그런 문장은
        # 쓰는 쪽도 읽는 쪽도 시간 낭비라, 결론을 만들지 않고 접는다(LLM도 안 부른다).
        return {"read": "", "confidence": "routine", "basis": "", "routine": True,
                "basis_kind": f"일반 화장품·판매업 출원만 {g['n_routine']}종 — 평소와 같음",
                "classes": g["classes"], "unknown": unknown, "needs_check": False}
    elif ev:
        conf, basis_kind = "medium", f"뉴스·검색 {len(ev)}건"
    else:
        return {"read": "", "confidence": "none", "basis": "", "basis_kind": "근거 없음",
                "classes": [], "unknown": unknown, "needs_check": True, "routine": False}

    try:
        out = _llm(_PROMPT.format(
            label=label, marks=", ".join(marks[:12]) or "(이름 없는 도형상표)",
            goods="\n".join(g["lines"][:16]) or "(없음)",
            notes="\n".join(notes[:6]) or "(없음)"))
    except Exception as e:
        logger.warning("상표 판독 실패(%s): %s", brand, e)
        return {"read": "", "confidence": "none", "basis": "", "basis_kind": f"판독 실패: {e}",
                "classes": g["classes"], "unknown": unknown, "needs_check": True, "routine": False}

    read = (out.get("read") or "").strip()
    basis = (out.get("basis") or "").strip()
    if not read:
        return {"read": "", "confidence": "none", "basis": "", "basis_kind": basis_kind,
                "classes": g["classes"], "unknown": unknown, "needs_check": True, "routine": False}
    return {"read": read, "confidence": conf, "basis": basis, "basis_kind": basis_kind,
            "classes": g["classes"], "unknown": unknown, "needs_check": False, "routine": False}


def read_all(session, brand_marks: dict, labels: dict = None,
             tokens: dict = None, use_web: bool = True) -> dict:
    """{brand: [상표명...]} → {brand: {read, confidence, basis, ...}}."""
    labels = labels or {}
    tokens = tokens or {}
    out = {}
    for b, marks in (brand_marks or {}).items():
        bt = tokens.get(b) or {t for t in re.split(r"[^A-Za-z0-9가-힣]+", (b or "").upper()) if t}
        try:
            out[b] = read_brand(session, b, labels.get(b, b), marks or [], bt, use_web=use_web)
        except Exception as e:
            logger.warning("판독 예외(%s): %s", b, e, exc_info=True)
            out[b] = {"read": "", "confidence": "none", "basis": "",
                      "basis_kind": f"예외: {type(e).__name__}", "classes": [],
                      "unknown": [], "needs_check": True}
    return out
