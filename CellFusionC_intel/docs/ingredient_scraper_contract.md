# 전성분 스크래퍼 툴 규격 (oliveyoung-review MCP에 추가)

우리 쪽(rival_intel)은 이 규격대로 소비하도록 이미 구현돼 있습니다. 스크래퍼(Vercel
oliveyoung-review MCP)에 아래 툴을 추가하면 **바로 실제 전성분이 흘러들어옵니다.**
툴이 없으면 우리 쪽은 자동으로 C(LLM 추정)로 폴백하니, 급하지 않게 붙이셔도 됩니다.

---

## A. 올리브영 전성분 — `get_ingredients`

```
tool: get_ingredients(goods_no: str) -> JSON
```

- **입력**: `goods_no` (예: `"A000000257580"`) — 우리가 랭킹 수집으로 이미 갖고 있는 값.
- **동작**: 리뷰 수집 때 이미 여는 상품 상세(getGoodsArtcAjax 등)에서
  전성분 블록만 추가 파싱.
- ⚠️ **파싱 주의(중요)**: 올영은 전성분을 `전성분`이 아니라
  **"화장품법에 따라 기재해야 하는 모든 성분"** 문구로 **상품정보제공고시** 섹션에 넣습니다.
  - 검색 문구를 `전성분` → `화장품법에 따라 기재해야 하는 모든 성분` 으로. 그 라벨 셀
    다음의 `<td>`/`<dd>` 텍스트가 전성분 목록.
  - 이 고시 블록은 리뷰와 다른 AJAX/섹션일 수 있음 → 응답 HTML에 그 문구가 실제로
    들어오는지 먼저 확인(goods_name까지 비면 상세 자체를 못 받는 것).
- **반환(JSON)**:

```json
{
  "goods_no": "A000000257580",
  "goods_name": "VT 피디알엔 에어 클라우드 선스크린",
  "brand": "VT",
  "ingredients_raw": "정제수, 나이아신아마이드, 폴리디옥시리보뉴클레오티드나트륨, ...",
  "available": true,
  "source": "oliveyoung"
}
```

- **전성분 없을 때**: `{"goods_no": "...", "available": false, "ingredients_raw": ""}`
- `ingredients_raw`는 **원문 그대로(쉼표 구분 문자열)** 면 충분합니다. 파싱·정규화는 우리 쪽에서 함.
- (선택) 리스트로도 주면 좋음: `"ingredients": ["정제수","나이아신아마이드", ...]`

---

## B. (선택) 아마존 전성분 — `get_amazon_ingredients`

해외 제품(올영에 없는 것) 보강용. 여력 될 때.

```
tool: get_amazon_ingredients(url: str) -> JSON   # 우리가 가진 product_url 전달
```

```json
{ "url": "...", "ingredients_raw": "Water, Niacinamide, ...", "available": true, "source": "amazon" }
```

---

## 흐름 요약

```
[그쪽 스크래퍼]  get_ingredients(goods_no)  →  ingredients_raw
       │
       ▼
[우리 rival_intel]  전성분 파싱 → LLM 요약(핵심성분·효능·피부타입·셀퓨전씨 대응각)
                    → product_ingredients 저장 → 대시보드 '제품 전성분 인텔' 표시
                    (툴 없거나 available=false면 C: LLM 추정 + '추정' 배지)
```

- 우리는 `CHANNEL_MCP_URL`(oliveyoung-review.vercel.app) 로 `get_ingredients`를 호출합니다.
- 툴 이름·반환 키만 위와 맞으면 됩니다. 나머지(주기 수집·LLM·표시)는 우리 쪽에서 처리.
