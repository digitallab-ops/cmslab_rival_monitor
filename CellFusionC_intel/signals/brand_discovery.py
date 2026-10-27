"""
신흥 브랜드 발견 — 미등록 K뷰티 브랜드를 광역 뉴스에서 자동 탐지 → '후보' 제안.

흐름: 광역 뉴스(구글 RSS, 카테고리 쿼리) → LLM으로 브랜드명 추출 → 이미 모니터링 중인
브랜드 제외 → 언급수 임계 이상만 brand_candidates 테이블에 적재(pending) →
새 후보를 슬랙으로 푸시(사람이 `추가 <브랜드>`로 승인해야 monitored_brands 등록).

자동 등록은 하지 않는다(오탐·잡음 브랜드 유입 방지). 제안만.
수동 실행: python -m signals.brand_discovery
"""

import os
import json
import time
import logging
from datetime import date
from urllib.parse import quote_plus

import feedparser

from sqlalchemy import text

from config.settings import DB_SCHEMA
from config.brands import ALL_BRANDS, SELF_BRANDS, BRAND_KO_NAMES, canonical_brand
from storage.models import get_session

logger = logging.getLogger(__name__)

_RSS = "https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"

# 카테고리 광역 쿼리 — 특정 브랜드가 아니라 '신흥 브랜드 조짐' 자체를 노림.
_QUERIES = [
    ("신상 (스킨케어 OR 화장품 OR 뷰티) 브랜드", "ko", "KR", "KR:ko"),
    ("K뷰티 (신흥 OR 급성장 OR 뜨는) 브랜드", "ko", "KR", "KR:ko"),
    ("올리브영 (신상 OR 인기 OR 랭킹) 브랜드", "ko", "KR", "KR:ko"),
    ("화장품 브랜드 (완판 OR 품절대란 OR 역주행 OR 매출 급증)", "ko", "KR", "KR:ko"),
    ('"korean skincare" brand (launch OR viral OR Sephora OR breakout)', "en-US", "US", "US:en"),
]

_MENTION_MIN = 2            # 한 번의 스냅샷에서 이 이상 언급돼야 관찰 대상
_WATCH_WINDOW_DAYS = 14     # 첫 목격 후 이 기간 안에 재등장해야 제안(넘으면 관찰 재시작)
_MAX_HEADLINES = 110        # LLM 입력 상한(토큰·비용 관리)

# 브랜드가 아닌 것(유통사·플랫폼·제조사·그룹·일반명사) — LLM이 놓쳐도 여기서 확정 제거.
_BLOCK = {
    # 유통·플랫폼
    "olive young", "oliveyoung", "올리브영", "cj올리브영", "cj olive", "다이소", "daiso",
    "다이소몰", "쿠팡", "coupang", "무신사", "musinsa", "세포라", "sephora", "ulta", "아마존",
    "myntra", "nykaa", "notino", "lookfantastic", "asos", "shopee", "lazada",
    "rossmann", "watsons", "sociolla", "freemans", "macy", "boots",
    "amazon", "큐텐", "qoo10", "지그재그", "에이블리", "ably", "컬리", "kurly", "네이버", "naver",
    "카카오", "kakao", "gs샵", "홈쇼핑", "免税", "면세", "올영", "올리브", "더현대",
    "신세계", "롯데", "현대백화점", "이마트", "emart", "왓슨스", "watsons",
    # 유통사 기획전·전용관·행사 이름(브랜드가 아니다)
    "신상", "전용관", "기획전", "페스타", "festa", "세일", "위크", "week", "어워드", "award",
    "팝업", "popup", "pop-up", "챌린지", "challenge", "콜라보", "collabo",
    # 유통사·정부의 중소브랜드 육성·지원 프로그램 이름(브랜드가 아니다).
    # 실제 오탐: 'K-슈퍼루키'(CJ올리브영 동반성장 프로젝트)가 18건 언급으로 후보에 올라왔다.
    # 이런 기사는 "30개사 육성"처럼 브랜드를 '모아서 돕는' 내용이라 제품이 없다.
    "슈퍼루키", "super rookie", "위드 영", "with young", "동반성장", "상생",
    "육성", "지원사업", "공모", "중기부", "진흥원", "협약",
    # 제조사·그룹(모회사)
    "코스맥스", "cosmax", "한국콜마", "콜마", "kolmar", "아모레", "amore", "lg생활건강",
    "lg생건", "lg h&h", "애경", "cj", "cj제일제당",
}

