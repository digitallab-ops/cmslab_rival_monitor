"""국내(KIPRIS) 상표 출원 + 지정상품 명세 수집.

해외상표(signals/trademark.py)는 '진출' 선행신호다. 이건 다른 신호다 —
**브랜드가 무엇을 만들려 하는지**가 지정상품 명세에 적혀 있다.

  아누아 2026-06-19 출원(10류) 지정상품 27종:
    고주파 온열치료기 · 피부치료용 레이저 · 전기자극 의료용 피부미용기 ·
    셀룰라이트 치료용 장치 · 초음파 마사지기 · 미세피부박피장치 …
  → 아누아가 뷰티 디바이스로 들어온다. 추측이 아니라 출원인이 직접 쓴 문서다.

해외 출원보다 **국내가 먼저**이고(우선권 주장), 지정상품이 한글로 상세히 붙는다.
해외상표 API(ForeignTradeMark)는 지정상품을 주지 않는다 — 상표명·류코드뿐이라
'DR.GOBI'가 화장품인지 미용기기인지 알 수 없었다. 그래서 이 모듈이 필요하다.

**검색은 출원인(회사명)으로 한다.** 브랜드명 검색은 부분일치라 못 쓴다 —
실측으로 '일소'→애자일소프트웨어, '이옴'→바이옴, 'Thome'→앳홈이 섞였다.
출원인으로 찾으면 30/30 정확하고, 덤으로 **아직 모르는 신규 라인**까지 잡힌다
(더파운더즈가 2026-09-14에 낸 'Arumy' 09류처럼).

출원인을 모르는 브랜드는 브랜드명으로 찾아 출원인 후보를 추려 **검색으로 확인**하고,
애매하면 슬랙으로 묻는다(자동 등록하지 않는다). 로레알이 닥터지를 인수한 것처럼
**정당한 출원인은 바뀐다** — 손으로 적어두면 반드시 어긋난다.

자격증명: .env KIPRIS_KEY (해외상표와 같은 키).
"""

import os
import re
import time
import logging
import xml.etree.ElementTree as ET
from datetime import date, timedelta

import requests
from sqlalchemy import text

from config.settings import DB_SCHEMA
from storage.models import get_session

logger = logging.getLogger(__name__)

_BASE = "http://plus.kipris.or.kr/openapi/rest/trademarkInfoSearchService/"
_TIMEOUT = 30
_PAGE = 50
_MAX_PAGES = 12                # 출원인당 최대 600건(상한 — 과호출 방지)
_SLEEP = 0.25                  # 루프 사이 여유
_MIN_GAP = 0.7                 # 호출 사이 최소 간격(한도 초과 방지)
_BACKOFF = 4.0                 # 한도에 걸렸을 때 첫 대기(초). 2배씩 늘린다

# 우리 세그먼트와 관련 있는 상품류. 이 밖(의류·식품 등)은 저장은 하되 판독에서 뺀다.
RELEVANT_CLASSES = {3, 5, 9, 10, 11, 21, 35, 44}
CLASS_LABEL = {
    3: "화장품", 5: "의약·기능성", 9: "전자기기", 10: "의료기기",
    11: "가전·LED", 21: "미용도구", 35: "판매·유통", 44: "미용·의료서비스",
}

# 출원인 자동 추론이 이 점수를 넘고 검색 확인까지 통과하면 자동 등록.
_AUTO_MIN_SCORE = 3
# 이보다 많이 내는 출원인은 브랜드 전용 회사로 보지 않는다(상표명으로 거른다).
_MANY_FILINGS = 120
# 법인격 접미/접두 — 회사명 비교 전에 떼어낸다.
_CORP_NOISE = re.compile(
    r"(주식회사|유한회사|유한책임회사|합자회사|㈜|\(주\)|\(유\)|주\)|"
    r"CO\.?,?\s*LTD\.?|CO\.?|LTD\.?|INC\.?|CORP\.?|LLC|GMBH|S\.?A\.?)", re.I)


def _key() -> str:
    return os.getenv("KIPRIS_KEY", "").strip()


def _norm_co(s: str) -> str:
    """회사명 비교용 정규화. '주식회사 더파운더즈' == '더파운더즈 (주)' == 'The Founders Inc'는
    아님에 주의 — 한글/영문 법인명은 별개 행으로 둔다(둘 다 등록하면 된다)."""
    s = _CORP_NOISE.sub("", s or "")
    return re.sub(r"[^0-9A-Za-z가-힣]", "", s).upper()


