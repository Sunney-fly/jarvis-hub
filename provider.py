"""模型路由：双档（强/便宜）自动选择 + 档内主备故障切换 + Embedding 客户端。

- ProviderRouter.chat/stream(tier=...)：按档位选择模型；档内依次尝试，
  失败自动切换下一个；所选档全部失败时用另一档兜底。
- tier="strong"：主模型档（deepseek/qwen/glm 按 PRIMARY_MODEL 顺序）
- tier="cheap"：便宜档（如 SiliconFlow 免费模型），用于简单对话、
  记忆抽取、代码审查等省 token 场景
- EmbeddingClient：向量化（SiliconFlow BGE-M3）。
"""
import logging
from dataclasses import dataclass
from typing import AsyncIterator

from openai import APIConnectionError, APIStatusError, APITimeoutError, AsyncOpenAI

logger = logging.getLogger("core.provider")


@dataclass
class ChatResult:
    content: str | None
    tool_calls: list[dict] | None
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    finish_reason: str | None
    tier: str = "strong"


class ProviderRouter:
    def __init__(self, specs: list[dict], timeout: float = 120.0, client_factory=None, cheap_specs: list[dict] | None = None):
        """client_factory(spec, timeout) -> openai 兼容客户端（测试注入用）。"""
        factory = client_factory or self._default_client
        self.providers = self._build(specs, factory, timeout)
        self.cheap_providers = self._build(cheap_specs or [], factory, timeout)
        if not self.providers and not self.cheap_providers:
            raise RuntimeError("没有可用的模型 provider，请检查 .env 中的 API Key")
        self.has_cheap_tier = bool(self.cheap_providers)

    @staticmethod
    def _build(specs: list[dict], factory, timeout: float) -> list[dict]:
        out = []
        for spec in specs:
            out.append(
                {
                    "name": spec["name"],
                    "model": spec["model"],
                    "client": factory(spec, timeout),
                }
            )
        return out

    @staticmethod
    def _default_client(spec: dict, timeout: float):
        return AsyncOpenAI(
            base_url=spec["base_url"],
            api_key=spec["api_key"],
            timeout=timeout,
            max_retries=0,  # 重试交给 failover 层，快速切换
        )

    def _order_for(self, tier: str) -> tuple[list[dict], list[dict]]:
        """返回 (首选档, 兜底档)。"""
        if tier == "cheap" and self.cheap_providers:
            return self.cheap_providers, self.providers
        return self.providers, self.cheap_providers

    def _first_error(self, errors: list[str], exc: Exception) -> RuntimeError:
        return RuntimeError(f"所有模型 provider 均失败: {'; '.join(errors)} | 最后异常: {exc!r}")

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        temperature: float = 0.7,
        max_tokens: int | None = None,
        tier: str = "strong",
    ) -> ChatResult:
        primary, backup = self._order_for(tier)
        errors: list[str] = []
        last_exc: Exception | None = None
        for p in primary + backup:
            try:
                kwargs: dict = dict(model=p["model"], messages=messages, temperature=temperature)
                if tools:
                    kwargs["tools"] = tools
                if max_tokens:
                    kwargs["max_tokens"] = max_tokens
                resp = await p["client"].chat.completions.create(**kwargs)
                choice = resp.choices[0]
                msg = choice.message
                return ChatResult(
                    content=msg.content,
                    tool_calls=[tc.model_dump() for tc in msg.tool_calls] if msg.tool_calls else None,
                    provider=p["name"],
                    model=p["model"],
                    prompt_tokens=(resp.usage.prompt_tokens if resp.usage else 0) or 0,
                    completion_tokens=(resp.usage.completion_tokens if resp.usage else 0) or 0,
                    finish_reason=choice.finish_reason,
                    tier="cheap" if p in self.cheap_providers else "strong",
                )
            except (APIConnectionError, APITimeoutError, APIStatusError) as e:
                code = getattr(e, "status_code", "")
                errors.append(f"{p['name']}:{type(e).__name__}:{code}")
                last_exc = e
                logger.warning("provider %s 调用失败，切换下一个: %s", p["name"], e)
        raise self._first_error(errors, last_exc or RuntimeError("unknown"))

    async def stream(
        self, messages: list[dict], temperature: float = 0.7, tier: str = "strong"
    ) -> AsyncIterator[dict]:
        """流式输出最终答案（不带工具）。yield {"delta": str, "provider": str, "model": str}"""
        primary, backup = self._order_for(tier)
        errors: list[str] = []
        for p in primary + backup:
            try:
                stream = await p["client"].chat.completions.create(
                    model=p["model"], messages=messages, temperature=temperature, stream=True
                )
                async for chunk in stream:
                    if not chunk.choices:
                        continue
                    delta = chunk.choices[0].delta
                    if delta and delta.content:
                        yield {"delta": delta.content, "provider": p["name"], "model": p["model"]}
                return
            except (APIConnectionError, APITimeoutError, APIStatusError) as e:
                errors.append(f"{p['name']}:{type(e).__name__}")
                logger.warning("provider %s 流式调用失败: %s", p["name"], e)
        raise RuntimeError(f"所有 provider 流式输出失败: {'; '.join(errors)}")


class EmbeddingClient:
    def __init__(self, base_url: str, api_key: str, model: str):
        self.model = model
        self.client = AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=60, max_retries=1)

    async def embed(self, texts: list[str]) -> tuple[list[list[float]], int]:
        resp = await self.client.embeddings.create(model=self.model, input=texts)
        usage = (resp.usage.prompt_tokens if resp.usage else 0) or 0
        return [d.embedding for d in resp.data], usage
