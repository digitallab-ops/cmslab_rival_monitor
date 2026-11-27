#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""11/28 컷오버 — 복원이 끝나는 순간을 기다렸다가 혼자 전환한다.

왜 이게 있나
------------
복원이 언제 끝나는지는 위시켓만 안다(예상 12:00~16:30, 폭이 4시간 반이다).
그동안 사람이 화면을 보고 있을 수 없고, 토요일이다. 그렇다고 알림만 보내면
"복원 끝났다 → 사람이 세션을 연다 → 전환한다" 사이가 몇 시간씩 빈다.

그래서 **완료를 코드가 알아보게** 한다.

완료 판정을 어떻게 하나 — 휴리스틱을 쓰지 않는다
------------------------------------------------
"테이블 34개 있으면 완료" 같은 건 쓰면 안 된다. 시험 복원본이 **이미** 그
조건을 만족하고 있어서(11/25자 269,387행), 폴링을 시작하자마자 "완료"로
오인한다. 복원 중간 상태도 똑같이 걸린다.

대신 **11:30 에 뜬 원본 기준값과 완전히 일치하는가**를 본다.
`cutover_stop.ps1` 이 수집을 세우고 바로 `cutover.py snapshot` 을 뜬다.
그 파일이 "원본이 멈춘 순간의 진실"이다. 복원본이 그것과
테이블 행수·시퀀스 현재값까지 전부 같아지면 — 그때가 복원 완료다.

이 판정은 휴리스틱이 아니라 **대사 그 자체**다. 완료 감지와 검증이 같은 일이다.
11/25자 옛 데이터도, 복원 중간 상태도 절대 통과하지 못한다.

막히면 전진이 아니라 정지
-------------------------
한 단계라도 어긋나면 아무것도 더 건드리지 않고 슬랙으로 알리고 멈춘다.
반쯤 복원된 DB 를 운영으로 물리는 게 이 작업에서 제일 무서운 사고다.

사용
----
  python tools/cutover_go.py --dry-run    # 리허설: 읽기만, 아무것도 안 바꾼다
  python tools/cutover_go.py              # 실제 (작업 스케줄러가 부른다)