# 위 _BLOCK은 부분일치라서 일반명사를 넣으면 진짜 브랜드까지 막는다.
# 실제로 'cosmetic'이 VT Cosmetics를, 'beauty'가 Beauty of Joseon을,
# '코스메틱'이 리쥬란코스메틱을 막고 있었다 — 이미 등록된 브랜드들이라
# 드러나지 않았을 뿐, 같은 형태의 신규 브랜드는 영영 제안되지 않았다.
# 카테고리 일반명사는 **이름 전체가 그 단어일 때만** 막는다.
_BLOCK_EXACT = {
    "k뷰티", "kbeauty", "k-beauty", "korean beauty", "화장품", "코스메틱", "cosmetic",
    "cosmetics", "skincare", "스킨케어", "뷰티", "beauty", "선크림", "앰플", "토너",
    "마스크팩",
}


def _cmp_key(s: str) -> str:
    """비교용 정규화 — 하이픈·공백·점을 뗀다.

    'K-뷰티'가 차단어 'k뷰티'와 안 맞아 후보로 올라온 적이 있다.
    """
    return "".join(c for c in (s or "").lower() if c.isalnum())


_BLOCK_K = {_cmp_key(b) for b in _BLOCK if _cmp_key(b)}
_BLOCK_EXACT_K = {_cmp_key(b) for b in _BLOCK_EXACT if _cmp_key(b)}


def _is_block(name: str, ko: str) -> bool:
    nk, kk = _cmp_key(name), _cmp_key(ko)
    if nk in _BLOCK_EXACT_K or (kk and kk in _BLOCK_EXACT_K):
        return True
    return any(b in nk or (kk and b in kk) for b in _BLOCK_K)


