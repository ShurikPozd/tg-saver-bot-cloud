import asyncio
import base64
import json
import logging
import os
import re
from datetime import datetime, timedelta

from telethon import TelegramClient, events, Button, utils
from telethon.errors import FloodWaitError
from telethon.network import ConnectionTcpMTProxyAbridged

from config import (
    API_HASH,
    API_ID,
    BACKUP_CHANNEL_ID,
    BOT_TOKEN,
    DB_PATH,
    GITHUB_BRANCH,
    GITHUB_PATH,
    GITHUB_REPO,
    GITHUB_TOKEN,
    GROQ_MODEL,
    HTTP_PORT,
    MT_PROXY_HOST,
    MT_PROXY_PORT,
    MT_PROXY_SECRET,
    OWNER_ID,
    SEED_FILE,
    SESSION_FILE,
)

import database as db
import keyboards as _kb
import linkmeta
import pending
from categorizer import categorize, organize, describe_image, propose_subgroups, strip_markdown, normalize_summary
from stt import transcribe_audio
from keyboards import (
    REPLY_BUTTONS,
    main_keyboard,
    root_categories_keyboard,
    maybe_pending_label,
    folder_page_keyboard,
    merge_source_keyboard,
    category_keyboard_rows,
    items_keyboard,
    view_keyboard,
    move_keyboard,
    sel_move_keyboard,
    mvcat_pick_keyboard,
    mvfolder_pick_keyboard,
    confirm_delete_keyboard,
    confirm_keyboard,
    quick_actions_keyboard,
    trash_keyboard,
    settings_keyboard,
    theme_icon as _kb_theme_icon,
    tag_editor_keyboard,
    tag_palette_keyboard,
    tags_section_keyboard,
    manage_tags_keyboard,
    manage_tag_actions_keyboard,
    category_icon_keyboard,
    DEFAULT_TAG_CIRCLES,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def _safe_handler(fn):
    async def wrapper(event):
        try:
            await fn(event)
        except Exception as e:
            logger.exception("Ошибка в обработчике %s", getattr(fn, "__name__", fn))
            try:
                await event.answer("⚠️ Что-то пошло не так. Попробуй ещё раз.", alert=True)
            except Exception:
                pass

    return wrapper

ITEMS_PER_PAGE = 8

_icon_overrides: dict = {}

# (chat_id, message_id) сообщений, отправленных show_item_view как ОТДЕЛЬНОЕ новое сообщение.
# Навигация (Назад/В начало) из них должна слать НОВОЕ сообщение, а не редактировать это.
_detached_views: set = set()

# У кого уже пробовали восстановить архив из канала-хранилища в этой сессии (один раз).
_restore_tried: set = set()


def _existing_category_names():
    return [c["category"] for c in db.get_categories()]


_CLARIFY_STOP = {
    "и", "в", "во", "на", "за", "не", "по", "с", "со", "о", "об", "для",
    "а", "из", "про", "или", "от", "к", "у", "до", "при", "то", "чтоб",
    "это", "без", "сериала", "товары", "такое", "общий", "для",
}


def _cat_words(name: str) -> list[str]:
    return [w for w in re.findall(r"[а-яёa-z0-9]+", (name or "").lower()) if len(w) >= 3 and w not in _CLARIFY_STOP]


def _suggest_names(text: str, exclude: str | None = None, limit: int = 6) -> list[str]:
    """Подсхемы (существующие категории), упомянутые в тексте поста. Без LLM."""
    t = (text or "").lower()
    scored = []
    for c in db.get_categories():
        name = c["category"]
        if c.get("locked") or not name or name == exclude:
            continue
        score = 0
        nl = name.lower()
        if nl in t:
            score += 3
        for w in _cat_words(name):
            if w in t:
                score += 1
        if score:
            scored.append((score, -len(name), name))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [s[2] for s in scored[:limit]]


def _clarify_buttons(names: list[str], sid: str) -> list:
    rows = []
    for i in range(0, len(names), 2):
        chunk = names[i:i + 2]
        rows.append([Button.inline(f"«{n}»", data=f"clrs|{sid}|{i + j}") for j, n in enumerate(chunk)])
    rows.append([Button.inline("✍️ Свой вариант", data="cancel_action")])
    return rows


def _refresh_icon_overrides():
    global _icon_overrides
    _icon_overrides = db.get_icon_overrides()
    _kb.set_icon_overrides(_icon_overrides)


def theme_icon(name: str, fallback: str = "📦") -> str:
    icon = _icon_overrides.get(name)
    if icon:
        return icon
    return _kb_theme_icon(name, fallback)

DEFAULT_CATEGORY = {
    "photo": "Фото",
    "video": "Видео",
    "audio": "Аудио",
    "voice": "Аудио",
    "document": "Документы",
    "animation": "Видео",
    "text": "Заметки",
}

DEFAULT_SUMMARY = {
    "photo": "Фото",
    "video": "Видео",
    "audio": "Аудио",
    "voice": "Аудио",
    "document": "Документ",
    "animation": "Анимация",
    "text": "Заметка",
}

pending_search = {}
selections = {}
merge_flow = {}
unlock_sel = {}
tag_filter = {}
tag_create = {}
tag_icon_apply = {}
trash_sel = {}
undo_data = {}
dup_stage = {}
clarify_opts = {}
subfolders_plan = {}
auto_order_plan = {}

SETTINGS_DEFAULTS = {
    "auto_lock": "1",
    "lock_move": "1",
    "lock_rename": "1",
    "lock_create": "1",
    "photo_vision": "1",
    "quick_actions": "1",
    "cleanup_empty": "1",
    "auto_order_posts": "10",
    "subfolders_enabled": "1",
    "subfolders_min": "5",
    "subfolders_in_auto": "1",
    "audio_vision": "1",
    "dedup_check": "1",
    "weekly_digest": "1",
    "backup_tg_daily": "1",
    "backup_every_posts": "5",
    "auto_heal_broken": "0",
    "link_enrich": "1",
    "auto_recover_posts": "1",
}

# Авто-порядок per-chat (у каждого пользователя свой счётчик/таймер/защита от параллельности).
_auto_order_pending: dict[int, int] = {}
_auto_order_busy: dict[int, bool] = {}
_auto_order_timer: dict[int, asyncio.TimerHandle] = {}


def settings_state(chat_id: int | None = None) -> dict:
    return {k: db.get_setting(k, v) for k, v in SETTINGS_DEFAULTS.items()}


def _maybe_cleanup() -> None:
    if db.get_setting("cleanup_empty", "1") == "1":
        db.cleanup_empty(include_manual=True)


def _activate_user(user_id: int | None) -> int | None:
    """Активирует БД конкретного пользователя (per-user архив). Возвращает user_id."""
    if not user_id:
        return None
    uid = int(user_id)
    db.register_user(uid)
    db.set_current_user(uid)
    _refresh_icon_overrides()
    return uid


def _saved_in_group(chat, sender_id) -> bool:
    """В группах каждый пользователь сохраняет в свой личный архив."""
    if chat is None or getattr(chat, "private", True):
        return False
    return bool(sender_id)


def _sel_entry(chat_id: int, category: str):
    key = (chat_id, category)
    if key not in selections:
        selections[key] = {"ids": set(), "page": 0}
    return selections[key]


def _lu_state(chat_id: int):
    if chat_id not in unlock_sel:
        unlock_sel[chat_id] = {"items": set(), "cats": set(), "folders": set(), "mode": False}
    return unlock_sel[chat_id]


def _lu_clear(chat_id: int):
    st = _lu_state(chat_id)
    st["items"].clear()
    st["cats"].clear()
    st["folders"].clear()


def _sel_clear(chat_id: int, category: str):
    selections.pop((chat_id, category), None)


def _is_placeholder_token(tok: str) -> bool:
    if len(tok) < 3:
        return False
    if re.fullmatch(r"[a-zA-Z0-9_\-\.]+", tok) and re.search(r"[0-9_]|-", tok):
        return True
    if "_" in tok or "-" in tok:
        return True
    return False


_CREDIT_PATTERNS = re.compile(
    r"(artist|illustrator|photographer|creator|source|channel|©)\s*[:\-—]?\s*\S*",
    re.IGNORECASE,
)


def _has_meaningful_text(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False
    if re.search(r"https?://\S+", t):
        return True
    remaining = re.sub(r"https?://\S+|@\w+|#\w+", " ", t)
    remaining = _CREDIT_PATTERNS.sub(" ", remaining)
    remaining = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F]+", " ", remaining)
    tokens = re.findall(r"\S+", remaining)
    meaningful = [tok for tok in tokens if not _is_placeholder_token(tok)]
    return len(meaningful) >= 2


def _pack_file(f):
    try:
        return utils.pack_bot_file_id(f)
    except Exception:
        return None


def _media_token(t: str, obj) -> str | None:
    """Стабильный идентификатор вложения для поиска дублей (в т.ч. фото, у которых
    нет bot-file_unique_id). Одинаковые вложения дают одинаковый токен."""
    uid = getattr(obj, "id", None)
    if uid is not None:
        return f"{t}:{uid}"
    return None


def _msg_media(msg) -> list[dict]:
    def add(t, obj):
        media.append({"t": t, "f": _pack_file(obj), "u": _media_token(t, obj)})

    media = []
    if getattr(msg, "photo", None):
        add("photo", msg.photo)
    if getattr(msg, "video", None):
        add("video", msg.video)
    if getattr(msg, "gif", None):
        add("animation", msg.gif)
    if getattr(msg, "audio", None):
        add("audio", msg.audio)
    if getattr(msg, "voice", None):
        add("voice", msg.voice)
    if not (getattr(msg, "photo", None) or getattr(msg, "video", None) or getattr(msg, "gif", None)
            or getattr(msg, "audio", None) or getattr(msg, "voice", None)) and getattr(msg, "document", None):
        add("document", msg.document)
    return media


def _primary_content_type(media: list[dict]) -> str:
    return media[0]["t"] if media else "text"


async def _get_source_name(msg) -> str | None:
    fwd = getattr(msg, "forward", None)
    if fwd is None:
        return None
    name = getattr(fwd, "from_name", None)
    if name:
        return str(name)
    from_id = getattr(fwd, "from_id", None)
    if from_id is None:
        return None
    try:
        entity = await client.get_entity(from_id)
        return getattr(entity, "title", None) or getattr(entity, "username", None) or None
    except Exception:
        return None


async def _describe_media(client, msg) -> str | None:
    if db.get_setting("photo_vision", "1") != "1":
        return None
    try:
        data = await client.download_media(msg, file=bytes)
        if not data:
            return None
        caption = await describe_image(data)
        return caption or None
    except Exception:
        return None


async def _transcribe_media(client, msg) -> str | None:
    if db.get_setting("audio_vision", "1") != "1":
        return None
    try:
        f = getattr(msg, "file", None)
        if f is not None and (getattr(f, "size", 0) or 0) > 45 * 1024 * 1024:
            return None
        data = await client.download_media(msg, file=bytes)
        if not data:
            return None
        result = await transcribe_audio(data)
        return result or None
    except Exception:
        return None


def _media_uniques(media: list[dict]) -> list[str]:
    out = []
    for m in media:
        u = m.get("u")
        if u and u not in out:
            out.append(u)
    return out


def _file_unique_str(media: list[dict]) -> str | None:
    uids = _media_uniques(media)
    return " ".join(uids) if uids else None


async def _save(
    client: TelegramClient,
    chat_id: int,
    processing,
    content_type: str,
    text: str,
    file_ids: list[dict],
    message_id: int,
    media_group_id: str | None = None,
    source_channel: str | None = None,
    vision_hint: str | None = None,
    audio_hint: str | None = None,
    dedup_note: str | None = None,
):
    text = (text or "").strip()
    display_original = text
    if vision_hint:
        if not _has_meaningful_text(text):
            text = vision_hint
        else:
            text = f"{text}\n\n(изображение: {vision_hint})"
    if audio_hint:
        if not _has_meaningful_text(text):
            text = audio_hint
        else:
            text = f"{text}\n\n(аудио: {audio_hint})"

    if (
        db.get_setting("link_enrich", "1") == "1"
        and re.search(r"https?://\S+", text)
        and _has_meaningful_text(text)
    ):
        try:
            text, _metas = await linkmeta.enrich_links(text)
        except Exception:
            log.warning("linkmeta.enrich_links failed", exc_info=True)

    if _has_meaningful_text(text):
        result = await categorize(
            text,
            content_type=content_type,
            source=source_channel,
            categories=_existing_category_names(),
        )
        if result.get("llm_ok") is False:
            category = DEFAULT_CATEGORY.get(content_type, "Другое")
            summary = result.get("summary") or DEFAULT_SUMMARY.get(content_type, "Сохранено")
        else:
            category = result.get("category", "Другое")
            summary = result.get("summary", text[:300])
    else:
        category = DEFAULT_CATEGORY.get(content_type, "Другое")
        summary = DEFAULT_SUMMARY.get(content_type, "Сохранено")

    item_id = db.save_item(
        category=category,
        content_type=content_type,
        summary=summary,
        original_text=text.strip(),
        file_id=file_ids[0]["f"] if file_ids else None,
        message_id=message_id,
        chat_id=chat_id,
        media_group_id=media_group_id,
        file_ids=file_ids or None,
        source_channel=source_channel,
        file_unique=_file_unique_str(file_ids),
    )

    _auto_order_pending[chat_id] = _auto_order_pending.get(chat_id, 0) + 1
    _arm_auto_order(client, chat_id, db.current_user_id() or 0)

    _maybe_auto_backup_tg(db.current_user_id() or 0)

    emoji = theme_icon(category, "📦")
    count = f" ({len(file_ids)} медиа)" if file_ids and len(file_ids) > 1 else ""
    chan = f"\n📡 {source_channel}" if source_channel else ""
    dnote = f"\n\n{dedup_note}" if dedup_note else ""
    links_block = ""
    if re.search(r"https?://\S+", display_original):
        links_block = f"\n\n🔗 {display_original[:600]}"
    qact = db.get_setting("quick_actions", "1") == "1"
    buttons = quick_actions_keyboard(item_id, category) if qact else [[Button.inline("👌 Ок", data="dismiss")]]
    try:
        await processing.edit(
            f"{emoji} Сохранено в «{category}»{count}{chan}\n\n{summary}{dnote}{links_block}",
            buttons=buttons,
        )
    except Exception:
        pass


async def _save_single(client: TelegramClient, msg, media_group_id: str | None = None):
    media = _msg_media(msg)
    content_type = _primary_content_type(media)
    processing = await msg.reply("⏳ Анализирую...")

    if not media_group_id:
        raw_text = (msg.text or "").strip()
        primary = media[0]["f"] if media else None
        dup = db.find_dup_media(primary) if primary else db.find_dup_text(raw_text)
        others = []
        if db.get_setting("dedup_check", "1") == "1" and _media_uniques(media):
            others = [d for d in db.find_dupes(_media_uniques(media)) if not dup or d["id"] != dup["id"]]
        if dup or others:
            payload = {
                "content_type": content_type,
                "text": raw_text,
                "media": media,
                "message_id": msg.id,
                "chat_id": msg.chat_id,
                "media_group_id": media_group_id,
                "source_channel": await _get_source_name(msg),
            }
            dup_stage[msg.chat_id] = payload
            lines = []
            if dup:
                emoji = theme_icon(dup["category"], "📦")
                lock = " 🔒" if dup["locked"] else ""
                lines.append(
                    f"⚠️ Похоже, такой пост уже сохранён (ID {dup['id']}{lock}):\n"
                    f"{emoji} {dup['summary']} — «{dup['category']}»"
                )
            if others:
                extra = " ".join(f"#{d['id']} ({d['category']})" for d in others[:3])
                lines.append(f"⚠️ Вложение уже есть в сохранённых: {extra}")
            lines.append("Сохранить всё равно?")
            ask_text = "\n".join(lines)
            pending.push(
                msg.chat_id, {"kind": "dup", "dup_payload": payload}, "⚠️ Сохранить дубль?",
                replay={"kind": "dup", "text": ask_text},
            )
            try:
                await processing.edit(
                    ask_text,
                    buttons=[
                        [Button.inline("✅ Да, сохранить", data="dup_save_yes")],
                        [Button.inline("❌ Нет, не надо", data="dup_save_no")],
                    ]
                    + main_keyboard(),
                )
            except Exception:
                pass
            return

    vision_hint = None
    audio_hint = None
    plain = (msg.text or "").strip()
    if content_type == "photo" and not _has_meaningful_text(plain):
        vision_hint = await _describe_media(client, msg)
    elif content_type in ("voice", "audio") and not _has_meaningful_text(plain):
        audio_hint = await _transcribe_media(client, msg)
    await _save(
        client,
        chat_id=msg.chat_id,
        processing=processing,
        content_type=content_type,
        text=plain,
        file_ids=media,
        message_id=msg.id,
        media_group_id=media_group_id,
        source_channel=await _get_source_name(msg),
        vision_hint=vision_hint,
        audio_hint=audio_hint,
    )


async def cmd_start(event):
    await event.respond(
        "👋 Привет! Я бот для сохранения контента.\n\n"
        "Просто пересылай мне сообщения, посты, альбомы с фото и видео — "
        "я автоматически определю категорию и сохраню целиком.\n\n"
        "Кнопки под сообщением — категории, последние посты, поиск и статистика.\n"
        "⚙️ Настройки — автозамок, распознавание фото и голоса, быстрые действия.\n"
        "Также доступны команды: /categories /search /stats /recent /export /settings /help",
        buttons=main_keyboard(),
    )


async def cmd_pending(event):
    es = pending.entries(event.chat_id)
    if not es:
        await event.respond(
            "📭 Висящих вопросов нет. Бот не ждёт от тебя ответов.",
            buttons=main_keyboard(),
        )
        return
    lines = [f"📬 Висящих вопросов: {len(es)}", ""]
    rows = []
    for i, e in enumerate(es, 1):
        lines.append(f"{i}. {e.get('label') or 'Вопрос'}")
        rows.append([Button.inline(f"➡️ {i}. Ответить", data=f"pend_replay|{e['sid']}")])
    rows.append([Button.inline("🗑 Очистить вопросы", data="pend_clear")])
    await event.respond(
        "\n".join(lines),
        buttons=rows + [[Button.inline("❌ Скрыть список", data="dismiss")]],
    )


def _history_label(r: dict) -> str:
    act = r["action"]
    old = r["old_value"]
    new = r["new_value"]
    if act == "category":
        return f"Перемещено: «{old}» → «{new}»"
    if act == "summary":
        return f"Название: {old[:40]!r} → {new[:40]!r}"
    if act == "comment":
        return f"Комментарий: {old[:40]!r} → {new[:40]!r}"
    if act == "trash":
        return "В корзину"
    if act == "restore":
        return f"Восстановлен → «{new}»"
    if act == "lock":
        return "Заблокировано"
    if act == "unlock":
        return "Разблокировано"
    return f"{act}: {old!r} → {new!r}"


def _resolve_dup_pending(chat_id: int) -> None:
    for e in reversed(pending.entries(chat_id)):
        if (e["act"] or {}).get("kind") == "dup":
            pending.remove(chat_id, e["sid"])
            return


async def cmd_dups(event):
    groups = db.find_archive_dups()
    if not groups:
        await event.respond("✅ Дублей в архиве нет.", buttons=main_keyboard())
        return
    extra = sum(len(its) - 1 for _, its in groups)
    await event.respond(
        f"🔁 В архиве {len(groups)} групп дублей — {extra} лишних постов.\n"
        "Удаляй лишние кнопкой «🗑» (с подтверждением и отменой).",
        buttons=[[Button.inline("❌ Закрыть", data="dismiss")]],
    )
    shown = 0
    for _tok, its in groups:
        shown += 1
        if shown > 5:
            await event.respond(f"…и ещё {len(groups) - 5} групп(ы) — отправь /dups снова.")
            break
        lines = [f"🔁 Дубли ({len(its)} постов, «{its[0]['content_type']}»):"]
        rows = []
        for it in its:
            emoji = theme_icon(it["category"], "📦")
            lock = " 🔒" if it["locked"] else ""
            lines.append(f"#{it['id']} {emoji}{it['category']}{lock} — {it['summary'][:80]}")
            rows.append(
                [
                    Button.inline(f"👀 #{it['id']}", data=f"view:{it['id']}"),
                    Button.inline(f"🗑 #{it['id']}", data=f"del:{it['id']}"),
                ]
            )
        rows.append([Button.inline("❌ Скрыть", data="dismiss")])
        await event.respond("\n".join(lines), buttons=rows)


async def cmd_help(event):
    sender_id = getattr(event, "sender_id", None)
    is_admin = sender_id is not None and int(sender_id) == int(_cached_owner() or 0)
    text = (
        "🤖 Как пользоваться:\n\n"
        "• Пересылай мне посты, сообщения, альбомы — сохранятся в категорию по содержанию\n"
        "• 📂 Категории — все сохранённое по темам (папки и подпапки)\n"
        "• В карточке поста: 📂 переместить · ✏️ название · 🏷 теги · 💬 комментарий · 🔒 замок\n"
        "• 🔍 Поиск — по тексту и дате\n"
        "• 🗑 Корзина — недавно удалённое, можно вернуть\n"
        "• 🕘 Последние — свежие сохранения\n"
        "• 📊 Статистика — сколько всего сохранено\n"
        "• ⚙️ Настройки — автозамок, распознавание фото и голоса, быстрые действия\n"
        "• 🔁 Дубли — найти и убрать повторяющиеся вложения\n"
        "• 💾 Экспорт — выгрузить архив в файл\n\n"
        "Команды:\n"
        "• /categories — папки и категории\n"
        "• /recent [N] — последние сохранения (по умолчанию 10)\n"
        "• /items <id | a-b | id1,id2> — показать посты по id / диапазону / перечню\n"
        "• /search <текст> — поиск по содержимому\n"
        "• /archive <дни> — перенести посты старше N дней в «Архив»\n"
        "• /dups — найти и убрать повторяющиеся вложения\n"
        "• /stats — статистика\n"
        "• /settings — настройки\n"
        "• /export — скачать архив\n"
        "• /help — эта справка\n\n"
        "Категории создаёт искусственный интеллект автоматически, "
        "так что они могут быть любыми — по содержанию, а не по типу файла."
    )
    if is_admin:
        text += (
            "\n\n🛠 Администратору:\n"
            "• /stats — здесь дополнительно видна статистика по всем пользователям"
        )
    await event.respond(
        text,
        buttons=main_keyboard(),
    )


async def cmd_categories(event):
    tree = db.get_tree()
    if not tree:
        await event.respond("Пока ничего не сохранено. Отправь мне что-нибудь!", buttons=main_keyboard())
        return
    await event.respond("📂 Папки и категории:", buttons=root_categories_keyboard(tree))


def _find_folder(root_list: list[dict], path: str) -> dict | None:
    cur = root_list
    node = None
    for seg in path.split("/"):
        node = next((n for n in cur if n["type"] == "folder" and n["name"] == seg), None)
        if not node:
            return None
        cur = node["children"]
    return node


async def _nav_send(event, text, buttons=None):
    """Показать страницу: если событие пришло из 'откреплённого' item-view сообщения —
    шлём НОВОЕ сообщение (чтобы не переписывать reply к исходному посту), иначе — edit."""
    try:
        key = (getattr(event, "chat_id", None), getattr(event, "message_id", None))
        if key in _detached_views:
            _detached_views.discard(key)
            try:
                await event.delete()
            except Exception:
                pass
            try:
                await event.respond(text, buttons=buttons)
                return
            except Exception:
                pass
    except Exception:
        pass
    try:
        await event.edit(text, buttons=buttons)
    except Exception:
        try:
            await event.respond(text, buttons=buttons)
        except Exception:
            pass


async def show_root_page(event):
    tree = _tree_with_tags()
    if not tree:
        await _nav_send(event, "Пока ничего не сохранено.")
        return
    await _nav_send(event, "📂 Папки и категории:", buttons=root_categories_keyboard(tree))


def _fmt_short(s: str, n: int = 34) -> str:
    s = strip_markdown(s or "")
    return s[:n] + ("…" if len(s) > n else "")


_undo_token = 0


def _set_undo(chat_id: int, payload: dict):
    """Сохраняет состояние отмены на 12 секунд."""
    global _undo_token
    _undo_token += 1
    token = _undo_token
    undo_data[chat_id] = {"payload": payload, "token": token}
    ent = undo_data[chat_id]

    async def expire():
        await asyncio.sleep(12)
        cur = undo_data.get(chat_id)
        if cur and cur.get("token") == token:
            undo_data.pop(chat_id, None)

    ent["task"] = asyncio.get_event_loop().create_task(expire())


def _undo_button():
    return [[Button.inline("↩️ Отменить", data="undo_action")]]


async def show_trash(event, note: str | None = None):
    items = db.get_trashed()
    sel = trash_sel.setdefault(event.chat_id, set())
    sel &= {it["id"] for it in items}
    hint = (
        "\n\nОтметь ✔ посты галочками (нажимай на строку) и нажми нужную кнопку:\n"
        "• ♻️ Вернуть выбранные — вернуть отмеченные посты в архив\n"
        "• 🗑 Удалить выбранные — стереть отмеченные безвозвратно\n"
        "• ♻️ Вернуть всё / 🧹 Удалить всё — для ВСЕЙ корзины разом\n\n"
        "Пока пост в корзине — он не удалён, его можно вернуть."
    )
    if not items:
        head = "🗑 Корзина пуста.\n\nСюда попадают удалённые посты. Отсюда можно вернуть их в архив или удалить навсегда."
        if note:
            head = f"{note}\n\n{head}"
        await event.edit(head, buttons=[[Button.inline("◀️ Назад", data="back_to_cats")]])
        return
    head = f"🗑 Корзина ({len(items)})"
    if note:
        head += f"\n\n{note}"
    head += "\n\nОтметь посты галочками и нажми кнопку ниже."
    await event.edit(head, buttons=trash_keyboard(items, sel))


async def show_locked_list(event):
    folders = db.get_locked_folders()
    cats = db.get_locked_categories()
    items = db.get_locked_items()
    tree = db.get_tree()
    all_cats = {x["category"] for x in db.get_categories()}

    folder_counts = {}
    existing_folders = []
    for f in folders:
        node = _find_folder(tree, f)
        if node is not None:
            existing_folders.append(f)
            folder_counts[f] = node["count"]
    existing_cats = [c for c in cats if c["category"] in all_cats]

    st = _lu_state(event.chat_id)
    selecting = st["mode"]

    if not existing_folders and not existing_cats and not items:
        _lu_clear(event.chat_id)
        await event.edit("🔒 Заблокированных элементов нет — всё свободно 🎉",
                         buttons=[Button.inline("◀️ Назад", data="back_to_cats")])
        return

    ls = []
    ls.append("🔒 Заблокированные элементы" if not selecting else "🔒 Выбери, что разблокировать:")
    if existing_folders:
        ls.append(f"📁 Папки ({len(existing_folders)})")
    if existing_cats:
        ls.append(f"📂 Категории ({len(existing_cats)})")
    if items:
        ls.append(f"📌 Элементы ({len(items)})")
        if len(items) > 50:
            ls.append(f"   показано {50} из {len(items)}")

    rows = []
    for f in existing_folders:
        mark = "☑️ " if selecting and f in st["folders"] else ""
        data = f"lctog:fold:{f}" if selecting else f"lcfold:{f}"
        rows.append([Button.inline(f"{mark}📁 {f} ({folder_counts[f]})", data=data)])
    for c in existing_cats:
        mark = "☑️ " if selecting and c["category"] in st["cats"] else ""
        icon = theme_icon(c["category"], "📦")
        rows.append([Button.inline(
            f"{mark}{icon} {c['category']} — {c['count']} постов",
            data=f"lctog:cat:{c['category']}" if selecting else f"lccat:{c['category']}",
        )])
    for it in items[:50]:
        mark = "☑️ " if selecting and it["id"] in st["items"] else ""
        icon = theme_icon(it["category"], "▶️")
        rows.append([Button.inline(
            f"{mark}{icon} ID {it['id']} {_fmt_short(it['summary'])}",
            data=f"lctog:item:{it['id']}" if selecting else f"lcitem:{it['id']}",
        )])

    if selecting:
        n_sel = len(st["items"]) + len(st["cats"]) + len(st["folders"])
        rows.append([Button.inline(f"🔓 Разблокировать выбранное ({n_sel})", data="lcunlock")])
        rows.append([Button.inline("✨ Снять выделение", data="lcclear"), Button.inline("✅ Готово", data="lcsel")])
    else:
        rows.append([Button.inline("☑️ Выбрать", data="lcsel"), Button.inline("🔓 Разблокировать всё", data="lcall")])
    rows.append([Button.inline("◀️ Назад", data="back_to_cats")])

    try:
        await event.edit("\n".join(ls), buttons=rows)
    except Exception as e:
        logger.error("Не удалось показать список заблокированных: %s", e)


async def show_folder_page(event, path: str):
    path = (path or "").strip("/")
    if not path:
        await show_root_page(event)
        return
    node = _find_folder(_tree_with_tags(), path)
    if node is None:
        await _nav_send(event, "Папка не найдена.")
        return
    parent = path.rsplit("/", 1)[0] if "/" in path else ""
    lock = " 🔒" if db.folder_locked(path) else ""
    tags = _tags_line(db.get_folder_tags(path))
    emoji = theme_icon(path, "📂")
    await _nav_send(event, f"{tags}{emoji} {lock}{path}  ({node['count']}):", buttons=folder_page_keyboard(node, parent))


async def show_cf_selection(event):
    st = cf_sel.get(event.chat_id)
    if not st:
        await event.answer("Сначала открой режим выбора", alert=True)
        return
    root = st["root"]
    tree = _tree_with_tags()
    if root:
        node = _find_folder(tree, root)
        children = node["children"] if node else []
    else:
        children = tree
    if not children:
        await event.edit("Здесь нечего выбирать.")
        return
    await event.edit(
        "🗂 Выбор категорий и папок (мультивыбор):\n\nУдаление переносит все посты внутри в корзину.",
        buttons=cf_choice_keyboard(children, st["sel"], root),
    )


# ---------------------------- Теги ----------------------------

def _tags_line(tags: list[dict]) -> str:
    return "".join(t["icon"] for t in (tags or []))


def _tree_with_tags():
    tree = db.get_tree()

    def go(nodes):
        for n in nodes:
            if n["type"] == "folder":
                n["tags"] = db.get_folder_tags(n["path"])
            else:
                n["tags"] = db.get_category_tags(n["category"])
            if n.get("children"):
                go(n["children"])

    go(tree)
    return tree


def _tag_label(t: dict) -> str:
    return f"{t['icon']} {t['name']}" if t.get("name") else t["icon"]


async def show_tags_section(event):
    tags = db.get_tags()
    sel = tag_filter.setdefault(event.chat_id, set())
    counts = db.tag_counts()
    head = "🏷 Теги\nВыбери несколько — покажу всё, что помечено ЛЮБЫМ из выбранных:"
    await event.edit(head, buttons=tags_section_keyboard(tags, sel, counts))


async def show_tags_result(event):
    sel = tag_filter.get(event.chat_id, set())
    if not sel:
        await event.answer("Не выбрано ни одного тега", alert=True)
        await show_tags_section(event)
        return
    tags = {t["id"]: t for t in db.get_tags()}
    names = " ".join(_tag_label(tags[i]) for i in list(sel)[:6])
    items = db.items_by_tags(list(sel))
    cats = db.cats_by_tags(list(sel))
    folders = db.folders_by_tags(list(sel))
    tree = _tree_with_tags()
    valid_cats = {c["category"] for c in db.get_categories()}
    existing_cats = [c for c in cats if c["category"] in valid_cats]
    existing_folders = []
    folder_counts = {}
    for f in folders:
        node = _find_folder(tree, f)
        if node is not None:
            existing_folders.append(f)
            folder_counts[f] = node["count"]

    ls = [f"🏷 «{names}»"]
    ls.append(f"📂 Категории ({len(existing_cats)}), 📁 Папки ({len(existing_folders)}), 📌 Посты ({len(items)})")
    ls.append("")

    rows = []
    for f in existing_folders:
        rows.append([Button.inline(f"📁 {f} ({folder_counts[f]})", data=f"lcfold:{f}")])
    for c in existing_cats:
        rows.append([Button.inline(f"{theme_icon(c['category'], '📦')} {c['category']} — {c['count']} постов", data=f"lccat:{c['category']}")])
    for it in items[:50]:
        icon = theme_icon(it["category"], "▶️")
        lock = " 🔒" if it["locked"] else ""
        rows.append([Button.inline(
            f"{_tags_line(db.get_item_tags(it['id']))}{icon}{lock} ID {it['id']} {_fmt_short(it['summary'])}",
            data=f"lcitem:{it['id']}",
        )])
    rows.append([Button.inline("◀️ Назад", data="tags_section")])
    if not rows:
        rows.append([Button.inline("◀️ Назад", data="tags_section")])
    await event.edit("\n".join(ls), buttons=rows)


async def show_tags_manage(event):
    tags = db.get_tags()
    counts = db.tag_counts()
    await event.edit("⚙️ Управление тегами:", buttons=manage_tags_keyboard(tags, counts))


async def show_tag_actions(event, tag_id: int):
    await event.edit("⚙️ Действия с тегом:", buttons=manage_tag_actions_keyboard(tag_id))


async def show_tag_editor(event, kind: str, target):
    tags = db.get_tags()
    if kind == "item":
        selected = {t["id"] for t in db.get_item_tags(int(target))}
        ctrl = f"ittog:{target}:"
        newd = f"itnew:{target}"
        back = f"view:{target}"
        head = f"🏷 Теги поста #{target}"
    elif kind == "fold":
        selected = {t["id"] for t in db.get_folder_tags(target)}
        ctrl = f"fttog|{target}|"
        newd = f"ftnew|{target}"
        back = f"fold|{target}"
        head = f"🏷 Теги папки «{target}»"
    elif kind == "sel":
        entry = _sel_entry(event.chat_id, target)
        selected = set()
        for s in entry["ids"]:
            it = db.get_item(int(s))
            if it:
                selected |= {t["id"] for t in db.get_item_tags(int(s))}
        ctrl = f"seltog|{target}|"
        newd = f"seltnew|{target}"
        back = f"selmode|{target}"
        head = f"🏷 Теги выделенных постов ({len(entry['ids'])})"
    elif kind == "cf":
        st = cf_sel.get(event.chat_id)
        targets = list(st["sel"]) if st else []
        selected = set()
        for key in targets:
            k, _, n = key.partition("|")
            if k == "cat":
                selected |= {t["id"] for t in db.get_category_tags(n)}
            else:
                selected |= {t["id"] for t in db.get_folder_tags(n)}
        ctrl = f"cftog|{target}|"
        newd = f"cfnew|{target}"
        back = f"cf_selmode|{target}"
        head = f"🏷 Теги выбранного ({len(targets)})"
    else:
        selected = {t["id"] for t in db.get_category_tags(target)}
        ctrl = f"cttog|{target}|"
        newd = f"ctnew|{target}"
        back = f"cat:{target}"
        head = f"🏷 Теги категории «{target}»"
    await event.edit(head, buttons=tag_editor_keyboard(tags, selected, newd, ctrl, back))


def _start_tag_create(chat_id: int, kind: str, target):
    tag_create[chat_id] = {"kind": kind, "target": target}


async def _tag_apply_to_target(chat_id: int, tag_id: int):
    flow = tag_create.get(chat_id)
    if not flow or not flow.get("kind"):
        return
    if flow["kind"] == "item":
        ids = {t["id"] for t in db.get_item_tags(int(flow["target"]))}
        ids.add(tag_id)
        db.set_item_tags(int(flow["target"]), ids)
    elif flow["kind"] == "fold":
        ids = {t["id"] for t in db.get_folder_tags(flow["target"])}
        ids.add(tag_id)
        db.set_folder_tags(flow["target"], ids)
    elif flow["kind"] == "sel":
        entry = _sel_entry(chat_id, flow["target"])
        for s in entry["ids"]:
            iid = int(s)
            ids = {t["id"] for t in db.get_item_tags(iid)}
            ids.add(tag_id)
            db.set_item_tags(iid, ids)
    elif flow["kind"] == "cf":
        st = cf_sel.get(chat_id)
        if not st:
            return
        for key in list(st["sel"]):
            k, _, n = key.partition("|")
            if k == "cat":
                ids = {t["id"] for t in db.get_category_tags(n)}
                ids.add(tag_id)
                db.set_category_tags(n, ids)
            else:
                ids = {t["id"] for t in db.get_folder_tags(n)}
                ids.add(tag_id)
                db.set_folder_tags(n, ids)
    else:
        ids = {t["id"] for t in db.get_category_tags(flow["target"])}
        ids.add(tag_id)
        db.set_category_tags(flow["target"], ids)


def _tag_editor_back(chat_id: int) -> str:
    flow = tag_create.get(chat_id)
    if not flow or not flow.get("kind"):
        return "tags_section"
    if flow["kind"] == "item":
        return f"view:{flow['target']}"
    if flow["kind"] == "fold":
        return f"fold|{flow['target']}"
    if flow["kind"] == "sel":
        return f"selmode|{flow['target']}"
    if flow["kind"] == "cf":
        return f"cf_selmode|{flow['target']}"
    return f"cat:{flow['target']}"


async def cmd_stats(event):
    sender_id = getattr(event, "sender_id", None)
    is_admin = sender_id is not None and int(sender_id) == int(_cached_owner() or 0)
    if is_admin:
        try:
            a = db.admin_stats()
            a_lines = [f"👥 Уникальных пользователей: {a['users']}"]
            a_lines.append(f"🗃 Всего постов в архиве: {a['total_posts']}")
            for uid, cnt in sorted(a["per_user"].items(), key=lambda x: -x[1]):
                a_lines.append(f"  • {uid}: {cnt}")
            lines = a_lines + ["", ""]
        except Exception:
            lines = []
    else:
        lines = []
    stats = db.get_stats()
    if stats["total"] == 0:
        if not lines:
            await event.respond("Пока ничего не сохранено.", buttons=main_keyboard())
            return
    if lines:
        lines.append(f"📊 Твои посты: {stats['total']}")
    else:
        lines.append(f"📊 Всего постов: {stats['total']}")
    for ct, cnt in sorted(stats["by_type"].items(), key=lambda x: -x[1]):
        lines.append(f"  {ct}: {cnt}")
    lines.append("")
    lines.append("📁 По категориям:")
    cat_rows = []
    for c in db.get_categories():
        cat_rows.append((c["category"], db.get_total_items(c["category"])))
    for name, cnt in sorted(cat_rows, key=lambda x: -x[1]):
        if cnt:
            lines.append(f"  {theme_icon(name, '📦')} {name}: {cnt}")
    await event.respond("\n".join(lines), buttons=main_keyboard())


def _parse_items_arg(args) -> list:
    """Разбирает аргумент: '12', '5-9', '3,7,12' -> список id (по порядку)."""
    ids = []
    text = (" ".join(args) if isinstance(args, list) else str(args or "")).strip()
    if not text:
        return ids
    for part in text.replace(",", " ").split():
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            try:
                a, b = part.split("-", 1)
                lo, hi = int(a), int(b)
            except Exception:
                continue
            if lo > hi:
                lo, hi = hi, lo
            ids.extend(range(lo, hi + 1))
        else:
            try:
                ids.append(int(part))
            except Exception:
                continue
    return ids


async def cmd_items(event, args):
    ids = _parse_items_arg(args)
    if not ids:
        await event.respond(
            "Используй: /items <id> — один пост\n"
            "/items <a-b> — диапазон\n"
            "/items <id1, id2, ...> — перечень\n"
            "Например: /items 5-12 или /items 3,7,9"
        )
        return
    posts = db.get_items_by_ids(ids)
    if not posts:
        await event.respond("Посты с такими id не найдены.", buttons=main_keyboard())
        return
    found = [p["id"] for p in posts]
    lines = [f"🗂 Найдено постов: {len(posts)}"]
    if len(posts) != len(ids):
        missing = [i for i in ids if i not in found]
        lines.append(f"⚠️ Не найдены id: {', '.join(map(str, missing))}")
    lines.append("")
    for p in posts:
        lines.append(
            f"#{p['id']} · {theme_icon(p['category'], '📦')}{p['category']} · {p['content_type']} · msg {p['message_id']}\n   {p['summary'][:80]}"
        )
    await event.respond("\n".join(lines), buttons=main_keyboard())


def _plural_posts(n: int) -> str:
    n = abs(int(n)) % 100
    if 11 <= n % 100 <= 19:
        return "постов"
    d = n % 10
    if d == 1:
        return "пост"
    if 2 <= d <= 4:
        return "поста"
    return "постов"


def _settings_text(st: dict) -> str:
    eff_auto = st["auto_lock"] == "1" and any(
        st[k] == "1" for k in ("lock_move", "lock_rename", "lock_create")
    )
    auto = "вкл" if eff_auto else "выкл"
    vision = "вкл" if st["photo_vision"] == "1" else "выкл"
    qact = "вкл" if st["quick_actions"] == "1" else "выкл"
    cln = "вкл" if st.get("cleanup_empty", "1") == "1" else "выкл"
    ao_every = st.get("auto_order_posts", "10")
    ao_txt = "выкл" if ao_every in ("", "0") else f"каждые {ao_every} {_plural_posts(ao_every)}"
    sf = "вкл" if st.get("subfolders_enabled", "1") == "1" else "выкл"
    sfm = st.get("subfolders_min", "5")
    sfa = "вкл" if st.get("subfolders_in_auto", "1") == "1" else "выкл"
    av = "вкл" if st.get("audio_vision", "1") == "1" else "выкл"
    dd = "вкл" if st.get("dedup_check", "1") == "1" else "выкл"
    wd = "вкл" if st.get("weekly_digest", "1") == "1" else "выкл"
    bt = "вкл" if st.get("backup_tg_daily", "1") == "1" else "выкл"
    bpe = st.get("backup_every_posts", "5")
    bpe_txt = "выкл" if bpe in ("", "0") else f"каждые {bpe} {_plural_posts(bpe)}"
    ahb = "вкл" if st.get("auto_heal_broken", "0") == "1" else "выкл"
    le = "вкл" if st.get("link_enrich", "1") == "1" else "выкл"
    arp = "вкл" if st.get("auto_recover_posts", "1") == "1" else "выкл"
    rows = [
        (f"🔒 Автозамок", auto, "ставится после ручных действий (перемещение, переименование, создание), чтобы автопорядок и деревья их не сдвигали"),
        (f"🖼️ Распознавание фото", vision, "ИИ описывает картинку и по описанию подбирает категорию (арты, мемы…)"),
        (f"⚡️ Быстрые действия", qact, "кнопки «Переместить / Название / Теги» сразу после сохранения"),
        (f"🧹 Чистка пустых", cln, "авто-порядок удаляет категории и папки без единого поста"),
        (f"⚡️ Авто-порядок", ao_txt, "объединяет дубли и раскладывает по папкам после N новых постов. «0» или «выкл» — только по кнопке"),
        (f"🧩 Подпапки", sf, "если в категории копится N похожих постов (например, несколько игр про PS5), бот создаст подпапку и перенесёт их туда"),
        (f"🧩 Мин. постов в подпапке", sfm, "минимальная высота ветки, при которой создаётся подпапка"),
        (f"🧩 Подпапки в авто-порядке", sfa, "включать эту проверку при каждом авто-порядке (не только по кнопке)"),
        (f"🎙️ Распознавание голосовых", av, "бот распознаёт голосовые и аудио через ИИ, чтобы категоризировать их"),
        (f"🔁 Проверка дублей", dd, "при сохранении проверяется, что вложение уже есть в архиве"),
        (f"📬 Дайджест за неделю", wd, "раз в неделю бот пришлёт статистику новых постов по категориям"),
        (f"🗄 Копия экспорта в TG", bt, "раз в день присылать полный экспорт сюда (страховка от потери архива)"),
        (f"🗄 Копия каждые N постов", bpe_txt, "присылать полный экспорт после каждых N сохранённых постов. «0» — выкл; ежедневная копия при отсутствии новых постов автоматически пропускается"),
        (f"🗑 Авто-лечение битых", ahb, "при старте сверяет оригинал каждого поста с исходным сообщением в чате; не совпавшие переносит в корзину (полезно после переноса БД)"),
        (f"🔗 Распознавать ссылки", le, "уточняет у источника название/описание по ссылке (YouTube и др.), чтобы точнее определить категорию и подпись"),
        (f"🔄 Авто-восстановление", arp, "при старте, если архив оказался пуст, предложит переслать последний файл «tg_saver_export.json» для восстановления истории (бот не может читать историю чата сам)"),
    ]
    lines = [
        "**⚙️ Настройки**",
        "",
        "Нажимай на кнопки ниже — они переключают параметры.",
        "",
    ]
    for name, val, note in rows:
        lines.append(f"▸ {name} — **{val}**")
    lines += ["", "**Пояснения:**", ""]
    for name, val, note in rows:
        lines.append(f"• {name} — {note}")
    return "\n".join(lines)


async def show_settings(event):
    st = settings_state()
    await event.edit(_settings_text(st), buttons=settings_keyboard(st))


async def cmd_settings(event):
    await event.respond(_settings_text(settings_state()), buttons=settings_keyboard(settings_state()))


async def _do_search(event, query: str):
    if not query.strip():
        await event.respond("Что ищем? Отправь текст для поиска.")
        return
    items = db.search_items(query.strip())
    if not items:
        await event.respond("Ничего не найдено. Ищу по тексту, описаниям и тегам.", buttons=main_keyboard())
        return
    lines = ["🔍 Результаты (текст, описание, теги):", ""]
    rows = []
    for item in items[:30]:
        tags = _tags_line(db.get_item_tags(item["id"]))
        emoji = theme_icon(item["category"], "📦")
        summary = item["summary"] if item["summary"] else "—"
        lock = " 🔒" if item.get("locked") else ""
        rows.append([Button.inline(
            f"{tags}{emoji}{lock} ID {item['id']} {_fmt_short(summary, 40)}",
            data=f"lcitem:{item['id']}",
        )])
    extra = len(items) - 30
    if extra > 0:
        lines.append(f"… и ещё {extra}.")
    try:
        await event.respond("\n".join(lines), buttons=rows or [[Button.inline("◀️ Назад", data="back_to_cats")]])
    except Exception:
        await event.respond("🔍 Результаты:", buttons=rows or [[Button.inline("◀️ Назад", data="back_to_cats")]])


async def cmd_search(event, args):
    await _do_search(event, " ".join(args))


async def _do_recent(event, n: int):
    items = db.recent_items(n)
    if not items:
        await event.respond("Пока ничего не сохранено.", buttons=main_keyboard())
        return
    lines = ["🕘 Последние посты — нажми на строку, чтобы открыть и уточнить:", ""]
    rows = []
    for it in items:
        tags = _tags_line(db.get_item_tags(it["id"]))
        emoji = theme_icon(it["category"], "📦")
        lock = " 🔒" if it.get("locked") else ""
        date_txt = ""
        if it.get("created_at"):
            try:
                date_txt = " · " + "-".join(reversed(str(it["created_at"])[:10].split("-")))
            except Exception:
                date_txt = ""
        rows.append([Button.inline(
            f"{tags}{emoji}{lock} ID {it['id']}{date_txt} · {it['category']}: {_fmt_short(it['summary'], 28)}",
            data=f"lcitem:{it['id']}",
        )])
    rows.append([Button.inline("◀️ Назад", data="back_to_cats")])
    await event.respond("\n".join(lines), buttons=rows)


def _parse_date_filters(raw: str) -> tuple | None:
    """Возвращает (start_dt, end_dt) в UTC по тексту пользователя. None — не распознано."""
    s = (raw or "").strip().lower()
    now = datetime.utcnow()

    def day_range(d: datetime):
        start = datetime(d.year, d.month, d.day)
        end = start + timedelta(days=1) - timedelta(seconds=1)
        return start, end

    if s in ("вчера", "вчерашний", "вчерашний день"):
        return day_range(now - timedelta(days=1))
    if s in ("сегодня", "сейчас", "current", "нынешний"):
        return day_range(now)
    if s in ("неделя", "за неделю", "7 дней", "неделю"):
        return now - timedelta(days=7), now
    if s in ("месяц", "за месяц", "30 дней", "месяц назад"):
        return now - timedelta(days=30), now
    if s in ("год", "за год", "365 дней"):
        return now - timedelta(days=365), now
    if s in ("3 месяца", "квартал", "90 дней"):
        return now - timedelta(days=90), now
    if s in ("6 месяцев", "полгода", "180 дней"):
        return now - timedelta(days=180), now

    m = re.match(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})(?:\s*-\s*(\d{1,2})\.(\d{1,2})\.(\d{4}))?$", s)
    if m:
        d1 = datetime(int(m.group(3)), int(m.group(2)), int(m.group(1)))
        if m.group(4) is None:
            start, end = day_range(d1)
        else:
            d2 = datetime(int(m.group(6)), int(m.group(5)), int(m.group(4)))
            start, end = min(d1, d2), max(d1, d2)
            end = end + timedelta(days=1) - timedelta(seconds=1)
        return start, end

    m = re.match(r"^(\d{1,2})\.(\d{1,2})(?:\s*-\s*(\d{1,2})\.(\d{1,2}))?\.(\d{4})$", s)
    if m:
        year = int(m.group(5))
        d1 = datetime(year, int(m.group(2)), int(m.group(1)))
        if m.group(3) is None:
            start, end = day_range(d1)
        else:
            d2 = datetime(year, int(m.group(4)), int(m.group(3)))
            start, end = min(d1, d2), max(d1, d2)
            end = end + timedelta(days=1) - timedelta(seconds=1)
        return start, end
    return None


