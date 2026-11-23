"""KIPRIS 키 교체 — 바꾸기 전에 확인하고, 바꾼 뒤에 장부를 푼다.

키만 갈아끼우면 안 된다. 세 가지가 같이 움직여야 수집이 다시 돈다.

  1. `.env` 의 KIPRIS_KEY        — 해외·국내가 **같은 키 하나**를 쓴다
  2. `api_quota` 장부 초기화     — 새 계정이면 한도가 새로 잡히는데, **우리 장부가
                                   막고 있으면** 다음 달 1일까지 국내상표가 안 돈다
  3. 스케줄러 재시작             — 안 하면 떠 있는 프로세스가 옛 키로 계속 돈다

이 도구는 1·3을 **대신 하지 않는다.** 바꾸기 전에 새 키가 진짜 되는지 보고(check),
바꾼 뒤에 장부를 푸는 것(reset)만 한다. `.env` 는 사람이 고친다 — 운영 자격증명
파일을 스크립트가 덮어쓰면, 실패했을 때 무엇이 남았는지 알 수 없다.

사용:
    # ① 발급받은 새 키가 두 서비스 다 되는지 — .env 는 안 건드린다
    KIPRIS_KEY_NEW=<새키> python -X utf8 tools/kipris_rotate.py check

    # ② (.env 교체 후) 이번 달 장부를 푼다
    python -X utf8 tools/kipris_rotate.py reset --force

    # 지금 상태와 남은 백로그 비용
    python -X utf8 tools/kipris_rotate.py status

🔴 키는 **절대 출력하지 않는다.** 앞 4자리만 보여 준다. 터미널 로그가 남는다.
"""
from __future__ import annotations

import argparse
import os
import sys
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config.settings as S          # noqa: E402  — load_dotenv 가 여기서 돈다
from sqlalchemy import create_engine, text   # noqa: E402

_KR_URL = "http://plus.kipris.or.kr/openapi/rest/trademarkInfoSearchService/freeSearchInfo"
_FR_URL = ("http://plus.kipris.or.kr/openapi/rest/"
           "ForeignTradeMarkAdvencedSearchService/advancedSearch")
_SERVICE = "kipris_trademark_kr"      # signals/trademark_kr.py 와 같은 값이어야 한다


def _mask(k: str) -> str:
    """키 확인용 꼬리표. 전체는 절대 찍지 않는다."""
    k = (k or "").strip()
    return f"{k[:4]}…({len(k)}자)" if k else "(없음)"


def _period() -> str:
    """KST 기준 'YYYY-MM'. trademark_kr._period() 와 같은 규칙."""
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y-%m")


# ── check ────────────────────────────────────────────────────────────────────

def _probe(name: str, url: str, params: dict, key: str) -> bool:
    """서비스 1회 호출. 성공/실패와 KIPRIS 가 돌려준 말을 그대로 보여 준다."""
    import requests
    try:
        r = requests.get(url, params={"accessKey": key, **params}, timeout=30)
        r.raise_for_status()
        root = ET.fromstring(r.content)
    except Exception as e:
        print(f"  ❌ {name:10} 호출 자체가 실패 — {type(e).__name__}: {str(e)[:120]}")
        return False

    # 🔴 두 서비스가 성공을 **다르게** 말한다. 처음엔 둘 다 'success' 표시를 요구했다가
    # 멀쩡한 해외상표를 실패로 읽었다(2026-11-20). 해외는 **정상일 때 header 가 빈 채로**
    # 오고 successYN 자체가 없다 — 운영 수집기(signals/trademark.py:148)도 successYN 이
    # 명시적으로 'N' 일 때만 실패로 본다. 여기도 그 기준에 맞춘다.
    msg = (root.findtext(".//resultMsg") or "").strip()
    yn = (root.findtext(".//successYN") or "").strip().upper()
    code = (root.findtext(".//resultCode") or "").strip()
    total = root.findtext(".//totalSearchCount")
    ok = (msg == "success") or (yn == "Y") or (yn != "N" and total is not None)
    if ok:
        got = f"  (검색결과 {total}건)" if total is not None else ""
        print(f"  ✅ {name:10} 정상{got}")
        return True

    said = msg or code or yn or "(응답에 결과 표시가 없음)"
    hint = ""
    if "LIMITED_NUMBER" in said.upper():
        hint = "  ← 이 키는 이미 한도를 썼습니다"
    elif "SERVICE" in said.upper() or "ACCESS" in said.upper():
        hint = "  ← 이 서비스를 구독 안 했을 수 있습니다"
    print(f"  ❌ {name:10} {said}{hint}")
    return False


