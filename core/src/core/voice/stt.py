"""语音转写（SiliconFlow FunASR / SenseVoice，中文识别强）。"""
import httpx

from ..config import Settings


async def transcribe(settings: Settings, filename: str, data: bytes) -> str:
    if settings.stt_provider != "siliconflow":
        raise RuntimeError(f"不支持的 STT provider: {settings.stt_provider}")
    if not settings.siliconflow_api_key:
        raise RuntimeError("未配置 SILICONFLOW_API_KEY，无法转写")
    headers = {"Authorization": f"Bearer {settings.siliconflow_api_key}"}
    files = {"file": (filename or "audio.mp3", data, "application/octet-stream")}
    form = {"model": settings.stt_model}
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(
            f"{settings.siliconflow_base_url}/audio/transcriptions",
            headers=headers,
            data=form,
            files=files,
        )
        resp.raise_for_status()
        payload = resp.json()
    text = payload.get("text") or ""
    if not text:
        results = payload.get("results") or []
        if results:
            text = results[0].get("text") or ""
    return text
