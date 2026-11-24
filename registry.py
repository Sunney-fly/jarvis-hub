"""工具注册表：name/描述/JSON Schema/handler 的统一管理。"""
import inspect
import json
from dataclasses import dataclass, field
from typing import Callable

MAX_OUTPUT_CHARS = 8000


@dataclass
class Tool:
    name: str
    description: str
    parameters: dict  # JSON Schema
    handler: Callable[[dict], object]  # 同步或异步函数，返回 str 或可序列化对象
    source: str = "builtin"  # builtin | config | generated | mcp
    extra: dict = field(default_factory=dict)  # 附加信息（如文件路径）


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> bool:
        """注册工具；内置工具同名时拒绝覆盖。返回是否注册成功。"""
        existing = self._tools.get(tool.name)
        if existing is not None and existing.source == "builtin":
            return False
        self._tools[tool.name] = tool
        return True

    def unregister(self, name: str) -> bool:
        return self._tools.pop(name, None) is not None

    def unregister_by_source(self, source: str) -> int:
        names = [n for n, t in self._tools.items() if t.source == source]
        for n in names:
            self._tools.pop(n)
        return len(names)

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def list_tools(self) -> list[Tool]:
        return list(self._tools.values())

    def names(self) -> list[str]:
        return list(self._tools)

    def openai_schemas(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": t.parameters,
                },
            }
            for t in self._tools.values()
        ]

    async def execute(self, name: str, args: dict) -> str:
        tool = self._tools.get(name)
        if tool is None:
            raise KeyError(f"未知工具: {name}")
        output = tool.handler(args)
        if inspect.isawaitable(output):
            output = await output
        if not isinstance(output, str):
            output = json.dumps(output, ensure_ascii=False)
        return output[:MAX_OUTPUT_CHARS]
