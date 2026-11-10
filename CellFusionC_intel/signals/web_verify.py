"""후보를 슬랙에 올리기 전에 실제로 검색해서 확인한다.

사람이 후보를 받으면 가장 먼저 하는 일이 검색이다 — 'hy가 뭐지?' 찾아보면
유산균 음료 회사(옛 한국야쿠르트)고, 화장품 브랜드는 '아이오셀'이라는 게
바로 나온다. 그 과정을 자동화한다.

LLM에게 그냥 물으면 아는 척을 한다. 그래서 **먼저 검색으로 근거를 모으고,
그 근거만 보고 판단하게** 한다. 근거가 없으면 모른다고 답하게 둔다.

검색은 네이버 API를 쓴다 — 이미 붙어 있고(수집기가 쓴다), 국내 브랜드·회사는
백과사전·웹문서가 구글보다 정확하다. 실측으로 'hy 화장품' → 백과사전이
"㈜에치와이, 유산균 음료 제조업체", '아이오셀' → 웹문서가 "hy가 만든 웰니스
브랜드 IOCELL 공식몰"을 바로 집어냈다.
"""

import html
import logging
import os
import re
import json

import requests

logger = logging.getLogger(__name__)

_NAVER = "https://openapi.naver.com/v1/search/{kind}.json"
_MODEL = os.getenv("WATCHDOG_MODEL", "gpt-4o-mini")
_TIMEOUT = 10


def _headers() -> dict:
    cid = os.getenv("NAVER_CLIENT_ID")
    sec = os.getenv("NAVER_CLIENT_SECRET")
    if not cid or not sec:
        return {}
    return {"X-Naver-Client-Id": cid, "X-Naver-Client-Secret": sec}


def _strip(t: str) -> str:
    return html.unescape(re.sub(r"<.*?>", "", t or "")).strip()


def search(query: str, kinds: tuple = ("encyc", "webkr", "news"), each: int = 3) -> list[str]:
    """검색 결과를 '제목 — 설명' 줄로 모은다. 실패하면 빈 리스트(판단 보류용)."""
    h = _headers()
    if not h:
        logger.warning("네이버 검색 키 없음 — 검증 건너뜀")
        return []
    out = []
    for kind in kinds:
        try:
            r = requests.get(_NAVER.format(kind=kind), headers=h,
                             params={"query": query, "display": each}, timeout=_TIMEOUT)
            if r.status_code != 200:
                continue
            for it in r.json().get("items", []):
                t, d = _strip(it.get("title")), _strip(it.get("description"))
                if t:
                    out.append(f"[{kind}] {t} — {d[:160]}")
        except Exception as e:
            logger.debug("검색 실패(%s/%s): %s", kind, query, e)
    return out


def _ask(system: str, user: str, max_tokens: int = 400) -> dict:
    """근거를 주고 JSON 판정을 받는다. 실패하면 빈 dict(=판단 못 함)."""
    try:
        from openai import OpenAI
        cli = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        r = cli.chat.completions.create(
            model=_MODEL, max_tokens=max_tokens, temperature=0,
            response_format={"type": "json_object"},
            messages=[{"role": "system", "content": system},
                      {"role": "user", "content": user}])
        return json.loads(r.choices[0].message.content or "{}")
    except Exception as e:
        logger.warning("검증 LLM 실패: %s", e)
        return {}


_BRAND_SYS = """너는 K-뷰티 경쟁사 모니터링 담당이다. 이 이름의 정체를 판정하라.

**정체는 [encyc]·[webkr] 검색 결과로 판단한다.** 뉴스 제목은 이 이름이 어떻게
등장했는지를 보여줄 뿐이다 — 뉴스에 "뷰티 진출"이 있어도 백과사전이 그 이름을
회사로 설명하면 회사다. 검색 결과에 없는 내용은 지어내지 마라.

verdict 중 하나:
  brand      — 화장품·뷰티 디바이스 브랜드. **회사명과 브랜드명이 같으면 brand다**
               (예: 아로마티카, 토리든 — 회사이기도 하지만 그 이름으로 화장품을 판다).
  company    — 회사인데 **화장품 브랜드는 다른 이름**인 경우에만. 그 브랜드명을
               actual_brand에 적어라. 예: hy(옛 한국야쿠르트)의 화장품 브랜드는
               '아이오셀', 구다이글로벌의 브랜드는 '조선미녀'.
  retailer   — 유통사·플랫폼·쇼핑몰
  ingredient — 성분·원료명. **그 성분을 만드는 회사가 검색에 나와도 ingredient다.**
               예: PDRN은 파마리서치가 만들지만 PDRN 자체는 성분이다.
  category   — 카테고리·일반명사·행사명·기획전
  unrelated  — 뷰티와 무관
  unknown    — 검색 결과만으로는 판단 불가. **검색 결과가 이 이름을 직접
               다루지 않으면 반드시 unknown이다.** 비슷해 보이는 다른 것을
               끌어다 판정하지 마라 — 진짜 브랜드를 탈락시키는 것이 모른다고
               답하는 것보다 나쁘다. 이름이 영문 음차라 국내 검색이 비는 경우가
               흔하다(헤브블루를 'Haveblue'로 찾으면 안 나온다).

판정 순서: ①성분인가? → ingredient ②유통·플랫폼인가? → retailer
③이 이름으로 화장품을 파는가? → brand ④화장품 브랜드가 딴 이름인가? → company

JSON으로만:
{"verdict":"...", "ko_name":"한국어 표기 또는 null", "actual_brand":"company일 때만, 그 외 null",
 "reason":"한 문장, 근거가 된 검색 결과를 인용", "confidence":"high|medium|low"}"""