def _ensure_tables(session) -> None:
    session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.brand_candidates (
            name          VARCHAR(120) PRIMARY KEY,
            ko_name       VARCHAR(120),
            mention_count INTEGER DEFAULT 0,
            sample_titles TEXT,
            status        VARCHAR(20) DEFAULT 'pending',
            first_seen    DATE,
            last_seen     DATE,
            proposed_at   TIMESTAMP WITH TIME ZONE DEFAULT NOW()
        )
    """))
    session.commit()


def _recent_article_titles(session, days: int = 10, limit: int = 160) -> list[str]:
    """이미 수집한 기사 제목도 발견 재료로 쓴다.

    예전엔 광역 RSS 5개 쿼리(최대 110건)만 봤다. 그런데 신규 브랜드는 보통
    기존 브랜드 기사 안에서 먼저 언급된다 — 실제로 구다이글로벌의 신규 브랜드
    '뎁티'가 우리 DB에 18건이나 들어와 있었는데, 조선미녀·티르티르 브랜드
    쿼리로 수집된 것이라 발견 쪽 광역 쿼리에는 한 번도 안 걸렸다.
    이미 긁어온 기사를 다시 쓰는 것이라 수집 비용은 0이다.
    """
    try:
        rows = session.execute(text(f"""
            SELECT t FROM (
              SELECT COALESCE(NULLIF(title_ko,''), title) AS t,
                     MAX(published_date) AS pd
              FROM {DB_SCHEMA}.news_articles
              WHERE published_date >= CURRENT_DATE - :d
                AND is_duplicate IS NOT TRUE
                AND COALESCE(NULLIF(title_ko,''), title) <> ''
              GROUP BY 1
            ) q ORDER BY pd DESC LIMIT :n
        """), {"d": days, "n": limit}).fetchall()
        return [r[0].strip() for r in rows if r[0]]
    except Exception as e:
        logger.warning("발견: 기존 기사 제목 조회 실패: %s", e)
        return []


def _fetch_headlines() -> list[str]:
    seen, out = set(), []
    for q, hl, gl, ceid in _QUERIES:
        url = _RSS.format(q=quote_plus(q), hl=hl, gl=gl, ceid=ceid)
        try:
            feed = feedparser.parse(url)
        except Exception as e:
            logger.warning("발견 RSS 오류 [%s]: %s", q, e)
            continue
        for e in feed.entries[:40]:
            t = (getattr(e, "title", "") or "").strip()
            if t and t not in seen:
                seen.add(t)
                out.append(t)
        time.sleep(1.0)
    return out[:_MAX_HEADLINES]


def _known_set(session) -> tuple[set, set]:
    """이미 등록/제안된 브랜드 제외용. (영문 소문자 set, 한글 set)."""
    en = {b.lower() for b in ALL_BRANDS} | {b.lower() for b in SELF_BRANDS}
    ko = {a for aliases in BRAND_KO_NAMES.values() for a in aliases}
    # 셀퓨전씨 자체 한글명
    ko |= {"셀퓨전씨", "셀퓨전"}
    try:
        for r in session.execute(text(
                f"SELECT name, ko_names FROM {DB_SCHEMA}.monitored_brands")).fetchall():
            if r[0]:
                en.add(r[0].lower())
            for a in (r[1] or []):
                ko.add(a)
    except Exception:
        pass
    try:  # 이미 제안/처리(제외 포함)된 후보는 재제안 안 함.
        # 변형까지 넣어야 'A.P.R.'을 제외해도 'APR'로 다시 올라오는 일이 없다.
        # 단 watching은 빼면 안 된다 — 재포착돼야 pending으로 승격되기 때문이다.
        for r in session.execute(text(
                f"SELECT name, ko_name FROM {DB_SCHEMA}.brand_candidates "
                f"WHERE status <> 'watching'")).fetchall():
            if r[0]:
                en |= {v.lower() for v in _corp_variants(r[0])}
            if r[1]:
                ko |= set(_corp_variants(r[1]))
    except Exception:
        pass
    # 운영사·지주사 이름은 브랜드가 아니다. '에이피알·구다이·달바글로벌, K-뷰티 엇갈린
    # 성장 셈법' 같은 운영사 비교 기사에서 회사명이 브랜드로 잡혀 올라온다(A.P.R. 실제 사례).
    # 승인하면 회사 뉴스(실적·M&A·주가)가 브랜드 활동으로 섞이고, 이미 등록된 산하
    # 브랜드(메디큐브)와 중복 집계된다. NICE 매핑에 있는 회사명을 그대로 제외어로 쓴다.
    # 단, 회사명이 곧 브랜드명인 곳((주)논픽션→논픽션, (주)토리든→토리든)은 빼면 안 된다.
    # 그 회사의 브랜드 태그 안에 회사 이름이 들어 있으면 '회사=브랜드'로 보고 건너뛴다.
    try:
        for co, tags in session.execute(text(
                f"SELECT DISTINCT company, COALESCE(brand_tags,'') FROM {DB_SCHEMA}.nice_company_brands "
                f"WHERE COALESCE(brand_tags,'') <> '' OR COALESCE(matched_brands,'') <> ''")):
            variants = _corp_variants(co)
            base = variants[0] if variants else ""
            for v in variants:      # 가장 짧은 형태가 브랜드명일 가능성이 높다
                if len(v) < len(base):
                    base = v
            if base and base in (tags or ""):
                continue            # 회사=브랜드 → 제외어로 쓰면 진짜 브랜드를 막는다
            for v in variants:
                en.add(v.lower())
                ko.add(v)
    except Exception:
        pass
    return en, ko


def _corp_variants(name: str) -> list:
    """'(주)에이피알' → ['(주)에이피알','에이피알','A.P.R.'처럼 점 제거한 형태'] 비교용 변형."""
    import re as _re
    base = _re.sub(r"\(주\)|\(유\)|㈜|주식회사|유한회사", "", name or "").strip()
    out = {name or "", base}
    out.add(base.replace(" ", ""))
    out.add(base.replace(".", ""))       # A.P.R. ↔ APR
    return [v for v in out if len(v) >= 2]


def _extract_brands(headlines: list[str]) -> list[dict]:
    """LLM으로 헤드라인에서 화장품 브랜드 고유명만 추출. 실패 시 []."""
    if not headlines:
        return []
    joined = "\n".join(f"- {h}" for h in headlines)
    prompt = f"""다음은 최근 뷰티/화장품 뉴스 헤드라인입니다.
여기서 **화장품·스킨케어 '브랜드' 고유명**만 뽑아주세요.

{joined}

규칙:
- 브랜드명만. 유통사(올리브영·쿠팡·무신사·세포라·아마존·큐텐), 성분(나이아신아마이드 등),
  일반명사(선크림·앰플·토너), 인물·매체명은 제외.
- **유통사의 기획전·전용관·행사 이름은 브랜드가 아니다.** 실제 오탐: '올영신상'(올리브영
  신상품 전용관)이 17건 언급으로 후보에 올라왔다. '○○페스타', '○○위크', '○○관',
  '○○신상'처럼 유통사가 만든 코너·행사명은 전부 제외하라.
  판단 기준: 그 이름으로 파는 **제품**이 있으면 브랜드, 제품을 **모아 파는 자리**면 아니다.
- **유통사·정부의 중소브랜드 육성·지원 프로그램 이름도 브랜드가 아니다.** 실제 오탐:
  'K-슈퍼루키'(CJ올리브영이 중기부와 운영하는 동반성장 프로젝트)가 18건 언급으로
  후보에 올라왔다. '○기 가동', '○○개사 육성', '동반성장', '자생력', '협약' 같은 말이
  함께 나오면 프로그램이다. 브랜드를 **모아서 돕는** 이름이지 제품을 파는 이름이 아니다.
- **운영사·제조사·지주사 이름은 브랜드가 아니다.** 대기업(아모레퍼시픽·LG생활건강)뿐 아니라
  신흥 운영사도 제외한다 — 에이피알(A.P.R.), 구다이글로벌, 달바글로벌, 더파운더즈,
  크레이버코퍼레이션, 엘앤피코스메틱, 해브앤비, 서린컴퍼니, 코스맥스, 한국콜마 등.
  판단 기준: '○○사(社)가 ~했다', '○○·○○·○○, 엇갈린 성장' 처럼 **기업 실적·M&A·상장·투자**
  맥락에서 나오는 이름은 회사다. 소비자가 제품에서 보는 이름만 브랜드다.
  (예: '에이피알'은 회사 → 제외, 그 회사의 '메디큐브'가 브랜드)
- 한 브랜드는 한 번만. 한글·영문 혼용이면 대표표기 하나로.
- 확실치 않으면 넣지 말 것(정밀도 우선).
JSON 배열만 출력: [{{"name":"영문 또는 대표표기","ko":"한글표기(없으면 빈문자열)"}}]"""
    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            max_tokens=1200,   # 700이면 묶음에 따라 잘려 JSON 파싱이 깨졌다
            temperature=0.1,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = (resp.choices[0].message.content or "").strip()
        raw = raw[raw.find("["): raw.rfind("]") + 1]
        data = json.loads(raw)
        return [d for d in data if isinstance(d, dict) and (d.get("name") or d.get("ko"))]
    except Exception as e:
        logger.warning("브랜드 추출 실패: %s", e)
        return []


def run() -> dict:
    cap = date.today()
    session = get_session()
    rss = _fetch_headlines()
    # 광역 RSS + 우리가 이미 수집한 기사 제목을 합쳐서 본다.
    mine = _recent_article_titles(session)
    seen, headlines = set(), []
    for t in rss + mine:
        if t and t not in seen:
            seen.add(t)
            headlines.append(t)
    logger.info("발견 재료: RSS %d + 기존기사 %d → 중복제거 %d건",
                len(rss), len(mine), len(headlines))
    if not headlines:
        logger.info("발견: 헤드라인 0건")
        session.close()
        return {"headlines": 0, "new": []}

    # 한 번에 270건을 넣으면 응답이 잘려 JSON 파싱이 깨진다(_MAX_HEADLINES가
    # 110인 이유). 묶음으로 나눠 호출하고 이름 기준으로 합친다.
    # 묶음이 크면 모델이 조용히 일부를 빠뜨린다 — 110건으로 돌렸을 때 '뎁티'가
    # 4번이나 나오는데도 추출 목록에서 빠졌다(4건만 따로 주면 바로 잡는다).
    _CHUNK = 55
    extracted, _seen_name = [], set()
    for i in range(0, len(headlines), _CHUNK):
        for d in _extract_brands(headlines[i:i + _CHUNK]):
            key = ((d.get("name") or d.get("ko") or "").strip().lower())
            if key and key not in _seen_name:
                _seen_name.add(key)
                extracted.append(d)
    logger.info("발견: 묶음 %d회 추출 → 후보명 %d개",
                (len(headlines) + _CHUNK - 1) // _CHUNK, len(extracted))
    new_candidates: list[dict] = []
    try:
        _ensure_tables(session)
        known_en, known_ko = _known_set(session)

        for d in extracted:
            name = (d.get("name") or d.get("ko") or "").strip()
            ko = (d.get("ko") or "").strip()
            if not name or len(name) < 2:
                continue
            if _is_block(name, ko):
                continue
            # 'A.P.R.'과 '에이피알'이 같은 것으로 걸리도록 점·공백을 뗀 형태까지 대조한다
            # 이미 등록된 브랜드면 건너뛴다. LLM이 한글명을 name 칸에 넣는 일이
            # 잦은데(예: name='메디큐브', ko=''), 예전엔 영문 집합하고만 대조해서
            # 메디큐브·아누아·조선미녀 같은 등록 브랜드가 '신규 후보'로 올라왔다.
            # canonical_brand는 한글·영문 표기를 모두 되돌린다.
            if canonical_brand(name) or (ko and canonical_brand(ko)):
                continue
            forms_en = {v.lower() for v in _corp_variants(name)}
            forms_ko = set(_corp_variants(ko)) if ko else set()
            if (forms_en & known_en) or (forms_ko & known_ko):
                continue
            # 언급수 = 이 이름/한글이 등장한 헤드라인 수
            keys = [k for k in {name, ko} if k]
            cnt = sum(1 for h in headlines if any(k in h for k in keys))
            if cnt < _MENTION_MIN:
                continue
            samples = " · ".join(h for h in headlines if any(k in h for k in keys))[:400]

            # 하루치 스냅샷만 보고 제안하면 보도자료 하나가 여러 매체에 실린 것도
            # 후보가 된다(A.P.R.이 그 사례 — 같은 날 운영사 비교 기사 2건).
            # 첫 포착은 watching으로 재워두고, **다른 날** 또 잡혀야 pending으로 올린다.
            # 이름뿐 아니라 한글명으로도 찾는다. 안 그러면 같은 브랜드가 영문('Looncell')과
            # 한글('루온셀')로 각각 후보가 된다(실제로 둘 다 목록에 올라왔다).
            prev = session.execute(text(
                f"SELECT status, first_seen, last_seen, name FROM {DB_SCHEMA}.brand_candidates "
                f"WHERE name = :n OR (:ko <> '' AND (ko_name = :ko OR name = :ko))"),
                {"n": name, "ko": ko or ""}).fetchone()

            if prev is None:
                session.execute(text(f"""
                    INSERT INTO {DB_SCHEMA}.brand_candidates
                        (name, ko_name, mention_count, sample_titles, status, first_seen, last_seen)
                    VALUES (:n, :ko, :c, :s, 'watching', :cap, :cap)
                """), {"n": name, "ko": ko or None, "c": cnt, "s": samples, "cap": cap})
                logger.info("   👀 관찰 시작: %s (언급 %d건)", name, cnt)
                continue

            status, first_seen, last_seen, row_name = prev[0], prev[1], prev[2], prev[3]
            if status != "watching":
                continue                      # pending/approved/rejected는 건드리지 않는다

            if last_seen == cap:
                continue                      # 같은 날 재실행 — 새로운 목격이 아니다

            # 첫 목격이 오래됐으면 연속성이 끊긴 것 → 관찰을 처음부터 다시 센다
            if first_seen and (cap - first_seen).days > _WATCH_WINDOW_DAYS:
                session.execute(text(
                    f"UPDATE {DB_SCHEMA}.brand_candidates SET first_seen=:cap, last_seen=:cap, "
                    f"mention_count=:c, sample_titles=:s WHERE name=:n"),
                    {"n": row_name, "c": cnt, "s": samples, "cap": cap})
                logger.info("   👀 관찰 재시작(간격 초과): %s", name)
                continue

            session.execute(text(f"""
                UPDATE {DB_SCHEMA}.brand_candidates
                SET status='pending', last_seen=:cap, sample_titles=:s,
                    mention_count=GREATEST(mention_count, :c), proposed_at=NOW()
                WHERE name=:n
            """), {"n": row_name, "c": cnt, "s": samples, "cap": cap})
            days = (cap - first_seen).days if first_seen else 0
            logger.info("   ✅ 제안 승격: %s (%d일에 걸쳐 재등장)", name, days)
            new_candidates.append({"name": name, "ko": ko, "count": cnt, "days": days,
                                   "sample": samples.split(" · ")[0] if samples else ""})
        session.commit()
        logger.info("발견: 헤드라인 %d · 추출 %d · 신규 후보 %d",
                    len(headlines), len(extracted), len(new_candidates))
    finally:
        session.close()

    if new_candidates:
        _notify_slack(new_candidates)
    return {"headlines": len(headlines), "extracted": len(extracted), "new": new_candidates}


def _notify_slack(cands: list[dict]) -> None:
    """새 후보를 슬랙으로 제안(제안만 — 승인은 봇에서 `추가 <브랜드>`)."""
    try:
        from notifications.slack import _post, mention_prefix
    except Exception as e:
        logger.warning("슬랙 모듈 로드 실패: %s", e)
        return
    # 승인해야 넘어가는 알림이라 관리자를 멘션한다 — 채널을 음소거해도 멘션은 뜬다
    lines = [f"{mention_prefix()}*🆕 신흥 브랜드 후보 {len(cands)}건* — 레이더에 없던 브랜드가 최근 뉴스에 떴어요",
             "_봇에게(@멘션): `승인 <브랜드>` 등록 · `후보` 목록 · `제외 <브랜드>` 무시_",
             "_※ 등록/제외는 관리자만(SLACK_BRAND_ADMINS). 내 ID는 `내 아이디`_", ""]
    for c in cands[:15]:
        label = c["name"] + (f" ({c['ko']})" if c.get("ko") and c["ko"] != c["name"] else "")
        lines.append(f"• *{label}* — 언급 {c['count']}건")
        if c.get("sample"):
            lines.append(f"    ↳ {c['sample'][:80]}")
    _post({"text": "\n".join(lines)}, secondary=False)


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    print(run())
