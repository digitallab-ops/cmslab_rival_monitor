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
from config.settings import DB_SCHEMA

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
    "CO": "콜롬비아", "CL": "칠레", "PE": "페루", "AR": "아르헨티나", "EG": "이집트", "NG": "나이지리아", "PK": "파키스탄", "BD": "방글라데시", "NZ": "뉴질랜드", "IE": "아일랜드", "AT": "오스트리아", "BE": "벨기에", "CH": "스위스", "CZ": "체코", "DK": "덴마크", "FI": "핀란드", "GR": "그리스", "HU": "헝가리", "NO": "노르웨이", "PT": "포르투갈", "RO": "루마니아", "UA": "우크라이나", "IL": "이스라엘", "QA": "카타르", "KW": "쿠웨이트",
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


def _p_strat(brand_ko, cc, rec, sig=""):
    chs = list((rec.get("channels") or {}).keys())[:4]
    tac = [TAC_KO.get(t, t) for t in (rec.get("tactics") or {}).keys()]
    return f"""이 브랜드({brand_ko})가 {_cty(cc)}에서 이번 주 보인 움직임을 1~2문장으로 써라.

**[1단계] 먼저 이것부터 판단하라.** 아래 헤드라인이 다음 중 하나면 다른 규칙은 무시하고
정확히 `SKIP` 네 글자만 출력하고 끝내라. 억지로 전략을 지어내지 마라.
  · 실적·투자·펀드·지분·상장·M&A·합병      · 채용·인사·조직개편
  · 골프·스포츠·시상·기부                   · 소송·가품·분쟁·단속
  · 주가·증시                                · 단순 브랜드 나열(여러 브랜드를 열거만)

**먼저 이 예외부터 본다 — 해당하면 위 목록과 겹쳐도 SKIP이 아니다.**
유통·입점·출시·팝업·마케팅·앰배서더·인플루언서 협업 이야기면 시장 움직임이다.
검토·예정 단계여도, 기사에 매출·지분 같은 수치가 곁들여 있어도 SKIP이 아니다.
  (실제 누락 사례: '스킨1004 브라질 유통망 확대'가 매출 언급 때문에 SKIP됐고,
   'Celimax 새 앰배서더 계약'이 인사로 읽혀 브랜드가 브리핑에서 통째로 사라졌다.)
채용은 '인재 확보·구인·공채·리크루팅'이 **기사의 요지일 때만** SKIP이다.

**[2단계] 통과했다면 — 사실을 되풀이하지 마라.**
기사에 이미 적힌 내용을 다른 말로 바꿔 쓰는 것은 정보가 0이다. 실제로 이런 게 나갔다:
  "팝업에 배우 수지를 초청했다. 이는 젊은 소비자층을 겨냥한 마케팅 전략으로 읽힌다."
수지를 부르면 젊은 층 겨냥인 건 누구나 안다. 이런 문장은 쓰지 마라.

쓸 게 있으면 아래 중 하나여야 한다. 없으면 사실만 쓰고 해석은 **붙이지 마라**.
  · 지표와 엮기 — 아래 '지표'의 숫자를 인용해 움직임과 연결(가장 좋다)
  · 패턴 — 이전·다른 나라와 같은 방식인지, 처음 보는 방식인지
  · 이례성 — 이 브랜드·이 카테고리에서 흔치 않은 선택인지
  · 규모 감각 — 채널 수, 매장 수, 가격대 등 크기를 가늠케 하는 것

- 반드시 **구체적 사실**을 담아라: 채널명·제품·수치·행사 등(아래 본문요지 근거). 뭉뚱그리기 금지.
- 아래 상투어 금지: '입지 강화', '프리미엄 포지셔닝 강화', '인지도 확보', '존재감 강화',
  '확장하려는 움직임', '경쟁력 강화', '주목된다', '젊은 소비자층을 겨냥'.
- **마무리를 일반론으로 끝내지 마라**: 'K뷰티의 상승세를 보여준다', '긍정적인 반응을 얻고 있다',
  '중요한 과제로 떠오르고 있다' 같은 문장은 아무것도 말하지 않는다. 차라리 사실에서 끝내라.
- 지표는 **이 나라 줄에 맞는 것만** 인용하라. '(글로벌)'이 붙은 것은 전 세계 합산이니
  특정 국가 이야기인 것처럼 쓰지 마라.
- 60~95자. 이 브랜드 움직임만('K뷰티 브랜드들이'처럼 일반화 금지). 관찰형(~보인다).
  어느 쪽이 낫다는 우열 단정은 하지 마라.

헤드라인: {rec.get('headline')}
본문요지: {rec.get('summary') or '(없음)'}
판매채널: {', '.join(chs) or '(없음)'}
활동분류: {', '.join(tac) or '(없음)'}
지표: {sig or '(없음)'}"""


