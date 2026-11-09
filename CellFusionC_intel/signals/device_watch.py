"""디바이스 진출 감시 — 추적 중인 스킨케어 브랜드가 뷰티 디바이스를 내는지 지켜본다.

자사 디바이스 사업이 시작되면서 경쟁 구도가 바뀌었다. 지금 디바이스 경쟁사로
등록된 곳들(톰더글로우·쿼드쎄라·메이크온 등)은 처음부터 디바이스 회사지만,
진짜 위협은 **이미 스킨케어로 싸우고 있는 브랜드가 디바이스로 넘어오는 것**이다.
실제로 달바가 '시그니처 울쎄라 더블샷'으로 3분기 누적 30억을 냈는데, 우리는
달바를 스킨케어로만 보고 있었다.

사람이 매번 뉴스를 훑을 수는 없으니 수집된 기사에서 자동으로 잡아 슬랙에 올린다.
브랜드 발견(brand_discovery)과 같은 자리 — 제안만 하고 판단은 사람이 한다.

오탐을 줄이는 두 가지 장치:
  1) 업계 동향 기사 제외 — "K뷰티 다음은 K미용기기" 같은 기사에 브랜드가
     예시로 한 줄 끼는 경우가 대부분이다. brand_focus=primary만 본다.
  2) 출시 동사 요구 — '디바이스'라는 단어만으로는 부족하고, 내놨다는 말이
     같이 있어야 한다. 전시회 참관·시장 전망 기사가 이걸로 걸러진다.
"""

import logging
import os
import re
from datetime import datetime

from sqlalchemy import text

from storage.models import get_session
from config.settings import DB_SCHEMA

logger = logging.getLogger(__name__)

# 디바이스 자체를 가리키는 말. 기전(LDM·HIFU·고주파)까지 넣는 이유는
# 제품명만 적고 '디바이스'라는 낱말은 안 쓰는 기사가 많아서다.
_DEVICE_TERMS = (
    r"디바이스|뷰티\s?디바이스|미용\s?기기|미용기기|뷰티\s?기기|홈케어\s?기기|"
    r"LED\s?마스크|고주파|초음파|갈바닉|EMS|리프팅\s?기기|뷰티테크|"
    r"HIFU|하이푸|LDM|울쎄라|슈링크|부스터기|관리기|beauty\s?device"
)

# '냈다'는 뜻의 말. 이게 없으면 전망·동향 기사다.
_LAUNCH_TERMS = (
    r"출시|론칭|런칭|공개|선보|내놓|출시한다|신제품|진출|사업\s?확대|"
    r"판매\s?개시|예약\s?판매|와디즈|펀딩|launch|unveil|release"
)

_RE_DEVICE = re.compile(_DEVICE_TERMS, re.I)
_RE_LAUNCH = re.compile(_LAUNCH_TERMS, re.I)

# 디바이스가 아닌 '시술'을 말하는 기사. 울쎄라·슈링크는 병원 시술명이기도 해서
# 이 말이 같이 있으면 가정용 기기 기사가 아니다.
_RE_CLINIC = re.compile(r"피부과\s?시술|병원\s?시술|시술\s?받|의원|클리닉\s?방문", re.I)

# 1건부터 알린다. 2건으로 뒀더니 달바의 '90만원 미용기기 승부수'(실제 출시,
# 3분기 누적 30억)가 안 잡혔다. 위 세 필터(primary + 디바이스어 + 출시어)가
# 이미 충분히 조여서, 180일을 훑어도 걸리는 브랜드가 달바 하나뿐이다.
# 놓치는 비용이 헛알림 비용보다 크고, 한 번 알린 브랜드는 다시 안 올린다.
MIN_HITS = 1
LOOKBACK_DAYS = 90


