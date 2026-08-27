"""
APScheduler 스케줄

- Tier1 브랜드 × Tier1 국가: 매일 18:00 KST (업무시간 이후 — 피크 16시 이후 수집)
- 전체 브랜드 × 전체 국가: 매주 월요일 20:00 KST (주간 풀스캔)
- 주간 모멘텀 계산: 매주 월요일 19:00 KST
- 주간 중복 정리: 매주 일요일 19:00 KST
(Render는 유료플랜 상시가동 — keep-alive 핑 불필요, 제거됨)
"""

import logging
import os
import urllib.request
from datetime import datetime

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from config.brands import TIER1_BRANDS, ALL_BRANDS, TIER1_COUNTRIES, COUNTRIES
from config.settings import TITLE_SIMILARITY_THRESHOLD
from scheduler.pipeline import run_pipeline, reset_jangup_cache
from scheduler.briefing import generate_weekly_briefing, generate_daily_briefing
from storage.models import get_session
from storage.repository import save_dedup_candidate, get_recent_titles
from deduplication.url_hasher import title_similarity

logger = logging.getLogger(__name__)


def _get_tier_brands(tier: int) -> list[str]:
    """monitored_brands DB에서 활성 브랜드 목록 조회. DB 실패 시 하드코딩 fallback."""
    try:
        from sqlalchemy import text
        session = get_session()
        rows = session.execute(text(
            "SELECT name FROM rival_intel.monitored_brands "
            "WHERE tier = :tier AND is_active = TRUE ORDER BY name"
        ), {"tier": tier}).fetchall()
        session.close()
        brands = [r[0] for r in rows]
        if brands:
            return brands
    except Exception as e:
        logger.warning("DB 브랜드 목록 조회 실패, fallback 사용: %s", e)
    return TIER1_BRANDS if tier == 1 else ALL_BRANDS


def _run_collection(label: str, brands: list[str], countries: list[str],
                    deep_query: bool = False) -> None:
    """브랜드×국가 수집 루프 + 완료 후 Slack 요약 리포트.

    deep_query: 구글뉴스 보조(활동) 쿼리 사용 여부. 주간 풀스캔만 True(비용 절감).
    """
    import time
    from collections import defaultdict
    from notifications.slack import notify_collection_summary
    from classifier.claude_classifier import reset_token_usage, get_token_usage
    import collectors.google_rss as _gr

    _gr.DEEP_QUERY = deep_query
    reset_token_usage()
    t0 = time.time()
    agg = {"found": 0, "saved": 0, "classified": 0, "high": 0, "errors": 0}
    saved_by_brand: dict = defaultdict(int)

    for brand in brands:
        for country in countries:
            try:
                st = run_pipeline(brand, country)
                agg["found"]      += st.found
                agg["saved"]      += st.saved
                agg["classified"] += st.classified
                agg["high"]       += st.high
                agg["errors"]     += st.errors
                if st.saved:
                    saved_by_brand[brand] += st.saved
            except Exception as e:
                agg["errors"] += 1
                logger.error("오류 [%s/%s]: %s", brand, country, e)

    usage = get_token_usage()
    agg["brands"]    = len(brands)
    agg["countries"] = len(countries)
    agg["duration"]  = time.time() - t0
    agg["top_saved"] = sorted(saved_by_brand.items(), key=lambda x: -x[1])
    agg["tokens_in"]  = usage["in"]
    agg["tokens_out"] = usage["out"]
    agg["api_calls"]  = usage["calls"]
    agg["cost_usd"]   = usage["cost_usd"]

    logger.info("=== [%s] 수집 완료 — 신규 %d건(HIGH %d) / 오류 %d / OpenAI %d콜 $%.3f ===",
                label, agg["saved"], agg["high"], agg["errors"],
                usage["calls"], usage["cost_usd"])
    try:
        notify_collection_summary(label, agg)
    except Exception as e:
        logger.warning("수집 요약 Slack 전송 실패: %s", e)
    ping_dashboard_refresh()   # 수집 직후 Render 대시보드 재생성 트리거


