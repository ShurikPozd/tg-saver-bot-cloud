"""Извлечение заголовка/описания контента по ссылке для улучшенной категоризации.

Поддерживает:
- YouTube: oEmbed API (https://www.youtube.com/oembed?url=...&format=json) без ключа.
- Прочие ссылки: парсинг og:title / <title> из HTML страницы.
"""

import asyncio
import json
import logging
import re

import aiohttp

from config import GROQ_PROXY

log = logging.getLogger(__name__)

_OEMBED_PROVIDERS = {
    "youtube": "https://www.youtube.com/oembed",
    "youtu.be": "https://www.youtube.com/oembed",
    "vimeo": "https://vimeo.com/api/oembed.json",
    "dailymotion": "https://www.dailymotion.com/services/oembed",
    "twitch": "https://api.twitch.tv/v5/oembed",
    "tiktok": "https://www.tiktok.com/oembed",
    "instagram": "https://graph.facebook.com/v18.0/instagram_oembed",
    "flickr": "https://www.flickr.com/services/oembed",
}

_YOUTUBE_RE = re.compile(
    r"(?:https?://)?(?:www\.)?"
    r"(?:youtu\.be/|youtube\.com/(?:watch\?v=|shorts/|live/|embed/))([\w-]{11})",
    re.IGNORECASE,
)

_GENERIC_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)

_TIMEOUT = aiohttp.ClientTimeout(total=8)

_TITLE_TAG_RE = re.compile(r"<meta[^>]+property=[\"']og:title[\"'][^>]+content=[\"']([^\"']+)[\"']", re.IGNORECASE)
_TITLE_TAG_RE2 = re.compile(r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+property=[\"']og:title[\"']", re.IGNORECASE)
_DESC_TAG_RE = re.compile(r"<meta[^>]+property=[\"']og:description[\"'][^>]+content=[\"']([^\"']+)[\"']", re.IGNORECASE)
_DESC_TAG_RE2 = re.compile(r"<meta[^>]+content=[\"']([^\"']+)[\"'][^>]+property=[\"']og:description[\"']", re.IGNORECASE)
_HTML_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def extract_youtube_id(text: str) -> str | None:
    m = _YOUTUBE_RE.search(text or "")
    return m.group(1) if m else None


def extract_first_url(text: str) -> str | None:
    m = _GENERIC_URL_RE.search(text or "")
    return m.group(0) if m else None


async def _fetch(session: aiohttp.ClientSession, url: str, headers: dict | None = None) -> str:
    async def _get() -> str:
        async with session.get(url, headers=headers or {}, timeout=_TIMEOUT) as resp:
            if resp.status != 200:
                return ""
            return await resp.text()

    try:
        return await asyncio.wait_for(_get(), timeout=12)
    except Exception:
        return ""


def _extract_og(raw: str) -> dict:
    out = {}
    for rx in (_TITLE_TAG_RE, _TITLE_TAG_RE2):
        m = rx.search(raw)
        if m:
            v = m.group(1).strip()
            if v:
                out["title"] = v
                break
    for rx in (_DESC_TAG_RE, _DESC_TAG_RE2):
        m = rx.search(raw)
        if m:
            v = re.sub(r"\s+", " ", m.group(1)).strip()
            if v:
                out["description"] = v
                break
    if "title" not in out:
        m = _HTML_TITLE_RE.search(raw)
        if m:
            v = re.sub(r"\s+", " ", m.group(1)).strip()
            if v:
                out["title"] = v
    return out


async def _youtube_oembed(session: aiohttp.ClientSession, video_id: str, video_url: str) -> dict:
    url = f"https://www.youtube.com/oembed?url={video_url}&format=json"
    try:
        raw = await _fetch(session, url)
    except Exception:
        raw = ""
    result = {}
    if raw:
        try:
            data = json.loads(raw)
            title = (data.get("title") or "").strip()
            author = (data.get("author_name") or "").strip()
            if title:
                result["title"] = title
            if author:
                result["author"] = author
        except Exception:
            pass
    try:
        page = await _fetch(
            session,
            f"https://www.youtube.com/watch?v={video_id}",
            headers={"User-Agent": "Mozilla/5.0 (compatible; TelegramBot/1.0)"},
        )
        og = _extract_og(page)
        if og.get("description"):
            result["description"] = og["description"]
        if not result.get("title") and og.get("title"):
            result["title"] = og["title"]
    except Exception:
        pass
    return result


async def _og_meta(session: aiohttp.ClientSession, url: str) -> dict:
    headers = {"User-Agent": "Mozilla/5.0 (compatible; TelegramBot/1.0)"}
    try:
        raw = await _fetch(session, url, headers=headers)
    except Exception:
        return {}
    if not raw:
        return {}
    return _extract_og(raw)


async def _generic_oembed(session: aiohttp.ClientSession, provider: str, url: str) -> dict:
    endpoint = _OEMBED_PROVIDERS.get(provider)
    if not endpoint:
        return {}
    oembed_url = f"{endpoint}?url={url}&format=json"
    try:
        raw = await _fetch(session, oembed_url)
    except Exception:
        return {}
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except Exception:
        return {}
    title = (data.get("title") or "").strip()
    author = (data.get("author_name") or "").strip()
    result = {}
    if title:
        result["title"] = title
    if author:
        result["author"] = author
    return result


def _detect_provider(url: str) -> str | None:
    url = (url or "").lower()
    for p in _OEMBED_PROVIDERS:
        if p in url:
            return p
    return None


async def _oembed_for_url(session: aiohttp.ClientSession, url: str) -> dict:
    video_id = extract_youtube_id(url)
    if video_id:
        return await _youtube_oembed(session, video_id, url)
    provider = _detect_provider(url)
    if provider:
        return await _generic_oembed(session, provider, url)
    return await _og_meta(session, url)


async def enrich_links(text: str, max_links: int = 2) -> tuple[str, list[dict]]:
    """Пытается получить название/автора по ссылкам в тексте.

    Возвращает (новый_текст, список метаданных). Не бросает исключений.
    """
    if not text:
        return text, []
    urls = re.findall(_GENERIC_URL_RE, text)
    if not urls:
        return text, []
    metas = []
    connector = None
    if GROQ_PROXY:
        try:
            if GROQ_PROXY.startswith("socks"):
                from aiohttp_socks import ProxyConnector

                connector = ProxyConnector.from_url(GROQ_PROXY)
            else:
                connector = aiohttp.ProxyConnector.from_url(GROQ_PROXY)
        except Exception:
            connector = None
    async with aiohttp.ClientSession(connector=connector) as session:
        for url in urls[:max_links]:
            meta = await _oembed_for_url(session, url)
            if meta.get("title"):
                metas.append({"url": url, **meta})
    if not metas:
        return text, []
    lines = []
    for m in metas:
        title = m.get("title", "").strip()
        author = m.get("author", "").strip()
        desc = m.get("description", "").strip()
        if title and author:
            lines.append(f"«{title}» ({author})")
        elif title:
            lines.append(f"«{title}»")
        else:
            lines.append("ссылка")
        if desc:
            lines.append("Описание: " + desc[:600])
    if not lines:
        return text, []
    new_text = text.strip()
    new_text += "\n\n(по ссылке: " + "; ".join(lines) + ")"
    return new_text, metas
