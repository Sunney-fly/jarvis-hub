"""Agent 主循环：规划 → 调工具 → 观察 → 继续/收尾。

- 每轮把工具 schema 交给 LLM，LLM 要么发起工具调用，要么给出最终答案。
- 工具结果以 tool 消息回填，循环直到最终答案或达到最大步数。
- 达到最大步数后强制合成收尾答案（不带工具）。
"""
import json
import logging
from dataclasses import dataclass, field

from ..models.provider import ChatResult, ProviderRouter
from ..tools.registry import ToolRegistry
from .prompts import build_system_prompt, sanitize

logger = logging.getLogger("core.agent")


@dataclass
class AgentResult:
    final: ChatResult
    messages: list[dict] = field(default_factory=list)
    usage: dict = field(default_factory=dict)
    steps: int = 0


async def run_agent(
    router: ProviderRouter,
    registry: ToolRegistry,
    client_messages: list[dict],
    memory_facts: list[str] | None = None,
    max_steps: int = 8,
    temperature: float | None = None,
    tz: str = "Asia/Shanghai",
    tier: str = "strong",
) -> AgentResult:
    system_content = build_system_prompt(memory_facts, tz)
    full: list[dict] = [{"role": "system", "content": system_content}] + sanitize(client_messages)
    if len(full) < 2:
        raise ValueError("消息为空，至少需要一条 user 消息")

    tools = registry.openai_schemas() or None
    temp = temperature if temperature is not None else 0.7
    usage: dict = {"provider": None, "model": None, "prompt_tokens": 0, "completion_tokens": 0}

    for step in range(max_steps):
        result = await router.chat(full, tools=tools, temperature=temp, tier=tier)
        usage["provider"] = result.provider
        usage["model"] = result.model
        usage["prompt_tokens"] += result.prompt_tokens
        usage["completion_tokens"] += result.completion_tokens

        if result.tool_calls:
            full.append(
                {
                    "role": "assistant",
                    "content": result.content or "",
                    "tool_calls": result.tool_calls,
                }
            )
            for tc in result.tool_calls:
                fn = tc.get("function") or {}
                name = fn.get("name", "")
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                    if not isinstance(args, dict):
                        args = {}
                except json.JSONDecodeError:
                    args = {}
                try:
                    output = await registry.execute(name, args)
                except Exception as e:  # 工具失败不中断整个循环
                    logger.warning("工具 %s 执行失败: %s", name, e)
                    output = f"工具执行失败：{e!r}"
                full.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.get("id") or f"call-{step}-{name}",
                        "content": str(output),
                    }
                )
            continue

        return AgentResult(final=result, messages=full, usage=usage, steps=step + 1)

    # 达到最大步数：强制收尾
    full.append(
        {
            "role": "system",
            "content": "已达到最大工具调用步数。请基于已获得的信息直接给出最终回答，不要调用工具。",
        }
    )
    result = await router.chat(full, tools=None, temperature=temp, tier=tier)
    usage["provider"] = result.provider
    usage["model"] = result.model
    usage["prompt_tokens"] += result.prompt_tokens
    usage["completion_tokens"] += result.completion_tokens
    return AgentResult(final=result, messages=full, usage=usage, steps=max_steps + 1)
