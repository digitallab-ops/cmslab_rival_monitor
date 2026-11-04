"""분류 정확도 측정 — 규칙 검사 + LLM 교차채점.

왜 필요한가 — 지금까지 분류가 맞는지 틀리는지 **측정한 적이 없다.**
우연히 발견한 오류만 고쳐왔는데(뎁티 18건 오귀속, 한글 브랜드 미인식 등),
그런 밀도면 못 찾은 게 더 있다고 보는 게 맞다. 수치가 있어야 어디를 고칠지 안다.

두 단계로 본다.
  1) 규칙 검사 — 비용 0. 스스로 모순인 것만 잡는다(브랜드가 본문에 없는데
     primary, 점수와 중요도 불일치 등). 반박 불가한 오류다.
  2) LLM 교차채점 — 분류기와 **다른 프롬프트**로 다시 보게 한다. 자기 채점이
     되지 않도록 원 분류 결과를 보여주지 않고 독립 판정시킨 뒤 비교한다.

실행: python -m tools.classify_eval [표본수]
"""

import json
import os
import random
import sys

from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from config.settings import DB_SCHEMA                       # noqa: E402
from config.brands import canonical_brand, BRAND_KO_NAMES   # noqa: E402
from storage.models import get_session                      # noqa: E402

_FOCUS_WITH_BRAND = ("primary", "secondary")


def _aliases(brand: str) -> list:
    c = canonical_brand(brand) or brand
    return [c, c.replace(" ", "")] + list(BRAND_KO_NAMES.get(c, []))


def _mentioned(row: dict) -> bool:
    """브랜드가 기사 어디에든 나오는가 — 제목·번역·요약·본문 전부."""
    blob = " ".join(str(row.get(k) or "") for k in
                    ("title", "title_ko", "details", "article_body", "article_body_ko")).lower()
    return any(a.lower() in blob for a in _aliases(row["brand"]) if a)


def rule_checks(rows: list) -> dict:
    """스스로 모순인 것만. 판단이 갈릴 여지가 없는 것들이다."""
    out = {k: [] for k in ("ghost", "score_mismatch", "unrelated_but_scored", "empty_details")}
    for r in rows:
        if r["brand_focus"] in _FOCUS_WITH_BRAND and not _mentioned(r):
            out["ghost"].append(r)
        s = r.get("strategic_score")
        if s is not None:
            want = "high" if s >= 75 else ("medium" if s >= 55 else "low")
            if want != r.get("importance"):
                out["score_mismatch"].append(r)
        if r["brand_focus"] == "unrelated" and (s or 0) >= 55:
            out["unrelated_but_scored"].append(r)
        if not (r.get("details") or "").strip():
            out["empty_details"].append(r)
    return out


_JUDGE = """다음 뉴스가 '{brand}' 브랜드와 어떤 관계인지 판정하라.
원래 분류 결과는 알려주지 않는다. 기사만 보고 네가 직접 판단하라.

제목: {title}
한글제목: {title_ko}
본문: {body}

brand_focus 중 하나를 고른다:
- primary    : 기사의 주인공이 이 브랜드
- secondary  : 여러 브랜드 중 하나로 의미있게 다뤄짐
- incidental : 업계 동향 기사에서 예시로 잠깐 언급. 또는 브랜드명이 모회사를
               설명하는 수식어로 쓰였고 실제 주체는 모회사·다른 브랜드
- unrelated  : 기사 어디에도 이 브랜드가 나오지 않음

JSON만: {{"focus":"...", "why":"15자 이내"}}"""


def llm_judge(rows: list, model: str = "gpt-4o-mini") -> list:
    from openai import OpenAI
    cli = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    res = []
    for r in rows:
        try:
            resp = cli.chat.completions.create(
                model=model, max_tokens=80, temperature=0,
                response_format={"type": "json_object"},
                messages=[{"role": "user", "content": _JUDGE.format(
                    brand=r["brand"], title=(r.get("title") or "")[:200],
                    title_ko=(r.get("title_ko") or "")[:200],
                    body=(r.get("article_body") or r.get("details") or "")[:2500])}])
            d = json.loads(resp.choices[0].message.content or "{}")
            res.append({**r, "judge": d.get("focus"), "why": d.get("why", "")})
        except Exception as e:
            res.append({**r, "judge": None, "why": f"오류 {type(e).__name__}"})
    return res


_REL = {"primary": 3, "secondary": 2, "incidental": 1, "unrelated": 0}


def severity(a: str, b: str) -> str:
    """불일치의 무게. primary↔secondary는 경계 판단이라 가볍고,
    '관련 있다 ↔ 없다'가 갈리면 지표를 직접 오염시키므로 무겁다."""
    x, y = _REL.get(a), _REL.get(b)
    if x is None or y is None:
        return "?"
    if x == y:
        return "일치"
    # 집계에 들어가나(primary/secondary) 아니냐(incidental/unrelated)가 갈리는가
    if (x >= 2) != (y >= 2):
        return "심각"
    return "경미"


def run(n: int = 100, judge: bool = True) -> dict:
    se = get_session()
    try:
        rows = [dict(x) for x in se.execute(text(f"""
            SELECT id, brand, brand_focus, importance, strategic_score, activity_type,
                   title, title_ko, details, article_body, article_body_ko
            FROM {DB_SCHEMA}.news_articles
            WHERE collected_at >= CURRENT_DATE - 30 AND is_duplicate IS NOT TRUE
            ORDER BY random() LIMIT :n"""), {"n": n}).mappings()]
    finally:
        se.close()
    rc = rule_checks(rows)
    out = {"sampled": len(rows), "rules": {k: len(v) for k, v in rc.items()}, "detail": rc}
    if judge and rows:
        j = llm_judge(rows)
        agree = sum(1 for x in j if x["judge"] == x["brand_focus"])
        graded = sum(1 for x in j if x["judge"])
        out["judge"] = {"graded": graded, "agree": agree,
                        "rate": round(100 * agree / graded, 1) if graded else None,
                        "disagree": [x for x in j if x["judge"] and x["judge"] != x["brand_focus"]]}
    return out


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 100
    r = run(n)
    print(f"표본 {r['sampled']}건\n")
    print("── 규칙 검사(반박 불가한 모순)")
    labels = {"ghost": "브랜드가 기사에 없는데 primary/secondary",
              "score_mismatch": "점수↔중요도 불일치",
              "unrelated_but_scored": "unrelated인데 점수 55+",
              "empty_details": "details 비어 있음"}
    for k, v in r["rules"].items():
        pct = 100 * v / r["sampled"] if r["sampled"] else 0
        print(f"   {labels[k]:<34}{v:>4}건 ({pct:.1f}%)")
    if "judge" in r:
        j = r["judge"]
        print(f"\n── LLM 교차채점 (독립 판정 후 비교)")
        print(f"   일치 {j['agree']}/{j['graded']} = {j['rate']}%")
        print(f"\n   불일치 상위 10건:")
        for x in j["disagree"][:10]:
            print(f"     [{x['id']}] {x['brand']:<14} 분류={x['brand_focus']:<10} "
                  f"재판정={x['judge']:<10} {x['why'][:18]}")
            print(f"            {(x.get('title_ko') or x.get('title') or '')[:62]}")
