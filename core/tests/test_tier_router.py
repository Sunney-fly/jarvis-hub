"""双档路由（强档/便宜档）测试：档位选择 + 互备回退。"""
from types import SimpleNamespace

from core.models.provider import ProviderRouter


def fake_response(content="hi"):
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                finish_reason="stop",
                message=SimpleNamespace(content=content, tool_calls=None),
            )
        ],
        usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )


class RecordingClient:
    """记录被调用并返回预设响应。"""

    def __init__(self, name, content=None):
        self.name = name
        self.content = content
        self.called = 0

    @property
    def chat(self):
        return self

    @property
    def completions(self):
        return self

    async def create(self, **kwargs):
        self.called += 1
        if isinstance(self.content, Exception):
            raise self.content
        return fake_response(f"from-{self.name}")


def make_router(clients: dict[str, RecordingClient]):
    def factory(spec, timeout):
        return clients[spec["name"]]

    return ProviderRouter(
        [
            {"name": "strong-a", "base_url": "https://a/v1", "api_key": "k", "model": "m-a"},
            {"name": "strong-b", "base_url": "https://b/v1", "api_key": "k", "model": "m-b"},
        ],
        client_factory=factory,
        cheap_specs=[{"name": "cheap-c", "base_url": "https://c/v1", "api_key": "k", "model": "m-c"}],
    )


async def test_cheap_tier_uses_cheap_provider():
    strong = RecordingClient("strong-a")
    cheap = RecordingClient("cheap-c")
    router = make_router({"strong-a": strong, "strong-b": RecordingClient("strong-b"), "cheap-c": cheap})
    assert router.has_cheap_tier is True
    result = await router.chat([{"role": "user", "content": "hi"}], tier="cheap")
    assert result.provider == "cheap-c"
    assert result.tier == "cheap"
    assert cheap.called == 1 and strong.called == 0


async def test_strong_tier_uses_strong_provider():
    strong = RecordingClient("strong-a")
    cheap = RecordingClient("cheap-c")
    router = make_router({"strong-a": strong, "strong-b": RecordingClient("strong-b"), "cheap-c": cheap})
    result = await router.chat([{"role": "user", "content": "hi"}], tier="strong")
    assert result.provider == "strong-a"
    assert result.tier == "strong"
    assert strong.called == 1 and cheap.called == 0


async def test_cheap_failure_falls_back_to_strong():
    from openai import APIConnectionError

    import httpx

    strong = RecordingClient("strong-a")
    cheap = RecordingClient("cheap-c", content=APIConnectionError(request=httpx.Request("POST", "https://c")))
    router = make_router({"strong-a": strong, "strong-b": RecordingClient("strong-b"), "cheap-c": cheap})
    result = await router.chat([{"role": "user", "content": "hi"}], tier="cheap")
    assert result.provider == "strong-a"
    assert cheap.called == 1 and strong.called == 1


async def test_strong_failure_falls_back_to_cheap():
    from openai import APIConnectionError

    import httpx

    err = APIConnectionError(request=httpx.Request("POST", "https://x"))
    strong = RecordingClient("strong-a", content=err)
    cheap = RecordingClient("cheap-c")
    router = make_router({"strong-a": strong, "strong-b": RecordingClient("strong-b", content=err), "cheap-c": cheap})
    result = await router.chat([{"role": "user", "content": "hi"}], tier="strong")
    assert result.provider == "cheap-c"


async def test_no_cheap_tier_degrades_to_strong():
    strong = RecordingClient("strong-a")
    router = make_router({"strong-a": strong, "strong-b": RecordingClient("strong-b"), "cheap-c": RecordingClient("cheap-c")})
    router.cheap_providers = []  # 模拟未配置便宜档
    router.has_cheap_tier = False
    result = await router.chat([{"role": "user", "content": "hi"}], tier="cheap")
    assert result.provider == "strong-a"
