"""MCP（Model Context Protocol）客户端：读 mcp.json，stdio 连接各服务器并注册其全部工具。

mcp.json 格式（标准格式）：
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/data"],
      "env": {}
    }
  }
}
当前版本支持 stdio transport（最通用）；http transport 的服务器暂跳过。
"""
import asyncio
import json
import logging
import os
from contextlib import AsyncExitStack
from pathlib import Path

from ..registry import Tool

logger = logging.getLogger("core.dynamic.mcp")

CALL_TIMEOUT = 60.0
CONNECT_TIMEOUT = 30.0


def parse_mcp_config(path: str) -> list[dict]:
    """读取 mcp.json → [{"name","command","args","env"}]。文件不存在返回 []。"""
    p = Path(path)
    if not p.is_file():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning("mcp.json 解析失败: %s", e)
        return []
    servers = data.get("mcpServers") or {}
    out = []
    for name, cfg in servers.items():
        if not isinstance(cfg, dict):
            continue
        if cfg.get("url"):
            logger.warning("MCP server %s 使用 http transport，当前仅支持 stdio，跳过", name)
            continue
        if not cfg.get("command"):
            logger.warning("MCP server %s 缺少 command，跳过", name)
            continue
        out.append(
            {
                "name": str(name),
                "command": str(cfg["command"]),
                "args": [str(a) for a in (cfg.get("args") or [])],
                "env": {str(k): str(v) for k, v in (cfg.get("env") or {}).items()},
            }
        )
    return out


class McpServer:
    """一个已连接的 MCP server：持有 session 与生命周期栈。"""

    def __init__(self, name: str, session, stack: AsyncExitStack):
        self.name = name
        self.session = session
        self._stack = stack

    async def call(self, tool_name: str, args: dict) -> str:
        from mcp import ClientSession  # noqa: F401（类型说明用）

        result = await asyncio.wait_for(
            self.session.call_tool(tool_name, arguments=args or {}), timeout=CALL_TIMEOUT
        )
        parts = []
        for item in result.content or []:
            if getattr(item, "type", None) == "text":
                parts.append(str(getattr(item, "text", "")))
            else:
                parts.append(str(getattr(item, "data", item))[:500])
        text = "\n".join(parts).strip()
        return text[:8000] or "(无输出)"

    async def close(self) -> None:
        try:
            await self._stack.aclose()
        except Exception:
            pass


class McpManager:
    def __init__(self, config_path: str, enabled: bool = True):
        self.config_path = config_path
        self.enabled = enabled
        self.servers: dict[str, McpServer] = {}

    async def connect(self) -> list[Tool]:
        """（重）连接全部配置的服务器，返回它们提供的工具。"""
        if not self.enabled:
            return []
        tools: list[Tool] = []
        for spec in parse_mcp_config(self.config_path):
            old = self.servers.pop(spec["name"], None)
            if old is not None:
                await old.close()
            server, server_tools = await self._connect_one(spec)
            if server is None:
                continue
            self.servers[spec["name"]] = server
            tools.extend(server_tools)
            logger.info("MCP server %s 已连接，提供 %d 个工具", spec["name"], len(server_tools))
        return tools

    async def _connect_one(self, spec: dict) -> tuple[McpServer | None, list[Tool]]:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        params = StdioServerParameters(
            command=spec["command"],
            args=spec["args"],
            env={**os.environ, **(spec["env"] or {})},
        )
        stack = AsyncExitStack()
        try:
            read, write = await stack.enter_async_context(stdio_client(params))
            session = await stack.enter_async_context(ClientSession(read, write))
            await asyncio.wait_for(session.initialize(), timeout=CONNECT_TIMEOUT)
            listed = await asyncio.wait_for(session.list_tools(), timeout=CONNECT_TIMEOUT)
        except Exception as e:
            await stack.aclose()
            logger.warning("MCP server %s 连接失败（继续运行其它服务）: %s", spec["name"], e)
            return None, []
        server = McpServer(spec["name"], session, stack)
        tools = [
            Tool(
                name=t.name,
                description=t.description or "",
                parameters=t.inputSchema or {"type": "object", "properties": {}},
                handler=lambda a, s=server, n=t.name: s.call(n, a),
                source="mcp",
                extra={"server": spec["name"]},
            )
            for t in listed.tools
        ]
        return server, tools

    async def close_all(self) -> None:
        for name, server in list(self.servers.items()):
            await server.close()
            logger.info("MCP server %s 已断开", name)
        self.servers.clear()