def _norm_mark(s: str) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣]", "", (s or "")).upper()


def _parse_date(s: str):
    s = (s or "").strip()
    if len(s) != 8 or not s.isdigit():
        return None
    try:
        return date(int(s[:4]), int(s[4:6]), int(s[6:8]))
    except ValueError:
        return None


def _classes(code: str) -> set:
    """'03|21' · '10' · '03' → {3,21} / {10} / {3}."""
    out = set()
    for tok in re.findall(r"\d+", code or ""):
        try:
            out.add(int(tok))
        except ValueError:
            pass
    return out


# ── KIPRIS 호출 ──────────────────────────────────────────────────────────────

# 한도는 두 종류다. 하나는 기다리면 풀리고(초당), 하나는 내일까지 안 풀린다(일일).
# 실측 2026-11-11: 전수수집으로 일일 한도가 바닥나자 40개 브랜드가 각각 4번씩
# 물러섰다 다시 걸어 40분을 허비하고도 0건이었다. 둘을 구별하지 않은 탓이다.
_LIMIT_SEC = "LIMITED_NUMBER_OF_SERVICE_REQUESTS_PER_SECOND_EXCEEDS"   # 코드 23
_LIMIT_DAY = "LIMITED_NUMBER_OF_SERVICE_REQUESTS_EXCEEDS"              # 코드 22
_last_call = [0.0]


class QuotaExhausted(RuntimeError):
    """오늘 치 호출을 다 썼다. 기다려도 안 풀리니 이번 회차는 접는다."""


def _get(endpoint: str, params: dict, tries: int = 4):
    """KIPRIS 호출. 호출 한도에 걸리면 **쉬었다 다시** 건다.

    처음엔 실패를 그냥 넘겼더니 조용히 '그 브랜드는 출원 0건'이 됐다 — 실측으로
    한 번에 28번 걸려서 루온셀·메이크온이 통째로 비었는데 로그만 보면 정상이었다.
    한도 초과는 **고장이 아니라 기다리면 되는 일**이므로 물러섰다 다시 건다.
    """
    last = None
    for i in range(tries):
        gap = time.monotonic() - _last_call[0]
        if gap < _MIN_GAP:
            time.sleep(_MIN_GAP - gap)
        _last_call[0] = time.monotonic()
        r = requests.get(_BASE + endpoint,
                         params={"accessKey": _key(), **params}, timeout=_TIMEOUT)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        msg = (root.findtext(".//resultMsg") or "").strip()
        if msg == "success":
            return root
        last = msg or "unknown"
        if _LIMIT_SEC in last:
            wait = _BACKOFF * (2 ** i)
            logger.info("   KIPRIS 초당 한도 — %.0f초 쉬고 재시도(%d/%d)", wait, i + 1, tries)
            time.sleep(wait)
            continue
        if _LIMIT_DAY in last:
            raise QuotaExhausted("KIPRIS 일일 호출 한도 소진")   # 기다려도 안 풀린다
        break
    raise RuntimeError(f"KIPRIS {endpoint}: {last}")