def ping_dashboard_refresh() -> None:
    """수집/스냅샷 후 Render 대시보드 재생성 트리거(POST /api/refresh).

    대시보드 HTML은 Render 프로세스 메모리에 캐시라 재배포 전엔 안 바뀜.
    로컬 수집이 최신 데이터를 DB에 넣은 뒤 이 핑으로 화면을 최신화. 실패해도 무해.
    """
    import os
    import requests
    url = os.getenv("RENDER_EXTERNAL_URL") or "https://cmslab-rival-monitor.onrender.com"
    try:
        requests.post(url.rstrip("/") + "/api/refresh", timeout=10)
        logger.info("대시보드 refresh 핑 전송: %s", url)
    except Exception as e:
        logger.warning("대시보드 refresh 핑 실패(무시): %s", e)


def job_daily_tier1() -> None:
    """Tier1 브랜드 × Tier1 국가 — 매일 수집 (구글RSS + 전문미디어 + 장업신문 + PRTIMES)."""
    reset_jangup_cache()
    tier1 = _get_tier_brands(1)
    logger.info("=== [일별] Tier1 수집 시작 (브랜드 %d개 x 국가 %d개) ===",
                len(tier1), len(TIER1_COUNTRIES))
    _run_collection("일별 Tier1", tier1, TIER1_COUNTRIES, deep_query=False)


def job_weekly_full() -> None:
    """전체 브랜드 × 전체 국가 — 주간 풀스캔."""
    all_countries = list(COUNTRIES.keys())
    try:
        from sqlalchemy import text
        session = get_session()
        rows = session.execute(text(
            "SELECT name FROM rival_intel.monitored_brands WHERE is_active = TRUE ORDER BY tier, name"
        )).fetchall()
        session.close()
        all_active = [r[0] for r in rows] or ALL_BRANDS
    except Exception:
        all_active = ALL_BRANDS
    logger.info("=== [주간] 전체 수집 시작 (브랜드 %d개 x 국가 %d개) ===",
                len(all_active), len(all_countries))
    _run_collection("주간 풀스캔", all_active, all_countries, deep_query=True)


TIER_CHANGE_COOLDOWN_DAYS = 14   # 최근 변경 후 이 기간 내 재변경 금지 (플립플롭 방지)


def job_weekly_momentum() -> None:
    """브랜드 모멘텀 계산 → momentum_score 갱신 + tier 자동 승급/강등."""
    logger.info("=== [주간] 모멘텀 계산 시작 ===")
    from analytics.queries import (
        compute_brand_momentum, upsert_brand_momentum,
        update_brand_tier, days_since_tier_change,
    )
    session = get_session()
    promoted, demoted = [], []
    try:
        scores = compute_brand_momentum(session)
        for s in scores:
            upsert_brand_momentum(session, s["brand"], s["momentum"])

            # 자동 티어링: 승급 T2→1(rising & 최근4주≥5), 강등 T1→2(cooling & 최근4주≤2)
            want_promote = s["signal"] == "rising"  and s["tier"] == 2 and s["recent_4w"] >= 5
            want_demote  = s["signal"] == "cooling" and s["tier"] == 1 and s["recent_4w"] <= 2
            if not (want_promote or want_demote):
                continue

            # 히스테리시스: 최근 변경 후 쿨다운 기간 내면 스킵
            since = days_since_tier_change(session, s["brand"])
            if since is not None and since < TIER_CHANGE_COOLDOWN_DAYS:
                logger.info("… 티어 변경 보류(쿨다운 %.0f일): %s", since, s["brand"])
                continue

            new_tier = 1 if want_promote else 2
            update_brand_tier(session, s["brand"], new_tier)
            if want_promote:
                promoted.append(s["brand"])
                logger.info("⬆  승급 T2→1: %-20s  momentum=%.2fx  (최근4주=%d건)",
                            s["brand"], s["momentum"], s["recent_4w"])
            else:
                demoted.append(s["brand"])
                logger.info("⬇  강등 T1→2: %-20s  momentum=%.2fx  (최근4주=%d건)",
                            s["brand"], s["momentum"], s["recent_4w"])

        logger.info("모멘텀 갱신 완료 (%d개 브랜드, 승급 %d / 강등 %d)",
                    len(scores), len(promoted), len(demoted))
        if promoted or demoted:
            _notify_tier_changes(promoted, demoted)
    except Exception as e:
        logger.error("모멘텀 계산 오류: %s", e)
    finally:
        session.close()
    logger.info("=== [주간] 모멘텀 계산 완료 ===")


