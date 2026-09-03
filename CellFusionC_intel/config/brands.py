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

# 자사(씨엠에스랩 대표 브랜드) — 경쟁사 집계와 분리하는 기준선(baseline).
# 경쟁사 목록(ALL_BRANDS)엔 넣지 않고, 전용 수집 잡 + is_self 플래그로 분리한다.
SELF_BRANDS = ["CellFusionC"]

# Tier 1 국가: 매일 수집 (K-뷰티 핵심 시장)
# IN·PH 승격(2026-08): 영어권 대형 K-뷰티 시장인데 주간(1회)이라 수집량 과소 → 매일로.
TIER1_COUNTRIES = ["US", "PL", "JP", "TH", "SG", "CN", "KR", "GB", "CA", "AU",
                   "ID", "MY", "VN", "IN", "PH"]

# Tier 2 국가: 주 1회 수집 (확장 시장 + 가이드 신규 권역 — 비용 위해 주간 티어)
# 참고: 주간 풀스캔은 COUNTRIES 전체를 돌므로 이 목록은 문서/분류용(코드 게이팅은 TIER1만 사용).
TIER2_COUNTRIES = ["DE", "FR", "IT", "AE", "SA", "BR", "MX", "ZA",
                   "RU", "KZ", "UZ", "BY"]  # 러시아·CIS(러시아어권)

# 국가별 언어 코드 + Google News 파라미터 (신규 로케일은 실제 수집 검증 완료)
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
    # ── 가이드 신규 권역 ──────────────────────────────────────
    "IT": {"hl": "it",     "gl": "IT", "ceid": "IT:it",     "name": "이탈리아"},
    "AE": {"hl": "en",     "gl": "AE", "ceid": "AE:en",     "name": "UAE"},
    "SA": {"hl": "ar",     "gl": "SA", "ceid": "SA:ar",     "name": "사우디"},
    "BR": {"hl": "pt-BR",  "gl": "BR", "ceid": "BR:pt-419", "name": "브라질"},
    "MX": {"hl": "es-419", "gl": "MX", "ceid": "MX:es-419", "name": "멕시코"},
    "IN": {"hl": "en",     "gl": "IN", "ceid": "IN:en",     "name": "인도"},
    "PH": {"hl": "en",     "gl": "PH", "ceid": "PH:en",     "name": "필리핀"},
    "ZA": {"hl": "en",     "gl": "ZA", "ceid": "ZA:en",     "name": "남아공"},
    # ── 러시아·CIS (러시아어권) ──────────────────────────────
    "RU": {"hl": "ru",     "gl": "RU", "ceid": "RU:ru",     "name": "러시아"},
    "KZ": {"hl": "ru",     "gl": "KZ", "ceid": "KZ:ru",     "name": "카자흐스탄"},
    "UZ": {"hl": "ru",     "gl": "UZ", "ceid": "UZ:ru",     "name": "우즈베키스탄"},
    "BY": {"hl": "ru",     "gl": "BY", "ceid": "BY:ru",     "name": "벨라루스"},
}

# 국가 → 권역 (대시보드 필터·보고서 그룹핑)
REGION_MAP = {
    "GB": "EU", "PL": "EU", "DE": "EU", "FR": "EU", "IT": "EU",
    "AE": "ME", "SA": "ME",
    "BR": "LATAM", "MX": "LATAM",
    "ZA": "AF",
    "TH": "SEA", "SG": "SEA", "ID": "SEA", "MY": "SEA", "VN": "SEA", "PH": "SEA",
    "IN": "IN",
    "US": "NA", "CA": "NA",
    "JP": "APAC", "CN": "APAC", "AU": "APAC", "KR": "KR",
    "RU": "CIS", "KZ": "CIS", "UZ": "CIS", "BY": "CIS",
}

