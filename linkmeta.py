"""Извлечение заголовка/описания контента по ссылке для улучшенной категоризации.

Поддерживает:
- YouTube: oEmbed API (https://www.youtube.com/oembed?url=...&format=json) без ключа.
- Прочие ссылки: парсинг og:title / <title> из HTML страницы.
"""

import asyncio
import html
import json
import logging
import re

import aiohttp
from urllib.parse import quote

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
    url = f"https://www.youtube.com/oembed?url={quote(video_url, safe='')}&format=json"
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
    oembed_url = f"{endpoint}?url={quote(url, safe='')}&format=json"
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


def _skip_url(url: str) -> bool:
    """Ссылки, по которым ходить бессмысленно: Telegram (приглашения/боты дают
    «Join group chat on Telegram»), Instagram/Twitch (oEmbed без токена мёртв)."""
    return bool(re.search(r"(^|//)(www\.)?(t\.me|telegram\.me|instagram\.com|twitch\.tv)/|^tg://", url or "", re.I))


async def fetch_link_meta(urls: list[str], max_links: int = 2) -> list[str]:
    """Заголовок/автор/описание по ссылкам → строки для категоризатора. Не бросает."""
    targets = [u for u in urls if u and not _skip_url(u)][:max_links]
    if not targets:
        return []
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
    lines = []
    try:
        async with aiohttp.ClientSession(connector=connector) as session:
            for url in targets:
                try:
                    meta = await _oembed_for_url(session, url)
                except Exception:
                    meta = {}
                title = html.unescape(meta.get("title", "")).strip()
                if not title or re.search(r"^(youtube|just a moment|access denied|attention required)", title, re.I):
                    continue
                author = html.unescape(meta.get("author", "")).strip()
                desc = html.unescape(meta.get("description", "")).strip()
                line = f"По ссылке: «{title}»" + (f" ({author})" if author else "")
                if desc:
                    line += ". " + desc[:400]
                lines.append(line)
    except Exception:
        log.info("fetch_link_meta: сбой", exc_info=True)
    return lines


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


# ── Текст статьи по ссылке ───────────────────────────────────────────────────
# Забираем один раз читаемый текст страницы: поиск находит пост по содержимому статьи,
# а текст остаётся, даже если сайт её удалит. Без ИИ — бесплатно.

from html.parser import HTMLParser  # noqa: E402

ARTICLE_MIN_CHARS = 300
_ARTICLE_MAX_BYTES = 1_500_000
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s)\]>\"'«»]+", re.IGNORECASE)
_NO_ARTICLE_RE = re.compile(
    r"(^|//)(www\.|m\.)?(t\.me|telegram\.me|instagram\.com|twitch\.tv|youtube\.com|youtu\.be|tiktok\.com|"
    r"x\.com|twitter\.com|vk\.com/(video|clip)|music\.yandex|open\.spotify|soundcloud\.com|"
    r"apps\.apple\.com|play\.google\.com|discord\.(gg|com)|"
    # магазины и объявления: там нет статьи, только карточка товара
    r"avito\.ru|go\.joom\.ru|joom\.(ru|com)|ozon\.ru|wildberries\.ru|aliexpress\.(ru|com)|"
    r"market\.yandex\.ru|megamarket\.ru|boosty\.to|max\.ru)(/|$)|^tg://", re.I)
_FILE_EXT_RE = re.compile(r"\.(jpe?g|png|gif|webp|mp4|mov|webm|mp3|pdf|zip|rar|7z|apk|exe)(\?|$)", re.I)


def article_url(text: str) -> str | None:
    """Первая ссылка из поста, по которой есть смысл забирать статью."""
    body = (text or "").split("\n\n〔", 1)[0]
    for m in _URL_IN_TEXT_RE.finditer(body):
        url = m.group(0).rstrip(".,;:!?…")
        if not _NO_ARTICLE_RE.search(url) and not _FILE_EXT_RE.search(url):
            return url
    return None


