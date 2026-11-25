"""动态工具管理器：装配三层"自己装工具"机制并管理生命周期。

- 配置式：tools.d/*.yaml → 启动/重载时注册
- MCP：mcp.json → 连接服务器并注册其全部工具
- 自我写码：install_tool 元工具 → 生成/审查/冒烟/热注册（confirm 或 auto 模式）

管理操作：reload / approve / reject / uninstall / list。
"""
import asyncio
import hashlib
import importlib.util
import inspect
import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

import httpx

from ...config import Settings
from ..registry import Tool, ToolRegistry
from .config_tools import load_config_tools
from .gate import check_generated_code, validate_spec
from .mcp_client import McpManager
from .selfcoder import ToolContext, generate_spec, review_code

logger = logging.getLogger("core.dynamic")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"


def _load_module(path: Path):
    """按文件名加载 Python 模块（每次唯一模块名，支持热重载）。"""
    module_name = f"jarvis_gen_{hashlib.md5(str(path).encode()).hexdigest()[:10]}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载模块 {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_generated_tools(tools_d_dir: str, ctx: ToolContext) -> list[Tool]:
    """扫描 tools.d/generated/*.py，返回可注册的工具（损坏的跳过并告警）。"""
    d = Path(tools_d_dir) / "generated"
    if not d.is_dir():
        return []
    tools = []
    for f in sorted(d.glob("*.py")):
        try:
            module = _load_module(f)
            handler = getattr(module, "handler", None)
            if not callable(handler):
                raise ValueError("缺少 handler 函数")
            tool = Tool(
                name=str(getattr(module, "NAME", f.stem)),
                description=str(getattr(module, "DESCRIPTION", "")),
                parameters=getattr(module, "PARAMETERS", None) or {"type": "object", "properties": {}},
                handler=lambda a, h=handler: h(a or {}, ctx),
                source="generated",
                extra={"file": str(f)},
            )
            tools.append(tool)
            logger.info("加载生成工具 %s (%s)", tool.name, f.name)
        except Exception as e:
            logger.warning("加载生成工具失败 %s: %s", f, e)
    return tools


