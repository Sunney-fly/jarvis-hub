"""便签（存在 Postgres notes 表）。"""
import json

from sqlalchemy import select

from ...config import Settings
from ...db import Note
from ..registry import Tool


async def _add_note(args: dict, session_factory) -> str:
    title = str(args.get("title") or "").strip()
    content = str(args.get("content") or "").strip()
    if not title:
        return "标题不能为空"
    async with session_factory() as s:
        s.add(Note(title=title[:256], content=content))
        await s.commit()
    return f"已保存便签「{title}」"


async def _list_notes(session_factory) -> str:
    async with session_factory() as s:
        rows = (await s.execute(select(Note).order_by(Note.updated_at.desc()).limit(20))).scalars().all()
    return json.dumps(
        [
            {
                "id": n.id,
                "title": n.title,
                "content": n.content[:200],
                "updated_at": n.updated_at.isoformat() if n.updated_at else None,
            }
            for n in rows
        ],
        ensure_ascii=False,
    )


def tools(settings: Settings, session_factory) -> list[Tool]:
    return [
        Tool(
            name="add_note",
            description="保存一条便签（跨设备可查）。适合记录灵感、清单、待办。",
            parameters={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "标题"},
                    "content": {"type": "string", "description": "内容"},
                },
                "required": ["title", "content"],
            },
            handler=lambda a: _add_note(a, session_factory),
        ),
        Tool(
            name="list_notes",
            description="列出最近的便签（最多 20 条）。",
            parameters={"type": "object", "properties": {}},
            handler=lambda a: _list_notes(session_factory),
        ),
    ]