_RU_MONTHS_SHORT = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"]


def _fmt_date_ru(d: datetime) -> str:
    return f"{d.day} {_RU_MONTHS_SHORT[d.month - 1]}"


def _fmt_date_period(start: datetime, end: datetime) -> str:
    if end.date() == start.date():
        return f"{_fmt_date_ru(start)} {start.year}"
    if start.year == end.year:
        return f"{_fmt_date_ru(start)} — {_fmt_date_ru(end)} {start.year}"
    return f"{_fmt_date_ru(start)} {start.year} — {_fmt_date_ru(end)} {end.year}"


async def _do_date_search(event, raw: str):
    parsed = _parse_date_filters(raw)
    if not parsed:
        await event.respond(
            "❌ Не понял дату. Примеры: «вчера», «неделя», «01.09.2026», "
            "«01.09-10.09.2026» (в выводе покажу «1 сен — 10 сен 2026»).",
            buttons=[[Button.inline("🕐 Ещё раз", data="search_date")], [Button.inline("❌ Отмена", data="search_cancel")]],
        )
        return
    start, end = parsed
    dlabel = _fmt_date_period(start, end)
    items = db.search_items_between(start, end)
    if not items:
        await event.respond(
            f"🕐 За {dlabel} ничего не найдено.",
            buttons=[[Button.inline("❌ Отмена", data="search_cancel")], [Button.inline("📂 Назад", data="back_to_cats")]],
        )
        return
    lines = [f"🕐 Посты за {dlabel} ({len(items)}):", ""]
    rows = []
    for item in items[:30]:
        tags = _tags_line(db.get_item_tags(item["id"]))
        emoji = theme_icon(item["category"], "📦")
        summary = item["summary"] if item["summary"] else "—"
        lock = " 🔒" if item.get("locked") else ""
        rows.append([Button.inline(
            f"{tags}{emoji}{lock} ID {item['id']} {_fmt_short(summary, 40)}",
            data=f"lcitem:{item['id']}",
        )])
    extra = len(items) - 30
    if extra > 0:
        lines.append(f"… и ещё {extra}.")
    await event.respond("\n".join(lines), buttons=rows or [[Button.inline("◀️ Назад", data="back_to_cats")]])


