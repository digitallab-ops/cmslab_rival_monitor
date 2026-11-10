"""용어 사전 단일 출처 — 국가코드 하나에 한국어명·권역·동의어를 함께 붙인다.

예전엔 같은 국가 지식이 네 군데에 나뉘어 있었다.

    config.brands.COUNTRIES      수집 대상 27개 (hl/gl/ceid)
    config.brands.REGION_MAP     브리핑 권역 25개
    brief_strategy._COUNTRY_KO   화면 한글표기 76개
    value_mappings(DB)           승인된 보정 (코드맵보다 우선)

한 군데만 고치면 다른 데서 어긋났다. 실측(2026-11-10, 최근 180일 유효기사
5,524건): 국가값 67종 중 REGION_MAP에 없는 42종 277건(5.0%)이 주간 브리핑에서
'기타'로 떨어졌고, 그중 ES·PT·CZ·HU·BE·CH·IE·FI는 전부 유럽인데 EU 권역에
못 들어갔다. 같은 영국이 GB 1,310건과 UK 17건으로 쪼개져 있었고, 분류기가
앞에 공백을 붙여 보낸 ' GB'는 또 다른 값이었다.

그래서 **코드가 씨앗, DB가 정본**으로 둔다. 아래 표는 부팅용 기본값이고,
운영 중 추가·수정은 `value_mappings`(kind='country')에 쌓여 코드 배포 없이
덮어쓴다. DB를 못 읽어도 씨앗만으로 돌아가므로 조회 전용 Render에서도 안전하다.

읽는 쪽은 전부 이 모듈의 함수를 쓴다 — 표를 복사해 가면 다시 어긋난다.
"""

import logging
import threading
import time

logger = logging.getLogger(__name__)


# ── 권역 ────────────────────────────────────────────────────────────────────
# 브리핑이 이 코드를 그대로 소제목에 쓴다(scheduler/briefing.py).
# 'ME'는 중동(Middle East)이다 — ISO로는 몬테네그로지만 데이터에 나온 적이 없고,
# 권역 이름이 먼저 자리를 잡았다. 몬테네그로가 나오면 그때 권역 이름을 바꾼다.
REGION_ORDER = ["KR", "APAC", "SEA", "NA", "EU", "ME", "LATAM", "AF",
                "IN", "SASIA", "CIS", "기타"]

_SEED_REGION = {
    "KR": "KR",
    # 동아시아·오세아니아
    "JP": "APAC", "CN": "APAC", "HK": "APAC", "TW": "APAC", "MO": "APAC",
    "AU": "APAC", "NZ": "APAC", "MN": "APAC",
    # 동남아
    "TH": "SEA", "SG": "SEA", "ID": "SEA", "MY": "SEA", "VN": "SEA",
    "PH": "SEA", "LA": "SEA", "KH": "SEA", "MM": "SEA", "BN": "SEA",
    # 북미
    "US": "NA", "CA": "NA",
    # 유럽 — UA는 러시아어권(CIS)이 아니라 유럽 시장으로 묶는다.
    "GB": "EU", "IE": "EU", "FR": "EU", "DE": "EU", "IT": "EU", "ES": "EU",
    "PT": "EU", "NL": "EU", "BE": "EU", "LU": "EU", "CH": "EU", "AT": "EU",
    "DK": "EU", "SE": "EU", "NO": "EU", "FI": "EU", "IS": "EU", "PL": "EU",
    "CZ": "EU", "SK": "EU", "HU": "EU", "RO": "EU", "BG": "EU", "HR": "EU",
    "RS": "EU", "SI": "EU", "GR": "EU", "EE": "EU", "LV": "EU", "LT": "EU",
    "MT": "EU", "CY": "EU", "UA": "EU", "MD": "EU", "AL": "EU", "BA": "EU",
    # 중동 — 튀르키예는 뷰티 유통상 중동권으로 본다.
    "AE": "ME", "SA": "ME", "QA": "ME", "KW": "ME", "BH": "ME", "OM": "ME",
    "IL": "ME", "JO": "ME", "LB": "ME", "TR": "ME", "IQ": "ME",
    # 중남미
    "BR": "LATAM", "MX": "LATAM", "CL": "LATAM", "PE": "LATAM", "CO": "LATAM",
    "AR": "LATAM", "GT": "LATAM", "DO": "LATAM", "PY": "LATAM", "UY": "LATAM",
    "EC": "LATAM", "BO": "LATAM", "CR": "LATAM", "PA": "LATAM", "VE": "LATAM",
    "KY": "LATAM", "HN": "LATAM", "SV": "LATAM", "NI": "LATAM", "JM": "LATAM",
    "TT": "LATAM",
    # 아프리카 — 북아프리카(EG·MA)도 여기로 묶는다.
    "ZA": "AF", "EG": "AF", "NG": "AF", "KE": "AF", "MA": "AF", "GH": "AF",
    "TZ": "AF", "DZ": "AF", "TN": "AF", "MG": "AF", "MW": "AF", "UG": "AF",
    "ET": "AF", "CI": "AF", "SN": "AF",
    # 인도 — 단일 시장 규모가 커서 권역을 따로 둔다(기존 동작 유지).
    "IN": "IN",
    # 남아시아 나머지
    "PK": "SASIA", "BD": "SASIA", "LK": "SASIA", "NP": "SASIA",
    "BT": "SASIA", "MV": "SASIA",
    # 러시아·CIS(러시아어권)
    "RU": "CIS", "KZ": "CIS", "UZ": "CIS", "BY": "CIS", "GE": "CIS",
    "KG": "CIS", "AM": "CIS", "AZ": "CIS", "TJ": "CIS", "TM": "CIS",
}

