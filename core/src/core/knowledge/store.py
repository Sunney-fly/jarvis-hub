"""知识库存取：切片、向量化、余弦检索、集合管理。

设计要点（对应"外挂知识库"的验收标准）：
- 知识放在模型之外，随时增删改，不需要重训模型
- 切片 + 向量化后按余弦相似度检索，毫秒级，不影响即时回答
"""
import logging
import re

from sqlalchemy import delete, func, select

from ..db import KbChunk, KbCollection, KbDocument

logger = logging.getLogger("core.knowledge")

DEFAULT_CHUNK_SIZE = 900
DEFAULT_CHUNK_OVERLAP = 150


def normalize_collection(name: str) -> str:
    """知识库名规范化：去空格、限制长度，空则用 default。"""
    name = (name or "").strip()
    name = re.sub(r"\s+", "-", name)
    name = re.sub(r"[^\w\u4e00-\u9fff\-]", "", name)
    return name[:64] or "default"


def chunk_text(text: str, size: int = DEFAULT_CHUNK_SIZE, overlap: int = DEFAULT_CHUNK_OVERLAP) -> list[str]:
    """按段落优先、长度兜底的方式切片，带重叠避免语义被截断。"""
    text = (text or "").strip()
    if not text:
        return []
    if overlap >= size:
        overlap = size // 4
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    buf = ""
    for para in paragraphs:
        if len(para) > size:  # 超长段落按句号二次切
            sentences = re.split(r"(?<=[。！？.!?；;])", para)
        else:
            sentences = [para]
        for sent in sentences:
            sent = sent.strip()
            if not sent:
                continue
            if len(buf) + len(sent) + 1 <= size:
                buf = f"{buf}\n{sent}" if buf else sent
            else:
                if buf:
                    chunks.append(buf)
                # 前缀重叠：把上一个切片尾部拼到新切片开头
                tail = buf[-overlap:] if buf else ""
                buf = (tail + "\n" + sent).strip() if tail else sent
                while len(buf) > size * 2:  # 极端长句兜底硬切
                    chunks.append(buf[:size])
                    buf = buf[size - overlap:]
    if buf:
        chunks.append(buf)
    return chunks


async def ensure_collection(session_factory, name: str, description: str = "") -> str:
    name = normalize_collection(name)
    async with session_factory() as s:
        row = (await s.execute(select(KbCollection).where(KbCollection.name == name))).scalar_one_or_none()
        if row is None:
            s.add(KbCollection(name=name, description=description[:500]))
            await s.commit()
        elif description and not row.description:
            row.description = description[:500]
            await s.commit()
    return name


async def add_document(
    session_factory,
    embedder,
    collection: str,
    title: str,
    content: str,
    source_url: str = "",
    tags: str = "",
    origin: str = "manual",
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    chunk_overlap: int = DEFAULT_CHUNK_OVERLAP,
) -> dict:
    """写入一份知识：切片 → 向量化 → 落库。返回统计信息。"""
    collection = await ensure_collection(session_factory, collection)
    content = (content or "").strip()
    if not content:
        raise ValueError("内容为空")
    chunks = chunk_text(content, chunk_size, chunk_overlap)
    if not chunks:
        raise ValueError("切片后内容为空")

    vectors: list[list[float]] = []
    if embedder is not None:
        vectors, _ = await embedder.embed(chunks)
    if len(vectors) != len(chunks):
        vectors = [None] * len(chunks)  # 无嵌入时仍存文本，可关键词兜底

    async with session_factory() as s:
        doc = KbDocument(
            collection=collection,
            title=(title or content[:40]).strip()[:256],
            source_url=(source_url or "")[:512],
            content=content,
            tags=(tags or "")[:256],
            origin=origin,
            chunk_count=len(chunks),
        )
        s.add(doc)
        await s.flush()
        for i, (chunk, vec) in enumerate(zip(chunks, vectors)):
            s.add(
                KbChunk(
                    document_id=doc.id,
                    collection=collection,
                    seq=i,
                    content=chunk,
                    embedding=vec,
                )
            )
        await s.commit()
        doc_id = doc.id
    return {
        "document_id": doc_id,
        "collection": collection,
        "title": (title or content[:40]).strip()[:256],
        "chunks": len(chunks),
        "embedded": bool(embedder is not None),
        "chars": len(content),
    }


