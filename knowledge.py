"""外挂知识库工具：检索、录入、全网自动采集、集合管理。

对应"知识库内容由需求方自行决定、随时可插拔"：
- kb_search   : 提问时先查知识库（RAG 检索，不改模型）
- kb_add      : 手动把一段知识存进某个知识库
- kb_harvest  : 让贾维斯自己上网搜、筛选、整理成一个独立知识库
- kb_list     : 看有哪些知识库、里面有什么
- kb_delete   : 删掉某份文档（旧资料换新资料）
"""
import json

from ...config import Settings
from ..registry import Tool


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


def tools(settings: Settings, session_factory, router_getter, embedder_getter) -> list[Tool]:
    from ...knowledge import harvest as kb_harvest
    from ...knowledge import store as kb

    async def _search(args: dict) -> str:
        query = str(args.get("query", "")).strip()
        if not query:
            return _dumps({"error": "query 不能为空"})
        hits = await kb.search(
            session_factory,
            embedder_getter(),
            query,
            collection=args.get("collection") or None,
            top_k=int(args.get("top_k") or settings.kb_top_k),
            min_similarity=settings.kb_min_similarity,
        )
        if not hits:
            return _dumps(
                {
                    "found": 0,
                    "note": "知识库里没有相关内容。可以改用 search_web 联网查，"
                            "或用 kb_harvest 把该主题的资料采集进知识库。",
                }
            )
        return _dumps(
            {
                "found": len(hits),
                "results": [
                    {
                        "content": h["content"],
                        "from": h["title"],
                        "source": h["source_url"],
                        "collection": h["collection"],
                        "similarity": h["similarity"],
                    }
                    for h in hits
                ],
            }
        )

    async def _add(args: dict) -> str:
        content = str(args.get("content", "")).strip()
        if not content:
            return _dumps({"error": "content 不能为空"})
        try:
            info = await kb.add_document(
                session_factory,
                embedder_getter(),
                str(args.get("collection") or "default"),
                str(args.get("title") or ""),
                content,
                source_url=str(args.get("source_url") or ""),
                tags=str(args.get("tags") or ""),
                origin="manual",
            )
        except Exception as e:  # noqa: BLE001
            return _dumps({"error": f"入库失败: {e}"})
        return _dumps({"ok": True, **info})

    async def _harvest(args: dict) -> str:
        topic = str(args.get("topic", "")).strip()
        if not topic:
            return _dumps({"error": "topic 不能为空"})
        result = await kb_harvest.harvest(
            settings,
            router_getter(),
            embedder_getter(),
            session_factory,
            topic,
            collection=args.get("collection") or topic,
            max_pages=int(args.get("max_pages") or 3),
        )
        return _dumps(result)

    async def _list(args: dict) -> str:
        collections = await kb.list_collections(session_factory)
        docs = await kb.list_documents(session_factory, args.get("collection") or None, limit=30)
        return _dumps(
            {
                "collections": collections,
                "documents": [
                    {
                        "id": d["id"],
                        "title": d["title"],
                        "collection": d["collection"],
                        "source": d["source_url"],
                        "chunks": d["chunks"],
                        "created_at": d["created_at"][:16],
                    }
                    for d in docs
                ],
            }
        )

    async def _delete(args: dict) -> str:
        doc_id = args.get("document_id")
        if doc_id is None:
            return _dumps({"error": "需要 document_id（可先用 kb_list 查）"})
        return _dumps(await kb.delete_document(session_factory, int(doc_id)))

    return [
        Tool(
            name="kb_search",
            description=(
                "检索你的外挂知识库（自建资料库）。回答专业问题、查自己的资料、"
                "确认某个知识点时优先用它，比联网搜索更准更省。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "要检索的内容"},
                    "collection": {"type": "string", "description": "限定某个知识库（可选）"},
                    "top_k": {"type": "integer", "description": "返回条数，默认 6"},
                },
                "required": ["query"],
            },
            handler=_search,
        ),
        Tool(
            name="kb_add",
            description="把一段知识手动存进外挂知识库（可指定知识库名）。用于用户直接提供的资料。",
            parameters={
                "type": "object",
                "properties": {
                    "content": {"type": "string", "description": "知识正文（Markdown 更佳）"},
                    "title": {"type": "string", "description": "标题"},
                    "collection": {"type": "string", "description": "知识库名，默认 default"},
                    "tags": {"type": "string", "description": "标签，逗号分隔（可选）"},
                    "source_url": {"type": "string", "description": "来源链接（可选）"},
                },
                "required": ["content"],
            },
            handler=_add,
        ),
        Tool(
            name="kb_harvest",
            description=(
                "【自己建知识库】给一个主题，自动上网搜索、筛选可用内容、整理成 Markdown 并存入"
                "一个独立知识库。用户说「帮我建个 XX 的知识库」「去网上找些 XX 的资料整理好」时用它。"
                "耗时约 30~90 秒（取决于 pages 数量）。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "topic": {"type": "string", "description": "主题，例如「ESP32 蓝牙开发」"},
                    "collection": {"type": "string", "description": "知识库名（默认同主题）"},
                    "max_pages": {"type": "integer", "description": "最多采集几篇，默认 3，上限 10"},
                },
                "required": ["topic"],
            },
            handler=_harvest,
        ),
        Tool(
            name="kb_list",
            description="查看外挂知识库里有哪些知识库、每个库里有哪些文档（含 id，便于删除）。",
            parameters={
                "type": "object",
                "properties": {"collection": {"type": "string", "description": "只看某个库（可选）"}},
            },
            handler=_list,
        ),
        Tool(
            name="kb_delete",
            description="从知识库删除某份文档（资料过时/有误时用）。需要 document_id，先用 kb_list 查。",
            parameters={
                "type": "object",
                "properties": {"document_id": {"type": "integer", "description": "文档 id"}},
                "required": ["document_id"],
            },
            handler=_delete,
        ),
    ]
