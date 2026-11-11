"""그래서 우리는 — 상표 판독을 '대응'과 '다음 먹거리'로 잇는 결론층.

`trademark_read`는 브랜드 하나가 **뭘 만들려 하는지**까지만 읽는다. 거기서 끊으면
받는 사람은 "그래서?"를 스스로 메워야 한다. 이 모듈이 두 걸음을 더 간다.

  ① 우리 대응   — 그 움직임에 셀퓨전씨가 뭘 할 수 있나 (브랜드별 한 줄)
  ② 다음 먹거리 — 여러 경쟁사가 **동시에 같은 쪽으로** 움직이면 그게 시장 신호다

②가 이 모듈의 핵심이다. 한 브랜드가 10류(의료기기)를 내면 그 브랜드 이야기지만,
여섯 곳이 같은 분기에 10류를 내면 그건 업계가 옮겨가는 중이라는 뜻이다. 그래서
류를 **축(axis)으로 묶어 세고**, 세는 일은 LLM에 맡기지 않는다 — 숫자는 틀리면
안 되고, 틀렸는지 확인할 수 있어야 한다.

세 가지를 조심했다.

  · **3·35류는 축에서 뺀다.** 화장품(3류)과 판매업(35류)은 이 바닥의 기본값이라
    여덟 곳이 냈어도 "화장품 회사가 화장품을 낸다"는 뜻밖에 없다. 수렴으로 세면
    항상 1위로 올라와 진짜 신호를 덮는다.
  · **분모를 숨기지 않는다.** 국내 상표는 아직 전수 수집 중이다(백필 진행형).
    "6곳이 디바이스로"만 쓰면 58곳 중 6곳으로 읽힌다. 수집이 끝난 브랜드 수를
    분모로 같이 들고 다니고, 화면과 프롬프트 양쪽에 적는다.
  · **근거가 없으면 만들지 않는다.** 특이 신호가 하나도 없으면 빈 결과를 낸다.
"""

import os
import re
import json
import logging
from collections import defaultdict

from sqlalchemy import text

from config.settings import DB_SCHEMA
from analytics.trademark_read import goods_evidence, CLASS_LABEL

logger = logging.getLogger(__name__)

_MODEL = os.getenv("NEXT_BET_MODEL", os.getenv("INSIGHT_MODEL_MARKET", "gpt-4o"))

# 기본 류 — 어느 화장품 회사나 내는 것이라 '움직임'으로 세지 않는다.
BASELINE_CLASSES = {3, 35}

# 류를 사람이 말하는 단위로 묶는다. 서로 겹치지 않게 둔다 — 같은 류가 두 축에
# 들어가면 "7곳이 기기로, 7곳이 앱으로"처럼 같은 브랜드를 두 번 세게 된다.
AXES = [
    ("device",   "뷰티 디바이스", {9, 10, 11},
     "집에서 쓰는 기기 — 피부측정기·마사지기·LED·고주파, 그리고 그걸 제어하는 앱"),
    ("inner",    "이너뷰티·먹는 것", {5, 29, 30, 32, 33},
     "건강기능식품·음료·식품 — 바르는 것에서 먹는 것으로"),
    ("tool",     "미용 도구", {21},
     "브러시·스펀지·빗 같은 전자가 아닌 도구"),
    ("material", "원료·소재", {1},
     "남의 회사에 파는 화장품 원료 — B2B로 가는 길"),
    ("service",  "서비스·공간", {38, 41, 42, 43, 44},
     "약국·클리닉·피부관리실·체험공간, 연구개발·플랫폼 등 서비스업"),
    ("goods",    "잡화·의류", {14, 16, 18, 20, 24, 25, 28},
     "굿즈·패션 — 브랜드를 제품 밖으로 넓히는 쪽"),
]
_AXIS_OF = {c: key for key, _, cls, _ in AXES for c in cls}
_AXIS_NAME = {key: name for key, name, _, _ in AXES}
_AXIS_DESC = {key: desc for key, _, _, desc in AXES}