def _p_why(cc, it, ns):
    rv = it.get("review_count") or 0
    # 리뷰 규모를 구간으로 나눠 라벨을 다르게 주지 않으면 모두 '검증된 스테디셀러'가 된다
    # (실측: 캐시 27건 전부 같은 표현, 리뷰 3,855개와 26,063개가 동급 취급).
    band = ("리뷰 3만 이상 — 카테고리 대표급" if rv >= 30000 else
            "리뷰 1만~3만 — 두터운 사용층" if rv >= 10000 else
            "리뷰 3천~1만 — 자리 잡은 제품" if rv >= 3000 else
            "리뷰 1천~3천 — 오르는 중" if rv >= 1000 else
            "리뷰 1천 미만 — 아직 초기")
    return f"""아마존 {_cty(cc)}에서 {it.get('category','')} 카테고리 {it['rank']}위인 제품이
그 나라에서 왜 팔리는지 한 줄(28~40자)로 써라.

- 근거는 아래 지표와 제품명뿐이다. 없는 사실을 지어내지 마라.
- **'검증된 스테디셀러'라는 표현을 쓰지 마라.** 아래 규모 구간을 네 말로 풀어 쓰되,
  리뷰 수가 다르면 다르게 말해야 한다. 3천짜리와 3만짜리가 같은 문장이면 실패다.
- 순위와 리뷰 수 중 **더 특징적인 쪽**을 골라 말하라
  (순위는 높은데 리뷰가 적으면 '최근 치고 올라온', 리뷰는 많은데 순위가 낮으면 '오래 팔린' 식).
- 제품명에 성분·기능이 있으면 그것을 근거로 붙여라.
- 추정형(~로 보인다). 브랜드명 생략.

지표: {band} (리뷰 {rv:,}개 · 별점 {it.get('rating')} · {it['rank']}위)
제품명: {it.get('product')}
관련 뉴스요지: {(ns or '(없음)')[:120]}"""


def _p_sum(brand_ko, strat_snips, prods):
    return f"""'{brand_ko}' 브랜드의 이번 주 글로벌 움직임을 1~2문장(55~85자)으로 종합해라.
- 어느 시장들에서 어떤 방식(대형유통 입점·앰배서더·신흥시장 선점 등)으로 움직이는지 큰 틀만.
- 펼치기 전 '미리보기'용이니 핵심만. 관찰형(~보인다). 우리 회사 언급·우열 단정 금지. 브랜드명으로 시작.
나라별 움직임: {' / '.join(strat_snips[:6])}
리테일 상위: {', '.join(prods) or '(없음)'}"""