async def cmd_archive(event, days: int):
    if days <= 0:
        await event.respond("Число дней должно быть больше 0.")
        return
    n = db.archive_older_than(days)
    await event.respond(
        f"📦 В «Архив» перенесено постов: {n} (старше {days} дн.)." if n else f"✅ Постов старше {days} дн. нет.",
        buttons=main_keyboard(),
    )


async def cmd_export(event):
    dump = db.export_json()
    raw = json.dumps(dump, ensure_ascii=False, indent=1).encode("utf-8")
    try:
        await client.send_file(event.chat_id, file=raw, file_name="tg_saver_export.json", caption="💾 Экспорт архива")
        uid = db.current_user_id() or getattr(event, "sender_id", None)
        try:
            await _push_github_file(_github_backup_path(uid), raw)
        except Exception:
            pass
        _maybe_forward_backup_to_channel(raw, reason="ручной экспорт", user_id=uid)
    except Exception as e:
        logger.exception("Не удалось выгрузить экспорт: %s", e)
        await event.respond("❌ Не удалось выгрузить экспорт.", buttons=main_keyboard())


async def _prompt(event, text: str):
    return await event.respond(text, buttons=[Button.inline("❌ Отмена", data="cancel_action")])


class _MsgProxy:
    def __init__(self, chat_id: int, msg_id: int):
        self.chat_id = chat_id
        self._msg_id = msg_id

    async def edit(self, text: str = "", **kw):
        return await client.edit_message(self.chat_id, self._msg_id, text, **kw)


async def _finish_action(event, act: dict, call, *args):
    mid = act.get("msg_id")
    if mid:
        try:
            await call(_MsgProxy(event.chat_id, mid), *args)
            return
        except Exception as e:
            logger.warning("Не удалось обновить промпт после действия: %s", e)
    try:
        await event.respond("✅ Готово.")
    except Exception:
        pass


async def _route_edit(proxy, data_s: str):
    """Вернуть экран по callback-метке (используется после действий)."""
    if data_s == "tags_section":
        await show_tags_section(proxy)
    elif data_s == "tags_manage":
        await show_tags_manage(proxy)
    elif data_s.startswith("view:"):
        await show_item_view(proxy, int(data_s.split(":", 1)[1]))
    elif data_s.startswith("fold|"):
        await show_folder_page(proxy, data_s.split("|", 1)[1])
    elif data_s.startswith("selmode|"):
        await show_selection_page(proxy, proxy.chat_id, data_s.split("|", 1)[1], 0)
    elif data_s.startswith("cat:"):
        await show_category_page(proxy, data_s.split(":", 1)[1], 0)


async def _finish_back(event, act: dict, back_data: str):
    mid = act.get("msg_id")
    if mid:
        try:
            await _route_edit(_MsgProxy(event.chat_id, mid), back_data)
            return
        except Exception as e:
            logger.warning("Не удалось обновить промпт после действия: %s", e)
    try:
        await event.respond("✅ Готово.")
    except Exception:
        pass


async def on_new_message(event):
    msg = event.message
    if not msg:
        return
    sender = await event.get_sender()
    if sender is not None and getattr(sender, "is_bot", False):
        return
    logger.info("✉️ Новое сообщение: chat=%s user=%s text=%r", msg.chat_id, getattr(sender, "id", None), (msg.text or "")[:60])
    _activate_user(getattr(sender, "id", None))
    try:
        if getattr(sender, "id", None):
            asyncio.create_task(_restore_if_empty_background(sender.id))
    except Exception:
        pass

    text = (msg.text or "").strip()

    if text.startswith("/"):
        cmd = (text.split()[0] or "").lower()
        if cmd == "/start":
            await cmd_start(event)
        elif cmd == "/help":
            await cmd_help(event)
        elif cmd == "/categories":
            await cmd_categories(event)
        elif cmd == "/stats":
            await cmd_stats(event)
        elif cmd == "/items":
            args = text.split()[1:]
            await cmd_items(event, args)
        elif cmd == "/search":
            args = text.split()[1:]
            if not args:
                await event.respond("Используй: /search <текст>", buttons=main_keyboard())
            else:
                await _do_search(event, " ".join(args))
        elif cmd == "/settings":
            await cmd_settings(event)
        elif cmd == "/export":
            await cmd_export(event)
        elif cmd == "/recent":
            args = text.split()[1:]
            n = 10
            if args and args[0].isdigit():
                n = min(int(args[0]), 50)
            await _do_recent(event, n)
        elif cmd == "/archive":
            args = text.split()[1:]
            if not args or not args[0].isdigit():
                await event.respond("Используй: /archive <кол-во дней> — старые посты уйдут в «Архив».", buttons=main_keyboard())
            else:
                await cmd_archive(event, int(args[0]))
        elif cmd == "/dups":
            await cmd_dups(event)
        return

    if text == REPLY_BUTTONS["categories"]:
        await cmd_categories(event)
        return
    if text == REPLY_BUTTONS["stats"]:
        await cmd_stats(event)
        return
    if text == REPLY_BUTTONS["settings"]:
        await cmd_settings(event)
        return
    if text == REPLY_BUTTONS["help"]:
        await cmd_help(event)
        return
    if text == REPLY_BUTTONS["export"]:
        await cmd_export(event)
        return
    if text == REPLY_BUTTONS["search"]:
        pending_search[msg.chat_id] = True
        await event.respond(
            "🔍 Что ищем? Напиши текст поиска. Или нажми «🕐 По дате», чтобы искать по дате.",
            buttons=[
                [Button.inline("🕐 По дате", data="search_date")],
                [Button.inline("❌ Отмена", data="search_cancel")],
            ],
        )
        return

    if text == REPLY_BUTTONS["recent"]:
        await _do_recent(event, 10)
        return

    if text.startswith("🔁 Дубли:"):
        await cmd_dups(event)
        return

    if pending_search.get(msg.chat_id) and text:
        mode = pending_search.pop(msg.chat_id, None)
        if mode == "date":
            await _do_date_search(event, text)
        else:
            await _do_search(event, text)
        return

    if text.startswith("📬 Висящих") or text == "📭 Вопросов нет":
        await cmd_pending(event)
        return

    if pending.top(msg.chat_id):
        entry = pending.top(msg.chat_id)
        if (entry["act"] or {}).get("kind") == "dup":
            if not _msg_media(msg) and not getattr(msg, "fwd_from", None):
                replay = entry.get("replay") or {}
                ask_text = replay.get("text") or "Сохранить всё равно?"
                try:
                    await event.respond(
                        ask_text,
                        buttons=[
                            [Button.inline("✅ Да, сохранить", data="dup_save_yes")],
                            [Button.inline("❌ Нет, не надо", data="dup_save_no")],
                        ]
                        + main_keyboard(),
                    )
                except Exception:
                    pass
                return
        else:
            entry = pending.pop(msg.chat_id)
            await _handle_pending_action(event, entry["act"], text)
            return

    if msg.chat and not getattr(msg.chat, "private", True) and not _saved_in_group(msg.chat, getattr(sender, "id", None)):
        return

    if getattr(msg, "document", None) is not None:
        fname = (getattr(msg.file, "name", "") or "").lower()
        mime = (getattr(msg.file, "mime_type", "") or "").lower()
        sz = int(getattr(msg.file, "size", 0) or 0)
        is_candidate = fname.endswith(".json") or "json" in mime or (0 < sz < 5_000_000)
        if is_candidate:
            try:
                raw = await client.download_media(msg, file=bytes)
                dump = json.loads(raw.decode("utf-8"))
                if isinstance(dump, dict) and dump.get("items") is not None:
                    res = db.import_json(dump)
                    await event.respond(
                        f"✅ Импорт завершён: {res['items']} постов, {res['categories']} категорий, {res['tags']} тегов.",
                        buttons=main_keyboard(),
                    )
                    return
                logger.info("JSON-файл не похож на экспорт — сохраню как пост: %s", fname or mime)
            except Exception as e:
                logger.info("JSON не похож на экспорт: %s", e)

    if getattr(msg, "grouped_id", None) is not None:
        return

    await _save_single(client, msg)


async def on_album(event):
    messages = list(event)
    try:
        sender = await event.get_sender()
        logger.info("🖼 Альбом: chat=%s user=%s фото/медиа=%s", messages[0].chat_id, getattr(sender, "id", None), len(messages))
    except Exception:
        pass
    media = []
    texts = []
    for m in messages:
        media.extend(_msg_media(m))
        t = (m.text or "").strip()
        if t and t not in texts:
            texts.append(t)

    if not media and not texts:
        return
    text = "\n\n".join(texts)
    content_type = _primary_content_type(media) if media else "text"
    first = messages[0]
    if first.chat and not getattr(first.chat, "private", True):
        try:
            sender = await event.get_sender()
        except Exception:
            sender = None
        if not _saved_in_group(first.chat, getattr(sender, "id", None)):
            return
        _activate_user(getattr(sender, "id", None))
    else:
        _activate_user(getattr(sender, "id", None))
    processing = await first.reply("⏳ Анализирую...")
    vision_hint = None
    if content_type == "photo" and not _has_meaningful_text(text):
        for m in messages:
            if getattr(m, "photo", None):
                vision_hint = await _describe_media(client, m)
                break
    dedup_note = None
    if db.get_setting("dedup_check", "1") == "1" and _media_uniques(media):
        dupes = db.find_dupes(_media_uniques(media))
        if dupes:
            extra = " ".join(f"#{d['id']} ({d['category']})" for d in dupes[:3])
            dedup_note = f"⚠️ Вложение уже есть в сохранённых: {extra}"
    await _save(
        client,
        chat_id=first.chat_id,
        processing=processing,
        content_type=content_type,
        text=text,
        file_ids=media,
        message_id=first.id,
        media_group_id=str(first.grouped_id),
        source_channel=await _get_source_name(first),
        vision_hint=vision_hint,
        dedup_note=dedup_note,
    )


async def _resend(item):
    refs = []
    for m in item["file_ids"] or []:
        try:
            refs.append(utils.resolve_bot_file_id(m["f"]))
        except Exception:
            continue
    caption = item["original_text"] or None
    return refs, caption


def _origin_media_tokens(item) -> list[str]:
    """Токены вложений поста из БД (file_ids['u']) для сверки с живым сообщением.
    Только 'u' (тип:id) — 'f' (упакованный file_id) зависит от бота-сохранителя и ненадёжен."""
    raw = (item.get("file_ids") or "").strip()
    if not raw or raw == "[]":
        return []
    try:
        arr = json.loads(raw)
    except Exception:
        return []
    out = []
    for x in arr if isinstance(arr, list) else []:
        if not isinstance(x, dict):
            continue
        u = x.get("u")
        if u and u not in out:
            out.append(u)
    return out


def _msg_media_tokens(msg) -> list[str]:
    out = []
    for m in _msg_media(msg):
        u = m.get("u")
        if u and u not in out:
            out.append(u)
    return out


def _norm_text(s: str) -> str:
    s = strip_markdown(s or "")
    s = re.sub(r"https?://\S+", "", s)
    s = re.sub(r"t\.me/[\w+/_]+", "", s)
    s = re.sub(r"@\w+", "", s)
    s = re.sub(r"#\w+", "", s)
    s = s.replace("**", " ").replace("__", " ")
    s = re.sub(r"[*_]", " ", s)
    s = re.sub(r"\s+", " ", s).strip().lower()
    return s


def _origin_matches(item, msg) -> bool:
    """Совпадает ли сообщение чата с сохранённым постом (медиа-токены либо текст).
    Результат True — совпало/нечего сверять, False — точно расходится."""
    if msg is None:
        return False
    exp_toks = _origin_media_tokens(item)
    if exp_toks:
        live_toks = _msg_media_tokens(msg)
        if set(exp_toks) & set(live_toks):
            return True
        if not _norm_text(getattr(msg, "text", "") or ""):
            return False
    a = _norm_text(item.get("original_text") or "")
    b = _norm_text(getattr(msg, "text", "") or "")
    if a or b:
        return bool(a and b and (a[:80] == b[:80] or a in b or b in a))
    return True


async def _origin_messages_checked(client, item) -> list:
    """Сообщения-первоисточники, только если они действительно соответствуют посту."""
    msg_id = item.get("message_id")
    chat_id = item.get("chat_id")
    if not msg_id or not chat_id:
        return []
    gid = (item.get("media_group_id") or "").strip()
    try:
        if gid:
            candidates = [i for i in range(msg_id - 20, msg_id + 21) if i > 0]
            msgs = [m for m in await client.get_messages(chat_id, ids=candidates) if m]
            grouped = [m for m in msgs if str(getattr(m, "grouped_id", "") or "") == gid]
            if grouped and any(_origin_matches(item, m) for m in grouped):
                return sorted(grouped, key=lambda m: m.id)
            return []
        m = await client.get_messages(chat_id, ids=[msg_id])
        mm = m[0] if isinstance(m, list) and m else m
        if _origin_matches(item, mm):
            return [mm]
        return []
    except Exception:
        return []


async def _resend_from_original(client, event, item) -> bool:
    """Переслать оригинальное сообщение(я) из чата, где оно было сохранено."""
    checked = await _origin_messages_checked(client, item)
    if not checked:
        return False
    try:
        await client.forward_messages(event.chat_id, [m.id for m in checked], from_peer=item.get("chat_id"))
        return True
    except Exception as e:
        logger.warning("Пересылка из оригинала не удалась: %s", e)
        return False