def _notify_tier_changes(promoted: list[str], demoted: list[str]) -> None:
    """티어 변경 시 Slack 알림 (webhook 없으면 스킵)."""
    url = os.getenv("SLACK_WEBHOOK_URL", "")
    if not url:
        return
    lines = []
    if promoted:
        lines.append("⬆ *승급 (Tier2→1)*: " + ", ".join(promoted))
    if demoted:
        lines.append("⬇ *강등 (Tier1→2)*: " + ", ".join(demoted))
    try:
        import json
        data = json.dumps({"text": "*브랜드 티어 자동 조정*\n" + "\n".join(lines)}).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        logger.warning("티어 변경 Slack 알림 실패: %s", e)


def job_search_trends() -> None:
    """네이버 데이터랩 검색 트렌드 수집 (수요 신호). 주 2회 — 뉴스(공급)와 대조용."""
    logger.info("=== [주2회] 네이버 검색 트렌드 수집 시작 ===")
    try:
        from signals.naver_trends import run as run_search_trends
        r = run_search_trends(days=120)
        logger.info("검색 트렌드 수집 완료: 그룹 %d / 행 %d", r["groups"], r["rows"])
    except Exception as e:
        logger.warning("검색 트렌드 수집 스킵(자격증명/네트워크 확인): %s", e)


def job_export_stats() -> None:
    """관세청 화장품 수출통계 수집 (성과 신호). 월1회 — 데이터 월1회 갱신."""
    logger.info("=== [월간] 관세청 수출통계 수집 시작 ===")
    try:
        from signals.export_stats import run as run_export_stats
        r = run_export_stats()
        logger.info("수출통계 수집 완료: 행 %d", r["rows"])
    except Exception as e:
        logger.warning("수출통계 수집 스킵(DATA_GO_KR_KEY/네트워크 확인): %s", e)


def job_dart_financials() -> None:
    """DART 경쟁사 재무 수집 (성과 신호). 월1회 — 연간 실적이라 자주 안 바뀜."""
    logger.info("=== [월간] DART 경쟁사 재무 수집 시작 ===")
    try:
        from signals.dart_financials import run as run_dart
        r = run_dart()
        logger.info("DART 재무 수집 완료: 매칭 %d / 저장 %d행", r["resolved"], r["saved"])
    except Exception as e:
        logger.warning("DART 재무 수집 스킵(OPENDART_KEY/네트워크 확인): %s", e)


def job_google_trends() -> None:
    """구글 트렌드(글로벌 수요) 수집 + 강한 검색 급등 Slack 알림. 주3회."""
    logger.info("=== [주3회] 구글 트렌드 수집 시작 ===")
    try:
        from signals.google_trends import run as run_gt
        r = run_gt()
        logger.info("구글 트렌드 수집 완료: 행 %d (실패배치 %d)", r["rows"], r["failed"])
    except Exception as e:
        logger.warning("구글 트렌드 수집 스킵(pytrends/네트워크 확인): %s", e)
        return
    # 강한 급등만 알림(오탐 억제): 급등 2배↑ & 최근지수 30↑
    try:
        from analytics.queries import get_google_spikes
        session = get_session()
        try:
            spikes = [x for x in get_google_spikes(session, spike_ratio=2.0, floor=30.0)]
        finally:
            session.close()
        if spikes:
            _notify_search_spikes(spikes[:6])
    except Exception as e:
        logger.warning("검색 급등 알림 스킵: %s", e)
    logger.info("=== [주3회] 구글 트렌드 완료 ===")


def _notify_search_spikes(spikes: list) -> None:
    """글로벌 검색 급등 Slack 알림(webhook 없으면 스킵)."""
    url = os.getenv("SLACK_WEBHOOK_URL_2") or os.getenv("SLACK_WEBHOOK_URL", "")
    if not url:
        return
    lines = [f"• *{s['brand']}* ({s['geo']}) 검색 {s['spike_ratio']}배↑ "
             f"(최근 {s['recent']} ← {s['baseline']})" for s in spikes]
    text_msg = "🔺 *글로벌 검색 급등 감지* (구글 트렌드, 최근7일 vs 직전28일)\n" + "\n".join(lines)
    try:
        import json
        data = json.dumps({"text": text_msg}).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        logger.warning("검색 급등 Slack 전송 실패: %s", e)