def collected_brands(session) -> list:
    """국내 상표를 실제로 받아온 브랜드. 아직 차례가 안 온 브랜드와 구별해야
    분모를 정직하게 쓸 수 있다(백필이 끝나기 전까지 분모는 계속 늘어난다)."""
    try:
        rows = session.execute(text(f"""
            SELECT DISTINCT brand FROM {DB_SCHEMA}.trademark_filings
            WHERE country = 'KR' ORDER BY brand
        """)).fetchall()
        return [r[0] for r in rows]
    except Exception as e:
        logger.warning("국내 상표 수집 브랜드 조회 실패: %s", e)
        return []


def brand_axes(session, brands: list = None) -> dict:
    """브랜드별로 '평소와 다른' 출원이 어느 축에 걸렸나.

    반환 {brand: {"axes": {axis: [cls...]}, "classes": [...], "lines": [...],
                  "marks": [...], "n_distinct": int}}
    특이 신호가 없는 브랜드는 아예 담지 않는다.
    """
    out = {}
    for b in (brands if brands is not None else collected_brands(session)):
        try:
            g = goods_evidence(session, b)
        except Exception as e:
            logger.debug("축 집계 실패(%s): %s", b, e)
            continue
        if not g["lines"]:
            continue
        axes = defaultdict(list)
        for c in g["distinct_classes"]:
            if c in BASELINE_CLASSES:
                continue
            ax = _AXIS_OF.get(c)
            if ax:
                axes[ax].append(c)
            else:
                # 축에 없는 류는 집계에서 통째로 사라진다. 조용히 흘리면 새 류가
                # 올라와도 아무도 모른다 — 로그로 남겨 AXES에 추가할 수 있게 한다.
                logger.warning("축 미정의 상품류 %d류(%s) — %s의 출원이 "
                               "'다음 먹거리' 집계에서 빠집니다. AXES에 추가하세요.",
                               c, CLASS_LABEL.get(c, "?"), b)
        if not axes:
            # 3·35류에서 '처음 낸 품목'만 잡힌 경우다. 브랜드 판독에는 쓰이지만
            # 먹거리 축으로는 세지 않는다 — 기본 류라 방향을 말해주지 않는다.
            continue
        out[b] = {"axes": {k: sorted(v) for k, v in axes.items()},
                  "classes": g["distinct_classes"], "lines": g["lines"][:3],
                  "marks": g["marks"][:8], "n_distinct": g["n_distinct"]}
    return out


def converging(session, brands: list = None) -> dict:
    """축별로 몇 개 브랜드가 동시에 움직이나. 숫자는 LLM이 아니라 여기서 센다.

    반환 {"n_collected": int, "axes": [{key,name,desc,brands,classes,n}],
          "classes": [{cls,label,brands,n}]}
    """
    cols = brands if brands is not None else collected_brands(session)
    per = brand_axes(session, cols)

    by_axis = defaultdict(lambda: {"brands": [], "classes": set()})
    by_cls = defaultdict(list)
    for b, info in per.items():
        for ax, clss in info["axes"].items():
            by_axis[ax]["brands"].append(b)
            by_axis[ax]["classes"].update(clss)
            for c in clss:
                by_cls[c].append(b)

    axes = [{"key": k, "name": _AXIS_NAME[k], "desc": _AXIS_DESC[k],
             "brands": sorted(v["brands"]), "classes": sorted(v["classes"]),
             "n": len(v["brands"])}
            for k, v in by_axis.items()]
    axes.sort(key=lambda a: (-a["n"], a["key"]))
    classes = [{"cls": c, "label": f"{c}류 {CLASS_LABEL.get(c, '')}".strip(),
                "brands": sorted(bs), "n": len(bs)}
               for c, bs in by_cls.items()]
    classes.sort(key=lambda x: (-x["n"], x["cls"]))
    return {"n_collected": len(cols), "n_moving": len(per),
            "axes": axes, "classes": classes, "per_brand": per}


