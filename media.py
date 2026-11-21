"""Работа с медиа для распознавания: кадр из видео и звуковая дорожка через ffmpeg.

ffmpeg есть в Docker-образе (Render) и обычно стоит локально. Если его нет —
функции тихо возвращают None, и бот работает по превью-кадру от Telegram.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile

logger = logging.getLogger(__name__)

FFMPEG = shutil.which("ffmpeg")

# Видео больше этого не качаем целиком (RAM/диск free-тарифа Render): только превью.
VIDEO_DOWNLOAD_MAX = 60 * 1024 * 1024
# Сколько секунд звука отдавать в Whisper — для понимания темы хватает.
AUDIO_SECONDS = 240


async def _run(args: list[str], timeout: float = 90) -> bytes | None:
    if not FFMPEG:
        return None
    try:
        proc = await asyncio.create_subprocess_exec(
            FFMPEG, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
        if proc.returncode != 0:
            logger.info("ffmpeg %s → код %s: %.200s", args[:4], proc.returncode, (err or b"").decode("utf-8", "replace"))
            return None
        return out or None
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        logger.info("ffmpeg: таймаут")
        return None
    except Exception as e:
        logger.info("ffmpeg не запустился: %s", e)
        return None


async def extract_frame(path: str, duration: float | None = None) -> bytes | None:
    """JPEG-кадр из видео: из первой трети ролика (заставки в начале бесполезны)."""
    at = 1.0
    if duration and duration > 3:
        at = min(max(duration / 3, 1.0), 60.0)
    return await _run([
        "-hide_banner", "-loglevel", "error", "-ss", f"{at:.1f}", "-i", path,
        "-frames:v", "1", "-vf", "scale='min(1024,iw)':-2", "-f", "image2", "-vcodec", "mjpeg", "pipe:1",
    ])


async def extract_audio(path: str) -> bytes | None:
    """Звуковая дорожка в компактном mp3 (моно, 16 кГц): влезает в лимит Groq 25 МБ."""
    return await _run([
        "-hide_banner", "-loglevel", "error", "-i", path, "-vn", "-ac", "1", "-ar", "16000",
        "-b:a", "32k", "-t", str(AUDIO_SECONDS), "-f", "mp3", "pipe:1",
    ], timeout=120)


class TempFile:
    """Временный файл, который гарантированно удаляется."""

    def __init__(self, suffix: str = ".mp4"):
        fd, self.path = tempfile.mkstemp(suffix=suffix, prefix="tgsaver_")
        os.close(fd)

    def __enter__(self):
        return self.path

    def __exit__(self, *exc):
        try:
            os.remove(self.path)
        except OSError:
            pass
