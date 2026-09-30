"""적중표 — 브리핑이 '지켜볼 것'으로 짚은 걸 나중에 채점한다.

브리핑은 매일 "이걸 지켜보라"고 말해왔다. 100건 중 23건에 그런 항목이 있고,
형식이 한결같다.

    • 센텔리안24 코스트코 입점 — 다음 달 관세청 수출액으로 확인.
    • 리쥬란 중국 CCM과 MOU — 다음 분기 중국 수출액으로 확인.

**확인 방법까지 문장에 적혀 있다.** 그런데 한 달 뒤 실제 수출액이 나와도
아무도 대조하지 않았다. 주장만 쌓이고 결과와 이어지지 않으니, 읽는 사람이
"저번에 짚은 건 어떻게 됐나"를 알 수가 없다.

여기서 그 고리를 잇는다. 짚은 것을 저장하고(extract), 기한이 지나면 우리
데이터로 채점한다(score). 채점 못 하는 지표(틱톡샵·SNS 등 수집 안 하는 것)는
'맞음'으로 얼버무리지 않고 **판단불가**로 남긴다 — 못 맞힌 걸 맞힌 척하면
적중표 자체가 못 믿을 것이 된다.
"""

import logging
import re
from datetime import date, timedelta

from sqlalchemy import text

from config.settings import DB_SCHEMA

logger = logging.getLogger(__name__)

# 확인 방법 문구 → 우리가 실제로 대조할 수 있는 지표.
# 여기 없는 것(틱톡샵 판매량·SNS 콘텐츠 증가 속도 등)은 수집 자체를 안 하므로
# 채점하지 않고 '판단불가'로 둔다.
_METRIC_RULES = [
    ("export",     ("수출액", "수출량", "관세청")),
    ("oliveyoung", ("올리브영", "올영")),
    ("retail",     ("아마존", "판매량", "랭킹", "순위")),
    ("search",     ("검색량", "검색 추이", "구글 트렌드")),
    ("news",       ("기사", "보도", "노출")),
]

_HORIZON = [
    (90, ("다음 분기", "차기 분기", "분기")),
    (30, ("다음 달", "다음달", "내달", "한 달")),
    (14, ("2주", "이주")),
    (7,  ("이번 주", "일주일", "주간")),
]


def _metric_of(method: str) -> str:
    t = method or ""
    for kind, keys in _METRIC_RULES:
        if any(k in t for k in keys):
            return kind
    return "other"


def _horizon_days(method: str) -> int:
    t = method or ""
    for days, keys in _HORIZON:
        if any(k in t for k in keys):
            return days
    return 30                      # 기간을 안 적었으면 한 달로 본다


def _brand_of(claim: str, ko2en: dict) -> "str | None":
    """문장 앞머리에서 브랜드를 찾는다. 긴 한글명부터 맞춰야 '리쥬란코스메틱'이
    '리쥬란'으로 잘리지 않는다."""
    t = claim or ""
    for ko in sorted(ko2en, key=len, reverse=True):
        if ko and ko in t:
            return ko2en[ko]
    return None


def _ensure_table(session) -> None:
    session.execute(text(f"""
        CREATE TABLE IF NOT EXISTS {DB_SCHEMA}.watch_items (
            id BIGSERIAL PRIMARY KEY,
            briefing_id BIGINT,
            said_on DATE NOT NULL,          -- 브리핑이 짚은 날
            brand VARCHAR(100),
            claim TEXT NOT NULL,            -- 무엇을 짚었나
            method TEXT,                    -- 무엇으로 확인한다고 했나
            metric VARCHAR(16),             -- export | oliveyoung | retail | search | news | other
            due_on DATE NOT NULL,           -- 언제쯤 결과가 나오나
            status VARCHAR(12) DEFAULT 'pending',  -- pending | hit | miss | unknown
            result_note TEXT,               -- 채점 근거(숫자 포함)
            progress_note TEXT,             -- 기한 전 잠정 경과(매일 갱신)
            scored_at TIMESTAMP WITH TIME ZONE,
            UNIQUE(said_on, claim)
        )
    """))
    session.execute(text(
        f"ALTER TABLE {DB_SCHEMA}.watch_items "
        f"ADD COLUMN IF NOT EXISTS progress_note TEXT"))
    session.execute(text(
        f"CREATE INDEX IF NOT EXISTS ix_watch_due "
        f"ON {DB_SCHEMA}.watch_items (status, due_on DESC)"))


