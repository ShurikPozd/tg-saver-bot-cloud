"""🔗 Ссылки из расширения «Сборщик вкладок» → посты в архиве.

Только разбор запроса, тексты постов, поиск повторов по ссылке и загрузка картинки —
без Telegram и ИИ. Сохранение идёт обычным путём бота: bot.save_links().

Контракт (общий с расширением): POST /api/save-links, тело ≤ 64 КБ,
{"mode": "each"|"list", "title"?, "batchKey"?, "items": [{url, site, kind, kindLabel, icon,
title, meta, description, image?}]}. batchKey (только list, необязательно) — ключ повтора:
тот же список, присланный ещё раз (браузер упал после ответа), не даёт второго поста.
Текст ссылки:
    {icon} {kindLabel} · {title}
    {meta}
    {description}      ← в списке только ~140 символов
    {url}
"""

import asyncio
import ipaddress
import logging
import re
import socket
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urljoin

from ytvideo import BadRequest, read_limited
from ytvideo import _clean_line as _yt_clean_line, _clean_text as _yt_clean_text

logger = logging.getLogger(__name__)

MAX_BODY = 64 * 1024
EACH_MAX = 10                 # mode=each: ссылок за запрос (расширение шлёт пачками)
LIST_MAX = 100                # mode=list: ссылок в одном списке
URL_MAX = 2048
SITE_MAX = 100
KIND_MAX = 32
KIND_LABEL_MAX = 40
ICON_MAX = 8
TITLE_MAX = 300
META_MAX = 300
DESC_MAX = 600
LIST_TITLE_MAX = 120
LIST_DESC = 140               # описание в посте-списке
MSG_MAX = 4096                # лимит текста сообщения Telegram
CAPTION_MAX = 1024            # лимит подписи к фото
AI_DESC = 400                 # описание в подсказку ИИ — бережём токены Groq
AI_LIST_MAX = 1500            # подсказка ИИ для списка целиком
IMAGE_MAX_BYTES = 5 * 1024 * 1024

ANIME_KINDS = {"anime", "manga", "ranobe"}
ANIME_SITES = {"shikimori.one", "myanimelist.net", "anilist.co"}
VIDEO_KINDS = {"video", "channel", "playlist", "short", "stream"}

# зеркала одного сайта — одна и та же ссылка
_HOST_ALIASES = {
    "shikimori.me": "shikimori.one", "shikimori.io": "shikimori.one",
    "shikimori.org": "shikimori.one", "shiki.one": "shikimori.one",
}
_SHIKI_HOSTS = ["shikimori.one", *_HOST_ALIASES]
_SHIKI_PATH = re.compile(r"^/(animes|mangas|ranobe|characters|people)/[a-z]?(\d+)(?:[-/].*)?$")
_YT_RE = re.compile(
    r"(?:youtu\.be/|youtube\.com/(?:watch\?(?:.*&)?v=|shorts/|live/|embed/))([A-Za-z0-9_-]{11})")
_TRACKING = re.compile(r"^(utm_\w+|fbclid|gclid|yclid|ysclid|mc_cid|mc_eid|igshid|si|ref|ref_src|feature|from)$")
_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"'«»]+")
# Одиночная половинка эмодзи (JSON "\ud83d" без пары). Python её пропускает, а Telegram
# и SQLite на ней падают («surrogates not allowed») — вырезаем во всём, что пришло в запросе.
_SURROGATE = re.compile(r"[\ud800-\udfff]")
BATCH_KEY_MAX = 100


# ------------------------------- разбор запроса -------------------------------

def _str(v) -> str:
    """Строка из запроса без одиночных суррогатов."""
    return _SURROGATE.sub("", str(v or ""))


def _clean_line(v, limit: int) -> str:
    return _yt_clean_line(_str(v), limit)


def _clean_text(v, limit: int) -> str:
    return _yt_clean_text(_str(v), limit)


def _clean_url(v) -> str | None:
    s = _str(v).strip()
    if not s or len(s) > URL_MAX or re.search(r"\s", s):
        return None
    try:
        p = urlsplit(s)
    except ValueError:
        return None
    if p.scheme.lower() not in ("http", "https") or not p.hostname:
        return None
    return s


def _clean_image(v) -> str:
    s = _str(v).strip()
    if not s or len(s) > URL_MAX or re.search(r"\s", s):
        return ""
    try:
        p = urlsplit(s)
    except ValueError:
        return ""
    return s if p.scheme == "https" and p.hostname else ""


