"""API 接口测试：健康检查/鉴权/模型列表/对话（FakeRouter 注入）。"""
import pytest
import httpx

from core.main import app
from core.models.provider import ChatResult


class FakeRouter:
    providers = [{"name": "fake", "model": "fake-1"}]
    cheap_providers = []

    async def chat(self, messages, tools=None, temperature=0.7, max_tokens=None, tier="strong"):
        return ChatResult(
            content="你好，我是中枢。",
            tool_calls=None,
            provider="fake",
            model="fake-1",
            prompt_tokens=10,
            completion_tokens=20,
            finish_reason="stop",
        )


@pytest.fixture(scope="session")
async def client():
    async with app.router.lifespan_context(app):
        app.state.router = FakeRouter()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as c:
            yield c


async def test_healthz(client):
    resp = await client.get("/healthz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] in ("ok", "degraded")
    assert "providers" in data


async def test_models_requires_auth(client):
    resp = await client.get("/v1/models")
    assert resp.status_code == 401
    resp = await client.get("/v1/models", headers={"Authorization": "Bearer test-token"})
    assert resp.status_code == 200
    assert resp.json()["object"] == "list"


async def test_chat_basic(client):
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "deepseek", "messages": [{"role": "user", "content": "你好"}]},
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["choices"][0]["message"]["content"] == "你好，我是中枢。"
    assert data["usage"]["total_tokens"] == 30


async def test_chat_stream(client):
    resp = await client.post(
        "/v1/chat/completions",
        json={
            "model": "deepseek",
            "messages": [{"role": "user", "content": "你好"}],
            "stream": True,
        },
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status_code == 200
    body = resp.text
    assert "data: [DONE]" in body
    # 流式回退路径：FakeRouter 无 stream 方法 → 回退输出已生成的答案
    assert "你好，我是中枢。" in body


async def test_chat_empty_messages(client):
    resp = await client.post(
        "/v1/chat/completions",
        json={"model": "deepseek", "messages": []},
        headers={"Authorization": "Bearer test-token"},
    )
    assert resp.status_code == 400


async def test_chat_unauthorized(client):
    resp = await client.post(
        "/v1/chat/completions",
        json={"messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 401


async def test_tools_list(client):
    resp = await client.get("/v1/tools", headers={"Authorization": "Bearer test-token"})
    assert resp.status_code == 200
    data = resp.json()
    names = [t["name"] for t in data["tools"]]
    assert "install_tool" in names
    assert "search_web" in names
    assert data["count"] == len(data["tools"])


async def test_uninstall_builtin_forbidden(client):
    resp = await client.delete(
        "/v1/tools/search_web", headers={"Authorization": "Bearer test-token"}
    )
    assert resp.status_code == 403
