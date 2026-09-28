"""
파이프라인 AI 파수꾼(Watchdog) — 1단계: 수집 헬스 이상감지 + 실패 자동진단.

수집 완료 후 소스/리테일 이상(0건·급감·오래됨·오류율)과 잡 실패(traceback)를 감지하고,
AI(mini)로 '가장 가능성 높은 원인 + 확인/조치 방향'을 진단해 슬랙(개인/수집 채널)에 리포트.
자동 수정은 하지 않음(감지→진단→제안). 중복 알림은 high_alert_log 재사용해 쿨다운.
"""

import os
import logging
from datetime import datetime, timedelta

from sqlalchemy import text

from storage.models import get_session
from config.settings import DB_SCHEMA

logger = logging.getLogger(__name__)

_WD_MODEL = os.getenv("BRIEF_MODEL", "gpt-4o-mini")
_COOLDOWN_HOURS = 72          # 같은 이상은 3일에 한 번만 알림(나그 방지)

# 분류기가 실제 낼 수 있는 활동유형(claude_classifier enum이 진짜 기준 —
# config/brands.ACTIVITY_TYPES는 '가격_프로모션' 누락 등 불일치가 있어 여기 고정).
_KNOWN_ACTIVITY = {"신시장_진출", "유통_채널", "신제품_런칭", "인플루언서_협업",
                   "투자_BD", "브랜드_마케팅", "실적_공시", "가격_프로모션", "기타"}


# ── 중복 방지(high_alert_log 재사용) ─────────────────────────────────────────
def _dedup_ok(session, key: str) -> bool:
    """이 이상 키를 지금 알려도 되는지(쿨다운 내 기존 알림 없으면 True). fail-open."""
    try:
        row = session.execute(text(f"""
            SELECT 1 FROM {DB_SCHEMA}.high_alert_log
            WHERE brand = '__WATCHDOG__' AND sig = :k
              AND sent_at >= now() - (:h || ' hours')::interval
            LIMIT 1"""), {"k": key, "h": _COOLDOWN_HOURS}).fetchone()
        if row:
            return False
        session.execute(text(f"""
            INSERT INTO {DB_SCHEMA}.high_alert_log (brand, country, activity_type, sig, sent_at)
            VALUES ('__WATCHDOG__', '', 'watchdog', :k, now())"""), {"k": key})
        session.commit()
        return True
    except Exception as e:
        logger.debug("watchdog dedup 실패(알림 진행): %s", e)
        try:
            session.rollback()
        except Exception:
            pass
        return True


