"""网页搜索（SearXNG）与网页抓取（httpx）。"""
import json
import re

import httpx

from ...config import Settings
from ..registry import Tool

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


async def _search_web(args: dict, settings: Settings) -> str:
    query = str(args.get("query", "")).strip()
    if not query:
        return json.dumps({"error": "query 不能为空"}, ensure_ascii=False)
    params = {"q": query, "format": "json", "language": "zh-CN"}
    engines = (getattr(settings, "searxng_engines", "") or "").strip()
    if engines:
        params["engines"] = engines  # 只用国内可达的引擎（google/ddg 在这台机器上不通）
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(f"{settings.searxng_url.rstrip('/')}/search", params=params)
        resp.raise_for_status()
        data = resp.json()
    results = data.get("results", [])[:8]
    if not results:
        return json.dumps({"results": [], "note": "无搜索结果"}, ensure_ascii=False)
    return json.dumps(
        {
            "results": [
                {
                    "title": r.get("title"),
                    "url": r.get("url"),
                    "snippet": (r.get("content") or "")[:300],
                }
                for r in results
            ]
        },
        ensure_ascii=False,
    )


def _extract_title(html: str) -> str:
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    return re.sub(r"\s+", " ", m.group(1)).strip()[:200] if m else ""


async def _fetch_url(args: dict, settings: Settings) -> str:
    url = str(args.get("url", "")).strip()
    if not url.startswith(("http://", "https://")):
        return "仅支持 http/https 链接"
    async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers={"User-Agent": UA}) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        html = resp.text
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    text = re.sub(r"(?is)<[^>]+>", " ", html)
    text = re.sub(r"\s+", " ", text).strip()
    return json.dumps(
        {"url": str(resp.url), "title": _extract_title(html), "text": text[:6000]},
        ensure_ascii=False,
    )


def tools(settings: Settings) -> list[Tool]:
    return [
        Tool(
            name="search_web",
            description="网页搜索，返回标题/链接/摘要。需要实时信息、查资料、验证事实时使用。",
            parameters={
                "type": "object",
                "properties": {"query": {"type": "string", "description": "搜索关键词"}},
                "required": ["query"],
            },
            handler=lambda a: _search_web(a, settings),
        ),
        Tool(
            name="fetch_url",
            description="抓取指定网页的正文文本（不执行 JS）。适用于新闻、博客等静态页面。",
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string", "description": "完整 http/https 链接"}},
                "required": ["url"],
            },
            handler=lambda a: _fetch_url(a, settings),
        ),
    ]