class _Readable(HTMLParser):
    _SKIP = {"script", "style", "noscript", "svg", "nav", "header", "footer", "aside", "form", "button",
             "iframe", "select", "template", "figcaption"}
    _BLOCK = {"p", "h1", "h2", "h3", "h4", "li", "blockquote", "pre", "dd", "td"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.article = 0
        self.main = 0
        self.buf: list[str] = []
        self.blocks: list[tuple[int, str, bool]] = []   # (2 — в <article>, 1 — в <main>, текст, заголовок)
        self.title = ""
        self._in_title = False
        self._cur_tag = ""

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self.skip += 1
        elif tag == "article":
            self.article += 1
        elif tag == "main":
            self.main += 1
        elif tag == "title":
            self._in_title = True
        elif tag == "br":
            self.buf.append("\n")
        if tag in self._BLOCK:
            self._flush()
            self._cur_tag = tag

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in ("article", "main"):
            self._flush()
            if tag == "article":
                self.article = max(0, self.article - 1)
            else:
                self.main = max(0, self.main - 1)
        elif tag == "title":
            self._in_title = False
        if tag in self._BLOCK:
            self._flush()

    def handle_data(self, data):
        if self._in_title and not self.title:
            self.title = data.strip()
        if not self.skip and self._cur_tag:
            self.buf.append(data)

    def _flush(self):
        txt = re.sub(r"[ \t\r\f\v]+", " ", "".join(self.buf)).strip()
        txt = re.sub(r"\s*\n\s*", "\n", txt)
        if txt:
            head = self._cur_tag in ("h1", "h2", "h3", "h4")
            if head or len(txt) >= 25:
                self.blocks.append((2 if self.article else 1 if self.main else 0, txt, head))
        self.buf = []
        self._cur_tag = ""


def extract_article(raw_html: str) -> tuple[str, str]:
    """(заголовок, читаемый текст) из HTML. Сначала то, что внутри <article>/<main>."""
    p = _Readable()
    try:
        p.feed(raw_html)
        p.close()
    except Exception:
        pass
    p._flush()
    og = _extract_og(raw_html)
    title = html.unescape(og.get("title") or p.title or "").strip()
    # сначала <article> (README на GitHub, текст статьи), потом <main>, потом вся страница
    use = p.blocks
    for lvl in (2, 1):
        inner = [b for b in p.blocks if b[0] >= lvl]
        if sum(len(b[1]) for b in inner if not b[2]) >= ARTICLE_MIN_CHARS:
            use = inner
            break
    out, seen = [], set()
    for _, txt, head in use:
        if txt in seen:
            continue
        seen.add(txt)
        out.append(txt)
    text = "\n\n".join(out).strip()
    if title and text.startswith(title):
        text = text[len(title):].lstrip()
    return title[:300], text


def _decode(body: bytes, charset: str | None) -> str:
    for enc in (charset, "utf-8"):
        if not enc:
            continue
        try:
            return body.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    m = re.search(rb"charset=[\"']?([\w-]+)", body[:4000], re.I)
    if m:
        try:
            return body.decode(m.group(1).decode(), errors="replace")
        except LookupError:
            pass
    return body.decode("cp1251", errors="replace")


async def fetch_article(url: str) -> dict:
    """{status: ok|fail, url, title, text}. Только публичные адреса (не сеть сервера),
    не больше 1,5 МБ, редиректы проверяем вручную. Не бросает."""
    from urllib.parse import urljoin, urlsplit

    from savelinks import _host_is_public

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
    headers = {"User-Agent": "Mozilla/5.0 (compatible; tg-saver-bot; +article-reader)",
               "Accept": "text/html,application/xhtml+xml", "Accept-Language": "ru,en;q=0.8"}
    cur = url
    try:
        async with aiohttp.ClientSession(connector=connector, timeout=aiohttp.ClientTimeout(total=30)) as s:
            for _ in range(4):
                parts = urlsplit(cur)
                if parts.scheme not in ("http", "https") or not await _host_is_public(parts.hostname or ""):
                    return {"status": "fail", "url": url, "title": "", "text": ""}
                async with s.get(cur, headers=headers, allow_redirects=False) as resp:
                    if resp.status in (301, 302, 303, 307, 308) and resp.headers.get("Location"):
                        cur = urljoin(cur, resp.headers["Location"])
                        continue
                    ctype = resp.headers.get("Content-Type", "").lower()
                    if resp.status != 200 or ("html" not in ctype and ctype):
                        return {"status": "fail", "url": cur, "title": "", "text": ""}
                    chunks, size = [], 0
                    async for chunk in resp.content.iter_chunked(65536):   # read(n) отдаёт лишь первый кусок
                        chunks.append(chunk)
                        size += len(chunk)
                        if size >= _ARTICLE_MAX_BYTES:
                            break
                    body = b"".join(chunks)
                    title, text = extract_article(_decode(body, resp.charset))
                    ok = len(text) >= ARTICLE_MIN_CHARS
                    return {"status": "ok" if ok else "fail", "url": cur, "title": title, "text": text if ok else ""}
    except Exception as e:
        log.info("fetch_article %s: %s %s", url[:80], type(e).__name__, e)
    return {"status": "fail", "url": url, "title": "", "text": ""}
