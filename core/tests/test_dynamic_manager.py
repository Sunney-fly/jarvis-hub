"""动态工具管理器测试：安装流程（确认/自动）、审批、卸载、生成工具加载、MCP 配置解析。"""
import json

import pytest

from core.config import get_settings
from core.models.provider import ChatResult
from core.tools.dynamic.manager import DynamicToolsManager, load_generated_tools
from core.tools.dynamic.mcp_client import parse_mcp_config
from core.tools.registry import ToolRegistry

SAFE_SPEC = {
    "name": "hello_world",
    "description": "演示工具",
    "parameters": {"type": "object", "properties": {}},
    "code": "import json\nasync def handler(args, ctx):\n    return json.dumps({'msg': 'hello'}, ensure_ascii=False)\n",
    "test_args": {},
}


class FakeRouter:
    """生成 → spec；审查 → review 判定。"""

    def __init__(self, spec=SAFE_SPEC, review=None):
        self.spec = spec
        self.review = review or {"approve": True, "reason": "ok"}

    async def chat(self, messages, tools=None, temperature=0.7, max_tokens=None, tier="strong"):
        content = str(messages[-1]["content"])
        payload = self.review if "安全审查员" in content else self.spec
        return ChatResult(
            content=json.dumps(payload, ensure_ascii=False),
            tool_calls=None,
            provider="fake",
            model="fake",
            prompt_tokens=1,
            completion_tokens=1,
            finish_reason="stop",
        )


def make_manager(tmp_path, spec=SAFE_SPEC, mode="confirm", review=None, review_enabled=False):
    s = get_settings()
    s.tools_d_dir = str(tmp_path)
    s.mcp_enabled = False
    s.tool_install_mode = mode
    s.review_generated_tools = review_enabled
    registry = ToolRegistry()
    manager = DynamicToolsManager(s, registry, lambda: FakeRouter(spec, review))
    return s, registry, manager


async def test_install_confirm_then_approve(tmp_path):
    _s, registry, manager = make_manager(tmp_path)
    await manager.start()
    tool = registry.get("install_tool")
    out = await tool.handler({"requirement": "演示能力"})
    assert "等待批准" in out
    pending = manager.list_pending()
    assert len(pending) == 1 and pending[0]["name"] == "hello_world"
    installed = await manager.approve_pending(pending[0]["id"])
    assert installed.name == "hello_world"
    assert registry.get("hello_world") is not None
    assert manager.list_pending() == []
    await manager.shutdown()


async def test_install_confirm_then_reject(tmp_path):
    _s, registry, manager = make_manager(tmp_path)
    await manager.start()
    tool = registry.get("install_tool")
    await tool.handler({"requirement": "演示能力"})
    pid = manager.list_pending()[0]["id"]
    assert manager.reject_pending(pid) is True
    assert manager.list_pending() == []
    assert registry.get("hello_world") is None
    await manager.shutdown()


async def test_install_auto_mode(tmp_path):
    _s, registry, manager = make_manager(tmp_path, mode="auto")
    await manager.start()
    tool = registry.get("install_tool")
    out = await tool.handler({"requirement": "演示能力"})
    assert "已安装并启用" in out
    assert registry.get("hello_world") is not None
    await manager.shutdown()


async def test_install_rejected_by_gate(tmp_path):
    bad = dict(SAFE_SPEC, code="import os\nasync def handler(args, ctx):\n    return 'x'\n")
    _s, registry, manager = make_manager(tmp_path, spec=bad, mode="auto")
    await manager.start()
    tool = registry.get("install_tool")
    out = await tool.handler({"requirement": "演示能力"})
    assert "安全检查" in out
    assert registry.get("hello_world") is None
    await manager.shutdown()


async def test_install_rejected_by_review(tmp_path):
    _s, registry, manager = make_manager(
        tmp_path, mode="auto", review={"approve": False, "reason": "危险"}, review_enabled=True
    )
    await manager.start()
    tool = registry.get("install_tool")
    out = await tool.handler({"requirement": "演示能力"})
    assert "审查未通过" in out
    await manager.shutdown()


async def test_install_smoke_test_failure_rolls_back(tmp_path):
    bad = dict(SAFE_SPEC, code="async def handler(args, ctx):\n    raise RuntimeError('boom')\n")
    _s, registry, manager = make_manager(tmp_path, spec=bad, mode="auto")
    await manager.start()
    tool = registry.get("install_tool")
    out = await tool.handler({"requirement": "演示能力"})
    assert "安装失败" in out
    assert registry.get("hello_world") is None
    generated = tmp_path / "generated"
    assert not generated.is_dir() or list(generated.glob("*.py")) == []
    await manager.shutdown()


async def test_uninstall(tmp_path):
    _s, registry, manager = make_manager(tmp_path, mode="auto")
    await manager.start()
    tool = registry.get("install_tool")
    await tool.handler({"requirement": "演示能力"})
    result = manager.uninstall("hello_world")
    assert result["uninstalled"] == "hello_world"
    assert registry.get("hello_world") is None
    assert not (tmp_path / "generated").is_dir() or list((tmp_path / "generated").glob("*.py")) == []
    with pytest.raises(PermissionError):
        manager.uninstall("install_tool")  # 内置不可卸载
    with pytest.raises(KeyError):
        manager.uninstall("nope")
    await manager.shutdown()


async def test_load_generated_tools_from_dir(tmp_path):
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "hello_abc12345.py").write_text(
        '"""问候工具"""\n'
        'NAME = "hello"\n'
        'DESCRIPTION = "问候工具"\n'
        'PARAMETERS = {"type": "object", "properties": {}}\n'
        "async def handler(args, ctx):\n"
        "    return '你好'\n",
        encoding="utf-8",
    )
    (generated / "broken_00000000.py").write_text("import os\n", encoding="utf-8")
    tools = load_generated_tools(str(tmp_path), ctx=None)
    names = [t.name for t in tools]
    assert "hello" in names
    out = await tools[0].handler({})
    assert out == "你好"


def test_parse_mcp_config(tmp_path):
    cfg = tmp_path / "mcp.json"
    cfg.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "fs": {"command": "npx", "args": ["-y", "x"], "env": {"K": "V"}},
                    "web": {"url": "https://example.com/mcp"},
                    "noop": {},
                }
            }
        ),
        encoding="utf-8",
    )
    specs = parse_mcp_config(str(cfg))
    assert [s["name"] for s in specs] == ["fs"]
    assert specs[0]["env"] == {"K": "V"}


def test_parse_mcp_config_missing_file(tmp_path):
    assert parse_mcp_config(str(tmp_path / "nope.json")) == []
