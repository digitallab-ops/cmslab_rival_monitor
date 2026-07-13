"""
모니터링 대상 브랜드 및 국가 설정
"""

# Tier 1: 매일 수집
# 티어는 monitored_brands 테이블에서 momentum 기반으로 자동 승급/강등됨.
# 아래 목록은 DB 미시드/장애 시 fallback 시드값.
TIER1_BRANDS = [
    "Anua",
    "Mediheal",
    "Dalba",
    "Beauty of Joseon",  # 조선미녀 — 글로벌 바이럴 급성장
    "Skin1004",          # 스킨1004 — 세포라 입점, 마다가스카르 센텔라
    "Dr.Jart+",          # 닥터자르트 — 에스티로더 인수, 미주 강세
    "Torriden",          # 토리든 — 일본·미국 인플루언서 성장
]

# Tier 2: 주 1회 수집
TIER2_BRANDS = [
    "Cos de Baha",       # 코스드바하 — 소규모, momentum 승급 시 T1
    "By Wishtrend",      # 바이위시트렌드 — 소규모, momentum 승급 시 T1
    "Roundlab",          # 라운드랩
    "Centellian24",      # 센텔리안24
    "VT Cosmetics",      # 브이티
    "Numbuzin",          # 넘버즈인
    "b.plain",           # 비플레인
    "Goodal",            # 구달
    "Abib",              # 아비브
    "Rejuran",           # 리쥬란
    "Mixsoon",           # 믹순
    "Aestura",           # 에스트라
    "Zeroid",            # 제로이드
    "Celimax",           # 셀리맥스
]

ALL_BRANDS = TIER1_BRANDS + TIER2_BRANDS

# Tier 1 국가: 매일 수집 (K-뷰티 핵심 시장)
TIER1_COUNTRIES = ["US", "PL", "JP", "TH", "SG", "CN", "KR", "GB", "CA", "AU", "ID", "MY", "VN"]

# Tier 2 국가: 주 1회 수집 (확장 시장)
TIER2_COUNTRIES = ["DE", "FR"]

# 국가별 언어 코드 + Google News 파라미터
COUNTRIES = {
    "US": {"hl": "en", "gl": "US", "ceid": "US:en", "name": "미국"},
    "PL": {"hl": "pl", "gl": "PL", "ceid": "PL:pl", "name": "폴란드"},
    "JP": {"hl": "ja", "gl": "JP", "ceid": "JP:ja", "name": "일본"},
    "CN": {"hl": "zh-CN", "gl": "CN", "ceid": "CN:zh-Hans", "name": "중국"},
    "TH": {"hl": "th", "gl": "TH", "ceid": "TH:th", "name": "태국"},
    "SG": {"hl": "en", "gl": "SG", "ceid": "SG:en", "name": "싱가포르"},
    "GB": {"hl": "en", "gl": "GB", "ceid": "GB:en", "name": "영국"},
    "CA": {"hl": "en", "gl": "CA", "ceid": "CA:en", "name": "캐나다"},
    "AU": {"hl": "en", "gl": "AU", "ceid": "AU:en", "name": "호주"},
    "DE": {"hl": "de", "gl": "DE", "ceid": "DE:de", "name": "독일"},
    "FR": {"hl": "fr", "gl": "FR", "ceid": "FR:fr", "name": "프랑스"},
    "ID": {"hl": "id", "gl": "ID", "ceid": "ID:id", "name": "인도네시아"},
    "MY": {"hl": "ms", "gl": "MY", "ceid": "MY:ms", "name": "말레이시아"},
    "VN": {"hl": "vi", "gl": "VN", "ceid": "VN:vi", "name": "베트남"},
    "KR": {"hl": "ko", "gl": "KR", "ceid": "KR:ko", "name": "한국"},
}

# 활동 유형 분류 기준
ACTIVITY_TYPES = [
    "신시장_진출",      # 신규 국가 공식 진출, 현지 미디어 최초 등장
    "유통_채널",        # Sephora, Amazon, 올리브영 글로벌 등 채널 입점
    "신제품_런칭",      # 신규 성분/포뮬라 제품 및 카테고리 확장
    "인플루언서_협업",  # KOL, 유튜버, TikTok 바이럴 캠페인
    "투자_BD",          # 투자 유치, 해외 법인 설립, 유통 파트너십, 채용
    "브랜드_마케팅",    # 포지셔닝 변경, 수상, 팝업/전시회
    "기타",
]

# 브랜드별 검색 보조 키워드 (오탐 방지용)
BRAND_CONTEXT_KEYWORDS = {
    "Anua": ["beauty", "skincare", "K-beauty", "Korean"],
    "Mediheal": ["mask", "skincare", "Korean"],
    "Cos de Baha": ["skincare", "Korean", "beauty"],
    "Roundlab": ["skincare", "Korean", "beauty"],
    "Skin1004": ["skincare", "Madagascar", "beauty"],
    "Dr.Jart+": ["skincare", "beauty", "Korean"],
}

# 장업신문 등 한국어 미디어 검색용 브랜드 한국명
BRAND_KO_NAMES: dict[str, list[str]] = {
    "Anua":             ["아누아"],
    "Mediheal":         ["메디힐"],
    "Cos de Baha":      ["코스드바하"],
    "By Wishtrend":     ["바이위시트렌드"],
    "Dalba":            ["달바"],
    "Dr.Jart+":         ["닥터자르트"],
    "Skin1004":         ["스킨1004"],
    "Roundlab":         ["라운드랩"],
    "Centellian24":     ["센텔리안24"],
    "VT Cosmetics":     ["브이티", "VT코스메틱"],
    "Numbuzin":         ["넘버즈인"],
    "b.plain":          ["비플레인"],
    "Goodal":           ["구달"],
    "Torriden":         ["토리든"],
    "Abib":             ["아비브"],
    "Rejuran":          ["리쥬란", "리쥬란코스메틱"],
    "Mixsoon":          ["믹순"],
    "Aestura":          ["에스트라"],
    "Zeroid":           ["제로이드"],
    "Beauty of Joseon": ["조선미녀"],
    "Celimax":          ["셀리맥스"],
}
