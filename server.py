"""Playwright 渲染服务：抓取 JS 渲染页面文本 / 截图。

仅被 core 在 compose 内网调用，不暴露宿主机端口。
"""
import asyncio
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from playwright.async_api import async_playwright

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

_playwright = None
_browser = None
_lock = asyncio.Lock()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _playwright, _browser
    _playwright = await async_playwright().start()
    _browser = await _playwright.chromium.launch(
        headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"]
    )
    yield
    try:
        await _browser.close()
    except Exception:
        pass
    try:
        await _playwright.stop()
    except Exception:
        pass


app = FastAPI(title="jarvis-browser", lifespan=lifespan)


def _check_url(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(400, "仅支持 http/https 链接")
    return url


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "browser": _browser is not None}


@app.get("/fetch")
async def fetch(url: str):
    url = _check_url(url)
    async with _lock:
        page = await _browser.new_page(user_agent=UA)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(1500)
            title = await page.title()
            text = await page.evaluate("document.body ? document.body.innerText : ''")
            return {"title": title, "text": (text or "")[:10000]}
        except Exception as e:
            raise HTTPException(502, f"渲染失败: {e!r}")
        finally:
            await page.close()


@app.get("/screenshot")
async def screenshot(url: str):
    url = _check_url(url)
    async with _lock:
        page = await _browser.new_page(user_agent=UA, viewport={"width": 1280, "height": 800})
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            await page.wait_for_timeout(1500)
            png = await page.screenshot(type="png", full_page=False)
            return Response(content=png, media_type="image/png")
        except Exception as e:
            raise HTTPException(502, f"截图失败: {e!r}")
        finally:
            await page.close()