def cmd_check(args) -> int:
    key = (os.getenv("KIPRIS_KEY_NEW") or "").strip()
    if not key:
        print("🔴 KIPRIS_KEY_NEW 가 비어 있습니다.")
        print("   KIPRIS_KEY_NEW=<새키> python -X utf8 tools/kipris_rotate.py check")
        return 2

    cur = (os.getenv("KIPRIS_KEY") or "").strip()
    print(f"새 키 : {_mask(key)}")
    print(f"현재  : {_mask(cur)}" + ("   ← 같은 키입니다" if key == cur else ""))
    print("\n두 서비스에 1회씩 겁니다(새 키의 한도를 2건 씁니다).\n")

    ok_kr = _probe("국내상표", _KR_URL,
                   {"word": "셀퓨전씨", "docsCount": 1, "docsStart": 1}, key)
    # 🔴 검색어는 **결과가 나오는 것**으로 쓴다. 'cellfusionc' 로 쟀더니 US 에 0건이라
    # 빈 응답이 왔고, 그걸 '구독 안 됨'으로 잘못 읽었다(2026-11-20). 0건과 권한거부는
    # 다르다 — 시험은 둘을 구분할 수 있는 입력으로 해야 한다.
    ok_fr = _probe("해외상표", _FR_URL,
                   {"tradeMarkName": "medicube", "collectionValues": "US",
                    "docsCount": 1, "currentPage": 1}, key)

    print()
    if ok_kr and ok_fr:
        print("✅ 둘 다 됩니다. 이제 `.env` 의 KIPRIS_KEY 를 바꾸고,")
        print("   스케줄러 재시작 → `reset --force` 로 장부를 푸세요.")
        return 0
    print("🔴 되지 않는 서비스가 있습니다. `.env` 를 바꾸기 전에 해결하세요.")
    print("   구독 목록에 '상표 출원 속보'(국내)와 '해외상표' 둘 다 있어야 합니다.")
    return 1


# ── status ───────────────────────────────────────────────────────────────────

def _engine():
    return create_engine(S.DATABASE_URL)


def cmd_status(args) -> int:
    s = S.DB_SCHEMA
    with _engine().connect() as c:
        print(f"접속   : {create_engine(S.DATABASE_URL).url.host}  /  {s}")
        print(f"키     : {_mask(os.getenv('KIPRIS_KEY'))}\n")

        print("── 호출 장부 ─────────────────────────────")
        rows = c.execute(text(
            f"SELECT period, used, updated_at FROM {s}.api_quota "
            f"WHERE service = :sv ORDER BY period DESC LIMIT 4"), {"sv": _SERVICE}).all()
        if not rows:
            print("  (기록 없음 — 아직 안 셌거나 장부가 비어 있습니다)")
        for p, used, at in rows:
            mark = "  ← 이번 달" if p == _period() else ""
            blocked = "  🔴 막힘(950 이상)" if used >= 950 else ""
            print(f"  {p}  used={used:>5}  {at:%Y-%m-%d %H:%M}{mark}{blocked}")

        print("\n── 남은 백로그 ───────────────────────────")
        done = c.execute(text(
            f"SELECT count(DISTINCT brand) FROM {s}.trademark_filings "
            f"WHERE country='KR'")).scalar() or 0
        filings = c.execute(text(
            f"SELECT count(*) FROM {s}.trademark_filings WHERE country='KR'")).scalar() or 0
        miss = c.execute(text(
            f"SELECT count(*) FROM (SELECT app_number FROM {s}.trademark_filings "
            f"WHERE country='KR' EXCEPT SELECT app_number FROM {s}.trademark_goods) x"
        )).scalar() or 0

        # 🔴 출원인 매핑이 없는 브랜드는 **호출을 못 한다** — 검색이 출원인 기준이다.
        # 예산이 아무리 남아도 안 받아진다. 남은 것에서 따로 세야 숫자가 거짓말을 안 한다.
        #
        # 🔴 **status='active' 만 센다.** 수집기(active_applicants)가 그렇게 고른다.
        # 처음엔 status 를 안 보고 셌다가 13개를 1개로 보고했다(2026-11-20) — 승인
        # 대기(pending)를 '매핑 있음'으로 읽은 것이다. 참고로 backlog() 에도 같은
        # 누락이 있으나, 거기선 '출원 0건' 절이 받쳐 줘서 목록에서 빠지진 않는다.
        no_app = c.execute(text(f"""
            SELECT count(*) FROM {s}.monitored_brands b
            WHERE b.is_active AND b.name <> '대기'
              AND NOT EXISTS (SELECT 1 FROM {s}.brand_applicants a
                              WHERE a.brand = b.name AND a.status = 'active')
        """)).scalar() or 0
        n_pending = c.execute(text(
            f"SELECT count(*) FROM {s}.brand_applicants WHERE status = 'pending'")).scalar() or 0

        from config.brands import ALL_BRANDS
        total = len(ALL_BRANDS)
        left = max(total - done, 0)
        reachable = max(left - no_app, 0)
        per = (filings / done) if done else 0.0

        # 브랜드당 호출은 **실측**이다. 2026-11-20 회차: 37개 브랜드에 약 338회
        # (검색 + 지정상품) ≈ 9회/브랜드. 전엔 '출원건수 × 1 + 5'로 추정했다가
        # 1,926회로 6배 부풀렸다 — 그 평균(36건/브랜드)은 2년치 백필에서 나온
        # 값인데 지금은 90일 창이라 애초에 비교 대상이 아니었다.
        _CALLS_PER_BRAND = 9
        est = reachable * _CALLS_PER_BRAND + miss

        print(f"  브랜드      {done}/{total} 수집됨 — 남은 {left}개")
        if no_app:
            print(f"              그중 {no_app}개는 🔴 **출원인 매핑이 없어 받을 수 없음**")
        print(f"  국내 출원   {filings}건 (브랜드당 평균 {per:.0f}건)")
        print(f"  지정상품    미수집 {miss}건  ← 출원건당 호출 1회")
        print(f"\n  받을 수 있는 {reachable}개를 끝내는 데 ≈ **{est:,}회** "
              f"(실측 {_CALLS_PER_BRAND}회/브랜드 기준)")
        if est > 950:
            print(f"  월 950회 한도로는 약 {est/950:.1f}개월 걸립니다.")
        if no_app:
            print(f"\n  🔴 {no_app}개는 예산을 풀어도 안 받아집니다 — 출원인(회사)이 "
                  f"active 로 정해져야 검색이 됩니다.")
            if n_pending:
                print(f"     승인 대기 중인 출원인 후보 {n_pending}건이 있습니다. "
                      f"승인하면 그만큼 풀립니다.")
    return 0