def _p_tongp(n, topb, mktxt, strat_examples, delta=""):
    """'이번 주의 한 방' — 뾰족한 헤드라인 + 근거 한 줄. 슬랙·대시보드 공용(단일 출처).

    입력의 핵심은 delta(전주 대비 달라진 것)다. 누적 현황만 주면 모델이 가장
    인용하기 좋은 수치를 집는데, 그게 월 단위로만 갱신되는 수출 성장률이라
    12주 내리 "캐나다 +396%, 영국 +234%"가 반복됐다.
    """
    return f"""K뷰티 경쟁 인텔리전스 브리핑의 '이번 주의 한 방'을 딱 2문장(합쳐 90자 이내)으로 써라.

**합격 기준: 지난주에 썼다면 틀렸을 문장이어야 한다.**
아무 주에나 갖다 붙일 수 있으면 실패다. 실제로 이런 게 나갔다 —
  "틱톡과 아마존이 K뷰티의 주요 전장으로 부상했다" (작년에도 맞는 말)
  "캐나다 +396%, 영국 +234% 성장" (12주 연속 똑같이 나갔다 — 월 단위 수치라서)

**아래 '이번 주 달라진 것'에서 소재를 골라라.** 이게 이번 주에만 있는 사실이다.
── 이번 주 달라진 것 ──
{delta or "(변화 정보 없음)"}
──

- 첫 문장 = 이번 주를 규정하는 헤드라인(35자 이내, 단정적).
- 둘째 문장 = 그렇게 본 근거(55자 이내).

**우리 수집량을 말하지 마라.** '8개 브랜드가 새로 포착됐다', '신호 12건 증가'는
우리 DB 얘기지 시장 얘기가 아니다. 읽는 사람은 시장에서 뭐가 일어났는지를 알고 싶다.
  나쁜 예: "새 브랜드와 채널의 활발한 등장. 8개 브랜드와 6개 채널이 새로 포착됐다."
  좋은 예: "진출선이 영국·일본·대만으로 갈라졌다. 한 주에 세 시장에서 동시에 첫 진입이 잡혔다."
  좋은 예: "채널이 현지 플랫폼으로 내려갔다. @cosme·Myntra 같은 로컬 커머스가 처음 등장."
**둘째 문장에는 실제 이름(국가·채널)을 넣어라.** 개수만 세지 마라.
**단, 위 '달라진 것' 목록에 적힌 이름만 써라.** 거기 없는 채널·국가를 지어내지 마라
(실제로 목록에 없는 'Boots', '메이시스'를 지어낸 적이 있다).
**브랜드명은 쓰지 마라** — 국가·채널·활동유형으로만 말해라. 브랜드는 아래 목록에서 따로 보여준다.
- **개별 브랜드명은 쓰지 마라** — 아래 목록에서 따로 보여주므로 겹친다.
  단 채널명(세포라·틱톡숍)·시장명(브라질·영국)은 써도 된다.
- 금지어(상투어): '다변화', '모색', '전환점', '입지 강화', '시장 점유율 확대',
  '긍정적', '기대', '주요 전장으로 부상', '새로운 무기'.
- 예측·당위 금지, 관찰형. 어느 쪽이 낫다는 우열 단정은 하지 마라.

참고(헤드라인 소재로 쓰지 말 것):
- 급성장 시장 {mktxt} ← **관세청 월 단위 집계라 지난주와 같은 수치다.**
  이걸 헤드라인으로 쓰면 지난주와 똑같은 문장이 된다.
- 이번 주 신호 {n}건 / 주요 브랜드: {topb}
- 브랜드 움직임: {' | '.join(strat_examples[:8])}"""


def _p_watch(strat_examples, mktxt):
    """'지켜볼 것' — 다음에 판가름날 지점(내일 또 열어보게 하는 훅). 줄바꿈 구분."""
    return f"""아래 K뷰티 경쟁 브랜드 움직임 중 '다음에 판가름날' 지점 2개를 뽑아라.

**각 줄에 반드시 '무엇으로 확인하는지'를 넣어라.** 궁금증만 던지고 끝내면 쓸모가 없다.
  나쁜 예: '리쥬란 사우디 론칭이 현지에 자리 잡을지'   ← 언제 어떻게 아는지가 없다
  좋은 예: '리쥬란 사우디 론칭 — 다음 달 중동 수출액으로 확인'
  좋은 예: '메디큐브 틱톡숍 — 2주 뒤 아마존 토너 순위가 1위를 지키는지'
확인 수단은 우리가 실제로 보는 것이어야 한다: 아마존 순위, 올리브영 랭킹, 리뷰 증가 속도,
관세청 수출액, 유튜브 조회수, 네이버 검색량, 추가 보도.

- 각 한 줄(35~55자). 브랜드명 포함 OK.
- **한국어 문법을 지켜라.** 주어에 조사가 겹치지 않게 하라
  (틀린 예: '리쥬란이 사우디에서 론칭이 자리 잡을지' — '이'가 두 번).
- 예측·단정 금지. 확인 관점으로.
- 출력은 딱 2줄, 각 줄에 하나씩. 번호·불릿·따옴표 없이 문장만.
브랜드 움직임: {' | '.join(strat_examples[:8])}
급성장 시장: {mktxt}"""



