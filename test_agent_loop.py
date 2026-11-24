"""Agent 主循环测试（FakeRouter，不依赖网络）。"""
from core.agent.loop import run_agent
from core.models.provider import ChatResult
from core.tools.registry import Tool, ToolRegistry


class FakeRouter:
    """按脚本顺序返回结果；记录每次调用是否带 tools。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    async def chat(self, messages, tools=None, temperature=0.7, max_tokens=None, tier="strong"):
        self.calls.append({"with_tools": tools is not None, "messages": list(messages), "tier": tier})
        if not self.script:
            return ChatResult(
                content="（无脚本）", tool_calls=None, provider="fake", model="fake-1",
                prompt_tokens=10, completion_tokens=20, finish_reason="stop",
            )
        resp = self.script.pop(0)
        return ChatResult(
            content=resp.get("content"),
            tool_calls=resp.get("tool_calls"),
            provider="fake",
            model="fake-1",
            prompt_tokens=10,
            completion_tokens=20,
            finish_reason="stop",
        )


def make_registry():
    calls = []

    async def echo(args):
        calls.append(args)
        return f"echo:{args['text']}"

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
            handler=echo,
        )
    )
    return reg, calls


async def test_tool_loop_then_final():
    router = FakeRouter(
        [
            {
                "tool_calls": [
                    {"id": "c1", "function": {"name": "echo", "arguments": '{"text": "x"}'}}
                ]
            },
            {"content": "最终答案"},
        ]
    )
    reg, calls = make_registry()
    result = await run_agent(router, reg, [{"role": "user", "content": "hi"}], max_steps=4)

    assert result.final.content == "最终答案"
    assert calls == [{"text": "x"}]
    assert result.steps == 2
    assert result.usage["prompt_tokens"] == 20  # 两轮各 10
    assert result.usage["completion_tokens"] == 40
    # 第一轮带工具，第二轮（最终答案）也带工具 schema
    assert router.calls[0]["with_tools"] is True
    assert router.calls[1]["with_tools"] is True


async def test_max_steps_synthesis():
    router = FakeRouter(
        [
            {"tool_calls": [{"id": f"c{i}", "function": {"name": "echo", "arguments": '{"text": "x"}'}}]}
            for i in range(3)
        ]
        + [{"content": "收尾答案"}]
    )
    reg, calls = make_registry()
    result = await run_agent(router, reg, [{"role": "user", "content": "hi"}], max_steps=3)

    assert len(calls) == 3  # echo 执行了 3 次
    assert result.final.content == "收尾答案"
    # 第 4 次调用是收尾合成，不带工具
    assert router.calls[3]["with_tools"] is False


async def test_memory_injection_and_time():
    router = FakeRouter([{"content": "ok"}])
    reg, _ = make_registry()
    result = await run_agent(
        router, reg, [{"role": "user", "content": "hi"}],
        memory_facts=["用户喜欢喝咖啡"],
        tz="Asia/Shanghai",
    )
    system = result.messages[0]["content"]
    assert "用户喜欢喝咖啡" in system
    assert "当前时间" in system
    assert "202" in system  # 年份


async def test_tool_error_not_fatal():
    router = FakeRouter(
        [
            {"tool_calls": [{"id": "c1", "function": {"name": "boom", "arguments": "{}"}}]},
            {"content": "恢复"},
        ]
    )
    reg = ToolRegistry()
    reg.register(
        Tool(
            name="boom",
            description="会炸的工具",
            parameters={"type": "object", "properties": {}},
            handler=lambda a: (_ for _ in ()).throw(RuntimeError("kaboom")),
        )
    )
    result = await run_agent(router, reg, [{"role": "user", "content": "hi"}], max_steps=4)
    assert result.final.content == "恢复"


async def test_empty_messages_raise():
    router = FakeRouter([{"content": "ok"}])
    reg, _ = make_registry()
    import pytest

    with pytest.raises(ValueError):
        await run_agent(router, reg, [], max_steps=2)