# ── 수집 헬스 이상감지 ───────────────────────────────────────────────────────
def check_collection_health(session, agg: dict | None = None) -> list[dict]:
    """소스별 급감·리테일 미갱신·오류율 이상을 찾아 findings 리스트로 반환."""
    findings: list[dict] = []

    # 1) 소스(collector_type)별 급감 판정.
    #
    #    기준일은 '오늘'이 아니라 **완결된 어제**다. 하루가 끝나기 전에 총량으로 재면
    #    수집이 아직 안 끝난 것을 급감으로 오판한다 — 실제 사고: 월요일 19:19에
    #    google_rss 2건으로 경보가 갔는데, 20시 주간 풀스캔이 돌자 최종 75건(중앙값의 4배)이었다.
    #    하루 늦게 알게 되지만 급감 대응은 어차피 같고, 매일 오는 오탐이 사라진다.
    #
    #    날짜는 KST로 끊는다. DB가 UTC라 그냥 ::date를 쓰면 하루 경계가 KST 09시가 되는데,
    #    바로 그 시각에 오전 수집이 돈다(실측 09:03~09:19). 몇 분만 당겨져도 전날로 밀린다.
    #    주간 풀스캔이 평균을 부풀리므로 평균 대신 '활동일수 + 중앙값'으로 판정한다.
    try:
        rows = session.execute(text(f"""
            SELECT collector_type,
                   (collected_at + interval '9 hours')::date AS dt,
                   count(*) AS c
            FROM {DB_SCHEMA}.news_articles
            WHERE collected_at >= now() - interval '16 days' AND collector_type IS NOT NULL
            GROUP BY 1, 2""")).fetchall()
        target = session.execute(text(
            "SELECT ((now() + interval '9 hours')::date - 1)")).scalar()   # 어제(KST)
        per: dict = {}
        for ct, dt, c in rows:
            if dt >= target + timedelta(days=1):
                continue                                   # 진행 중인 오늘은 판정에서 제외
            d = per.setdefault(ct, {"day": 0, "prior": []})
            if dt == target:
                d["day"] += int(c)
            else:
                d["prior"].append(int(c))
        # 여러 소스가 **같이** 줄면 우리 장애가 아니라 바깥 사정(연휴·주말·뉴스 비수기)이다.
        # 실제 오탐: 추석 연휴(9/24~) 기사량이 100건/일 → 6~12건/일로 떨어졌는데
        # naver_news 급감으로 3일 연속 경보가 갔다. 수집기·API는 정상이었다.
        _tot_t = sum(v["day"] for v in per.values())
        _tot_p = [sum(v["prior"][i] for v in per.values() if i < len(v["prior"]))
                  for i in range(max((len(v["prior"]) for v in per.values()), default=0))]
        _tot_med = sorted(_tot_p)[len(_tot_p) // 2] if _tot_p else 0
        _sitewide = bool(_tot_med) and _tot_t < _tot_med * 0.5
        if _sitewide:
            logger.info("전체 수집량이 중앙값의 %.0f%% — 소스별 급감 경보를 보류(바깥 사정 추정)",
                        _tot_t / _tot_med * 100)
            return findings

        for ct, d in per.items():
            prior = sorted(d["prior"])
            active_days = len(prior)                       # 그 이전 14일 중 수집된 날 수
            med = prior[len(prior) // 2] if prior else 0   # 중앙값(주간 스파이크 영향 적음)
            t = d["day"]
            # 저빈도 소스는 0건이 평소에도 흔하다 — reddit은 15일 중 11일만 들어오고
            # 중앙값이 2건이라 하루 0건이 정상 범위인데 경보가 갔다. 매일 꾸준히
            # 들어오던 소스(중앙값 3건 이상 + 14일 중 12일 이상)에만 0건 경보를 건다.
            if t == 0 and active_days >= 12 and med >= 3:
                # 거의 매일 오던 소스가 어제 0건 → 명백한 이상
                findings.append({
                    "type": "source_drop", "key": f"src:{ct}:{target}",
                    "title": f"수집 소스 '{ct}' 어제({target}) 0건 — 평소 거의 매일 수집",
                    "detail": (f"'{ct}'가 어제 0건. 그 전 14일 중 {active_days}일 수집(중앙값 {med}건/일)했는데 "
                               f"어제는 전무. 수집기를 단독 실행해 응답을 먼저 확인할 것."),
                })
            elif med >= 15 and active_days >= 10 and 0 < t < med * 0.2:
                # 고빈도 소스가 중앙값의 20% 미만으로 급감
                findings.append({
                    "type": "source_drop", "key": f"src:{ct}:{target}",
                    "title": f"수집 소스 '{ct}' 급감 — 어제({target}) {t}건 (중앙값 {med}건/일)",
                    "detail": (f"'{ct}'가 어제 {t}건으로 중앙값 {med}건의 {t/med*100:.0f}% 수준. "
                               f"수집기 단독 실행과 직전 잡 로그를 먼저 확인할 것."),
                })
    except Exception as e:
        logger.warning("소스 헬스 체크 실패: %s", e)

    # 2) 아마존 리테일 신선도 — 며칠째 미갱신(스크래핑 차단/구조변경 신호)
    try:
        _r = session.execute(text(
            f"SELECT MAX(capture_date), (now()::date - MAX(capture_date)) "
            f"FROM {DB_SCHEMA}.retail_rankings")).fetchone()
        cap = _r[0] if _r else None
        age = int(_r[1]) if _r and _r[1] is not None else None
        if cap and age is not None:
            if age >= 4:      # 주2회(월·목) 수집이라 4일 이상이면 이상
                findings.append({
                    "type": "retail_stale", "key": f"retail:{cap}",
                    "title": f"아마존 리테일 {age}일째 미갱신 (최신 {cap})",
                    "detail": (f"retail_rankings 최신 capture_date={cap} ({age}일 전). 아마존 베스트셀러 "
                               f"수집 잡 로그와 단독 실행 결과를 확인할 것."),
                })
    except Exception as e:
        logger.warning("리테일 신선도 체크 실패: %s", e)

    # 3) 이번 수집 오류율 — agg 전달 시
    if agg:
        attempts = (agg.get("brands", 0) * agg.get("countries", 0)) or 0
        errs = agg.get("errors", 0)
        if attempts and errs and errs / attempts > 0.4:
            samples = agg.get("error_samples") or []
            findings.append({
                "type": "error_rate", "key": "errrate",
                "title": f"수집 오류율 높음 — {errs}/{attempts}건 오류",
                "detail": (f"이번 수집에서 {attempts}건 중 {errs}건 오류(>40%). 예시: "
                           + " / ".join(samples[:3]) if samples else
                           f"이번 수집 {attempts}건 중 {errs}건 오류(>40%)."),
            })

    return findings


# ── AI 진단 ─────────────────────────────────────────────────────────────────
def _diagnose(findings: list[dict]) -> dict:
    """이상들에 대해 '가장 가능성 높은 원인 + 확인/조치 방향' 한 줄씩. {key: read}."""
    if not findings:
        return {}
    lines = [f"[{i}] {f['title']} — {f['detail']}" for i, f in enumerate(findings)]
    prompt = f"""너는 데이터 수집 파이프라인(뉴스·리테일 스크래핑, Python/APScheduler) 운영 엔지니어다.
아래 이상 항목마다 **무엇부터 확인해야 하는지**를 한 줄로 제시하라.

{chr(10).join(lines)}

규칙:
- 너에게 주어진 것은 **건수뿐이다.** 로그도, 응답 코드도, 소스 목록도 보지 못했다.
  그러니 원인을 단정하지 마라. "URL이 변경되었다", "차단되었다" 같은 서술은 금지다.
  실제로 이런 오진이 있었다 — 건수만 보고 "RSS 피드 URL 변경 추정"이라 했는데
  알고 보니 수집이 그 시각에 아직 안 끝났을 뿐 URL은 멀쩡했다.
- 대신 **사람이 바로 실행할 수 있는 확인 절차**를 쓴다.
  좋은 예: "해당 수집기 단독 실행해 응답 코드 확인"
           "직전 잡 로그에서 예외·타임아웃 유무 확인"
           "같은 날 다른 소스도 줄었는지 대조(네트워크 공통 문제 구분)"
  나쁜 예: "RSS URL 변경 추정", "봇 차단됨", "API 키 만료"
- 원인을 꼭 언급해야 하면 두 개 이상 병렬로 두고 구분법을 붙여라
  (예: "부분 실패인지 소스 변경인지 — 단독 실행 결과로 구분").
- 각 50자 내외, 한국어.
반드시 JSON만: {{"reads": [{{"i": 0, "read": "..."}}, ...]}} — 모든 인덱스 포함."""
    try:
        from openai import OpenAI
        import json as _json
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        resp = client.chat.completions.create(
            model=_WD_MODEL, max_tokens=500, temperature=0.3,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
        )
        data = _json.loads(resp.choices[0].message.content or "{}")
        out = {}
        for it in data.get("reads", []):
            i = it.get("i")
            rd = (it.get("read") or "").strip()
            if isinstance(i, int) and 0 <= i < len(findings) and rd:
                out[findings[i]["key"]] = rd
        return out
    except Exception as e:
        logger.warning("watchdog 진단 실패: %s", e)
        return {}


# ── 2단계: 데이터 드리프트 감시 ──────────────────────────────────────────────
def _suggest_country_ko(codes: list[str]) -> dict:
    """미매핑 국가코드 → {코드: 한국어명} 제안(1 LLM). 실패 시 코드=코드."""
    out = {c: c for c in codes}
    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        resp = client.chat.completions.create(
            model=_WD_MODEL, max_tokens=200, temperature=0,
            messages=[{"role": "user", "content":
                       f"다음 국가코드(ISO 또는 약칭)를 한국어 국가명으로. 'CODE=한국어' 콤마구분 한 줄만: {', '.join(codes)}"}])
        txt = (resp.choices[0].message.content or "").strip()
        for pair in txt.replace("\n", ",").split(","):
            if "=" in pair:
                k, v = pair.split("=", 1)
                k, v = k.strip().upper(), v.strip()
                if k in out and v:
                    out[k] = v
    except Exception:
        pass
    return out



# 잡별 '이 주기 안에는 한 번 돌았어야 한다' 상한(일). 크론 주기보다 넉넉히 잡아
# 하루이틀 밀린 것으로는 안 울리게 한다.
_JOB_MAX_GAP_DAYS = {
    "daily_tier1": 2, "weekly_full": 10, "weekly_momentum": 10,
    "brand_discovery": 3, "retail_ranking": 5, "oliveyoung_ranking": 3,
    "search_trends": 7, "google_trends": 5, "youtube_buzz": 3,
    "export_stats": 40, "dart_financials": 20, "trademark": 40,
    "score_snapshot": 10, "weekly_dedup": 10,
}


def check_job_runs(session) -> list[dict]:
    """예정대로 돌았어야 할 잡이 안 돈 것을 잡는다.

    지금까지는 실패(EVENT_JOB_ERROR)만 알렸고 '아예 실행되지 않은' 경우는 몰랐다.
    월간 잡은 한 번 걸러도 한 달을 모른다 — 실제로 상표 수집이 9/4에 안 돌았는데
    25일 뒤 수동 점검에서야 발견했다.

    job_runs 이력이 아직 없으면(기능 도입 직후) 조용히 넘어간다.
    """
    try:
        rows = session.execute(text("""
            SELECT job_id, MAX(ran_at)::date, (CURRENT_DATE - MAX(ran_at)::date)
            FROM rival_intel.job_runs WHERE ok GROUP BY job_id""")).fetchall()
    except Exception:
        return []                       # 테이블 없음 = 아직 기록 시작 전
    if not rows:
        return []
    seen = {r[0]: (r[1], int(r[2] or 0)) for r in rows}
    late = []
    for jid, maxgap in _JOB_MAX_GAP_DAYS.items():
        if jid not in seen:
            continue                    # 한 번도 안 돈 잡은 도입 직후라 판단 보류
        last, gap = seen[jid]
        if gap > maxgap:
            late.append((jid, last, gap, maxgap))
    if not late:
        return []
    late.sort(key=lambda x: -x[2])
    body = " / ".join(f"{j} 마지막 {d}({g}일 전, 기준 {m}일)" for j, d, g, m in late[:5])
    return [{
        "type": "job_missed", "key": "job:missed:" + ",".join(j for j, *_ in late),
        "title": f"예정대로 안 돈 잡 {len(late)}건",
        "detail": (f"실패가 아니라 **실행 자체가 없었다**. {body}. "
                   f"스케줄러가 그 시각에 꺼져 있었거나 크론이 어긋났을 수 있다."),
        "read": "스케줄러 프로세스 가동 이력과 해당 잡의 CronTrigger 설정을 확인할 것",
    }]

def check_data_drift(session) -> list[dict]:
    """미매핑 국가코드·새 활동유형·제품명 이상치를 감지 → 매핑/점검 제안."""
    findings: list[dict] = []
    # 1) 미매핑 국가코드(최근 7일 뉴스+리테일) — 화면에 코드로 노출될 위험
    try:
        from analytics.brief_strategy import _COUNTRY_KO
        from storage.repository import known_mapping_codes, propose_mapping
        known = set(_COUNTRY_KO) | known_mapping_codes(session, "country")
        ccs = set()
        for (c,) in session.execute(text(
            f"SELECT DISTINCT country FROM {DB_SCHEMA}.news_articles "
            f"WHERE published_date >= now() - interval '7 days' AND country IS NOT NULL")):
            ccs.add((c or "").upper())
        for (c,) in session.execute(text(
            f"SELECT DISTINCT country FROM {DB_SCHEMA}.retail_rankings "
            f"WHERE capture_date >= now()::date - 7 AND country IS NOT NULL")):
            ccs.add((c or "").upper())
        unmapped = sorted(c for c in ccs
                          if c and c != "NULL" and c.isalpha() and len(c) <= 3
                          and c not in known)
        if unmapped:
            sugg = _suggest_country_ko(unmapped)
            for c in unmapped:                       # 제안을 DB에 등록(pending)
                try:
                    propose_mapping(session, "country", c, sugg.get(c, ""))
                except Exception:
                    pass
            sugg_txt = ", ".join(f"{c}={sugg.get(c, c)}" for c in unmapped)
            findings.append({
                "type": "drift_country", "key": "drift:cc:" + ",".join(unmapped),
                "title": f"미매핑 국가코드 {len(unmapped)}개: {', '.join(unmapped)}",
                "detail": (f"화면에 한국어명 없이 코드로 노출될 수 있음(예전 AE='UAE' 케이스). "
                           f"제안: {sugg_txt}"),
                "read": "슬랙봇에게 `매핑` 확인 후 `매핑 승인`(전체) 또는 `매핑 " + unmapped[0]
                        + "=" + sugg.get(unmapped[0], "한국어") + "`(개별)로 반영",
            })
    except Exception as e:
        logger.warning("드리프트(국가) 체크 실패: %s", e)
    # 2) 새 활동유형 — 분류기 enum 밖 값(오분류/새 카테고리)
    try:
        known = _KNOWN_ACTIVITY
        ats = set()
        for (a,) in session.execute(text(
            f"SELECT DISTINCT activity_type FROM {DB_SCHEMA}.news_articles "
            f"WHERE published_date >= now() - interval '7 days' AND activity_type IS NOT NULL")):
            ats.add(a)
        newats = sorted(a for a in ats if a and a not in known)
        if newats:
            findings.append({
                "type": "drift_activity", "key": "drift:act:" + ",".join(newats),
                "title": f"새 활동유형 {len(newats)}개: {', '.join(newats)}",
                "detail": "분류기 정의(ACTIVITY_TYPES/enum) 밖 값. 오분류이거나 새 카테고리 등장.",
                "read": "classifier enum·config/brands.ACTIVITY_TYPES 정합성 점검",
            })
    except Exception as e:
        logger.warning("드리프트(활동유형) 체크 실패: %s", e)
    # 3) 제품명 이상치율 — 기사 제목/문장이 product_name에 섞임
    try:
        row = session.execute(text(f"""
            SELECT count(*) FILTER (WHERE product_name IS NOT NULL AND product_name <> ''),
                   count(*) FILTER (WHERE product_name IS NOT NULL AND product_name <> ''
                                    AND (char_length(product_name) > 40
                                         OR product_name ~ '[…?“”]'))
            FROM {DB_SCHEMA}.news_articles
            WHERE published_date >= now() - interval '7 days'""")).fetchone()
        tot, bad = int(row[0] or 0), int(row[1] or 0)
        if tot >= 20 and bad / tot > 0.35:
            findings.append({
                "type": "drift_product", "key": "drift:prod",
                "title": f"제품명 이상치 {bad}/{tot} ({bad/tot*100:.0f}%)",
                "detail": "product_name에 기사 제목·문장이 섞임(추출 규칙 이탈).",
                "read": "분류 프롬프트의 product_name 추출 규칙(제품명만) 점검",
            })
    except Exception as e:
        logger.warning("드리프트(제품명) 체크 실패: %s", e)
    return findings


# ── 2단계: 분류 품질 스팟체크 ────────────────────────────────────────────────
def check_classification_quality(session, sample: int = 10) -> list[dict]:
    """분류 품질 점검 — **코드로 확정할 수 있는 불일치만** 보고한다.

    예전엔 표본 10건을 LLM에 주고 "이상한 것을 지적하라"고 했는데, 판정 기준을 주지
    않아 취향 차이가 이견으로 나왔다("중요도를 medium으로 조정할 필요가 있음" 같은).
    게다가 '최대 5건'이 사실상 목표치가 돼 매번 5건을 채워 왔다.

    대신 규칙서에 명시된 두 가지만 본다. 둘 다 사람이 따질 여지가 없다.
      · importance ↔ strategic_score 불일치 (규칙: >=75 high, 55~74 medium, <=54 low)
      · 기사 어디에도 브랜드가 안 나오는데 incidental/unrelated가 아님
    """
    try:
        rows = session.execute(text(f"""
            SELECT id, brand, importance, COALESCE(strategic_score, 0),
                   title, COALESCE(title_ko, ''), COALESCE(details, ''),
                   COALESCE(brand_focus, '')
            FROM {DB_SCHEMA}.news_articles
            WHERE collected_at >= now() - interval '2 days'
              AND is_duplicate IS NOT TRUE AND is_self IS NOT TRUE
        """)).fetchall()
    except Exception as e:
        logger.warning("스팟체크 표본 조회 실패: %s", e)
        return []
    if not rows:
        return []

    try:
        from config.brands import BRAND_KO_NAMES
    except Exception:
        BRAND_KO_NAMES = {}

    def _expect(sc):
        return "high" if sc >= 75 else ("medium" if sc >= 55 else "low")

    def _mentioned(r):
        blob = " ".join([r[4] or "", r[5] or "", r[6] or ""]).lower()
        cands = [r[1], (r[1] or "").replace(" ", "")] + list(BRAND_KO_NAMES.get(r[1], []))
        return any(n and n.lower() in blob for n in cands)

    mism = [r for r in rows if r[3] and _expect(r[3]) != r[2]]
    ghost = [r for r in rows if r[7] not in ("incidental", "unrelated") and not _mentioned(r)]

    findings = []
    n = len(rows)
    if mism and len(mism) / n >= 0.10:      # 산발적 1~2건은 굳이 알리지 않는다
        ex = " / ".join(f"[{r[0]}] 점수 {r[3]}이면 {_expect(r[3])}인데 {r[2]}" for r in mism[:3])
        findings.append({
            "type": "classify_qa", "key": f"classify:score:{len(mism)}",
            "title": f"중요도가 점수 기준과 어긋남 — 최근 2일 {len(mism)}/{n}건",
            "detail": (f"분류 규칙은 strategic_score >=75 high, 55~74 medium, <=54 low인데 "
                       f"{len(mism)}건이 다르다. {ex}"),
            "read": "classifier/prompts.py의 점수↔중요도 정합 문구와 실제 출력 대조",
        })
    if ghost and len(ghost) / n >= 0.10:
        ex = " / ".join(f"[{r[0]}] {r[1]}" for r in ghost[:3])
        findings.append({
            "type": "classify_qa", "key": f"classify:ghost:{len(ghost)}",
            "title": f"브랜드가 기사에 없는데 무관 처리가 아님 — 최근 2일 {len(ghost)}/{n}건",
            "detail": (f"제목·한글제목·본문 어디에도 브랜드명이 없는데 brand_focus가 "
                       f"incidental/unrelated가 아니다. 지표와 브리핑에 그대로 섞인다. {ex}"),
            "read": "brand_focus 'unrelated' 판정이 실제로 쓰이는지 분류 출력 확인",
        })
    return findings


def _daily_deep_ok(session) -> bool:
    """드리프트·스팟체크는 하루 1회만(수집이 하루 2회 돌아도 중복 방지)."""
    return _dedup_ok(session, "wd:deep_daily")


# ── 실행 진입점 ──────────────────────────────────────────────────────────────
def run_watchdog(agg: dict | None = None) -> int:
    """수집 완료 후 호출 — 헬스 이상 감지→쿨다운 필터→AI 진단→슬랙. 반환: 알린 건수."""
    session = get_session()
    try:
        findings = check_collection_health(session, agg)
        # 2단계 심층 체크(드리프트·분류 스팟체크)는 하루 1회만
        if _daily_deep_ok(session):
            try:
                findings += check_data_drift(session)
            except Exception as e:
                logger.warning("드리프트 체크 실패: %s", e)
            try:
                findings += check_classification_quality(session)
            except Exception as e:
                logger.warning("스팟체크 실패: %s", e)
            try:
                findings += check_job_runs(session)
            except Exception as e:
                logger.warning("잡 미실행 체크 실패: %s", e)
        fresh = [f for f in findings if _dedup_ok(session, f["key"])]
        if not fresh:
            logger.info("watchdog: 새 이상 없음(전체 %d, 쿨다운 후 0)", len(findings))
            return 0
        # 사전 진단(read)이 없는 항목만 AI 진단
        need = [f for f in fresh if not f.get("read")]
        reads = _diagnose(need) if need else {}
        try:
            from notifications.slack import send_watchdog
            body_lines = []
            for f in fresh:
                rd = f.get("read") or reads.get(f["key"])
                body_lines.append(f"• *{f['title']}*\n{f['detail']}"
                                  + (f"\n🔧 _AI 진단:_ {rd}" if rd else ""))
            send_watchdog(f"🐕 파이프라인 파수꾼 — 이상 {len(fresh)}건 감지", "\n\n".join(body_lines))
        except Exception as e:
            logger.warning("watchdog 슬랙 전송 실패: %s", e)
        logger.info("watchdog: 이상 %d건 알림", len(fresh))
        return len(fresh)
    except Exception as e:
        logger.warning("watchdog 실행 실패: %s", e)
        return 0
    finally:
        session.close()


def diagnose_failure(job_id: str, exc: Exception, tb_str: str = "") -> None:
    """잡이 예기치 않게 크래시했을 때(APScheduler EVENT_JOB_ERROR) 원인·수정방향 AI 진단→슬랙."""
    tb_tail = (tb_str or "")[-1600:]
    prompt = f"""파이썬 데이터 수집 잡 '{job_id}'이 예기치 않게 실패했다. 아래 traceback을 보고
(1) 실패 원인 한 줄, (2) 수정 방향 한 줄을 한국어로 제시하라. 기술적·구체적으로.

에러: {type(exc).__name__}: {exc}

traceback(끝부분):
{tb_tail}"""
    summary = ""
    try:
        from openai import OpenAI
        client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        resp = client.chat.completions.create(
            model=_WD_MODEL, max_tokens=300, temperature=0.2,
            messages=[{"role": "user", "content": prompt}])
        summary = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        logger.warning("실패 진단 LLM 실패: %s", e)
        summary = f"{type(exc).__name__}: {exc}"
    # 같은 잡 실패 쿨다운
    session = get_session()
    try:
        if not _dedup_ok(session, f"fail:{job_id}"):
            return
    finally:
        session.close()
    try:
        from notifications.slack import send_watchdog
        send_watchdog(f"🐕 파이프라인 실패 — 잡 '{job_id}' 크래시",
                      f"*{type(exc).__name__}:* {exc}\n\n🔧 _AI 진단:_\n{summary}")
    except Exception as e:
        logger.warning("실패 진단 슬랙 전송 실패: %s", e)