def collect_brand_signals(session) -> dict:
    """브랜드별 수치 신호를 한 줄로 — 전략 해석이 인용할 근거.

    판매속도·아마존 순위변동·유튜브·검색 모멘텀이 DB에 쌓여 있는데 브리핑은 순위
    하나만 쓰고 나머지를 버리고 있었다. 해석이 사실을 되풀이하던 원인 중 하나다.
    실패해도 브리핑이 죽지 않게 각 신호를 개별 try로 감싼다.
    """
    # 글로벌 신호(어느 나라 줄에서도 인용 가능)와 국가별 신호를 나눈다.
    # 섞으면 '아누아·한국' 줄에 아마존 미국 순위가 붙어 문맥이 어긋난다.
    glob: dict = defaultdict(list)
    bycc: dict = defaultdict(list)
    # 판매속도(리뷰 증가/일)는 당분간 인용하지 않는다.
    # DE/ES/IT/FR 리뷰수가 천단위 구분자 파싱 문제로 폭주한다 — 같은 ASIN이 13일 만에
    # 30 → 31,899로 뛴다(US·GB·AU·JP는 정상). 그 값이 "하루 19,065개 리뷰 증가"처럼
    # 슬랙 브리핑 문장으로 나갔다. 파서를 고치고 과거 데이터를 재계산하기 전까지는
    # 틀린 숫자를 인용하는 것보다 빼는 편이 낫다.
    # TODO: signals/retail_ranking.py `_parse_reviews` 로케일 처리 점검 후 되살릴 것.
    try:
        from analytics.queries import get_retail_rank_history
        for h in (get_retail_rank_history(session) or []):
            dl = h.get("delta") or 0
            cc = h.get("country") or ""
            if abs(dl) >= 3 and cc:
                bycc[(h["brand"], cc)].append(
                    f"아마존 {h.get('category','')} {h['ranks'][-1]}위"
                    f"({'+' if dl > 0 else ''}{dl}계단)")
    except Exception as e:
        logger.warning("신호 수집(순위변동) 실패: %s", e)
    try:
        from analytics.queries import get_social_verdict
        _V = {"organic_viral": "자발 바이럴", "paid_push": "광고성 밀어내기",
              "mention_only": "조회는 많으나 스침 언급", "rising": "상승세"}
        for b, d in (get_social_verdict(session) or {}).items():
            lab = _V.get(d.get("verdict"))
            if lab and (d.get("views") or 0) >= 300_000:
                glob[b].append(f"유튜브 {d['views']:,}회 · {lab}")
    except Exception as e:
        logger.warning("신호 수집(유튜브) 실패: %s", e)
    try:
        from analytics.queries import get_search_momentum
        for b, d in (get_search_momentum(session) or {}).items():
            if d.get("signal") == "rising":
                bycc[(b, "KR")].append(f"국내 검색량 {d.get('momentum')}배(직전 4주 대비)")
    except Exception as e:
        logger.warning("신호 수집(검색) 실패: %s", e)
    return {"global": {b: v for b, v in glob.items()}, "by_cc": dict(bycc)}


