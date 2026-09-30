"""오늘 달라진 것 — 첫 화면 맨 위.

generate.py의 거대 f-string 밖에 두어 중괄호 이스케이프를 피한다.
"""

_CH_STYLE = """<style>
#chg .ch-grid{display:grid;grid-template-columns:minmax(0,1.15fr) minmax(0,1fr);gap:30px}
#chg .ch-col h4{margin:0 0 10px;font-size:12px;font-weight:700;letter-spacing:1.4px;
  color:#8189cc;padding-bottom:8px;border-bottom:1px solid #2f3a6b}
#chg .ch-row2{padding:10px 2px;border-bottom:1px solid rgba(47,58,107,.45)}
#chg .ch-row2:last-child{border-bottom:none}
#chg .ch-top{display:flex;align-items:baseline;gap:11px}
#chg .ch-p{font-size:12.5px;color:#8490b0;margin:3px 0 0 14px;line-height:1.45}
#chg .ch-b{font-size:14px;font-weight:700;color:#e7eafc;white-space:nowrap}
#chg .ch-w{font-size:12px;color:#6b769a;white-space:nowrap}
#chg .ch-m{font-size:13.5px;font-variant-numeric:tabular-nums;white-space:nowrap;
  margin-left:auto;font-weight:600}
#chg .up{color:#05e0e0}
#chg .dn{color:#ff6b7a}
#chg .new{color:#f0a256}
#chg .ch-why{font-size:12px;color:#8490b0;white-space:nowrap}
#chg .ch-star{color:#8b95ff;font-size:11px;margin-right:2px}
#chg .ch-n{display:block;padding:10px 2px;border-bottom:1px solid rgba(47,58,107,.45);
  text-decoration:none}
#chg .ch-n:last-child{border-bottom:none}
#chg .ch-n .t{display:block;font-size:13.5px;color:#dbe3f4;line-height:1.5}
#chg .ch-n:hover .t{color:#8fb4ff}
#chg .ch-n .m{display:block;font-size:11.5px;color:#6b769a;margin-top:3px}
#chg .ch-empty{font-size:13.5px;color:#8490b0;padding:14px 2px}
#chg .ch-foot{font-size:12px;color:#6b769a;margin-top:14px;line-height:1.7}
@media (max-width:900px){#chg .ch-grid{grid-template-columns:1fr}}
/* 곧 나올 것 — 화면이 답으로만 끝나면 다시 올 이유가 없다 */
#chg .up-strip{display:flex;flex-wrap:wrap;gap:0;margin:16px 0 0;border-top:1px solid #2f3a6b}
#chg .up-i{flex:1 1 220px;padding:12px 18px 12px 0;border-right:1px solid rgba(47,58,107,.5)}
#chg .up-i:last-child{border-right:none}
#chg .up-d{font-size:11px;font-weight:700;letter-spacing:1px;color:#f0a256;
  font-variant-numeric:tabular-nums}
#chg .up-t{font-size:14px;font-weight:700;color:#e7eafc;margin:4px 0 3px}
#chg .up-n{font-size:12px;color:#8490b0;line-height:1.5}
@media (max-width:900px){#chg .up-i{border-right:none;border-bottom:1px solid rgba(47,58,107,.5)}}
</style>"""


def render_changes(data: dict, esc, upcoming: list = None) -> str:
    """{rank, news, since} + 곧 나올 것 → HTML. 다 비면 섹션을 그리지 않는다."""
    rank = (data or {}).get("rank") or []
    news = (data or {}).get("news") or []
    up = upcoming or []
    if not rank and not news and not up:
        return ""

    def _move(m):
        if m.get("size") == 99:                      # 신규 진입
            cls, mark = "new", "신규"
        else:
            cls, mark = ("up", "▲") if m.get("up") else ("dn", "▼")
        star = '<span class="ch-star">★</span>' if m.get("ours") else ""
        # 순위가 붙는 대상은 브랜드가 아니라 **제품**이다. 제품명을 빼면
        # '센텔리안24라는 브랜드가 3위'로 읽힌다.
        prod = m.get("product") or ""
        return ('<div class="ch-row2">'
                '<div class="ch-top">'
                f'{star}<span class="ch-b">{esc(m.get("brand") or "")}</span>'
                f'<span class="ch-w">{esc(m.get("where") or "")}</span>'
                f'<span class="ch-why">{esc(m.get("why") or "")}</span>'
                f'<span class="ch-m {cls}">{mark} {esc(m.get("text") or "")}</span>'
                '</div>'
                + (f'<div class="ch-p">{esc(prod)}</div>' if prod else "")
                + '</div>')

    def _news(n):
        url = n.get("url") or ""
        inner = (f'<span class="t">{esc(n.get("text") or "")}</span>'
                 f'<span class="m">{esc(n.get("brand") or "")}'
                 f'{" · " + esc(n.get("where")) if n.get("where") else ""}</span>')
        if url:
            return f'<a class="ch-n" href="{esc(url)}" target="_blank" rel="noopener">{inner}</a>'
        return f'<div class="ch-n">{inner}</div>'

    left = ("".join(_move(m) for m in rank) if rank
            else '<div class="ch-empty">순위가 크게 움직인 브랜드가 없습니다.</div>')
    right = ("".join(_news(n) for n in news) if news
             else '<div class="ch-empty">새로 들어온 주요 소식이 없습니다.</div>')

    strip = ""
    if up:
        strip = ('<div class="up-strip">' + "".join(
            f'<div class="up-i"><div class="up-d">D-{u.get("days", 0)} · {esc(u.get("when", ""))}</div>'
            f'<div class="up-t">{esc(u.get("title", ""))}</div>'
            f'<div class="up-n">{esc(u.get("note", ""))}</div></div>'
            for u in up[:3]) + '</div>')

    return (_CH_STYLE + '''
    <div class="section" id="chg">
      <div class="section-title">오늘 달라진 것<span class="section-sub">
        어제 대비 새로 생긴 것만 · <span style="color:#8b95ff">★</span>는 우리 판(더마·선케어·스킨케어)</span>
        <button class="collapse-btn" data-sec="chg-body"
                onclick="toggleSec('chg-body', this)">▲ 접기</button></div>
      <div id="chg-body">
      <div class="ch-grid">
        <div class="ch-col"><h4>판매 순위가 움직인 제품</h4>''' + left + '''</div>
        <div class="ch-col"><h4>새로 들어온 소식</h4>''' + right + '''</div>
      </div>
      {UPSTRIP}
      <p class="ch-foot">
        여기 순위는 <b>브랜드 순위가 아니라 그 브랜드 제품의 판매 랭킹</b>입니다 —
        올리브영은 카테고리별 <b>상위 20개</b>, 아마존은 카테고리별 베스트셀러 순위입니다.
        <b>5계단 이상</b> 움직인 것만 올리고, 올리브영은 저장된 전일 대비,
        아마존은 직전 수집분과 비교합니다.<br>
        소식은 브랜드당 하루 한 건 — 같은 사건을 여러 매체가 쓰면 점수가 가장 높은
        하나만 남깁니다.
      </p>
      </div>
    </div>''').replace("{UPSTRIP}", strip)