# ── reset ────────────────────────────────────────────────────────────────────

def cmd_reset(args) -> int:
    s, p = S.DB_SCHEMA, (args.period or _period())
    eng = _engine()
    with eng.begin() as c:
        row = c.execute(text(
            f"SELECT used FROM {s}.api_quota WHERE service=:sv AND period=:p"),
            {"sv": _SERVICE, "p": p}).first()
        if row is None:
            print(f"{p} 장부가 없습니다 — 이미 0입니다. 할 일이 없습니다.")
            return 0
        print(f"{p}  used = {row[0]}  →  0")
        if not args.force:
            print("\n실제로 지우려면 --force 를 붙이세요.")
            print("⚠️ `.env` 의 KIPRIS_KEY 를 **먼저** 새 키로 바꾸고 스케줄러를 재시작한 뒤에")
            print("   하세요. 옛 키인 채로 장부만 풀면 KIPRIS 한도를 그대로 때립니다 —")
            print("   한도를 때리면 그 달 남은 날의 다른 KIPRIS 기능까지 같이 죽습니다.")
            return 0
        c.execute(text(
            f"UPDATE {s}.api_quota SET used = 0, updated_at = NOW() "
            f"WHERE service=:sv AND period=:p"), {"sv": _SERVICE, "p": p})
    print("✅ 장부를 풀었습니다. 다음 월요일 07:10 회차부터 국내상표가 다시 돕니다.")
    # cli.py 에는 run-job 이 없다(2026-11-20 확인). 수동 실행은 러너 함수를 직접 부른다.
    print("   지금 바로 밀린 브랜드만 받으려면:")
    print('     python -X utf8 -c "from scheduler.runner import '
          'job_trademark_kr_catchup as j; j()"')
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="KIPRIS 키 교체 보조")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="KIPRIS_KEY_NEW 로 두 서비스 시험 호출(.env 안 건드림)")
    sub.add_parser("status", help="장부·백로그 현황")
    r = sub.add_parser("reset", help="이번 달 호출 장부 초기화")
    r.add_argument("--force", action="store_true", help="실제로 0 으로 만든다")
    r.add_argument("--period", help="YYYY-MM (기본: 이번 달)")
    a = ap.parse_args()
    return {"check": cmd_check, "status": cmd_status, "reset": cmd_reset}[a.cmd](a)


if __name__ == "__main__":
    raise SystemExit(main())
