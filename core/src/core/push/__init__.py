"""统一推送出口（仅 WxPusher）。

业务代码只调用本模块的 push_text / configured，不关心具体通道；
将来若要更换通道，只改这一个文件即可。
"""
import logging

from ..config import Settings
from . import wxpusher

__all__ = ["wxpusher", "configured", "push_text"]

logger = logging.getLogger("core.push")


def configured(settings: Settings) -> bool:
    """是否配置了推送通道"""
    return wxpusher.configured(settings)


async def push_text(settings: Settings, content: str, title: str | None = None) -> bool:
    """发一条微信通知（WxPusher）"""
    return await wxpusher.push_text(settings, content, title)
