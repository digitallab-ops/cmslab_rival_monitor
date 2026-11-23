"""이관 컷오버 도구 — 알림 · 대사 기준값 · 대조 · 쓰기검증.

왜 따로 두나:
  컷오버 당일엔 APScheduler 를 **멈춘 채로** 작업한다. 그래서 이 도구는
  스케줄러 안의 잡이 아니라 **Windows 작업 스케줄러가 직접 부르는 독립 실행**이다.
  스케줄러가 멈춰도 알림은 계속 와야 하기 때문이다.

사용:
  python tools/cutover.py notify   --step stop_now
  python tools/cutover.py snapshot                 # 덤프 직전 기준값 저장
  python tools/cutover.py compare  --baseline <경로>
  python tools/cutover.py writetest
  python tools/cutover.py grants                   # 권한 회수 **전에** 떠둘 것
  python tools/cutover.py botmem --export

  시험 복원본 검증(.env 를 안 건드린다):
  python tools/cutover.py writetest --url "<RDS URL>" --schema rival_intel_test
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from datetime import datetime
from pathlib import Path

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 🔴 반드시 notifications.slack 보다 **먼저** 들어와야 한다.
# `.env` 를 읽는 쪽은 config.settings 의 load_dotenv() 뿐인데,
# slack.py 는 import 시점에 os.getenv 로 웹훅과 라벨을 읽는다.
# 순서가 바뀌면 웹훅이 빈 문자열이 되어 **조용히 전송 실패**한다
# (작업 스케줄러에서는 exit 1 만 남아 원인이 안 보인다).
import config.settings  # noqa: E402,F401

SNAP_DIR = Path(__file__).resolve().parent.parent / "logs" / "cutover"

# 덤프에서 빠지는 2개. 대사 대상도 아니다.
EXCLUDED = ("bot_conversations", "bot_user_memory")


# ─────────────────────────────────────────────────────────── 알림

STEPS: dict[str, tuple[str, str]] = {
    "fullscan_defer": (
        "11/25 — 오늘 풀스캔을 미뤄야 합니다",
        "오늘 20:00 주간 풀스캔 3/3 이 시작됩니다. 실측 2.5~3.2시간이라 "
        "*sales 컷오버 창(21:00~22:00)* 을 덮습니다.\n"
        "스키마는 다르지만 Supabase 인스턴스가 같습니다.\n\n"
        "• 미루려면: 작업 스케줄러에서 `CMSLab_RivalScheduler` 를 20:00 이전에 "
        "비활성화했다가 23:00 이후 다시 켜거나,\n"
        "• 저와 세션을 열어 `weekly_full_2` 잡만 하루 건너뛰게 하세요.",
    ),
    "move_pause": (
        "12/6 — 이사가 내일이면 지금 수집을 내리세요",
        "사옥 이전(12/6~9) 중 수집 PC 전원이 빠집니다. 전원을 내리기 *전에* "
        "`tools\\cutover_stop.ps1` 을 실행하세요.\n\n"
        "*왜 꼭 내려야 하나* — `CmsLab_RivalMonitor` 는 *부팅 트리거* 입니다. "
        "새 건물에서 PC 를 켜는 순간 수집이 깨어나는데, 그때 공인 IP 가 아직 "
        "RDS 보안그룹에 등록돼 있지 않으면 잡이 줄줄이 실패하고 파수꾼 알림이 "
        "쏟아집니다. 진짜 원인(IP 미등록)이 그 소음에 묻힙니다.\n\n"
        "*복귀 절차* — 회선 개통 → 공인 IP 확인 → 위시켓에 허용 요청 → "
        "열린 것 확인 후 `tools\\cutover_start.ps1`.\n\n"
        "상표·구글/네이버 트렌드·성분은 날짜구간·워터마크로 받아 *자동으로 메워집니다*. "
        "뉴스·미디어 RSS·순위는 그 시점만 존재하므로 공백이 남습니다.",
    ),
    "t_minus": (
        "11/28 — 5분 뒤 스케줄러를 자동 정지합니다",
        "11:30 에 `CMSLab_RivalScheduler` 작업을 *비활성화* 하고 프로세스를 내립니다.\n"
        "그 직후 34개 테이블 행수·시퀀스 기준값을 자동으로 떠둡니다.\n\n"
        "⚠️ 지금 돌고 있는 잡이 있으면 끝나기를 기다렸다 멈춥니다.\n\n"
        "🔴 *Render(`kbeauty-intel`) 도 지금 Suspend 하세요.* 읽기 전용이 아닙니다 — "
        "슬랙 봇이 브랜드·출원인 승인을(`monitored_brands`·`brand_candidates`·"
        "`brand_applicants`) 쓰고, 대시보드가 인사이트 캐시를 씁니다. 덤프(12:00) 뒤에 "
        "들어온 승인은 복원본에 없고 *에러 없이* 사라집니다.\n"
        "(봇 대화·기억 2개는 덤프 대상이 아닙니다 — `botmem --export` 로 따로 옮깁니다.)",
    ),
    "stopped": (
        "11/28 — 스케줄러 정지 완료 · 덤프 시작 신호를 남기세요",
        "수집이 멈췄고 대사 기준값을 저장했습니다. (아래 결과 참고)\n\n"
        "*지금 하실 일* — 이관 슬랙 방에 «rival_intel 스케줄러 정지 완료, 덤프 시작해 주세요» 를 남기세요.\n"
        "위시켓은 그 신호를 받고 덤프를 시작합니다.",
    ),
    "restore_check": (
        "11/28 16:00 — 복원 진행 상황을 확인하세요",
        "컷오버 창이 30분 남았습니다.\n\n"
        "• 위시켓 복원이 끝났는지 슬랙 방 확인\n"
        "• 끝났으면 저와 세션을 여세요 — `.env` 교체 → 대조 → 쓰기검증을 돌립니다\n"
        "• 아직이면 *미루는 게 맞습니다*. 11/29(일) 05:30 로 넘기면 됩니다.",
    ),
    "restart": (
        "11/28 16:30 — 재시작 판단 시점",
        "*끝났다면* 저와 세션에서 아래를 돌립니다.\n"
        "① `.env` 의 DB_* 를 RDS 로 교체  ② Render 환경변수 교체 + *재배포*\n"
        "③ 작업 재활성화  ④ 대조·쓰기검증\n\n"
        "17:00 Tier1 수집이 *RDS 첫 실쓰기* 가 됩니다.\n\n"
        "⚠️ Render 재배포를 빠뜨리지 마세요. 커밋 d4bc3d8(sslmode=require)은 "
        "origin·handover 양쪽에 올라가 있습니다(11/23 확인). 남은 건 Render 가 "
        "그 커밋으로 *실제 재배포됐는지* 확인하는 것입니다 — RDS 는 평문을 거부합니다.",
    ),
    "first_write": (
        "11/28 17:10 — RDS 첫 쓰기 결과를 확인하세요",
        "17:00 Tier1 수집이 돌고 있어야 합니다.\n\n"
        "• `logs/scheduler.log` 에 «[일별] Tier1 수집 시작» 이 찍혔는지\n"
        "• 권한 오류(`permission denied for schema` / `must be owner of table`)가 없는지\n\n"
        "⚠️ 우리 수집기는 실행마다 DDL(`CREATE TABLE IF NOT EXISTS` 등)을 돌립니다. "
        "앱 계정에 스키마 CREATE 권한과 테이블 소유권이 없으면 *여기서* 터집니다.",
    ),
    # 11/28 알림은 전부 그날 하루짜리다. 전환을 미루면 11/29 엔 아무것도 안 울린다.
    # 그리고 cutover_stop.ps1 이 작업을 *비활성화* 해놔서 증상이 에러가 아니라 침묵이다.
    # 파수꾼은 스케줄러 안에서 도니 같이 꺼져 있어 자기가 꺼진 걸 못 알린다.
    "still_down": (
        "수집이 아직 멈춰 있습니다",
        "컷오버 때 내린 작업이 *아직 비활성화* 상태입니다. "
        "11/28 알림은 그날로 끝났으니 이 메시지 말고는 더 울리지 않습니다.\n\n"
        "*되살리려면* — `.env` 의 DB_* 가 RDS 로 바뀌어 있는지 먼저 보고, "
        "`tools\\cutover_start.ps1` 을 실행하세요. 그 뒤 `cutover.py compare` 와 "
        "`writetest` 로 확인합니다.\n\n"
        "⚠️ 되살리기 전에 Render 환경변수 교체와 *재배포*도 끝나 있어야 합니다.",
    ),
}


def cmd_notify(args) -> int:
    from notifications.slack import notify_ops, mention_prefix

    title, detail = STEPS[args.step]
    extra = ""
    if args.step == "stopped":
        latest = _latest_snapshot()
        if latest:
            d = json.loads(latest.read_text(encoding="utf-8"))
            extra = (f"\n\n*기준값 저장됨* — 테이블 {len(d['tables'])}개 / "
                     f"합계 {sum(d['tables'].values()):,}행 / 시퀀스 {len(d['sequences'])}개\n"
                     f"`{latest}`")
        else:
            extra = "\n\n🔴 *기준값 파일을 찾지 못했습니다* — 대사를 못 합니다. 확인하세요."
    ok = notify_ops(title, mention_prefix() + detail + extra)
    print("슬랙 전송", "성공" if ok else "실패")
    return 0 if ok else 1


# ─────────────────────────────────────────────────────── 기준값/대조

def _targets(conn, schema: str):
    from sqlalchemy import text
    tables = [r[0] for r in conn.execute(text("""
        SELECT table_name FROM information_schema.tables
        WHERE table_schema = :s AND table_type = 'BASE TABLE'
        ORDER BY table_name"""), {"s": schema})]
    return [t for t in tables if t not in EXCLUDED]


def _collect(conn, schema: str) -> dict:
    """행수는 count(*) 로 센다 — n_live_tup 은 추정치라 대사에 쓰면 안 된다."""
    from sqlalchemy import text
    out = {"tables": {}, "sequences": {}}
    for t in _targets(conn, schema):
        out["tables"][t] = conn.execute(
            text(f'SELECT count(*) FROM "{schema}"."{t}"')).scalar()
    for (seq,) in conn.execute(text("""
            SELECT sequence_name FROM information_schema.sequences
            WHERE sequence_schema = :s ORDER BY sequence_name"""), {"s": schema}):
        out["sequences"][seq] = conn.execute(
            text(f'SELECT last_value FROM "{schema}"."{seq}"')).scalar()
    return out


def _open(args=None):
    """접속 대상을 연다.

    기본은 `.env` 의 DATABASE_URL·DB_SCHEMA 다. 시험 복원본처럼 **운영 설정을
    건드리지 않고** 다른 곳을 봐야 할 때만 --url / --schema 로 덮어쓴다.
    컷오버 전주에 .env 를 만지는 건 그 자체가 사고 원인이다.
    """
    import config.settings as S
    from sqlalchemy import create_engine
    from sqlalchemy.engine import make_url

    url = make_url(args.url) if getattr(args, "url", None) else S.DATABASE_URL
    schema = getattr(args, "schema", None) or S.DB_SCHEMA
    return create_engine(url), schema, url.host


def _add_target_opts(parser) -> None:
    parser.add_argument("--url", help="접속 URL 직접 지정(기본: .env 의 DATABASE_URL)")
    parser.add_argument("--schema", help="스키마 직접 지정(기본: .env 의 DB_SCHEMA)")


def _latest_snapshot() -> Path | None:
    if not SNAP_DIR.exists():
        return None
    files = sorted(SNAP_DIR.glob("baseline_*.json"))
    return files[-1] if files else None


def cmd_snapshot(args) -> int:
    eng, schema, host = _open(args)
    with eng.connect() as c:
        data = _collect(c, schema)
    data["taken_at"] = datetime.now().isoformat(timespec="seconds")
    data["host"] = host
    data["schema"] = schema

    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    out = Path(args.out) if args.out else \
        SNAP_DIR / f"baseline_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"기준값 저장 : {out}")
    print(f"  원본 호스트 : {host}")
    print(f"  테이블 {len(data['tables'])}개 / 합계 {sum(data['tables'].values()):,}행")
    print(f"  시퀀스 {len(data['sequences'])}개")
    return 0


def cmd_compare(args) -> int:
    base_path = Path(args.baseline) if args.baseline else _latest_snapshot()
    if not base_path or not base_path.exists():
        print("🔴 기준값 파일이 없습니다. snapshot 을 먼저 뜨세요.")
        return 1
    base = json.loads(base_path.read_text(encoding="utf-8"))

    eng, schema, host = _open(args)
    with eng.connect() as c:
        cur = _collect(c, schema)

    print(f"기준값 : {base_path.name}  ({base['host']})")
    print(f"현재   : {host}")
    if base["host"] == host:
        print("⚠️  두 쪽이 같은 호스트입니다 — .env 를 아직 안 바꿨을 수 있습니다.")
    print()

    bad = 0
    print("=== 테이블 행수 ===")
    for t in sorted(set(base["tables"]) | set(cur["tables"])):
        b, a = base["tables"].get(t), cur["tables"].get(t)
        if b == a:
            continue
        bad += 1
        print(f"  불일치 {t:32s} 기준 {b} → 현재 {a}")
    print(f"  {len(base['tables'])}개 중 불일치 {bad}개"
          f"{'  ✅ 전부 일치' if bad == 0 else ''}")

    print()
    print("=== 시퀀스 현재값 ===")
    sbad = 0
    for s in sorted(set(base["sequences"]) | set(cur["sequences"])):
        b, a = base["sequences"].get(s), cur["sequences"].get(s)
        if b == a:
            continue
        sbad += 1
        print(f"  불일치 {s:32s} 기준 {b} → 현재 {a}")
    print(f"  {len(base['sequences'])}개 중 불일치 {sbad}개"
          f"{'  ✅ 전부 일치' if sbad == 0 else ''}")

    print()
    print("판정:", "통과 ✅" if bad == 0 and sbad == 0 else "실패 🔴")
    return 0 if bad == 0 and sbad == 0 else 1


# ───────────────────────────────────────────────────────── 쓰기검증

def cmd_writetest(args) -> int:
    """읽기만 되고 쓰기가 막힌 경우를 잡는다.

    대시보드는 멀쩡해 보이는데 다음날부터 안 쌓이는 사고가 이것이다.
    DML 뿐 아니라 **DDL 도 본다** — 수집기가 실행마다 DDL 을 돌리기 때문이다.
    """
    from sqlalchemy import text
    eng, schema, host = _open(args)
    print(f"대상 호스트 : {host}  (스키마 {schema})")
    print()

    results: list[tuple[str, bool, str]] = []

    def run(label: str, sql: str, rollback: bool = True):
        with eng.connect() as c:
            try:
                c.execute(text(sql))
                if rollback:
                    c.rollback()
                else:
                    c.commit()
                results.append((label, True, ""))
            except Exception as e:
                results.append((label, False,
                                f"{e.__class__.__name__}: {str(e).splitlines()[0][:100]}"))

    run("DML  INSERT (롤백)",
        f"INSERT INTO {schema}.collection_runs (run_at, collector_type) "
        f"VALUES (NOW(), '__cutover_probe__')")
    run("DDL  CREATE TABLE IF NOT EXISTS",
        f"CREATE TABLE IF NOT EXISTS {schema}.news_articles (id bigint)")
    # 🔴 이미 있는 인덱스를 쓴다. 없는 이름으로 때리면 실제로 만들어지면서
    # 운영 테이블에 ShareLock 이 걸린다 — 권한 검사는 객체가 있어도 똑같이 돈다.
    run("DDL  CREATE INDEX IF NOT EXISTS",
        f"CREATE INDEX IF NOT EXISTS ix_google_trends_term_geo "
        f"ON {schema}.google_trends (term, geo, period DESC)")
    run("DDL  ALTER TABLE ADD COLUMN IF NOT EXISTS",
        f"ALTER TABLE {schema}.news_articles "
        f"ADD COLUMN IF NOT EXISTS brand_focus VARCHAR(20)")

    for label, ok, msg in results:
        print(f"  {'통과' if ok else '실패'}  {label}")
        if msg:
            print(f"        → {msg}")

    failed = [r for r in results if not r[1]]
    print()
    if not failed:
        print("판정: 통과 ✅  — 쓰기와 DDL 모두 가능합니다.")
        return 0
    print("판정: 실패 🔴")
    if any("DDL" in r[0] for r in failed):
        print("  DDL 이 막혔습니다. 앱 계정에 스키마 CREATE 권한과")
        print("  테이블 소유권(또는 소유 역할 멤버십)이 필요합니다.")
        print("  지금은 수집이 돌다가 잡 단위로 죽습니다 — 위시켓에 요청하세요.")
    return 1


# ─────────────────────────────────────────────── 권한·소유자 기록

_Q_SCHEMA = """
SELECT nspname, pg_get_userbyid(nspowner) AS owner, nspacl::text AS acl
FROM pg_namespace WHERE nspname = :s"""

_Q_OBJECTS = """
SELECT c.relkind, c.relname,
       pg_get_userbyid(c.relowner) AS owner, c.relacl::text AS acl
FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = :s AND c.relkind IN ('r','p','v','m','S')
ORDER BY c.relkind, c.relname"""

_Q_DEFACL = """
SELECT pg_get_userbyid(d.defaclrole) AS role,
       d.defaclobjtype AS objtype, d.defaclacl::text AS acl
FROM pg_default_acl d JOIN pg_namespace n ON n.oid = d.defaclnamespace
WHERE n.nspname = :s"""

_KIND = {"r": "테이블", "p": "파티션테이블", "v": "뷰", "m": "구체화뷰", "S": "시퀀스"}


def cmd_grants(args) -> int:
    """지금 누가 무엇을 소유하고 무슨 권한을 쥐고 있는지 파일로 남긴다.

    왜 필요한가: 컷오버 직후 Supabase 쓰기 권한을 회수하는데, **무엇을
    회수했는지 기록이 없으면 롤백할 때 복구가 추측이 된다.** 회수 전에 떠라.

    이 파일은 재생 스크립트가 아니라 **기록**이다. ACL 문자열(`app=arwdDxt/owner`)
    을 그대로 남기므로 원래 상태를 눈으로 대조할 수 있다.
    """
    from sqlalchemy import text
    eng, schema, host = _open(args)

    with eng.connect() as c:
        row = c.execute(text(_Q_SCHEMA), {"s": schema}).mappings().first()
        if row is None:
            print(f"🔴 스키마 {schema} 가 없습니다 ({host}).")
            return 1
        data = {
            "taken_at": datetime.now().isoformat(timespec="seconds"),
            "host": host,
            "schema": dict(row),
            "objects": [dict(r) for r in
                        c.execute(text(_Q_OBJECTS), {"s": schema}).mappings()],
            "default_acl": [dict(r) for r in
                            c.execute(text(_Q_DEFACL), {"s": schema}).mappings()],
        }

    SNAP_DIR.mkdir(parents=True, exist_ok=True)
    out = Path(args.out) if args.out else         SNAP_DIR / f"grants_{datetime.now():%Y%m%d_%H%M%S}.json"
    out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"권한 기록 저장 : {out}")
    print(f"  호스트 : {host}  /  스키마 : {schema}")
    print(f"  스키마 소유자 : {data['schema']['owner']}")
    print(f"  스키마 ACL    : {data['schema']['acl'] or '(기본값 — 명시 GRANT 없음)'}")
    print()

    owners: dict[str, int] = {}
    for o in data["objects"]:
        owners[o["owner"]] = owners.get(o["owner"], 0) + 1
    print("=== 객체 소유자 분포 ===")
    for who, n in sorted(owners.items(), key=lambda kv: -kv[1]):
        print(f"  {who:30s} {n:4d}개")

    granted = [o for o in data["objects"] if o["acl"]]
    print()
    print(f"=== 명시 GRANT 가 걸린 객체 {len(granted)}개 ===")
    for o in granted[:15]:
        print(f"  {_KIND.get(o['relkind'], o['relkind']):6s} "
              f"{o['relname']:30s} {o['acl']}")
    if len(granted) > 15:
        print(f"  … 나머지 {len(granted) - 15}개는 파일에 있습니다")

    if data["default_acl"]:
        print()
        print("=== 기본 권한(ALTER DEFAULT PRIVILEGES) ===")
        for d in data["default_acl"]:
            print(f"  {d['role']} / {d['objtype']} : {d['acl']}")
    return 0


# ──────────────────────────────────────────── 봇 기억 백업/복원

_BOT_COLS = {
    "bot_conversations": ("user_id", "role", "content", "created_at"),
    "bot_user_memory": ("user_id", "mem_key", "mem_value", "source", "updated_at"),
}


def cmd_botmem(args) -> int:
    """덤프에서 빠지는 2개 테이블을 우리 손으로 들고 넘어간다.

    개인정보라 위시켓 덤프에서 일부러 뺐다. 그래서 RDS 에는 **아예 없다.**
    코드(_ensure_bot_tables)가 빈 테이블을 다시 만들긴 하지만 내용은 안 돌아온다.
    `기억해` 로 쌓인 사용자 기억은 여기서 챙기지 않으면 그대로 사라진다.

    산출 파일은 logs/cutover/ 아래에 남고 .gitignore 로 막혀 있다 — 커밋되면 안 된다.
    """
    from sqlalchemy import text
    eng, schema, host = _open(args)

    if args.export:
        data = {"taken_at": datetime.now().isoformat(timespec="seconds"),
                "host": host, "schema": schema, "rows": {}}
        with eng.connect() as c:
            for t, cols in _BOT_COLS.items():
                try:
                    rs = c.execute(text(
                        f'SELECT {", ".join(cols)} FROM "{schema}"."{t}" ORDER BY id'))
                except Exception as e:
                    print(f"  건너뜀 {t} : {e.__class__.__name__}")
                    data["rows"][t] = []
                    continue
                data["rows"][t] = [
                    {k: (v.isoformat() if hasattr(v, "isoformat") else v)
                     for k, v in dict(r).items()} for r in rs.mappings()]

        SNAP_DIR.mkdir(parents=True, exist_ok=True)
        out = SNAP_DIR / f"botmem_{datetime.now():%Y%m%d_%H%M%S}.json"
        out.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"봇 기억 백업 : {out}")
        for t, rows in data["rows"].items():
            print(f"  {t:20s} {len(rows):5d}행")
        print()
        print("🔴 개인정보입니다. 메일·슬랙에 붙이지 마세요. 커밋도 막혀 있습니다.")
        return 0

    # ── 복원
    import config.settings as S
    if schema != S.DB_SCHEMA:
        print(f"🔴 복원은 운영 스키마({S.DB_SCHEMA})로만 합니다. --schema 를 빼세요.")
        return 1

    src = Path(args.restore)
    if not src.exists():
        print(f"🔴 파일이 없습니다 : {src}")
        return 1
    data = json.loads(src.read_text(encoding="utf-8"))
    print(f"복원 원본 : {src.name}  (뜬 시각 {data['taken_at']} / {data['host']})")
    print(f"대상      : {host}  스키마 {schema}")
    if data["host"] == host:
        print("⚠️  원본과 대상이 같은 호스트입니다 — .env 를 아직 안 바꿨을 수 있습니다.")

    from sqlalchemy.orm import Session as _Session
    from storage.repository import _ensure_bot_tables
    with _Session(eng) as sess:          # 테이블이 없으면 여기서 생긴다
        _ensure_bot_tables(sess)

    total = 0
    with eng.connect() as c:
        for t, cols in _BOT_COLS.items():
            rows = data["rows"].get(t, [])
            if not rows:
                print(f"  {t:20s} 백업에 행이 없습니다 — 건너뜁니다")
                continue
            have = c.execute(text(f'SELECT count(*) FROM "{schema}"."{t}"')).scalar()
            if have and not args.force:
                print(f"  🔴 {t} 에 이미 {have}행이 있습니다 — 중복을 막으려고 멈춥니다."
                      f" 정말이면 --force")
                return 1
            conflict = (" ON CONFLICT (user_id, mem_key) DO NOTHING"
                        if t == "bot_user_memory" else "")
            c.execute(text(
                f'INSERT INTO "{schema}"."{t}" ({", ".join(cols)}) '
                f'VALUES ({", ".join(":" + x for x in cols)}){conflict}'), rows)
            c.commit()
            total += len(rows)
            print(f"  {t:20s} {len(rows):5d}행 복원")

    print()
    print(f"복원 완료 — 합계 {total}행")
    return 0


# ───────────────────────────────────────────────────────────── main

def main() -> int:
    p = argparse.ArgumentParser(description="이관 컷오버 도구")
    sub = p.add_subparsers(dest="cmd", required=True)

    n = sub.add_parser("notify", help="슬랙 알림")
    n.add_argument("--step", required=True, choices=sorted(STEPS))
    n.set_defaults(fn=cmd_notify)

    s = sub.add_parser("snapshot", help="대사 기준값 저장")
    s.add_argument("--out")
    _add_target_opts(s)
    s.set_defaults(fn=cmd_snapshot)

    c = sub.add_parser("compare", help="기준값과 현재 DB 대조")
    c.add_argument("--baseline")
    _add_target_opts(c)
    c.set_defaults(fn=cmd_compare)

    w = sub.add_parser("writetest", help="쓰기·DDL 권한 검증")
    _add_target_opts(w)
    w.set_defaults(fn=cmd_writetest)

    g = sub.add_parser("grants", help="권한·소유자 현재 상태 기록")
    g.add_argument("--out")
    _add_target_opts(g)
    g.set_defaults(fn=cmd_grants)

    b = sub.add_parser("botmem", help="봇 기억 백업/복원(덤프에서 빠지는 2개 테이블)")
    bm = b.add_mutually_exclusive_group(required=True)
    bm.add_argument("--export", action="store_true")
    bm.add_argument("--restore", metavar="경로")
    b.add_argument("--force", action="store_true",
                   help="대상 테이블이 비어 있지 않아도 복원(중복 주의)")
    _add_target_opts(b)
    b.set_defaults(fn=cmd_botmem)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