def _notify_new_trademarks(filings: list) -> None:
    """신규 해외 상표 출원 = 진출 임박 조기경보 Slack 알림(webhook 없으면 스킵)."""
    url = os.getenv("SLACK_WEBHOOK_URL_2") or os.getenv("SLACK_WEBHOOK_URL", "")
    if not url:
        return
    _FL = {"US": "🇺🇸", "JP": "🇯🇵", "EU": "🇪🇺"}
    lines = [f"• {f['date']} *{f['brand']}* {_FL.get(f['country'], f['country'])} — {f['mark']}"
             for f in filings]
    text_msg = ("🪧 *진출 임박 조기경보 — 신규 해외 상표 출원*\n"
                "_경쟁사가 해당 시장에 상표를 냈습니다. 뉴스보다 앞선 진출·신제품 신호._\n"
                + "\n".join(lines))
    try:
        import json
        data = json.dumps({"text": text_msg}).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        logger.warning("신규 상표 Slack 전송 실패: %s", e)


def job_trademark() -> None:
    """KIPRIS 해외상표 수집 (진출 선행신호) + 신규 출원 조기경보. 월1회."""
    logger.info("=== [월간] KIPRIS 해외상표 수집 시작 ===")
    try:
        from signals.trademark import run as run_tm
        r = run_tm()
        new = r.get("new_filings", [])
        logger.info("해외상표 수집 완료: 저장 %d(자기출원 %d · 신규 %d)",
                    r["saved"], r.get("own", 0), len(new))
        if new:
            _notify_new_trademarks(new[:8])
    except Exception as e:
        logger.warning("해외상표 수집 스킵(KIPRIS_KEY/네트워크 확인): %s", e)


def job_score_snapshot() -> None:
    """브랜드 종합스코어·모멘텀·리테일 순위 주간 스냅샷 (시계열 추세용). 주1회."""
    logger.info("=== [주간] 브랜드 스코어 스냅샷 시작 ===")
    try:
        from storage.models import get_session
        from analytics.history import snapshot_now
        s = get_session()
        try:
            n = snapshot_now(s)
            logger.info("스코어 스냅샷 완료: %d개 브랜드", n)
        finally:
            s.close()
    except Exception as e:
        logger.warning("스코어 스냅샷 스킵: %s", e)


def job_retail_ranking() -> None:
    """아마존 베스트셀러 리테일 랭킹 수집 (실판매 성과 = '잘 나간다' 신호). 주2회."""
    logger.info("=== [주2회] 아마존 리테일 랭킹 수집 시작 ===")
    try:
        from signals.retail_ranking import run as run_retail
        r = run_retail()
        logger.info("리테일 랭킹 수집 완료: 저장 %d(모니터링 %d) · 브랜드 %d",
                    r.get("captured", 0), r.get("monitored", 0), len(r.get("by_brand", {})))
        ping_dashboard_refresh()
    except Exception as e:
        logger.warning("리테일 랭킹 수집 스킵(네트워크/차단 확인): %s", e)


def job_oliveyoung_ranking() -> None:
    """올리브영 국내 랭킹 수집(국내 최대 H&B 채널·시계열). 원격 채널 MCP 프록시. 매일."""
    logger.info("=== [매일] 올리브영 국내 랭킹 수집 시작 ===")
    try:
        from signals.oliveyoung_channel import run as run_oy
        r = run_oy()
        logger.info("올영 랭킹 수집 완료: 저장 %d · 모니터링 %d · 자사 %d · 브랜드 %d",
                    r.get("captured", 0), r.get("monitored", 0),
                    r.get("ours", 0), len(r.get("brands", {})))
        ping_dashboard_refresh()
    except Exception as e:
        logger.warning("올영 랭킹 수집 스킵(원격 MCP/네트워크 확인): %s", e)


def job_profile_sync() -> None:
    """Cafe24 → 자사 제품 라인 자동 동기화 (company_profile.md). 실패해도 파이프라인 무영향."""
    logger.info("=== 자사 제품 프로필 동기화 시작 ===")
    try:
        from analytics.product_sync import sync_company_profile
        sync_company_profile()
        logger.info("=== 제품 프로필 동기화 완료 ===")
    except Exception as e:
        logger.warning("제품 프로필 동기화 스킵(자격증명·연결 확인): %s", e)


