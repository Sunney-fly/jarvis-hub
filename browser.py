"""浏览器工具：调用 Playwright 服务渲染 JS 页面后抓取。"""
import json

import httpx

from ...config import Settings
from ..registry import Tool


async def _browse_web(args: dict, settings: Settings) -> str:
    url = str(args.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return "仅支持 http/https 链接"
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.get(f"{settings.browser_url.rstrip('/')}/fetch", params={"url": url})
        resp.raise_for_status()
        data = resp.json()
    return json.dumps(
        {"title": data.get("title"), "text": (data.get("text") or "")[:6000]},
        ensure_ascii=False,
    )


def tools(settings: Settings) -> list[Tool]:
    return [
        Tool(
            name="browse_web",
            description="用无头浏览器渲染网页后抓取内容（支持 JS 页面）。fetch_url 抓不到时用它。",
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string", "description": "完整 http/https 链接"}},
                "required": ["url"],
            },
            handler=lambda a: _browse_web(a, settings),
        ),
    ]
