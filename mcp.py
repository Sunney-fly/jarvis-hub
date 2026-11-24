"""MCP over HTTP —— 给 Grok Bot 等外部 agent 调的工具端点。

复用 relay 的内存队列：这里投进去的任务，电脑端（jarvis-bridge）用
/v1/relay/pull 一样能领，不会出现两套队列。

挂载点：POST /v1/mcp（见 main.py 末尾），复用现有反代与 require_auth。
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Optional

from .relay import RelayHub

PROTOCOL = "2024-11-05"

TOOLS = [
    {
        "name": "bridge_status",
        "description": "查看任务桥状态：电脑端是否在线、积压数、队列实现。",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    {
        "name": "list_tasks",
        "description": "列出任务。status 取 pending / running / done / all。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "default": "pending"},
                "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 100},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "submit_task",
        "description": "派任务给电脑端（本机 DSH）执行。title 必填，detail 放上下文与验收标准。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "detail": {"type": "string", "default": ""},
                "priority": {"type": "string", "enum": ["high", "normal", "low"], "default": "normal"},
                "target": {"type": "string", "enum": ["desktop", "cloud", "ecs"], "default": "desktop"},
            },
            "required": ["title"],
            "additionalProperties": False,
        },
    },
    {
        "name": "task_result",
        "description": "查单个任务的状态与结果。",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
]


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _status(job) -> str:
    if job.result is not None:
        return "done"
    if job.claimed_at is not None:
        return "running"
    return "pending"


def _view(job) -> dict[str, Any]:
    p = job.payload or {}
    title = p.get("title") or (p.get("text") or "")[:80]
    return {
        "id": job.id,
        "title": title,
        "target": p.get("target", "desktop"),
        "priority": p.get("priority", "normal"),
        "status": _status(job),
        "created": datetime.fromtimestamp(job.created_at).astimezone().isoformat(timespec="seconds"),
    }


def _ok(obj: Any) -> dict:
    import json

    return {"content": [{"type": "text", "text": json.dumps(obj, ensure_ascii=False)}], "isError": False}


def _err(msg: str) -> dict:
    import json

    return {"content": [{"type": "text", "text": json.dumps({"ok": False, "error": msg}, ensure_ascii=False)}], "isError": True}


async def _call(name: str, args: dict, hub: RelayHub) -> dict:
    if name == "bridge_status":
        st = hub.stats()
        return _ok({"ok": True, **st, "now": _now()})

    if name == "list_tasks":
        status = str(args.get("status", "pending")).strip().lower()
        limit = max(1, min(int(args.get("limit", 20) or 20), 100))
        jobs = list(getattr(hub, "_jobs", {}).values())
        if status != "all":
            jobs = [j for j in jobs if _status(j) == status]
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return _ok(
            {
                "ok": True,
                "count": len(jobs[:limit]),
                "total": len(jobs),
                "tasks": [_view(j) for j in jobs[:limit]],
            }
        )

    if name == "submit_task":
        title = str(args.get("title", "")).strip()
        if not title:
            return _err("title 不能为空")
        detail = str(args.get("detail", ""))
        target = str(args.get("target", "desktop")).strip().lower()
        if target not in {"desktop", "cloud", "ecs"}:
            return _err("target 只能是 desktop / cloud / ecs")
        priority = str(args.get("priority", "normal")).strip().lower()
        if priority not in {"high", "normal", "low"}:
            priority = "normal"
        payload = {
            # 兼容现有 relay 电脑端（它读 kind/text）
            "kind": "text",
            "text": (title + ("\n\n" + detail if detail else "")).strip(),
            "image_b64": "",
            "speak_on": "phone",
            "title": title[:200],
            "detail": detail[:4000],
            "priority": priority,
            "target": target,
        }
        job = await hub.submit(payload)
        return _ok({"ok": True, "id": job.id, "status": "pending", "target": target})

    if name == "task_result":
        tid = str(args.get("task_id", "")).strip()
        if not tid:
            return _err("task_id 不能为空")
        job = getattr(hub, "_jobs", {}).get(tid)
        if job is None:
            return _err("not found: " + tid)
        return _ok({"ok": True, "task": {**_view(job), "result": job.result or {}}})

    return _err("unknown tool: " + name)


async def handle(msg: dict, hub: RelayHub) -> Optional[dict]:
    """处理一条 JSON-RPC 消息；notification 返回 None。"""
    method = msg.get("method")
    msg_id = msg.get("id")

    if method == "initialize":
        return {
            "jsonrpc": "2.0",
            "id": msg_id,
            "result": {
                "protocolVersion": PROTOCOL,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "jarvis-core-bridge", "version": "0.1.0"},
            },
        }
    if method in ("notifications/initialized", "notifications/cancelled"):
        return None
    if method == "ping":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": msg_id, "result": {"tools": TOOLS}}
    if method == "tools/call":
        params = msg.get("params") or {}
        result = await _call(params.get("name", ""), params.get("arguments") or {}, hub)
        return {"jsonrpc": "2.0", "id": msg_id, "result": result}
    if method in ("resources/list", "prompts/list"):
        key = "resources" if method.startswith("resources") else "prompts"
        return {"jsonrpc": "2.0", "id": msg_id, "result": {key: []}}

    return {
        "jsonrpc": "2.0",
        "id": msg_id,
        "error": {"code": -32601, "message": "method not found: " + str(method)},
    }