def search(word: str, since: "date | None" = None, max_pages: int = _MAX_PAGES) -> list[dict]:
    """상표 검색(출원일 역순). since보다 오래된 출원이 나오면 멈춘다.

    sortSpec=AD·descSort=true가 핵심이다 — 정렬 없이 받으면 순서가 뒤섞여
    '최근 N건'을 뽑을 수 없다(실측으로 1페이지에 2026년과 2019년이 섞여 나왔다).

    쪽넘김은 **docsCount·docsStart**다. numOfRows·pageNo·currentPage는 조용히
    무시된다 — 실측으로 pageNo를 1·2·3·7로 바꿔도 같은 30건이 돌아왔다.
    그대로 뒀으면 브랜드마다 최신 30건만 모으고 나머지를 통째로 놓쳤을 것이다.
    """
    out, stop, seen = [], False, set()
    search.failed = False            # 호출이 끝까지 못 갔는지 — 호출부가 봐야 한다
    for page in range(max_pages):
        try:
            root = _get("freeSearchInfo", {
                "word": word, "docsCount": _PAGE, "docsStart": page * _PAGE + 1,
                "sortSpec": "AD", "descSort": "true"})
        except QuotaExhausted:
            search.failed = True
            raise
        except Exception as e:
            logger.warning("국내상표 검색 실패(%s @%d): %s", word, page * _PAGE + 1, e)
            search.failed = True
            break
        rows = list(root.iter("TradeMarkInfo"))
        if not rows:
            break
        for it in rows:
            g = lambda t: (it.findtext(t) or "").strip()      # noqa: E731
            ad = _parse_date(g("ApplicationDate"))
            if since and ad and ad < since:
                stop = True
                continue
            apn = g("ApplicationNumber")
            if apn and apn in seen:      # docsStart 경계에서 한 건씩 겹쳐 온다
                continue
            if apn:
                seen.add(apn)
            out.append({
                "apn": g("ApplicationNumber"), "app_date": ad,
                "reg_date": _parse_date(g("RegistrationDate")),
                "cls": g("GoodClassificationCode"), "title": g("Title"),
                "applicant": g("ApplicantName"), "status": g("ApplicationStatus"),
                "agent": g("AgentName"),
                "right_holder": g("RegistrationRightholderName"),
            })
        if stop or len(rows) < _PAGE:
            break
        time.sleep(_SLEEP)
    return out


def goods(apn: str) -> list[dict]:
    """지정상품 명세. 출원인이 '이 상표를 어떤 상품에 쓰겠다'고 직접 쓴 1차 자료."""
    try:
        root = _get("trademarkDesignationGoodstInfo", {"applicationNumber": apn})
    except QuotaExhausted:
        raise
    except Exception as e:
        logger.debug("지정상품 조회 실패(%s): %s", apn, e)
        return []
    ko = [(e.text or "").strip() for e in root.iter("DesignationGoodsHangeulName")]
    en = [(e.text or "").strip() for e in root.iter("DesignationGoodsEnglishsentenceName")]
    cc = [(e.text or "").strip() for e in root.iter("DesignationGoodsClassificationInformationCode")]
    sg = [(e.text or "").strip() for e in root.iter("SimilargroupCode")]
    n = len(ko)
    pad = lambda xs: (xs + [""] * n)[:n]                      # noqa: E731
    en, cc, sg = pad(en), pad(cc), pad(sg)
    return [{"seq": i + 1, "name_ko": ko[i], "name_en": en[i],
             "cls": cc[i], "similar": sg[i]} for i in range(n) if ko[i]]


# ── 테이블 ───────────────────────────────────────────────────────────────────

def mark_matches(mark: str, keys: set) -> bool:
    """상표명이 그 브랜드를 가리키는가(한글·영문 표기 중 하나로 시작/포함)."""
    m = _norm_mark(mark)
    return bool(m) and any(k and k in m for k in keys)


def _brand_keys(brand: str, ko_names: list) -> set:
    ks = {_norm_mark(x) for x in ([brand] + list(ko_names or [])) if x}
    return {k for k in ks if len(k) >= 2}


def needs_mark_match(session, applicant: str, n_filings: int) -> str:
    """출원인 상표를 **전부** 그 브랜드 것으로 볼 수 있는가. 아니면 이름으로 걸러야 한다.

    '더파운더즈'는 아누아 전용 회사라 전부 가져와도 된다 — 덕분에 아직 모르는
    신규 라인('Arumy' 09류)까지 잡힌다. 그런데 '아모레퍼시픽'을 그대로 두면
    설화수·라네즈 상표가 에스트라·메이크온 것으로 들어온다.

    자동 판정 기준 두 가지 — 손으로 대기업 목록을 적지 않는다(반드시 어긋난다):
      · 한 출원인이 **우리 브랜드 둘 이상**에 걸려 있다 (아모레=에스트라+메이크온,
        동국제약=센텔리안24+루온셀) → 이름으로 걸러야 한다
      · 최근 구간 출원이 너무 많다 → 브랜드 전용 회사로 보기 어렵다
    반환: "" (전부 수용) 또는 사유 문자열.
    """
    n = session.execute(text(
        f"SELECT COUNT(DISTINCT brand) FROM {DB_SCHEMA}.brand_applicants "
        f"WHERE status = 'active' AND applicant = :a"), {"a": applicant}).scalar() or 0
    if n > 1:
        return f"출원인이 브랜드 {n}개에 걸침"
    if n_filings > _MANY_FILINGS:
        return f"출원 {n_filings}건(전용 회사로 보기 어려움)"
    return ""


