"""工具注册表测试。"""
import pytest

from core.tools.registry import MAX_OUTPUT_CHARS, Tool, ToolRegistry


def make_registry():
    reg = ToolRegistry()
    reg.register(
        Tool(
            name="echo",
            description="回声",
            parameters={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
            handler=lambda a: f"got {a['text']}",
        )
    )
    return reg


async def test_execute():
    reg = make_registry()
    assert await reg.execute("echo", {"text": "hi"}) == "got hi"


async def test_unknown_tool():
    reg = make_registry()
    with pytest.raises(KeyError):
        await reg.execute("nope", {})


def test_openai_schemas():
    reg = make_registry()
    schemas = reg.openai_schemas()
    assert schemas[0]["type"] == "function"
    assert schemas[0]["function"]["name"] == "echo"
    assert schemas[0]["function"]["parameters"]["required"] == ["text"]


async def test_output_truncation():
    reg = ToolRegistry()
    reg.register(
        Tool(
            name="big",
            description="大输出",
            parameters={"type": "object", "properties": {}},
            handler=lambda a: "x" * (MAX_OUTPUT_CHARS + 500),
        )
    )
    out = await reg.execute("big", {})
    assert len(out) == MAX_OUTPUT_CHARS
