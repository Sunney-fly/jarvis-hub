"""模型路由故障切换测试（注入假客户端，不碰网络）。"""
from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError, APIStatusError

from core.models.provider import ProviderRouter


def fake_response(content: str = "hi"):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content=content, tool_calls=None),
            )
        ],
        usage=SimpleNamespace(prompt_tokens=5, completion_tokens=2, total_tokens=7),
    )


class FakeClient:
    """按脚本顺序：返回响应对象或抛出异常。"""

    def __init__(self, script):
        self.script = list(script)

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    async def create(self, **kwargs):
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def status_error_500() -> APIStatusError:
    request = httpx.Request("POST", "https://api.a.com/v1/chat/completions")
    return APIStatusError("server error", response=httpx.Response(500, request=request), body=None)


def make_router(scripts: dict[str, list]):
    def factory(spec, timeout):
        return FakeClient(scripts[spec["name"]])

    return ProviderRouter(
        [
            {"name": "a", "base_url": "https://api.a.com/v1", "api_key": "k1", "model": "m1"},
            {"name": "b", "base_url": "https://api.b.com/v1", "api_key": "k2", "model": "m2"},
        ],
        client_factory=factory,
    )


async def test_failover_on_500():
    router = make_router({"a": [status_error_500()], "b": [fake_response()]})
    result = await router.chat([{"role": "user", "content": "hi"}])
    assert result.provider == "b"
    assert result.content == "hi"
    assert result.prompt_tokens == 5


async def test_failover_on_connection_error():
    router = make_router({"a": [APIConnectionError(request=httpx.Request("POST", "https://api.a.com"))], "b": [fake_response()]})
    result = await router.chat([{"role": "user", "content": "hi"}])
    assert result.provider == "b"


async def test_failover_skips_healthy_order():
    # a 正常时不触碰 b
    router = make_router({"a": [fake_response("from-a")], "b": []})
    result = await router.chat([{"role": "user", "content": "hi"}])
    assert result.provider == "a"
    assert result.content == "from-a"


async def test_all_fail_raises():
    router = make_router({"a": [status_error_500()], "b": [APIConnectionError(request=httpx.Request("POST", "https://api.b.com"))]})
    with pytest.raises(RuntimeError, match="均失败"):
        await router.chat([{"role": "user", "content": "hi"}])


def test_empty_providers_raise():
    with pytest.raises(RuntimeError, match="没有可用的模型"):
        ProviderRouter([])