def signal_line(sigs: dict, brand: str, cc: str) -> str:
    """이 브랜드·이 나라 줄에서 인용해도 되는 지표만 골라 한 줄로."""
    parts = list((sigs.get("by_cc") or {}).get((brand, cc)) or [])
    # '(글로벌)' 접두는 규칙 목록에 묻혀 모델이 무시했다(전 세계 누적 조회수가
    # "이번 캠페인 조회수"로 둔갑). 꼬리표 대신 문장 자체에 범위를 박아 넣는다.
    parts += [f"{x} ← 이 나라가 아니라 전 세계 합산이다. 특정 국가·특정 캠페인 수치로 쓰지 말 것"
              for x in ((sigs.get("global") or {}).get(brand) or [])]
    return " · ".join(parts[:3])

def _week_anchor():
    """'한 방'이 다루는 구간 — **직전 완결 주(월~일)**. 캐시 키도 여기 고정.

    예전엔 today-7~today 롤링이라 창이 매일 하루씩 밀렸다. 연속한 두 창은
    6일이 겹쳐(86% 동일) 입력이 사실상 같은데 LLM만 매일 다시 불렸고,
    그래서 12주 내리 같은 문장이 나왔다. 주 1회 생성으로 바꾼다.

    진행 중인 주를 쓰지 않는 이유 — 화요일엔 2일치를 지난주 7일치와 비교하게
    돼 '멈춘 브랜드'가 매주 월·화에 쏟아진다. 완결된 주끼리 비교해야 증감이
    말이 되고, 한 주 내내 문구도 안 흔들린다.
    """
    from datetime import date, timedelta as _td
    today = date.today()
    this_mon = today - _td(days=today.weekday())
    last_mon = this_mon - _td(days=7)
    return last_mon.isoformat(), (this_mon - _td(days=1)).isoformat()