# ── LLM ──────────────────────────────────────────────────────────────────────

_P_ACTION = """당신은 씨엠에스랩(더마 선케어 브랜드 '셀퓨전씨' 운영)의 경쟁 인텔리전스 분석가입니다.
아래는 경쟁 브랜드들이 **특허청에 직접 써낸 상표 지정상품**에서 읽어낸 움직임입니다.
추측 기사가 아니라 1차 자료입니다.

{blocks}

{profile}

각 브랜드마다 **"그래서 우리(셀퓨전씨)가 할 것"**을 딱 한 문장(한국어, 55자 이내)으로 쓰세요.

**반드시 아래 {n}개 브랜드 전부를 출력하세요. 하나도 빠뜨리지 마세요.**
{keys}

- **브랜드마다 다른 문장을 쓰세요.** 두 브랜드에 같은 말이 들어가면 틀린 것입니다.
  각 브랜드의 근거에 적힌 **구체적인 상표명·상품명·제품군**을 집어서 쓰세요.
  나쁨: '의료용 피부관리기구에 맞대응해볼 만하다' (어느 브랜드에나 붙는다)
  좋음 **형식**: '<그 브랜드의 실제 상표명> 같은 <제품군>에 <우리 강점>을 ~하는 쪽을 고려'
  ⚠️ 위는 형식 예시일 뿐입니다. 꺾쇠 안은 **그 브랜드의 근거에 실제로 적힌 말**로
  채우세요. 예시 문구를 그대로 베껴 쓰면 틀린 답입니다.
- 한국어 문법을 지키세요. 조사가 어긋난 문장('~기능을 점검이 필요해 보인다')은 틀린 답입니다.
- **문장 틀도 돌려 쓰세요.** 전부 '~을 연계해볼 만하다'처럼 같은 꼴로 끝나면
  읽는 사람에겐 한 문장과 같습니다. 브랜드마다 **다른 수(手)**를 쓰세요 —
  선점 / 대체 / 방어 / 성분 검증 / 가격대 비교 / 채널 선점 / 임상 근거 강조 등.
- 그 브랜드가 **열고 있는 카테고리**에 대한 우리 수(手)를 쓰세요. 우리 강점은
  임상 더마·선케어·자외선차단·PDRN·시카·배리어입니다.
- ⚠️ 이들은 **경쟁사**입니다. "○○와 협업", "○○ 유통망 활용"처럼 손잡는 액션은 금지.
  우리가 **독자적으로** 선점·대체·맞대응하는 액션만 쓰세요.
- 근거에 없는 사실을 지어내지 마세요. 움직임에 적힌 카테고리·제품군·성분만 씁니다.
- ⚠️ **나라·시장을 언급하지 마세요.** 위 자료는 전부 한국 특허청 출원이라
  어느 나라에서 팔지에 대한 정보가 없습니다. '미국에서 선점' 같은 말은 창작입니다.
- **반드시 조언형 어미로 끝내세요** — '~해볼 만하다', '~선점을 고려', '~점검이 필요해 보인다',
  '~쪽이 있다', '~검토해볼 만하다'.
  ❌ **절대 금지**: '~해야 한다', '~하라', '~하자', '~할 것이다', '~하면 된다', '~필요하다'.
- 금지 상투어(정보량 0): '입지를 강화', '존재감을 높이', '경쟁력 강화', '다변화',
  '시장 점유율 확대', '주목할 필요', '모니터링이 필요'. 어느 브랜드에 붙여도 맞는 말입니다.
- 정말로 우리가 할 게 없으면 빈 문자열("")로 두세요. 억지로 채우지는 마세요.

반드시 JSON만: {{"actions": [{{"brand": "<영문 브랜드키 그대로>", "action": "..."}}, ...]}}"""