# ── 한국어 표기 ─────────────────────────────────────────────────────────────
_SEED_KO = {
    "US": "미국", "JP": "일본", "KR": "한국", "CN": "중국", "GB": "영국",
    "PL": "폴란드", "SG": "싱가포르", "TH": "태국", "CA": "캐나다", "AU": "호주",
    "DE": "독일", "FR": "프랑스", "ID": "인도네시아", "MY": "말레이시아",
    "VN": "베트남", "PH": "필리핀", "IT": "이탈리아", "IN": "인도", "MX": "멕시코",
    "RU": "러시아", "ES": "스페인", "PT": "포르투갈", "KE": "케냐",
    "UZ": "우즈베키스탄", "AE": "아랍에미리트", "BR": "브라질", "SA": "사우디",
    "NL": "네덜란드", "SE": "스웨덴", "TR": "튀르키예", "HK": "홍콩", "TW": "대만",
    "GT": "과테말라", "LA": "라오스", "KZ": "카자흐스탄", "BY": "벨라루스",
    "ZA": "남아공", "CO": "콜롬비아", "CL": "칠레", "PE": "페루",
    "AR": "아르헨티나", "EG": "이집트", "NG": "나이지리아", "PK": "파키스탄",
    "BD": "방글라데시", "NZ": "뉴질랜드", "IE": "아일랜드", "AT": "오스트리아",
    "BE": "벨기에", "CH": "스위스", "CZ": "체코", "DK": "덴마크", "FI": "핀란드",
    "GR": "그리스", "HU": "헝가리", "NO": "노르웨이", "RO": "루마니아",
    "UA": "우크라이나", "IL": "이스라엘", "QA": "카타르", "KW": "쿠웨이트",
    # 2026-11-10 추가 — 기사에 등장했는데 한국어명이 없어 코드로 노출되던 것들.
    # 티르티르 몽골 울란바토르 첫 팝업(MN)처럼 신시장 진출 신호가 섞여 있다.
    "MN": "몽골", "GE": "조지아", "BG": "불가리아", "DO": "도미니카공화국",
    "GH": "가나", "HR": "크로아티아", "KY": "케이맨제도", "LK": "스리랑카",
    "MA": "모로코", "NP": "네팔", "PY": "파라과이", "RS": "세르비아",
    # 2026-11-11 추가 — 용어 단일화하며 데이터에서 발견된 나머지.
    "MG": "마다가스카르", "KG": "키르기스스탄", "MW": "말라위", "EC": "에콰도르",
    "BH": "바레인", "OM": "오만", "JO": "요르단", "LB": "레바논", "IQ": "이라크",
    "SK": "슬로바키아", "SI": "슬로베니아", "EE": "에스토니아", "LV": "라트비아",
    "LT": "리투아니아", "IS": "아이슬란드", "LU": "룩셈부르크", "MT": "몰타",
    "CY": "키프로스", "MD": "몰도바", "AL": "알바니아", "BA": "보스니아",
    "KH": "캄보디아", "MM": "미얀마", "BN": "브루나이", "MO": "마카오",
    "UY": "우루과이", "BO": "볼리비아", "CR": "코스타리카", "PA": "파나마",
    "VE": "베네수엘라", "HN": "온두라스", "SV": "엘살바도르", "NI": "니카라과",
    "JM": "자메이카", "TT": "트리니다드토바고", "TZ": "탄자니아",
    "DZ": "알제리", "TN": "튀니지", "UG": "우간다", "ET": "에티오피아",
    "CI": "코트디부아르", "SN": "세네갈", "AM": "아르메니아",
    "AZ": "아제르바이잔", "TJ": "타지키스탄", "TM": "투르크메니스탄",
    "BT": "부탄", "MV": "몰디브",
}