async def _original_messages(client, item) -> list:
    """Сообщения-первоисточники сохранённого поста (для медиа-копии)."""
    return await _origin_messages_checked(client, item)


async def _heal_broken_origins(owner: int) -> list:
    """Проверяет все посты владельца: не совпадающий с живым сообщением оригинал
    (битые message_id после переноса БД) переносит в корзину. Возвращает список id.
    Посты, чью проверку не удалось выполнить (сеть/другие ошибки), не трогает."""
    prev = db.current_user_id()
    db.set_current_user(owner)
    moved = []
    try:
        for iid in db.get_item_ids():
            item = db.get_item(iid)
            if not item or not item.get("message_id") or not item.get("chat_id"):
                continue
            try:
                ok = bool(await _origin_messages_checked(client, item))
            except Exception:
                ok = True
            if ok:
                continue
            try:
                if db.trash_item(iid):
                    moved.append(iid)
            except Exception as e:
                logger.warning("Не удалось убрать битый пост id=%s: %s", iid, e)
    except Exception as e:
        logger.warning("Авто-лечение битых оригиналов не удалось: %s", e)
    finally:
        db.set_current_user(prev)
    return moved


async def _try_apply_action(event, items: list[dict], hint: str) -> bool:
    """Пытается распознать в уточнении команду (создать/переместить/переименовать).
    Возвращает True, если команда выполнена (или обработана с ошибкой), False — если это обычное уточнение темы."""
    from categorizer import interpret_clarify

    cmd = await interpret_clarify(hint, _existing_category_names())
    action = (cmd or {}).get("action")
    if action in (None, "", "hint"):
        return False

    if action == "rename":
        new_summary = (cmd.get("summary") or "").strip()
        if not new_summary:
            return False
        done = 0
        for it in items:
            if db.rename_item(it["id"], new_summary)[0]:
                done += 1
        await event.respond(f"✏️ Переименовано постов: {done} → «{new_summary}»")
        await show_item_view(event, items[0]["id"])
        return True

    if action in ("move", "create_move"):
        cat = (cmd.get("category") or "").strip().strip("\"'«» \t")
        if not cat:
            await event.respond("Не понял, куда переместить. Напиши имя категории, например: «перемести в Комиксы».")
            return True
        created = False
        if not db.get_category_row(cat):
            if action == "create_move":
                folder = (cmd.get("folder") or "").strip("/").strip()
                ok, err = db.create_category(cat, path=folder or "")
                if not ok:
                    await event.respond(f"❌ Не удалось создать категорию: {err}")
                    return True
                created = True
            elif action == "move":
                if cat not in _existing_category_names():
                    await event.respond(f"❌ Категории «{cat}» нет. Можно: «создай папку {cat} и отправь сюда».")
                    return True
        moved = 0
        for it in items:
            if it["category"] != cat:
                db.update_item_category(it["id"], cat)
                moved += 1
        where = f" создал категорию «{cat}»" if created else ""
        note = " (уже была)" if not created and db.get_category_row(cat) else ""
        await event.respond(
            f"{'📁' if created else '📂'} Перемещено постов: {moved} → «{cat}»{where}{note}"
        )
        await show_item_view(event, items[0]["id"])
        return True

    return False


async def _handle_pending_action(event, act: dict, text: str):
    kind = act.get("kind")
    try:
        if kind == "clarify":
            item_id = act["target"]
            reply_id = act.get("reply_id")
            item = db.get_item(item_id)
            if not item:
                await event.respond("Элемент не найден.")
                return
            if item["locked"]:
                await event.respond("🔒 Элемент заблокирован — изменение запрещено.")
                return
            hint = (text or "").strip()
            if not hint:
                await event.respond("❌ Пустое уточнение. Напиши текст или нажми «❌ Отмена».")
                return
            if await _try_apply_action(event, [item], hint):
                return
            if reply_id:
                try:
                    await client.send_message(event.chat_id, "⏳ Перераспознаю с учётом уточнения...", reply_to=reply_id)
                except Exception:
                    pass
            else:
                await event.respond("⏳ Перераспознаю с учётом уточнения...")
            base = await _recat_text(item)
            if not base:
                await event.respond("❌ Не из чего распознавать (нет текста и не удалось описать фото).")
                return
            txt = f"{base}\n\nУточнение пользователя: {hint}"
            result = await categorize(
                txt,
                content_type=item["content_type"],
                source=item["source_channel"],
                categories=_existing_category_names(),
            )
            db.update_item_category(item_id, result["category"], result["summary"])
            if reply_id:
                try:
                    await client.send_message(
                        event.chat_id,
                        f"✅ Готово: «{result['category']}» — {result['summary']}",
                        reply_to=reply_id,
                    )
                except Exception:
                    await event.respond(f"✅ Готово: «{result['category']}» — {result['summary']}")
            else:
                await event.respond(f"✅ Готово: «{result['category']}» — {result['summary']}")
            await show_item_view(event, item_id)
            return

        if kind == "comment":
            item_id = act.get("target")
            item = db.get_item(item_id)
            if not item:
                await event.respond("Элемент не найден.")
                return
            if item["locked"]:
                await event.respond("🔒 Элемент заблокирован — изменение запрещено.")
                return
            val = (text or "").strip()
            if val == "-":
                db.set_item_comment(item_id, "")
                await event.respond("✅ Комментарий удалён.")
            else:
                if not val:
                    await event.respond("❌ Пустой комментарий. Напиши текст или «-» для удаления.")
                    return
                db.set_item_comment(item_id, val)
                await event.respond(f"✅ Комментарий сохранён: {strip_markdown(val)}")
            await show_item_view(event, item_id)
            return

        if kind == "selclarify":
            reply_id = act.get("reply_id")
            hint = (text or "").strip()
            if not hint:
                await event.respond("❌ Пустое уточнение. Напиши текст или нажми «❌ Отмена».")
                return
            ids = [i for i in act.get("ids", [])]
            if not ids:
                await event.respond("Элементы не найдены.")
                return
            items = []
            for i in ids:
                it = db.get_item(i)
                if it and not it["locked"]:
                    items.append(it)
            if not items:
                await event.respond("Все выбранные элементы заблокированы.")
                return
            if await _try_apply_action(event, items, hint):
                return
            done, skipped, errors = [], 0, []
            progress = await _prompt(event, f"⏳ Перераспознаю {len(items)} с учётом уточнения...")
            for i, item in enumerate(items):
                try:
                    base = await _recat_text(item)
                    if not base:
                        skipped += 1
                        continue
                    txt = f"{base}\n\nУточнение пользователя: {hint}"
                    result = await categorize(
                        txt,
                        content_type=item["content_type"],
                        source=item["source_channel"],
                        categories=_existing_category_names(),
                    )
                    db.update_item_category(item["id"], result["category"], result["summary"])
                    done.append((item["id"], result["category"], result["summary"]))
                except Exception as e:
                    logger.exception("Ошибка уточнения поста %s", item["id"])
                    errors += 1
                if (i + 1) % 3 == 0:
                    try:
                        await _MsgProxy(event.chat_id, progress.id).edit(f"⏳ Обработано {i + 1}/{len(items)}...")
                    except Exception:
                        pass
            lines = [f"✍️ Уточнено {len(done)}:", ""]
            for iid, cat, summ in done[:12]:
                lines.append(f"• ID {iid} → «{cat}»: {_fmt_short(summ, 60)}")
            if len(done) > 12:
                lines.append(f"… и ещё {len(done) - 12}.")
            if skipped or errors:
                lines.append(f"\nПропущено: {skipped}, ошибок: {errors}.")
            try:
                await _MsgProxy(event.chat_id, progress.id).edit("\n".join(lines) or "Готово.")
            except Exception:
                await event.respond("\n".join(lines) or "Готово.")
            return

        if kind == "rename_item":
            item_id = act["target"]
            item = db.get_item(item_id)
            if not item:
                await event.respond("Элемент не найден.")
                return
            if item["locked"]:
                await event.respond("🔒 Элемент заблокирован — переименование запрещено.")
                return
            ok, _ = db.rename_item(item_id, text)
            if not ok:
                await event.respond("❌ Заголовок не может быть пустым.")
                return
            if db.should_lock("rename"):
                db.set_item_locked(item_id, True)
            await _finish_action(event, act, show_item_view, item_id)
            return

        if kind == "rename_cat":
            old = act["target"]
            row = db.get_category_row(old)
            if row and row["locked"]:
                await event.respond("🔒 Категория заблокирована — переименование запрещено.")
                return
            ok, res = db.rename_category(old, text)
            if not ok:
                await event.respond(f"❌ {res}")
                return
            if db.should_lock("rename"):
                db.set_category_locked(res, True)
            await _finish_action(event, act, show_category_page, res, 0)
            return

        if kind == "rename_folder":
            path = act["target"]
            ok, res = db.rename_folder(path, text)
            if not ok:
                await event.respond(f"❌ {res}")
                return
            if db.should_lock("rename"):
                db.set_folder_locked(res, True)
            await _finish_action(event, act, show_folder_page, res)
            return

        if kind == "add_cat":
            path = act.get("path") or ""
            ok, res = db.create_category(text, path)
            if not ok:
                await event.respond(f"❌ {res}")
                return
            if db.should_lock("create"):
                db.set_category_locked(res, True)
            await _finish_action(event, act, show_category_page, res, 0)
            return

        if kind == "add_folder":
            parent = act.get("path") or ""
            name = (text or "").strip().strip("/")
            if not name or "/" in name:
                await event.respond("❌ Имя папки не может быть пустым или содержать «/».")
                return
            path = f"{parent}/{name}" if parent else name
            ok, res = db.create_folder(path)
            if not ok:
                await event.respond(f"❌ {res}")
                return
            if db.should_lock("create"):
                db.set_folder_locked(res, True)
            await _finish_action(event, act, show_folder_page, res)
            return

        if kind == "cat_icon_custom":
            category = act["target"]
            icon = (text or "").strip()
            if not icon or len(icon) > 4:
                await event.respond("❌ Пришли одну эмодзи-иконку (не больше 4 символов).")
                return
            db.set_category_icon(category, icon)
            _refresh_icon_overrides()
            await _finish_action(event, act, show_category_page, category, 0)
            return

        if kind == "tag_create":
            flow = tag_create.get(event.chat_id)
            if not flow:
                return
            icon = flow.get("icon") or "🏷"
            name = "" if text.strip() in ("", "-") else text.strip()
            tag_id = db.add_tag(icon, name)
            await _tag_apply_to_target(event.chat_id, tag_id)
            await _finish_back(event, act, _tag_editor_back(event.chat_id))
            return

        if kind == "tag_icon_custom":
            icon = text.strip()
            if not icon:
                await event.respond("❌ Пустая иконка.")
                return
            flow = tag_create.get(event.chat_id)
            if not flow:
                return
            tag_create[event.chat_id]["icon"] = icon
            sid = pending.push(
                event.chat_id, {"kind": "tag_create"}, "🏷 Имя нового тега",
                replay={"kind": "text", "text": "Название тега (или «-» если без названия):"},
            )
            msg = await _prompt(event, "Название тега (или «-» если без названия):")
            pending.set_msg(event.chat_id, sid, msg.id)
            return

        if kind == "tag_icon_existing":
            icon = text.strip()
            if not icon:
                await event.respond("❌ Пустая иконка.")
                return
            db.set_tag_icon(act["target"], icon)
            await _finish_action(event, act, show_tags_manage)
            return

        if kind == "tag_rename":
            name = "" if text.strip() == "-" else text.strip()
            db.set_tag_name(act["target"], name)
            await _finish_action(event, act, show_tags_manage)
            return
    except Exception as e:
        logger.exception("Ошибка обработки действия")
        await event.respond("❌ Что-то пошло не так.")


def _subfolders_path(category: str, group: str) -> str:
    """Путь подпапки: группа папки + слеш + имя категории (например «Игры/PS5»)."""
    group = (group or "").strip("/")
    if "/" in (category or ""):
        return group
    return f"{group}/{category}".strip("/")


async def _subfolders_propose(items: list[dict], category: str, min_posts: int, group: str) -> list[dict]:
    """Возвращает план подпапок для одной категории: [{'topic', 'path', 'ids'}]."""
    if len(items) < min_posts:
        return []
    try:
        groups = await propose_subgroups(category, items, min_posts)
    except Exception:
        return []
    path = _subfolders_path(category, group)
    plan = []
    for topic, ids in groups.items():
        topic = (topic or "").strip().strip("/")
        if not topic or "/" in topic:
            continue
        if db.get_category_row(topic):
            continue
        plan.append({"topic": topic, "path": path, "ids": ids})
    return plan


async def _subfolders_preview() -> list[dict]:
    """Строит {chat: план подпапок} без применения. Возвращает (lines, plan)."""
    min_posts = max(int(db.get_setting("subfolders_min", "5") or 5), 2)
    lines = ["🧩 Подпапки — план:"]
    plan = []
    for c in db.get_categories():
        cat = c["category"]
        group = (c.get("group") or "").strip("/")
        if c.get("locked") or db.folder_locked(group):
            continue
        items = [i for i in db.get_items_by_category(cat, limit=500) if not i.get("locked")]
        props = await _subfolders_propose(items, cat, min_posts, group)
        for p in props:
            path = p["path"]
            if db.folder_locked(path):
                continue
            p["category"] = cat
            p["group_parent"] = group
            p["ids"] = [iid for iid in p["ids"]]
            plan.append(p)
            lines.append(f"  • «{cat}» → «{p['topic']}» ({len(p['ids'])} постов)")
    if not plan:
        lines.append("  Ничего не нашлось — похожих подтем меньше минимума (или всё заблокировано).")
    return lines, plan


def _subfolders_apply(plan: list[dict]) -> list[str]:
    """Применяет план подпапок. Возвращает строки результата.
    Схема: для категории C создаём подпапку «C» (внутри текущей группы),
    переносим саму категорию C внутрь неё, а похожие посты — в новые подкатегории.
    Пример: «Игры» (+ её подпапки: темы PS5 и т.п.) внутри группы «Развлечения».
    """
    lines = []
    moved_cats: set[str] = set()
    for p in plan:
        category = p.get("category")
        if not category:
            continue
        db.create_folder(p["path"])
        if category not in moved_cats:
            row = db.get_category_row(category)
            if not row or row.get("locked"):
                continue
            db.set_category_path(category, p["path"])
            moved_cats.add(category)
        ok, _ = db.create_category(p["topic"], p["path"])
        if not ok:
            continue
        moved = 0
        for iid in p.get("ids", []):
            item = db.get_item(iid)
            if not item or item.get("locked"):
                continue
            if item["category"] != category:
                continue
            db.update_item_category(iid, p["topic"])
            moved += 1
        if moved:
            lines.append(f"  • «{category}» → подпапка «{p['topic']}» ({moved} постов)")
    if lines or moved_cats:
        _maybe_cleanup()
    return lines


async def _auto_order_plan() -> tuple[list[str], dict]:
    """Строит план авто-порядка БЕЗ применения. Возвращает (строки-превью, план)."""
    cats = db.get_categories()
    if not cats:
        return ["⚡ Авто-порядок: нет категорий."], {}
    res = await organize([c["category"] for c in cats])
    locked = {c["category"]: c["locked"] for c in cats}
    valid = {c["category"] for c in cats}

    merges = []
    for src, dst in res.get("merge", {}).items():
        if locked.get(src) or locked.get(dst) or dst not in valid:
            continue
        cnt = next((c["count"] for c in cats if c["category"] == src), 0)
        merges.append({"src": src, "dst": dst, "count": cnt})

    moved_away = {m["src"] for m in merges}
    category_names = {c["category"] for c in cats}
    current_folder = {c["category"]: (c["group"] or "").strip("/") for c in cats}
    folders_map: dict[str, list[str]] = {}
    for folder, names in res.get("folders", {}).items():
        folder_name = folder.strip("/")
        if folder_name in category_names:
            continue
        members = []
        for name in names:
            if name in moved_away or locked.get(name) or name not in valid:
                continue
            if folder_name == name:
                continue
            if db.folder_locked(current_folder.get(name, "")) or db.folder_locked(folder_name):
                continue
            members.append(name)
        if members:
            folders_map[folder_name] = members

    plan = {"merges": merges, "folders": folders_map, "subfolders": [], "skip_merges": set(), "skip_folders": set(), "skip_sub": set()}

    if db.get_setting("subfolders_enabled", "1") == "1" and db.get_setting("subfolders_in_auto", "1") == "1":
        sub_lines, sub_plan = await _subfolders_preview()
        plan["subfolders"] = sub_plan

    lines = _plan_lines(plan)
    return lines, plan


def _plan_lines(plan: dict) -> list[str]:
    """Формирует строки превью плана с учётом пропусков."""
    skips_m = set(plan.get("skip_merges", set()))
    skips_f = set(plan.get("skip_folders", set()))
    skips_s = set(plan.get("skip_sub", set()))
    lines = ["⚡ Авто-порядок — найдено:"]
    if plan.get("merges"):
        lines.append("\nОбъединить:")
        for m in plan["merges"]:
            m_mark = "💤 " if m["src"] in skips_m else ""
            lines.append(f"• {m_mark}{m['src']} → {m['dst']} ({m['count']} эл.)")
    if plan.get("folders"):
        lines.append("\nПапки:")
        for folder, names in plan["folders"].items():
            f_mark = "💤 " if folder in skips_f else ""
            lines.append(f"• {f_mark}{folder}: {', '.join(names[:6])}")
    sub_plan = plan.get("subfolders", [])
    if sub_plan:
        lines.append("\n🧩 Подпапки — план:")
        for p in sub_plan:
            s_mark = "💤 " if str(id(p)) in skips_s else ""
            lines.append(f"  • {s_mark}«{p['topic']}» ({len(p['ids'])} постов)")
    if len(lines) == 1:
        lines.append("\nНичего не найдено (всё уже хорошо или заблокировано).")
    return lines


def _auto_order_apply(plan: dict) -> list[str]:
    """Применяет план авто-порядка. Возвращает строки результата."""
    if not plan:
        return ["⚡ Ничего не применено."]
    cats = db.get_categories()
    locked = {c["category"]: c["locked"] for c in cats}
    valid = {c["category"] for c in cats}

    merged_lines = []
    for m in plan.get("merges", []):
        src, dst = m["src"], m["dst"]
        if src in plan.get("skip_merges", set()):
            continue
        if locked.get(src) or locked.get(dst) or dst not in valid:
            continue
        moved = db.merge_categories(src, dst, lock_items=False)
        if moved:
            merged_lines.append(f"• {src} → {dst} ({moved} эл.)")

    cats = db.get_categories()
    locked = {c["category"]: c["locked"] for c in cats}
    valid = {c["category"] for c in cats}
    category_names = set(valid)
    current_folder = {c["category"]: (c["group"] or "").strip("/") for c in cats}

    folded_lines = []
    for folder, names in plan.get("folders", {}).items():
        folder_name = folder.strip("/")
        if folder_name in plan.get("skip_folders", set()):
            continue
        if folder_name in category_names:
            continue
        for name in names:
            if name in [x["src"] for x in plan.get("merges", [])] or locked.get(name) or name not in valid:
                continue
            if folder_name == name:
                continue
            if db.folder_locked(current_folder.get(name, "")) or db.folder_locked(folder_name):
                continue
            db.set_category_path(name, folder_name)
            folded_lines.append(f"• «{name}» → папка «{folder_name}»")

    skipped_m = [f"• {m['src']} → {m['dst']}" for m in plan.get("merges", []) if m["src"] in plan.get("skip_merges", set())]
    skipped_f = [f"• «{folder}»" for folder in plan.get("skip_folders", set())]
    _maybe_cleanup()

    lines = ["⚡ Авто-порядок:"]
    if merged_lines:
        lines.append("\nОбъединено:")
        lines.extend(merged_lines)
    if folded_lines:
        lines.append("\nПапки:")
        lines.extend(folded_lines)

    sub = _subfolders_apply([p for p in plan.get("subfolders", []) if id(p) not in plan.get("skip_sub", set())]) if plan.get("subfolders") else []
    if sub:
        lines.append("\n🧩 Подпапки:")
        lines.extend(sub)

    if skipped_m:
        lines.append("\n⏭️ Пропущены объединения:")
        lines.extend(skipped_m)
    if skipped_f:
        lines.append("\n⏭️ Пропущены папки:")
        lines.extend(skipped_f)

    if len(lines) == 1:
        lines.append("\nНичего не изменилось (всё уже хорошо или заблокировано).")
    return lines


def _auto_order_buttons(plan: dict) -> list:
    """Кнопки плана авто-порядка: по-элементный выбор + применить/отмена."""
    if not plan:
        return [[Button.inline("⬅️ Назад", data="back_to_cats")]]
    rows = []
    skip_m = set(plan.get("skip_merges", set()))
    for m in plan.get("merges", []):
        src = m["src"]
        skipped = src in skip_m
        lbl = "↩️ Вернуть" if skipped else "🚫 Пропустить"
        rows.append([Button.inline(f"{lbl}: {src} → {m['dst']}", data=f"ao_skip_m:{src}")])
    skip_f = set(plan.get("skip_folders", set()))
    for folder in plan.get("folders", {}):
        skipped = folder in skip_f
        lbl = "↩️ Вернуть" if skipped else "🚫 Пропустить"
        rows.append([Button.inline(f"{lbl}: папку «{folder}»", data=f"ao_skip_f:{folder}")])
    for p in plan.get("subfolders", []):
        key = str(id(p))
        skipped = key in set(plan.get("skip_sub", set()))
        lbl = "↩️ Вернуть" if skipped else "🚫 Пропустить"
        rows.append([Button.inline(f"{lbl}: «{p['topic']}»", data=f"ao_skip_s:{key}")])
    rows.append(
        [
            Button.inline("✅ Применить", data="auto_order_apply"),
            Button.inline("❌ Отмена", data="auto_order_cancel"),
        ]
    )
    return rows


