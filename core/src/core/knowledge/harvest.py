"""全网知识采集：搜索 → 抓取 → 模型筛选/整理 → 切片入库。

对应需求："让贾维斯自己从全网筛选出可用的知识，整合成独立知识库"
每一步都可控：
- 搜索：SearXNG（自建，不依赖第三方搜索 API）
- 抓取：httpx + 正文提取（去脚本/导航/广告等噪音）
- 筛选：便宜档模型判断相关性并整理成 Markdown（无关的直接丢弃，不污染知识库）
- 入库：切片 + 向量化，带上来源链接便于溯源
"""
import asyncio
import json
import logging
import re

import httpx

from ..config import Settings

logger = logging.getLogger("core.knowledge.harvest")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

STRIP_TAGS = ("script", "style", "noscript", "svg", "nav", "header", "footer",
              "aside", "form", "iframe", "button", "select")

FILTER_PROMPT = """你是知识库编辑。下面是一条网页内容，主题是「{topic}」。

请严格判断并处理：
1. 如果这条内容和主题无关、或只有广告/导航/无实质信息，只回复：NONE
2. 如果相关，提取其中关于该主题的**事实性知识**，整理成条理清晰的 Markdown：
   - 保留关键结论、数据、步骤、参数、注意事项
   - 用小标题和要点组织，不要照抄整段原文
   - 不要写"本文""该网页""作者认为"这类话，直接陈述知识本身
   - 不要加入你自己的推测或原文没有的内容
   - 控制在 1500 字以内

网页标题：{title}
网页地址：{url}
网页正文：
{text}
"""


async def searxng_search(settings: Settings, query: str, limit: int = 8) -> list[dict]:
    params = {"q": query, "format": "json", "language": "zh-CN"}
    engines = (getattr(settings, "searxng_engines", "") or "").strip()
    if engines:
        params["engines"] = engines  # 只用国内可达的引擎，避免把时间浪费在超时上
    try:
        async with httpx.AsyncClient(timeout=25) as client:
            resp = await client.get(f"{settings.searxng_url.rstrip('/')}/search", params=params)
            resp.raise_for_status()
            data = resp.json()
    except Exception as e:  # noqa: BLE001
        logger.warning("搜索失败 %s: %s", query, e)
        return []
    out = []
    for r in data.get("results", [])[:limit]:
        url = r.get("url") or ""
        if url.startswith(("http://", "https://")):
            out.append({"title": (r.get("title") or "")[:200], "url": url,
                        "snippet": (r.get("content") or "")[:300]})
    return out


def extract_text(html: str) -> str:
    """从 HTML 提取正文文本（去脚本/样式/导航等噪音）。"""
    html = re.sub(r"(?is)<!--.*?-->", " ", html)
    for tag in STRIP_TAGS:
        html = re.sub(rf"(?is)<{tag}\b.*?</{tag}>", " ", html)
    html = re.sub(r"(?is)<br\s*/?>", "\n", html)
    html = re.sub(r"(?is)</(p|div|li|h[1-6]|tr)>", "\n", html)
    text = re.sub(r"(?is)<[^>]+>", " ", html)
    text = re.sub(r"&nbsp;?", " ", text)
    text = re.sub(r"&(amp|lt|gt|quot|#39);", " ", text)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def _title_of(html: str) -> str:
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    return re.sub(r"\s+", " ", m.group(1)).strip()[:200] if m else ""


async def fetch_page(url: str, max_chars: int = 12000) -> tuple[str, str]:
    """抓取网页，返回 (标题, 正文)。失败返回空串。"""
    try:
        async with httpx.AsyncClient(timeout=25, follow_redirects=True, headers={"User-Agent": UA}) as client:
            resp = await client.get(url)
            ctype = resp.headers.get("content-type", "")
            if "html" not in ctype and "text" not in ctype:
                return "", ""
            html = resp.text
    except Exception as e:  # noqa: BLE001
        logger.info("抓取失败 %s: %s", url, e)
        return "", ""
    return _title_of(html), extract_text(html)[:max_chars]


