"""🎬 Видео YouTube из расширения «Сводка комментариев YouTube» → пост в архиве.

Только разбор запроса, текст поста и обложка — без Telegram и ИИ. Сохранение
(категория ИИ, папки, кнопки, бэкап) идёт обычным путём бота: bot.save_video().
"""

import asyncio
import logging
import re

logger = logging.getLogger(__name__)

MAX_BODY = 16 * 1024          # байт в теле запроса
TITLE_MAX = 300
CHANNEL_MAX = 120
TLDR_MAX = 3000               # в архив (подпись в Telegram короче — см. CAPTION_MAX)
TLDR_FOR_AI = 1200            # в подсказку ИИ — бережём суточный запас Groq
CAPTION_MAX = 1024            # лимит подписи к фото в Telegram
THUMB_MAX_BYTES = 2 * 1024 * 1024

_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁦-⁩]")


class BadRequest(ValueError):
    """Запрос не годится — текст уходит клиенту в поле error."""


def _clean_line(v, limit: int) -> str:
    s = _CTRL_RE.sub("", str(v or ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit].rstrip()


def _clean_text(v, limit: int) -> str:
    s = _CTRL_RE.sub("", str(v or "")).replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


def parse_request(body) -> dict:
    """Проверяет тело запроса. Возвращает {video_id, title, channel, seconds, tldr}."""
    if not isinstance(body, dict):
        raise BadRequest("json object expected")
    vid = str(body.get("videoId") or "").strip()
    if not _ID_RE.fullmatch(vid):
        raise BadRequest("invalid videoId")
    title = _clean_line(body.get("title"), TITLE_MAX)
    channel = _clean_line(body.get("channel"), CHANNEL_MAX)
    try:
        seconds = int(float(body.get("len") or 0))
    except (TypeError, ValueError):
        seconds = 0
    if seconds < 0 or seconds > 60 * 60 * 200:
        seconds = 0
    tldr = _clean_text(body.get("tldr"), TLDR_MAX)
    return {"video_id": vid, "title": title, "channel": channel, "seconds": seconds, "tldr": tldr}


def video_url(video_id: str) -> str:
    return f"https://youtu.be/{video_id}"


def link_fragments(video_id: str) -> list[str]:
    """Как ссылка на это видео может выглядеть в тексте поста (для поиска повторов)."""
    return [f"youtu.be/{video_id}", f"watch?v={video_id}", f"shorts/{video_id}",
            f"live/{video_id}", f"embed/{video_id}"]


def fmt_duration(seconds: int) -> str:
    if not seconds:
        return ""
    h, rest = divmod(int(seconds), 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _head(v: dict) -> list[str]:
    lines = [f"▶️ {v['title'] or 'Видео YouTube'}"]
    meta = " · ".join(x for x in (v["channel"], fmt_duration(v["seconds"])) if x)
    if meta:
        lines.append(f"📺 {meta}")
    lines.append(video_url(v["video_id"]))
    return lines


def post_text(v: dict, limit: int | None = None) -> str:
    """Текст поста: название, канал · длительность, ссылка и (если есть) пересказ.
    limit — урезать пересказ, чтобы всё влезло (подпись к фото ≤ 1024)."""
    head = "\n".join(_head(v))
    tldr = v["tldr"]
    if not tldr:
        return head
    prefix = f"{head}\n\n📝 О чём видео:\n"
    if limit is not None and len(prefix) + len(tldr) > limit:
        room = limit - len(prefix) - 1
        if room < 40:
            return head[:limit]
        tldr = tldr[:room].rstrip() + "…"
    return prefix + tldr


def llm_text(v: dict) -> str:
    """Подсказка ИИ для выбора категории — тот же categorize(), что у постов."""
    parts = ["Видео YouTube."]
    if v["title"]:
        parts.append(f"Название: {v['title']}")
    if v["channel"]:
        parts.append(f"Канал: {v['channel']}")
    if v["seconds"]:
        parts.append(f"Длительность: {fmt_duration(v['seconds'])}")
    if v["tldr"]:
        t = v["tldr"]
        if len(t) > TLDR_FOR_AI:
            t = t[:TLDR_FOR_AI].rstrip() + "…"
        parts.append(f"О чём видео: {t}")
    return "\n".join(parts)


async def read_limited(stream, limit: int) -> bytes | None:
    """Весь поток, но не больше limit байт (иначе None). StreamReader.read(n)
    отдаёт лишь то, что уже пришло, поэтому читаем кусками до конца."""
    buf = bytearray()
    async for chunk in stream.iter_chunked(64 * 1024):
        buf += chunk
        if len(buf) > limit:
            return None
    return bytes(buf)


THUMB_NAMES = ("maxresdefault.jpg", "sddefault.jpg", "hqdefault.jpg")


async def fetch_thumb(video_id: str, timeout: float = 10) -> bytes | None:
    """Обложка с i.ytimg.com (адрес строим сами по videoId — чужие URL не качаем).
    maxres есть не у всех видео: YouTube отдаёт тогда 404 или серую заглушку 120×90."""
    try:
        import aiohttp
    except ImportError:
        return None
    try:
        tmo = aiohttp.ClientTimeout(total=timeout)
        async with aiohttp.ClientSession(timeout=tmo) as session:
            for name in THUMB_NAMES:
                url = f"https://i.ytimg.com/vi/{video_id}/{name}"
                try:
                    async with session.get(url) as r:
                        if r.status != 200:
                            continue
                        data = await read_limited(r.content, THUMB_MAX_BYTES)
                        if data and len(data) > 2000:
                            return data
                except (aiohttp.ClientError, asyncio.TimeoutError):
                    continue
    except Exception:
        logger.info("Обложка %s не скачалась", video_id, exc_info=True)
    return None