# 현지어 활동 키워드 (google_rss 주간 심층수집에서 브랜드명과 결합 → 현지 기사 recall↑)
LOCALE_KEYWORDS = {
    "BR": ["cosméticos coreanos", "marca coreana", "lançamento", "chega ao Brasil"],
    "MX": ["cosmética coreana", "lanzamiento", "llega a México", "distribuidor"],
    "PL": ["kosmetyki koreańskie", "koreańska marka", "debiut", "wchodzi do Polski"],
    "AE": ["Korean beauty", "K-beauty launch", "market entry"],
    "SA": ["مستحضرات التجميل الكورية", "علامة كورية", "إطلاق", "دخول السوق"],
    "ID": ["skincare Korea", "brand Korea", "resmi hadir", "masuk Indonesia"],
    "TH": ["เครื่องสำอางเกาหลี", "สกินแคร์เกาหลี", "เปิดตัว", "เข้าไทย"],
    "VN": ["mỹ phẩm Hàn Quốc", "thương hiệu Hàn Quốc", "ra mắt", "chính thức có mặt"],
    "IT": ["cosmetici coreani", "skincare coreana", "lancio", "arriva in Italia"],
    # ── 공백 보강(2026-08): 매일 Tier1인데 현지어 쿼리가 없던 비영어권 + 승격시장 ──
    "CN": ["韩国化妆品", "韩国护肤", "韩妆品牌", "上市", "进入中国市场"],
    "MY": ["kosmetik Korea", "jenama Korea", "penjagaan kulit Korea", "dilancarkan", "masuk Malaysia"],
    "DE": ["koreanische Kosmetik", "K-Beauty", "koreanische Marke", "Markteinführung", "kommt nach Deutschland"],
    "FR": ["cosmétiques coréens", "marque coréenne", "K-beauté", "lancement", "arrive en France"],
    "JP": ["韓国コスメ", "韓国スキンケア", "韓国ブランド", "新発売", "日本上陸"],
    # 영어권이지만 'K-beauty'·현지어 부스터로 recall↑ (인도=힌디 일부, 필리핀=따갈로그 일부)
    "IN": ["Korean beauty", "K-beauty", "Korean skincare", "कोरियाई ब्यूटी", "launches in India"],
    "PH": ["Korean beauty", "K-beauty", "Korean skincare", "kosmetiko Korea", "now in the Philippines"],
    # 러시아·CIS — 러시아어 공통(카자흐·우즈벡·벨라루스도 러시아어 매체 다수)
    "RU": ["корейская косметика", "корейский бренд", "запуск", "выходит на рынок", "уходовая косметика"],
    "KZ": ["корейская косметика", "корейский бренд", "Казахстан", "Golden Apple", "запуск"],
    "UZ": ["корейская косметика", "корейский бренд", "Узбекистан", "Uzum", "запуск"],
    "BY": ["корейская косметика", "корейский бренд", "Беларусь", "запуск", "открытие магазина"],
}

# 자사(셀퓨전씨) 카테고리 × 경쟁 활동 "대결 뷰" — 우리 카테고리 → 경쟁기사 매칭 키워드
# (제품명+details+제목에서 매칭. Cafe24 카탈로그 카테고리와 정렬. 선케어=우리 간판)
OUR_CATEGORIES = ["선케어", "크림", "앰플/세럼", "마스크팩", "클렌징", "토너", "비비/쿠션"]
CATEGORY_KEYWORDS = {
    "선케어":    ["선크림", "선스크린", "썬스크린", "선스틱", "자외선", "톤업", "sunscreen", "sun stick", "spf", "uv", "suncare"],
    "크림":      ["크림", "수분크림", "아이크림", "cream", "moisturizer"],
    "앰플/세럼":  ["앰플", "세럼", "부스터", "ampoule", "serum", "pdrn", "펩타이드", "peptide", "essence", "에센스"],
    "마스크팩":   ["마스크팩", "마스크", "시트마스크", "sheet mask", "mask pack", "패드", "pad"],
    "클렌징":    ["클렌징", "클렌저", "클렌징폼", "클렌징오일", "cleansing", "cleanser", "foam", "미셀라"],
    "토너":      ["토너", "toner", "토닝패드", "toner pad"],
    "비비/쿠션":  ["비비", "쿠션", "bb크림", "bb cream", "cushion", "선베이스"],
}

# 활동 유형 분류 기준
ACTIVITY_TYPES = [
    "신시장_진출",      # 신규 국가 공식 진출, 현지 미디어 최초 등장
    "유통_채널",        # Sephora, Amazon, 올리브영 글로벌 등 채널 입점
    "신제품_런칭",      # 신규 성분/포뮬라 제품 및 카테고리 확장
    "인플루언서_협업",  # KOL, 유튜버, TikTok 바이럴 캠페인
    "투자_BD",          # 투자 유치, 해외 법인 설립, 유통 파트너십, 채용
    "브랜드_마케팅",    # 포지셔닝 변경, 팝업/전시회, PR·광고, 콜라보, 브랜드 스토리
    "실적_공시",        # 매출·실적 공시, 수상·인증·랭킹, 규제·법적, 경영진 인사
    "기타",
]

# 활동유형 4대분류 (전 탭 공통 — 브리핑·경쟁사·기록·슬랙 동일 적용).
# 9개 나열형은 위계가 없어 '기타'로 몰려 보임 → 4개 대분류로 묶어 스캔성 개선.
ACTIVITY_GROUPS = {
    "제품":     ["신제품_런칭"],
    "마케팅":   ["인플루언서_협업", "브랜드_마케팅", "가격_프로모션"],
    "채널":     ["유통_채널", "신시장_진출"],
    "투자·BD":  ["투자_BD"],
}
ACTIVITY_GROUP_ORDER = ["제품", "마케팅", "채널", "투자·BD", "기타"]
ACTIVITY_GROUP_OF = {t: g for g, ts in ACTIVITY_GROUPS.items() for t in ts}
# 실적_공시 = 분석·표시에서 제외(적재만). 그 외 미매핑 유형은 '기타'로.
STORE_ONLY_ACTS = {"실적_공시"}


def activity_group(activity_type: str) -> str:
    """활동유형(세부 9종) → 4대분류. 실적_공시는 STORE_ONLY(표시 제외 판정은 호출측),
    미매핑은 '기타'."""
    return ACTIVITY_GROUP_OF.get(activity_type, "기타")

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
    "CellFusionC":      ["셀퓨전씨", "셀퓨전C", "셀퓨전시씨"],   # 자사
}