async def screen_and_clean(router, topic: str, title: str, url: str, text: str, max_chars: int) -> str:
    """用便宜档模型判断相关性并整理成 Markdown；无关返回空串。"""
    if router is None or len(text) < 200:
        return ""
    prompt = FILTER_PROMPT.format(topic=topic, title=title or "(无标题)", url=url,
                                  text=text[:max_chars])
    try:
        result = await router.chat(
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=2000,
            tier="cheap",
        )
    except Exception as e:  # noqa: BLE001
        logger.warning("筛选失败 %s: %s", url, e)
        return ""
    out = (result.content or "").strip()
    if not out or out.upper().startswith("NONE") or len(out) < 150:
        return ""
    return out


async def harvest(
    settings: Settings,
    router,
    embedder,
    session_factory,
    topic: str,
    collection: str | None = None,
    max_pages: int = 5,
    extra_queries: list[str] | None = None,
    progress=None,
) -> dict:
    """采集一个主题的知识并建成知识库。

    progress: 可选回调 async fn(stage: str, detail: str) 用于上报进度。
    """
    from . import store as kb_store

    topic = (topic or "").strip()
    if not topic:
        return {"ok": False, "error": "主题不能为空"}
    collection = kb_store.normalize_collection(collection or topic)
    max_pages = max(1, min(10, int(max_pages or 5)))
    await kb_store.ensure_collection(session_factory, collection, description=f"自动采集：{topic}")

    async def report(stage: str, detail: str = "") -> None:
        if progress is not None:
            try:
                await progress(stage, detail)
            except Exception:  # noqa: BLE001
                pass

    queries = [topic] + [q for q in (extra_queries or []) if q and q != topic]
    await report("search", f"搜索：{' / '.join(queries)}")

    seen_urls: set[str] = set()
    candidates: list[dict] = []
    for q in queries:
        for r in await searxng_search(settings, q, limit=max_pages * 2):
            if r["url"] in seen_urls:
                continue
            seen_urls.add(r["url"])
            candidates.append(r)
        if len(candidates) >= max_pages * 3:
            break

    stats = {
        "ok": True,
        "topic": topic,
        "collection": collection,
        "searched": len(candidates),
        "fetched": 0,
        "skipped_duplicate": 0,
        "skipped_irrelevant": 0,
        "added": [],
        "chars": 0,
    }

    for cand in candidates:
        if len(stats["added"]) >= max_pages:
            break
        url = cand["url"]
        if await kb_store.url_exists(session_factory, url):
            stats["skipped_duplicate"] += 1
            continue

        await report("fetch", url)
        title, text = await fetch_page(url, max_chars=settings.kb_harvest_max_chars)
        if len(text) < 200:  # 正文太短：多半是 JS 渲染页或反爬拦截，跳过
            stats["skipped_irrelevant"] += 1
            continue
        stats["fetched"] += 1

        await report("screen", f"筛选整理：{title or url}")
        cleaned = await screen_and_clean(
            router, topic, title or cand["title"], url, text, settings.kb_harvest_max_chars
        )
        if not cleaned:
            stats["skipped_irrelevant"] += 1
            continue

        doc_title = title or cand["title"] or topic
        marker = f"\n\n> 来源：[{url}]({url})\n"
        try:
            info = await kb_store.add_document(
                session_factory,
                embedder,
                collection,
                doc_title,
                cleaned + marker,
                source_url=url,
                tags=topic,
                origin="harvest",
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("入库失败 %s: %s", url, e)
            continue
        stats["added"].append({"title": doc_title[:80], "url": url, "chunks": info["chunks"]})
        stats["chars"] += info["chars"]
        await report("stored", f"已入库：{doc_title[:40]}")
        await asyncio.sleep(0.3)  # 对目标站点友好一点

    summary = await kb_store.stats(session_factory)
    stats["kb_total"] = summary
    return stats
