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
from config.brands import ALL_BRANDS, SELF_BRANDS, BRAND_KO_NAMES
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

_MENTION_MIN = 2            # 이 이상 언급된 후보만 제안
_MAX_HEADLINES = 110        # LLM 입력 상한(토큰·비용 관리)

# 브랜드가 아닌 것(유통사·플랫폼·제조사·그룹·일반명사) — LLM이 놓쳐도 여기서 확정 제거.
_BLOCK = {
    # 유통·플랫폼
    "olive young", "oliveyoung", "올리브영", "cj올리브영", "cj olive", "다이소", "daiso",
    "다이소몰", "쿠팡", "coupang", "무신사", "musinsa", "세포라", "sephora", "ulta", "아마존",
    "amazon", "큐텐", "qoo10", "지그재그", "에이블리", "ably", "컬리", "kurly", "네이버", "naver",
    "카카오", "kakao", "gs샵", "홈쇼핑", "免税", "면세",
    # 제조사·그룹(모회사)
    "코스맥스", "cosmax", "한국콜마", "콜마", "kolmar", "아모레", "amore", "lg생활건강",
    "lg생건", "lg h&h", "애경", "cj", "cj제일제당",
    # 일반명사·카테고리
    "k뷰티", "kbeauty", "k-beauty", "korean beauty", "화장품", "코스메틱", "cosmetic",
    "skincare", "스킨케어", "뷰티", "beauty", "선크림", "앰플", "토너", "마스크팩",
}


def _is_block(name: str, ko: str) -> bool:
    nl, kl = (name or "").lower(), (ko or "").lower()
    return any(b in nl or (kl and b in kl) for b in _BLOCK)


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
        for r in session.execute(text(
                f"SELECT name, ko_name FROM {DB_SCHEMA}.brand_candidates")).fetchall():
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
            max_tokens=700,
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
    headlines = _fetch_headlines()
    if not headlines:
        logger.info("발견: 헤드라인 0건")
        return {"headlines": 0, "new": []}

    extracted = _extract_brands(headlines)
    session = get_session()
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
            session.execute(text(f"""
                INSERT INTO {DB_SCHEMA}.brand_candidates
                    (name, ko_name, mention_count, sample_titles, status, first_seen, last_seen)
                VALUES (:n, :ko, :c, :s, 'pending', :cap, :cap)
                ON CONFLICT (name) DO UPDATE SET
                    mention_count = GREATEST({DB_SCHEMA}.brand_candidates.mention_count, EXCLUDED.mention_count),
                    sample_titles = EXCLUDED.sample_titles, last_seen = EXCLUDED.last_seen
            """), {"n": name, "ko": ko or None, "c": cnt, "s": samples, "cap": cap})
            new_candidates.append({"name": name, "ko": ko, "count": cnt,
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
        from notifications.slack import _post
    except Exception as e:
        logger.warning("슬랙 모듈 로드 실패: %s", e)
        return
    lines = [f"*🆕 신흥 브랜드 후보 {len(cands)}건* — 레이더에 없던 브랜드가 최근 뉴스에 떴어요",
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