async def _auto_order_notify(client: TelegramClient, chat_id: int, user_id: int):
    db.set_current_user(user_id)
    _refresh_icon_overrides()
    if _auto_order_busy.get(chat_id):
        return
    _auto_order_busy[chat_id] = True
    try:
        lines, plan = await _auto_order_plan()
        if plan:
            auto_order_plan[chat_id] = plan
        try:
            await client.send_message(
                chat_id,
                "\n".join(lines),
                buttons=_auto_order_buttons(plan),
            )
        except Exception:
            pass
    finally:
        _auto_order_busy[chat_id] = False


def _auto_order_every_n() -> int:
    try:
        return max(int(db.get_setting("auto_order_posts", "10") or "10"), 0)
    except Exception:
        return 10


def _auto_order_fire(client: TelegramClient, chat_id: int, user_id: int):
    """Запуск авто-порядка после того, как поток постов успокоился."""
    every_n = _auto_order_every_n()
    if every_n <= 0:
        return
    if _auto_order_pending.get(chat_id, 0) >= every_n and not _auto_order_busy.get(chat_id):
        _auto_order_pending[chat_id] = 0
        asyncio.create_task(_auto_order_notify(client, chat_id, user_id))


def _arm_auto_order(client: TelegramClient, chat_id: int, user_id: int):
    """Откладывает авто-порядок на 4 секунды. Каждое новое сохранение сдвигает таймер."""
    if chat_id in _auto_order_timer:
        _auto_order_timer[chat_id].cancel()
    _auto_order_timer[chat_id] = asyncio.get_event_loop().call_later(
        4.0, lambda: _auto_order_fire(client, chat_id, user_id)
    )