def ensure_tables(session) -> None:
    session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.trademark_goods (
            country      VARCHAR(4)  NOT NULL,
            app_number   VARCHAR(40) NOT NULL,
            seq          INTEGER     NOT NULL,
            cls          VARCHAR(8),
            name_ko      TEXT,
            name_en      TEXT,
            similar_code VARCHAR(40),
            fetched_at   TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            PRIMARY KEY (country, app_number, seq)
        )
    """))
    session.execute(text(
        f"CREATE INDEX IF NOT EXISTS ix_tm_goods_apn "
        f"ON {DB_SCHEMA}.trademark_goods (app_number)"))
    # 상표명이 그 브랜드를 가리키는가. 출원인이 여러 브랜드를 가진 회사일 때
    # '그 브랜드 것'과 '같은 회사의 다른 라인'을 화면에서 가르는 기준이 된다.
    session.execute(text(
        f"ALTER TABLE {DB_SCHEMA}.trademark_filings "
        f"ADD COLUMN IF NOT EXISTS mark_match BOOLEAN"))
    session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.brand_applicants (
            brand      VARCHAR(100) NOT NULL,
            applicant  VARCHAR(200) NOT NULL,
            status     VARCHAR(20)  DEFAULT 'pending',  -- active | pending | rejected
            source     VARCHAR(20)  DEFAULT 'auto',     -- auto | slack | seed
            score      INTEGER      DEFAULT 0,
            evidence   TEXT,
            added_at   TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
            decided_at TIMESTAMP WITH TIME ZONE,
            PRIMARY KEY (brand, applicant)
        )
    """))
    session.commit()


def active_applicants(session, brand: str) -> list[str]:
    rows = session.execute(text(
        f"SELECT applicant FROM {DB_SCHEMA}.brand_applicants "
        f"WHERE brand = :b AND status = 'active'"), {"b": brand}).fetchall()
    return [r[0] for r in rows]


def _known(session, brand: str) -> set:
    rows = session.execute(text(
        f"SELECT applicant FROM {DB_SCHEMA}.brand_applicants WHERE brand = :b"),
        {"b": brand}).fetchall()
    return {_norm_co(r[0]) for r in rows}


# ── 출원인 추론 ──────────────────────────────────────────────────────────────

def guess_applicants(brand: str, ko_names: list, limit: int = 3) -> list[dict]:
    """브랜드명으로 검색해 출원인 후보를 점수순으로. 등록은 하지 않는다.

    점수: 상표명이 브랜드명으로 **시작**하면 +2(가장 강한 단서), 포함하면 +1,
          우리 세그먼트 상품류면 +1. 최근 3년만 본다.
    """
    # 한글명을 **전부** 넣는다. 첫 번째만 쓰면 놓친다 — 스케덤은 '스케덤'으로는
    # 국내 출원이 없고 모회사 표기('클래시스')로만 잡힌다.
    terms = [t for t in list(ko_names or [])[:3] + [brand] if t]
    keys = {_norm_mark(t) for t in ([brand] + list(ko_names or [])) if t}
    keys = {k for k in keys if len(k) >= 2}
    since = date.today() - timedelta(days=365 * 3)
    agg: dict = {}
    for t in terms:
        for r in search(t, since=since, max_pages=2):
            ap = (r["applicant"] or "").strip()
            if not ap:
                continue
            mk = _norm_mark(r["title"])
            sc = 0
            if mk and any(mk.startswith(k) for k in keys):
                sc += 2
            elif mk and any(k in mk for k in keys):
                sc += 1
            if _classes(r["cls"]) & RELEVANT_CLASSES:
                sc += 1
            if sc <= 0:
                continue
            e = agg.setdefault(_norm_co(ap), {"applicant": ap, "score": 0, "n": 0, "sample": ""})
            e["score"] += sc
            e["n"] += 1
            if not e["sample"] and r["title"]:
                e["sample"] = r["title"]
        time.sleep(_SLEEP)
    out = sorted(agg.values(), key=lambda x: (-x["score"], -x["n"]))
    return out[:limit]


