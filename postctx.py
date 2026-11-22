"""Разбор присланного поста: чистый текст без подвалов каналов + всё, что Telegram
уже знает о сообщении (превью ссылки, кнопки, опрос, файл, музыка, место...).

Чистые функции (clean_post_text, strip_aux, ...) не зависят от Telethon и
покрыты тестами в tests/test_postctx.py.
"""

from __future__ import annotations

import re

# Разделитель между текстом поста и тем, что бот распознал сам (картинка, речь,
# превью ссылки). В original_text хранится «текст поста + AUX_MARK + распознанное»:
# распознанное нужно для поиска и перераспознавания без повторного скачивания,
# но не должно попадать в подпись при пересылке и в сравнение дублей.
AUX_TAG = "〔распознано〕"
AUX_MARK = f"\n\n{AUX_TAG}\n"

# Старые форматы приписок (до AUX_MARK) — для совместимости с уже сохранёнными постами.
_LEGACY_AUX = re.compile(r"\n\n\((?:по ссылке|изображение|аудио): .*\)\s*$", re.DOTALL)

_MD_LINK = re.compile(r"\[([^\]]*)\]\(([^)\s]+)\)")
_BARE_URL = re.compile(r"(?:https?://|tg://)\S+|(?<![\w/])t\.me/\S+", re.IGNORECASE)
_MENTION = re.compile(r"(?<!\w)@\w{3,}")
_HASHTAG = re.compile(r"(?<!\w)#(\w+)")
_EMOJI = re.compile(
    "[\U0001F000-\U0001FAFF☀-➿⬀-⯿️‍⃣←-⇿■-◿⌀-⏿]+"
)
_MD_MARKS = re.compile(r"\*\*|__|~~|`+|\|\||\*|(?<!\w)_|_(?!\w)")

# Ссылки, которые почти всегда — подвал канала, а не содержание поста.
_FOOTER_URL = re.compile(
    r"t\.me/(\+|joinchat|addlist|addemoji|addstickers|boost|c/)"
    r"|t\.me/[\w]+bot\b|t\.me/[\w]+_bot\b"
    r"|t\.me/[A-Za-z][\w]{3,}/?(\?[^\s)]*)?$"  # ссылка на канал без номера поста
    r"|^tg://|boosty\.to|max\.ru/join|patreon\.com|donationalerts|vk\.(com|cc)/(?!video|wall|clip)"
    r"|tribute|yoomoney|cloudtips",
    re.IGNORECASE,
)

# Слова/фразы подвалов («Подписаться», «Наш чат | Предложка | Бусти»...).
_FOOTER_WORDS = re.compile(
    r"подпис\w*|подпишись|наш\w* (канал|чат)|канал\w*|чат\w*|предложк\w*|предложить( пост| новость)?|"
    r"прислать (пост|новость)|private|приват\w*|бусти|boosty|донат\w*|поддержать|поддержка|"
    r"реклам\w*|сотрудничеств\w*|все мои каналы|мы в \w+|сделать стикеры|стикеры|vpn|впн|"
    r"subscribe|join|support|our (channel|chat)|source|author|artist|автор|источник|"
    r"ошибки в сериях|расписание|топор|читать (далее|полностью|в источнике)|зеркало|резерв\w*|"
    r"\+18|18\+|папк\w*|ссылк\w*|группа|вайбер\w*|max",
    re.IGNORECASE,
)

_WORD = re.compile(r"[^\W\d_]{2,}")


def has_aux(stored_text: str) -> bool:
    return AUX_TAG in (stored_text or "")


def strip_aux(stored_text: str) -> str:
    """original_text из БД → только текст поста (без распознанных приписок)."""
    t = stored_text or ""
    if AUX_TAG in t:
        t = t.split(AUX_TAG, 1)[0]
    t = _LEGACY_AUX.sub("", t)
    return t.strip()


def aux_part(stored_text: str) -> str:
    """Распознанная часть original_text (или '')."""
    t = stored_text or ""
    return t.split(AUX_TAG, 1)[1].strip() if AUX_TAG in t else ""