# ── 동의어 ──────────────────────────────────────────────────────────────────
# 같은 나라가 두 코드로 쪼개져 집계가 나뉘던 것들. 읽을 때 왼쪽을 오른쪽으로 접는다.
_SEED_ALIAS = {
    "UK": "GB",     # 영국 — GB 1,310건과 UK 17건으로 나뉘어 있었다
    "UAE": "AE",
    "LAT": "LATAM",  # 데이터상 중남미를 뜻했다. 라트비아(LV)가 아니다
    "ROK": "KR", "USA": "US", "KOR": "KR",
}

# ── 나라가 아닌 값 ──────────────────────────────────────────────────────────
# 분류기가 국가 칸에 넣어 버린 값들. 나라로 세면 안 되지만, 권역을 알 수 있는
# 것(EU·LATAM·GCC)은 권역만 살려서 브리핑 '기타'로 떨어지지 않게 한다.
# EU는 혼자 166건이라 이것만으로도 '기타'가 크게 줄어든다.
_NON_COUNTRY_REGION = {
    "EU": "EU", "LATAM": "LATAM", "GCC": "ME",
    # NA는 ISO로 나미비아지만, 우리 데이터에서는 분류기가 권역 코드(북미)를
    # 국가 칸에 쓴 것이었다 — 4건 전부 "북미·남미 진출" 기사였고 나미비아는
    # 한 건도 없었다. 나미비아 기사가 실제로 들어오면 value_mappings에
    # NA를 등록해 이 줄을 덮는다.
    "NA": "NA",
    "APAC": "APAC",
}
# 나라는 아니어도 화면에는 나간다. 한글 라벨이 없으면 코드가 그대로 노출된다 —
# 역검증에서 country='EU' 162건이 '유럽'에서 'EU'로 되돌아가는 걸 잡았다.
_NON_COUNTRY_KO = {
    "EU": "유럽", "LATAM": "중남미", "GCC": "걸프 6개국", "EMEA": "유럽·중동·아프리카",
    "GLOBAL": "글로벌", "NA": "북미", "APAC": "아시아태평양", "WORLD": "글로벌",
}
# 권역조차 알 수 없는 값 — 집계에서 빠진다. 파수꾼이 분류기 문제로 올린다.
_NOT_A_PLACE = {
    "", "NULL", "NONE", "GLOBAL", "UNRELATED", "OTHER", "ALL", "N/A",
    "UN", "EMEA", "WORLD", "ETC",
}

_NON_COUNTRY = set(_NON_COUNTRY_REGION) | _NOT_A_PLACE


# ── DB 정본 덮어쓰기 ────────────────────────────────────────────────────────
# value_mappings(kind='country')의 active 행이 씨앗을 덮는다. 코드 배포 없이
# 슬랙 승인만으로 반영되는 경로를 그대로 쓴다(storage/repository.py).
_TTL = 600  # 초. 스케줄러가 몇 시간씩 떠 있으므로 주기적으로 다시 읽는다.
_lock = threading.Lock()
_db_ko: dict = {}
_db_region: dict = {}
_db_alias: dict = {}
_loaded_at = 0.0