def _verify(brand: str, ko: str, applicant: str, sample: str) -> dict:
    """검색으로 '이 회사가 이 브랜드의 운영사인가'를 확인. 실패하면 unknown."""
    try:
        from signals.web_verify import search as wsearch, _ask, _mentions_any
    except Exception as e:
        logger.warning("검증 모듈 로드 실패: %s", e)
        return {"verdict": "unknown", "reason": "검증 모듈 없음"}
    label = ko or brand
    ev = []
    for q in (f"{label} {applicant}", f"{applicant} 화장품", f"{label} 브랜드 운영사"):
        ev += wsearch(q, kinds=("encyc", "webkr", "news"), each=2)
        if len(ev) >= 12:
            break
    if not ev:
        return {"verdict": "unknown", "reason": "검색 결과 없음"}

    # 🔴 출원인이 근거에 실제로 나오지 않으면 묻지도 않는다.
    # 브랜드 이야기만 잔뜩 들려주면 모델은 그걸로 "맞다"고 답한다 — 실측으로
    # '메이크온 ← 메이커빌'과 '닥터지 ← 윤현식'이 그렇게 owner로 통과했다
    # (후자의 근거는 로레알 인수 기사였다. 윤현식은 한 글자도 안 나온다).
    # 이름이 안 나오면 사람에게 묻는 쪽(unknown→pending→슬랙)으로 보낸다.
    bare = _CORP_NOISE.sub("", applicant or "").strip()
    if not _mentions_any(ev, [applicant, bare]):
        return {"verdict": "unknown",
                "reason": f"검색 결과에 '{bare or applicant}'가 안 나옴 — 확인 못 함"}

    sysmsg = (
        "너는 한국 화장품 업계 조사원이다. 아래 검색 결과만 보고 판단해라. "
        "검색 결과에 없는 내용을 지어내지 마라. 모르면 unknown이라고 답해라.\n"
        'JSON만: {"verdict":"owner|unrelated|unknown","reason":"한 문장"}\n'
        "owner = 이 회사가 그 브랜드를 운영·소유(인수한 경우 포함)한다는 근거가 있음\n"
        "unrelated = 전혀 다른 회사라는 근거가 있음\n"
        "unknown = 근거가 부족함\n"
        "🔴 **회사 이름과 브랜드를 이어주는 문장이 검색 결과에 없으면 반드시 unknown이다.** "
        "브랜드 설명만 있고 그 회사가 안 나오면 owner가 아니다. "
        "reason에는 근거가 된 문장을 그대로 인용해라.")
    user = (f"브랜드: {label}\n출원인(회사): {applicant}\n"
            f"그 회사가 낸 상표 예: {sample or '(이름 없는 도형상표)'}\n\n"
            "[검색 결과]\n" + "\n".join(ev[:12]))
    try:
        v = _ask(sysmsg, user, max_tokens=200) or {"verdict": "unknown", "reason": ""}
    except Exception as e:
        logger.warning("출원인 검증 실패(%s/%s): %s", brand, applicant, e)
        return {"verdict": "unknown", "reason": str(e)[:80]}

    # 근거 문장에 출원인이 없으면 owner로 인정하지 않는다. 이름이 검색 결과
    # 어딘가에 있기만 해도 모델은 **다른 문장**을 근거로 "맞다"고 답한다 —
    # 실측으로 '닥터지 ← 윤현식'의 근거가 로레알 인수 기사였다.
    if (v.get("verdict") or "").lower() == "owner" and \
            not _mentions_any([v.get("reason") or ""], [applicant, bare]):
        return {"verdict": "unknown",
                "reason": f"근거 문장이 '{bare or applicant}'를 가리키지 않음 — 확인 못 함"}
    return v