def _norm(s: str) -> str:
    """비교용 정규화 — 대소문자·공백·점·하이픈을 지운다('S.NATURE'↔'에스네이처'는 별칭으로 처리)."""
    return "".join(c for c in (s or "").lower() if c.isalnum())


def _mentions_any(evidence: list, labels: list) -> bool:
    """검색 결과 중 하나라도 이 이름을 실제로 담고 있는가."""
    blob = _norm(" ".join(evidence))
    return any(_norm(lb) and _norm(lb) in blob for lb in labels)


def verify_brand(name: str, ko: str = "", samples: list = None) -> dict:
    """브랜드 후보 검증. 반환에 verdict·reason·evidence가 들어간다.

    검색이 안 되거나 LLM이 실패하면 verdict='unknown' — 막지 않는다.
    확신 없이 거르면 진짜 신흥 브랜드를 놓친다.
    """
    # 등록된 브랜드면 한글 별칭도 같이 검색한다. 등록명이 영문 음차라 국내
    # 매체가 그 철자를 안 쓰는 경우가 많다 — "Haveblue"로는 아무것도 안 나오고
    # "헤브블루"로는 나온다(수집기에서 같은 문제를 이미 겪었다).
    labels = [x for x in (name, ko) if x]
    try:
        from config.brands import BRAND_KO_NAMES
        for alias in (BRAND_KO_NAMES.get(name) or [])[:1]:
            if alias and alias not in labels:
                labels.append(alias)
    except Exception:
        pass

    ev = []
    for lb in labels[:2]:
        ev += search(f"{lb} 화장품") + search(lb, kinds=("encyc",))
    if not ev:
        return {"verdict": "unknown", "reason": "검색 결과 없음", "evidence": []}

    # 검색 결과가 이 이름을 실제로 다루는지 확인한다. 안 그러면 LLM이 엉뚱한
    # 결과를 끌어다 판정한다 — "Haveblue"를 찾다 'ACID BLUE 9'이라는 원료가
    # 걸려서 성분으로 판정하고 진짜 브랜드를 탈락시킨 적이 있다.
    if not _mentions_any(ev, labels):
        return {"verdict": "unknown",
                "reason": f"검색 결과에 '{name}'를 다룬 내용이 없음 — 판단 보류",
                "evidence": ev[:5]}

    # 근거(검색)를 먼저, 참고(뉴스)를 뒤에. 순서를 바꿨더니 뉴스 제목에 끌려가
    # hy를 브랜드로 판정했다 — 백과사전은 '유산균 음료 제조업체'라고 적고 있었는데도.
    user = (f"이름: {name}" + (f" (한국어 표기 후보: {ko})" if ko else "") +
            "\n\n== 검색 결과 (정체 판단의 근거) ==\n" +
            "\n".join(f"- {e}" for e in ev[:12]) +
            ("\n\n== 참고: 이 이름이 등장한 뉴스 제목 (정체 판단 근거 아님) ==\n" +
             "\n".join(f"- {t}" for t in (samples or [])[:3]) if samples else ""))
    out = _ask(_BRAND_SYS, user)
    if not out.get("verdict"):
        return {"verdict": "unknown", "reason": "판정 실패", "evidence": ev[:5]}

    # 'company'는 **다른** 브랜드명을 댈 수 있을 때만 탈락 사유가 된다.
    # 회사명과 브랜드명이 같으면(에스트라·아로마티카) 추적해야 할 브랜드다.
    # 백과사전이 "○○는 …전문기업이다"라고 적어 두면 LLM이 회사로 기울어서,
    # 실제로 에스트라가 탈락할 뻔했다.
    if out["verdict"] == "company":
        actual = (out.get("actual_brand") or "").strip()
        if not actual or _norm(actual) == _norm(name) or _norm(actual) == _norm(ko):
            out["verdict"] = "brand"
            out["actual_brand"] = None
            out["reason"] = (out.get("reason") or "") + " (회사명=브랜드명이라 브랜드로 본다)"

    out["evidence"] = ev[:5]
    return out


_COUNTRY_SYS = """주어진 코드가 실제 나라를 가리키는지 판정하라.
'global'·'null'·'other'·'unrelated'·'EMEA'·'LATAM' 같은 것은 나라가 아니다.

JSON으로만: {"is_country": true/false, "ko_name":"한국어 국가명 또는 null"}"""


def verify_country(code: str) -> dict:
    """국가코드 → {is_country, ko_name}. 확인 못 하면 ko_name=None."""
    c = (code or "").strip()
    if not c:
        return {"is_country": False, "ko_name": None}
    ev = search(f"{c} 국가 코드", kinds=("encyc", "webkr"), each=2)
    user = f"코드: {c}\n\n검색 결과:\n" + ("\n".join(f"- {e}" for e in ev[:6]) or "(없음)")
    out = _ask(_COUNTRY_SYS, user, max_tokens=120)
    if not out:
        return {"is_country": False, "ko_name": None}
    ko = (out.get("ko_name") or "").strip()
    # 코드를 그대로 돌려준 건 '모른다'와 같다 — 예전 폴백이 MN=MN을 내보냈다.
    if not ko or ko.upper() == c.upper():
        return {"is_country": bool(out.get("is_country")), "ko_name": None}
    return {"is_country": bool(out.get("is_country")), "ko_name": ko}