async def on_callback(event):
    data = event.data
    try:
        await event.answer()
    except Exception:
        pass
    data_s = data.decode("utf-8", "ignore")

    if data_s == "noop":
        return

    logger.info("🔘 Колбэк: %s chat=%s", data_s[:60], event.chat_id)

    try:
        _activate_user(event.sender_id)
    except Exception:
        pass

    if data_s == "dismiss":
        await event.delete()
        return

    if data_s.startswith("clrs|"):
        _, sid, idx_s = data_s.split("|", 2)
        try:
            idx = int(idx_s)
        except ValueError:
            return
        entry = None
        for e in pending.entries(event.chat_id):
            if e["sid"] == sid:
                entry = e
                break
        if not entry or not (entry["act"] or {}).get("kind") in ("clarify", "selclarify"):
            await event.answer("Устарело", alert=True)
            return
        names = clarify_opts.pop(sid, None)
        if names is None or idx >= len(names):
            await event.answer("Устарело", alert=True)
            return
        pending.remove(event.chat_id, sid)
        hint = f"Тема поста: {names[idx]}"
        await _handle_pending_action(event, entry["act"], hint)
        return

    if data_s.startswith("pend_replay|"):
        sid = data_s.split("|", 1)[1]
        entry = pending.remove(event.chat_id, sid)
        if not entry:
            await event.answer("Уже не актуально", alert=True)
            return
        replay = entry.get("replay") or {}
        kind = replay.get("kind")
        if kind == "dup":
            payload = (entry["act"] or {}).get("dup_payload")
            if not payload:
                await event.answer("Устарело", alert=True)
                return
            dup_stage[event.chat_id] = payload
            try:
                ask_text = replay.get("text") or "Сохранить всё равно?"
            except Exception:
                ask_text = "Сохранить всё равно?"
            pending.push(
                event.chat_id, {"kind": "dup", "dup_payload": payload}, "⚠️ Сохранить дубль?",
                replay={"kind": "dup", "text": ask_text},
            )
            try:
                await client.send_message(
                    event.chat_id, ask_text,
                    buttons=[
                        [Button.inline("✅ Да, сохранить", data="dup_save_yes")],
                        [Button.inline("❌ Нет, не надо", data="dup_save_no")],
                    ],
                )
            except Exception:
                pass
            try:
                await event.answer("Вопрос повторён")
            except Exception:
                pass
            return
        text = replay.get("text") or "Ответь, пожалуйста:"
        act = dict(entry["act"])
        act["msg_id"] = None
        nsid = pending.push(event.chat_id, act, entry.get("label") or "Вопрос", replay=replay)
        msg = await _prompt(event, text)
        pending.set_msg(event.chat_id, nsid, msg.id)
        try:
            await event.answer("Вопрос повторён")
        except Exception:
            pass
        return

    if data_s == "pend_clear":
        n = pending.remove_for_chat(event.chat_id)
        try:
            await event.edit(f"🧹 Очищено вопросов: {n}.")
        except Exception:
            pass
        return

    if data_s == "dup_save_yes":
        _resolve_dup_pending(event.chat_id)
        st = dup_stage.pop(event.chat_id, None)
        if not st:
            await event.answer("Информация уже неактуальна", alert=True)
            return
        proxy = _MsgProxy(event.chat_id, event.message_id)
        try:
            await proxy.edit("⏳ Сохраняю...")
        except Exception:
            pass
        await _save(
            client,
            chat_id=st["chat_id"],
            processing=proxy,
            content_type=st["content_type"],
            text=st["text"],
            file_ids=st["media"],
            message_id=st["message_id"],
            media_group_id=st["media_group_id"],
            source_channel=st["source_channel"],
        )
        return

    if data_s == "dup_save_no":
        _resolve_dup_pending(event.chat_id)
        dup_stage.pop(event.chat_id, None)
        try:
            await event.edit("Ок, не сохраняю.")
        except Exception:
            pass
        return

    if data_s.startswith("settoggle|"):
        key = data_s.split("|", 1)[1]
        if key not in SETTINGS_DEFAULTS:
            return
        cur = db.get_setting(key, SETTINGS_DEFAULTS[key])
        new = "0" if cur == "1" else "1"
        db.set_setting(key, new)
        if key == "auto_lock" and new == "1":
            st = settings_state()
            if not any(st[k] == "1" for k in ("lock_move", "lock_rename", "lock_create")):
                for k in ("lock_move", "lock_rename", "lock_create"):
                    db.set_setting(k, "1")
        await show_settings(event)
        await event.answer("Готово!")
        return

    if data_s.startswith("setcycle|"):
        parts = data_s.split("|")
        if len(parts) == 3:
            key, direction = parts[1], parts[2]
            cycles = {
                "auto_order_posts": ["0", "5", "10", "15", "20", "30", "50"],
                "subfolders_min": ["3", "4", "5", "8", "10", "15"],
                "backup_every_posts": ["0", "3", "5", "10", "20", "50"],
            }
            if key in cycles and direction in ("up", "down"):
                cur = db.get_setting(key, SETTINGS_DEFAULTS[key])
                vals = cycles[key]
                try:
                    idx = vals.index(cur)
                except ValueError:
                    idx = 0
                idx += 1 if direction == "up" else -1
                idx = idx % len(vals)
                db.set_setting(key, vals[idx])
                await show_settings(event)
                await event.answer("Готово!")
                return

    if data_s == "back_to_cats" or data_s == "to_folders":
        await show_root_page(event)
        return

    if data_s.startswith("fold|"):
        path = data_s[len("fold|"):]
        await show_folder_page(event, path)
        return

    if data_s == "btn_help":
        text = (
            "ℹ️ Что делают кнопки:\n\n"
            "• 📂 + 💾 — каждая строка с папкой/категорией открывает содержимое\n"
            "• ➕ Папка / ➕ Категория — создать папку или категорию\n"
            "• ⚡ Авто-порядок — ИИ объединяет дубли и раскладывает категории по папкам\n"
            "• 🔀 Объединить — вручную объединить две категории\n"
            "• 🔁 Перераспознать всё — заново определён категории и описания всех постов\n"
            "• 🔒 Заблокированные — список всего, что защищено от изменений\n"
            "• 🧩 Подпапки — находит похожие посты внутри категории и предлагает подпапки\n"
            "• 🏷 Теги — управление тегами\n"
            "• 🗑 Корзина — удалённые посты\n"
            "• ☑️ Выбрать — режим выбора нескольких постов (массовое перемещение/теги/удаление)\n"
            "• ⚙️ Настройки — авто-порядок, подпапки, замки, фото\n\n"
            "Под сообщением с сохранением быстро: переместить, переименовать, теги."
        )
        try:
            await event.answer()
        except Exception:
            pass
        await _nav_send(event, text)
        return

    if data_s == "auto_order":
        if not db.get_categories():
            await event.answer("Нет категорий", alert=True)
            return
        try:
            await event.answer()
        except Exception:
            pass
        await event.edit("⏳ Анализирую категории... (может занять до 60 сек)")
        lines, plan = await _auto_order_plan()
        auto_order_plan[event.chat_id] = plan
        await event.edit("\n".join(lines), buttons=_auto_order_buttons(plan))
        return

    if data_s.startswith("ao_skip_m:"):
        plan = auto_order_plan.get(event.chat_id)
        if not plan:
            await event.answer("План уже неактуален", alert=True)
            return
        src = data_s.split(":", 1)[1]
        s = set(plan.get("skip_merges", set()))
        if src in s:
            s.discard(src)
        else:
            s.add(src)
        plan["skip_merges"] = s
        await event.edit("\n".join(_plan_lines(plan)), buttons=_auto_order_buttons(plan))
        return

    if data_s.startswith("ao_skip_f:"):
        plan = auto_order_plan.get(event.chat_id)
        if not plan:
            await event.answer("План уже неактуален", alert=True)
            return
        folder = data_s.split(":", 1)[1]
        s = set(plan.get("skip_folders", set()))
        if folder in s:
            s.discard(folder)
        else:
            s.add(folder)
        plan["skip_folders"] = s
        await event.edit("\n".join(_plan_lines(plan)), buttons=_auto_order_buttons(plan))
        return

    if data_s.startswith("ao_skip_s:"):
        plan = auto_order_plan.get(event.chat_id)
        if not plan:
            await event.answer("План уже неактуален", alert=True)
            return
        key = data_s.split(":", 1)[1]
        s = set(plan.get("skip_sub", set()))
        if key in s:
            s.discard(key)
        else:
            s.add(key)
        plan["skip_sub"] = s
        await event.edit("\n".join(_plan_lines(plan)), buttons=_auto_order_buttons(plan))
        return

    if data_s == "auto_order_apply":
        plan = auto_order_plan.pop(event.chat_id, None)
        if not plan:
            await event.answer("План уже неактуален", alert=True)
            return
        try:
            await event.answer()
        except Exception:
            pass
        await event.edit("⏳ Применяю...")
        lines = _auto_order_apply(plan)
        await event.edit("⚡ Готово:\n" + "\n".join(lines))
        await event.respond("📂 Папки и категории:", buttons=root_categories_keyboard(db.get_tree()))
        return

    if data_s == "auto_order_cancel":
        auto_order_plan.pop(event.chat_id, None)
        try:
            await event.answer()
        except Exception:
            pass
        try:
            await event.edit("❌ Авто-порядок отменён.")
        except Exception:
            pass
        return

    if data_s == "subfolders_preview":
        if db.get_setting("subfolders_enabled", "1") != "1":
            await event.answer("Подпапки отключены в настройках", alert=True)
            return
        try:
            await event.answer()
        except Exception:
            pass
        await event.edit("⏳ Ищу похожие посты (может занять до 60 сек)...")
        lines, plan = await _subfolders_preview()
        subfolders_plan[event.chat_id] = plan
        buttons = []
        if plan:
            buttons.append([Button.inline("✅ Применить", data="subfolders_apply"), Button.inline("❌ Отмена", data="subfolders_cancel")])
        else:
            buttons.append([Button.inline("⬅️ Назад", data="back_to_cats")])
        await event.edit("\n".join(lines), buttons=buttons)
        return

    if data_s == "subfolders_apply":
        plan = subfolders_plan.pop(event.chat_id, None)
        if not plan:
            await event.answer("План уже неактуален", alert=True)
            return
        try:
            await event.answer()
        except Exception:
            pass
        await event.edit("⏳ Создаю подпапки...")
        lines = _subfolders_apply(plan)
        await event.edit("🧩 Готово:\n" + ("\n".join(lines) if lines else "  Ничего не применено."))
        await event.respond("📂 Папки и категории:", buttons=root_categories_keyboard(db.get_tree()))
        return

    if data_s == "subfolders_cancel":
        subfolders_plan.pop(event.chat_id, None)
        try:
            await event.answer()
        except Exception:
            pass
        try:
            await event.edit("❌ Подпапки отменены.")
        except Exception:
            pass
        return

    if data_s == "merge":
        cats = [c for c in db.get_categories() if not c["locked"]]
        if len(cats) < 2:
            await event.answer("Нужно минимум 2 незаблокированные категории", alert=True)
            return
        await event.edit("🔀 Что объединить? (источник):", buttons=merge_source_keyboard(cats))
        return

    if data_s.startswith("merge_src|"):
        src = data_s.split("|", 1)[1]
        merge_flow[event.chat_id] = src
        cats = [c for c in db.get_categories() if not c["locked"] and c["category"] != src]
        if not cats:
            await event.answer("Некуда объединять", alert=True)
            return
        await event.edit(f"Перенести элементы из «{src}» в:", buttons=category_keyboard_rows(cats))
        return

    if data_s.startswith("merge_dst|"):
        dst = data_s.split("|", 1)[1]
        src = merge_flow.pop(event.chat_id, None)
        if not src:
            await event.answer("Сессия объединения истекла", alert=True)
            await show_root_page(event)
            return
        moved = db.merge_categories(src, dst, lock_items=db.should_lock("move"))
        _maybe_cleanup()
        if moved:
            await event.answer(f"🔀 Объединено {moved} эл. в «{dst}»")
        else:
            await event.answer("Ничего не перенесено (всё заблокировано)")
        await show_root_page(event)
        return

    if data_s.startswith("mvcat|"):
        category = data_s.split("|", 1)[1]
        paths = db.get_folder_paths()
        await event.edit(f"📁 В какую папку переместить «{category}»?", buttons=mvcat_pick_keyboard(paths, category))
        return

    if data_s.startswith("mvcat_dest|"):
        rest = data_s[len("mvcat_dest|"):]
        category, dst = rest.rsplit("|", 1)
        row = db.get_category_row(category)
        if row and row["locked"]:
            await event.answer("🔒 Категория заблокирована", alert=True)
            return
        db.set_category_path(category, dst)
        if db.should_lock("move"):
            db.set_category_locked(category, True)
        await event.answer("📁 Готово!")
        await show_category_page(event, category, 0)
        return

    if data_s.startswith("mvfolder|"):
        src = data_s.split("|", 1)[1]
        paths = db.get_folder_paths()
        await event.edit(f"📂 Куда переместить папку «{src}»?", buttons=mvfolder_pick_keyboard(paths, src))
        return

    if data_s.startswith("mvfolder_dest|"):
        rest = data_s[len("mvfolder_dest|"):]
        src, dst = rest.rsplit("|", 1)
        if not src:
            await event.answer("У корня нет папки-родителя", alert=True)
            return
        if dst and (dst == src or dst.startswith(src + "/")):
            await event.answer("Нельзя перенести папку внутрь себя", alert=True)
            return
        moved = db.move_folder_path(src, dst)
        if moved:
            if db.should_lock("move"):
                db.set_folder_locked(src, True)
            await event.answer(f"📂 Перемещено {moved} категорий в «{dst or 'корень'}»")
        else:
            await event.answer("Нечего перемещать (заблокировано)")
        await show_root_page(event)
        return

    if data_s.startswith("catlock|"):
        category = data_s.split("|", 1)[1]
        row = db.get_category_row(category)
        locked = bool(row["locked"]) if row else False
        db.set_category_locked(category, not locked)
        await event.answer("Заблокировано 🔒" if not locked else "Разблокировано 🔓")
        await show_category_page(event, category, 0)
        return

    if data_s.startswith("itemlock:"):
        item_id = int(data_s.split(":")[1])
        item = db.get_item(item_id)
        if not item:
            await event.answer("Не найдено!", alert=True)
            return
        db.set_item_locked(item_id, not item["locked"])
        await event.answer("Заблокировано 🔒" if not item["locked"] else "Разблокировано 🔓")
        await show_item_view(event, item_id)
        return

    if data_s.startswith("rencat|"):
        category = data_s.split("|", 1)[1]
        act = {"kind": "rename_cat", "target": category}
        sid = pending.push(
            event.chat_id, act, f"✏️ Переименовать категорию «{category}»",
            replay={"kind": "text", "text": f"✏️ Новое название категории «{category}»:"},
        )
        msg = await _prompt(event, f"✏️ Новое название категории «{category}»:")
        pending.set_msg(event.chat_id, sid, msg.id)
        return

    if data_s.startswith("renfolder|"):
        path = data_s.split("|", 1)[1]
        act = {"kind": "rename_folder", "target": path}
        sid = pending.push(
            event.chat_id, act, f"✏️ Переименовать папку «{path}»",
            replay={"kind": "text", "text": f"✏️ Новое имя папки «{path}» (без «/»):"},
        )
        msg = await _prompt(event, f"✏️ Новое имя папки «{path}» (без «/»):")
        pending.set_msg(event.chat_id, sid, msg.id)
        return

    if data_s.startswith("renitem:"):
        item_id = int(data_s.split(":")[1])
        item = db.get_item(item_id)
        if item and item["locked"]:
            await event.answer("🔒 Элемент заблокирован — снимите замок", alert=True)
            return
        act = {"kind": "rename_item", "target": item_id}
        sid = pending.push(
            event.chat_id, act, f"✏️ Переименовать пост ID {item_id}",
            replay={"kind": "text", "text": "✏️ Новый заголовок поста:"},
        )
        msg = await _prompt(event, "✏️ Новый заголовок поста:")
        pending.set_msg(event.chat_id, sid, msg.id)
        return

    if data_s.startswith("addcat|"):
        path = data_s.split("|", 1)[1] if "|" in data_s else ""
        act = {"kind": "add_cat", "path": path}
        where = f" в «{path}»" if path else ""
        sid = pending.push(
            event.chat_id, act, f"➕ Создать категорию{where}",
            replay={"kind": "text", "text": f"➕ Название новой категории{where}:"},
        )
        msg = await _prompt(event, f"➕ Название новой категории{where}:")
        pending.set_msg(event.chat_id, sid, msg.id)
        return

    if data_s.startswith("addfolder|"):
        path = data_s.split("|", 1)[1] if "|" in data_s else ""
        act = {"kind": "add_folder", "path": path}
        where = f" в «{path}»" if path else ""
        sid = pending.push(
            event.chat_id, act, f"➕ Создать папку{where}",
            replay={"kind": "text", "text": f"➕ Название новой папки{where} (без «/»):"},
        )
        msg = await _prompt(event, f"➕ Название новой папки{where} (без «/»):")
        pending.set_msg(event.chat_id, sid, msg.id)
        return

    if data_s == "cancel_action":
        pending.pop(event.chat_id)
        try:
            await event.edit("❌ Действие отменено.")
        except Exception:
            pass
        return

    if data_s == "search_cancel":
        pending_search.pop(event.chat_id, None)
        try:
            await event.edit("❌ Поиск отменён.")
        except Exception:
            pass
        return

    if data_s == "search_date":
        pending_search[event.chat_id] = "date"
        await event.edit(
            "🕐 Ищу по дате сохранения.\n\n"
            "Напиши:\n"
            "• дату — «31.12.2025» (все посты за этот день)\n"
            "• диапазон — «01.01.2025-31.01.2025»\n"
            "• или слово: «вчера», «неделя», «месяц», «год» (за последние 7/30/365 дней)\n\n"
            "Например: «03.09.2026», «01.09-07.09.2026», «вчера»",
            buttons=[[Button.inline("❌ Отмена", data="search_cancel")]],
        )
        return

    if data_s == "recatall":
        ids = db.get_item_ids()
        if ids:
            await run_bulk_recat(event, ids, "все элементы")
        else:
            await event.answer("Нет элементов", alert=True)
        return

    if data_s == "fix_truncated":
        await _fix_truncated(event)
        return

    if data_s == "locked_list":
        await show_locked_list(event)
        return

    if data_s == "undo_action":
        ent = undo_data.pop(event.chat_id, None)
        if not ent:
            await event.answer("⏰ Время на отмену вышло", alert=True)
            return
        payload = ent["payload"]
        if payload.get("kind") == "untrash":
            n = 0
            for iid in payload.get("ids", []):
                if db.restore_item(iid):
                    n += 1
            await event.edit(f"↩️ Отменено: возвращено из корзины {n} постов.")
            await event.answer("✅")
        elif payload.get("kind") == "unpurge":
            data = payload.get("data", [])
            for item in data:
                db.restore_purged(item)
            await event.edit(f"↩️ Отменено: {len(data)} постов восстановлено в корзину.")
            await event.answer("✅")
        return

    if data_s == "trash_list":
        await show_trash(event)
        return

    if data_s.startswith("trash_tog:"):
        item_id = int(data_s.split(":", 1)[1])
        sel = trash_sel.setdefault(event.chat_id, set())
        if item_id in sel:
            sel.discard(item_id)
        else:
            sel.add(item_id)
        await show_trash(event)
        return

    if data_s == "trash_selall":
        trash_sel[event.chat_id] = {it["id"] for it in db.get_trashed(limit=100000)}
        await show_trash(event)
        return

    if data_s == "trash_selnone":
        trash_sel.pop(event.chat_id, None)
        await show_trash(event)
        return

    if data_s == "trash_selrestore":
        sel = trash_sel.pop(event.chat_id, set())
        restored = [iid for iid in sel if db.restore_item(iid)]
        await event.answer(f"✅ Восстановлено: {len(restored)}")
        await show_trash(event, f"✅ {len(restored)} постов возвращено в архив." if restored else "Ничего не отмечено для возврата.")
        return

    if data_s == "trash_selpurge":
        sel = trash_sel.pop(event.chat_id, set())
        n = len(sel)
        if not n:
            await event.answer("Ничего не выбрано", alert=True)
            await show_trash(event)
            return
        await event.edit(f"🗑 Удалить {n} выбранных постов НАВСЕГДА?\n\nВосстановление будет невозможно.", buttons=[
            [Button.inline("Да, удалить навсегда", data="trash_selpurgego")],
            [Button.inline("◀️ Назад", data="trash_list")],
        ])
        trash_sel[event.chat_id] = sel
        return

    if data_s.startswith("trash_selpurgego"):
        sel = trash_sel.pop(event.chat_id, set())
        purged = []
        for iid in list(sel):
            d = db.purge_trash_item(iid)
            if d:
                purged.append(d)
        _set_undo(event.chat_id, {"kind": "unpurge", "data": purged})
        buttons = _undo_button() + [[Button.inline("🗑 В корзину", data="trash_list")]]
        await event.edit(
            f"🗑 {len(purged)} постов удалено безвозвратно." if purged else "Ничего не удалено.",
            buttons=buttons,
        )
        return

    if data_s.startswith("trash_restore:"):
        item_id = int(data_s.split(":", 1)[1])
        sel = trash_sel.get(event.chat_id, set())
        sel.discard(item_id)
        if db.restore_item(item_id):
            await event.answer("✅ Пост восстановлен")
            await show_trash(event, f"✅ Пост №{item_id} возвращён в архив.")
        else:
            await event.answer("Не найдено", alert=True)
            await show_trash(event)
        return

    if data_s == "trash_allrestore":
        n = db.restore_all_trash()
        await event.answer(f"✅ Восстановлено: {n}")
        await show_trash(event, f"✅ {n} постов возвращено в архив." if n else "✅ Корзина уже была пуста.")
        return

    if data_s == "trash_empty":
        await event.edit("🧹 Удалить ВСЕ посты из корзины?\n\nОни будут удалены безвозвратно, восстановление невозможно.", buttons=[
            [Button.inline("Да, удалить всё", data="trash_emptygo")],
            [Button.inline("◀️ Назад", data="trash_list")],
        ])
        return

    if data_s == "trash_emptygo":
        purged = db.empty_trash()
        n = len(purged)
        _set_undo(event.chat_id, {"kind": "unpurge", "data": purged})
        buttons = _undo_button() + [[Button.inline("🗑 В корзину", data="trash_list")]]
        await event.edit(
            f"🧹 Корзина очищена: {n} постов удалено безвозвратно." if n else "✅ Очищать было нечего.",
            buttons=buttons,
        )
        return

    if data_s.startswith("trash_purge:"):
        item_id = int(data_s.split(":", 1)[1])
        await event.edit(f"🗑 Удалить пост №{item_id} НАВСЕГДА?\n\nВосстановление будет невозможно, теги поста будут потеряны.", buttons=[
            [Button.inline("Да, удалить навсегда", data=f"trash_purgego:{item_id}")],
            [Button.inline("◀️ Назад", data="trash_list")],
        ])
        return

    if data_s.startswith("trash_purgego:"):
        item_id = int(data_s.split(":", 1)[1])
        d = db.purge_trash_item(item_id)
        _set_undo(event.chat_id, {"kind": "unpurge", "data": [d] if d else []})
        buttons = _undo_button() + [[Button.inline("🗑 В корзину", data="trash_list")]]
        await event.edit(f"🗑 Пост №{item_id} удалён навсегда.", buttons=buttons)
        return

    if data_s == "lcsel":
        st = _lu_state(event.chat_id)
        st["mode"] = not st["mode"]
        await show_locked_list(event)
        return

    if data_s == "lcclear":
        _lu_clear(event.chat_id)
        await show_locked_list(event)
        return

    if data_s == "lcall":
        for it in db.get_locked_items():
            db.set_item_locked(it["id"], False)
        for c in db.get_locked_categories():
            db.set_category_locked(c["category"], False)
        for f in db.get_locked_folders():
            db.set_folder_locked(f, False)
        _lu_clear(event.chat_id)
        await event.answer("🔓 Всё разблокировано")
        await show_locked_list(event)
        return

    if data_s == "lcunlock":
        st = _lu_state(event.chat_id)
        n = len(st["items"]) + len(st["cats"]) + len(st["folders"])
        if not n:
            await event.answer("Ничего не выбрано", alert=True)
            return
        for i in st["items"]:
            db.set_item_locked(i, False)
        for c in st["cats"]:
            db.set_category_locked(c, False)
        for f in st["folders"]:
            db.set_folder_locked(f, False)
        _lu_clear(event.chat_id)
        await event.answer(f"🔓 Разблокировано ({n})")
        await show_locked_list(event)
        return

    if data_s.startswith("lctog:"):
        parts = data_s.split(":", 2)
        if len(parts) != 3:
            return
        kind, name = parts[1], parts[2]
        st = _lu_state(event.chat_id)
        if kind == "item":
            bucket = st["items"]
            key = int(name)
        elif kind == "cat":
            bucket = st["cats"]
            key = name
        elif kind == "fold":
            bucket = st["folders"]
            key = name
        else:
            return
        if key in bucket:
            bucket.discard(key)
        else:
            bucket.add(key)
        await show_locked_list(event)
        return

    if data_s.startswith("lccat:"):
        await show_category_page(event, data_s.split(":", 1)[1], 0)
        return

    if data_s.startswith("lcfold:"):
        await show_folder_page(event, data_s.split(":", 1)[1].strip("/"))
        return

    if data_s.startswith("lcitem:"):
        await show_item_view(event, int(data_s.split(":", 1)[1]))
        return

    if data_s.startswith("recatcat|"):
        category = data_s.split("|", 1)[1]
        ids = db.get_item_ids(category)
        if ids:
            await run_bulk_recat(event, ids, category)
        else:
            await event.answer("Пусто", alert=True)
        return

    if data_s.startswith("selmode|"):
        category = data_s.split("|", 1)[1]
        await show_selection_page(event, event.chat_id, category, 0)
        return

    if data_s.startswith("sel|"):
        rest = data_s[4:]
        category, id_str = rest.rsplit("|", 1)
        entry = _sel_entry(event.chat_id, category)
        if id_str in entry["ids"]:
            entry["ids"].discard(id_str)
        else:
            entry["ids"].add(id_str)
        await show_selection_page(event, event.chat_id, category, entry["page"])
        return

    if data_s.startswith("selclear|"):
        category = data_s.split("|", 1)[1]
        entry = _sel_entry(event.chat_id, category)
        page = entry["page"]
        _sel_clear(event.chat_id, category)
        await show_category_page(event, category, page)
        return

    if data_s.startswith("selrecat|"):
        category = data_s.split("|", 1)[1]
        entry = _sel_entry(event.chat_id, category)
        ids = []
        for x in entry["ids"]:
            it = db.get_item(int(x))
            if it and not it["locked"]:
                ids.append(int(x))
        if not ids:
            await event.answer("Нечего распознавать (выбрано всё заблокировано)", alert=True)
            return
        _sel_clear(event.chat_id, category)
        await run_bulk_recat(event, ids, category)
        return

    if data_s.startswith("selmv|"):
        category = data_s.split("|", 1)[1]
        entry = _sel_entry(event.chat_id, category)
        if not entry["ids"]:
            await event.answer("Ничего не выбрано", alert=True)
            return
        cats = [c for c in db.get_categories() if not c["locked"]]
        await event.edit("📂 Переместить выбранное в:", buttons=sel_move_keyboard(category, cats))
        return

    if data_s.startswith("selmvgo|"):
        rest = data_s[len("selmvgo|"):]
        src, target = rest.rsplit("|", 1)
        entry = _sel_entry(event.chat_id, src)
        trow = db.get_category_row(target)
        if trow and trow["locked"]:
            await event.answer("🔒 Категория заблокирована", alert=True)
            return
        ids = []
        for x in entry["ids"]:
            it = db.get_item(int(x))
            if it and not it["locked"]:
                ids.append(int(x))
        count = 0
        for iid in ids:
            db.update_item_category(iid, target)
            if db.should_lock("move"):
                db.set_item_locked(iid, True)
            count += 1
        _sel_clear(event.chat_id, src)
        await event.edit(f"📂 Перемещено {count} эл. в «{target}».")
        await event.answer("Готово!")
        return

    if data_s.startswith("seldel|"):
        category = data_s.split("|", 1)[1]
        entry = _sel_entry(event.chat_id, category)
        if not entry["ids"]:
            await event.answer("Ничего не выбрано", alert=True)
            return
        await event.edit(
            "🗑 Переместить выбранные элементы в корзину?",
            buttons=[
                [Button.inline("Да, в корзину", data=f"seldelgo|{category}")],
                [Button.inline("◀️ Назад", data=f"selclear|{category}")],
            ],
        )
        return

    if data_s.startswith("seldelgo|"):
        category = data_s.split("|", 1)[1]
        entry = _sel_entry(event.chat_id, category)
        ids = []
        for x in entry["ids"]:
            it = db.get_item(int(x))
            if it and not it["locked"]:
                ids.append(int(x))
        trashed = [iid for iid in ids if db.trash_item(iid)]
        _sel_clear(event.chat_id, category)
        _set_undo(event.chat_id, {"kind": "untrash", "ids": trashed})
        await event.edit(f"🗑 В корзине: {len(trashed)} элементов.", buttons=_undo_button())
        await event.answer("Готово!")
        return

    if data_s.startswith("seltags|"):
        await show_tag_editor(event, "sel", data_s.split("|", 1)[1])
        return

    if data_s.startswith("seltog|"):
        rest = data_s.split("|", 1)[1]
        category, tid_s = rest.rsplit("|", 1)
        entry = _sel_entry(event.chat_id, category)
        selected = set()
        for x in entry["ids"]:
            it = db.get_item(int(x))
            if it:
                selected |= {t["id"] for t in db.get_item_tags(int(x))}
        tid = int(tid_s)
        if tid in selected:
            selected.discard(tid)
        else:
            selected.add(tid)
        for x in entry["ids"]:
            db.set_item_tags(int(x), selected)
        await show_tag_editor(event, "sel", category)
        return

    if data_s.startswith("seltnew|"):
        category = data_s.split("|", 1)[1]
        _start_tag_create(event.chat_id, "sel", category)
        await event.edit("🎨 Иконка нового тега:", buttons=tag_palette_keyboard(f"selmode|{category}"))
        return

    if data_s == "tags_section":
        await show_tags_section(event)
        return

    if data_s == "tags_manage":
        await show_tags_manage(event)
        return

    if data_s.startswith("tagpick:"):
        tid = int(data_s.split(":", 1)[1])
        sel = tag_filter.setdefault(event.chat_id, set())
        if tid in sel:
            sel.discard(tid)
        else:
            sel.add(tid)
        await show_tags_section(event)
        return

    if data_s == "tagclear":
        tag_filter.pop(event.chat_id, None)
        await show_tags_section(event)
        return

    if data_s == "tagshow":
        await show_tags_result(event)
        return

    if data_s == "tagnew":
        _start_tag_create(event.chat_id, None, None)
        await event.edit("🎨 Иконка нового тега:", buttons=tag_palette_keyboard("tags_section"))
        return

    if data_s.startswith("tagact:"):
        await show_tag_actions(event, int(data_s.split(":", 1)[1]))
        return

    if data_s.startswith("tagren:"):
        tid = int(data_s.split(":", 1)[1])
        sid = pending.push(
            event.chat_id, {"kind": "tag_rename", "target": tid}, "✏️ Переименовать тег",
            replay={"kind": "text", "text": "Новое название тега (или «-» если убрать название):"},
        )
        msg = await _prompt(event, "Новое название тега (или «-» если убрать название):")
        pending.set_msg(event.chat_id, sid, msg.id)
        return

    if data_s.startswith("tagicon:"):
        tid = int(data_s.split(":", 1)[1])
        tag_icon_apply[event.chat_id] = tid
        await event.edit("🎨 Новая иконка тега:", buttons=tag_palette_keyboard("tags_manage"))
        return

    if data_s.startswith("tagdel:"):
        tid = int(data_s.split(":", 1)[1])
        db.delete_tag(tid)
        await event.answer("Тег удалён 🗑")
        await show_tags_manage(event)
        return

    if data_s.startswith("tagrecat:"):
        tid = int(data_s.split(":", 1)[1])
        n = len(db.items_with_tag(tid))
        if not n:
            await event.answer("С этим тегом нет постов", alert=True)
            return
        await event.edit(
            f"🔁 Перераспознать {n} постов с этим тегом?\nКатегории будут пересчитаны ИИ.",
            buttons=[
                [Button.inline("🔁 Да, перераспознать", data=f"tagrecatgo:{tid}")],
                [Button.inline("❌ Отмена", data=f"tagact:{tid}")],
            ],
        )
        return

    if data_s.startswith("tagrecatgo:"):
        tid = int(data_s.split(":", 1)[1])
        ids = [it["id"] for it in db.items_with_tag(tid)]
        await run_bulk_recat(event, ids, "Тег")
        return

    if data_s.startswith("tagpal:"):
        code = data_s.split(":", 1)[1]
        if code == "custom":
            if event.chat_id in tag_icon_apply:
                act = {"kind": "tag_icon_existing", "target": tag_icon_apply[event.chat_id]}
                label = "🎨 Иконка существующего тега"
            else:
                flow = tag_create.get(event.chat_id)
                if not flow:
                    return
                act = {"kind": "tag_icon_custom"}
                label = "🎨 Иконка нового тега"
            sid = pending.push(
                event.chat_id, act, label,
                replay={"kind": "text", "text": "Пришли эмодзи-иконку тега:"},
            )
            msg = await _prompt(event, "Пришли эмодзи-иконку тега:")
            pending.set_msg(event.chat_id, sid, msg.id)
            return
        icon = DEFAULT_TAG_CIRCLES[int(code)]
        if event.chat_id in tag_icon_apply:
            tid = tag_icon_apply.pop(event.chat_id)
            db.set_tag_icon(tid, icon)
            await event.answer("Иконка обновлена 🎨")
            await show_tags_manage(event)
            return
        flow = tag_create.get(event.chat_id)
        if not flow:
            return
        flow["icon"] = icon
        sid = pending.push(
            event.chat_id, {"kind": "tag_create"}, "🏷 Имя нового тега",
            replay={"kind": "text", "text": "Название тега (или «-» если без названия):"},
        )
        msg = await _prompt(event, "Название тега (или «-» если без названия):")
        pending.set_msg(event.chat_id, sid, msg.id)
        return

    if data_s.startswith("itag:"):
        await show_tag_editor(event, "item", data_s.split(":", 1)[1])
        return
    if data_s.startswith("ctag|"):
        await show_tag_editor(event, "cat", data_s.split("|", 1)[1])
        return
    if data_s.startswith("ftag|"):
        await show_tag_editor(event, "fold", data_s.split("|", 1)[1])
        return

    if data_s.startswith("itnew:"):
        target = data_s.split(":", 1)[1]
        _start_tag_create(event.chat_id, "item", int(target))
        await event.edit("🎨 Иконка нового тега:", buttons=tag_palette_keyboard(_tag_editor_back(event.chat_id)))
        return
    if data_s.startswith("ctnew|"):
        target = data_s.split("|", 1)[1]
        _start_tag_create(event.chat_id, "cat", target)
        await event.edit("🎨 Иконка нового тега:", buttons=tag_palette_keyboard(_tag_editor_back(event.chat_id)))
        return
    if data_s.startswith("ftnew|"):
        target = data_s.split("|", 1)[1]
        _start_tag_create(event.chat_id, "fold", target)
        await event.edit("🎨 Иконка нового тега:", buttons=tag_palette_keyboard(_tag_editor_back(event.chat_id)))
        return
    if data_s.startswith("cfnew|"):
        target = data_s.split("|", 1)[1]
        _start_tag_create(event.chat_id, "cf", target)
        await event.edit("🎨 Иконка нового тега:", buttons=tag_palette_keyboard(_tag_editor_back(event.chat_id)))
        return

    if data_s.startswith("ittog:"):
        _, rest = data_s.split(":", 1)
        item_id, tid_s = rest.split(":", 1)
        ids = {t["id"] for t in db.get_item_tags(int(item_id))}
        tid = int(tid_s)
        if tid in ids:
            ids.discard(tid)
        else:
            ids.add(tid)
        db.set_item_tags(int(item_id), ids)
        await show_tag_editor(event, "item", item_id)
        return
    if data_s.startswith("cttog|"):
        rest = data_s.split("|", 1)[1]
        category, tid_s = rest.rsplit("|", 1)
        ids = {t["id"] for t in db.get_category_tags(category)}
        tid = int(tid_s)
        if tid in ids:
            ids.discard(tid)
        else:
            ids.add(tid)
        db.set_category_tags(category, ids)
        await show_tag_editor(event, "cat", category)
        return
    if data_s.startswith("fttog|"):
        rest = data_s.split("|", 1)[1]
        path, tid_s = rest.rsplit("|", 1)
        ids = {t["id"] for t in db.get_folder_tags(path)}
        tid = int(tid_s)
        if tid in ids:
            ids.discard(tid)
        else:
            ids.add(tid)
        db.set_folder_tags(path, ids)
        await show_tag_editor(event, "fold", path)
        return
    if data_s.startswith("cftog|"):
        rest = data_s.split("|", 1)[1]
        root, tid_s = rest.rsplit("|", 1)
        st = cf_sel.get(event.chat_id)
        if st:
            selected = set()
            for key in st["sel"]:
                k, _, n = key.partition("|")
                if k == "cat":
                    selected |= {t["id"] for t in db.get_category_tags(n)}
                else:
                    selected |= {t["id"] for t in db.get_folder_tags(n)}
            tid = int(tid_s)
            if tid in selected:
                selected.discard(tid)
            else:
                selected.add(tid)
            for key in list(st["sel"]):
                k, _, n = key.partition("|")
                if k == "cat":
                    db.set_category_tags(n, selected)
                else:
                    db.set_folder_tags(n, selected)
        await show_tag_editor(event, "cf", root)
        return

    if data_s.startswith("cat:") or data_s.startswith("back_to_cat:"):
        category = data_s.split(":", 1)[1]
        await show_category_page(event, category, 0)
        return

    if data_s.startswith("page:"):
        rest = data_s.split(":", 1)[1]
        page_str = rest.rsplit("|", 1)[1]
        category = rest.rsplit("|", 1)[0]
        await show_category_page(event, category, int(page_str))
        return

    if data_s.startswith("cf_selmode|"):
        root = data_s.split("|", 1)[1]
        cf_sel[event.chat_id] = {"sel": set(), "root": root}
        await show_cf_selection(event)
        return

    if data_s.startswith("cf_tog|"):
        rest = data_s.split("|", 1)[1]
        root, key = rest.split("|", 1)
        st = cf_sel.setdefault(event.chat_id, {"sel": set(), "root": root})
        if key in st["sel"]:
            st["sel"].discard(key)
        else:
            st["sel"].add(key)
        await show_cf_selection(event)
        return

    if data_s.startswith("cf_selall|"):
        root = data_s.split("|", 1)[1]
        tree = _tree_with_tags()
        children = _find_folder(tree, root)["children"] if root else tree
        st = cf_sel.setdefault(event.chat_id, {"sel": set(), "root": root})
        for node in children:
            key = f"cat|{node['category']}" if node["type"] == "cat" else f"fold|{node['path']}"
            st["sel"].add(key)
        await show_cf_selection(event)
        return

    if data_s.startswith("cf_selnone|"):
        root = data_s.split("|", 1)[1]
        st = cf_sel.setdefault(event.chat_id, {"sel": set(), "root": root})
        st["sel"].clear()
        await show_cf_selection(event)
        return

    if data_s.startswith("cf_back|"):
        root = data_s.split("|", 1)[1]
        cf_sel.pop(event.chat_id, None)
        if root:
            await show_folder_page(event, root)
        else:
            await show_root_page(event)
        return

    if data_s.startswith("cf_tags|"):
        root = data_s.split("|", 1)[1]
        st = cf_sel.get(event.chat_id)
        if not st or not st["sel"]:
            await event.answer("Сначала выбери категории/папки", alert=True)
        else:
            await show_tag_editor(event, "cf", root)
        return

    if data_s.startswith("cf_del|"):
        root = data_s.split("|", 1)[1]
        st = cf_sel.get(event.chat_id)
        if not st or not st["sel"]:
            await event.answer("Сначала выбери категории/папки", alert=True)
            return
        checks = []
        for key in st["sel"]:
            k, _, n = key.partition("|")
            if k == "cat":
                row = db.get_category_row(n)
                checks.append((n, bool(row and row.get("locked"))))
            else:
                checks.append((n, db.folder_locked(n)))
        if any(locked for _, locked in checks):
            await event.answer("🔒 Среди выбранных есть заблокированные — удаление запрещено", alert=True)
            return
        await event.edit(
            f"⚠️ Удалить выбранные {len(st['sel'])} шт.? Все посты внутри уйдут в корзину, «назад» пути нет.",
            buttons=[
                [Button.inline("🗑 Да, удалить", data=f"cf_delgo|{root}")],
                [Button.inline("❌ Отмена", data=f"cf_selmode|{root}")],
            ],
        )
        return

    if data_s.startswith("cf_delgo|"):
        root = data_s.split("|", 1)[1]
        st = cf_sel.pop(event.chat_id, None)
        if not st:
            await event.edit("Выделение уже сброшено.")
            return
        n = 0
        for key in st["sel"]:
            k, _, name = key.partition("|")
            if k == "cat":
                n += db.delete_category(name)
            else:
                n += db.delete_folder(name)
        await event.edit(f"🗑 Удалено. В корзину перенесено постов: {n}.",
                         buttons=[[Button.inline("📂 Папки и категории", data="back_to_cats")]])
        return

    if data_s.startswith("catdel:"):
        category = data_s.split(":", 1)[1]
        count = db.get_total_items(category)
        lock = db.get_category_row(category) and db.get_category_row(category).get("locked")
        if lock:
            await event.answer("🔒 Категория заблокирована — удаление запрещено", alert=True)
            return
        await event.edit(
            f"⚠️ Удалить категорию «{category}»?\nПостов внутри: {count}. Они уйдут в корзину.",
            buttons=[
                [Button.inline("🗑 Да, удалить", data=f"catdelgo:{category}")],
                [Button.inline("❌ Отмена", data=f"cat:{category}")],
            ],
        )
        return

    if data_s.startswith("caticon:"):
        category = data_s.split(":", 1)[1]
        await event.edit("🎨 Иконка категории:", buttons=category_icon_keyboard(category))
        return

    if data_s.startswith("caticon_pick:"):
        rest = data_s.split(":", 1)[1]
        idx_s, category = rest.split("|", 1)
        if idx_s == "custom":
            sid = pending.push(
                event.chat_id, {"kind": "cat_icon_custom", "target": category}, "🎨 Иконка категории",
                replay={"kind": "text", "text": "Пришли эмодзи-иконку категории:"},
            )
            msg = await _prompt(event, "Пришли эмодзи-иконку категории:")
            pending.set_msg(event.chat_id, sid, msg.id)
            return
        db.set_category_icon(category, DEFAULT_TAG_CIRCLES[int(idx_s)])
        _refresh_icon_overrides()
        await show_category_page(event, category, 0)
        return

    if data_s.startswith("catdelgo:"):
        category = data_s.split(":", 1)[1]
        n = db.delete_category(category)
        await event.edit(f"🗑 Категория «{category}» удалена. Постов ушло в корзину: {n}.",
                         buttons=[[Button.inline("📂 Папки и категории", data="back_to_cats")]])
        return

    if data_s.startswith("folddel:"):
        path = data_s.split(":", 1)[1]
        if db.folder_locked(path):
            await event.answer("🔒 Папка заблокирована — удаление запрещено", alert=True)
            return
        count = db.folder_post_count(path)
        await event.edit(
            f"⚠️ Удалить папку «{path}» (и вложенные)?\nПостов внутри: {count}. Они уйдут в корзину.",
            buttons=[
                [Button.inline("🗑 Да, удалить", data=f"folddelgo:{path}")],
                [Button.inline("❌ Отмена", data=f"fold|{path}")],
            ],
        )
        return

    if data_s.startswith("folddelgo:"):
        path = data_s.split(":", 1)[1]
        n = db.delete_folder(path)
        await event.edit(f"🗑 Папка «{path}» удалена. Постов ушло в корзину: {n}.",
                         buttons=[[Button.inline("📂 Папки и категории", data="back_to_cats")]])
        return

    if data_s == "pend_list":
        await cmd_pending(event)
        return

    if data_s.startswith("view:"):
        item_id = int(data_s.split(":")[1])
        await show_item_view(event, item_id)
        return

    if data_s.startswith("recat:"):
        item_id = int(data_s.split(":")[1])
        item = db.get_item(item_id)
        if not item:
            await event.answer("Не найдено!", alert=True)
            return
        if item["locked"]:
            await event.answer("🔒 Элемент заблокирован — снимите замок", alert=True)
            return
        if item["content_type"] == "photo" and not _has_meaningful_text((item["original_text"] or "").strip()):
            try:
                await event.answer("⏳ Описываю фото (llava)...")
            except Exception:
                pass
        txt = await _recat_text(item)
        if not txt:
            await event.answer("Не из чего распознавать (нет текста и не удалось описать фото)", alert=True)
            return
        try:
            await event.answer("🔁 Распознаю...")
        except Exception:
            pass
        result = await categorize(
            txt,
            content_type=item["content_type"],
            source=item["source_channel"],
            categories=_existing_category_names(),
        )
        db.update_item_category(item_id, result["category"], result["summary"])
        try:
            await show_item_view(event, item_id)
            await event.answer(f"✅ Готово: {result['category']}")
        except Exception as e:
            logger.error("Не удалось обновить сообщение после перераспознавания: %s", e)
        return

    if data_s.startswith("clarify:") or data_s.startswith("clarifyv:"):
        item_id = int(data_s.split(":")[1])
        reply_id = event.message_id if data_s.startswith("clarify:") else None
        item = db.get_item(item_id)
        if not item:
            await event.answer("Не найдено!", alert=True)
            return
        if item["locked"]:
            await event.answer("🔒 Элемент заблокирован — снимите замок", alert=True)
            return
        if data_s.startswith("clarifyv:"):
            reply_id = item.get("message_id") or None
        act = {
            "kind": "clarify", "target": item_id, "msg_id": None,
            "back": f"view:{item_id}", "reply_id": reply_id,
        }
        sid = pending.push(
            event.chat_id, act, "✍️ Уточнить пост",
            replay={"kind": "text", "text": "✍️ Что уточнить? Напиши, о чём этот пост на самом деле."},
        )
        try:
            await event.answer()
        except Exception:
            pass
        suggest_text = " ".join(
            filter(None, [item.get("summary") or "", item.get("original_text") or ""])
        )
        names = _suggest_names(suggest_text, exclude=item["category"])
        clarify_opts[sid] = names
        opt_buttons = _clarify_buttons(names, sid) if names else [[Button.inline("❌ Отмена", data="cancel_action")]]
        q = ("✍️ Что уточнить? Напиши, о чём этот пост на самом деле "
             "(например: «здесь речь про носки, а не кроссовки», «это вакансия на hh.ru»). "
             "Или дай команду: «создай папку Комиксы в Развлечениях и отправь сюда», «перемести в …», «назови …»."
             "")
        q = q + ("\n\nИли выбери готовый вариант ↓" if names else "")
        if reply_id:
            msg = await client.send_message(event.chat_id, q, buttons=opt_buttons, reply_to=reply_id)
        else:
            msg = await _prompt(event, q)
            if names:
                try:
                    await msg.edit(q, buttons=opt_buttons)
                except Exception:
                    pass
        pending.set_msg(event.chat_id, sid, msg.id)
        return

    if data_s.startswith("selclarify|"):
        category = data_s.split("|", 1)[1]
        entry = _sel_entry(event.chat_id, category)
        ids = []
        for x in entry["ids"]:
            it = db.get_item(int(x))
            if it and not it["locked"]:
                ids.append(int(x))
        if not ids:
            await event.answer("Нечего уточнять (выбрано всё заблокировано)", alert=True)
            return
        _sel_clear(event.chat_id, category)
        act = {
            "kind": "selclarify", "ids": ids, "msg_id": None,
            "back": f"cat:{category}", "reply_id": event.message_id,
        }
        sid = pending.push(
            event.chat_id, act, f"✍️ Уточнить {len(ids)} постов",
            replay={"kind": "text", "text": "✍️ Уточнение для нескольких постов."},
        )
        try:
            await event.answer()
        except Exception:
            pass
        suggest_text = " ".join(
            filter(None, [
                (db.get_item(i) or {}).get("summary") or ""
                for i in ids[:6]
            ])
        )
        names = _suggest_names(suggest_text, exclude=category)
        clarify_opts[sid] = names
        opt_buttons = _clarify_buttons(names, sid) if names else [[Button.inline("❌ Отмена", data="cancel_action")]]
        q = (f"✍️ Уточнение для {len(ids)} постов. Напиши, о чём они на самом деле "
             "(общий контекст, напр. «это всё посты с аниме-новостями», «это вакансии с hh.ru»). "
             "Или дай команду: «создай папку Комиксы и отправь всех туда», «перемести в …», «назови …»."
             "")
        q = q + ("\n\nИли выбери готовый вариант ↓" if names else "")
        msg = await client.send_message(
            event.chat_id,
            q,
            buttons=opt_buttons,
            reply_to=event.message_id if event.message_id else None,
        )
        pending.set_msg(event.chat_id, sid, msg.id)
        return

    if data_s.startswith("cmt:"):
        item_id = int(data_s.split(":")[1])
        item = db.get_item(item_id)
        if not item:
            await event.answer("Не найдено!", alert=True)
            return
        if item["locked"]:
            await event.answer("🔒 Элемент заблокирован — снимите замок", alert=True)
            return
        act = {
            "kind": "comment", "target": item_id, "msg_id": None,
            "back": f"view:{item_id}", "reply_id": event.message_id,
        }
        sid = pending.push(
            event.chat_id, act, "💬 Комментарий к посту",
            replay={"kind": "text", "text": "💬 Напиши комментарий."},
        )
        try:
            await event.answer()
        except Exception:
            pass
        cur = item.get("comment") or ""
        q = ("💬 Напиши комментарий к посту. Он будет виден в карточке поста.\n"
             "Чтобы удалить комментарий — отправь «-».")
        if cur:
            q += f"\n\nТекущий: {strip_markdown(cur)}"
        if event.message_id:
            try:
                msg = await client.send_message(
                    event.chat_id, q,
                    buttons=[[Button.inline("❌ Отмена", data="cancel_action")]],
                    reply_to=event.message_id,
                )
            except Exception:
                msg = await _prompt(event, q)
        else:
            msg = await _prompt(event, q)
        pending.set_msg(event.chat_id, sid, msg.id)
        return

    if data_s.startswith("hist:"):
        item_id = int(data_s.split(":")[1])
        item = db.get_item(item_id)
        if not item:
            await event.answer("Не найдено!", alert=True)
            return
        rows = db.get_history(item_id)
        lines = [f"📜 История поста #{item_id} («{item['category']}»):"]
        if not rows:
            lines.append("  изменений пока нет.")
        for r in rows[:15]:
            lines.append(f"• {r['at'][11:16]} {_history_label(r)}")
        await event.edit(
            "\n".join(lines),
            buttons=[
                [Button.inline("◀️ К посту", data=f"view:{item_id}")],
                [Button.inline("❌ Закрыть", data="dismiss")],
            ],
        )
        return

    if data_s.startswith("move:"):
        item_id = int(data_s.split(":")[1])
        item = db.get_item(item_id)
        if not item:
            await event.answer("Не найдено!", alert=True)
            return
        if item["locked"]:
            await event.answer("🔒 Элемент заблокирован — снимите замок", alert=True)
            return
        cats = [c for c in db.get_categories() if not c["locked"]]
        if not cats:
            await event.answer("Нет доступных категорий", alert=True)
            return
        await event.edit("📂 Переместить в категорию:", buttons=move_keyboard(item_id, cats))
        return

    if data_s.startswith("mvset:"):
        _, rest = data_s.split(":", 1)
        item_id_str, category = rest.split(":", 1)
        item = db.get_item(int(item_id_str))
        if item and item["locked"]:
            await event.answer("🔒 Элемент заблокирован — снимите замок", alert=True)
            return
        trow = db.get_category_row(category)
        if trow and trow["locked"]:
            await event.answer("🔒 Категория заблокирована", alert=True)
            return
        db.update_item_category(int(item_id_str), category)
        if db.should_lock("move"):
            db.set_item_locked(int(item_id_str), True)
        await show_item_view(event, int(item_id_str))
        return

    if data_s.startswith("del:"):
        item_id = int(data_s.split(":")[1])
        item = db.get_item(item_id)
        if not item:
            await event.answer("Не найдено!", alert=True)
            return
        if item["locked"]:
            await event.answer("🔒 Элемент заблокирован — снимите замок", alert=True)
            return
        await event.edit("🗑 Переместить в корзину? Восстановить можно позже.", buttons=confirm_delete_keyboard(item_id))
        return

    if data_s.startswith("delgo:"):
        item_id = int(data_s.split(":")[1])
        item = db.get_item(item_id)
        if not item:
            await event.answer("Не найдено!", alert=True)
            return
        if item["locked"]:
            await event.answer("🔒 Элемент заблокирован — снимите замок", alert=True)
            return
        db.trash_item(item_id)
        _set_undo(event.chat_id, {"kind": "untrash", "ids": [item_id]})
        await event.edit("🗑 В корзине. Вернуть можно из раздела «🗑 Корзина».", buttons=_undo_button())
        return

    if data_s.startswith("forward:"):
        item_id = int(data_s.split(":")[1])
        item = db.get_item(item_id)
        if not item:
            await event.answer("Не найдено!", alert=True)
            return

        try:
            sent = await _resend_from_original(client, event, item)
            if not sent:
                refs, caption = await _resend(item)
                cap = caption[:1024] if caption else None
                if len(refs) > 1:
                    try:
                        await client.send_file(event.chat_id, refs, caption=cap)
                    except Exception:
                        for ref in refs:
                            await client.send_file(event.chat_id, ref, caption=cap)
                elif refs:
                    await client.send_file(event.chat_id, refs[0], caption=cap)
                elif caption:
                    await client.send_message(event.chat_id, caption)
                else:
                    raise Exception("Нет содержимого")
            await event.answer("Переслано!")
        except Exception as e:
            logger.exception("Ошибка пересылки")