def resolve_applicants(session, brand: str, ko_names: list) -> dict:
    """출원인이 없는 브랜드의 출원인을 찾아 등록하거나 슬랙 대기로 올린다.

    자동 등록은 **점수도 높고 검색으로도 확인된 경우**만. 애매하면 사람에게 묻는다 —
    틀린 회사를 자동 등록하면 남의 상표를 그 브랜드 활동으로 보고하게 된다.
    """
    seen = _known(session, brand)
    raw = guess_applicants(brand, ko_names)
    cands = [c for c in raw if _norm_co(c["applicant"]) not in seen]
    if not cands:
        return {"active": 0, "pending": 0, "cands": []}

    # 🔴 자동 등록은 **1순위이면서 압도적일 때만**. 2·3순위는 아무리 점수가 높아도
    # 자동으로 켜지 않는다 — 닥터지 후보에 엘오케이(40)·안향민(13)·윤현식(11)이
    # 같이 올라왔는데, 뒤 둘은 동명이인·유사상표다. 2순위 이하는 1순위의 절반도
    # 안 되면 슬랙에도 올리지 않는다(물어볼 가치가 없는 소음).
    top_score = cands[0]["score"]
    cands = [cands[0]] + [c for c in cands[1:] if c["score"] * 2 >= top_score]

    # 먼저 전부 검증하고, 그 결과를 보고 정한다. 점수만으로 가르면 뻔한 것도 묻게
    # 된다 — 구달은 클리오(14)가 맞는데 2순위 조준형(9)이 절반을 넘어 보류됐다.
    # 조준형은 검증에서 unknown이었으니 경쟁자가 아니었다.
    ko = ko_names[0] if ko_names else ""
    for c in cands:
        v = _verify(brand, ko, c["applicant"], c.get("sample", ""))
        c["_verdict"] = (v.get("verdict") or "unknown").lower()
        c["_reason"] = (v.get("reason") or "")[:300]

    owners = [c for c in cands if c["_verdict"] == "owner"]
    # 자동 등록 조건: 1순위가 owner이고, 점수가 충분하고,
    #   (다른 owner가 없거나 | 점수가 2순위의 2배 이상)
    runner = cands[1]["score"] if len(cands) > 1 else 0
    dominant = (cands[0]["_verdict"] == "owner"
                and cands[0]["score"] >= _AUTO_MIN_SCORE
                and (len(owners) == 1 or cands[0]["score"] >= runner * 2))

    n_act = n_pend = 0
    for i, c in enumerate(cands):
        verdict, reason = c["_verdict"], c["_reason"]
        if verdict == "unrelated":
            status = "rejected"
        elif i == 0 and dominant:
            status = "active"
        else:
            status = "pending"
        session.execute(text(f"""
            INSERT INTO {DB_SCHEMA}.brand_applicants
                (brand, applicant, status, source, score, evidence, decided_at)
            VALUES (:b, :a, :s, 'auto', :sc, :e, CASE WHEN :s <> 'pending' THEN NOW() END)
            ON CONFLICT (brand, applicant) DO NOTHING
        """), {"b": brand, "a": c["applicant"][:200], "s": status,
               "sc": c["score"], "e": f"[{verdict}] {reason}"})
        c["_status"] = status
        c["_brand"] = brand
        n_act += status == "active"
        n_pend += status == "pending"
    session.commit()
    return {"active": n_act, "pending": n_pend, "cands": cands}


# ── 저장 ─────────────────────────────────────────────────────────────────────

def _save_filing(session, brand: str, r: dict, mark_match: "bool | None" = None) -> bool:
    """trademark_filings에 country='KR'로. 반환: 신규면 True."""
    apn = (r.get("apn") or "").strip()[:40]
    if not apn:
        mk = (r.get("title") or "").strip()
        apn = ("NM:" + mk[:36]) if mk else "-"
    cls = (r.get("cls") or "")[:80]
    row = session.execute(text(f"""
        INSERT INTO {DB_SCHEMA}.trademark_filings
            (brand, country, mark_name, applicant, right_holder, app_number,
             app_date, reg_date, nice_code, cls_code, is_cosmetic, is_own, mark_match)
        VALUES (:b, 'KR', :mk, :ap, :rh, :an, :ad, :rd, :nc, :cl, :cos, TRUE, :mm)
        ON CONFLICT (brand, country, app_number) DO UPDATE SET
            reg_date = EXCLUDED.reg_date, mark_name = EXCLUDED.mark_name,
            is_cosmetic = EXCLUDED.is_cosmetic, mark_match = EXCLUDED.mark_match,
            fetched_at = NOW()
        RETURNING (xmax = 0) AS inserted
    """), {"b": brand, "mm": mark_match, "mk": (r.get("title") or "")[:200],
           "ap": (r.get("applicant") or "")[:200],
           "rh": (r.get("right_holder") or "")[:200], "an": apn,
           "ad": r.get("app_date"), "rd": r.get("reg_date"),
           "nc": cls, "cl": cls, "cos": 3 in _classes(cls)}).fetchone()
    return bool(row[0]) if row else False