def parse_item(raw) -> dict | None:
    """Одна ссылка → словарь с обрезанными полями; None — ссылка негодная."""
    if not isinstance(raw, dict):
        return None
    url = _clean_url(raw.get("url"))
    if not url:
        return None
    return {
        "url": url,
        "site": _clean_line(raw.get("site"), SITE_MAX) or (urlsplit(url).hostname or ""),
        "kind": _clean_line(raw.get("kind"), KIND_MAX).lower(),
        "kind_label": _clean_line(raw.get("kindLabel"), KIND_LABEL_MAX),
        "icon": _clean_line(raw.get("icon"), ICON_MAX),
        "title": _clean_line(raw.get("title"), TITLE_MAX) or url[:TITLE_MAX],
        "meta": _clean_line(raw.get("meta"), META_MAX),
        "description": _clean_text(raw.get("description"), DESC_MAX),
        "image": _clean_image(raw.get("image")),
    }


def parse_request(body) -> dict:
    """{mode, title, items: [dict | None]} — None на месте негодной ссылки
    (в mode=each ей ответят ошибкой, в mode=list её пропустят)."""
    if not isinstance(body, dict):
        raise BadRequest("json object expected")
    mode = str(body.get("mode") or "").strip().lower()
    if mode not in ("each", "list"):
        raise BadRequest("mode must be each or list")
    items = body.get("items")
    if not isinstance(items, list) or not items:
        raise BadRequest("items list required")
    cap = EACH_MAX if mode == "each" else LIST_MAX
    if len(items) > cap:
        raise BadRequest(f"too many items (max {cap} for mode={mode})")
    parsed = [parse_item(x) for x in items]
    raw_urls = [_str(x.get("url"))[:URL_MAX] if isinstance(x, dict) else "" for x in items]
    if mode == "list" and not any(parsed):
        raise BadRequest("no valid urls")
    title = _clean_line(body.get("title"), LIST_TITLE_MAX) if mode == "list" else ""
    batch_key = _clean_line(body.get("batchKey"), BATCH_KEY_MAX) if mode == "list" else ""
    return {"mode": mode, "title": title, "items": parsed, "raw_urls": raw_urls, "batch_key": batch_key}


def list_fingerprint(title: str, items: list[dict]) -> str:
    """Отпечаток списка для сверки повторов без batchKey: заголовок + набор ссылок
    (без шума — url_key), порядок не важен."""
    import hashlib
    keys = sorted({url_key(it["url"]) or it["url"] for it in items})
    return hashlib.sha256("\n".join([title, *keys]).encode("utf-8")).hexdigest()[:32]


# ------------------------------- тексты постов --------------------------------

def _first_line(it: dict) -> str:
    head = " ".join(x for x in (it["icon"], it["kind_label"]) if x)
    return " · ".join(x for x in (head, it["title"]) if x)


def _cut(s: str, n: int) -> str:
    s = s.replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def item_text(it: dict, desc_limit: int | None = None) -> str:
    """Текст одной ссылки (пустые строки опускаются)."""
    desc = it["description"]
    if desc and desc_limit is not None:
        desc = _cut(desc, desc_limit)
    return "\n".join(x for x in (_first_line(it), it["meta"], desc, it["url"]) if x)


def post_text(it: dict, limit: int | None = None) -> str:
    """Пост для mode=each. limit — урезать описание (подпись к фото ≤ 1024);
    ссылка не режется никогда. Не влезает и без описания — None."""
    full = item_text(it)
    if limit is None or len(full) <= limit:
        return full
    bare = item_text({**it, "description": ""})
    room = limit - len(bare) - 1
    if room < 40 or not it["description"]:
        return bare if len(bare) <= limit else None
    return item_text(it, room)


def links_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        w = "ссылка"
    elif 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        w = "ссылки"
    else:
        w = "ссылок"
    return f"{n} {w}"