async def search(
    session_factory,
    embedder,
    query: str,
    collection: str | None = None,
    top_k: int = 6,
    min_similarity: float = 0.25,
) -> list[dict]:
    """向量检索知识库，返回 [{content, title, source_url, collection, similarity}]。"""
    query = (query or "").strip()
    if not query:
        return []
    top_k = max(1, min(20, top_k))

    if embedder is not None:
        vectors, _ = await embedder.embed([query])
        qvec = vectors[0] if vectors else None
    else:
        qvec = None

    async with session_factory() as s:
        if qvec is not None:
            stmt = select(KbChunk, KbChunk.embedding.cosine_distance(qvec).label("dist")).where(
                KbChunk.embedding.is_not(None)
            )
            if collection:
                stmt = stmt.where(KbChunk.collection == normalize_collection(collection))
            rows = (
                await s.execute(stmt.order_by(KbChunk.embedding.cosine_distance(qvec)).limit(top_k))
            ).all()
            hits = []
            for chunk, dist in rows:
                sim = 1.0 - float(dist)
                if sim < min_similarity:
                    continue
                hits.append((chunk, sim))
        else:
            # 无嵌入：退化为关键词包含匹配
            stmt = select(KbChunk).where(KbChunk.content.ilike(f"%{query}%")).limit(top_k)
            if collection:
                stmt = stmt.where(KbChunk.collection == normalize_collection(collection))
            chunks = (await s.execute(stmt)).scalars().all()
            hits = [(c, 0.5) for c in chunks]

        out = []
        for chunk, sim in hits:
            doc = await s.get(KbDocument, chunk.document_id)
            out.append(
                {
                    "content": chunk.content,
                    "similarity": round(sim, 4),
                    "collection": chunk.collection,
                    "document_id": chunk.document_id,
                    "title": doc.title if doc else "",
                    "source_url": doc.source_url if doc else "",
                }
            )
    return out


async def list_collections(session_factory) -> list[dict]:
    async with session_factory() as s:
        rows = (await s.execute(select(KbCollection).order_by(KbCollection.name))).scalars().all()
        doc_counts = dict(
            (await s.execute(select(KbDocument.collection, func.count()).group_by(KbDocument.collection))).all()
        )
        chunk_counts = dict(
            (await s.execute(select(KbChunk.collection, func.count()).group_by(KbChunk.collection))).all()
        )
    return [
        {
            "name": r.name,
            "description": r.description,
            "documents": int(doc_counts.get(r.name, 0)),
            "chunks": int(chunk_counts.get(r.name, 0)),
            "created_at": r.created_at.isoformat() if r.created_at else "",
        }
        for r in rows
    ]


async def list_documents(session_factory, collection: str | None = None, limit: int = 50) -> list[dict]:
    limit = max(1, min(200, limit))
    async with session_factory() as s:
        stmt = select(KbDocument).order_by(KbDocument.id.desc()).limit(limit)
        if collection:
            stmt = stmt.where(KbDocument.collection == normalize_collection(collection))
        rows = (await s.execute(stmt)).scalars().all()
    return [
        {
            "id": r.id,
            "collection": r.collection,
            "title": r.title,
            "source_url": r.source_url,
            "tags": r.tags,
            "origin": r.origin,
            "chunks": r.chunk_count,
            "chars": len(r.content or ""),
            "created_at": r.created_at.isoformat() if r.created_at else "",
            "preview": (r.content or "")[:200],
        }
        for r in rows
    ]


async def url_exists(session_factory, url: str) -> bool:
    if not url:
        return False
    async with session_factory() as s:
        row = (
            await s.execute(select(KbDocument.id).where(KbDocument.source_url == url[:512]).limit(1))
        ).first()
    return row is not None


async def delete_document(session_factory, document_id: int) -> dict:
    async with session_factory() as s:
        doc = await s.get(KbDocument, document_id)
        if doc is None:
            return {"deleted": 0, "reason": "没有这份文档"}
        await s.execute(delete(KbChunk).where(KbChunk.document_id == document_id))
        await s.delete(doc)
        await s.commit()
    return {"deleted": 1, "document_id": document_id}


async def delete_collection(session_factory, name: str) -> dict:
    name = normalize_collection(name)
    async with session_factory() as s:
        chunks = (await s.execute(delete(KbChunk).where(KbChunk.collection == name))).rowcount or 0
        docs = (await s.execute(delete(KbDocument).where(KbDocument.collection == name))).rowcount or 0
        await s.execute(delete(KbCollection).where(KbCollection.name == name))
        await s.commit()
    return {"collection": name, "documents_deleted": int(docs), "chunks_deleted": int(chunks)}


async def stats(session_factory) -> dict:
    async with session_factory() as s:
        docs = (await s.execute(select(func.count()).select_from(KbDocument))).scalar() or 0
        chunks = (await s.execute(select(func.count()).select_from(KbChunk))).scalar() or 0
        colls = (await s.execute(select(func.count()).select_from(KbCollection))).scalar() or 0
    return {"collections": int(colls), "documents": int(docs), "chunks": int(chunks)}