def extract(session, limit: int = 0) -> dict:
    """브리핑 본문에서 '지켜볼 것' 항목을 뽑아 저장. 이미 있는 건 건너뛴다."""
    from config.brands import BRAND_KO_NAMES
    ko2en = {}
    for en, kos in BRAND_KO_NAMES.items():
        for ko in (kos or []):
            ko2en[ko] = en
    # 브리핑은 '파마리서치 중국 MOU'처럼 **회사명**으로 쓰기도 한다. 한글 브랜드명만
    # 보면 그런 항목은 브랜드 미상으로 남아 채점이 안 된다.
    try:
        from signals.dart_financials import BRAND_CORP
        for en, spec in BRAND_CORP.items():
            for nm in (spec.get("names") or []):
                ko2en.setdefault(nm, en)
    except Exception as e:
        logger.info("회사명 매핑 생략: %s", str(e)[:60])

    _ensure_table(session)
    session.commit()

    sql = (f"SELECT id, generated_at::date, content FROM {DB_SCHEMA}.briefings "
           f"WHERE content LIKE '%지켜볼%' ORDER BY generated_at DESC")
    if limit:
        sql += f" LIMIT {int(limit)}"
    rows = session.execute(text(sql)).fetchall()

    found = saved = 0
    for bid, said_on, content in rows:
        txt = re.sub(r"<[^>]+>", "", content or "")
        i = txt.find("지켜볼")
        if i < 0:
            continue
        # '지켜볼 것' 다음 블록의 불릿만. 다음 소제목(*로 끝나는 줄)에서 멈춘다.
        block = []
        for line in txt[i:].split("\n")[1:]:
            ls = line.strip()
            if not ls:
                continue
            if ls.endswith("*") and not ls.startswith(("•", "-", "·")):
                break
            if ls.startswith(("•", "-", "·")):
                block.append(ls.lstrip("•-· ").strip())
            elif block:
                break
        for item in block:
            # '{주장} — {확인 방법}' — em/en dash 모두 받는다
            parts = re.split(r"\s[—–-]\s", item, maxsplit=1)
            claim = parts[0].strip().rstrip(".")
            method = parts[1].strip().rstrip(".") if len(parts) > 1 else ""
            if len(claim) < 4:
                continue
            found += 1
            due = said_on + timedelta(days=_horizon_days(method))
            try:
                r = session.execute(text(f"""
                    INSERT INTO {DB_SCHEMA}.watch_items
                        (briefing_id, said_on, brand, claim, method, metric, due_on)
                    VALUES (:b, :s, :br, :c, :m, :k, :d)
                    ON CONFLICT (said_on, claim) DO NOTHING
                    RETURNING id
                """), {"b": bid, "s": said_on, "br": _brand_of(claim, ko2en),
                       "c": claim, "m": method, "k": _metric_of(method), "d": due})
                if r.fetchone():
                    saved += 1
            except Exception as e:
                session.rollback()
                logger.warning("지켜볼것 저장 실패 [%s]: %s", claim[:30], str(e)[:80])
    session.commit()
    logger.info("지켜볼 것 추출 — 문장 %d개 중 신규 %d건 저장", found, saved)
    return {"found": found, "saved": saved}


