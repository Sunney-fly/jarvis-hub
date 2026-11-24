"""视觉分析模块：把图片交给多模态大模型，返回文字结论。

使用 SiliconFlow 的 Qwen3-VL 系列（与文本弱档共用同一个 API key）。
实测：Qwen/Qwen3-VL-32B-Instruct 单次约 3.7 秒，720p JPEG 约 13KB。
"""
import base64
import logging
import time

import httpx

from .config import Settings

logger = logging.getLogger("core.vision")

API = "https://api.siliconflow.cn/v1/chat/completions"

DEFAULT_PROMPT = (
    "你是贾维斯的视觉模块。简洁描述这张图片里的关键信息，"
    "并给出对用户有用的结论或建议。用中文，不超过 150 字，不要客套话。"
)


class VisionResult:
    """一次视觉分析的结果"""

    def __init__(self, text: str, model: str, latency_ms: int,
                 prompt_tokens: int = 0, completion_tokens: int = 0) -> None:
        self.text = text
        self.model = model
        self.latency_ms = latency_ms
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens

    def as_dict(self) -> dict:
        return {
            "text": self.text,
            "model": self.model,
            "latency_ms": self.latency_ms,
            "usage": {
                "prompt_tokens": self.prompt_tokens,
                "completion_tokens": self.completion_tokens,
                "total_tokens": self.prompt_tokens + self.completion_tokens,
            },
        }


def configured(settings: Settings) -> bool:
    return bool(settings.siliconflow_api_key)


async def analyze_image(settings: Settings, image_bytes: bytes,
                        mime: str = "image/jpeg", prompt: str | None = None,
                        max_tokens: int = 500) -> VisionResult:
    """把图片交给视觉模型分析，返回文字结论"""
    key = (settings.siliconflow_api_key or "").strip()
    if not key:
        raise RuntimeError("未配置 SILICONFLOW_API_KEY，无法使用视觉功能")
    model = settings.vision_model or "Qwen/Qwen3-VL-32B-Instruct"
    b64 = base64.b64encode(image_bytes).decode()
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt or DEFAULT_PROMPT},
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                ],
            }
        ],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }
    t0 = time.time()
    async with httpx.AsyncClient(timeout=180) as client:
        resp = await client.post(
            API, headers={"Authorization": f"Bearer {key}"}, json=payload
        )
        resp.raise_for_status()
        data = resp.json()
    latency_ms = int((time.time() - t0) * 1000)
    text = (data["choices"][0]["message"]["content"] or "").strip()
    usage = data.get("usage") or {}
    logger.info(
        "视觉分析完成 model=%s 图片=%.0fKB 耗时=%dms tokens=%s",
        data.get("model", model), len(image_bytes) / 1024, latency_ms,
        usage.get("total_tokens"),
    )
    return VisionResult(
        text=text,
        model=data.get("model", model),
        latency_ms=latency_ms,
        prompt_tokens=usage.get("prompt_tokens") or 0,
        completion_tokens=usage.get("completion_tokens") or 0,
    )