def _needs_goods(session, apn: str) -> bool:
    """이 출원의 지정상품 명세가 아직 없나. '신규 출원'만 보고 받으면, 한도로
    끊긴 회차에 출원만 저장된 건의 명세를 영영 못 받는다."""
    return not session.execute(text(
        f"SELECT 1 FROM {DB_SCHEMA}.trademark_goods "
        f"WHERE country = 'KR' AND app_number = :an LIMIT 1"), {"an": apn}).first()


def _save_goods(session, apn: str, items: list) -> int:
    n = 0
    for g in items:
        session.execute(text(f"""
            INSERT INTO {DB_SCHEMA}.trademark_goods
                (country, app_number, seq, cls, name_ko, name_en, similar_code)
            VALUES ('KR', :an, :sq, :cl, :ko, :en, :sg)
            ON CONFLICT (country, app_number, seq) DO UPDATE SET
                name_ko = EXCLUDED.name_ko, name_en = EXCLUDED.name_en,
                cls = EXCLUDED.cls, similar_code = EXCLUDED.similar_code
        """), {"an": apn, "sq": g["seq"], "cl": (g.get("cls") or "")[:8],
               "ko": g.get("name_ko") or "", "en": g.get("name_en") or "",
               "sg": (g.get("similar") or "")[:40]})
        n += 1
    return n


# ── 실행 ─────────────────────────────────────────────────────────────────────