async def show_item_view(event, item_id: int):
    item = db.get_item(item_id)
    if not item:
        try:
            await event.edit("Элемент не найден.")
        except Exception:
            pass
        return
    tags = _tags_line(db.get_item_tags(item_id))
    emoji = theme_icon(item["category"], "📦")
    lock = " 🔒" if item["locked"] else ""
    chan = f"\n\n📡 {item['source_channel']}" if item["source_channel"] else ""
    summary = strip_markdown(item["summary"] or "")
    cnote = f"\n\n💬 {strip_markdown(item['comment'])}" if item.get("comment") else ""
    text = f"{tags}{emoji} {item['category']}{lock}\n\n{summary}{chan}{cnote}\n\n📅 ID: {item['id']}"
    buttons = view_keyboard(item_id, item["category"], item["locked"])
    chat_id = item.get("chat_id") or getattr(event, "chat_id", None)
    if chat_id and item.get("message_id"):
        try:
            sent = await client.send_message(chat_id, text, buttons=buttons, reply_to=item["message_id"])
            _detached_views.add((chat_id, sent.id))
            return
        except Exception as e:
            logger.warning("Не удалось ответить на исходный пост: %s", e)
    try:
        sent = await client.send_message(chat_id or getattr(event, "chat_id", None), text, buttons=buttons)
        _detached_views.add((chat_id or getattr(event, "chat_id", None), sent.id))
    except Exception:
        try:
            await event.edit(text, buttons=buttons)
        except Exception:
            pass


async def show_selection_page(event, chat_id: int, category: str, page: int):
    total = db.get_total_items(category)
    total_pages = max(1, (total + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))

    items = db.get_items_by_category(category, limit=ITEMS_PER_PAGE, offset=page * ITEMS_PER_PAGE)
    if not items:
        await _nav_send(event, "Нет элементов.")
        return

    for it in items:
        it["tags"] = db.get_item_tags(it["id"])
    entry = _sel_entry(chat_id, category)
    entry["page"] = page
    cat_row = db.get_category_row(category)
    folder_path = (cat_row["group"] or "").strip("/") if cat_row else ""
    emoji = theme_icon(category, "📦")
    header = f"{emoji} {category}  (стр. {page + 1}/{total_pages})\nВыбери элементы:"
    await _nav_send(event, header, buttons=items_keyboard(items, category, page, total_pages, selection=entry, folder_path=folder_path))


async def _recat_text(item) -> str:
    text = (item["original_text"] or "").strip()
    if _has_meaningful_text(text):
        return text
    if db.get_setting("photo_vision", "1") == "1" and item["content_type"] == "photo":
        if item.get("message_id") and item.get("chat_id"):
            try:
                res = await client.get_messages(item["chat_id"], ids=[item["message_id"]])
                m = res[0] if isinstance(res, list) and res else res
                if m and getattr(m, "media", None):
                    data = await client.download_media(m, file=bytes)
                    if data:
                        cap = await describe_image(data)
                        if cap:
                            return cap
            except Exception:
                pass
    if db.get_setting("audio_vision", "1") == "1" and item["content_type"] in ("voice", "audio"):
        if item.get("message_id") and item.get("chat_id"):
            try:
                res = await client.get_messages(item["chat_id"], ids=[item["message_id"]])
                m = res[0] if isinstance(res, list) and res else res
                if m and getattr(m, "media", None):
                    data = await client.download_media(m, file=bytes)
                    if data:
                        txt = await transcribe_audio(data)
                        if txt:
                            return txt
            except Exception:
                pass
    return ""


def _looks_truncated(s: str) -> bool:
    s = (s or "").strip()
    if not s:
        return False
    if s.endswith(("...", "…")):
        return True
    if len(s) >= 115 and s[-1] not in ".!?…":
        return True
    return False


async def _fix_truncated(event):
    cleaned = 0
    to_fix = []
    for row in db.all_summaries():
        s2 = normalize_summary(row["summary"])
        if s2 != row["summary"]:
            db.set_item_summary(row["id"], s2)
            cleaned += 1
        if _looks_truncated(s2):
            it = db.get_item(row["id"])
            if it and not it["locked"] and (_recat_prospect(it)):
                to_fix.append(it)
    if not to_fix:
        try:
            await event.edit(f"🧹 Обрезанных заголовков нет. Поправлено (markdown/хвосты): {cleaned}.")
        except Exception:
            pass
        await event.answer("Готово!")
        return
    try:
        progress = await event.edit(f"🧹 Завершаю summaries: 0/{len(to_fix)}...")
    except Exception:
        progress = await event.respond(f"🧹 Завершаю summaries: 0/{len(to_fix)}...")
    fixed = skipped = 0
    for i, item in enumerate(to_fix, 1):
        try:
            txt = await _recat_text(item)
            if not txt:
                skipped += 1
                continue
            result = await categorize(
                txt,
                content_type=item["content_type"],
                source=item["source_channel"],
                categories=_existing_category_names(),
            )
            db.update_item_category(item["id"], result["category"], result["summary"])
            fixed += 1
        except Exception as e:
            logger.exception("Ошибка при завершении summary %s", item["id"])
            skipped += 1
        if i % 3 == 0:
            try:
                await _MsgProxy(event.chat_id, progress.id).edit(f"🧹 Завершаю summaries: {i}/{len(to_fix)}...")
            except Exception:
                pass
    try:
        await _MsgProxy(event.chat_id, progress.id).edit(
            f"🧹 Готово: поправлено заголовков {cleaned}, до-распознано summaries {fixed}, пропущено {skipped}."
        )
    except Exception:
        pass
    await event.answer("Готово!")


def _recat_prospect(item) -> bool:
    if (item.get("original_text") or "").strip():
        return True
    return bool(item.get("message_id") and item.get("chat_id"))


async def run_bulk_recat(event, ids: list[int], title: str = "") -> int:
    total = len(ids)
    progress = await event.respond("🔁 Перераспознаю...")
    done = 0
    skipped_locked = 0
    skipped_nodata = 0
    for i, item_id in enumerate(ids, 1):
        item = db.get_item(item_id)
        if item and not item["locked"]:
            txt = await _recat_text(item)
            if txt:
                result = await categorize(
                    txt,
                    content_type=item["content_type"],
                    source=item["source_channel"],
                    categories=_existing_category_names(),
                )
                db.update_item_category(item_id, result["category"], result["summary"])
                done += 1
            else:
                skipped_nodata += 1
        else:
            skipped_locked += 1
        try:
            await progress.edit(f"🔁 {i}/{total}...")
        except Exception:
            pass
    notes = []
    if skipped_locked:
        notes.append(f"{skipped_locked} 🔒")
    if skipped_nodata:
        notes.append(f"{skipped_nodata} без данных")
    suffix = f" (пропущено: {', '.join(notes)})" if notes else ""
    try:
        await progress.edit(f"✅ Перераспознано {done} из {total}{suffix} ({title})")
    except Exception:
        pass
    await event.answer("Готово!")
    return done


async def show_category_page(event, category: str, page: int):
    total = db.get_total_items(category)
    total_pages = max(1, (total + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))

    items = db.get_items_by_category(category, limit=ITEMS_PER_PAGE, offset=page * ITEMS_PER_PAGE)
    if not items:
        await _nav_send(event, "Нет элементов.")
        return

    for it in items:
        it["tags"] = db.get_item_tags(it["id"])
    cat_row = db.get_category_row(category)
    cat_locked = bool(cat_row["locked"]) if cat_row else False
    folder_path = (cat_row["group"] or "").strip("/") if cat_row else ""
    tags = _tags_line(db.get_category_tags(category))
    emoji = theme_icon(category, "📦")
    lock = " 🔒" if cat_locked else ""
    header = f"{tags}{emoji} {category}{lock}  (стр. {page + 1}/{total_pages})"
    await _nav_send(event, header, buttons=items_keyboard(items, category, page, total_pages, category_locked=cat_locked, folder_path=folder_path))


async def watchdog():
    """Следит за соединением: если обновления перестали доходить — переподнимает связь."""
    while True:
        await asyncio.sleep(60)
        try:
            if not client.is_connected():
                logger.warning("Watchdog: соединение потеряно, переподключаюсь")
                await client.disconnect()
                return
            await asyncio.wait_for(client.get_me(), timeout=15)
        except asyncio.TimeoutError:
            logger.warning("Watchdog: Telegram молчит >15с, переподключаюсь")
            try:
                await client.disconnect()
            except Exception:
                pass
            return
        except Exception as e:
            logger.warning("Watchdog: ошибка %s, переподключаюсь", e)
            try:
                await client.disconnect()
            except Exception:
                pass
            return


BACKUP_INTERVAL = 6 * 3600


def _cached_owner() -> int | None:
    """Владелец из OWNER_ID, либо из settings старой БД (legacy)."""
    if OWNER_ID:
        try:
            return int(OWNER_ID)
        except Exception:
            return None
    return db.legacy_owner_id()