def job_semantic_dedup() -> None:
    """의미 임베딩 기반 중복 병합 (매일 수집 후). 대표 외 is_duplicate 플래그."""
    logger.info("=== 의미 중복 병합 시작 ===")
    from deduplication.semantic_dedup import dedup_recent
    session = get_session()
    try:
        r = dedup_recent(session, days=10)
        logger.info("의미 중복 병합 완료: 임베딩 %d / 클러스터 %d / 중복 %d건",
                    r["embedded"], r["clusters"], r["marked"])
    except Exception as e:
        logger.error("의미 중복 병합 오류: %s", e)
    finally:
        session.close()


def job_weekly_dedup() -> None:
    """제목 유사도 기반 중복 쌍 기록."""
    logger.info("=== 주간 중복 정리 시작 ===")
    session = get_session()
    try:
        recent = get_recent_titles(session, days=7)
        count = 0
        for i in range(len(recent)):
            for j in range(i + 1, len(recent)):
                id1, title1 = recent[i]
                id2, title2 = recent[j]
                score = title_similarity(title1, title2)
                if score >= TITLE_SIMILARITY_THRESHOLD:
                    save_dedup_candidate(session, id1, id2, score)
                    count += 1
        logger.info("중복 후보 %d쌍 기록 완료", count)
    except Exception as e:
        logger.error("주간 중복 정리 오류: %s", e)
    finally:
        session.close()
    logger.info("=== 주간 중복 정리 완료 ===")


