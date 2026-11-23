"""Render 서비스 제어 — 컷오버 때 웹을 내리고 올리고, 접속정보를 갈아끼운다.

왜 필요한가:
    Render 는 읽기 전용이 아니다. 슬랙 봇이 브랜드·출원인 승인을 쓰고
    대시보드가 인사이트 캐시를 쓴다. 덤프(12:00) 이후에 들어온 승인은
    복원본에 없고 **에러 없이** 사라진다. 그래서 수집기와 같은 시각에 내린다.

🔴 env-set 주의:
    Render 의 환경변수 교체 API(PUT)는 목록을 **통째로 갈아끼운다**.
    DB_HOST 만 보내면 나머지(슬랙 토큰·API 키)가 전부 지워진다.
    그래서 반드시 read -> merge -> write -> verify 순서로만 쓴다.
    읽기가 0건이면 쓰지 않고 멈춘다 — 빈 목록으로 덮어쓰는 사고를 막는다.

사용:
    python -X utf8 tools/render_ctl.py status
    python -X utf8 tools/render_ctl.py suspend
    python -X utf8 tools/render_ctl.py resume
    python -X utf8 tools/render_ctl.py env-dump
    python -X utf8 tools/render_ctl.py env-set DB_HOST=... DB_NAME=... --yes
    python -X utf8 tools/render_ctl.py deploys
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import requests

API = "https://api.render.com/v1"
TIMEOUT = 30

# 블루프린트에 적힌 이름(kbeauty-intel)과 실제 서비스 이름이 다르다.
# 이름으로 찾지 말고 ID 로 박아둔다 — 이름은 언제든 바뀐다.
SERVICE_ID = os.getenv("RENDER_SERVICE_ID", "srv-d95j31pkh4rs738htalg")
SERVICE_NAME = "cmslab_rival_monitor"

# 값을 찍으면 안 되는 변수 — 로그가 슬랙·파일에 남는다.
SECRET_HINTS = ("KEY", "TOKEN", "PASSWORD", "SECRET", "WEBHOOK", "DSN")


def _mask(key: str, value) -> str:
    v = "" if value is None else str(value)
    if any(h in key.upper() for h in SECRET_HINTS):
        return (v[:4] + "***") if v else "(빈값)"
    return v


def _load_key() -> str:
    key = (os.getenv("RENDER_API_KEY") or "").strip()
    if key:
        return key
    env = Path(__file__).resolve().parent.parent / ".env"
    if env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            if "=" not in line or line.lstrip().startswith("#"):
                continue
            name, val = line.split("=", 1)
            # '=' 앞뒤 공백을 허용한다. 손으로 붙여넣으면 흔히 들어간다.
            if name.strip() == "RENDER_API_KEY":
                return val.strip().strip('"').strip("'")
    print("RENDER_API_KEY 가 없습니다 (.env 또는 환경변수).", file=sys.stderr)
    sys.exit(2)


def _headers() -> dict:
    return {"Authorization": "Bearer " + _load_key(), "Accept": "application/json"}


def _get(path: str, **params):
    return requests.get(API + path, headers=_headers(),
                        params=params or None, timeout=TIMEOUT)


def cmd_status(_args) -> int:
    r = _get("/services/" + SERVICE_ID)
    if r.status_code != 200:
        print("조회 실패 {}: {}".format(r.status_code, r.text[:200]))
        return 1
    s = r.json()
    d = s.get("serviceDetails", {})
    print("{}  {}".format(s.get("name"), s.get("id")))
    print("  상태     : {}".format(s.get("suspended")))
    print("  브랜치   : {}   autoDeploy={}".format(s.get("branch"), s.get("autoDeploy")))
    print("  리전/플랜: {} / {}".format(d.get("region"), d.get("plan")))
    return 0


def _toggle(action: str) -> int:
    """suspend / resume. 이미 그 상태면 호출하지 않고 성공으로 본다."""
    want = "suspended" if action == "suspend" else "not_suspended"
    r = _get("/services/" + SERVICE_ID)
    if r.status_code != 200:
        print("🔴 상태 조회 실패 {}: {}".format(r.status_code, r.text[:200]))
        return 1
    now = r.json().get("suspended")
    if now == want:
        print("이미 {} 입니다 — 호출 생략".format(want))
        return 0

    r2 = requests.post("{}/services/{}/{}".format(API, SERVICE_ID, action),
                       headers=_headers(), timeout=TIMEOUT)
    # Render 는 202(Accepted) 로 답한다. 200 도 성공으로 받는다.
    if r2.status_code not in (200, 202):
        print("🔴 {} 실패 {}: {}".format(action, r2.status_code, r2.text[:200]))
        return 1

    # 요청이 받아들여진 것과 실제로 반영된 것은 다르다 — 다시 읽어서 확인한다.
    r3 = _get("/services/" + SERVICE_ID)
    got = r3.json().get("suspended") if r3.status_code == 200 else "?"
    ok = (got == want)
    print("{} {}: {} -> {}".format("✅" if ok else "⚠️", action, now, got))
    return 0 if ok else 1


def cmd_suspend(_args) -> int:
    return _toggle("suspend")


def cmd_resume(_args) -> int:
    return _toggle("resume")


# Render 의 limit 상한. 200 을 넣으면 400 "invalid limit: too large" 가 난다.
ENV_PAGE = 100


def _env_list():
    r = _get("/services/{}/env-vars".format(SERVICE_ID), limit=ENV_PAGE)
    if r.status_code != 200:
        print("🔴 환경변수 조회 실패 {}: {}".format(r.status_code, r.text[:200]))
        sys.exit(1)
    out = []
    for it in r.json():
        e = it.get("envVar", it)
        if "key" in e:
            out.append({"key": e["key"], "value": e.get("value", "")})
    if len(out) >= ENV_PAGE:
        # 한 페이지를 꽉 채웠다 = 뒤가 잘렸을 수 있다.
        # 잘린 목록으로 PUT 하면 못 읽은 변수들이 조용히 사라진다.
        print("🔴 환경변수가 {}개 이상입니다 — 목록이 잘렸을 수 있어 중단합니다.".format(ENV_PAGE))
        print("   페이지네이션을 붙이기 전에는 env-set 를 쓰지 마세요.")
        sys.exit(1)
    return out


def cmd_env_dump(_args) -> int:
    cur = _env_list()
    print("{} 환경변수 {}개 (비밀값은 앞 4자만)".format(SERVICE_NAME, len(cur)))
    for e in sorted(cur, key=lambda x: x["key"]):
        print("  {:<24} = {}".format(e["key"], _mask(e["key"], e["value"])))
    return 0


def cmd_env_set(args) -> int:
    pairs = {}
    for item in args.pairs:
        if "=" not in item:
            print("형식 오류: {} (KEY=VALUE)".format(item))
            return 2
        k, v = item.split("=", 1)
        pairs[k.strip()] = v
    if not pairs:
        print("바꿀 변수가 없습니다")
        return 2

    cur = _env_list()
    if not cur:
        # 빈 목록으로 PUT 하면 서비스의 모든 변수가 날아간다.
        print("🔴 현재 환경변수를 0건으로 읽었습니다 — 덮어쓰지 않고 중단합니다.")
        return 1

    before = {e["key"]: e["value"] for e in cur}
    merged = dict(before)
    merged.update(pairs)

    print("현재 {}개 -> 적용 후 {}개".format(len(before), len(merged)))
    for k, v in pairs.items():
        was = before.get(k)
        tag = "신규" if was is None else "변경"
        old = _mask(k, was) if was is not None else "-"
        print("  [{}] {:<20} {}  ->  {}".format(tag, k, old, _mask(k, v)))
    kept = len(before) - len([k for k in pairs if k in before])
    print("  (그대로 두는 변수 {}개 — 함께 다시 써서 보존합니다)".format(kept))

    if not args.yes:
        print("")
        print("실제로 적용하려면 --yes 를 붙이세요. (지금은 미리보기)")
        return 0

    body = [{"key": k, "value": v} for k, v in merged.items()]
    r = requests.put("{}/services/{}/env-vars".format(API, SERVICE_ID),
                     headers=dict(_headers(), **{"Content-Type": "application/json"}),
                     json=body, timeout=TIMEOUT)
    if r.status_code not in (200, 201):
        print("🔴 적용 실패 {}: {}".format(r.status_code, r.text[:300]))
        return 1

    # 되읽어서 확인한다. 200 을 받았다고 반영된 것은 아니다.
    after = {e["key"]: e["value"] for e in _env_list()}
    bad = [k for k, v in pairs.items() if after.get(k) != v]
    lost = [k for k in before if k not in after]
    if bad:
        print("🔴 반영 안 된 변수: {}".format(", ".join(bad)))
    if lost:
        print("🔴 사라진 변수: {}".format(", ".join(lost)))
    if bad or lost:
        return 1
    print("✅ 적용·확인 완료 ({}개 유지)".format(len(after)))
    print("   autoDeploy 가 켜져 있으면 Render 가 알아서 재배포합니다 — deploys 로 확인하세요.")
    return 0


def cmd_deploys(args) -> int:
    r = _get("/services/{}/deploys".format(SERVICE_ID), limit=args.limit)
    if r.status_code != 200:
        print("조회 실패 {}: {}".format(r.status_code, r.text[:200]))
        return 1
    for it in r.json():
        d = it.get("deploy", it)
        c = d.get("commit") or {}
        raw = c.get("message") or ""
        msg = raw.splitlines()[0][:45] if raw else ""
        print("  {:<12} {:<9} {}  {}".format(
            str(d.get("status")), str(c.get("id"))[:7],
            d.get("finishedAt") or d.get("createdAt"), msg))
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Render 서비스 제어 (컷오버용)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="서비스 상태").set_defaults(fn=cmd_status)
    sub.add_parser("suspend", help="서비스 정지").set_defaults(fn=cmd_suspend)
    sub.add_parser("resume", help="서비스 재개").set_defaults(fn=cmd_resume)
    sub.add_parser("env-dump", help="환경변수 조회 (비밀값 마스킹)").set_defaults(fn=cmd_env_dump)

    e = sub.add_parser("env-set", help="환경변수 교체 (read-merge-write-verify)")
    e.add_argument("pairs", nargs="+", metavar="KEY=VALUE")
    e.add_argument("--yes", action="store_true", help="실제 적용 (없으면 미리보기)")
    e.set_defaults(fn=cmd_env_set)

    d = sub.add_parser("deploys", help="최근 배포")
    d.add_argument("--limit", type=int, default=5)
    d.set_defaults(fn=cmd_deploys)

    args = p.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