_P_BET = """당신은 씨엠에스랩(더마 선케어 브랜드 '셀퓨전씨' 운영)의 경쟁 인텔리전스 분석가입니다.
경쟁사들이 특허청에 낸 상표의 **지정상품**을 모아, 어느 쪽으로 몰리는지 센 결과입니다.
아래 숫자는 이미 집계된 사실이니 **다시 세지 말고 그대로 쓰세요.**

[집계 범위] 국내 상표 수집이 끝난 경쟁 브랜드 {n_collected}곳.
  (전체 모니터링 대상은 더 많지만 수집이 진행 중입니다. 숫자를 말할 때는 반드시
   "수집된 {n_collected}곳 중 N곳"처럼 **분모를 같이** 쓰세요. 분모를 빼면 과장이 됩니다.)

[어디로 몰리나 — 축별]
{axes}

[브랜드별로 무엇을 냈나]
{brands}

{profile}

다음 JSON을 쓰세요.

  "headline" : 가장 크게 몰리는 쪽 한 줄(40자 이내). 숫자와 분모를 넣으세요.
               예: '수집된 14곳 중 7곳이 가정용 기기로 넘어가는 중'
  "body"     : 3~4문장.
               ① 1위 축에 몇 곳이 몰렸나(분모 포함).
               ② **두 번째 축도 반드시 함께 쓰세요.** 한 축만 말하면 그림이 반쪽입니다.
                  특히 **한두 곳만 간 쪽**(식품·약국처럼 드문 쪽)이 더 큰 뉴스일 때가
                  많습니다 — 남들이 아직 안 간 곳이니까요.
               ③ 우리(셀퓨전씨)에게 뭘 뜻하나.
               **위 목록의 실제 상표명을 2개 이상 「」로 인용하세요.**
               단, **브랜드 이름 그 자체인 상표**는 인용으로 치지 않습니다 —
               아무 정보가 없습니다. 제품명다운 것을 고르세요.
               제품도 구체적으로 — '가정용 고주파기', '부기 완화 음료', '피부진단 앱'.
  "watch"    : 지금 당장 확인해볼 것 2~3개. 각 25자 이내의 짧은 구(句).
               **각 항목에 위 목록의 브랜드명이나 상표명을 넣으세요.**
               셋이 같은 축이면 안 됩니다 — 서로 다른 축에서 고르세요.
               ⚠️ 아래는 **형식만 보여주는 가짜 예시**입니다. 이름을 그대로 쓰지 말고
               반드시 위 목록에 실제로 있는 브랜드·상표로 바꿔 쓰세요.
               나쁨: '디바이스 출시 일정', '시장 수요 점검' (뭘 보라는 건지 없음)
               좋음 형식: '<브랜드> <제품명> 출시 여부', '<브랜드> <제품명> 가격대'

규칙:
- **집계에 없는 축을 지어내지 마세요.** 위 표에 없으면 없는 것입니다.
- ⚠️ **나라·시장 이야기를 쓰지 마세요.** 위 자료는 전부 **한국 특허청 출원**이라
  어느 나라에서 팔지에 대한 정보가 **하나도 없습니다**. '베트남 시장에서',
  '미국·유럽 공략' 같은 말은 근거 없는 창작입니다.
  (실제로 '한국과 베트남 시장에서 우리 입지를 위협한다'가 나와 걸러졌습니다.)
- 확인된 사실(출원·류·브랜드 수)은 단정형('~냈다', '~곳이다'), 의도 추정은
  추정형('~로 보인다', '~로 읽힌다')으로 어미를 구분하세요.
- 금지 상투어: '입지를 강화', '글로벌 시장 확장', '경쟁력 강화', '다변화', '전환점',
  '주목할 필요'. 어느 업계에 붙여도 맞는 말이라 정보량이 0입니다.
- ❌ **절대 금지 어미**: '~해야 한다', '~하라', '~하자', '~할 것이다', '~하면 된다',
  '~필요하다', '~중요하다'. 우리가 할 일은 **조언형**으로만 쓰세요 —
  '~해볼 만하다', '~쪽을 고려', '~점검이 필요해 보인다', '~여지가 있다'.
  (실제로 '선케어 라인업을 강화해야 한다'로 나가 지적받은 적이 있습니다.)

반드시 JSON만: {{"headline":"...", "body":"...", "watch":["...","..."]}}"""