def _score_export(session, it) -> tuple:
    """수출액 — 짚은 달 대비 기한 이후 달의 수출액. 국가를 못 집으면 전체."""
    country = None
    for cc, names in (("CN", ("중국",)), ("US", ("미국",)), ("JP", ("일본",)),
                      ("MX", ("멕시코",)), ("GB", ("영국", "런던")),
                      ("FR", ("프랑스",)), ("VN", ("베트남",))):
        if any(n in (it.claim + " " + (it.method or "")) for n in names):
            country = cc
            break
    where = "WHERE period IS NOT NULL"
    params = {"a": it.said_on, "b": it.due_on}
    if country:
        where += " AND country_code = :cc"
        params["cc"] = country
    try:
        # period는 문자열이 아니라 date다(2026-07-01 = 7월분). 날짜로 비교한다.
        cut = "date_trunc('month', CAST(:a AS date))"
        before = session.execute(text(
            f"SELECT sum(exp_usd) FROM {DB_SCHEMA}.export_stats {where} "
            f"AND period < {cut}"), params).scalar()
        after = session.execute(text(
            f"SELECT sum(exp_usd) FROM {DB_SCHEMA}.export_stats {where} "
            f"AND period >= {cut}"), params).scalar()
    except Exception as e:
        session.rollback()
        return "unknown", f"수출 데이터 조회 실패: {str(e)[:60]}"
    if not before:
        return "unknown", "짚기 전 수출 데이터가 없어 비교 기준이 없다"
    if not after:
        last = session.execute(text(
            f"SELECT max(period) FROM {DB_SCHEMA}.export_stats")).scalar()
        return "unknown", (f"관세청 확정분이 {last:%Y-%m}까지라 아직 그 달 수치가 안 나왔다"
                           if last else "수출 데이터가 없다")
    ch = (after / before - 1) * 100
    tag = country or "전체"
    if ch > 0:
        return "hit", f"{tag} 수출액 {ch:+.0f}% — 짚은 방향대로 늘었다"
    return "miss", f"{tag} 수출액 {ch:+.0f}% — 늘지 않았다"


def _score_oliveyoung(session, it) -> tuple:
    """올리브영 — 짚은 시점 이후 최고 순위가 이전보다 올라갔나."""
    if not it.brand:
        return "unknown", "브랜드를 특정하지 못해 순위를 대조할 수 없다"
    try:
        before = session.execute(text(
            f"SELECT min(rank_position) FROM {DB_SCHEMA}.oliveyoung_rankings "
            f"WHERE brand = :b AND capture_date < :a"),
            {"b": it.brand, "a": it.said_on}).scalar()
        after = session.execute(text(
            f"SELECT min(rank_position) FROM {DB_SCHEMA}.oliveyoung_rankings "
            f"WHERE brand = :b AND capture_date >= :a"),
            {"b": it.brand, "a": it.said_on}).scalar()
    except Exception as e:
        session.rollback()
        return "unknown", f"올영 순위 조회 실패: {str(e)[:60]}"
    if after is None:
        return "unknown", "짚은 뒤 올영 랭킹에 잡힌 적이 없다"
    if before is None:
        return "hit", f"올영 랭킹 신규 진입 — 최고 {after}위"
    if after < before:
        return "hit", f"올영 최고 순위 {before}위 → {after}위로 상승"
    return "miss", f"올영 최고 순위 {before}위 → {after}위, 오르지 않았다"


def _score_retail(session, it) -> tuple:
    """아마존 등 해외 리테일 — 같은 방식으로 최고 순위 비교."""
    if not it.brand:
        return "unknown", "브랜드를 특정하지 못해 순위를 대조할 수 없다"
    try:
        before = session.execute(text(
            f"SELECT min(rank) FROM {DB_SCHEMA}.retail_rankings "
            f"WHERE brand = :b AND capture_date < :a"),
            {"b": it.brand, "a": it.said_on}).scalar()
        after = session.execute(text(
            f"SELECT min(rank) FROM {DB_SCHEMA}.retail_rankings "
            f"WHERE brand = :b AND capture_date >= :a"),
            {"b": it.brand, "a": it.said_on}).scalar()
    except Exception as e:
        session.rollback()
        return "unknown", f"리테일 순위 조회 실패: {str(e)[:60]}"
    if after is None:
        return "unknown", "짚은 뒤 해외 랭킹에 잡힌 적이 없다"
    if before is None:
        return "hit", f"해외 랭킹 신규 진입 — 최고 {after}위"
    if after < before:
        return "hit", f"해외 최고 순위 {before}위 → {after}위로 상승"
    return "miss", f"해외 최고 순위 {before}위 → {after}위, 오르지 않았다"


