"""长期记忆存取：pgvector 余弦相似度去重与检索。"""
from sqlalchemy import select

from ..db import Fact, utcnow


async def add_fact(session_factory, content: str, embedding: list[float], threshold: float) -> str:
    """写入一条事实；与已有事实高度相似时只刷新访问时间（去重）。返回 'new'/'dup'。"""
    async with session_factory() as s:
        nearest = (
            await s.execute(
                select(Fact, Fact.embedding.cosine_distance(embedding).label("dist"))
                .where(Fact.embedding.is_not(None))
                .order_by(Fact.embedding.cosine_distance(embedding))
                .limit(1)
            )
        ).first()
        if nearest is not None:
            fact, dist = nearest[0], float(nearest[1])
            if 1.0 - dist >= threshold:
                fact.last_accessed_at = utcnow()
                await s.commit()
                return "dup"
        s.add(Fact(content=content, embedding=embedding))
        await s.commit()
        return "new"


async def search_facts(
    session_factory, embedding: list[float], k: int = 8, min_similarity: float = 0.3
) -> list[tuple[str, float]]:
    """按余弦相似度检索 top-k 事实，返回 [(content, similarity)]。"""
    async with session_factory() as s:
        rows = (
            await s.execute(
                select(Fact, Fact.embedding.cosine_distance(embedding).label("dist"))
                .where(Fact.embedding.is_not(None))
                .order_by(Fact.embedding.cosine_distance(embedding))
                .limit(k)
            )
        ).all()
    out = []
    for row in rows:
        fact, dist = row[0], float(row[1])
        similarity = 1.0 - dist
        if similarity >= min_similarity:
            out.append((fact.content, similarity))
    return out


async def delete_fact(session_factory, fact_id: int) -> bool:
    async with session_factory() as s:
        fact = await s.get(Fact, fact_id)
        if fact is None:
            return False
        await s.delete(fact)
        await s.commit()
        return True