def _llm(prompt: str, max_tokens: int = 900) -> dict:
    from openai import OpenAI
    client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    resp = client.chat.completions.create(
        model=_MODEL, max_tokens=max_tokens, temperature=0.3,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": prompt}],
    )
    return json.loads(resp.choices[0].message.content or "{}")


def _profile() -> str:
    try:
        from analytics.summarizer import CMS_PROFILE
        return CMS_PROFILE
    except Exception:
        return "[우리] 씨엠에스랩 — 더마 선케어 브랜드 '셀퓨전씨'. 임상 기반 자외선차단·진정."


# 어느 브랜드·업계에 붙여도 맞는 말이라 정보량이 0인 표현. 나오면 버린다.
_CLICHES = ("입지를 강화", "입지 강화", "존재감을 높이", "인지도를 높이",
            "글로벌 시장 확장", "시장 점유율", "경쟁력 강화", "다변화",
            "전환점", "주목할 필요", "모니터링이 필요", "모니터링 필요",
            "라인을 더욱 강화", "라인업을 강화", "활발해지고 있")

# 명령·확정 어미. 프롬프트로만 막으면 그냥 뚫린다 — 실측으로 '선케어 라인업을
# 강화해야 한다'가 금지 지시를 넣은 채로 그대로 나왔다. 코드로 한 번 더 본다.
_IMPERATIVE = ("해야 한다", "해야한다", "되어야 한다", "돼야 한다", "하여야 한다",
               "하라.", "하라 ", "하자.", "하자 ", "할 것이다", "하면 된다",
               "강화하라", "확대하라", "집중하라", "주력하라")


def _has_imperative(s: str) -> str:
    """걸린 표현을 돌려준다(없으면 빈 문자열). 재작성 지시에 그대로 인용하려고."""
    for k in _IMPERATIVE:
        if k in (s or ""):
            return k.strip()
    # '~필요하다/중요하다'로 끝나는 단정도 지시문으로 읽힌다.
    for k in ("필요하다", "중요하다"):
        if (s or "").rstrip().endswith(k):
            return k
    return ""


def _foreign_market(s: str) -> str:
    """근거에 없는 나라 이야기를 집어낸다.

    국내 상표 지정상품에는 **나라 정보가 없다**(전부 한국 특허청 출원이다).
    그런데도 모델은 '한국과 베트남 시장에서 우리 입지를 위협한다'처럼 시장을
    지어낸다 — 실측으로 나왔다. 근거에 없는 말이니 다시 쓰게 한다.
    """
    # 두 글자 국가명은 보통 한국어에 섞여 오탐을 낸다 — '제품 인도', '오만한',
    # '부탄가스', '가나다순'. 세 글자 이상만 쓰고, 두 글자는 이 바닥에서 실제로
    # 입에 오르는 것만 골라 넣는다.
    _SHORT_OK = {"미국", "일본", "중국", "영국", "대만", "태국", "호주", "홍콩", "독일"}
    try:
        from config.vocab import COUNTRY_KO
        names = {v for k, v in COUNTRY_KO.items()
                 if k != "KR" and (len(v) >= 3 or v in _SHORT_OK)}
    except Exception:
        names = _SHORT_OK | {"베트남", "유럽", "동남아"}
    names |= {"유럽", "동남아", "북미", "중동"}
    for n in sorted(names, key=len, reverse=True):
        if n in (s or ""):
            return n
    return ""


def _tail_skew(vals) -> float:
    """끝 9글자가 같은 문장이 전체의 몇 할인가. 1.0이면 전부 같은 꼴이다."""
    vals = [v for v in vals if v]
    if not vals:
        return 0.0
    tails = {}
    for v in vals:
        t = re.sub(r"[\s.·]", "", v)[-9:]
        tails[t] = tails.get(t, 0) + 1
    return max(tails.values()) / len(vals)


