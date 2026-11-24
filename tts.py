"""语音合成：MiniMax（自然）或 Edge-TTS（免费）。"""
import base64
import io

import httpx

from ..config import Settings


async def synthesize(settings: Settings, text: str) -> bytes:
    text = text.strip()
    if not text:
        return b""
    if settings.tts_provider == "minimax":
        return await _minimax(settings, text)
    return await _edge(settings, text)


async def _minimax(settings: Settings, text: str) -> bytes:
    if not (settings.minimax_api_key and settings.minimax_group_id):
        raise RuntimeError("未配置 MiniMax（MINIMAX_API_KEY/MINIMAX_GROUP_ID）")
    url = f"https://api.minimax.chat/v1/t2a_v2?GroupId={settings.minimax_group_id}"
    headers = {
        "Authorization": f"Bearer {settings.minimax_api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": "speech-02-hd",
        "text": text,
        "stream": False,
        "voice_setting": {
            "voice_id": settings.minimax_voice_id,
            "speed": 1.0,
            "vol": 1.0,
            "pitch": 0,
        },
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    base_resp = data.get("base_resp") or {}
    if base_resp.get("status_code") not in (0, None):
        raise RuntimeError(f"MiniMax 返回错误: {base_resp}")
    audio_b64 = (data.get("data") or {}).get("audio")
    if not audio_b64:
        raise RuntimeError("MiniMax 未返回音频数据")
    return base64.b64decode(audio_b64)


async def _edge(settings: Settings, text: str) -> bytes:
    import edge_tts  # 延迟导入

    communicate = edge_tts.Communicate(text, settings.edge_voice)
    buf = io.BytesIO()
    async for chunk in communicate.stream():
        if chunk.get("type") == "audio":
            buf.write(chunk["data"])
    return buf.getvalue()