"""

import argparse

import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

# 🔴 cutover.py 처럼 `sys.stdout = TextIOWrapper(...)` 로 **갈아끼우면 안 된다.**
# 여기서 갈아끼운 뒤 cutover.py 를 import 하면 그쪽이 또 갈아끼우고,
# 처음 래퍼가 회수되면서 **원본 버퍼를 닫는다** → 이후 print 가 전부
# ValueError: I/O operation on closed file 로 죽는다(작업 스케줄러에선 원인이 안 보인다).
# reconfigure 는 객체를 바꾸지 않아서 그 사고가 없다.
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

PROJ = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJ))

# 🔴 notifications.slack 보다 **먼저** 들어와야 한다 — cutover.py 와 같은 이유.
# slack.py 는 import 시점에 os.getenv 로 웹훅을 읽는데, .env 를 읽는 건
# config.settings 의 load_dotenv() 뿐이다. 순서가 바뀌면 조용히 전송 실패한다.
import config.settings  # noqa: E402,F401

from tools import cutover as CO        # noqa: E402  _collect / _latest_snapshot
from tools import render_ctl as RC     # noqa: E402

LOG = PROJ / "logs" / "cutover" / "go.log"
RESULT = PROJ / "logs" / "cutover" / "go_result.json"
RDS_FILE = PROJ / "logs" / "cutover" / "rds.txt"
ENV_FILE = PROJ / ".env"
SCHEMA = "rival_intel"
DB_KEYS = ("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME")


# ───────────────────────────────────────────────────────────── 출력

def say(msg: str) -> None:
    line = "{}  {}".format(datetime.now().strftime("%H:%M:%S"), msg)
    print(line, flush=True)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write("{}  {}\n".format(datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))


def slack(title: str, body: str) -> None:
    try:
        from notifications.slack import notify_ops, mention_prefix
        notify_ops(title, mention_prefix() + body)
    except Exception as e:
        say("⚠️ 슬랙 전송 실패: {}".format(e.__class__.__name__))


DRY = False  # 리허설이 가짜 결과 파일을 남기면 안 된다 — 알림이 그걸 믿는다.


def write_result(status: str, summary: str) -> None:
    """16:00·16:30 알림이 이 파일을 보고 '자동이 돌았는지'를 말해준다.

    파일이 **없으면 '아예 안 돎'** 이다. 작업이 뜨지도 않았다는 뜻이고,
    그게 제일 조용하고 제일 위험한 실패라서 꼭 구분해야 한다.
    """
    if DRY:
        say("[리허설] 결과 파일 기록 생략 ({})".format(status))
        return
    try:
        RESULT.parent.mkdir(parents=True, exist_ok=True)
        RESULT.write_text(json.dumps(
            {"status": status,
             "at": datetime.now().isoformat(timespec="seconds"),
             "summary": summary}, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        say("⚠️ 결과 파일 기록 실패: {}".format(e.__class__.__name__))


def die(title: str, body: str, code: int = 1):
    """멈춘다. 사람이 무엇을 해야 하는지까지 적어서 보낸다."""
    say("🔴 " + title)
    write_result("failed", title)
    slack("🔴 컷오버 자동전환 중단 — " + title,
          body + "\n\n자동 전환은 *여기서 멈췄습니다*. 더 건드리지 않았습니다.\n"
                 "로그: `logs/cutover/go.log`")
    sys.exit(code)


# ─────────────────────────────────────────────────── 접속정보 읽기

def read_rds() -> dict:
    """logs/cutover/rds.txt 를 읽는다. URL 한 줄이든 key: value 든 받는다."""
    if not RDS_FILE.exists():
        die("접속정보 파일이 없습니다",
            "`logs/cutover/rds.txt` 를 찾지 못했습니다. 위시켓 handoff 링크의 "
            "내용을 그 파일에 넣어야 자동 전환이 돕니다.")
    raw = RDS_FILE.read_text(encoding="utf-8-sig", errors="replace")

    m = re.search(r"postgres(?:ql)?://([^:\s]+):([^@\s]+)@([^:/\s]+):(\d+)/([^\s?]+)", raw)
    if m:
        u, p, h, pt, db = m.groups()
        d = {"user": u, "password": p, "host": h, "port": pt, "dbname": db}
    else:
        ALIAS = {
            "host": "host", "hostname": "host", "endpoint": "host", "address": "host",
            "주소": "host", "서버": "host",
            "port": "port", "포트": "port",
            "dbname": "dbname", "database": "dbname", "db": "dbname", "db명": "dbname",
            "데이터베이스": "dbname",
            "user": "user", "username": "user", "role": "user", "유저": "user",
            "사용자": "user", "계정": "user", "id": "user",
            "password": "password", "passwd": "password", "pw": "password",
            "비밀번호": "password", "패스워드": "password", "암호": "password",
        }
        d = {}
        for line in raw.splitlines():
            line = line.strip().lstrip("-*•").strip()
            if not line or line.startswith("#"):
                continue
            mm = re.match(r"^([A-Za-z_가-힣0-9 ]+?)\s*[:=]\s*(.+?)\s*$", line)
            if not mm:
                continue
            k = mm.group(1).strip().lower().replace(" ", "")
            v = mm.group(2).strip().strip('"').strip("'")
            if k in ALIAS and ALIAS[k] not in d:
                d[ALIAS[k]] = v
    d.setdefault("port", "5432")

    missing = [k for k in ("host", "port", "dbname", "user", "password") if not d.get(k)]
    if missing:
        die("접속정보를 못 읽었습니다",
            "`logs/cutover/rds.txt` 에서 다음을 못 찾았습니다: {}".format(", ".join(missing)))

    # 🔴 운영 사고 방지. 파싱이 어긋나 Supabase 나 localhost 를 물면
    #    운영 DB 에 쓰게 된다. 호스트가 RDS 가 아니면 여기서 죽는다.
    if "rds.amazonaws.com" not in d["host"]:
        die("접속정보가 RDS 가 아닙니다",
            "호스트가 `{}` 입니다. RDS 엔드포인트가 아니라서 중단합니다.".format(d["host"]))
    return d


def rds_url(d: dict) -> str:
    return "postgresql+psycopg2://{}:{}@{}:{}/{}?sslmode=require".format(
        quote(d["user"], safe=""), quote(d["password"], safe=""),
        d["host"], d["port"], d["dbname"])


# ─────────────────────────────────────────────────────── 기준값

def load_baseline(require_today: bool) -> tuple:
    p = CO._latest_snapshot()
    if not p:
        die("기준값 스냅샷이 없습니다",
            "`logs/cutover/baseline_*.json` 이 없습니다. 11:30 `cutover_stop.ps1` 이 "
            "떠야 생깁니다. 정지가 안 돈 것 같습니다 — 확인하세요.")
    data = json.loads(p.read_text(encoding="utf-8"))
    taken = data.get("taken_at", "")
    today = datetime.now().strftime("%Y-%m-%d")
    if require_today and not taken.startswith(today):
        die("기준값이 오늘 것이 아닙니다",
            "가장 최근 기준값이 `{}` ({}) 입니다. 오늘 11:30 스냅샷이 아니면 "
            "대사 결과를 믿을 수 없습니다.\n"
            "`cutover_stop.ps1` 의 snapshot 단계가 실패했을 수 있습니다."
            .format(p.name, taken))
    return p, data


def diff(base: dict, cur: dict) -> tuple:
    """(불일치 테이블 수, 불일치 시퀀스 수, 사람이 읽을 요약)"""
    tb = [t for t in set(base["tables"]) | set(cur["tables"])
          if base["tables"].get(t) != cur["tables"].get(t)]
    sb = [s for s in set(base["sequences"]) | set(cur["sequences"])
          if base["sequences"].get(s) != cur["sequences"].get(s)]
    lines = []
    for t in sorted(tb)[:8]:
        lines.append("  {:32s} 기준 {} → 복원 {}".format(
            t, base["tables"].get(t), cur["tables"].get(t)))
    if len(tb) > 8:
        lines.append("  … 외 {}개".format(len(tb) - 8))
    return len(tb), len(sb), "\n".join(lines)


# ───────────────────────────────────────────── ① 복원 완료 대기

def wait_for_restore(url: str, base: dict, until: datetime, interval: int,
                     dry: bool = False) -> dict:
    from sqlalchemy import create_engine

    eng = create_engine(url, connect_args={"connect_timeout": 20})
    n = 0
    if dry:
        # 리허설은 한 번만 보고 넘어간다. 19시까지 붙잡고 있으면 리허설이 아니다.
        with eng.connect() as c:
            cur = CO._collect(c, SCHEMA)
        tb, sb, detail = diff(base, cur)
        say("[리허설] 1회 확인 — 테이블 불일치 {}개 / 시퀀스 불일치 {}개".format(tb, sb))
        say("[리허설] (실제로는 이 둘이 0 이 될 때까지 기다린다)")
        if detail:
            for ln in detail.splitlines()[:4]:
                say("    " + ln)
        return cur
    last_report = datetime.now()
    while datetime.now() < until:
        n += 1
        try:
            with eng.connect() as c:
                cur = CO._collect(c, SCHEMA)
            tb, sb, detail = diff(base, cur)
            if tb == 0 and sb == 0:
                say("✅ 복원 완료 감지 ({}회차) — 기준값과 완전 일치".format(n))
                return cur
            say("대기 {}회차 — 테이블 불일치 {}개 / 시퀀스 불일치 {}개".format(n, tb, sb))
        except Exception as e:
            # 복원 중에는 DROP SCHEMA 로 스키마가 아예 없을 수 있다. 정상이다.
            say("대기 {}회차 — 아직 못 붙음 ({})".format(n, e.__class__.__name__))

        # 한 시간에 한 번만 살아있다고 알린다. 더 자주 보내면 알림이 쓰레기가 된다.
        if datetime.now() - last_report > timedelta(hours=1):
            last_report = datetime.now()
            slack("컷오버 자동전환 — 복원 대기 중",
                  "{}회 확인했고 아직 복원 완료 신호가 없습니다. "
                  "{} 까지 계속 봅니다.".format(n, until.strftime("%H:%M")))
        time.sleep(interval)

    die("복원 완료를 끝내 확인하지 못했습니다",
        "{} 까지 {}회 확인했지만 기준값과 일치하지 않았습니다.\n"
        "복원이 늦어지는 중이거나, 복원본이 기준값과 다릅니다.\n"
        "저(클로드)와 세션을 열어 손으로 진행해야 합니다."
        .format(until.strftime("%H:%M"), n))


# ────────────────────────────────────────────── ② .env 교체

def swap_env(d: dict, dry: bool) -> None:
    text = ENV_FILE.read_text(encoding="utf-8")
    new_vals = {"DB_HOST": d["host"], "DB_PORT": d["port"], "DB_USER": d["user"],
                "DB_PASSWORD": d["password"], "DB_NAME": d["dbname"]}

    out, seen = [], set()
    for line in text.splitlines():
        m = re.match(r"^(\s*)([A-Z_][A-Z0-9_]*)(\s*=)(.*)$", line)
        if m and m.group(2) in new_vals:
            k = m.group(2)
            seen.add(k)
            out.append("{}={}".format(k, new_vals[k]))
        else:
            out.append(line)
    missing = [k for k in DB_KEYS if k not in seen]
    if missing:
        die(".env 에서 바꿀 줄을 못 찾았습니다",
            "다음 키가 `.env` 에 없습니다: {}\n손으로 넣어야 합니다.".format(", ".join(missing)))

    if dry:
        say("[리허설] .env 5줄 교체 — 실제로는 안 씀")
        return

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = PROJ / "logs" / "cutover" / "env_backup_{}.bak".format(stamp)
    shutil.copy2(ENV_FILE, backup)
    ENV_FILE.write_text("\n".join(out) + "\n", encoding="utf-8")
    say(".env 교체 완료 (백업 {})".format(backup.name))

    # 되읽어 확인한다. 썼다고 반영된 것은 아니다.
    chk = {}
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            if k.strip() in new_vals:
                chk[k.strip()] = v.strip()
    bad = [k for k, v in new_vals.items() if chk.get(k) != v]
    if bad:
        shutil.copy2(backup, ENV_FILE)
        die(".env 교체가 반영되지 않았습니다",
            "반영 안 된 키: {}\n백업으로 되돌렸습니다.".format(", ".join(bad)))
    say(".env 되읽기 확인 — 5개 모두 반영")


# ────────────────────────────────────── ③ Render 환경변수 + resume

def render_switch(d: dict, dry: bool) -> None:
    pairs = ["DB_HOST=" + d["host"], "DB_PORT=" + d["port"],
             "DB_USER=" + d["user"], "DB_PASSWORD=" + d["password"],
             "DB_NAME=" + d["dbname"]]
    # argv 를 거치지 않고 함수로 직접 부른다 — 비밀번호가 프로세스 목록에 안 뜬다.
    ns = argparse.Namespace(pairs=pairs, yes=not dry)
    if RC.cmd_env_set(ns) != 0:
        die("Render 환경변수 교체 실패",
            "`render_ctl.py env-set` 이 실패했습니다. Render 는 아직 정지 상태입니다.\n"
            "수집기는 이미 RDS 를 보고 있으니 *수집은 돕니다*. 대시보드만 내려가 있습니다.")
    if dry:
        say("[리허설] Render resume 생략")
        return

    say("Render resume 요청")
    if RC.cmd_resume(argparse.Namespace()) != 0:
        die("Render resume 실패",
            "환경변수는 바뀌었지만 서비스가 안 올라옵니다. 대시보드에서 직접 Resume 하세요.")


def wait_live(until_min: int, dry: bool) -> bool:
    """resume 은 재빌드를 트리거한다(11/23 실측). live 가 될 때까지 본다."""
    if dry:
        say("[리허설] 배포 대기 생략")
        return True
    deadline = datetime.now() + timedelta(minutes=until_min)
    while datetime.now() < deadline:
        r = RC._get("/services/{}/deploys".format(RC.SERVICE_ID), limit=1)
        if r.status_code == 200 and r.json():
            dep = r.json()[0]
            st = (dep.get("deploy", dep)).get("status")
            say("배포 상태 {}".format(st))
            if st == "live":
                return True
            if st in ("build_failed", "update_failed", "canceled", "deactivated"):
                return False
        time.sleep(30)
    say("⚠️ {}분 안에 live 가 안 됐습니다".format(until_min))
    return False


# ───────────────────────────────────────────── ④ 수집기 재가동

def restart_collector(dry: bool) -> bool:
    if dry:
        say("[리허설] 수집기 재가동 생략")
        return True
    ps = shutil.which("powershell") or "powershell"
    r = subprocess.run(
        [ps, "-NoProfile", "-ExecutionPolicy", "Bypass",
         "-File", str(PROJ / "tools" / "cutover_start.ps1")],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    for ln in (r.stdout or "").splitlines():
        say("  " + ln)
    return r.returncode == 0 and "🔴" not in (r.stdout or "")


# ───────────────────────────────────────────────── ⑤ 최종 검증

def final_checks(dry: bool) -> tuple:
    """별도 프로세스로 돈다 — 바뀐 .env 를 새로 읽어야 하기 때문이다."""
    if dry:
        say("[리허설] 최종 검증 생략")
        return True, True
    def run(args):
        r = subprocess.run([sys.executable, "-X", "utf8",
                            str(PROJ / "tools" / "cutover.py")] + args,
                           capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=900)
        for ln in (r.stdout or "").splitlines():
            say("  " + ln)
        return r.returncode == 0
    say("대사(compare) 실행")
    ok1 = run(["compare"])
    say("쓰기검증(writetest) 실행")
    ok2 = run(["writetest"])
    return ok1, ok2


# ─────────────────────────────────────────────────────────── main

def main() -> int:
    ap = argparse.ArgumentParser(description="11/28 컷오버 자동 전환")
    ap.add_argument("--until", default="19:00", help="복원 대기 마감 시각 HH:MM (기본 19:00)")
    ap.add_argument("--interval", type=int, default=300, help="확인 간격 초 (기본 300)")
    ap.add_argument("--dry-run", action="store_true",
                    help="리허설 — 읽기만 하고 .env·Render·수집기를 안 건드린다")
    ap.add_argument("--allow-old-baseline", action="store_true",
                    help="리허설용: 오늘 것이 아닌 기준값도 허용")
    a = ap.parse_args()

    global DRY
    dry = DRY = a.dry_run
    say("=== 컷오버 자동 전환 {}시작 ===".format("[리허설] " if dry else ""))

    d = read_rds()
    say("대상 {} / DB {} / 역할 {}".format(d["host"], d["dbname"], d["user"]))

    bp, base = load_baseline(require_today=not (dry or a.allow_old_baseline))
    say("기준값 {} — 테이블 {}개 / 합계 {:,}행 / 시퀀스 {}개".format(
        bp.name, len(base["tables"]), sum(base["tables"].values()), len(base["sequences"])))

    hh, mm = (int(x) for x in a.until.split(":"))
    until = datetime.now().replace(hour=hh, minute=mm, second=0, microsecond=0)
    if until < datetime.now():
        until = datetime.now() + timedelta(minutes=10)

    if not dry:
        slack("컷오버 자동 전환 시작 — 복원을 기다립니다",
              "기준값 `{}` (테이블 {}개 / {:,}행).\n"
              "{}초마다 RDS 를 보고 *기준값과 완전히 일치하는 순간* 전환합니다.\n"
              "마감 {}. 손대실 것 없습니다 — 끝나면 결과를 보냅니다."
              .format(bp.name, len(base["tables"]),
                      sum(base["tables"].values()), a.interval, a.until))

    cur = wait_for_restore(rds_url(d), base, until, a.interval, dry)
    rows = sum(cur["tables"].values())
    if not dry:
        slack("복원 완료 감지 — 전환을 시작합니다",
              "복원본이 기준값과 *완전히 일치*합니다 (테이블 {}개 / {:,}행 / 시퀀스 {}개).\n"
              "이제 `.env` → Render → 수집기 순서로 올립니다."
              .format(len(cur["tables"]), rows, len(cur["sequences"])))

    swap_env(d, dry)
    render_switch(d, dry)
    live = wait_live(20, dry)
    started = restart_collector(dry)
    ok_cmp, ok_wr = final_checks(dry)

    lines = [
        "대사(행수·시퀀스)   {}".format("✅ 일치" if ok_cmp else "🔴 불일치"),
        "쓰기·DDL 검증       {}".format("✅ 통과" if ok_wr else "🔴 실패"),
        "수집기              {}".format("✅ 기동" if started else "🔴 확인 필요"),
        "Render 대시보드     {}".format("✅ live" if live else "⚠️ 아직 배포 중"),
    ]
    allok = ok_cmp and ok_wr and started and live
    say("=== 종료 — {} ===".format("전부 통과" if allok else "확인 필요"))

    if dry:
        print("\n".join(lines))
        return 0

    write_result("ok" if allok else "partial", " / ".join(lines))

    if allok:
        slack("✅ 컷오버 전환 완료 — RDS 로 올라갔습니다",
              "\n".join(lines) + "\n\n테이블 {}개 / {:,}행.\n"
              "17:00 Tier1 수집이 *RDS 첫 실쓰기* 가 됩니다. "
              "결과는 17:10 에 따로 알려드립니다.".format(len(cur["tables"]), rows))
        return 0

    slack("⚠️ 컷오버 전환 — 올라갔지만 확인할 것이 있습니다",
          "\n".join(lines) + "\n\n*전환 자체는 끝났습니다* — `.env` 와 Render 는 RDS 를 "
          "보고 있습니다. 위에서 🔴 인 항목만 확인하면 됩니다.\n"
          "로그: `logs/cutover/go.log`")
    return 1


if __name__ == "__main__":
    sys.exit(main())