def _clean(s: str) -> str:
    s = (s or "").strip()
    if not s:
        return ""
    if any(c in s for c in _CLICHES):
        logger.info("   상투어라 버림: %s", s[:40])
        return ""
    bad = _has_imperative(s)
    if bad:
        logger.info("   명령형이라 버림('%s'): %s", bad, s[:40])
        return ""
    cty = _foreign_market(s)
    if cty:
        # 다시 써보라고 한 뒤에도 나라를 지어내면 싣지 않는다. 근거에 없는
        # 사실은 적게 말하는 것보다 나쁘다 — 받는 사람이 틀린 줄 모른다.
        logger.info("   근거에 없는 나라('%s')라 버림: %s", cty, s[:40])
        return ""
    return s


def build(session, reads: dict = None, labels: dict = None) -> dict:
    """결론층 생성. LLM 2콜(브랜드별 대응 1 + 먹거리 총평 1).

    reads:  {brand: {"read": ..., "basis": ...}}  상표 판독 결과(있으면 함께 넣는다)
    labels: {brand: 한글명}
    반환:   {"converge": {...}, "actions": {brand: str}, "bet": {headline, body, watch}}
            특이 신호가 없으면 {} — 없는 결론을 만들지 않는다.
    """
    reads = reads or {}
    labels = labels or {}
    conv = converging(session)
    per = conv.get("per_brand") or {}
    if not per:
        logger.info("다음 먹거리: 특이 신호 없음 — 결론을 만들지 않습니다")
        return {}

    def _ko(b):
        return labels.get(b) or b

    # ── 브랜드별 대응
    # 근거를 아끼면 브랜드마다 같은 문장이 나온다 — 실측으로 두 브랜드가 똑같이
    # '의료용 피부관리기구에 맞대응해볼 만하다'를 받았다. 상표명이 보여야 갈린다.
    order = sorted(per, key=lambda b: -per[b]["n_distinct"])
    blocks = []
    for b in order:
        info = per[b]
        ax = " · ".join(_AXIS_NAME[a] for a in info["axes"])
        rd = (reads.get(b) or {}).get("read") or ""
        evs = "".join(f"   근거: {ln[:260]}\n" for ln in info["lines"][:3])
        blocks.append(
            f"▣ {b} ({_ko(b)}) — 넓히는 쪽: {ax}\n"
            + (f"   판독: {rd}\n" if rd else "") + evs)

    def _ask_actions(extra: str = "") -> dict:
        out = _llm(_P_ACTION.format(
            blocks="\n".join(blocks), profile=_profile(),
            n=len(order), keys=", ".join(order)) + extra, max_tokens=1200)
        got = {}
        for it in out.get("actions", []):
            b = (it.get("brand") or "").strip()
            act = _clean(it.get("action") or "")
            if b in per and act:
                got[b] = act
        return got

    actions = {}
    try:
        actions = _ask_actions()
        # 빠진 브랜드를 조용히 넘기지 않는다. 한 번은 되물어본다 — 모델이 그냥
        # 몇 개만 쓰고 멈추는 일이 잦고, 빠진 브랜드는 화면에서 사라진다.
        missing = [b for b in order if b not in actions]
        if missing:
            logger.info("   대응 누락 %d개 — 다시 물어봅니다: %s", len(missing),
                        ", ".join(missing))
            more = _ask_actions(
                f"\n\n**앞선 답에서 다음 브랜드가 빠졌습니다. 이들만 다시 쓰세요: "
                f"{', '.join(missing)}**\n조언형 어미('~해볼 만하다')로 끝내고, "
                f"브랜드마다 다른 내용을 쓰세요.")
            for b in missing:
                if more.get(b):
                    actions[b] = more[b]
        # 문장이 전부 같은 꼴로 끝나면(붕어빵) 명사만 다를 뿐 한 문장과 같다.
        # 글자 단위 중복 검사로는 안 잡힌다 — 실측으로 8건이 모두
        # '~에 ○○을 연계해볼 만하다'로 끝난 적이 있다. 어미 쏠림을 따로 본다.
        if len(actions) >= 4 and _tail_skew(actions.values()) > 0.5:
            logger.info("   대응이 전부 같은 문형 — 다시 물어봅니다")
            more = _ask_actions(
                "\n\n**앞선 답은 모든 문장이 같은 꼴로 끝나 한 문장과 다름없었습니다.**\n"
                "브랜드마다 서로 다른 수(선점·대체·방어·성분 검증·가격대 비교·채널 선점·"
                "임상 근거 강조 등)를 쓰고, 끝맺는 말도 바꿔 전부 다시 쓰세요.")
            if more and _tail_skew(more.values()) < _tail_skew(actions.values()):
                actions = {**actions, **more}

        still = [b for b in order if b not in actions]
        if still:
            logger.info("   대응 없음(근거 부족 또는 어미 위반): %s", ", ".join(still))
    except Exception as e:
        logger.warning("브랜드별 대응 생성 실패: %s", e)

    # ── 다음 먹거리 총평
    axes_txt = "\n".join(
        f"· {a['name']} ({', '.join(f'{c}류' for c in a['classes'])}) — "
        f"수집된 {conv['n_collected']}곳 중 {a['n']}곳: "
        + ", ".join(_ko(x) for x in a["brands"])
        for a in conv["axes"])
    # 축 이름과 브랜드 수만 주면 "의료기기 개발이 활발하다" 같은 맹탕이 나온다.
    # 실제 상표명과 지정상품을 보여줘야 '가정용 고주파기', '부기 완화 음료'가 나온다.
    brands_txt = "\n".join(
        f"· {_ko(b)} [{' / '.join(_AXIS_NAME[a] for a in info['axes'])}]"
        + (f" — {(reads.get(b) or {}).get('read','')}" if (reads.get(b) or {}).get("read") else "")
        + "".join(f"\n    · {ln[:170]}" for ln in info["lines"][:2])
        for b, info in sorted(per.items(), key=lambda kv: -kv[1]["n_distinct"]))

    base = _P_BET.format(n_collected=conv["n_collected"], axes=axes_txt,
                         brands=brands_txt, profile=_profile())
    bet = {}
    try:
        out = _llm(base)
        head, body = (out.get("headline") or "").strip(), (out.get("body") or "").strip()
        # 본문을 통째로 버리면 결론이 사라진다. 어미나 상투어만 틀렸으면 그 부분을
        # 집어 한 번 다시 쓰게 한다 — 버리는 것보다 고치는 쪽이 정보를 남긴다.
        cty = _foreign_market(body)
        bad = (_has_imperative(body) or next((c for c in _CLICHES if c in body), "")
               or (f"'{cty}'(근거에 없는 나라)" if cty else ""))
        if bad:
            logger.info("   총평에 금지 표현(%s) — 다시 씁니다", bad)
            out2 = _llm(base + f"\n\n**방금 쓴 본문에 금지 표현 {bad}가 들어갔습니다.**\n"
                               f"그 표현을 빼고 다시 쓰세요. 어미는 조언형('~해볼 만하다', "
                               f"'~쪽을 고려', '~여지가 있다')으로, 내용은 위 목록의 "
                               f"**실제 상표명을 인용해** 구체적으로 쓰세요.")
            if (out2.get("body") or "").strip():
                out = out2
                head = (out.get("headline") or head or "").strip()
                body = (out.get("body") or "").strip()
        head, body = _clean(head), _clean(body)
        watch = [w for w in (_clean(x) for x in (out.get("watch") or [])) if w][:3]
        if head or body:
            bet = {"headline": head, "body": body, "watch": watch}
        else:
            logger.info("   총평이 상투어·명령형만 남아 싣지 않습니다")
    except Exception as e:
        logger.warning("다음 먹거리 총평 생성 실패: %s", e)

    return {"converge": {k: v for k, v in conv.items() if k != "per_brand"},
            "actions": actions, "bet": bet}