def _ensure_table(session) -> None:
    """이미 알린 브랜드를 기억해 매주 같은 걸 또 올리지 않게 한다."""
    session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.device_watch_log (
            brand       TEXT PRIMARY KEY,
            first_seen  DATE NOT NULL,
            notified_at TIMESTAMPTZ,
            hits        INTEGER DEFAULT 0,
            sample      TEXT
        )
    """))
    session.commit()


def _candidates(session) -> list[dict]:
    """스킨케어로만 등록된 브랜드 중 디바이스 신호가 잡힌 곳."""
    rows = session.execute(text(f"""
        SELECT a.brand,
               COALESCE(NULLIF(a.title_ko,''), a.title) AS title,
               COALESCE(a.details,'') AS details,
               a.published_date::date::text AS d,
               a.source_url
        FROM {DB_SCHEMA}.news_articles a
        JOIN {DB_SCHEMA}.monitored_brands m ON m.name = a.brand
        WHERE m.is_active
          AND m.segment = 'skincare'          -- 이미 device/both면 볼 필요 없다
          AND a.is_duplicate IS NOT TRUE
          AND a.is_self IS NOT TRUE
          AND a.brand_focus = 'primary'        -- 업계 동향 기사 제외(핵심 오탐 차단)
          AND a.published_date >= now() - (:days || ' days')::interval
    """), {"days": LOOKBACK_DAYS}).fetchall()

    agg: dict[str, dict] = {}
    for brand, title, details, d, url in rows:
        blob = f"{title} {details}"
        if not _RE_DEVICE.search(blob) or not _RE_LAUNCH.search(blob):
            continue
        if _RE_CLINIC.search(blob):
            continue
        e = agg.setdefault(brand, {"brand": brand, "hits": 0, "sample": "", "url": "", "date": d})
        e["hits"] += 1
        if not e["sample"]:
            e["sample"], e["url"], e["date"] = title, url or "", d
    return [v for v in agg.values() if v["hits"] >= MIN_HITS]


def run(notify: bool = True) -> dict:
    """감시 1회 실행. 새로 잡힌 브랜드만 슬랙으로 알린다."""
    session = get_session()
    try:
        _ensure_table(session)
        cands = _candidates(session)

        already = {r[0] for r in session.execute(text(
            f"SELECT brand FROM {DB_SCHEMA}.device_watch_log WHERE notified_at IS NOT NULL"
        )).fetchall()}
        fresh = [c for c in cands if c["brand"] not in already]

        for c in cands:
            session.execute(text(f"""
                INSERT INTO {DB_SCHEMA}.device_watch_log (brand, first_seen, hits, sample)
                VALUES (:b, CURRENT_DATE, :h, :s)
                ON CONFLICT (brand) DO UPDATE SET hits = :h, sample = :s
            """), {"b": c["brand"], "h": c["hits"], "s": c["sample"][:300]})
        session.commit()

        if fresh and notify:
            _notify_slack(fresh)
            session.execute(text(
                f"UPDATE {DB_SCHEMA}.device_watch_log SET notified_at = now() WHERE brand = ANY(:b)"
            ), {"b": [c["brand"] for c in fresh]})
            session.commit()

        logger.info("디바이스 진출 감시: 신호 %d개 브랜드 · 신규 %d", len(cands), len(fresh))
        return {"found": cands, "new": fresh}
    finally:
        session.close()


def _notify_slack(cands: list[dict]) -> None:
    try:
        from notifications.slack import _post, mention_prefix
    except Exception as e:
        logger.warning("슬랙 모듈 로드 실패: %s", e)
        return
    lines = [f"{mention_prefix()}*🔌 디바이스 진출 신호 {len(cands)}건* — 스킨케어로 보던 브랜드가 기기를 냈어요",
             "_맞으면 디바이스 경쟁사로 옮겨야 합니다. 아니면 그냥 두세요(다시 안 알립니다)._", ""]
    for c in sorted(cands, key=lambda x: -x["hits"])[:10]:
        lines.append(f"• *{c['brand']}* — 관련 기사 {c['hits']}건 ({c['date']})")
        lines.append(f"    ↳ {c['sample'][:90]}")
        if c.get("url"):
            lines.append(f"    {c['url']}")
    _post({"text": "\n".join(lines)}, secondary=False)


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    logging.basicConfig(level=logging.INFO)
    import json
    r = run(notify=False)
    print(json.dumps(r, ensure_ascii=False, indent=2, default=str))