def list_parts(title: str, items: list[dict], limit: int = MSG_MAX) -> list[str]:
    """Пост-список; длиннее limit — несколько сообщений по границам ссылок.
    У каждой части свой заголовок, чтобы она читалась (и находилась) сама по себе."""
    blocks = [item_text(it, LIST_DESC)[: limit - 200] for it in items]
    n = len(items)

    def header(k: int, total: int) -> str:
        name = f"🔗 {title}" if title else "🔗 Список ссылок"
        if total == 1:
            return f"{name}\n{links_word(n)}"
        return f"{name} · часть {k}/{total}\n{links_word(n)}"

    # Заголовок с «часть 99/99» — самый длинный, им и меряем место под ссылки.
    room = limit - len(header(99, 99)) - 2
    groups, cur, size = [], [], 0
    for b in blocks:
        add = len(b) + (2 if cur else 0)
        if cur and size + add > room:
            groups.append(cur)
            cur, size = [], 0
            add = len(b)
        cur.append(b)
        size += add
    if cur:
        groups.append(cur)
    total = len(groups)
    return [header(k, total) + "\n\n" + "\n\n".join(g) for k, g in enumerate(groups, 1)]


def llm_text(it: dict) -> str:
    """Подсказка ИИ для выбора категории — тот же categorize(), что у постов."""
    parts = [f"Ссылка: {it['kind_label'] or it['kind'] or 'страница'} с сайта {it['site']}."]
    parts.append(f"Название: {it['title']}")
    if it["meta"]:
        parts.append(f"Подробности: {it['meta']}")
    if it["description"]:
        parts.append(f"Описание: {_cut(it['description'], AI_DESC)}")
    parts.append(it["url"])
    return "\n".join(parts)


def list_llm_text(title: str, items: list[dict]) -> str:
    lines = [f"Список ссылок ({links_word(len(items))})" + (f": {title}" if title else ".")]
    for it in items:
        line = " · ".join(x for x in (it["kind_label"], it["title"], it["meta"]) if x)
        line = f"- {_cut(line, 160)} ({it['site']})"
        if sum(len(x) + 1 for x in lines) + len(line) > AI_LIST_MAX:
            lines.append(f"…и ещё {len(items) - len(lines) + 1}")
            break
        lines.append(line)
    return "\n".join(lines)


def list_source(items: list[dict]) -> str | None:
    """Сайт списка, если все ссылки с одного сайта (для привычек «сайт → категория»)."""
    sites = {it["site"] for it in items}
    return sites.pop() if len(sites) == 1 else None


def default_category(items: list[dict], existing: list[str]) -> str:
    """Куда класть, если ИИ не ответил: аниме/манга — в подходящую уже заведённую
    категорию («Аниме», «Аниме и манга»…), видео — «Видео», остальное — «Ссылки»."""
    def pick(pattern: str, fallback: str) -> str:
        rx = re.compile(pattern, re.I)
        found = [c for c in existing if rx.search(c or "")]
        return min(found, key=len) if found else fallback

    if items and all(it["kind"] in ANIME_KINDS or _norm_host(it["site"]) in ANIME_SITES for it in items):
        return pick(r"аниме|манг|anime", "Аниме")
    if items and all(it["kind"] in VIDEO_KINDS for it in items):
        return "Видео"
    return pick(r"^ссылк|^links?$|закладк", "Ссылки")


# ------------------------------ повторы по ссылке -----------------------------

def _norm_host(host: str) -> str:
    h = (host or "").lower().rstrip(".")
    if h.startswith("www."):
        h = h[4:]
    return _HOST_ALIASES.get(h, h)


