"""사람이 채점한 분류 표본 → 정확도 리포트.

왜 사람 채점인가 — LLM 교차채점은 같은 모델 계열이라 ground truth가 못 된다
(실측에서 판정자가 골프 기사를 '브랜드 관련'으로 보는 등 원 분류가 맞는
경우가 많았다). 수치를 말하려면 사람이 본 표본이 필요하다.

실행: python -m tools.score_report "요청사항/분류_채점표_261104.xlsx"
"""

import collections
import sys

from openpyxl import load_workbook

COUNTED = ("primary", "secondary")   # 지표·브리핑에 들어가는 등급


def report(path: str) -> None:
    ws = load_workbook(path, data_only=True).active
    rows = []
    for r in ws.iter_rows(min_row=5, values_only=True):
        if not r or not r[0]:
            continue
        rows.append({"brand": r[1], "title": r[4], "ours": r[7],
                     "verdict": (r[8] or "").strip(), "truth": (r[9] or "").strip()})
    done = [x for x in rows if x["verdict"]]
    if not done:
        print("채점된 행이 없습니다. I열(판정)을 채워주세요.")
        return

    v = collections.Counter(x["verdict"] for x in done)
    n = len(done)
    print(f"채점 {n}/{len(rows)}건")
    for k in ("맞음", "틀림", "애매"):
        print(f"  {k:<5}{v[k]:>4}건  {100*v[k]/n:>5.1f}%")
    firm = v["맞음"] + v["틀림"]
    if firm:
        print(f"\n  정확도(애매 제외) {100*v['맞음']/firm:.1f}%")

    # 등급별 — 어디가 약한지
    print("\n── 우리 분류 등급별 정확도")
    by = collections.defaultdict(lambda: collections.Counter())
    for x in done:
        by[x["ours"]][x["verdict"]] += 1
    for g, c in sorted(by.items()):
        t = c["맞음"] + c["틀림"]
        rate = f"{100*c['맞음']/t:.0f}%" if t else "-"
        print(f"  {str(g):<12}맞음 {c['맞음']:>3} · 틀림 {c['틀림']:>3} · 애매 {c['애매']:>3}   정확도 {rate}")

    # 집계 오염 — 가장 중요한 두 수치
    fp = [x for x in done if x["ours"] in COUNTED and x["verdict"] == "틀림"
          and x["truth"] and x["truth"] not in COUNTED]
    fn = [x for x in done if x["ours"] not in COUNTED and x["verdict"] == "틀림"
          and x["truth"] and x["truth"] in COUNTED]
    cnt = sum(1 for x in done if x["ours"] in COUNTED)
    print(f"\n── 지표 영향")
    print(f"  집계에 넣었는데 넣지 말았어야  {len(fp)}건" +
          (f" / 집계분 {cnt}건 중 {100*len(fp)/cnt:.1f}%" if cnt else ""))
    print(f"  뺐는데 넣었어야               {len(fn)}건")
    for lab, lst in (("잘못 들어간 것", fp), ("놓친 것", fn)):
        if lst:
            print(f"\n  [{lab}]")
            for x in lst[:8]:
                print(f"    {x['brand']:<12}{x['ours']:<11}→{x['truth']:<11}{str(x['title'])[:44]}")


if __name__ == "__main__":
    report(sys.argv[1] if len(sys.argv) > 1
           else r"C:\loki_project\cmslab_rival\요청사항\분류_채점표_261104.xlsx")
