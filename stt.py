import aiohttp
import asyncio
import logging

from config import (
    GROQ_API_KEY,
    GROQ_BASE_URL,
    GROQ_STT_MODEL,
    GROQ_PROXY,
    GROQ_TIMEOUT_SEC,
)

logger = logging.getLogger(__name__)

_LOCK = asyncio.Lock()


def _connector():
    """Коннектор с учётом прокси (socks5/http) или None для прямого соединения."""
    if not GROQ_PROXY:
        return None
    if GROQ_PROXY.startswith("socks"):
        from aiohttp_socks import ProxyConnector

        return ProxyConnector.from_url(GROQ_PROXY)
    return aiohttp.ProxyConnector.from_url(GROQ_PROXY)


async def transcribe_audio(audio_bytes: bytes, language: str = "ru") -> str:
    """Транскрибация голосового/аудио через Groq whisper. '' при ошибке."""
    if not audio_bytes:
        return ""
    async with _LOCK:
        try:
            form = aiohttp.FormData()
            form.add_field("model", GROQ_STT_MODEL)
            form.add_field("language", language)
            form.add_field("file", audio_bytes, filename="voice.ogg", content_type="application/octet-stream")

            headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
            connector = _connector()
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.post(
                    f"{GROQ_BASE_URL}/audio/transcriptions",
                    data=form,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as resp:
                    if resp.status != 200:
                        logger.warning("Groq STT вернул статус %s", resp.status)
                        return ""
                    data = await resp.json()
                    return (data.get("text") or "").strip()[:500]
        except Exception as e:
            logger.warning("Транскрибация не удалась: %s", e)
            return ""