class DynamicToolsManager:
    def __init__(self, settings: Settings, registry: ToolRegistry, router_getter):
        self.settings = settings
        self.registry = registry
        self.router_getter = router_getter  # callable -> ProviderRouter
        self.mcp = McpManager(settings.mcp_config, settings.mcp_enabled)
        self._client: httpx.AsyncClient | None = None
        self._ctx: ToolContext | None = None

    # ---------------------------------------------------------- 生命周期
    async def start(self) -> None:
        self._client = httpx.AsyncClient(timeout=30.0, follow_redirects=True, headers={"User-Agent": UA})
        self._ctx = ToolContext(settings=self.settings, http=self._client)
        self.registry.register(self._install_tool())
        await self.reload()
        logger.info("动态工具层已就绪，当前工具 %d 个", len(self.registry.list_tools()))

    async def shutdown(self) -> None:
        await self.mcp.close_all()
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass

    async def reload(self) -> dict:
        """重载全部动态工具：配置 + 生成 + MCP。返回统计。"""
        removed = {s: self.registry.unregister_by_source(s) for s in ("config", "generated", "mcp")}
        count = 0
        for tool in load_config_tools(self.settings.tools_d_dir):
            if self.registry.register(tool):
                count += 1
        for tool in load_generated_tools(self.settings.tools_d_dir, self._ctx):
            if self.registry.register(tool):
                count += 1
        for tool in await self.mcp.connect():
            if self.registry.register(tool):
                count += 1
        result = {"removed": removed, "registered": count, "total": len(self.registry.list_tools())}
        logger.info("动态工具重载完成: %s", result)
        return result

    # ---------------------------------------------------------- 元工具
    def _install_tool(self) -> Tool:
        async def handler(args: dict) -> str:
            requirement = str(args.get("requirement") or "").strip()
            if not requirement:
                return "需要描述新能力需求（requirement），例如：查快递单号物流信息"
            name_hint = str(args.get("name_hint") or "")
            router = self.router_getter()
            if router is None:
                return "当前没有可用的模型，无法生成工具"
            try:
                spec = await generate_spec(router, requirement, name_hint)
            except Exception as e:
                return f"生成工具规格失败：{e!r}"
            problems = validate_spec(spec)
            if problems:
                return "模型输出不合法：" + "；".join(problems)
            gate_problems = check_generated_code(spec["code"])
            if gate_problems:
                return "代码未通过安全检查：" + "；".join(gate_problems)
            if self.settings.review_generated_tools:
                try:
                    ok, reason = await review_code(router, spec["code"])
                except Exception as e:
                    return f"审查调用失败（保守拒绝）：{e!r}"
                if not ok:
                    return f"代码审查未通过：{reason}"
            if self.settings.tool_install_mode == "confirm":
                try:
                    pid = self._save_pending(spec, requirement)
                except Exception as e:
                    return f"保存待批准文件失败：{e!r}"
                return (
                    f"新工具「{spec['name']}」已生成并通过安全检查，等待批准。\n"
                    f"批准：POST /v1/tools/pending/{pid}/approve\n"
                    f"拒绝：DELETE /v1/tools/pending/{pid}"
                )
            try:
                tool = await self.install_spec(spec, requirement)
            except Exception as e:
                return f"安装失败：{e!r}"
            return f"✅ 已安装并启用新工具「{tool.name}」：{tool.description}"

        return Tool(
            name="install_tool",
            description="当用户需要中枢当前不具备的新能力（如查快递、汇率换算、新 API 集成）时，"
            "生成并安装一个新工具。默认生成后需要用户批准才生效。",
            parameters={
                "type": "object",
                "properties": {
                    "requirement": {"type": "string", "description": "需要的新能力，描述越具体越好"},
                    "name_hint": {"type": "string", "description": "建议工具名（snake_case，可选）"},
                },
                "required": ["requirement"],
            },
            handler=handler,
            source="builtin",
        )

    # ---------------------------------------------------------- 自我写码流程
    def _dirs(self) -> tuple[Path, Path]:
        base = Path(self.settings.tools_d_dir)
        return base / "pending", base / "generated"

    def _save_pending(self, spec: dict, requirement: str) -> str:
        pending_dir, _ = self._dirs()
        pending_dir.mkdir(parents=True, exist_ok=True)
        pid = uuid.uuid4().hex[:8]
        doc = {
            "id": pid,
            "spec": spec,
            "requirement": requirement[:500],
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        (pending_dir / f"{pid}.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return pid

    def _load_pending(self, pid: str) -> dict | None:
        pending_dir, _ = self._dirs()
        f = pending_dir / f"{pid}.json"
        if not f.is_file():
            return None
        try:
            return json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            return None

    async def install_spec(self, spec: dict, requirement: str) -> Tool:
        """写入文件 → 加载 → 冒烟测试 → 热注册。失败自动回滚。"""
        name = spec["name"]
        code = spec["code"]
        _, generated_dir = self._dirs()
        generated_dir.mkdir(parents=True, exist_ok=True)
        hash8 = hashlib.md5(code.encode()).hexdigest()[:8]
        path = generated_dir / f"{name}_{hash8}.py"
        header = (
            f'""" {spec.get("description", "")} """\n'
            f"# 自动生成于 {datetime.now(timezone.utc).isoformat()}，需求：{requirement[:100]}\n"
            f'NAME = {json.dumps(name, ensure_ascii=False)}\n'
            f'DESCRIPTION = {json.dumps(spec.get("description", ""), ensure_ascii=False)}\n'
            f"PARAMETERS = {json.dumps(spec.get('parameters'), ensure_ascii=False)}\n\n"
        )
        path.write_text(header + code + "\n", encoding="utf-8")
        try:
            module = _load_module(path)
            handler = getattr(module, "handler", None)
            if not callable(handler):
                raise ValueError("生成的代码缺少 handler 函数")
            await self._smoke_test(handler, spec.get("test_args") or {})
            tool = Tool(
                name=name,
                description=spec.get("description", ""),
                parameters=spec.get("parameters") or {"type": "object", "properties": {}},
                handler=lambda a, h=handler: h(a or {}, self._ctx),
                source="generated",
                extra={"file": str(path)},
            )
            if not self.registry.register(tool):
                path.unlink(missing_ok=True)
                raise RuntimeError(f"工具名 {name} 与内置工具冲突")
            return tool
        except Exception:
            path.unlink(missing_ok=True)  # 回滚
            raise

    async def _smoke_test(self, handler, test_args: dict) -> str:
        try:
            output = handler(test_args, self._ctx)
            if inspect.isawaitable(output):
                output = await asyncio.wait_for(output, timeout=self.settings.tool_code_timeout)
            else:
                output = await asyncio.wait_for(
                    asyncio.to_thread(handler, test_args, self._ctx), timeout=self.settings.tool_code_timeout
                )
        except Exception as e:
            raise RuntimeError(f"冒烟测试失败: {e!r}") from e
        if not isinstance(output, str):
            output = json.dumps(output, ensure_ascii=False)
        logger.info("冒烟测试通过，输出预览: %s", output[:200])
        return output[:200]

    # ---------------------------------------------------------- 管理操作
    def list_pending(self) -> list[dict]:
        pending_dir, _ = self._dirs()
        if not pending_dir.is_dir():
            return []
        out = []
        for f in sorted(pending_dir.glob("*.json")):
            doc = self._load_pending(f.stem)
            if doc:
                out.append(
                    {
                        "id": doc.get("id", f.stem),
                        "name": (doc.get("spec") or {}).get("name"),
                        "description": (doc.get("spec") or {}).get("description"),
                        "requirement": doc.get("requirement"),
                        "created_at": doc.get("created_at"),
                    }
                )
        return out

    async def approve_pending(self, pid: str) -> Tool:
        doc = self._load_pending(pid)
        if doc is None:
            raise FileNotFoundError(f"待批准工具 {pid} 不存在")
        spec = doc.get("spec") or {}
        problems = validate_spec(spec) + check_generated_code(spec.get("code", ""))
        if problems:
            raise ValueError("代码未通过安全检查：" + "；".join(problems))
        tool = await self.install_spec(spec, doc.get("requirement") or "")
        pending_dir, _ = self._dirs()
        (pending_dir / f"{pid}.json").unlink(missing_ok=True)
        return tool

    def reject_pending(self, pid: str) -> bool:
        pending_dir, _ = self._dirs()
        f = pending_dir / f"{pid}.json"
        if not f.is_file():
            return False
        f.unlink()
        return True

    def uninstall(self, name: str) -> dict:
        tool = self.registry.get(name)
        if tool is None:
            raise KeyError(f"工具 {name} 不存在")
        if tool.source == "builtin":
            raise PermissionError("内置工具不可卸载")
        file_path = tool.extra.get("file")
        if file_path and tool.source == "generated":
            Path(file_path).unlink(missing_ok=True)
        self.registry.unregister(name)
        note = ""
        if tool.source in ("config", "mcp"):
            note = f"（{tool.source} 工具重启或 reload 后会重新加载；config 请删除对应 yaml，mcp 请修改 mcp.json）"
        return {"uninstalled": name, "source": tool.source, "note": note}