def create_scheduler() -> BackgroundScheduler:
    scheduler = BackgroundScheduler(timezone="Asia/Seoul")

    # 매일 09:00 & 18:00 KST — 하루 2회 수집(오전·저녁) → HIGH 속보를 오전/저녁 두 번 포착
    scheduler.add_job(
        job_daily_tier1,
        trigger=CronTrigger(hour="9,18", minute=0),
        id="daily_tier1",
        name="[일별] Tier1 브랜드x국가 수집 (오전·저녁)",
        max_instances=1,
        coalesce=True,
    )

    # 매주 월요일 20:00 KST (일별 수집 완료 후 풀스캔)
    scheduler.add_job(
        job_weekly_full,
        trigger=CronTrigger(day_of_week="mon", hour=20, minute=0),
        id="weekly_full",
        name="[주간] 전체 브랜드x국가 풀스캔",
        max_instances=1,
        coalesce=True,
    )

    # 매주 월요일 19:00 KST — 모멘텀 계산 (풀스캔 전 실행)
    scheduler.add_job(
        job_weekly_momentum,
        trigger=CronTrigger(day_of_week="mon", hour=19, minute=0),
        id="weekly_momentum",
        name="[주간] 브랜드 모멘텀 계산",
        max_instances=1,
        coalesce=True,
    )

    # 월·목 07:00 KST — 네이버 검색 트렌드 수집(국내 수요 신호). 월요일분은 주간 브리핑(08:00) 전.
    scheduler.add_job(
        job_search_trends,
        trigger=CronTrigger(day_of_week="mon,thu", hour=7, minute=0),
        id="search_trends",
        name="[주2회] 네이버 검색 트렌드 수집",
        max_instances=1,
        coalesce=True,
    )

    # 월·수·금 07:20 KST — 구글 트렌드(글로벌 수요) 수집 + 검색 급등 알림.
    scheduler.add_job(
        job_google_trends,
        trigger=CronTrigger(day_of_week="mon,wed,fri", hour=7, minute=20),
        id="google_trends",
        name="[주3회] 구글 트렌드(글로벌) 수집·급등감지",
        max_instances=1,
        coalesce=True,
    )

    # 매월 3일 06:30 KST — 관세청 화장품 수출통계(성과 신호). 관세청 월1회 갱신.
    scheduler.add_job(
        job_export_stats,
        trigger=CronTrigger(day=3, hour=6, minute=30),
        id="export_stats",
        name="[월간] 관세청 화장품 수출통계 수집",
        max_instances=1,
        coalesce=True,
    )

    # 매월 4일 06:40 KST — DART 경쟁사 재무(성과 신호). 수출통계(3일) 다음날.
    scheduler.add_job(
        job_dart_financials,
        trigger=CronTrigger(day=4, hour=6, minute=40),
        id="dart_financials",
        name="[월간] DART 경쟁사 재무 수집",
        max_instances=1,
        coalesce=True,
    )

    # 매월 4일 06:50 KST — KIPRIS 해외상표(진출 선행신호). DART 다음.
    scheduler.add_job(
        job_trademark,
        trigger=CronTrigger(day=4, hour=6, minute=50),
        id="trademark",
        name="[월간] KIPRIS 해외상표 수집",
        max_instances=1,
        coalesce=True,
    )

    # 매일 06:20 KST — 아마존 리테일 랭킹(실판매 성과·시계열). 랭킹 매일 변동 + 추세 축적.
    scheduler.add_job(
        job_retail_ranking,
        trigger=CronTrigger(hour=6, minute=20),
        id="retail_ranking",
        name="[매일] 아마존 리테일 랭킹 수집(9개국)",
        max_instances=1,
        coalesce=True,
    )

    # 매일 06:30 KST — 올리브영 국내 랭킹(국내 최대 H&B 채널·시계열). 원격 채널 MCP 프록시.
    scheduler.add_job(
        job_oliveyoung_ranking,
        trigger=CronTrigger(hour=6, minute=30),
        id="oliveyoung_ranking",
        name="[매일] 올리브영 국내 랭킹 수집",
        max_instances=1,
        coalesce=True,
    )

    # 매주 월요일 07:40 KST — 브랜드 스코어 시계열 스냅샷(리테일 수집 후, 브리핑 전).
    scheduler.add_job(
        job_score_snapshot,
        trigger=CronTrigger(day_of_week="mon", hour=7, minute=40),
        id="score_snapshot",
        name="[주간] 브랜드 스코어 스냅샷",
        max_instances=1,
        coalesce=True,
    )

    # 매주 월요일 17:00 KST — 자사 제품 프로필 Cafe24 동기화 (인사이트 생성 전 최신화)
    scheduler.add_job(
        job_profile_sync,
        trigger=CronTrigger(day_of_week="mon", hour=17, minute=0),
        id="profile_sync",
        name="[주간] 자사 제품 프로필 동기화",
        max_instances=1,
        coalesce=True,
    )

    # 매주 일요일 19:00 KST
    scheduler.add_job(
        job_weekly_dedup,
        trigger=CronTrigger(day_of_week="sun", hour=19, minute=0),
        id="weekly_dedup",
        name="[주간] 중복 정리",
        max_instances=1,
    )

    # 매주 월요일 08:00 KST — 지난주(월~일) 심층 종합 브리핑
    scheduler.add_job(
        generate_weekly_briefing,
        trigger=CronTrigger(day_of_week="mon", hour=8, minute=0),
        id="weekly_briefing",
        name="[주간] 심층 브리핑 생성 및 Slack 전송",
        max_instances=1,
    )

    # 매일 23:00 KST — 의미 중복 병합 (수집 완료 후, 아침 브리핑 전)
    scheduler.add_job(
        job_semantic_dedup,
        trigger=CronTrigger(hour=23, minute=0),
        id="semantic_dedup",
        name="[일간] 의미 중복 병합",
        max_instances=1,
        coalesce=True,
    )

    # 매일 08:00 KST — 전날 수집분 일간 브리핑
    scheduler.add_job(
        generate_daily_briefing,
        trigger=CronTrigger(hour=8, minute=0),
        id="daily_briefing",
        name="[일간] 브리핑 생성 및 Slack 전송",
        max_instances=1,
        coalesce=True,
    )

    # keep-alive 잡 제거 — Render 유료플랜은 유휴 슬립이 없어 핑 불필요.
    # (무료티어 시절 슬립 방지용이었음. 상시 핑이 오히려 사용시간을 소진했음.)

    return scheduler


def start() -> None:
    """스케줄러 독립 실행 (CLI용 — Ctrl+C 로 종료)."""
    import sys
    import time
    log_dir = os.path.join(os.path.dirname(__file__), "..", "logs")
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, "scheduler.log")
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    if hasattr(stream_handler.stream, "reconfigure"):
        try:
            stream_handler.stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    # force=True: cli() 그룹이 이미 basicConfig를 호출했으므로 기존 핸들러를
    # 교체해야 file_handler가 실제로 붙는다 (없으면 no-op → scheduler.log 미기록).
    logging.basicConfig(level=logging.INFO, handlers=[file_handler, stream_handler], force=True)
    scheduler = create_scheduler()

    logger.info("스케줄러 시작")
    for job in scheduler.get_jobs():
        logger.info("  %-14s %s", job.id, job.name)

    scheduler.start()
    try:
        while True:
            time.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        logger.info("스케줄러 종료")