def join_stored(display_text: str, aux: str) -> str:
    display_text = (display_text or "").strip()
    aux = (aux or "").strip()
    if not aux:
        return display_text
    return f"{display_text}{AUX_MARK}{aux}".strip()


def _plain(md: str) -> str:
    """markdown → обычный текст (ссылки превращаются в их подписи)."""
    t = _MD_LINK.sub(lambda m: m.group(1), md or "")
    # разметку снимаем только вне ссылок, иначе ломаются url с «_» (youtube id и т.п.)
    out, pos = [], 0
    for m in _BARE_URL.finditer(t):
        out.append(_MD_MARKS.sub("", t[pos:m.start()]))
        out.append(m.group(0))
        pos = m.end()
    out.append(_MD_MARKS.sub("", t[pos:]))
    return "".join(out)


def _line_is_footer(line: str) -> bool:
    links = _MD_LINK.findall(line)
    urls = [u for _, u in links] + _BARE_URL.findall(_MD_LINK.sub(" ", line))
    has_footer_link = any(_FOOTER_URL.search(u.strip(").,")) for u in urls)
    has_mention = bool(_MENTION.search(line))
    rest = _MD_LINK.sub(lambda m: " " + m.group(1) + " ", line)
    rest = _BARE_URL.sub(" ", rest)
    rest = _MENTION.sub(" ", rest)
    rest = _HASHTAG.sub(" ", rest)
    rest = _MD_MARKS.sub(" ", rest)
    rest_wo_footer_words = _FOOTER_WORDS.sub(" ", rest)
    words = _WORD.findall(rest_wo_footer_words)
    footer_vocab = bool(_FOOTER_WORDS.search(rest))
    if has_footer_link and len(words) <= 3:
        return True
    if has_mention and not _WORD.findall(rest):
        return True  # строка из одних @упоминаний
    if footer_vocab and (has_footer_link or has_mention or urls) and len(words) <= 2:
        return True
    # «👉 Топор +18. Подписаться» без ссылки (ссылка в entity, которую мы не видим)
    if footer_vocab and not words and len(rest.strip()) < 60:
        return True
    return False


# Мусор от старого обогащения ссылок-приглашений t.me (лежит в уже сохранённых постах).
_TG_JUNK = re.compile(
    r"\(по ссылке:[^)]*(Join group chat on Telegram|Telegram\s*[–-]\s*a new era of messaging|"
    r"Telegram: (Add Chats|Contact|Join|View))[^)]*\)?",
    re.IGNORECASE,
)


def strip_footer_md(md_text: str) -> str:
    """Текст поста для пересылки другу: разметка и ссылки остаются, подвал канала
    («Подписаться на …», @канал, кнопки-эмодзи) уходит."""
    lines = _TG_JUNK.sub("", md_text or "").split("\n")
    kept = [ln for ln in lines if not (ln.strip() and _share_footer(ln))]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(kept)).strip()


_TG_URL = re.compile(r"^(?:https?://)?(?:t\.me|telegram\.me)/|^tg://", re.IGNORECASE)


def _share_footer(line: str) -> bool:
    """Строже, чем для ИИ: другу не отправляем только явную рекламу канала. Титры
    («Звукорежиссёр: …») и ссылки на источник вне Telegram (TikTok и т.п.) остаются."""
    if not _line_is_footer(line):
        return False
    urls = [u for _, u in _MD_LINK.findall(line)] + _BARE_URL.findall(_MD_LINK.sub(" ", line))
    if any(not (_TG_URL.search(u.strip()) or _FOOTER_URL.search(u.strip(").,"))) for u in urls):
        return False  # ссылка на источник (TikTok, сайт) — это содержание
    if _FOOTER_WORDS.search(_plain(line)):
        return True
    outside = _MENTION.sub(" ", _BARE_URL.sub(" ", _MD_LINK.sub(" ", line)))
    return not _WORD.findall(_MD_MARKS.sub(" ", outside))  # одни ссылки, @упоминания и эмодзи