def url_key(url: str) -> str | None:
    """Ссылка без шума: схемы, www, зеркал, хвостового «/», #якоря и меток utm_*.
    Видео YouTube и страницы Shikimori — по их номеру."""
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return None
    if p.scheme.lower() not in ("http", "https") or not p.hostname:
        return None
    m = _YT_RE.search(url)
    if m:
        return "youtube:" + m.group(1)
    host = _norm_host(p.hostname)
    path = unquote(p.path).rstrip("/")
    if host == "shikimori.one":
        sm = _SHIKI_PATH.match(path)
        if sm:
            return f"shikimori:{sm.group(1)}/{sm.group(2)}"
    q = sorted((k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not _TRACKING.match(k))
    return host + path + ("?" + urlencode(q) if q else "")


def search_fragments(url: str) -> list[str]:
    """Куски текста для грубого поиска кандидатов в базе (дальше — сверка url_key)."""
    key = url_key(url)
    if not key:
        return []
    if key.startswith("youtube:"):
        vid = key.split(":", 1)[1]
        return [f"youtu.be/{vid}", f"v={vid}", f"shorts/{vid}", f"live/{vid}", f"embed/{vid}"]
    if key.startswith("shikimori:"):
        section, num = key.split(":", 1)[1].split("/")
        return [f"/{section}/{num}", f"/{section}/z{num}", f"/{section}/y{num}", f"/{section}/x{num}"]
    p = urlsplit(url.strip())
    host = _norm_host(p.hostname or "")
    raw = p.path.rstrip("/")
    out = [host + raw]
    if unquote(raw) != raw:
        out.append(host + unquote(raw))
    return out


def first_url(text: str) -> str | None:
    """Первая ссылка в тексте поста (для сверки, что пост тот же)."""
    m = _URL_IN_TEXT.search(text or "")
    return m.group(0).rstrip(").,;:!?…]") if m else None


def text_has_url(text: str, key: str) -> bool:
    for m in _URL_IN_TEXT.finditer(text or ""):
        u = m.group(0).rstrip(").,;:!?…]")
        if url_key(u) == key:
            return True
    return False


def find_saved(url: str, candidates_fn) -> dict | None:
    """Пост архива с этой же ссылкой. candidates_fn(fragments) → [{id, category,
    original_text}] (database.items_with_text_fragments)."""
    key = url_key(url)
    frags = search_fragments(url)
    if not key or not frags:
        return None
    for row in candidates_fn(frags):
        if text_has_url(row.get("original_text") or "", key):
            return row
    return None


# ---------------------------------- картинка ----------------------------------

def _public_ip(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    return ip.is_global and not ip.is_multicast


async def _host_is_public(host: str) -> bool:
    """Чужие картинки качаем только с публичных адресов (не localhost/сеть Render)."""
    if not host or host.lower() in ("localhost",) or host.lower().endswith(".localhost"):
        return False
    try:
        ipaddress.ip_address(host.strip("[]"))
        return _public_ip(host.strip("[]"))
    except ValueError:
        pass
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, 443, type=socket.SOCK_STREAM)
    except OSError:
        return False
    return bool(infos) and all(_public_ip(i[4][0]) for i in infos)


def _photo_bytes(data: bytes) -> bytes | None:
    """JPEG/PNG разумных размеров — как есть; прочее (WebP, GIF, огромное) — в JPEG.
    Не картинка — None."""
    import io
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            fmt = im.format
            w, h = im.size
            if w < 10 or h < 10:
                return None
            ok_size = w + h <= 10000 and max(w, h) / max(1, min(w, h)) <= 20
            if fmt in ("JPEG", "PNG") and ok_size:
                return data
            im.seek(0)
            im = im.convert("RGB")
            im.thumbnail((2560, 2560))
            out = io.BytesIO()
            im.save(out, "JPEG", quality=88)
            return out.getvalue()
    except Exception:
        return None


async def fetch_image(url: str, timeout: float = 10) -> bytes | None:
    """Картинка ссылки: только https, только публичные адреса, ≤ 5 МБ, до 3 переадресаций."""
    try:
        import aiohttp
    except ImportError:
        return None
    try:
        tmo = aiohttp.ClientTimeout(total=timeout)
        headers = {"User-Agent": "Mozilla/5.0 (compatible; SaveandSortBot/1.0)",
                   "Accept": "image/avif,image/webp,image/png,image/jpeg,image/*;q=0.8"}
        async with aiohttp.ClientSession(timeout=tmo, headers=headers) as session:
            for _ in range(4):
                p = urlsplit(url)
                if p.scheme != "https" or not await _host_is_public(p.hostname or ""):
                    return None
                async with session.get(url, allow_redirects=False) as r:
                    if r.status in (301, 302, 303, 307, 308) and r.headers.get("Location"):
                        url = urljoin(url, r.headers["Location"])
                        continue
                    if r.status != 200:
                        return None
                    ctype = (r.headers.get("Content-Type") or "").lower()
                    if ctype and not ctype.startswith(("image/", "application/octet-stream")):
                        return None
                    if (r.content_length or 0) > IMAGE_MAX_BYTES:
                        return None
                    data = await read_limited(r.content, IMAGE_MAX_BYTES)
                if not data or len(data) < 500:
                    return None
                return await asyncio.to_thread(_photo_bytes, data)
    except Exception:
        logger.info("Картинка ссылки не скачалась: %s", url[:200], exc_info=True)
    return None