def _maybe_restore_db(user_id: int) -> bool:
    """Если БД юзера пуста — восстанавливает из seed-файла (только для владельца)."""
    try:
        if int(user_id) != int(_cached_owner() or 0):
            return False
        if db.count_all_items() > 0 or not os.path.exists(SEED_FILE):
            return False
        import json as _json

        with open(SEED_FILE, "r", encoding="utf-8") as f:
            data = _json.load(f)
        res = db.import_json(data)
        logger.info("Восстановлено из seed (user=%s): items=%s cats=%s tags=%s",
                    user_id, res["items"], res["categories"], res["tags"])
        return bool(res["items"])
    except Exception as e:
        logger.warning("Восстановление из seed не удалось: %s", e)
        return False


def _media_unique_empty() -> bool:
    return True


async def _restore_if_empty_background(uid: int) -> None:
    """Раз на сессию: если у пользователя пустая БД — восстановить из GitHub-синка."""
    key = uid
    if key in _restore_tried:
        return
    _restore_tried.add(key)
    if not GITHUB_TOKEN:
        return
    prev = db.current_user_id()
    db.set_current_user(uid)
    try:
        if db.count_all_items() == 0:
            await _restore_from_github()
    finally:
        db.set_current_user(prev)


async def health_http() -> None:
    """Поднимает HTTP-сервер на PORT с /healthz для Render (UptimeRobot/healthcheck)."""
    if not HTTP_PORT:
        return
    from aiohttp import web

    async def handler_health(_request):
        return web.Response(text="ok", content_type="text/plain")

    async def handler_ready(_request):
        connected = bool(getattr(client, "is_connected", lambda: False)() if client else False)
        status = 503 if not connected else 200
        return web.Response(text="ok" if connected else "disconnected", status=status)

    app = web.Application()
    app.router.add_get("/healthz", handler_ready)
    app.router.add_get("/", handler_health)
    runner = web.AppRunner(app)
    try:
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", HTTP_PORT)
        await site.start()
        logger.info("HTTP health-сервер запущен на порту %s", HTTP_PORT)
    except Exception as e:
        logger.warning("Не удалось поднять HTTP-сервер: %s", e)


async def backup_loop():
    """Каждые 6 часов бэкапит БД каждого пользователя и шлёт экспорт ему в TG."""
    while True:
        await asyncio.sleep(BACKUP_INTERVAL)
        for uid in db.all_users():
            try:
                prev = db.current_user_id()
                db.set_current_user(uid)
                _refresh_icon_overrides()
                dst = db.backup_db()
                if dst:
                    logger.info("Авто-бэкап БД (user=%s): %s", uid, dst)
                await _send_tg_backup(uid)
            except Exception as e:
                logger.warning("Бэкап для user=%s не удался: %s", uid, e)
            finally:
                db.set_current_user(prev)


async def _send_weekly_digest(user_id: int) -> None:
    since = datetime.utcnow() - timedelta(days=7)
    rows = db.stats_since(since)
    total = sum(r["count"] for r in rows)
    if total == 0:
        await client.send_message(user_id, "📬 Дайджест за неделю: новых постов нет.")
        return
    lines = [f"📬 Дайджест за неделю (всего {total} постов):", ""]
    for r in rows[:20]:
        lines.append(f"{theme_icon(r['category'], '📦')} {r['category']}: {r['count']}")
    if len(rows) > 20:
        lines.append(f"… и ещё {len(rows) - 20} категорий.")
    await client.send_message(user_id, "\n".join(lines))


_backup_in_flight = False


def _refresh_seed(dump: dict) -> None:
    """Перезаписывает запасной seed-файл свежим экспортом, чтобы фолбэк никогда не был устаревшим."""
    try:
        with open(SEED_FILE, "w", encoding="utf-8") as f:
            json.dump(dump, f, ensure_ascii=False, indent=1)
        logger.info("Запасной файл %s обновлён: %s постов", SEED_FILE, len(dump.get("items", [])))
    except Exception as e:
        logger.warning("Не удалось обновить запасной файл %s: %s", SEED_FILE, e)


async def _send_tg_backup(user_id: int, reason: str = "Ежедневная") -> None:
    global _backup_in_flight
    if _backup_in_flight:
        return
    _backup_in_flight = True
    try:
        dump = db.export_json()
        _refresh_seed(dump)
        raw = json.dumps(dump, ensure_ascii=False, indent=1).encode("utf-8")
        github_ok = await _push_github_file(_github_backup_path(user_id), raw)
        await client.send_file(
            user_id,
            file=raw,
            file_name="tg_saver_export.json",
            caption=f"🗄 Копия экспорта ({reason})",
        )
        db.set_setting("last_export_count", str(db.count_all_items()))
        _maybe_forward_backup_to_channel(raw, reason=reason, user_id=user_id)
        note = (
            "📦 Это автоматическая копия твоего архива — страховка от потери данных "
            "(например, если сервер сбросит БД). Файл может пригодиться для восстановления.\n\n"
            "⚙️ Функция настраивается: отправлять копию каждые N постов и/или ежедневно — "
            "см. «Копия экспорта» в ⚙️ Настройки."
        )
        if BACKUP_CHANNEL_ID:
            note += "\n\n🔗 Снимок также сохранён в резервный канал."
        if GITHUB_TOKEN:
            note += "\n\n🔐 Экспорт также отправлен в репозиторий на GitHub — при потере БД бот сам восстановится из него."
        try:
            await client.send_message(user_id, note)
        except Exception:
            pass
    finally:
        _backup_in_flight = False


def _maybe_forward_backup_to_channel(raw: bytes, reason: str, user_id: int | None = None) -> None:
    """Кладёт копию экспорта в приватный канал-хранилище (бот = админ → может прочитать обратно)."""
    if not BACKUP_CHANNEL_ID:
        return
    try:
        asyncio.create_task(_send_backup_to_channel(raw, reason, user_id))
    except Exception as e:
        logger.warning("Копия в канал-хранилище не поставлена: %s", e)


async def _send_backup_to_channel(raw: bytes, reason: str, user_id: int | None = None) -> None:
    try:
        uid = int(user_id or 0) or 0
        fname = f"tg_saver_export_user{uid}_{datetime.utcnow():%Y%m%d_%H%M}.json" if uid else "tg_saver_export.json"
        dump = json.loads(raw.decode("utf-8")) if isinstance(raw, (bytes, bytearray)) else {}
        n_items = len(dump.get("items", [])) if isinstance(dump, dict) else 0
        n_cats = len(dump.get("categories", [])) if isinstance(dump, dict) else 0
        caption = (
            f"🗄 Снимок архива (user{uid}) · {reason}\n"
            f"📦 Постов: {n_items} · 📂 Категорий: {n_cats}\n"
            f"🕓 {datetime.utcnow():%d.%m.%Y %H:%M} (UTC)"
        )
        await client.send_file(BACKUP_CHANNEL_ID, file=raw, file_name=fname, caption=caption)
        logger.info("Снимок экспорта отправлен в канал-хранилище (%s, user=%s)", reason, uid or "-")
    except Exception as e:
        logger.warning("Не удалось отправить снимок в канал-хранилище: %s", e)


def _github_backup_path(user_id: int | None) -> str:
    uid = int(user_id or 0) or 0
    fname = f"tg_saver_export_user{uid}.json" if uid else "tg_saver_export.json"
    return f"{GITHUB_PATH}/{fname}" if GITHUB_PATH else fname


SESSION_REMOTE_PATH = f"{GITHUB_PATH}/session_bot.b64" if GITHUB_PATH else "session_bot.b64"


async def _restore_telegram_session() -> None:
    """Если сессия потеряна (эфемерный диск) — достаёт её из GitHub-синка (base64).

    Без этого каждый рестарт делает новую авторизацию Telegram, что рано или поздно
    ловит FloodWait на ImportBotAuthorizationRequest и кладёт бот надолго."""
    if not GITHUB_TOKEN or os.path.exists(SESSION_FILE):
        return
    try:
        got = await _fetch_github_file(SESSION_REMOTE_PATH)
        if not got:
            logger.info("GitHub-сессии нет — будет создана новая авторизация Telegram.")
            return
        raw = base64.b64decode(got[1])
        if not raw:
            return
        d = os.path.dirname(SESSION_FILE) or "."
        os.makedirs(d, exist_ok=True)
        with open(SESSION_FILE, "wb") as f:
            f.write(raw)
        logger.info("Сессия Telegram восстановлена из GitHub-синка (%s байт)", len(raw))
    except Exception as e:
        logger.warning("Не удалось восстановить сессию из GitHub: %s", e)


async def _store_telegram_session() -> None:
    """Пушит сессию в GitHub-синк, чтобы следующие рестарты не делали новую авторизацию."""
    if not GITHUB_TOKEN:
        return
    try:
        if not os.path.exists(SESSION_FILE):
            return
        raw = base64.b64encode(open(SESSION_FILE, "rb").read())
        ok = await _push_github_file(SESSION_REMOTE_PATH, raw)
        if ok:
            logger.info("Сессия Telegram отправлена в GitHub-синк")
    except Exception as e:
        logger.warning("Не удалось сохранить сессию в GitHub: %s", e)


async def _fetch_github_file(path: str) -> tuple[int, bytes] | None:
    """GET сырого файла из репо через GitHub Contents API.
    Возвращает (sha, содержимое) или None, если файла нет/ошибка."""
    token = GITHUB_TOKEN
    if not token:
        return None
    try:
        import aiohttp

        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"Bearer {token}",
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status == 404:
                    return None
                if resp.status != 200:
                    logger.warning("GitHub GET %s: HTTP %s", path, resp.status)
                    return None
                data = await resp.json()
        import base64

        sha = data.get("sha")
        content = base64.b64decode(data.get("content", ""))
        return (sha, content)
    except Exception as e:
        logger.warning("GitHub чтение %s не удалось: %s", path, e)
        return None


async def _push_github_file(path: str, raw: bytes) -> bool:
    """Создаёт/обновляет файл в репо (Contents API). True — успех."""
    token = GITHUB_TOKEN
    if not token:
        return False
    try:
        import base64
        import aiohttp

        got = await _fetch_github_file(path)
        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }
        body: dict = {
            "message": f"backup: обновление экспорта ({datetime.utcnow():%Y-%m-%d %H:%M} UTC)",
            "content": base64.b64encode(raw).decode(),
            "branch": GITHUB_BRANCH,
        }
        if got:
            body["sha"] = got[0]
        async with aiohttp.ClientSession() as session:
            async with session.put(url, headers=headers, json=body, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                if resp.status not in (200, 201):
                    err_text = await resp.text()
                    logger.warning("GitHub PUT %s: HTTP %s: %s", path, resp.status, err_text[:300])
                    return False
        logger.info("GitHub: экспорт запушен в %s", path)
        return True
    except Exception as e:
        logger.warning("GitHub запись %s не удалась: %s", path, e)
        return False


async def _restore_from_github() -> tuple[bool, str]:
    """Скачивает последний экспорт пользователя из репозитория и импортирует."""
    if not GITHUB_TOKEN:
        return False, "GITHUB_TOKEN не задан"
    uid = db.current_user_id()
    path = _github_backup_path(uid)
    got = await _fetch_github_file(path)
    if not got:
        return False, f"в репо нет файла {path} или ошибка чтения"
    try:
        dump = json.loads(got[1].decode("utf-8"))
    except Exception as e:
        return False, f"файл {path} не читается как JSON: {e}"
    if not isinstance(dump, dict) or not dump.get("items"):
        return False, f"файл {path} не похож на экспорт"
    res = db.import_json(dump)
    if res.get("items"):
        logger.info("Восстановлено из GitHub (user=%s, %s): items=%s cats=%s tags=%s",
                    uid, path, res["items"], res["categories"], res["tags"])
        return True, path
    return False, f"импорт {path} дал пустой результат"


async def _restore_from_channel() -> bool:
    """Импортирует последний снимок экспорта пользователя из канала-хранилища
    (бот-админ может читать историю канала). Снимки помечены id пользователя в имени файла."""
    if not BACKUP_CHANNEL_ID:
        logger.info("_restore_from_channel: BACKUP_CHANNEL_ID не задан")
        return False, "BACKUP_CHANNEL_ID не задан"
    uid = db.current_user_id()
    want = f"user{uid}" if uid else ""
    logger.info("_restore_from_channel: user=%s want=%r", uid, want)
    try:
        msgs = await client.get_messages(BACKUP_CHANNEL_ID, limit=60)
    except Exception as e:
        logger.warning("_restore_from_channel: не удалось прочитать канал (user=%s): %s", uid, e)
        return False, f"ошибка чтения канала: {e}"
    logger.info("_restore_from_channel: получено сообщений из канала: %s", len(msgs))
    skipped = []
    for m in msgs:
        if not (m.document and m.file):
            continue
        fname = (getattr(m.file, "name", "") or "").lower()
        caption = (m.message or "").lower()
        is_snapshot = fname.endswith(".json") or "снимок архива" in caption
        if not is_snapshot:
            skipped.append(f"ид{m.id}:файл{fname!r}:не снимок")
            continue
        suid = None
        imported = re.search(r"user(\d+)_", fname)
        if imported:
            suid = imported.group(1)
        elif fname.startswith("tg_saver_export_user") and fname.endswith(".json"):
            imported = re.match(r"tg_saver_export_user(\d+)\.json", fname)
            suid = imported.group(1) if imported else suid
        if want and suid is not None and suid != str(uid):
            skipped.append(f"ид{m.id}:другой юзер {suid}")
            continue
        logger.info("_restore_from_channel: подходящий снимок %r (id=%s), скачиваю", fname, m.id)
        try:
            raw = await client.download_media(m, file=bytes)
        except Exception as e:
            logger.warning("_restore_from_channel: скачивание %s не удалось: %s", fname, e)
            skipped.append(f"ид{m.id}:скачивание: {e}")
            continue
        if not raw:
            logger.warning("_restore_from_channel: скачивание %s вернуло пусто", fname)
            skipped.append(f"ид{m.id}:скачивание пусто")
            continue
        try:
            dump = json.loads(raw.decode("utf-8"))
        except Exception as e:
            logger.warning("_restore_from_channel: %s не читается как JSON: %s", fname, e)
            skipped.append(f"ид{m.id}:не json: {e}")
            continue
        if not isinstance(dump, dict) or not dump.get("items"):
            logger.warning("_restore_from_channel: %s не похож на экспорт (ключ items отсутствует)", fname)
            skipped.append(f"ид{m.id}:нет items")
            continue
        res = db.import_json(dump)
        if res.get("items"):
            logger.info(
                "Восстановлено из канала-хранилища (user=%s): items=%s cats=%s tags=%s",
                uid, res["items"], res["categories"], res["tags"],
            )
            return True, ""
        return False, f"импорт {fname!r} дал пустой результат"
    reason = f"получено {len(msgs)} сообщений; подходящий снимок не найден"
    if skipped:
        reason += "; проверял: " + ", ".join(skipped[:30])
    logger.warning("_restore_from_channel: %s", reason)
    return False, reason


def _posts_since_export() -> int:
    try:
        last = int(db.get_setting("last_export_count", "0") or "0")
    except Exception:
        last = 0
    delta = db.count_all_items() - last
    return max(0, delta)


def _maybe_auto_backup_tg(user_id: int) -> None:
    """После сохранения поста: шлёт TG-копию, если с последней копии накопилось N новых постов."""
    if not user_id:
        return
    if db.get_setting("backup_every_posts", "5") != "0":
        try:
            n = int(db.get_setting("backup_every_posts", "5"))
        except Exception:
            n = 5
        if n > 0 and _posts_since_export() >= n:
            try:
                asyncio.create_task(_send_tg_backup(user_id, reason=f"каждые {n} постов"))
            except Exception as e:
                logger.warning("Авто-бэкап по N постов не удался: %s", e)


async def scheduler_loop():
    """Дайджест (воскресенье 07:00 UTC) и ежедневная копия экспорта (04:00 UTC) — для каждого юзера."""
    while True:
        await asyncio.sleep(3600)
        now = datetime.utcnow()
        for uid in db.all_users():
            try:
                prev = db.current_user_id()
                db.set_current_user(uid)
                _refresh_icon_overrides()
                if db.get_setting("weekly_digest", "1") == "1" and now.weekday() == 6 and now.hour == 7:
                    week_key = f"{now.isocalendar()[0]:04d}-{now.isocalendar()[1]:02d}"
                    if db.get_setting("last_digest_week", "") != week_key:
                        db.set_setting("last_digest_week", week_key)
                        await _send_weekly_digest(uid)
                if db.get_setting("backup_tg_daily", "1") == "1" and now.hour == 4:
                    today = now.strftime("%Y-%m-%d")
                    if db.get_setting("last_backup_tg_day", "") != today:
                        if _posts_since_export() <= 0:
                            db.set_setting("last_backup_tg_day", today)
                        else:
                            db.set_setting("last_backup_tg_day", today)
                            await _send_tg_backup(uid, reason="Ежедневная")
            except Exception as e:
                logger.warning("Планировщик (user=%s): ошибка %s", uid, e)
            finally:
                db.set_current_user(prev)


def main():
    import time

    if API_ID <= 0 or not API_HASH:
        logger.error("Не заданы API_ID / API_HASH (my.telegram.org). Бот не запущен.")
        return

    db.init_db()
    if db.migrate_legacy():
        logger.info("Старая БД мигрирована в архив владельца (user_%s.db)", _cached_owner())
    _refresh_icon_overrides()
    logger.info("БД инициализирована")

    global client
    if MT_PROXY_HOST:
        client = TelegramClient(
            SESSION_FILE,
            API_ID,
            API_HASH,
            connection=ConnectionTcpMTProxyAbridged,
            proxy=(MT_PROXY_HOST, MT_PROXY_PORT, MT_PROXY_SECRET),
        )
    else:
        client = TelegramClient(SESSION_FILE, API_ID, API_HASH)

    client.add_event_handler(_safe_handler(on_new_message), events.NewMessage(incoming=True))
    client.add_event_handler(_safe_handler(on_album), events.Album())
    client.add_event_handler(_safe_handler(on_callback), events.CallbackQuery())
    client.sequential_updates = True

    async def start():
        await _restore_telegram_session()
        await client.start(bot_token=BOT_TOKEN)
        me = await client.get_me()
        logger.info(
            "Telethon-бот %s подключён%s",
            getattr(me, "username", "?"),
            f" через MTProxy {MT_PROXY_HOST}:{MT_PROXY_PORT}" if MT_PROXY_HOST else " напрямую",
        )
        await _store_telegram_session()
        try:
            owner = _cached_owner()
            if owner:
                prev = db.current_user_id()
                db.register_user(owner)
                db.set_current_user(owner)
                _refresh_icon_overrides()
                try:
                    removed = db.migrate_delete_broken_message_ids()
                    if removed:
                        logger.info("Удалены посты с битой привязкой к исходному сообщению: %s", removed)
                    _recover_enabled = db.get_setting("auto_recover_posts", "1") == "1"
                    dst = db.backup_db()
                    if dst:
                        logger.info("Стартовая резервная копия: %s", dst)
                    restored_gh = False
                    restored_seed = False
                    gh_reason = ""
                    if db.count_all_items() == 0:
                        ok, gh_reason = await _restore_from_github()
                        if ok:
                            restored_gh = True
                        elif _maybe_restore_db(owner):
                            restored_seed = True
                    if _recover_enabled:
                        n = db.count_all_items()
                        try:
                            if n == 0:
                                await client.send_message(
                                    owner,
                                    "🆘 Архив пуст (судя по всему, после перезапуска файлы БД не сохранились).\n\n"
                                    "Чтобы восстановить историю: перешли мне сюда последний файл "
                                    "«tg_saver_export.json» из этого чата — я импортирую его автоматически.",
                                )
                            elif restored_gh:
                                await client.send_message(
                                    owner,
                                    f"✅ БД была потеряна — восстановлено из GitHub-копии: {n} постов.",
                                )
                            elif restored_seed:
                                extra = f"\n\nПричина (для меня): {gh_reason}" if gh_reason else ""
                                await client.send_message(
                                    owner,
                                    f"⚠️ БД была потеряна, GitHub недоступен — восстановлены {n} постов "
                                    f"из запасного файла (он обновляется при каждом бэкапе, так что это свежий снимок)."
                                    f"{extra}",
                                )
                            else:
                                await client.send_message(
                                    owner,
                                    f"✅ Архив в порядке: сохранено {n} постов.",
                                )
                        except Exception:
                            pass
                    if db.get_setting("auto_heal_broken", "0") == "1":
                        moved = await _heal_broken_origins(owner)
                        if moved:
                            try:
                                await client.send_message(
                                    owner,
                                    f"🗑 Найдено {len(moved)} постов с битой привязкой к исходному сообщению "
                                    f"(ID: {', '.join(map(str, moved))}) — перенесены в корзину. "
                                    f"Их можно вернуть или удалить в «Корзине».",
                                )
                            except Exception:
                                pass
                finally:
                    db.set_current_user(prev)
        except Exception as e:
            logger.warning("Стартовая инициализация владельца не удалась: %s", e)
        await asyncio.gather(
            client.run_until_disconnected(),
            watchdog(),
            backup_loop(),
            scheduler_loop(),
            health_http(),
        )

    while True:
        try:
            client.loop.run_until_complete(start())
        except KeyboardInterrupt:
            break
        except FloodWaitError as e:
            wait = int(getattr(e, "seconds", 60) or 60) + 10
            logger.error(
                "Telegram FloodWait(%s) при авторизации — жду %s c, чтобы не продлевать лимит.",
                e.seconds, wait,
            )
            try:
                time.sleep(wait)
            except KeyboardInterrupt:
                break
        except Exception as e:
            logger.exception("Бот аварийно завершился: %s. Перезапуск через 5 с.", e)
        else:
            logger.warning("Соединение завершилось — перезапуск через 5 с.")
        try:
            time.sleep(5)
        except KeyboardInterrupt:
            break


client = None

if __name__ == "__main__":
    main()