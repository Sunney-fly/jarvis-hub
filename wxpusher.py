"""微信推送（WxPusher）：主动通知的唯一出口。"""
import logging

import httpx

from ..config import Settings

logger = logging.getLogger("core.push")

API = "https://wxpusher.zjiecode.com/api/send/message"


def configured(settings: Settings) -> bool:
    return bool(settings.wxpusher_app_token and settings.wxpusher_uids)


async def push_text(settings: Settings, content: str, title: str | None = None) -> bool:
    if not configured(settings):
        logger.warning("未配置 WxPusher，跳过推送")
        return False
    uids = [u.strip() for u in settings.wxpusher_uids.split(",") if u.strip()]
    payload = {
        "appToken": settings.wxpusher_app_token,
        "content": content[:4000],
        "contentType": 1,  # 文本
        "uids": uids,
    }
    if title:
        payload["summary"] = title[:64]
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(API, json=payload)
            resp.raise_for_status()
            data = resp.json()
        if data.get("success"):
            return True
        logger.error("WxPusher 返回失败: %s", data)
        return False
    except Exception as e:
        logger.error("WxPusher 推送异常: %s", e)
        return False
