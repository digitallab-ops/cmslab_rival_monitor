"""
의미 임베딩 기반 중복 병합

제목 문자열 유사도로는 같은 사건 다른 제목(예: 매체별 재보도)을 못 잡음.
title+details 임베딩 코사인 유사도로 브랜드 내 같은 사건을 클러스터링 →
대표 1건(최고 strategic_score)만 남기고 나머지는 is_duplicate=true 로 soft-flag.

- 삭제하지 않음(플래그) → 되돌리기 가능.
- 임베딩은 embedding 컬럼에 JSON 캐시 → 재계산 방지.
- 매일 수집 후(23:00 KST) dedup_recent() 실행.
"""

import json
import logging
import math
from collections import defaultdict

from openai import OpenAI
from sqlalchemy import text

from config.settings import OPENAI_API_KEY, DB_SCHEMA, EMBED_MODEL, DEDUP_COSINE_THRESHOLD

logger = logging.getLogger(__name__)

_client = None


def _get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=OPENAI_API_KEY)
    return _client


def embed_texts(texts: list[str]) -> list[list[float]]:
    """텍스트 배치 → 임베딩 벡터 목록 (한 번에 최대 100건씩)."""
    out: list[list[float]] = []
    for i in range(0, len(texts), 100):
        batch = [t[:1000] or " " for t in texts[i:i + 100]]
        resp = _get_client().embeddings.create(model=EMBED_MODEL, input=batch)
        out.extend([d.embedding for d in resp.data])
    return out


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def dedup_recent(session, days: int = 10, threshold: float = None) -> dict:
    """최근 N일 기사 임베딩→브랜드 내 클러스터링→대표 외 is_duplicate 플래그.

    반환: {embedded, clusters, marked}
    """
    threshold = DEDUP_COSINE_THRESHOLD if threshold is None else threshold

    rows = session.execute(text(f"""
        SELECT id, brand,
               COALESCE(NULLIF(title_ko,''), title) AS ttl,
               COALESCE(details,''), COALESCE(strategic_score,0),
               published_date, embedding, activity_type, brand_focus
        FROM {DB_SCHEMA}.news_articles
        WHERE published_date >= NOW() - (:days || ' days')::interval
          AND (is_duplicate IS NOT TRUE)
        ORDER BY brand, COALESCE(strategic_score,0) DESC, published_date ASC
    """), {"days": days}).fetchall()

    if not rows:
        return {"embedded": 0, "clusters": 0, "marked": 0}

    # 1) 임베딩 없는 것 계산·저장
    arts = []
    to_embed_idx, to_embed_txt = [], []
    for r in rows:
        aid, brand, ttl, det, score, pub, emb, act, focus = r
        vec = None
        if emb:
            try:
                vec = json.loads(emb)
            except Exception:
                vec = None
        arts.append({"id": aid, "brand": brand, "score": score or 0, "pub": pub,
                     "vec": vec, "act": act, "focus": focus})
        if vec is None:
            to_embed_idx.append(len(arts) - 1)
            to_embed_txt.append(f"{ttl} {det}")

    embedded = 0
    if to_embed_txt:
        vectors = embed_texts(to_embed_txt)
        for k, vidx in enumerate(to_embed_idx):
            arts[vidx]["vec"] = vectors[k]
            session.execute(text(f"""
                UPDATE {DB_SCHEMA}.news_articles SET embedding = :e WHERE id = :id
            """), {"e": json.dumps(vectors[k]), "id": arts[vidx]["id"]})
        embedded = len(vectors)
        session.commit()

    # 2) 브랜드별 전이적(union-find) 클러스터링
    #    같은 사건이 매체마다 표현이 달라 쌍 유사도는 낮아도(0.42~) 체인으로 연결됨.
    #   incidental(스쳐 언급, 다이제스트/나열형)은 클러스터 오염원 → 제외
    by_brand = defaultdict(list)
    for a in arts:
        if a["vec"] and a["focus"] != "incidental":
            by_brand[a["brand"]].append(a)

    clusters = 0
    marked = 0
    for brand, items in by_brand.items():
        n = len(items)
        if n < 2:
            continue
        parent = list(range(n))

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for i in range(n):
            for j in range(i + 1, n):
                # 같은 활동유형 + 임계값 이상만 병합(교차토픽 오병합 방지)
                if items[i]["act"] == items[j]["act"] \
                        and _cosine(items[i]["vec"], items[j]["vec"]) >= threshold:
                    parent[find(i)] = find(j)

        comps = defaultdict(list)
        for i in range(n):
            comps[find(i)].append(i)

        for members in comps.values():
            if len(members) < 2:
                continue
            # 대표: 최고 strategic_score, 동점이면 최초 published
            def _pub_ts(k):
                p = items[k]["pub"]
                return p.timestamp() if p else 0.0
            rep_k = sorted(members, key=lambda k: (-items[k]["score"], _pub_ts(k)))[0]
            dup_ids = [items[k]["id"] for k in members if k != rep_k]
            clusters += 1
            marked += len(dup_ids)
            session.execute(text(f"""
                UPDATE {DB_SCHEMA}.news_articles
                SET is_duplicate = TRUE, dup_of = :rep
                WHERE id = ANY(:ids)
            """), {"rep": items[rep_k]["id"], "ids": dup_ids})
    session.commit()

    logger.info("의미 dedup: 임베딩 %d, 클러스터 %d, 중복표시 %d건",
                embedded, clusters, marked)
    return {"embedded": embedded, "clusters": clusters, "marked": marked}
