"""적중표 화면 — 브리핑이 짚은 것과 그 뒤 실제 결과.

generate.py가 워낙 커서(7천 줄) 새 섹션은 여기 따로 둔다. 문자열 상수라
generate.py의 거대 f-string 밖에서 만들어지므로 중괄호 이스케이프가 필요 없다.
"""

_SB_STYLE = """<style>
#sb .sb-head{display:flex;align-items:baseline;gap:14px;flex-wrap:wrap;margin:0 0 14px}
#sb .sb-rate{font-size:30px;font-weight:700;letter-spacing:-1px;font-variant-numeric:tabular-nums}
#sb .sb-rate small{font-size:13px;font-weight:600;color:#8490b0;margin-left:5px;letter-spacing:0}
#sb .sb-cnt{font-size:13px;color:#8490b0}
#sb .sb-cnt b{color:#c6ccf2;font-variant-numeric:tabular-nums}
#sb .sb-none{font-size:13.5px;color:#8490b0;line-height:1.6}
#sb .sb-list{display:flex;flex-direction:column;border-top:1px solid #2f3a6b}
#sb .sb-row{display:grid;grid-template-columns:96px 1fr 240px;gap:16px;align-items:start;
  padding:13px 4px;border-bottom:1px solid rgba(47,58,107,.5)}
#sb .sb-when{font-size:12px;color:#6b769a;font-variant-numeric:tabular-nums;line-height:1.5}
#sb .sb-when i{display:block;font-style:normal;color:#8189cc;margin-top:2px}
#sb .sb-claim{font-size:14.5px;color:#e7eafc;line-height:1.5}
#sb .sb-claim em{font-style:normal;color:#8b95ff;font-weight:700;margin-right:7px}
#sb .sb-how{display:block;font-size:12px;color:#6b769a;margin-top:4px}
#sb .sb-res{font-size:13px;line-height:1.5}
#sb .sb-tag{display:inline-block;font-size:11.5px;font-weight:700;border-radius:5px;
  padding:2px 8px;margin-bottom:5px}
#sb .t-hit{background:rgba(5,224,224,.14);color:#05e0e0}
#sb .t-miss{background:rgba(255,107,122,.14);color:#ff6b7a}
#sb .t-unknown{background:rgba(132,144,176,.14);color:#8490b0}
#sb .t-pending{background:rgba(240,162,86,.14);color:#f0a256}
#sb .sb-note{color:#c6ccf2}
#sb .sb-note.dim{color:#8490b0}
#sb .sb-foot{font-size:12.5px;color:#6b769a;line-height:1.7;margin-top:12px}
@media (max-width:900px){#sb .sb-row{grid-template-columns:1fr}}
</style>"""

_STATUS = {
    "hit":     ("맞음", "t-hit"),
    "miss":    ("빗나감", "t-miss"),
    "unknown": ("판단불가", "t-unknown"),
}


def render_scoreboard(sb: dict, esc) -> str:
    """{rows, stat} → HTML. 데이터가 없으면 빈 문자열(섹션 자체를 안 그린다)."""
    rows = (sb or {}).get("rows") or []
    if not rows:
        return ""
    st = (sb or {}).get("stat") or {}
    hit, miss = st.get("hit", 0), st.get("miss", 0)
    rate = st.get("rate")
    pending = st.get("pending", 0)

    if rate is not None:
        head = (f'<span class="sb-rate">{rate}%<small>적중</small></span>'
                f'<span class="sb-cnt">맞음 <b>{hit}</b> · 빗나감 <b>{miss}</b>'
                f' · 결과 대기 <b>{pending}</b></span>')
    else:
        head = (f'<span class="sb-cnt">아직 기한이 된 항목이 없습니다 — '
                f'결과 대기 <b>{pending}</b>건. 그동안의 경과는 아래에 매일 갱신됩니다.</span>')

    out = []
    for r in rows:
        stt = r.get("status") or "pending"
        if stt == "pending":
            left = r.get("left")
            label = f"D-{left}" if isinstance(left, int) and left >= 0 else "기한 지남"
            tag = f'<span class="sb-tag t-pending">{esc(label)}</span>'
            note = r.get("progress") or "아직 움직임이 잡히지 않았습니다"
            note_cls = "sb-note dim"
        else:
            ko, cls = _STATUS.get(stt, ("판단불가", "t-unknown"))
            tag = f'<span class="sb-tag {cls}">{ko}</span>'
            note = r.get("note") or ""
            note_cls = "sb-note"
        brand = esc(r.get("brand") or "")
        out.append(
            '<div class="sb-row">'
            f'<div class="sb-when">{esc(r.get("said", ""))}<i>기한 {esc(r.get("due", ""))}</i></div>'
            f'<div class="sb-claim">{f"<em>{brand}</em>" if brand else ""}{esc(r.get("claim", ""))}'
            f'<span class="sb-how">확인 방법 — {esc(r.get("method") or "적히지 않음")}</span></div>'
            f'<div class="sb-res">{tag}<div class="{note_cls}">{esc(note)}</div></div>'
            '</div>')

    return (_SB_STYLE + '''
    <div class="section" id="sb">
      <div class="section-title">지난 판단, 맞았나<span class="section-sub">
        브리핑이 "지켜볼 것"으로 짚은 항목에 그 뒤 실제 수치를 붙였습니다 ·
        기한 전에는 지금까지의 경과를 매일 갱신합니다</span>
        <button class="collapse-btn" data-sec="sb-body"
                onclick="toggleSec('sb-body', this)">▼ 펼치기</button></div>
      <div id="sb-body">
      <div class="sb-head">''' + head + '''</div>
      <div class="sb-list">''' + "".join(out) + '''</div>
      <p class="sb-foot">
        채점은 우리가 수집하는 지표로만 합니다 — 관세청 수출액 · 올리브영 순위 ·
        해외 리테일 순위 · 기사량. 틱톡샵 매출처럼 수집하지 않는 지표는
        <b>맞혔다고 적지 않고</b> 판단불가로 둡니다. 수출액은 관세청 확정분이
        두 달가량 늦어 그만큼 결과가 늦게 나옵니다.<br>
        확인 방법을 적지 않은 옛 항목(23건)은 채점할 수 없어 적중률 계산에서 뺐습니다.
      </p>
      </div>
    </div>''')