def run(months: int = 24, resolve: bool = True) -> dict:
    """국내 상표 + 지정상품 수집. 반환 {brands, filings, new, goods, pending}."""
    if not _key():
        logger.warning("KIPRIS_KEY 미설정 — 국내상표 수집 스킵")
        return {"brands": 0, "filings": 0, "new": 0, "goods": 0, "pending": 0}

    session = get_session()
    stat = {"brands": 0, "filings": 0, "new": 0, "goods": 0, "pending": 0,
            "no_applicant": [], "by_brand": {}, "device_new": [], "incomplete": [],
            "quota_exhausted": False, "not_reached": []}
    try:
        ensure_tables(session)
        brands = session.execute(text(
            f"SELECT name, ko_names FROM {DB_SCHEMA}.monitored_brands "
            f"WHERE is_active AND name <> '대기' ORDER BY tier, name")).fetchall()
        since = date.today() - timedelta(days=int(months * 30.5))
        newly_pending = []

        done = set()
        try:
            for name, kos in brands:
                done.add(name)            # continue로 빠져도 '못 돈 것'은 아니다
                kos = list(kos or [])
                apps = active_applicants(session, name)
                # 이미 사람 판단을 기다리거나 제외된 후보가 있으면 다시 찾지 않는다.
                # 안 그러면 매주 같은 브랜드를 또 검색하고 또 묻게 된다.
                if not apps and resolve and not _known(session, name):
                    res = resolve_applicants(session, name, kos)
                    if res["pending"]:
                        newly_pending += [c for c in res["cands"] if c.get("_status") == "pending"]
                        stat["pending"] += res["pending"]
                    apps = active_applicants(session, name)
                if not apps:
                    stat["no_applicant"].append(name)
                    continue

                stat["brands"] += 1
                keys = _brand_keys(name, kos)
                seen_apn, n_new = set(), 0
                for ap in apps:
                    key = _norm_co(ap)
                    found = search(ap, since=since)
                    if getattr(search, "failed", False):
                        # 조회가 끊긴 걸 '출원 0건'으로 보면 그 브랜드가 조용히 사라진다.
                        stat["incomplete"].append(f"{name}←{ap}")
                        logger.warning("   %s ← %s: 조회가 끝까지 못 감 — 이번 회차 건너뜀", name, ap)
                        continue
                    rows = [r for r in found
                            # 출원인 대조 — 검색은 부분일치라 다른 회사가 섞여 들어온다.
                            if _norm_co(r["applicant"]) == key]
                    why = needs_mark_match(session, ap, len(rows))
                    if why:
                        before = len(rows)
                        rows = [r for r in rows if mark_matches(r["title"], keys)]
                        logger.info("   %s ← %s: 상표명 대조 적용(%s) %d→%d",
                                    name, ap, why, before, len(rows))
                    for r in rows:
                        apn = r["apn"]
                        if not apn or apn in seen_apn:
                            continue
                        seen_apn.add(apn)
                        stat["filings"] += 1
                        is_new = _save_filing(session, name, r,
                                              mark_match=mark_matches(r["title"], keys))
                        # 명세가 비어 있으면 이미 있던 출원이라도 다시 집는다 —
                        # 한도로 끊긴 회차에 출원만 저장된 건을 주워 담기 위해서다.
                        want_goods = _needs_goods(session, apn)
                        if not is_new and not want_goods:
                            continue
                        if is_new:
                            n_new += 1
                        # 지정상품은 우리 세그먼트 류만 — 전부 받으면 호출이 낭비된다.
                        cls = _classes(r["cls"])
                        if cls & RELEVANT_CLASSES and want_goods:
                            items = goods(apn)
                            stat["goods"] += _save_goods(session, apn, items)
                            # 디바이스·의약 영역은 '하던 일과 다른 일'이라 따로 알린다.
                            if cls & {9, 10, 11}:
                                stat["device_new"].append({
                                    "brand": name, "date": str(r.get("app_date") or ""),
                                    "cls": "·".join(str(c) for c in sorted(cls)),
                                    "mark": r.get("title") or "",
                                    "goods": list(dict.fromkeys(
                                        g["name_ko"] for g in items if g.get("name_ko")))[:8],
                                })
                            time.sleep(_SLEEP)
                    session.commit()
                    time.sleep(_SLEEP)
                stat["new"] += n_new
                if n_new:
                    stat["by_brand"][name] = n_new
                    logger.info("   %s: 신규 %d건", name, n_new)
        except QuotaExhausted as e:
            # 남은 브랜드를 '0건'으로 저장하면 그 브랜드가 조용히 사라진다.
            session.commit()
            rest = [b for b, _ in brands if b not in done]
            stat["quota_exhausted"] = True
            stat["not_reached"] = rest
            logger.warning("   %s — 남은 %d개 브랜드는 다음 회차로 미룹니다: %s",
                           e, len(rest), ", ".join(rest[:12]))
        session.commit()
        if newly_pending:
            _notify_pending(newly_pending)
    finally:
        session.close()

    logger.info("국내상표: 브랜드 %d · 출원 %d(신규 %d) · 지정상품 %d · 확인대기 %d",
                stat["brands"], stat["filings"], stat["new"], stat["goods"], stat["pending"])
    if stat["no_applicant"]:
        logger.info("   출원인 미확인(수집 안 됨): %s", ", ".join(stat["no_applicant"]))
    if stat["incomplete"]:
        # 조용히 넘기면 다음 주에도 모른다. 경고로 남겨 로그에서 눈에 띄게 한다.
        logger.warning("   조회 미완료 %d건(다음 회차에 다시 시도): %s",
                       len(stat["incomplete"]), ", ".join(stat["incomplete"][:10]))
    return stat


def _notify_pending(cands: list) -> None:
    """출원인 확인이 애매한 건을 슬랙으로 묻는다(자동 등록하지 않는다)."""
    try:
        from notifications.slack import _post, mention_prefix
    except Exception as e:
        logger.warning("슬랙 모듈 로드 실패: %s", e)
        return
    lines = [f"{mention_prefix()}*🏷 상표 출원인 확인 요청 {len(cands)}건*",
             "_이 회사가 그 브랜드의 상표를 내는 게 맞나요? 맞아야 국내 상표를 수집합니다._",
             "_봇에게(@멘션): `출원인` 목록 · `출원인 승인 <브랜드>` · `출원인 제외 <브랜드>`_", ""]
    for c in cands[:15]:
        lines.append(f"• *{c.get('_brand', '')}* ← `{c['applicant']}` (점수 {c['score']})")
        if c.get("sample"):
            lines.append(f"    ↳ 그 회사 상표 예: {c['sample'][:60]}")
        if c.get("_verdict") == "unknown":
            lines.append("    ⚠️ 검색으로 확인 못 했습니다 — 직접 보고 판단해 주세요")
        if c.get("_reason"):
            lines.append(f"    ↳ {c['_reason'][:110]}")
    _post({"text": "\n".join(lines)}, secondary=False)


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(run())