def _week_delta(session, bko):
    """이번 주에 '새로 생긴 것'만 추린다 — 전주 대비 비교.

    누적 현황을 통째로 주면 모델이 매번 같은 소재(월 단위 수출 수치)를 집는다.
    달라진 것만 줘야 그 주에만 쓸 수 있는 문장이 나온다.
    """
    from sqlalchemy import text as _t
    from datetime import date, timedelta as _td
    # _week_anchor와 같은 구간(직전 완결 주)을 그 전 주와 비교한다.
    mon = date.today() - _td(days=date.today().weekday()) - _td(days=7)
    q = _t(f"""
        SELECT brand, country, COALESCE(activity_type,'') act,
               COALESCE(channel,'') ch, COUNT(*) c
        FROM {DB_SCHEMA}.news_articles
        WHERE published_date >= :a AND published_date < :b
          AND brand IS NOT NULL AND is_duplicate IS NOT TRUE AND is_self IS NOT TRUE
          AND (brand_focus NOT IN ('incidental','unrelated') OR brand_focus IS NULL)
        GROUP BY 1,2,3,4
    """)
    try:
        cur = session.execute(q, {"a": mon.isoformat(),
                                  "b": (mon + _td(days=7)).isoformat()}).fetchall()
        prv = session.execute(q, {"a": (mon - _td(days=7)).isoformat(),
                                  "b": mon.isoformat()}).fetchall()
    except Exception as e:
        logger.warning("주간 델타 조회 실패: %s", e)
        return ""

    def agg(rows):
        brands, pairs, chans, acts = {}, set(), set(), {}
        for b, c, a, ch, n in rows:
            b = (b or "").strip()
            c = (c or "").strip()
            ch = (ch or "").strip()
            if not b:
                continue
            brands[b] = brands.get(b, 0) + n
            if c and c.lower() not in ("null", "none", "global"):
                pairs.add((b, c))
            if ch and ch.lower() not in ("null", "none"):
                chans.add(ch)
            if a:
                acts[a] = acts.get(a, 0) + n
        return brands, pairs, chans, acts

    cb, cp, cc, ca = agg(cur)
    pb, pp, pc, pa = agg(prv)
    out = []
    new_b = sorted(b for b in cb if b not in pb)
    if new_b:
        out.append("그 주 처음 잡힌 브랜드: " + ", ".join(bko(b) for b in new_b[:6]))
    new_pair = sorted((b, c) for (b, c) in cp if (b, c) not in pp)
    if new_pair:
        out.append("새로 등장한 브랜드×국가: " +
                   ", ".join(f"{bko(b)}/{c}" for b, c in new_pair[:8]))
    new_ch = sorted(c for c in cc if c not in pc)
    if new_ch:
        out.append("그 주 새로 나온 판매채널: " + ", ".join(new_ch[:6]))
    surge = sorted(((b, n, pb.get(b, 0)) for b, n in cb.items() if n >= pb.get(b, 0) + 3),
                   key=lambda x: -(x[1] - x[2]))[:5]
    if surge:
        out.append("활동 급증(그 전주→그 주): " +
                   ", ".join(f"{bko(b)} {o}→{n}건" for b, n, o in surge))
    quiet = sorted(b for b in pb if b not in cb and pb[b] >= 3)
    if quiet:
        out.append("그 전주 활발했다가 그 주 멈춘 브랜드: " +
                   ", ".join(bko(b) for b in quiet[:5]))
    act_up = sorted(((a, n, pa.get(a, 0)) for a, n in ca.items() if n >= pa.get(a, 0) + 3),
                    key=lambda x: -(x[1] - x[2]))[:3]
    if act_up:
        out.append("늘어난 활동 유형: " +
                   ", ".join(f"{a} {o}→{n}건" for a, n, o in act_up))
    return chr(10).join(out) if out else "(전주 대비 눈에 띄는 변화 없음)"


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

    wk_from, wk_to = _week_anchor()
    try:    # 주 단위 캐시는 창이 달라 따로 읽는다
        wk_cache = {k: v.get("summary", "")
                    for k, v in get_insights_cache(session, wk_from, wk_to).items()}
    except Exception:
        wk_cache = {}

    def _get_or_gen(key, gen_fn, model, max_tokens, temp=0.4, weekly=False):
        """weekly=True면 이번 주(월~일) 창에 캐시한다 — 주 1회만 생성."""
        nonlocal dirty
        store, d_from, d_to = (wk_cache, wk_from, wk_to) if weekly else (cached, from_date, to_date)
        if key in store:
            return store[key]
        try:
            txt = _llm(model, gen_fn(), max_tokens, temp)
        except Exception as e:
            logger.warning("브리핑 전략 생성 실패 [%s]: %s", key, e)
            txt = ""
        if txt:
            try:
                upsert_insight_cache(session, key, d_from, d_to,
                                     {"summary": txt, "top_act": "brief", "top_pct": 0, "high_pct": 0.0})
                dirty = True
            except Exception:
                session.rollback()
        store[key] = txt
        return txt

    by_brand = defaultdict(list)
    for r in records:
        by_brand[r["brand"]].append(r)

    # 1) 나라별 전략 해석(BS) — 수치 신호를 근거로 함께 넘긴다
    try:
        sigs = collect_brand_signals(session)
    except Exception as e:
        logger.warning("브랜드 신호 수집 실패(해석은 계속): %s", e)
        sigs = {"global": {}, "by_cc": {}}
    strat = {}
    for r in records:
        k = f"BS|{r['brand']}|{r['country']}"
        strat[(r["brand"], r["country"])] = _get_or_gen(
            k, lambda r=r: _p_strat(bko(r["brand"]), r["country"], r,
                                    signal_line(sigs, r["brand"], r["country"])),
            _MODEL_LINE, 160)

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
    delta = _week_delta(session, bko)
    tongp = _get_or_gen("BTONGP",
                        lambda: _p_tongp(len(records), topb, mktxt, examples, delta),
                        _MODEL_TONGP, 200, weekly=True)
    watch_raw = _get_or_gen("BWATCH", lambda: _p_watch(examples, mktxt),
                            _MODEL_TONGP, 200, weekly=True)
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