def _score_news(session, it) -> tuple:
    """기사 노출 — 짚은 뒤 같은 길이 기간의 기사 수가 이전보다 늘었나."""
    if not it.brand:
        return "unknown", "브랜드를 특정하지 못해 기사량을 대조할 수 없다"
    span = max((it.due_on - it.said_on).days, 7)
    try:
        before = session.execute(text(
            f"SELECT count(*) FROM {DB_SCHEMA}.news_articles WHERE brand = :b "
            f"AND is_duplicate IS NOT TRUE AND published_date >= :a - make_interval(days => :n) "
            f"AND published_date < :a"),
            {"b": it.brand, "a": it.said_on, "n": span}).scalar() or 0
        after = session.execute(text(
            f"SELECT count(*) FROM {DB_SCHEMA}.news_articles WHERE brand = :b "
            f"AND is_duplicate IS NOT TRUE AND published_date >= :a "
            f"AND published_date < :a + make_interval(days => :n)"),
            {"b": it.brand, "a": it.said_on, "n": span}).scalar() or 0
    except Exception as e:
        session.rollback()
        return "unknown", f"기사량 조회 실패: {str(e)[:60]}"
    if before == 0 and after == 0:
        return "unknown", "양쪽 기간 모두 기사가 없어 판단 못 함"
    if after > before:
        return "hit", f"기사 {before}건 → {after}건으로 늘었다"
    return "miss", f"기사 {before}건 → {after}건, 늘지 않았다"


_SCORERS = {
    "export": _score_export,
    "oliveyoung": _score_oliveyoung,
    "retail": _score_retail,
    "news": _score_news,
}


def score(session) -> dict:
    """기한이 지난 건 확정 채점하고, 아직인 건 '지금까지 이렇다'를 매일 갱신한다.

    기한(한 달)까지 기다리면 표가 한 달간 비어 있다. 그동안에도 지표는 움직이고
    있으니 잠정 경과를 보여준다 — 확정처럼 보이지 않게 status는 pending 그대로 둔다.
    """
    _ensure_table(session)
    session.commit()

    # 확인 방법을 안 적은 옛 항목은 기다릴 이유가 없다. 바로 '채점불가'로 빼서
    # 적중률 분모를 더럽히지 않는다(프롬프트에 검증 방법 요구를 넣기 전 브리핑들).
    n_unv = session.execute(text(f"""
        UPDATE {DB_SCHEMA}.watch_items
        SET status = 'unverifiable', scored_at = NOW(),
            result_note = '확인 방법을 적지 않아 채점할 수 없다(검증 방법 요구 이전 브리핑)'
        WHERE status = 'pending' AND COALESCE(method, '') = ''
    """)).rowcount
    session.commit()

    items = session.execute(text(f"""
        SELECT id, said_on, due_on, brand, claim, method, metric
        FROM {DB_SCHEMA}.watch_items
        WHERE status = 'pending' ORDER BY due_on
    """)).fetchall()
    done = {"hit": 0, "miss": 0, "unknown": 0, "progress": 0,
            "unverifiable": n_unv}
    today = date.today()
    for it in items:
        fn = _SCORERS.get(it.metric)
        if not fn:
            st, note = "unknown", f"'{(it.method or '')[:28]}'은 우리가 수집하지 않는 지표다"
        else:
            st, note = fn(session, it)
        final = it.due_on <= today
        try:
            if final:
                session.execute(text(
                    f"UPDATE {DB_SCHEMA}.watch_items SET status = :s, result_note = :n, "
                    f"scored_at = NOW() WHERE id = :i"), {"s": st, "n": note, "i": it.id})
                done[st] = done.get(st, 0) + 1
            else:
                # 잠정 — 아직 결론이 아니므로 status는 그대로
                session.execute(text(
                    f"UPDATE {DB_SCHEMA}.watch_items SET progress_note = :n WHERE id = :i"),
                    {"n": note, "i": it.id})
                done["progress"] += 1
            # 건별로 커밋한다. 한 건이 실패해 rollback하면 앞서 저장한 것까지
            # 되돌아가서, 실제로 14건 중 1건만 남았던 적이 있다.
            session.commit()
        except Exception as e:
            session.rollback()
            logger.warning("채점 저장 실패 id=%s: %s", it.id, str(e)[:80])
    session.commit()
    logger.info("적중표 — 확정 맞음 %d · 틀림 %d · 판단불가 %d · 잠정갱신 %d · 채점불가 %d",
                done["hit"], done["miss"], done["unknown"], done["progress"], n_unv)
    return done