def clean_post_text(md_text: str) -> dict:
    """Текст поста (markdown от Telethon) → суть без подвалов.

    Возвращает {"text": чистый текст без разметки, "links": [(подпись, url)] из
    содержательной части, "hashtags": [...], "footer": [вырезанные строки]}."""
    lines = _TG_JUNK.sub("", md_text or "").split("\n")
    kept, footer = [], []
    for line in lines:
        if line.strip() and _line_is_footer(line):
            footer.append(line.strip())
        else:
            kept.append(line)
    body = "\n".join(kept).strip()
    links = []
    for anchor, url in _MD_LINK.findall(body):
        if not _FOOTER_URL.search(url):
            links.append((_plain(anchor).strip(), url))
    for url in _BARE_URL.findall(_MD_LINK.sub(" ", body)):
        url = url.rstrip(").,!?»")
        if not _FOOTER_URL.search(url):
            links.append(("", url))
    hashtags = []
    for h in _HASHTAG.findall(md_text or ""):
        h = h.strip("_")
        if h and h.lower() not in {x.lower() for x in hashtags}:
            hashtags.append(h)
    text = _plain(body)
    text = _BARE_URL.sub(lambda m: m.group(0) if not _FOOTER_URL.search(m.group(0)) else "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return {"text": text, "links": links, "hashtags": hashtags[:12], "footer": footer}


def content_letters(text: str) -> int:
    """Сколько «содержательных» букв в тексте (без ссылок, @, #, эмодзи)."""
    t = _BARE_URL.sub(" ", text or "")
    t = _MENTION.sub(" ", t)
    t = _HASHTAG.sub(" ", t)
    t = _EMOJI.sub(" ", t)
    return len(re.findall(r"[^\W\d_]", t))


def is_weak_text(clean_text: str, threshold: int = 25) -> bool:
    """Подпись слишком бедная, чтобы по ней одной понять тему."""
    return content_letters(clean_text) < threshold


# ---------------------------------------------------------------------------
# Извлечение из Telethon-сообщения (без сетевых вызовов)
# ---------------------------------------------------------------------------

def _tw(x) -> str:
    """TextWithEntities | str → str (в новых слоях MTProto опросы — TextWithEntities)."""
    if x is None:
        return ""
    return str(getattr(x, "text", x) or "")


def message_extras(msg) -> list[str]:
    """Подсказки о теме, которые Telegram уже прислал вместе с сообщением."""
    out: list[str] = []
    media = getattr(msg, "media", None)
    mname = type(media).__name__ if media is not None else ""

    # Превью ссылки: Telegram сам сходил на страницу — заголовок/описание бесплатно.
    wp = getattr(msg, "web_preview", None)
    if wp is not None:
        site = (getattr(wp, "site_name", "") or "").strip()
        title = (getattr(wp, "title", "") or "").strip()
        desc = re.sub(r"\s+", " ", getattr(wp, "description", "") or "").strip()
        if site.lower() != "telegram" and (title or desc):
            line = "Превью ссылки: " + " — ".join(x for x in (site, title) if x)
            if desc:
                line += f". {desc[:400]}"
            out.append(line)

    # Кнопки под пересланным постом («Смотреть трейлер», «Купить в Steam»...).
    rm = getattr(msg, "reply_markup", None)
    btns = []
    for row in getattr(rm, "rows", None) or []:
        for b in getattr(row, "buttons", None) or []:
            t = (getattr(b, "text", "") or "").strip()
            url = getattr(b, "url", None) or ""
            if not t or _FOOTER_URL.search(url) or _FOOTER_WORDS.fullmatch(t.strip(" 👉🔥⭐️")):
                continue
            dom = re.search(r"https?://(?:www\.)?([^/\s]+)", url)
            btns.append(f"{t} ({dom.group(1)})" if dom else t)
    if btns:
        out.append("Кнопки: " + "; ".join(btns[:6]))

    # Опрос.
    poll = getattr(msg, "poll", None)
    if poll is not None:
        p = getattr(poll, "poll", poll)
        q = _tw(getattr(p, "question", ""))
        answers = [_tw(getattr(a, "text", "")) for a in (getattr(p, "answers", None) or [])]
        out.append("Опрос: " + q + (" Варианты: " + "; ".join(a for a in answers if a) if answers else ""))

    # Файлы: имя, тип, музыка, стикер.
    f = getattr(msg, "file", None)
    doc = getattr(msg, "document", None)
    if f is not None and doc is not None:
        performer = (getattr(f, "performer", None) or "").strip()
        title = (getattr(f, "title", None) or "").strip()
        if getattr(msg, "sticker", None):
            emoji = getattr(f, "emoji", None) or ""
            out.append(f"Стикер {emoji}".strip())
        elif performer or title:
            out.append("Музыка: " + " — ".join(x for x in (performer, title) if x))
        else:
            name = (getattr(f, "name", None) or "").strip()
            mime = (getattr(f, "mime_type", None) or "").strip()
            if name and not re.fullmatch(r"(video|IMG|VID|photo|image|file)?[_\- ]?\d[\d_\- .]*\.\w+", name, re.I):
                out.append(f"Файл: {name}" + (f" ({mime})" if mime else ""))
            elif mime and not mime.startswith(("video/", "image/", "audio/")):
                out.append(f"Файл ({mime})")

    # Место, контакт, игра, история.
    venue = getattr(msg, "venue", None)
    if venue is not None:
        out.append("Место: " + ", ".join(x for x in (getattr(venue, "title", ""), getattr(venue, "address", "")) if x))
    elif getattr(msg, "geo", None) is not None:
        out.append("Геопозиция (точка на карте)")
    contact = getattr(msg, "contact", None)
    if contact is not None:
        out.append("Контакт: " + " ".join(x for x in (getattr(contact, "first_name", ""), getattr(contact, "last_name", "")) if x))
    game = getattr(msg, "game", None)
    if game is not None:
        out.append("Игра: " + (getattr(game, "title", "") or ""))
    if mname == "MessageMediaStory":
        story = getattr(media, "story", None)
        cap = (getattr(story, "caption", "") or "").strip() if story is not None else ""
        out.append("Репост истории" + (f": {cap[:300]}" if cap else ""))
    if getattr(msg, "dice", None) is not None:
        out.append("Кубик/эмодзи-игра")
    return out


def forward_source(msg) -> tuple[str | None, str | None]:
    """(название, @username) канала/человека, откуда переслан пост."""
    fwd = getattr(msg, "forward", None)
    if fwd is None:
        return None, None
    chat = None
    try:
        chat = fwd.chat or fwd.sender
    except Exception:
        chat = None
    if chat is not None:
        # как и раньше в боте: у канала — название, у человека — @username (иначе имя),
        # чтобы статистика «куда обычно попадает этот источник» не разъехалась
        username = getattr(chat, "username", None)
        title = getattr(chat, "title", None) or username or " ".join(
            x for x in (getattr(chat, "first_name", None), getattr(chat, "last_name", None)) if x
        ) or None
        return title, username
    name = getattr(fwd, "from_name", None)
    return (str(name) if name else None), None


def build_llm_text(
    clean: dict,
    extras: list[str],
    vision: list[tuple[str, str]],
    speech: str = "",
    channel_username: str | None = None,
) -> str:
    """Собирает вход для категоризатора из всех источников."""
    parts = []
    if clean.get("text"):
        parts.append("Текст поста:\n" + clean["text"][:2000])
    links = [f"{a} ({u})" if a and a not in u else u for a, u in clean.get("links", [])][:5]
    if links:
        parts.append("Ссылки: " + "; ".join(links))
    if clean.get("hashtags"):
        parts.append("Хэштеги: " + ", ".join("#" + h for h in clean["hashtags"]))
    for label, desc in vision:
        if desc:
            parts.append(f"{label}: {desc}")
    if speech:
        parts.append("Речь в видео/аудио: " + speech[:1200])
    parts.extend(extras)
    if channel_username:
        parts.append(f"Канал: @{channel_username}")
    return "\n".join(parts).strip()