def load_db(session=None, force: bool = False) -> bool:
    """DB 정본을 읽어 캐시에 올린다. 실패해도 씨앗으로 계속 돈다(False 반환)."""
    global _db_ko, _db_region, _db_alias, _loaded_at
    with _lock:
        if not force and _loaded_at and time.time() - _loaded_at < _TTL:
            return True
        own = session is None
        try:
            from sqlalchemy import text
            from config.settings import DB_SCHEMA
            if own:
                from storage.models import get_session
                session = get_session()
            from storage.repository import _ensure_value_mappings
            _ensure_value_mappings(session)
            rows = session.execute(text(
                f"SELECT code, value, region, canonical FROM {DB_SCHEMA}.value_mappings "
                f"WHERE kind='country' AND status='active'")).fetchall()
            ko, reg, al = {}, {}, {}
            for code, value, region, canonical in rows:
                c = (code or "").strip().upper()
                if not c:
                    continue
                # 코드를 값으로 그대로 돌려주는 행은 쓰레기다(옛 파수꾼이 LLM
                # 실패 시 코드=코드를 제안했고 승인된 적이 있다). 무시한다.
                if value and value.strip().upper() != c:
                    ko[c] = value.strip()
                if region and region.strip():
                    reg[c] = region.strip().upper()
                if canonical and canonical.strip().upper() != c:
                    al[c] = canonical.strip().upper()
            _db_ko, _db_region, _db_alias = ko, reg, al
            _loaded_at = time.time()
            return True
        except Exception as e:
            logger.debug("용어사전 DB 로드 실패(씨앗으로 계속): %s", e)
            _loaded_at = time.time()   # 매 호출마다 재시도하지 않는다
            return False
        finally:
            if own and session is not None:
                try:
                    session.close()
                except Exception:
                    pass


def refresh() -> bool:
    """다음 조회 때 DB를 다시 읽게 한다(승인 직후 호출)."""
    return load_db(force=True)


# ── 조회 ────────────────────────────────────────────────────────────────────
def norm_country(code) -> str:
    """국가값 정규화 — 공백·대소문자를 맞추고 동의어를 대표코드로 접는다.

    ' GB'·'uk'·'UK'가 전부 'GB'가 된다. 나라가 아닌 값은 그대로 돌려주므로,
    나라인지 아닌지는 is_country()로 따로 묻는다.
    """
    c = (code or "").strip().upper()
    if not c:
        return ""
    load_db()
    return _db_alias.get(c) or _SEED_ALIAS.get(c) or c


def is_country(code) -> bool:
    """실제 나라인가. EU·GLOBAL·NULL 같은 값은 False."""
    c = norm_country(code)
    if not c or c in _NON_COUNTRY:
        return False
    load_db()
    return c in _db_ko or c in _db_region or c in _SEED_KO or c in _SEED_REGION


def country_ko(code) -> str:
    """국가코드 → 한국어명. 모르면 코드를 그대로 돌려준다(화면에 빈칸보단 낫다)."""
    c = norm_country(code)
    if not c:
        return ""
    load_db()
    return _db_ko.get(c) or _SEED_KO.get(c) or _NON_COUNTRY_KO.get(c) or c


def country_region(code, default: str = "기타") -> str:
    """국가코드 → 권역. EU·GCC처럼 나라가 아니어도 권역을 알면 돌려준다."""
    c = norm_country(code)
    if not c:
        return default
    load_db()
    return (_db_region.get(c) or _SEED_REGION.get(c)
            or _NON_COUNTRY_REGION.get(c) or default)


def unknown_countries(codes) -> list:
    """사전에 없는 값만 추려 돌려준다 — 파수꾼이 제안할 대상."""
    out = []
    for raw in codes:
        c = norm_country(raw)
        if not c or c in _NOT_A_PLACE:
            continue
        if not country_region(c, "") or country_ko(c) == c:
            out.append(c)
    return sorted(set(out))


# ── 국기 ────────────────────────────────────────────────────────────────────
class _Flags:
    """국가코드 → 국기 이모지. 표가 아니라 코드에서 바로 만든다.

    국기 이모지는 ISO 두 글자를 지역표시기호로 바꾼 것일 뿐이라 표가 필요 없다.
    예전엔 dashboard/generate.py와 notifications/slack.py가 27개짜리 표를 각자
    들고 있어서 칠레·뉴질랜드·몽골처럼 나중에 들어온 나라는 국기가 없었다.

    dict처럼 .get(code, default)로 쓸 수 있게 해 뒀다 — 호출부를 안 건드린다.
    """

    @staticmethod
    def get(code, default="🌐"):
        c = norm_country(code)
        if len(c) != 2 or not c.isalpha() or not is_country(c):
            return default
        return chr(0x1F1E6 + ord(c[0]) - 65) + chr(0x1F1E6 + ord(c[1]) - 65)

    def __getitem__(self, code):
        return self.get(code)

    def __contains__(self, code):
        return self.get(code, "") != ""


COUNTRY_FLAGS = _Flags()


# ── 되읽기용 별칭 ───────────────────────────────────────────────────────────
# 기존 코드가 표를 직접 import 하던 자리를 깨지 않게 둔다. 새 코드는 함수를 쓸 것.
COUNTRY_KO = _SEED_KO
COUNTRY_REGION = _SEED_REGION