def get_scoreboard(session, limit: int = 12) -> dict:
    """화면용 — {rows:[...], stat:{hit,miss,unknown,pending,rate}}."""
    try:
        # 브리핑이 하루에 여러 번 돌면 같은 사건이 조금씩 다른 문장으로 쌓인다
        # ('리쥬란 중국 CCM과 MOU' / '리쥬란 중국 CCM MOU 체결' / '파마리서치 중국 MOU').
        # 문장 앞머리로 묶으려니 표현 차이를 못 넘어서, **브랜드 + 지표 + 주차**로 묶고
        # 그 주에 가장 먼저 짚은 것만 남긴다. 같은 주에 같은 지표로 같은 브랜드를
        # 두 번 짚으면 사실상 같은 사건이다.
        rows = session.execute(text(f"""
            WITH dedup AS (
              SELECT DISTINCT ON (COALESCE(brand, claim), metric,
                                  date_trunc('week', said_on))
                     said_on, due_on, brand, claim, method, metric, status,
                     result_note, progress_note
              FROM {DB_SCHEMA}.watch_items
              WHERE status <> 'unverifiable'
              ORDER BY COALESCE(brand, claim), metric,
                       date_trunc('week', said_on), said_on ASC
            )
            SELECT * FROM dedup
            -- 결과가 난 것부터(최근 순), 그다음 결론이 임박한 것부터.
            -- 기한이 먼 D-88을 위에 두면 제일 안 궁금한 게 먼저 보인다.
            ORDER BY (status = 'pending') ASC,
                     CASE WHEN status = 'pending' THEN due_on END ASC,
                     said_on DESC
            LIMIT :n
        """), {"n": limit}).fetchall()
        st = dict(session.execute(text(
            f"SELECT status, count(*) FROM {DB_SCHEMA}.watch_items GROUP BY 1")).fetchall())
    except Exception as e:
        logger.warning("적중표 조회 실패: %s", e)
        return {"rows": [], "stat": {}}
    hit, miss = st.get("hit", 0), st.get("miss", 0)
    return {
        "rows": [{"said": str(r[0]), "due": str(r[1]), "brand": r[2] or "",
                  "claim": r[3], "method": r[4] or "", "metric": r[5],
                  "status": r[6], "note": r[7] or "", "progress": r[8] or "",
                  "left": (r[1] - date.today()).days} for r in rows],
        "stat": {"hit": hit, "miss": miss, "unknown": st.get("unknown", 0),
                 "pending": st.get("pending", 0),
                 "rate": round(hit / (hit + miss) * 100) if (hit + miss) else None},
    }


if __name__ == "__main__":
    import sys

    from dotenv import load_dotenv

    from storage.models import get_session

    sys.stdout.reconfigure(encoding="utf-8")
    load_dotenv()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    se = get_session()
    try:
        print(extract(se))
        print(score(se))
        sb = get_scoreboard(se)
        print("\n적중률:", sb["stat"])
        for r in sb["rows"]:
            print(f"  [{r['status']:7}] {r['said']} {r['brand']:14} {r['claim'][:40]}")
            print(f"            → {r['note'] or ('D' + str(r['left']) if r['left'] else '')}")
    finally:
        se.close()
