import asyncio
import base64
import gzip
import hmac
import json
import logging
import logging.handlers
import os
import re
import secrets
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

from telethon import TelegramClient, events, Button, utils
from telethon.tl import functions, types
from telethon.errors import FloodWaitError
from telethon.network import ConnectionTcpMTProxyAbridged

from config import (
    API_HASH,
    API_ID,
    BACKUP_CHANNEL_ID,
    BOT_TOKEN,
    DB_PATH,
    EXT_MODELS_ALLOW,
    EXT_SECRET,
    EXT_DL_MAX_BYTES,
    EXT_DL_QUALITIES,
    GITHUB_BRANCH,
    GITHUB_PATH,
    GITHUB_REPO,
    GITHUB_TOKEN,
    GROQ_CATEGORIZE_MODEL,
    GROQ_FALLBACK_MODEL,
    GROQ_MODEL,
    GROQ_VISION_MODEL,
    HTTP_PORT,
    MT_PROXY_HOST,
    MT_PROXY_PORT,
    MT_PROXY_SECRET,
    TG_SOCKS_HOST,
    TG_SOCKS_PORT,
    TG_TRANSPORT,
    OWNER_ID,
    SEED_FILE,
    SESSION_FILE,
)

import analyzer
import assistant
import imghash
import webarchive
import share_summary
import savelinks
import extbackup
import ytqueue
import ytvideo
import categorizer as _cat
import database as db
import keyboards as _kb
import gamedeals
import watchers
import linkmeta
import pending
import postctx
from postctx import forward_source, has_aux, join_stored, strip_aux
from categorizer import (
    categorize,
    describe_image,
    llm_chat,
    normalize_summary,
    organize,
    propose_subgroups,
    strip_markdown,
)
from stt import transcribe_audio
from keyboards import (
    REPLY_BUTTONS,
    main_keyboard,
    root_categories_keyboard,
    service_keyboard,
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
    SETTINGS_MAIN,
    SETTINGS_MORE,
    SETTINGS_MORE_KEYS,
    theme_icon as _kb_theme_icon,
    tag_editor_keyboard,
    tag_palette_keyboard,
    tags_section_keyboard,
    manage_tags_keyboard,
    manage_tag_actions_keyboard,
    category_icon_keyboard,
    cf_choice_keyboard,
    DEFAULT_TAG_CIRCLES,
)

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)
_handlers: list[logging.Handler] = [
    logging.handlers.RotatingFileHandler(
        LOG_DIR / "bot.log", maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
]
if sys.stderr:  # под pythonw (скрытый запуск) консоли нет
    _handlers.append(logging.StreamHandler())
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=_handlers,
)
logger = logging.getLogger(__name__)


# ── Сбои — владельцу в Telegram ──────────────────────────────────────────────
# Раньше на любой сбой пользователь видел только «Что-то пошло не так», а
# причина оставалась в логах Render. Теперь каждая ошибка (уровень ERROR) коротко
# приходит владельцу — не чаще раза в час для одной и той же.
ALERT_EVERY = 3600
_alert_last: dict[tuple, float] = {}


def _alert_text(record: logging.LogRecord) -> str | None:
    import traceback

    msg = record.getMessage()
    lines = [f"🚨 Сбой в боте: {msg[:300]}"]
    if record.exc_info and record.exc_info[1] is not None:
        etype, evalue, tb = record.exc_info
        lines.append(f"{etype.__name__}: {str(evalue)[:300]}")
        frames = [f for f in traceback.extract_tb(tb) if "site-packages" not in f.filename]
        if frames:
            f = frames[-1]
            lines.append(f"Где: {f.filename.replace(chr(92), '/').rsplit('/', 1)[-1]}:{f.lineno} ({f.name})")
    lines.append("\nПодробности — в логах Render. Если повторится, пришли этот текст.")
    return "\n".join(lines)


async def _send_owner_alert(text: str) -> None:
    owner = _cached_owner()
    if not owner or client is None:
        return
    try:
        await client.send_message(int(owner), text, parse_mode=None, silent=True)
    except Exception as e:  # WARNING, не ERROR — иначе сами себя зациклим
        logger.warning("Не удалось отправить уведомление о сбое: %s", e)


class _OwnerAlertHandler(logging.Handler):
    def __init__(self):
        super().__init__(logging.ERROR)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            exc = record.exc_info[0].__name__ if record.exc_info and record.exc_info[0] else ""
            key = (record.getMessage()[:60], exc)
            now = time.time()
            if now - _alert_last.get(key, 0) < ALERT_EVERY:
                return
            _alert_last[key] = now
            text = _alert_text(record)
            loop = asyncio.get_event_loop()
            if text and loop.is_running():
                loop.create_task(_send_owner_alert(text))
        except Exception:
            pass


logging.getLogger().addHandler(_OwnerAlertHandler())


_update_sem = asyncio.Semaphore(3)


def _event_hint(event) -> str:
    """Что нажали/прислали — для сообщения о сбое."""
    data = getattr(event, "data", None)
    if isinstance(data, (bytes, bytearray)):
        d = data.decode("utf-8", "ignore")
        return "кнопка " + (_kb.LONG_DATA.get(d, d))[:60]
    msg = getattr(event, "message", None)
    txt = (getattr(msg, "text", None) or "") if msg is not None else ""
    if txt:
        return f"сообщение «{txt[:40]}»"
    return "событие " + type(event).__name__


def _safe_handler(fn, sem=None, watchdog: float | None = None):
    async def _report(event, text: str):
        try:
            if hasattr(event, "answer"):
                await event.answer(text, alert=True)
            elif hasattr(event, "reply"):
                await event.reply(text)
        except Exception:
            pass

    async def wrapper(event):
        try:
            if sem is not None:

                async def _run():
                    async with sem:
                        await fn(event)

                if watchdog:
                    await asyncio.wait_for(_run(), timeout=watchdog)
                else:
                    await _run()
            else:
                if watchdog:
                    await asyncio.wait_for(fn(event), timeout=watchdog)
                else:
                    await fn(event)
        except asyncio.TimeoutError:
            logger.error("Таймаут обработчика %s (%s)", getattr(fn, "__name__", fn), _event_hint(event))
            await _report(event, "⚠️ Обработка заняла слишком долго — отправь ещё раз, пожалуйста.")
        except Exception as e:
            logger.exception("Ошибка в обработчике %s (%s)", getattr(fn, "__name__", fn), _event_hint(event))
            owner = _cached_owner()
            mine = owner and str(getattr(event, "sender_id", "")) == str(owner)
            await _report(event, "⚠️ Что-то пошло не так — подробности пришлю отдельным сообщением."
                          if mine else "⚠️ Что-то пошло не так. Попробуй ещё раз.")

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


def _canonical_existing_cat(name: str) -> str:
    """Возвращает точное имя существующей категории без учёта регистра (иначе как есть)."""
    name = (name or "").strip()
    if not name:
        return name
    for c in _existing_category_names():
        if c.lower() == name.lower():
            return c
    return name


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


def _cf_bg_get():
    try:
        return json.loads(db.get_setting("last_cf_bg_vision", "") or "null")
    except Exception:
        return None


def _cf_bg_set(st: dict) -> None:
    db.set_setting("last_cf_bg_vision", json.dumps(st))


_cat.CF_BG_STORE = (_cf_bg_get, _cf_bg_set)


def _ai_icons() -> dict[str, str]:
    try:
        data = json.loads(db.get_setting("ai_icons", "{}") or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _refresh_icon_overrides():
    """Иконки: выбранные вручную важнее подобранных ИИ, те — важнее словаря по ключевым словам."""
    global _icon_overrides
    _icon_overrides = {**{k: v for k, v in _ai_icons().items() if v}, **db.get_icon_overrides()}
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
    "video_note": "Видео",
    "document": "Документы",
    "animation": "Видео",
    "text": "Заметки",
}

DEFAULT_SUMMARY = {
    "photo": "Фото",
    "video": "Видео",
    "audio": "Аудио",
    "voice": "Аудио",
    "video_note": "Кружок",
    "document": "Документ",
    "animation": "Анимация",
    "text": "Заметка",
}

BOT_USERNAME = ""   # заполняется при старте (get_me) — для ссылок t.me/<бот>
pending_search = {}
smart_query: dict[int, str] = {}   # chat_id → последний текст поиска (для «🤖 По смыслу»)
cf_sel = {}  # мультивыбор категорий/папок («☑️ Выбрать»): chat_id → {root, sel}
selections = {}
merge_flow = {}
unlock_sel = {}
tag_filter = {}
tag_create = {}
tag_icon_apply = {}
trash_sel = {}
nodata_batch = {}
undo_data = {}
dup_stage = {}
clarify_opts = {}
subfolders_plan = {}
auto_order_plan = {}
assistant_sessions: dict[int, dict] = {}   # chat_id → {until, history, ids, plan, off, msg_id}
ASSISTANT_UNDO_TTL = 24 * 3600
assistant_undo: dict[int, dict] = {}       # chat_id → снимок структуры до последнего «Применить»

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
    "weekly_file": "1",
    "media_copy": "1",
    "monthly_digest": "1",
    "cleanup_monthly": "1",
    "game_deals": "1",
    "anime_watch": "1",
    "github_watch": "1",
    "daily_read": "1",
    "backup_every_posts": "5",
    "auto_heal_broken": "0",
    "link_enrich": "1",
    "auto_recover_posts": "1",
    "night_recat": "1",
    "weekly_cleanup": "1",
    "on_this_day": "1",
    "article_text": "1",
    "link_check": "1",
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
    if getattr(msg, "video_note", None):
        add("video_note", msg.video_note)
    elif getattr(msg, "video", None):
        add("video", msg.video)
    if getattr(msg, "gif", None):
        add("animation", msg.gif)
    if getattr(msg, "audio", None):
        add("audio", msg.audio)
    if getattr(msg, "voice", None):
        add("voice", msg.voice)
    if not (getattr(msg, "photo", None) or getattr(msg, "video_note", None) or getattr(msg, "video", None)
            or getattr(msg, "gif", None) or getattr(msg, "audio", None) or getattr(msg, "voice", None)) and getattr(msg, "document", None):
        add("document", msg.document)
    return media


def _primary_content_type(media: list[dict]) -> str:
    return media[0]["t"] if media else "text"


async def _get_source_name(msg) -> str | None:
    fwd = getattr(msg, "forward", None)
    if fwd is None:
        return None
    # Сначала то, что Telegram уже прислал в апдейте (без лишних запросов).
    title, _username = forward_source(msg)
    if title:
        return title
    from_id = getattr(fwd, "from_id", None)
    if from_id is None:
        return None
    try:
        entity = await asyncio.wait_for(client.get_entity(from_id), timeout=8)
        return getattr(entity, "title", None) or getattr(entity, "username", None) or None
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
    analysis: dict,
    file_ids: list[dict],
    message_id: int,
    media_group_id: str | None = None,
    source_channel: str | None = None,
    dedup_note: str | None = None,
    default_category: str | None = None,
) -> dict:
    """Сохраняет пост и правит «⏳ Анализирую…» на «Сохранено в …».
    default_category — куда класть, если ИИ не ответил (иначе — по типу файла).
    Возвращает {"id", "category", "summary", "failed"}."""
    display = (analysis.get("display") or "").strip()
    llm_text = (analysis.get("llm_text") or "").strip()
    stored_text = join_stored(display, analysis.get("aux") or "")
    logger.info("SAVE-BEGIN ct=%s text=%r llm_len=%s", content_type, display[:50], len(llm_text))

    confidence, alternatives, failed, result = 1.0, [], False, None
    topic_rule_hit = None
    rule_cat = _channel_rule(source_channel)
    rule_text = postctx.clean_post_text(display)["text"] if rule_cat else ""
    if rule_cat and postctx.content_letters(rule_text) >= CHRULE_TEXT_LETTERS:
        category = rule_cat
        summary = normalize_summary(rule_text, 150) or DEFAULT_SUMMARY.get(content_type, "Сохранено")
        logger.info("SAVE: по правилу канала %s → %s (без ИИ)", source_channel, category)
    elif analysis.get("media_failed"):
        failed = True
        category = rule_cat or default_category or DEFAULT_CATEGORY.get(content_type, "Другое")
        summary = normalize_summary(strip_aux(stored_text)) or DEFAULT_SUMMARY.get(content_type, "Сохранено")
        logger.info("SAVE: медиа не распознано (лимиты ИИ) — в очередь на дораспознавание")
    elif llm_text:
        result = await _ai_categorize(llm_text, content_type, source_channel)
        if result is None:
            failed = True
            category = rule_cat or default_category or DEFAULT_CATEGORY.get(content_type, "Другое")
            summary = normalize_summary(strip_aux(stored_text) or llm_text) or DEFAULT_SUMMARY.get(content_type, "Сохранено")
        else:
            category = result.get("category", "Другое")
            summary = result.get("summary") or normalize_summary(llm_text)
            confidence = float(result.get("confidence", 1.0))
            alternatives = result.get("alternatives") or []
            trule = None if rule_cat else _topic_rule(_cat.clean_topics(result.get("topics") or []))
            if trule and trule[1] != category:
                logger.info("SAVE: по правилу темы «%s» → %s (ИИ предлагал %s)", trule[0], trule[1], category)
                category = trule[1]
                topic_rule_hit = trule[0]
        logger.info("SAVE categorize done ok=%s cat=%s conf=%.2f", not failed, category, confidence)
    else:
        category = DEFAULT_CATEGORY.get(content_type, "Другое")
        m = re.search(r"https?://([^/\s)]+)", display)
        summary = f"Ссылка на {m.group(1)}" if m else DEFAULT_SUMMARY.get(content_type, "Сохранено")

    item_id = db.save_item(
        category=category,
        content_type=content_type,
        summary=summary,
        original_text=stored_text,
        file_id=file_ids[0]["f"] if file_ids else None,
        message_id=message_id,
        chat_id=chat_id,
        media_group_id=media_group_id,
        file_ids=file_ids or None,
        source_channel=source_channel,
        file_unique=_file_unique_str(file_ids),
    )
    if failed:
        db.queue_llm_retry(item_id, category)
    if result and result.get("topics") is not None and not failed:
        db.set_item_topics({item_id: result.get("topics") or []})
    proposed = None if failed or not result else result.get("proposed_new")
    if proposed:
        _note_newcat_offer(proposed, item_id)
    sim_note, sim_row = _similar_note(item_id, analysis.get("phash"))

    _auto_order_pending[chat_id] = _auto_order_pending.get(chat_id, 0) + 1
    _arm_auto_order(client, chat_id, db.current_user_id() or 0)

    _maybe_auto_backup_tg(db.current_user_id() or 0)

    emoji = theme_icon(category, "📦")
    count = f" ({len(file_ids)} медиа)" if file_ids and len(file_ids) > 1 else ""
    chan = f"\n📡 {source_channel}" if source_channel else ""
    if rule_cat and category == rule_cat:
        chan += " · по правилу канала"
    elif topic_rule_hit:
        chan += f"\n🏷 по правилу темы «{topic_rule_hit}»"
    dnote = f"\n\n{dedup_note}" if dedup_note else ""
    qact = db.get_setting("quick_actions", "1") == "1"
    buttons = quick_actions_keyboard(item_id, category) if qact else [[Button.inline("👌 Ок", data="dismiss")]]
    note = ""
    if failed:
        note = "\n\n⏳ ИИ сейчас перегружен — дораспознаю пост сам чуть позже."
    elif confidence < LOW_CONFIDENCE:
        alt_row = _alt_buttons(item_id, category, alternatives)
        if alt_row:
            note = "\n\n🤔 Не уверен в папке — если не то, выбери:"
            buttons = [alt_row] + list(buttons)
    if proposed:
        same = len(_newcat_offers().get(proposed, []))
        if same < NEWCAT_ASK_AT:
            note += (f"\n\n💡 Похоже на новую тему «{proposed}» ({same} из {NEWCAT_ASK_AT}). "
                     f"Наберётся {NEWCAT_ASK_AT} таких поста — предложу завести папку.")
    if sim_row:
        buttons = list(buttons) + [sim_row]
    try:
        await processing.edit(
            f"{emoji} Сохранено в «{category}»{count}{chan}\n\n{summary}{note}{sim_note}{dnote}",
            buttons=buttons,
        )
    except Exception:
        pass
    logger.info("Сохранено item=%s category=%s channel=%s mid=%s", item_id, category, source_channel or "-", message_id)
    if proposed:
        try:
            await _maybe_ask_newcat(chat_id, proposed)
        except Exception:
            logger.warning("Вопрос про новую категорию не отправлен", exc_info=True)
    return {"id": item_id, "category": category, "summary": summary, "failed": failed}


LOW_CONFIDENCE = 0.6


def _alt_buttons(item_id: int, category: str, alternatives: list[str]) -> list:
    """Кнопки «→ Другая категория» при низкой уверенности ИИ (используют mvset:)."""
    existing = set(_existing_category_names())
    row = []
    for alt in alternatives:
        alt = _canonical_existing_cat(alt)
        if alt == category or alt not in existing:
            continue
        data = f"mvset:{item_id}:{alt}"
        if len(data.encode("utf-8")) > 64:  # лимит Telegram на callback data
            continue
        row.append(Button.inline(f"→ {alt}", data=data))
    return row[:2]


async def _ai_categorize(
    llm_text: str,
    content_type: str,
    source: str | None,
    exclude_id: int | None = None,
    hint: str | None = None,
    model: str | None = None,
    fallback_model: str | None = "",
) -> dict | None:
    """categorize() + личный контекст: похожие посты из архива и привычки канала.
    None — если ИИ не ответил (вызывающий решает: фолбэк, очередь или отказ)."""
    text = llm_text
    if hint:
        text = f"{llm_text}\n\nУточнение пользователя: {hint}"
    try:
        examples = db.similar_items(llm_text, limit=4, exclude_id=exclude_id)
    except Exception:
        logger.warning("similar_items упал", exc_info=True)
        examples = []
    try:
        prior = db.channel_category_counts(source)
    except Exception:
        prior = []
    result = await categorize(
        text,
        content_type=content_type,
        source=source,
        categories=_existing_category_names(),
        examples=examples,
        channel_prior=prior,
        model=model,
        fallback_model=fallback_model,
    )
    if result.get("llm_ok") is False:
        return None
    result["category"] = _canonical_existing_cat(result.get("category") or "Другое")
    existing = _existing_category_names()
    if not hint and existing and result["category"] not in existing:
        # ИИ придумал новую категорию. Раньше она создавалась молча — так появлялись
        # «Shikimori» и прочие однопостовые. Теперь пост идёт в ближайшую существующую,
        # а новую категорию бот предлагает кнопкой.
        proposed = result["category"]
        alt = next((a for a in (_canonical_existing_cat(x) for x in result.get("alternatives") or [])
                    if a in existing), None)
        if not alt and exclude_id:  # перераспознавание: лучше оставить, где лежал, чем в «Другое»
            cur_it = db.get_item(exclude_id)
            if cur_it and cur_it["category"] in existing and cur_it["category"] not in _STUB_CATS:
                alt = cur_it["category"]
        result.update({"category": alt or "Другое", "proposed_new": proposed,
                       "confidence": min(float(result.get("confidence") or 0.5), 0.59)})
        if proposed in _newcat_declined():
            result.pop("proposed_new", None)
        elif exclude_id:
            _note_newcat_offer(proposed, exclude_id)
    rule = None if hint else _channel_rule(source)
    if rule:
        result.update({"category": rule, "confidence": 1.0, "alternatives": [], "by_rule": True})
    return result


def _newcat_offers() -> dict[str, list[int]]:
    try:
        data = json.loads(db.get_setting("newcat_offers", "{}") or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _note_newcat_offer(name: str, item_id: int) -> None:
    """Запоминает, что ИИ хотел для поста новую категорию name (до 30 имён)."""
    offers = _newcat_offers()
    for k in list(offers):
        offers[k] = [i for i in offers[k] if i != item_id]
        if not offers[k]:
            offers.pop(k)
    offers.setdefault(name, []).append(int(item_id))
    if len(offers) > 30:
        for k in list(offers)[: len(offers) - 30]:
            offers.pop(k)
    db.set_setting("newcat_offers", json.dumps(offers, ensure_ascii=False))


NEWCAT_ASK_AT = 3


def _newcat_declined() -> list[str]:
    try:
        return list(json.loads(db.get_setting("newcat_declined", "[]") or "[]"))
    except Exception:
        return []


async def _maybe_ask_newcat(chat_id: int, name: str) -> None:
    """Когда ИИ в NEWCAT_ASK_AT-й раз хочет одну и ту же новую категорию — спрашиваем один раз."""
    ids = _newcat_offers().get(name, [])
    if len(ids) < NEWCAT_ASK_AT:
        return
    asked = set(json.loads(db.get_setting("newcat_asked", "[]") or "[]"))
    if name in asked:
        return
    asked.add(name)
    db.set_setting("newcat_asked", json.dumps(sorted(asked)[-60:], ensure_ascii=False))
    lines = [f"💡 Набралось {len(ids)} поста, для которых ИИ хочет новую папку «{name}»:", ""]
    for iid in ids[:5]:
        it = db.get_item(iid)
        if it:
            lines.append(f"• #{iid} сейчас в «{it['category']}»: {_fmt_short(it.get('summary') or '', 60)}")
    lines.append("\nСоздать её и перенести эти посты?")
    await client.send_message(chat_id, "\n".join(lines), parse_mode=None, buttons=[
        [Button.inline(f"🆕 Создать «{name[:28]}» ({len(ids)})", data=f"newcat:{ids[-1]}")],
        [Button.inline("🙅 Не надо, не предлагай", data=f"newcatno:{ids[-1]}")],
    ])


def _newcat_for_item(item_id: int) -> str | None:
    for name, ids in _newcat_offers().items():
        if item_id in ids:
            return name
    return None


async def handle_newcat_callback(event, item_id: int):
    name = _newcat_for_item(item_id)
    if not name:
        await event.answer("Предложение устарело", alert=True)
        return
    offers = _newcat_offers()
    ids = offers.pop(name, [])
    db.set_setting("newcat_offers", json.dumps(offers, ensure_ascii=False))
    if not db.get_category_row(name):
        ok, err = db.create_category(name)
        if not ok:
            await event.answer(f"Не вышло: {err}", alert=True)
            return
    moved = 0
    for iid in ids:
        it = db.get_item(iid)
        if it and not it.get("locked"):
            db.update_item_category(iid, name)
            moved += 1
    asyncio.create_task(_icons_for_new([name]))
    await event.respond(f"🆕 Создал папку «{name}» и перенёс туда постов: {moved}.", buttons=main_keyboard())
    await show_item_view(event, item_id)


def _analysis_flags() -> dict:
    return {
        "vision_on": db.get_setting("photo_vision", "1") == "1",
        "audio_on": db.get_setting("audio_vision", "1") == "1",
        "link_enrich_on": db.get_setting("link_enrich", "1") == "1",
    }


async def _analyze(client: TelegramClient, msgs: list, content_type: str) -> dict:
    try:
        return await analyzer.analyze(client, msgs, content_type, **_analysis_flags())
    except Exception:
        logger.warning("Разбор поста упал — сохраняю по тексту", exc_info=True)
        display = "\n\n".join((getattr(m, "text", None) or "").strip() for m in msgs if getattr(m, "text", None))
        return {"display": display, "llm_text": strip_markdown(display), "aux": "", "source": None, "username": None}


async def _save_single(client: TelegramClient, msg, media_group_id: str | None = None):
    media = _msg_media(msg)
    content_type = _primary_content_type(media)
    logger.info("SAVE-SINGLE ct=%s text=%r media=%s group=%s", content_type, (msg.text or "")[:60], len(media), media_group_id)
    processing = await msg.reply("⏳ Анализирую...")

    if not media_group_id:
        raw_text = (msg.text or "").strip()
        primary = media[0]["f"] if media else None
        dup = db.find_dup_media(primary) if primary else db.find_dup_text(raw_text)
        others = []
        if db.get_setting("dedup_check", "1") == "1" and _media_uniques(media):
            others = [d for d in db.find_dupes(_media_uniques(media)) if not dup or d["id"] != dup["id"]]
        if dup or others:
            logger.info("DUP-ASK dup=%s others=%s", dup["id"] if dup else None, [d["id"] for d in others[:3]])
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
                await processing.delete()
            except Exception:
                try:
                    await processing.edit("⚠️ Дубль обнаружен (вопрос ниже).")
                except Exception:
                    pass
            try:
                await msg.reply(
                    ask_text,
                    buttons=[
                        [Button.inline("✅ Да, сохранить", data="dup_save_yes")],
                        [Button.inline("❌ Нет, не надо", data="dup_save_no")],
                    ],
                )
                logger.info("DUP-ASK question sent")
            except Exception:
                logger.warning("DUP-ASK question send failed", exc_info=True)
            return

    analysis = await _analyze(client, [msg], content_type)
    await _save(
        client,
        chat_id=msg.chat_id,
        processing=processing,
        content_type=content_type,
        analysis=analysis,
        file_ids=media,
        message_id=msg.id,
        media_group_id=media_group_id,
        source_channel=analysis.get("source") or await _get_source_name(msg),
    )


async def cmd_start(event):
    await event.respond(
        "👋 Привет! Я бот для сохранения контента.\n\n"
        "Просто пересылай мне сообщения, посты, альбомы с фото и видео — "
        "я сам пойму, о чём пост, и разложу по папкам.\n\n"
        "Кнопки под сообщением — папки, помощник, поиск, последние посты.\n"
        "⚙️ Настройки — автозамок, распознавание фото и голоса, быстрые действия.\n"
        "Также доступны команды: /categories /search /stats /recent /export /settings /help",
        buttons=main_keyboard(),
    )


_INLINE_TYPES = {"video": "video", "animation": "gif", "video_note": "video", "audio": "audio",
                 "voice": "voice", "document": "file"}


_INLINE_ALIASES: dict[str, list[str]] = {}   # запрос → слова от ИИ (кэш: inline шлёт запрос на каждую букву)


async def _inline_search(q: str) -> list[dict]:
    """Слова (с транслитом и раскладкой) → если нашлось мало, ИИ подсказывает, как ещё
    пишут название (киберпанк → cyberpunk, 2077), и ищем по этим словам. ИИ ждём ≤3,5 с."""
    items = db.search_items(q)[:20]
    parts = q.split()
    # «ван пис»: короткие слова находятся внутри других («описание», «Иван») — такому
    # совпадению не верим и тоже спрашиваем ИИ (one piece), его ответ ставим первым
    shaky = len(parts) > 1 and all(len(p) <= 3 for p in parts)
    if (len(items) >= 5 and not shaky) or len(q) < 3:
        return items
    key = q.casefold().strip()
    words = _INLINE_ALIASES.get(key)
    if words is None:
        try:
            words = await asyncio.wait_for(_cat.name_variants(q), 3.5)
        except Exception:
            words = None   # не дождались — в кэш не кладём, попробуем на следующей букве
        if words is not None:
            if len(_INLINE_ALIASES) > 300:
                _INLINE_ALIASES.clear()
            _INLINE_ALIASES[key] = words
    if not words:
        return items
    seen = set() if shaky else {it["id"] for it in items}
    # название — с начала слова: «rat» найдёт «rats», но не «Scratch» и не «generation»
    name_re = [re.compile(rf"(?<!\w){re.escape(w)}\w{{0,3}}(?!\w)") for w in words if len(w) >= 3]
    scored = []
    for r in db.items_for_smart_search():
        if r["id"] in seen:
            continue
        hay = f"{r['summary']} {r['original_text']} {r['category']} {r['source_channel']} {r['tags']} {r.get('comment') or ''}"
        hay = hay.casefold().replace("ё", "е")
        sc = sum(1 for rx in name_re if rx.search(hay))
        if sc:
            scored.append((sc, r))
    scored.sort(key=lambda x: (-x[0], -x[1]["id"]))
    named = [r for _, r in scored]
    if shaky:  # ИИ узнал название — случайные совпадения коротких слов уже не нужны
        return named[:20] if named else items
    return items + named[: 20 - len(items)]


async def on_inline(event):
    """@бот запрос в любом чате: найденные посты из своего архива — отправить другу в один тап.
    Пустой запрос — последние сохранённые."""
    uid = _activate_user(event.sender_id)
    q = (event.text or "").strip()
    try:
        items = await _inline_search(q) if q else db.recent_items(20)
        priv = db.private_categories()
        if priv:   # инлайн-ответ видят в чужих чатах — личное туда никогда не попадает
            items = [it for it in items if (db.get_item(it["id"]) or {}).get("category") not in priv]
    except Exception:
        items = []
    if not items:
        await event.answer([], cache_time=5, private=True,
                           switch_pm="Ничего не нашёл — открыть бота" if q else "Архив пуст — открыть бота",
                           switch_pm_param="inline")
        return
    b = event.builder
    arts, rich = [], []
    for it in items:
        full = db.get_item(it["id"])
        if not full:
            continue
        cat = full["category"]
        summary = full.get("summary") or "—"
        # другу — без подвала канала («Подписаться на …»): премиум-эмодзи из него бот
        # всё равно показать не может (Telegram не даёт ботам), получались «🟦🟦»
        caption = (postctx.strip_footer_md(strip_aux(full.get("original_text") or "")) or summary)[:1000]
        title = f"{theme_icon(cat, '📦')} {_fmt_short(summary, 60)}"
        descr = f"{cat} · #{full['id']}" + (f" · {full['source_channel']}" if full.get("source_channel") else "")
        art = await b.article(title=title, description=descr, text=caption, id=f"a{full['id']}", link_preview=False)
        arts.append(art)
        res = art
        media = db._parse_file_ids(full.get("file_ids")) or []
        if media and isinstance(media[0], dict) and media[0].get("f"):
            try:
                ref = utils.resolve_bot_file_id(media[0]["f"])
                t = media[0].get("t") or full.get("content_type")
                if t == "photo":
                    res = await b.photo(ref, text=caption, id=f"m{full['id']}")
                elif ref is not None:
                    res = await b.document(ref, title=title, description=descr, text=caption, id=f"m{full['id']}",
                                           type=_INLINE_TYPES.get(t, "file"))
            except Exception:
                res = art
        rich.append(res)
    try:
        await event.answer(rich, cache_time=5, private=True)
    except Exception as e:
        # file_id мог устареть — тогда хотя бы текстом
        logger.info("Inline: с медиа не вышло (%s) — отвечаю текстом", e)
        await event.answer(arts, cache_time=5, private=True)
    logger.info("Inline: user=%s q=%r → %s", uid, q[:40], len(rich))


async def send_item_to_chat(chat_id: int, item_id: int) -> bool:
    """«📤 Прислать в чат» из мини-приложения: пост целиком в чат с ботом."""
    item = db.get_item(item_id)
    if not item:
        return False

    class _Ev:
        pass
    ev = _Ev()
    ev.chat_id = chat_id
    try:
        if await _resend_from_original(client, ev, item):
            return True
        refs, caption = await _resend(item)
        cap = caption[:1024] if caption else None
        if len(refs) > 1:
            await client.send_file(chat_id, refs, caption=cap)
        elif refs:
            await client.send_file(chat_id, refs[0], caption=cap)
        else:
            await client.send_message(chat_id, caption or item.get("summary") or "—")
        return True
    except Exception:
        logger.warning("Мини-приложение: пост #%s не отправился", item_id, exc_info=True)
        return False


def _app_button(text: str = "📚 Открыть архив"):
    url = webarchive.app_url()
    if not url:
        return None
    # Telethon 1.46 (его ставит Render) заменил KeyboardButtonWebView на общий
    # KeyboardInlineButton с типом — поддерживаем обе версии
    if hasattr(types, "InlineButtonTypeWebView"):
        return types.KeyboardInlineButton(text, types.InlineButtonTypeWebView(url))
    return types.KeyboardButtonWebView(text, url)


async def setup_menu_button() -> None:
    """Кнопка «📚 Архив» слева от поля ввода в чате с ботом — открывает мини-приложение."""
    url = webarchive.app_url()
    if not url:
        return
    try:
        await client(functions.bots.SetBotMenuButtonRequest(
            user_id=types.InputUserEmpty(), button=types.BotMenuButton(text="📚 Архив", url=url)))
        logger.info("Кнопка меню «📚 Архив» → %s", url)
    except Exception as e:
        logger.warning("Кнопку меню поставить не вышло: %s", e)


async def cmd_web(event):
    uid = db.current_user_id() or event.sender_id
    link = webarchive.archive_link(uid)
    app_btn = _app_button()
    if not link or not app_btn:
        await event.respond("🌐 Веб-архив недоступен: у сервера не задан PUBLIC_URL.")
        return
    await event.respond(
        "📚 Архив с картинками: плитка, папки, поиск по словам и по смыслу.\n\n"
        "• «📚 Открыть архив» — прямо здесь, в Telegram (ещё есть кнопка «📚 Архив» слева от поля ввода).\n"
        f"• «🔗 Для браузера» — ссылка для компьютера. Она личная и действует {webarchive.TOKEN_DAYS} дней, "
        "не пересылай её.",
        buttons=[[app_btn], [Button.url("🔗 Для браузера", link)]], parse_mode=None, link_preview=False)


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
        return f"Заметка: {old[:40]!r} → {new[:40]!r}"
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


DUP_KINDS = {
    "file": "тот же самый файл сохранён несколько раз",
    "text": "одинаковый текст — похоже, пост переслали дважды",
    "img": "очень похожие картинки — проверь: бывает, что это разные мемы на одном шаблоне",
}
_dup_skip: dict[int, set] = {}


def _dup_ignored() -> set:
    try:
        return set(json.loads(db.get_setting("dup_ignore", "[]") or "[]"))
    except Exception:
        return set()


def _dup_groups() -> list[tuple[str, list[int]]]:
    """Все кандидаты в дубли: [(вид, [id…])], без отмеченных «это не дубли»."""
    out, seen = [], set()
    ign = _dup_ignored()

    def add(kind, ids):
        ids = sorted(set(ids))
        key = "-".join(map(str, ids))
        if len(ids) < 2 or key in seen or key in ign:
            return
        seen.add(key)
        out.append((kind, ids))
    for _tok, its in db.find_archive_dups(limit=200):
        add("file", [x["id"] for x in its])
    for ids in db.find_text_dups():
        add("text", ids)
    try:
        for a, b, _d in _similar_pairs():
            add("img", [a, b])
    except Exception as e:
        logger.info("Похожие картинки: %s", e)
    return out

# ---------------------------- 🧹 Уборка архива ----------------------------
# Кандидаты на выброс: мёртвые ссылки (без сохранённой статьи), копии (тот же файл, тот же текст,
# та же картинка — самый ранний пост остаётся), давно не открытые. Избранное, замок и личное не трогаем.
# «Оставить» запоминается навсегда; удаление — только в корзину.

CLEAN_OLD_DAYS = 90
_clean_cache: dict[int, tuple[float, list]] = {}
_clean_skip: dict[int, set] = {}


def _cleanup_keep() -> set:
    try:
        v = json.loads(db.get_setting("cleanup_keep", "[]") or "[]")
        return {int(x) for x in v} if isinstance(v, list) else set()
    except Exception:
        return set()


def cleanup_keep_add(ids) -> bool:
    keep = _cleanup_keep() | {int(i) for i in ids}
    db.set_setting("cleanup_keep", json.dumps(sorted(keep)[-5000:]))
    for _ts, lst in _clean_cache.values():
        lst[:] = [c for c in lst if c[0] not in keep]
    return True


def cleanup_candidates(limit: int = 150) -> list[tuple[int, str]]:
    """[(id поста, почему предлагаю убрать)] — мёртвые ссылки, копии, потом давно забытые."""
    rows = {r["id"]: r for r in db.items_for_smart_search()}
    keep, favs, priv = _cleanup_keep(), set(db.favorites()), db.private_categories()
    arts, opened = set(db.articles_brief()), _opened_at()
    out, used = [], set()

    def add(iid, why):
        r = rows.get(iid)
        if (r and iid not in used and iid not in keep and iid not in favs and not r["locked"]
                and r["category"] not in priv):
            used.add(iid)
            out.append((iid, why))

    for iid, since in sorted(dead_links().items()):
        if iid not in arts:
            add(iid, f"🪦 Ссылка больше не открывается (с {since})")
    ign = _dup_ignored()
    groups = [[x["id"] for x in its] for _t, its in db.find_archive_dups(limit=200)]
    groups += [list(g) for g in db.find_text_dups()]
    try:
        groups += [[a, b] for a, b, d in _similar_pairs() if d <= IMG_SAME_BITS]
    except Exception as e:
        logger.info("Уборка: похожие картинки: %s", e)
    for ids in groups:
        ids = sorted(set(ids))
        if len(ids) < 2 or "-".join(map(str, ids)) in ign:
            continue
        first = rows.get(ids[0])
        for iid in ids[1:]:
            add(iid, f"🔁 Копия: это уже сохранено раньше — #{ids[0]} «{_fmt_short((first or {}).get('summary') or '', 40)}»")
    cut = (datetime.utcnow() - timedelta(days=CLEAN_OLD_DAYS)).strftime("%Y-%m-%d")
    for iid in sorted(rows):
        d = (rows[iid]["created_at"] or "")[:10]
        if d and d < cut and opened.get(str(iid), "") < cut:
            add(iid, f"🕰 Сохранён {d[8:10]}.{d[5:7]}.{d[:4]} и больше 3 месяцев не открывался")
    return out[:limit]


async def _clean_list(uid: int, fresh: bool = False) -> list:
    hit = _clean_cache.get(uid)
    if hit and not fresh and time.time() - hit[0] < 600:
        return hit[1]
    lst = await asyncio.to_thread(cleanup_candidates)
    _clean_cache[uid] = (time.time(), lst)
    return lst


def web_cleanup() -> list[dict]:
    return [{"id": i, "why": w} for i, w in cleanup_candidates()]


def _clean_counts(cands) -> str:
    n = {"🪦": 0, "🔁": 0, "🕰": 0}
    for _i, why in cands:
        for k in n:
            if why.startswith(k):
                n[k] += 1
    parts = [f"• 🪦 ссылка больше не открывается: {n['🪦']}" if n["🪦"] else "",
             f"• 🔁 копии того, что уже есть: {n['🔁']}" if n["🔁"] else "",
             f"• 🕰 давно не открывал (больше 3 месяцев): {n['🕰']}" if n["🕰"] else ""]
    return "\n".join(p for p in parts if p)


async def show_cleanup(event, uid: int | None = None) -> None:
    uid = uid or db.current_user_id() or event.chat_id
    _clean_skip.pop(event.chat_id, None)
    cands = await _clean_list(uid, fresh=True)
    if not cands:
        await event.respond("🧹 Уборка архива\n\nУбирать нечего: мёртвых ссылок, копий и давно забытых постов нет 👍",
                            buttons=[[Button.inline("◀️ Сервис", data="service")]])
        return
    await event.respond(
        f"🧹 Уборка архива\n\nПостов, которые, возможно, уже не нужны: {len(cands)}\n{_clean_counts(cands)}\n\n"
        "Покажу по одному. «✅ Оставить» — больше не спрошу про этот пост, «🗑 В корзину» — его можно "
        "вернуть из корзины. Избранное, личное и посты под замком не трогаю.",
        buttons=[[Button.inline("🧹 Разобрать по одному", data="cln:go")],
                 [Button.inline("◀️ Сервис", data="service")]])


async def cleanup_next(event) -> None:
    uid = db.current_user_id() or event.chat_id
    skip = _clean_skip.setdefault(event.chat_id, set())
    cands = [c for c in await _clean_list(uid) if c[0] not in skip and db.get_item(c[0])]
    if not cands:
        await event.respond("🎉 Уборка закончена" + (" (отложенные покажу в следующий раз)." if skip else "."),
                            buttons=[[Button.inline("◀️ Сервис", data="service")]])
        _clean_skip.pop(event.chat_id, None)
        return
    iid, why = cands[0]
    it = db.get_item(iid)
    summary = strip_markdown(it.get("summary") or "") or "—"
    d = (it.get("created_at") or "")[:10]
    text = (f"🧹 Уборка · осталось {len(cands)}\n\n{why}\n\n{theme_icon(it['category'], '📦')} {it['category']}\n"
            f"{_fmt_short(summary, 300)}" + (f"\n📡 {it['source_channel']}" if it.get("source_channel") else "") +
            (f"\n📅 {d[8:10]}.{d[5:7]}.{d[:4]}" if d else "") + f" · #{iid}")
    rows = [[Button.inline("✅ Оставить", data=f"cln:k:{iid}"), Button.inline("🗑 В корзину", data=f"cln:d:{iid}")],
            [Button.inline("👀 Открыть пост", data=f"view:{iid}"), Button.inline("⏭ Позже", data=f"cln:s:{iid}")],
            [Button.inline("✖ Закончить", data="cln:x")]]
    chat_id = it.get("chat_id") or event.chat_id
    if it.get("message_id") and chat_id == event.chat_id:
        try:   # ответом на сам пост — видно, о чём речь (картинка, видео)
            await client.send_message(chat_id, text, buttons=rows, reply_to=it["message_id"], link_preview=False)
            return
        except Exception:
            pass
    await event.respond(text, buttons=rows, link_preview=False)


async def handle_cleanup_cb(event, data_s: str) -> None:
    parts = data_s.split(":")
    act = parts[1] if len(parts) > 1 else ""
    uid = db.current_user_id() or event.chat_id
    if act == "x":
        _clean_skip.pop(event.chat_id, None)
        await event.edit("🧹 Уборка остановлена. Продолжить: «🛠 Сервис → 🧹 Уборка архива» или /cleanup.", buttons=None)
        return
    if act == "go":
        await cleanup_next(event)
        return
    try:
        iid = int(parts[2])
    except (IndexError, ValueError):
        return
    if act == "k":
        cleanup_keep_add([iid])
        mark = "✅ Оставил"
    elif act == "d":
        mark = "🗑 В корзине" if web_trash([iid]) else "🔒 Не удалился: пост под замком"
    else:
        _clean_skip.setdefault(event.chat_id, set()).add(iid)
        mark = "⏭ Позже"
    hit = _clean_cache.get(uid)
    if hit and act in ("k", "d"):
        hit[1][:] = [c for c in hit[1] if c[0] != iid]
    try:
        await event.edit(buttons=[[Button.inline(mark, data="noop")]])
    except Exception:
        pass
    await cleanup_next(event)


async def send_monthly_cleanup(uid: int) -> bool:
    """15-го числа: если есть что убрать (от 5 постов) — короткое письмо с кнопкой разбора."""
    cands = await _clean_list(uid, fresh=True)
    if len(cands) < 5:
        return False
    await client.send_message(
        uid, f"🧹 Раз в месяц: в архиве {len(cands)} постов, которые, возможно, уже не нужны.\n{_clean_counts(cands)}\n\n"
             "Разберёшь по одному? Без тебя ничего не удаляется.",
        buttons=[[Button.inline("🧹 Разобрать по одному", data="cln:go")],
                 [Button.inline("🔕 Не присылать", data="settoggle|cleanup_monthly")]])
    return True


# ---------------------------- 📅 Календарь сохранений ----------------------------
RU_MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь", "Июль", "Август", "Сентябрь", "Октябрь",
             "Ноябрь", "Декабрь"]
RU_MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября",
                 "ноября", "декабря"]


def day_ids_msk(ym: str) -> dict[str, list[int]]:
    """{"ГГГГ-ММ-ДД": [id…]} за месяц ym; дни — по Москве."""
    y, m = int(ym[:4]), int(ym[5:7])
    start = datetime(y, m, 1) - timedelta(hours=3)
    end = datetime(y + (m == 12), m % 12 + 1, 1) - timedelta(hours=3)
    with db.get_connection() as conn:
        rows = conn.execute("SELECT id, created_at FROM saved_items WHERE created_at >= ? AND created_at < ? ORDER BY id DESC",
                            (start.strftime("%Y-%m-%d %H:%M:%S"), end.strftime("%Y-%m-%d %H:%M:%S"))).fetchall()
    out: dict[str, list[int]] = {}
    for iid, ca in rows:
        try:
            t = datetime.strptime((ca or "")[:19].replace("T", " "), "%Y-%m-%d %H:%M:%S") + timedelta(hours=3)
        except ValueError:
            continue
        out.setdefault(t.strftime("%Y-%m-%d"), []).append(iid)
    return out


def _valid_ym(s: str) -> str | None:
    m = re.fullmatch(r"(\d{4})-(\d{2})", s or "")
    return s if m and 1 <= int(m.group(2)) <= 12 and 2000 <= int(m.group(1)) <= 2100 else None


async def show_calendar(event, ym: str | None = None, edit: bool = False) -> None:
    now = datetime.utcnow() + timedelta(hours=3)
    cur_ym = now.strftime("%Y-%m")
    ym = _valid_ym(ym or "") or cur_ym
    ym = min(ym, cur_ym)
    y, m = int(ym[:4]), int(ym[5:7])
    by = await asyncio.to_thread(day_ids_msk, ym)
    total = sum(len(v) for v in by.values())
    if total:
        bk, bv = max(by.items(), key=lambda kv: (len(kv[1]), kv[0]))
        text = (f"📅 Календарь сохранений — {RU_MONTHS[m - 1]} {y}\n\nПостов: {total} за {len(by)} дн. "
                f"Больше всего — {int(bk[8:])} {RU_MONTHS_GEN[m - 1]} ({len(bv)}).\n\n"
                "Нажми на число — покажу посты за этот день. «·» — в этот день ничего не сохранял.")
    else:
        text = f"📅 Календарь сохранений — {RU_MONTHS[m - 1]} {y}\n\nВ этом месяце ничего не сохранял."
    prev_ym = f"{y - (m == 1)}-{(m - 2) % 12 + 1:02d}"
    next_ym = f"{y + (m == 12)}-{m % 12 + 1:02d}"
    rows = [[Button.inline("◀️", data=f"cal:{prev_ym}"),
             Button.inline(f"{RU_MONTHS[m - 1]} {y}", data="noop"),
             Button.inline("▶️" if ym < cur_ym else "·", data=f"cal:{next_ym}" if ym < cur_ym else "noop")],
            [Button.inline(w, data="noop") for w in ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")]]
    import calendar as _calendar
    first, ndays = _calendar.monthrange(y, m)
    cells = [Button.inline("⠀", data="noop") for _ in range(first)]
    for d in range(1, ndays + 1):
        key = f"{ym}-{d:02d}"
        cells.append(Button.inline(str(d), data=f"cald:{key}") if by.get(key) else Button.inline("·", data="noop"))
    while len(cells) % 7:
        cells.append(Button.inline("⠀", data="noop"))
    rows += [cells[k:k + 7] for k in range(0, len(cells), 7)]
    rows.append([Button.inline("◀️ Сервис", data="service")])
    if edit:
        try:
            await event.edit(text, buttons=rows)
            return
        except Exception:
            pass
    await event.respond(text, buttons=rows)


async def show_calendar_day(event, day: str) -> None:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day or ""):
        return
    ids = (await asyncio.to_thread(day_ids_msk, day[:7])).get(day, [])
    y, m, d = int(day[:4]), int(day[5:7]), int(day[8:])
    rows = []
    for iid in ids[:40]:
        it = db.get_item(iid)
        if it:
            rows.append([Button.inline(f"{theme_icon(it['category'], '📦')} {_fmt_short(it.get('summary') or '—', 44)}",
                                       data=f"lcitem:{iid}")])
    more = f"\n… и ещё {len(ids) - 40}." if len(ids) > 40 else ""
    rows.append([Button.inline("◀️ К календарю", data=f"cal:{day[:7]}")])
    text = f"📅 {d} {RU_MONTHS_GEN[m - 1]} {y} — постов: {len(ids)}{more}"
    try:
        await event.edit(text, buttons=rows)
    except Exception:
        await event.respond(text, buttons=rows)


# ---------------------------- 📤 Выгрузка папки в файл ----------------------------
_xp_jobs: dict[str, tuple[str, str]] = {}


async def export_ask(event, kind: str, val: str) -> None:
    n = len(await asyncio.to_thread(webarchive.export_rows, {"kind": kind, "val": val}))
    name = val.rpartition("/")[2] or val
    if not n:
        await event.answer("В этой папке нет постов", alert=True)
        return
    tok = secrets.token_hex(4)
    _xp_jobs[tok] = (kind, val)
    while len(_xp_jobs) > 200:
        _xp_jobs.pop(next(iter(_xp_jobs)))
    more = f"\n\nВ файл войдут первые {webarchive.EXPORT_MAX} — новые." if n > webarchive.EXPORT_MAX else ""
    await event.respond(
        f"📤 Выгрузить «{name}» ({n} {_posts_word(n)}) одним файлом:\n\n"
        "🌐 Страница с картинками — открывается в любом браузере даже без интернета, её можно отправить кому угодно.\n"
        f"📝 Markdown — текст для Obsidian, Notion и других заметок (без картинок).{more}",
        buttons=[[Button.inline("🌐 Страница с картинками", data=f"xpg:{tok}:h")],
                 [Button.inline("📝 Markdown для заметок", data=f"xpg:{tok}:m")]])


async def export_go(event, tok: str, fmt: str) -> None:
    job = _xp_jobs.get(tok)
    if not job:
        await event.answer("Кнопка устарела — нажми «📤 Выгрузить» ещё раз", alert=True)
        return
    kind, val = job
    name = val.rpartition("/")[2] or val
    try:
        await event.edit(f"📤 Собираю «{name}» в файл…" + (" Картинки — до пары минут." if fmt == "h" else ""), buttons=None)
    except Exception:
        pass
    uid = db.current_user_id() or event.sender_id
    try:
        raw, fname, n, total = await webarchive.build_export(client, uid, {"kind": kind, "val": val},
                                                             "md" if fmt == "m" else "html")
    except Exception:
        logger.warning("Выгрузка папки", exc_info=True)
        await event.respond("📤 Не получилось собрать файл — попробуй ещё раз чуть позже.")
        return
    cap = f"📤 «{name}»: {n} {_posts_word(n)}" + (f" из {total} (самые новые)" if total > n else "")
    await client.send_file(event.chat_id, _named_file(raw, fname), caption=cap, force_document=True)
    try:
        await event.edit(f"📤 Готово: «{name}» — файл в чате.", buttons=None)
    except Exception:
        pass


async def show_dups(event, edit: bool = False):
    chat_id = event.chat_id
    skip = _dup_skip.setdefault(chat_id, set())
    groups = [g for g in await asyncio.to_thread(_dup_groups) if "-".join(map(str, g[1])) not in skip]
    if not groups:
        text = ("🔁 Дублей не нашёл — всё сохранено по одному разу."
                if not skip else "🔁 Больше дублей нет (пропущенные покажу в следующий раз).")
        rows = [[Button.inline("◀️ Сервис", data="service")]]
        _dup_skip.pop(chat_id, None)
        await (event.edit(text, buttons=rows) if edit else event.respond(text, buttons=rows))
        return
    kind, ids = groups[0]
    items = [it for it in (db.get_item(i) for i in ids) if it]
    if len(items) < 2:
        skip.add("-".join(map(str, ids)))
        return await show_dups(event, edit)
    keep = next((it for it in items if it["locked"]), items[0])   # заблокированный или самый старый
    extra = [it for it in items if it is not keep and not it["locked"]]
    lines = [f"🔁 Дубли — осталось проверить: {len(groups)}", f"Почему: {DUP_KINDS[kind]}.", ""]
    for it in items:
        mark = "✅ оставлю" if it is keep else ("🔒 заблокирован" if it["locked"] else "🗑 лишний")
        d = (it.get("created_at") or "")[:10]
        date = f"{d[8:10]}.{d[5:7]}.{d[:4]}" if len(d) == 10 else ""
        lines.append(f"{mark} · #{it['id']} {theme_icon(it['category'], '📦')} {it['category']}"
                     f"{' · ' + date if date else ''}\n   «{_fmt_short(strip_markdown(it.get('summary') or ''), 70)}»")
    key = "-".join(str(it["id"]) for it in items)
    rows = []
    if extra:
        lines += ["", "Лишнее уйдёт в корзину — оттуда можно вернуть."]
        rows.append([Button.inline(f"🗑 Убрать лишнее ({len(extra)})", data=f"dupx:{key}")])
    rows.append([Button.inline(f"👀 #{it['id']}", data=f"view:{it['id']}") for it in items[:4]])
    rows.append([Button.inline("✅ Это не дубли", data=f"dupk:{key}"), Button.inline("⏭ Дальше", data=f"dupn:{key}")])
    rows.append([Button.inline("◀️ Сервис", data="service")])
    text = "\n".join(lines)
    await (event.edit(text, buttons=rows, parse_mode=None) if edit
           else event.respond(text, buttons=rows, parse_mode=None))


async def handle_dups(event, data_s: str) -> bool:
    if data_s == "dupv":
        _dup_skip.pop(event.chat_id, None)
        await show_dups(event, edit=True)
        return True
    if not data_s.startswith(("dupx:", "dupk:", "dupn:")):
        return False
    op, key = data_s.split(":", 1)
    try:
        ids = [int(x) for x in key.split("-")]
    except ValueError:
        return True
    if op == "dupx":
        items = [it for it in (db.get_item(i) for i in ids) if it]
        if len(items) < 2:
            await event.answer("Уже разобрано")
        else:
            keep = next((it for it in items if it["locked"]), items[0])
            gone = [it["id"] for it in items if it is not keep and not it["locked"] and db.trash_item(it["id"])]
            if gone:
                _set_undo(event.chat_id, {"kind": "untrash", "ids": gone})
            await event.answer(f"🗑 В корзину: {len(gone)}. Оставил #{keep['id']}")
    elif op == "dupk":
        ign = _dup_ignored()
        ign.add(key)
        db.set_setting("dup_ignore", json.dumps(sorted(ign)[-500:]))
        await event.answer("Хорошо, больше не покажу")
    else:
        _dup_skip.setdefault(event.chat_id, set()).add(key)
        await event.answer()
    await show_dups(event, edit=True)
    return True


async def cmd_dups(event):
    await show_dups(event)


async def _cmd_dups_old(event):
    groups = db.find_archive_dups()
    await _send_similar_report(event)
    if not groups:
        await event.respond("✅ Точных дублей (тот же файл) в архиве нет.", buttons=main_keyboard())
        return
    extra = sum(len(its) - 1 for _, its in groups)
    await event.respond(
        f"🔁 В архиве {len(groups)} групп дублей — {extra} лишних постов.\n"
        "Удаляй лишние кнопкой «🗑» (с подтверждением и отменой).",
        buttons=[
            [Button.inline(f"🗑 Удалить все лишние ({extra})", data="dups_clean")],
            [Button.inline("❌ Закрыть", data="dismiss")],
        ],
    )
    shown = 0
    for _tok, its in groups:
        shown += 1
        if shown > 5:
            await event.respond(f"…и ещё {len(groups) - 5} групп(ы) — отправь /dups снова.")
            break
        lines = [f"🔁 Дубли ({len(its)} постов, «{its[0]['content_type']}»):"]
        lines.append(f"🔗 общий токен: {(_tok or '')[:48]}")
        logger.info("DUPGROUP tok=%s n=%s ids=%s cts=%s",
                    (_tok or "")[:60], len(its), [x["id"] for x in its], [x["content_type"] for x in its])
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


async def _send_similar_report(event) -> None:
    pairs = _similar_pairs()
    missing = len(db.items_missing_hash(100000))
    tail = f"\n⏳ Ещё не проверено картинок: {missing} — бот досчитает фоном." if missing else ""
    if not pairs:
        await event.respond(f"🖼 Похожих картинок (пережатые копии одного мема/арта) не нашёл.{tail}")
        return
    n = len(pairs)
    word = "пара" if n % 10 == 1 and n % 100 != 11 else ("пары" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "пар")
    lines = [f"🖼 Похожие картинки — {n} {word} (тот же мем из разных каналов или один шаблон):"]
    rows = []
    for a, b, d in pairs[:8]:
        ia, ib = db.get_item(a), db.get_item(b)
        if not ia or not ib:
            continue
        lines.append(f"#{a} «{_fmt_short(ia['summary'] or '', 40)}» ↔ #{b} «{_fmt_short(ib['summary'] or '', 40)}»")
        rows.append([Button.inline(f"👀 #{a}", data=f"view:{a}"), Button.inline(f"👀 #{b}", data=f"view:{b}"),
                     Button.inline(f"🗑 #{b}", data=f"del:{b}")])
    if len(pairs) > 8:
        lines.append(f"…и ещё {len(pairs) - 8}.")
    rows.append([Button.inline("❌ Скрыть", data="dismiss")])
    await event.respond("\n".join(lines) + tail, buttons=rows)


async def cmd_help(event):
    sender_id = getattr(event, "sender_id", None)
    is_admin = sender_id is not None and int(sender_id) == int(_cached_owner() or 0)
    text = (
        "🤖 Как пользоваться:\n\n"
        "• Пересылай мне посты, сообщения, альбомы — сохранятся в папку по содержанию\n"
        "• 📂 Папки — всё сохранённое по темам: папки и подпапки\n"
        "• В карточке поста: 📂 переместить · ✏️ название · 🏷 теги · 💬 комментарий · 🔒 замок\n"
        "• 🔍 Поиск — по тексту и дате\n"
        "• 🗑 Корзина — недавно удалённое, можно вернуть\n"
        "• 🕘 Последние — свежие сохранения\n"
        "• 📊 Статистика — сколько всего сохранено\n"
        "• ⚙️ Настройки — автозамок, распознавание фото и голоса, быстрые действия\n"
        "• 🔁 Дубли — найти и убрать повторяющиеся вложения\n"
        "• 💾 Экспорт — выгрузить архив в файл\n\n"
        "Команды:\n"
        "• /categories — папки\n"
        "• /recent [N] — последние сохранения (по умолчанию 10)\n"
        "• /items <id | a-b | id1,id2> — показать посты по id / диапазону / перечню\n"
        "• /search <текст> — поиск по тексту, описаниям, тегам и тому, что ИИ увидел на картинке или услышал в видео\n"
        "• /archive <дни> — перенести посты старше N дней в «Архив»\n"
        "• /dups — найти и убрать повторяющиеся вложения\n"
        "• /stats — статистика\n"
        "• /ai [просьба] — 🤖 помощник: наведёт порядок, перенесёт, переименует, объединит, найдёт по смыслу\n"
        "• /web или кнопка «📚 Архив» у поля ввода — архив с картинками прямо в Telegram\n"
        "• В любом чате набери @имя_бота и слово — найденное из архива отправится в один тап\n"
        "• /menu — вернуть кнопки снизу, если пропали\n"
        "• /limits — лимиты бесплатного ИИ и фоновые очереди\n"
        "• /yt — очередь YouTube из расширения на телефоне\n"
        "• /checkbackup — проверить, что копия из GitHub восстанавливается целиком\n"
        "• /info — информация о последнем бэкапе\n"
        "• /settings — настройки\n"
        "• /export — скачать архив\n"
        "• /help — эта справка\n\n"
        "Папки создаёт и раскладывает искусственный интеллект, "
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
    await event.respond("📂 Папки:", buttons=root_categories_keyboard(tree))


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
    await _nav_send(event, "📂 Папки:", buttons=root_categories_keyboard(tree))


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
        ls.append(f"📂 Папки с постами ({len(existing_cats)})")
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
    twin = db.folder_twin(path)
    twin_items, twin_total = [], 0
    if twin:
        twin_total = db.get_total_items(twin)
        twin_items = db.get_items_by_category(twin, limit=_kb.TWIN_PREVIEW, offset=0)
        for it in twin_items:
            it["tags"] = db.get_item_tags(it["id"])
        _mark_new(twin_items)
    await _nav_send(event, f"{tags}{emoji} {lock}{path}  ({node['count']}):",
                    buttons=folder_page_keyboard(node, parent, twin, twin_items, twin_total))


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
        "🗂 Выбор папок (можно несколько):\n\nУдаление переносит все посты внутри в корзину.",
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
    ls.append(f"📂 Папки с постами ({len(existing_cats)}), 📁 Общие папки ({len(existing_folders)}), 📌 Посты ({len(items)})")
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
        head = f"🏷 Теги папки «{target}»"
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


def _latest_local_backup(uid) -> str:
    """Время самой свежей локальной .db-копии пользователя (data/backups).

    Имя файла: user_<uid>_YYYYMMDD_HHMMSS.db (время создания, локальное).
    mtime не используем: backup_db делает copy2 и сохраняет время исходной БД.
    """
    try:
        if not uid:
            return ""
        base = os.path.dirname(db.user_db_path(uid)) or "data"
        bk_dir = os.path.join(base, "backups")
        if not os.path.isdir(bk_dir):
            return ""
        prefix = f"user_{int(uid)}_"
        stamps = [f[len(prefix):-3] for f in os.listdir(bk_dir)
                  if f.startswith(prefix) and f.endswith(".db")]
        if not stamps:
            return ""
        latest = max(stamps)
        dt = datetime.strptime(latest, "%Y%m%d_%H%M%S")
        return dt.strftime("%d.%m.%Y %H:%M")
    except Exception:
        return ""


# ── Проверка бэкапа ──────────────────────────────────────────────────────────
# Раз в неделю (и по /checkbackup) бот скачивает свою копию из GitHub, восстанавливает
# её во ВРЕМЕННУЮ базу и сверяет с живой: посты, категории, папки, теги, корзина.
# Так поломка бэкапа всплывёт сразу, а не в день, когда он понадобится.
def compare_snapshots(live: dict, restored: dict) -> list[str]:
    """Расхождения живой базы и восстановленной копии (пусто — всё сходится)."""
    problems = []
    names = {"saved_items": "постов", "categories": "папок с постами", "folders": "папок",
             "tags": "тегов", "item_tags": "привязок тегов", "trash": "в корзине"}
    li, ri = live["items"], restored["items"]
    newest_backup = max(ri) if ri else 0
    missing = sorted(i for i in li if i not in ri and i <= newest_backup)
    fresh = [i for i in li if i not in ri and i > newest_backup]  # сохранены после копии — норм
    changed = sorted(i for i in li if i in ri and li[i] != ri[i])
    if missing:
        problems.append(f"в копии нет постов ({len(missing)}): #{', #'.join(map(str, missing[:10]))}")
    if changed:
        problems.append(f"в копии отличаются посты ({len(changed)}): #{', #'.join(map(str, changed[:10]))}")
    for key, label in names.items():
        lv, rv = live["counts"].get(key, 0), restored["counts"].get(key, 0)
        if key == "saved_items":
            lv -= len(fresh)
        if rv < lv:
            problems.append(f"{label}: в базе {lv}, в копии {rv}")
    return problems


async def backup_selfcheck(uid: int) -> tuple[bool | None, str]:
    """(True/False, текст). None — проверять нечем (нет GitHub)."""
    if not GITHUB_TOKEN:
        return None, "GitHub-копия не настроена"
    db.set_current_user(uid)
    await _backup_if_changed(uid, "перед проверкой бэкапа")
    path = _github_backup_path(uid)
    got, used = None, ""
    for p in (path, path[:-5] + ".json.gz") if path.endswith(".json") else (path,):
        got = await _fetch_github_raw(p)
        if got:
            used = p
            break
    if not got:
        return False, "не смог скачать копию из GitHub"
    tmp = os.path.join(tempfile.gettempdir(), f"bkcheck_{uid}_{secrets.token_hex(6)}.db")

    def _restore_snapshot():
        tok = db.use_db_path(tmp)
        try:
            db.init_db()
            db.import_json_stream(got)
            return db.archive_snapshot()
        finally:
            db.reset_db_path(tok)
            for suffix in ("", "-journal", "-wal", "-shm"):
                try:
                    os.remove(tmp + suffix)
                except OSError:
                    pass

    try:
        restored = await asyncio.to_thread(_restore_snapshot)
    except Exception as e:
        logger.warning("Проверка бэкапа: копия не восстанавливается", exc_info=True)
        return False, f"копия не восстанавливается: {e}"
    live = await asyncio.to_thread(db.archive_snapshot)
    problems = compare_snapshots(live, restored)
    n = restored["counts"].get("saved_items", 0)
    if problems:
        return False, "; ".join(problems)
    return True, f"копия из GitHub восстанавливается целиком: {n} {_plural_posts(n)}, " \
                 f"{restored['counts'].get('categories', 0)} папок, в корзине {restored['counts'].get('trash', 0)}"


async def run_backup_selfcheck(uid: int, notify_ok: bool = False, chat_id: int | None = None) -> None:
    ok, text = await backup_selfcheck(uid)
    db.set_current_user(uid)
    stamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    if ok is not None:
        db.set_setting("last_backup_check", f"{stamp} UTC · {'✅' if ok else '⚠️'} {text}")
    logger.info("Проверка бэкапа (user=%s): %s %s", uid, ok, text)
    if ok is False:
        msg = (f"⚠️ Проверка бэкапа не прошла: {text}.\n\n"
               "Посты в боте на месте, а вот на копию в GitHub сейчас рассчитывать нельзя. Сделай /export — "
               "файл придёт сюда и будет надёжной копией, а мне пришли скрин этого сообщения.")
    elif notify_ok:
        msg = f"✅ Бэкап в порядке — {text}." if ok else f"ℹ️ {text}."
    else:
        return
    try:
        await client.send_message(chat_id or uid, msg, buttons=main_keyboard())
    except Exception:
        pass


async def cmd_info(event):
    uid = db.current_user_id()
    total = db.count_all_items()
    n_cats = sum(1 for c in db.get_categories() if c.get("category"))
    last_time = db.get_setting("last_export_time", "")
    last_count = db.get_setting("last_export_count", "")
    last_reason = db.get_setting("last_backup_reason", "")
    gh_time = db.get_setting("last_github_backup_time", "")
    daily_on = db.get_setting("backup_tg_daily", "1") == "1"
    bpe = db.get_setting("backup_every_posts", "5")
    local = _latest_local_backup(uid)
    delta = _posts_since_export()

    lines = ["ℹ️ Информация об архиве и бэкапах", ""]
    lines.append(f"📊 Сейчас: постов {total}, папок {n_cats}")
    if last_time:
        extra = f" · {last_reason}" if last_reason else ""
        cnt = f" · постов: {last_count}" if last_count else ""
        lines.append(f"💾 Последний бэкап: {last_time} UTC{extra}{cnt}")
    else:
        lines.append("💾 Бэкапов ещё не было")
    lines.append(f"☁️ GitHub-копия: {gh_time} UTC" if gh_time else "☁️ GitHub-копия: —")
    if local:
        lines.append(f"🗃 Локальная копия БД: {local}")
    daily_txt = "вкл (ежедневно ~04:00 UTC)" if daily_on else "выкл"
    bpe_txt = f"каждые {bpe} постов" if bpe and bpe != "0" else "выкл"
    lines.append(f"⚙️ Авто-копии: {daily_txt}; {bpe_txt}")
    lines.append(f"📥 Новых постов с последнего бэкапа: {delta}")
    chk = db.get_setting("last_backup_check", "")
    lines.append(f"🧪 Проверка восстановления: {chk}" if chk else "🧪 Проверка восстановления: ещё не было (раз в неделю или /checkbackup)")
    await event.respond("\n".join(lines), buttons=main_keyboard())


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
    lines.append("📁 По папкам:")
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


_SETTINGS_NOTES = {
    "photo_vision": "ИИ смотрит на картинку или кадр видео, если подписи мало, и раскладывает по тому, что на ней",
    "audio_vision": "голосовые, кружки и речь в видео расшифровываются и учитываются при раскладке",
    "link_enrich": "уточняет название и описание по ссылке (YouTube и др.)",
    "quick_actions": "кнопки «Переместить / Название / Теги» сразу после сохранения",
    "dedup_check": "предупреждает, если такой файл или похожая картинка уже есть в архиве",
    "link_check": "каждую ночь бот проверяет часть сохранённых ссылок (весь архив — примерно за неделю); если страница удалена, пост помечается «🪦 ссылка умерла» и пишет тебе — а сохранённый текст статьи остаётся",
    "article_text": "для постов со ссылкой бот один раз забирает текст статьи: поиск находит её по содержимому, а текст остаётся, даже если сайт её удалит (ИИ не тратится)",
    "on_this_day": "по воскресеньям в 11:00 МСК бот присылает 2–3 поста, сохранённых месяц, три месяца, полгода или год назад",
    "weekly_cleanup": "по субботам в 12:00 МСК помощник смотрит архив и присылает план уборки (дубли, лишние папки); без «Применить» ничего не меняет",
    "night_recat": "с 01:00 до 08:00 МСК дораспознаёт посты, где ИИ раньше гадал вслепую, на остатке бесплатного лимита",
    "weekly_digest": "сводка новых постов по папкам раз в неделю",
    "game_deals": "раз в день в 13:00 МСК бот проверяет цены игр из ссылок на Steam в архиве и пишет, когда на игру скидка или она стала бесплатной (только о новых скидках; ИИ не тратится; команда /deals — проверить сейчас)",
    "anime_watch": "раз в день в 19:00 МСК бот проверяет аниме из ссылок на Shikimori и пишет, когда вышла новая серия, начался показ или тайтл вышел целиком (ИИ не тратится; /anime — список)",
    "github_watch": "раз в день в 14:00 МСК бот проверяет программы из ссылок на GitHub и пишет о новых версиях; «🧠 Что нового» — пересказ по-русски (/releases — список)",
    "daily_read": "каждый вечер в 20:00 МСК — одна сохранённая статья или видео, которые ещё не попадались (/read — прямо сейчас)",
    "monthly_digest": "1-го числа в 11:00 МСК: сколько сохранил за месяц, любимые папки и темы, сколько ещё не смотрел (ИИ не тратится)",
    "cleanup_monthly": "15-го числа в 12:00 МСК бот присылает посты, которые, возможно, уже не нужны: мёртвые ссылки, копии, давно не открытые. Разбираешь по одному, без тебя ничего не удаляется (ИИ не тратится; /cleanup — прямо сейчас)",
    "auto_lock": "после ручных правок пост защищается, чтобы авто-порядок и перераспознавание его не трогали",
    "cleanup_empty": "авто-порядок удаляет папки без единого поста",
    "auto_order_posts": "после N новых постов ИИ объединяет папки-дубли и раскладывает их по общим папкам",
    "subfolders_enabled": "если в папке копится несколько похожих постов, бот предложит подпапку",
    "subfolders_min": "сколько похожих постов нужно для подпапки",
    "subfolders_in_auto": "проверять подпапки при каждом авто-порядке, а не только по кнопке",
    "lock_move": "автозамок после ручного перемещения",
    "lock_rename": "автозамок после ручного переименования",
    "lock_create": "автозамок для папок, созданных вручную",
    "backup_tg_daily": "раз в день тихая копия архива в GitHub и резервный канал (если были изменения)",
    "media_copy": "каждое фото и видео архива бот тихо копирует в закрытый канал-хранилище: если чат с ботом случайно очистить, файлы не пропадут, а архив будет брать превью оттуда",
    "weekly_file": "по воскресеньям в 10:00 МСК бот присылает файл с полной копией архива; если всё пропадёт — перешли его боту, и он всё вернёт",
    "backup_every_posts": "дополнительная копия после каждых N новых постов (основная и так идёт при любых изменениях раз в 10 минут)",
    "auto_recover_posts": "если при старте архив пуст, восстановить его из копии",
    "auto_heal_broken": "при старте переносит в корзину посты, у которых пропало исходное сообщение",
}


def _settings_text(st: dict, page: str = "main") -> str:
    if page == "more":
        lines = ["**⚙️ Дополнительные настройки**", "",
                 "Редкие параметры: авто-порядок, подпапки, замки и страховочные копии. "
                 "Основная копия архива делается сама при любых изменениях — здесь только дополнительные.", ""]
        keys = [k for k, _ in SETTINGS_MORE] + ["auto_order_posts", "subfolders_min", "backup_every_posts"]
    else:
        lines = ["**⚙️ Настройки**", "", "Нажимай на кнопки — они переключают параметры.", ""]
        keys = [k for k, _ in SETTINGS_MAIN]
    for k in keys:
        lines.append(f"▸ {_setting_label(k)} — {_SETTINGS_NOTES[k]}")
    return "\n".join(lines)


def _setting_label(key: str) -> str:
    for k, label in SETTINGS_MAIN + SETTINGS_MORE:
        if k == key:
            return label
    return {"auto_order_posts": "⚡️ Авто-порядок", "subfolders_min": "🧩 Мин. в подпапке",
            "backup_every_posts": "🗄 Копия каждые N"}.get(key, key)


async def show_settings(event, page: str = "main"):
    st = settings_state()
    await event.edit(_settings_text(st, page), buttons=settings_keyboard(st, page))


async def cmd_settings(event):
    await event.respond(_settings_text(settings_state()), buttons=settings_keyboard(settings_state()))


async def _do_search(event, query: str):
    if not query.strip():
        await event.respond("Что ищем? Отправь текст для поиска.")
        return
    items = db.search_items(query.strip())
    smart_query[event.chat_id] = query.strip()
    smart_btn = [Button.inline("🤖 Поискать по смыслу (синонимы, описание своими словами)", data="ssearch")]
    if not items:
        await event.respond("Ничего не найдено по словам. Ищу по тексту, описаниям, тегам и тому, что ИИ увидел "
                            "на картинках. Можно поискать по смыслу — ИИ поймёт «мем где кот в коробке» "
                            "или «что я сохранял про PS5 в октябре».", buttons=[smart_btn])
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
    rows.append(smart_btn)
    try:
        await event.respond("\n".join(lines), buttons=rows or [[Button.inline("◀️ Назад", data="back_to_cats")]])
    except Exception:
        await event.respond("🔍 Результаты:", buttons=rows or [[Button.inline("◀️ Назад", data="back_to_cats")]])


_TYPE_GROUPS = {"photo": ("photo",), "video": ("video", "animation", "video_note"), "text": ("text",),
                "audio": ("audio", "voice"), "document": ("document",)}


async def smart_search(query: str, _terms: dict | None = None) -> tuple[list[dict], str]:
    """Поиск по смыслу: ИИ разворачивает запрос в слова (с синонимами и английским),
    бот отбирает до 60 кандидатов, ИИ выбирает подходящие. Возвращает (посты, пометка)."""
    terms = _terms or await _cat.search_terms(query)
    note = ""
    if terms is None:
        terms = {"words": [w for w in re.split(r"\W+", query.casefold().replace("ё", "е")) if len(w) > 2],
                 "date_from": "", "date_to": "", "type": ""}
        note = "ИИ не ответил — искал по словам запроса."
    rows = db.items_for_smart_search()
    df, dt = terms.get("date_from") or "", terms.get("date_to") or ""
    if df or dt:
        rows = [r for r in rows if (not df or r["created_at"][:10] >= df) and (not dt or r["created_at"][:10] <= dt)]
    kinds = _TYPE_GROUPS.get(terms.get("type") or "")
    if kinds:
        rows = [r for r in rows if r["content_type"] in kinds]
    scored = []
    for r in rows:
        art = r.get("article") or ("", "")
        hay = (f"{r['summary']} {r['original_text']} {r['category']} {r['source_channel']} {r['tags']} "
               f"{' '.join(r['topics'])} {art[0]} {art[1]} {r.get('comment') or ''}")
        hay = hay.casefold().replace("ё", "е")
        sc = sum(1 for w in terms["words"] if w in hay)
        if sc:
            scored.append((sc, r))
    scored.sort(key=lambda x: (-x[0], -x[1]["id"]))
    cands = [r for _, r in scored[:60]]
    seen = {r["id"] for r in cands}
    if (df or dt or kinds) and len(cands) < 60:  # при периоде/типе — и то, что слова не поймали
        cands += [r for r in rows if r["id"] not in seen][: 60 - len(cands)]
    if not cands:
        logger.info("Поиск по смыслу %r: слова=%s — кандидатов нет", query[:60], terms["words"][:8])
        if kinds:
            return await _smart_retry_any_type(query, terms)
        return [], note
    for r in cands:
        r["aux"] = postctx.aux_part(r["original_text"]) if has_aux(r["original_text"]) else ""
        if (r.get("article") or ("", ""))[0]:
            r["aux"] = f"Статья: {r['article'][0]}. {r['aux']}"
        r["date"] = r["created_at"]
    ids = await _cat.rank_posts(query, cands)
    logger.info("Поиск по смыслу %r: слова=%s период=%s..%s тип=%s, кандидатов %s, выбрано %s",
                query[:60], terms["words"][:8], df, dt, terms.get("type"), len(cands), ids)
    if ids is None:
        return [r for _, r in scored[:15]], "ИИ не ответил — показываю лучшие совпадения по словам."
    by_id = {r["id"]: r for r in cands}
    if not ids and kinds:
        return await _smart_retry_any_type(query, terms)
    return [by_id[i] for i in ids], note


async def _smart_retry_any_type(query: str, terms: dict) -> tuple[list[dict], str]:
    """«Видео с крысами», а крыса в архиве на фото — ищем ещё раз без фильтра по типу."""
    items, note = await smart_search(query, dict(terms, type=""))
    if items:
        note = (note + " " if note else "") + "Такого типа не нашёл — показываю всё подходящее."
    return items, note


def _found_rows(items: list[dict]) -> list:
    rows = []
    for it in items[:30]:
        tags = _tags_line(db.get_item_tags(it["id"]))
        emoji = theme_icon(it["category"], "📦")
        lock = " 🔒" if it.get("locked") else ""
        rows.append([Button.inline(f"{tags}{emoji}{lock} ID {it['id']} {_fmt_short(it.get('summary') or '—', 40)}",
                                   data=f"lcitem:{it['id']}")])
    return rows


async def _do_smart_search(event, query: str):
    progress = await event.respond(f"🤖 Ищу по смыслу: «{_fmt_short(query, 60)}»…", parse_mode=None)
    try:
        items, note = await smart_search(query)
    except Exception:
        logger.exception("Поиск по смыслу упал")
        items, note = [], "ошибка поиска"
    head = (f"🤖 По смыслу нашёл {len(items)}:" if items else "🤖 По смыслу ничего не нашёл.")
    if note:
        head += f"\n({note})"
    rows = _found_rows(items) + [[Button.inline("◀️ Назад", data="back_to_cats")]]
    try:
        await _MsgProxy(event.chat_id, progress.id).edit(head, buttons=rows, parse_mode=None)
    except Exception:
        await event.respond(head, buttons=rows, parse_mode=None)


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
    n_items = len(dump.get("items", []))
    n_cats = len(dump.get("categories") or [])
    raw = _gzip_export(dump)
    del dump
    try:
        await client.send_file(event.chat_id, file=_named_file(raw, "tg_saver_export.json.gz"), caption="💾 Экспорт архива")
        uid = db.current_user_id() or getattr(event, "sender_id", None)
        try:
            await _send_backup_to_channel(raw, "ручной экспорт", uid, n_items, n_cats)
        except Exception as e:
            logger.warning("Копия в канал-хранилище при экспорте не отправлена: %s", e)
        github_ok = False
        _backup_blocked.discard(uid)  # ручной экспорт — осознанное решение пользователя
        try:
            github_ok = await _push_github_file(_github_backup_path(uid), raw)
        except Exception as e:
            logger.warning("GitHub push при экспорте не удался: %s", e)
        db.set_setting("last_export_count", str(db.count_all_items()))
        db.set_setting("last_export_time", datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
        db.set_setting("last_backup_reason", "ручной экспорт")
        if github_ok:
            db.set_setting("last_github_backup_time", datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
        last_time = db.get_setting("last_export_time", "")
        last_count = db.get_setting("last_export_count", "")
        info = "💾 Файл отправлен. "
        if github_ok:
            info += "GitHub-копия обновлена. "
        if last_time:
            info += f"Дата и время (UTC): {last_time.replace('T', ' ')}. "
        if last_count:
            info += f"Элементов: {last_count}. "
        info += f"Состав экспорта: {db.count_all_items()} постов, {n_cats} папок."
        await event.respond(info, buttons=main_keyboard())
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
            arg = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
            if re.fullmatch(r"i\d+", arg) and db.get_item(int(arg[1:])):
                await show_item_view(event, int(arg[1:]))
            else:
                await cmd_start(event)
        elif cmd == "/help":
            await cmd_help(event)
        elif cmd == "/categories":
            await cmd_categories(event)
        elif cmd == "/stats":
            await cmd_stats(event)
        elif cmd == "/info":
            await cmd_info(event)
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
        elif cmd == "/deals":
            await event.respond("🎮 Проверяю цены в Steam… (до минуты)")
            await check_game_deals(db.current_user_id() or event.sender_id, show_all=True, chat_id=event.chat_id)
        elif cmd in ("/fav", "/favorites"):
            await show_favorites(event)
        elif cmd == "/topics":
            await show_topics(event)
        elif cmd in ("/cleanup", "/clean"):
            await show_cleanup(event)
        elif cmd in ("/calendar", "/cal"):
            await show_calendar(event)
        elif cmd == "/anime":
            await event.respond("📺 Смотрю на Shikimori…")
            await check_watch(db.current_user_id() or event.sender_id, "anime", show_all=True, chat_id=event.chat_id)
        elif cmd in ("/releases", "/updates"):
            await event.respond("⬆️ Смотрю на GitHub…")
            await check_watch(db.current_user_id() or event.sender_id, "github", show_all=True, chat_id=event.chat_id)
        elif cmd == "/read":
            await send_daily_read(db.current_user_id() or event.sender_id, chat_id=event.chat_id)
        elif cmd == "/limits":
            await cmd_limits(event)
        elif cmd == "/yt":
            await cmd_yt(event)
        elif cmd == "/menu":
            await event.respond("⌨️ Кнопки снизу снова на месте.", buttons=main_keyboard())
        elif cmd == "/web":
            await cmd_web(event)
        elif cmd in ("/ai", "/assistant"):
            rest = text.split(maxsplit=1)[1].strip() if len(text.split(maxsplit=1)) > 1 else ""
            await assistant_open(event, request=rest or None)
        elif cmd == "/checkbackup":
            await event.respond("🧪 Проверяю бэкап: скачиваю копию из GitHub и восстанавливаю её отдельно от архива…")
            await run_backup_selfcheck(db.current_user_id() or event.sender_id, notify_ok=True, chat_id=event.chat_id)
        return

    if text in (REPLY_BUTTONS["categories"], "📂 Категории"):  # старая надпись на ещё не обновлённой клавиатуре
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
            "🔍 Что ищем? Напиши текст поиска. Или нажми «🕐 По дате», чтобы искать по дате, "
            "или «🤖 По смыслу» — тогда можно описать своими словами: «мем где кот в коробке», "
            "«что я сохранял про PS5 в октябре».",
            buttons=[
                [Button.inline("🤖 По смыслу", data="search_smart"), Button.inline("🕐 По дате", data="search_date")],
                [Button.inline("❌ Отмена", data="search_cancel")],
            ],
        )
        return

    if text == REPLY_BUTTONS["recent"]:
        await _do_recent(event, 10)
        return

    if text == REPLY_BUTTONS["assistant"]:
        await assistant_open(event)
        return

    if text.startswith("🔁 Дубли:"):
        await cmd_dups(event)
        return

    if pending_search.get(msg.chat_id) and text:
        mode = pending_search.pop(msg.chat_id, None)
        if mode == "date":
            await _do_date_search(event, text)
        elif mode == "smart":
            await _do_smart_search(event, text)
        else:
            await _do_search(event, text)
        return

    if text.startswith("📬 Висящих") or text == "📭 Вопросов нет":
        await cmd_pending(event)
        return

    if pending.top(msg.chat_id):
        entry = pending.top(msg.chat_id)
        logger.info("PENDING top kind=%s sid=%s", (entry["act"] or {}).get("kind"), entry.get("sid"))
        if (entry["act"] or {}).get("kind") == "dup":
            if not _msg_media(msg) and not getattr(msg, "fwd_from", None):
                try:
                    await event.respond(
                        "⚠️ Есть неотвеченный вопрос о дубле выше — нажми «✅ Да» или «❌ Нет». "
                        "Этот пост сохраню отдельно.",
                        buttons=[[Button.inline("📬 Посмотреть вопросы", data="pend_list")]],
                    )
                    logger.info("PENDING dup reminder sent")
                except Exception:
                    logger.warning("PENDING dup reminder failed", exc_info=True)
            logger.info("PENDING dup — continue saving")
        else:
            pending_kind = (entry["act"] or {}).get("kind")
            is_new_post = bool(_msg_media(msg)) or bool(getattr(msg, "fwd_from", None))
            if pending_kind in ("clarify", "selclarify") and is_new_post:
                popped = pending.pop(msg.chat_id)
                logger.info("PENDING %s auto-cancelled (new post arrives)", pending_kind)
                await _delete_pending_prompt(event.client, msg.chat_id, popped)
            else:
                if (pending_kind == "comment" and not text and not getattr(msg, "fwd_from", None)
                        and (getattr(msg, "voice", None) or getattr(msg, "video_note", None))):
                    text = await _voice_to_text(msg)
                    if not text:
                        await event.respond("🎙 Не расслышал — скажи ещё раз или напиши заметку текстом.")
                        return
                entry = pending.pop(msg.chat_id)
                logger.info("PENDING other kind=%s consume text=%r", pending_kind, text[:40])
                await _handle_pending_action(event, entry["act"], text)
                return

    if (_assistant_active(msg.chat_id) and not getattr(msg, "fwd_from", None)
            and (getattr(msg, "voice", None) or getattr(msg, "video_note", None))):
        # голосом помощнику: «найди видео с крысами», «сложи мемы в одну папку»
        await assistant_voice(event, msg)
        return

    if text and _assistant_active(msg.chat_id) and not _msg_media(msg) and not getattr(msg, "fwd_from", None):
        await assistant_ask(event, text)
        return

    if msg.chat and not getattr(msg.chat, "private", True) and not _saved_in_group(msg.chat, getattr(sender, "id", None)):
        return

    if getattr(msg, "photo", None) is not None and not getattr(msg, "fwd_from", None) \
            and not getattr(msg, "grouped_id", None) and IMG_SEARCH_RE.match((text or "").strip()):
        # «найди» / «было?» с картинкой — не сохранять, а искать её в архиве
        await image_search_reply(event, msg)
        return

    if getattr(msg, "document", None) is not None:
        fname = (getattr(msg.file, "name", "") or "").lower()
        mime = (getattr(msg.file, "mime_type", "") or "").lower()
        sz = int(getattr(msg.file, "size", 0) or 0)
        if mime.startswith(("audio/", "video/")):
            is_candidate = False
        else:
            is_candidate = fname.endswith(".json") or fname.endswith(".json.gz") or "json" in mime or (0 < sz < 5_000_000) \
                or ((fname in ("", "unnamed") or "gzip" in mime or mime == "application/octet-stream") and 0 < sz < 80_000_000)
        if is_candidate:
            try:
                raw = await client.download_media(msg, file=bytes)
                dump = json.loads(_maybe_gunzip(raw).decode("utf-8"))
                if isinstance(dump, dict) and dump.get("items") is not None:
                    res = db.import_json(dump)
                    _backup_blocked.discard(db.current_user_id())
                    await event.respond(
                        f"✅ Импорт завершён: {res['items']} постов, {res['categories']} папок, {res['tags']} тегов.",
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
    sender = None
    try:
        sender = await event.get_sender()
        logger.info("🖼 Альбом: chat=%s user=%s фото/медиа=%s", messages[0].chat_id, getattr(sender, "id", None), len(messages))
    except Exception:
        pass
    media = []
    has_text = False
    for m in messages:
        media.extend(_msg_media(m))
        has_text = has_text or bool((m.text or "").strip())

    if not media and not has_text:
        return
    content_type = _primary_content_type(media) if media else "text"
    first = messages[0]
    if first.chat and not getattr(first.chat, "private", True):
        if not _saved_in_group(first.chat, getattr(sender, "id", None)):
            return
    _activate_user(getattr(sender, "id", None))
    processing = await first.reply("⏳ Анализирую...")
    dedup_note = None
    if db.get_setting("dedup_check", "1") == "1" and _media_uniques(media):
        dupes = db.find_dupes(_media_uniques(media))
        if dupes:
            extra = " ".join(f"#{d['id']} ({d['category']})" for d in dupes[:3])
            dedup_note = f"⚠️ Вложение уже есть в сохранённых: {extra}"
    analysis = await _analyze(client, messages, content_type)
    await _save(
        client,
        chat_id=first.chat_id,
        processing=processing,
        content_type=content_type,
        analysis=analysis,
        file_ids=media,
        message_id=first.id,
        media_group_id=str(first.grouped_id),
        source_channel=analysis.get("source") or await _get_source_name(first),
        dedup_note=dedup_note,
    )


async def _resend(item):
    refs = []
    for m in item["file_ids"] or []:
        try:
            refs.append(utils.resolve_bot_file_id(m["f"]))
        except Exception:
            continue
    # распознанное ботом (описание картинки, превью ссылки) в подпись не идёт
    caption = strip_aux(item["original_text"] or "") or None
    return refs, caption


def _origin_media_tokens(item) -> list[str]:
    """Токены вложений поста из БД (file_ids['u']) для сверки с живым сообщением.
    Только 'u' (тип:id) — 'f' (упакованный file_id) зависит от бота-сохранителя и ненадёжен."""
    # get_item отдаёт уже разобранный список; раньше тут был .strip() у списка →
    # AttributeError, и сверка с оригиналом молча проваливалась для всех медиа-постов.
    arr = db._parse_file_ids(item.get("file_ids"))
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
    a = _norm_text(strip_aux(item.get("original_text") or ""))
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
    if action == "assistant":
        await assistant_open(event, ids=[it["id"] for it in items], request=hint)
        return True
    if action == "hint":
        cat = ((cmd or {}).get("category") or "").strip().strip("\"'«» \t")
        if not cat:
            return False
        cat = _canonical_existing_cat(cat)
        if not db.get_category_row(cat):
            ok, err = db.create_category(cat)
            if not ok:
                await event.respond(f"❌ Не удалось создать папку «{cat}»: {err}")
                return True
            created = True
        else:
            created = False
        moved = 0
        for it in items:
            if it["category"] != cat:
                db.update_item_category(it["id"], cat)
                moved += 1
        note = f" и создал папку «{cat}»" if created else ""
        await event.respond(f"📂 Перемещено постов: {moved} → «{cat}»{note}")
        await show_item_view(event, items[0]["id"])
        return True
    if action in (None, ""):
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
        cat = _canonical_existing_cat(cat)
        if not cat:
            await event.respond("Не понял, куда переместить. Напиши имя папки, например: «перемести в Комиксы».")
            return True
        created = False
        if not db.get_category_row(cat):
            if action == "create_move":
                folder = (cmd.get("folder") or "").strip("/").strip()
                ok, err = db.create_category(cat, path=folder or "")
                if not ok:
                    await event.respond(f"❌ Не удалось создать папку: {err}")
                    return True
                created = True
            elif action == "move":
                if cat not in _existing_category_names():
                    await event.respond(f"❌ Папки «{cat}» нет. Можно: «создай папку {cat} и отправь сюда».")
                    return True
        moved = 0
        for it in items:
            if it["category"] != cat:
                db.update_item_category(it["id"], cat)
                moved += 1
        where = f" создал папку «{cat}»" if created else ""
        note = " (уже была)" if not created and db.get_category_row(cat) else ""
        await event.respond(
            f"{'📁' if created else '📂'} Перемещено постов: {moved} → «{cat}»{where}{note}"
        )
        await show_item_view(event, items[0]["id"])
        return True

    return False


async def _delete_pending_prompt(client, chat_id: int, entry: dict | None) -> None:
    """Удаляет висящее окно с вопросом (msg_id промпта), которое становится неактуальным
    после авто-отмены pending-запроса (пришёл новый пост / нажата посторонняя кнопка)."""
    if not entry:
        return
    mid = (entry.get("act") or {}).get("msg_id")
    if not mid:
        return
    try:
        await client.delete_messages(chat_id, [mid])
        logger.info("Удалено висящее окно вопроса msg_id=%s chat=%s", mid, chat_id)
    except Exception:
        logger.warning("Не удалось удалить висящее окно вопроса msg_id=%s", mid)


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
            result = await _ai_categorize(
                base, item["content_type"], item["source_channel"], exclude_id=item_id, hint=hint
            )
            if result is None:
                await event.respond("⏳ ИИ сейчас перегружен — повтори уточнение через минуту.")
                return
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
            val = (text or "").strip()
            if val == "-":
                db.set_item_comment(item_id, "")
                await event.respond("✅ Заметка удалена.")
            else:
                if not val:
                    await event.respond("❌ Пустая заметка. Напиши текст, скажи голосом или отправь «-», чтобы удалить.")
                    return
                db.set_item_comment(item_id, val[:2000])
                await event.respond(f"✅ Заметка сохранена: {strip_markdown(val[:2000])}")
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
            done, skipped, errors = [], 0, 0
            progress = await _prompt(event, f"⏳ Перераспознаю {len(items)} с учётом уточнения...")
            for i, item in enumerate(items):
                try:
                    base = await _recat_text(item)
                    if not base:
                        skipped += 1
                        continue
                    result = await _ai_categorize(
                        base, item["content_type"], item["source_channel"], exclude_id=item["id"], hint=hint
                    )
                    if result is None:
                        skipped += 1
                        continue
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
                await event.respond("🔒 Папка заблокирована — переименование запрещено.")
                return
            group = (row["group"] or "").strip("/") if row else ""
            if group and db.folder_twin(group) == old:
                # это посты самой папки — переименовываем папку целиком
                if _twin_name_taken(old, text):
                    await event.respond(f"❌ Имя «{text.strip()}» уже занято другой папкой — выбери другое.")
                    return
                ok, res = db.rename_folder(group, text)
                if not ok:
                    await event.respond(f"❌ {res}")
                    return
                db.rename_twin(old, text.strip())
                await _finish_action(event, act, show_folder_page, res)
                return
            ok, res = db.rename_category(old, text)
            if not ok:
                await event.respond(f"❌ {res}")
                return
            if db.should_lock("rename"):
                db.set_category_locked(res, True)
            await _finish_action(event, act, show_category_page, res, 0)
            return

        if kind == "set_pin":
            pin = re.sub(r"\D", "", text or "")
            try:
                await event.delete()          # код не должен оставаться в переписке
            except Exception:
                pass
            if not 4 <= len(pin) <= 8 or pin != (text or "").strip():
                await event.respond("❌ Нужны только цифры, от 4 до 8. Нажми «🔐 Сделать личной» ещё раз.")
                return
            db.set_private_pin(pin)
            path = act.get("target") or ""
            if path:
                db.set_private_paths(db.private_paths() + [path])
            await event.respond("🔐 Код сохранён" + (f", папка «{path}» теперь личная" if path else "") +
                                ".\nВ мини-приложении она скрыта, пока не введёшь код (слева «🔐 Личное»). "
                                "В инлайн-поиске и ссылках для друзей её посты не показываются никогда.",
                                buttons=[[Button.inline("📂 К папке", data=f"fold|{path}")]] if path else None)
            return

        if kind == "rename_folder":
            path = act["target"]
            twin = db.folder_twin(path)
            if twin and _twin_name_taken(twin, text):
                # иначе папка переименовалась бы, а её посты остались под старым именем
                await event.respond(f"❌ Имя «{text.strip()}» уже занято другой папкой — выбери другое.")
                return
            ok, res = db.rename_folder(path, text)
            if not ok:
                await event.respond(f"❌ {res}")
                return
            priv = db.private_paths()
            if priv:
                db.set_private_paths([res + p[len(path):] if p == path or p.startswith(path + "/") else p for p in priv])
            if twin:  # посты самой папки переезжают под новое имя вместе с ней
                db.rename_twin(twin, res.rpartition("/")[2])
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
    if group.rpartition("/")[2] == category:
        # «Технологии» уже лежит в папке «Технологии» — подпапки кладём туда же,
        # а не во вложенную «Технологии/Технологии»
        return group
    return f"{group}/{category}".strip("/")


def flatten_double_folders() -> list[str]:
    """Убирает двойные вложения «X/X» (их делали подпапки): содержимое внутренней папки
    поднимается в наружную, посты не трогаются. Возвращает убранные пути."""
    done = []
    for path in sorted(db.get_folder_paths(), key=lambda p: -p.count("/")):
        parts = path.split("/")
        if len(parts) >= 2 and parts[-1] == parts[-2] and not db.folder_locked(path):
            ok, _ = db.dissolve_folder(path)
            if ok:
                done.append(path)
    return done


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
        return ["⚡ Авто-порядок: папок нет."], {}
    res = await organize([c["category"] for c in cats], assistant.structure_text(1))
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

    plan = {"merges": merges, "folders": folders_map, "subfolders": [], "skip_merges": set(), "skip_folders": set(),
            "skip_sub": set(), "dissolve": _lonely_folders(cats, moved_away, folders_map), "skip_dis": set()}

    if db.get_setting("subfolders_enabled", "1") == "1" and db.get_setting("subfolders_in_auto", "1") == "1":
        sub_lines, sub_plan = await _subfolders_preview()
        plan["subfolders"] = sub_plan

    lines = _plan_lines(plan)
    return lines, plan


def _lonely_folders(cats: list[dict], moved_away: set, folders_map: dict) -> list[dict]:
    """Папки, где после плана останется одна категория и нет подпапок (как «Реклама» → «Продажи»):
    такая папка только добавляет лишний клик."""
    groups = {c["category"]: (c["group"] or "").strip("/") for c in cats if c["category"] not in moved_away}
    for folder, names in folders_map.items():
        for n in names:
            groups[n] = folder
    out = []
    for path in db.get_folder_paths():
        if path in folders_map or db.folder_locked(path):
            continue
        direct = [c for c, g in groups.items() if g == path]
        nested = any(g.startswith(path + "/") for g in groups.values()) or \
            any(f.startswith(path + "/") for f in db.get_folder_paths())
        if len(direct) == 1 and not nested and not any(c["locked"] for c in cats if c["category"] == direct[0]):
            out.append({"path": path, "cat": direct[0]})
    return out[:8]


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
    if plan.get("dissolve"):
        lines.append("\nЛишние папки (в них одна подпапка):")
        for i, d in enumerate(plan["dissolve"]):
            d_mark = "💤 " if i in plan.get("skip_dis", set()) else ""
            lines.append(f"• {d_mark}убрать «{d['path']}», «{d['cat']}» поднимется на уровень выше")
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

    dissolved_lines = []
    for i, d in enumerate(plan.get("dissolve", [])):
        if i in plan.get("skip_dis", set()):
            continue
        ok_d, _msg = db.dissolve_folder(d["path"])
        if ok_d:
            dissolved_lines.append(f"• папка «{d['path']}» убрана, «{d['cat']}» на уровень выше")

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
    if dissolved_lines:
        lines.append("\nУбраны лишние папки:")
        lines.extend(dissolved_lines)

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
    for i, d in enumerate(plan.get("dissolve", [])):
        lbl = "↩️ Вернуть" if i in plan.get("skip_dis", set()) else "🚫 Оставить"
        rows.append([Button.inline(f"{lbl}: папку «{d['path'][:30]}»", data=f"ao_skip_d:{i}")])
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
        has_work = bool(plan.get("merges")) or bool(plan.get("folders")) or bool(plan.get("subfolders")) \
            or bool(plan.get("dissolve"))
        if not has_work:
            logger.info("Авто-порядок (auto): нечего делать — молчим (user=%s)", user_id)
            return
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


def _defer_callback_answer(event) -> None:
    """Telegram принимает ОДИН ответ на нажатие кнопки. Раньше бот сразу отвечал пустым,
    и все всплывашки обработчиков («🔁 Распознаю…», «✅ Готово», «План устарел»)
    молча терялись. Теперь первый ответ — от обработчика; если он молчит 1,5 с —
    отвечаем пустым сами (чтобы кнопка не «крутилась»). Опоздавшее важное
    сообщение (alert) приходит обычным текстом в чат."""
    orig = event.answer
    state = {"done": False}

    async def answer(message=None, cache_time=0, *, url=None, alert=False):
        if state["done"]:
            if alert and message:
                try:
                    await event.respond(message)
                except Exception:
                    pass
            return None
        state["done"] = True
        try:
            return await orig(message, cache_time, url=url, alert=alert)
        except Exception:
            return None

    async def auto():
        await asyncio.sleep(1.5)
        if not state["done"]:
            await answer()

    event.answer = answer
    asyncio.create_task(auto())


async def on_callback(event):
    data = event.data
    _defer_callback_answer(event)
    data_s = data.decode("utf-8", "ignore")
    if data_s.startswith("h:"):   # длинные данные кнопки (см. keyboards.LONG_DATA)
        full = _kb.LONG_DATA.get(data_s)
        if not full:
            await event.answer("Кнопка устарела после обновления бота — открой папку заново", alert=True)
            return
        data_s = full

    if data_s == "noop":
        return

    logger.info("🔘 Колбэк: %s chat=%s", data_s[:60], event.chat_id)

    if (
        data_s != "noop"
        and not data_s.startswith(("clrs|", "pend_replay|"))
        and data_s not in ("pend_clear", "cancel_action")
    ):
        top = pending.top(event.chat_id)
        if top and (top["act"] or {}).get("kind") in ("clarify", "selclarify"):
            popped = pending.pop(event.chat_id)
            logger.info("PENDING %s auto-cancelled (unrelated button press)", (top["act"] or {}).get("kind"))
            await _delete_pending_prompt(event.client, event.chat_id, popped)

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
        orig = None
        try:
            res = await client.get_messages(st["chat_id"], ids=[st["message_id"]])
            orig = res[0] if isinstance(res, list) and res else res
        except Exception:
            orig = None
        if orig is not None:
            analysis = await _analyze(client, [orig], st["content_type"])
        else:
            analysis = {"display": st["text"], "llm_text": strip_markdown(st["text"] or ""), "aux": ""}
        await _save(
            client,
            chat_id=st["chat_id"],
            processing=proxy,
            content_type=st["content_type"],
            analysis=analysis,
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

    if data_s in ("settings_more", "settings_main"):
        await show_settings(event, "more" if data_s == "settings_more" else "main")
        await event.answer()
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
        await show_settings(event, "more" if key in SETTINGS_MORE_KEYS else "main")
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
                await show_settings(event, "more")
                await event.answer("Готово!")
                return

    if data_s == "back_to_cats" or data_s == "to_folders":
        await show_root_page(event)
        return

    if data_s.startswith("fold|"):
        path = data_s[len("fold|"):]
        await show_folder_page(event, path)
        return

    if await handle_chrule_callback(event, data_s):
        return

    if data_s.startswith("newcatno:"):
        try:
            name = _newcat_for_item(int(data_s.split(":", 1)[1]))
        except ValueError:
            name = None
        if name:
            offers = _newcat_offers()
            offers.pop(name, None)
            db.set_setting("newcat_offers", json.dumps(offers, ensure_ascii=False))
            dec = _newcat_declined() + [name]
            db.set_setting("newcat_declined", json.dumps(dec[-60:], ensure_ascii=False))
        try:
            await event.edit(f"👌 Не буду предлагать «{name}». Посты остались, где лежат." if name else "👌 Ок.",
                             buttons=None, parse_mode=None)
        except Exception:
            pass
        return

    if data_s.startswith("newcat:"):
        try:
            await handle_newcat_callback(event, int(data_s.split(":", 1)[1]))
        except ValueError:
            pass
        return

    if data_s == "assistant" or data_s.startswith("as_"):
        await handle_assistant_callback(event, data_s)
        return

    if data_s.startswith(("remind:", "remset:", "remdel:")):
        parts = data_s.split(":")
        try:
            item_id = int(parts[1])
        except (IndexError, ValueError):
            return
        item = db.get_item(item_id)
        if not item:
            await event.answer("Пост не найден", alert=True)
            return
        if parts[0] == "remind":
            rows = [[Button.inline(f"⏰ {label}", data=f"remset:{item_id}:{k}")] for k, label in REMIND_CHOICES]
            rows.append([Button.inline("◀️ Назад", data=f"view:{item_id}")])
            await event.edit("⏰ Когда напомнить про этот пост? Пришлю его в 9:00 по Москве.", buttons=rows)
            return
        lst = [r for r in _reminders() if r.get("id") != item_id]
        if parts[0] == "remset":
            due = _remind_due(parts[2] if len(parts) > 2 else "tom")
            lst.append({"id": item_id, "due": due, "chat": event.chat_id})
            note = f"⏰ Напомню {_remind_label(due)} в 9:00."
        else:
            note = "⏰ Напоминание отменено."
        _reminders_set(lst)
        await event.answer(note)
        rem = _reminder_for(item_id)
        summary = strip_markdown(item.get("summary") or "")
        await event.edit(f"{note}\n\n{theme_icon(item['category'], '📦')} {item['category']}\n\n{summary}\n\n📅 ID: {item_id}",
                         buttons=view_keyboard(item_id, item["category"], item["locked"],
                                               _remind_label(rem["due"]) if rem else None,
                                               article=_has_article(item_id), fav=item_id in db.favorites(),
                            foreign=postctx.is_foreign(item.get("original_text") or "")))
        return

    if data_s == "cleanup":
        await show_cleanup(event)
        return

    if data_s.startswith("cln:"):
        await handle_cleanup_cb(event, data_s)
        return

    if data_s.startswith("cal:"):
        await show_calendar(event, data_s[4:], edit=True)
        return

    if data_s.startswith("cald:"):
        await show_calendar_day(event, data_s[5:])
        return

    if data_s.startswith(("xpf|", "xpc|")):
        await export_ask(event, "folder" if data_s.startswith("xpf|") else "cat", data_s.split("|", 1)[1])
        return

    if data_s.startswith("xpg:"):
        _p = data_s.split(":")
        if len(_p) == 3:
            await export_go(event, _p[1], _p[2])
        return

    if data_s.startswith("share|"):
        await share_folder(event, "folder", data_s.split("|", 1)[1])
        return

    if data_s.startswith("sharec|"):
        await share_folder(event, "cat", data_s.split("|", 1)[1])
        return

    if data_s.startswith("unshare:"):
        sid = data_s.split(":", 1)[1]
        shares = _shares()
        sh = shares.pop(sid, None)
        _shares_set(shares)
        try:
            await event.edit(f"🚫 Ссылка на «{_share_target_title(sh)}» отозвана — она больше не открывается."
                             if sh else "🚫 Эта ссылка уже отозвана.", buttons=None, parse_mode=None)
        except Exception:
            pass
        return

    if data_s == "shares":
        shares = _shares()
        rows = []
        lines = ["🔗 Ссылки на папки:" if shares else "🔗 Ссылок пока нет. Поделиться — кнопкой «🔗 Поделиться» в папке."]
        for sid, sh in list(shares.items())[:20]:
            title = _share_target_title(sh)
            lines.append(f"• {title}")
            link = webarchive.share_link(db.current_user_id() or event.sender_id, sid)
            row = [Button.url(f"🌐 {_fmt_short(title, 24)}", link)] if link else []
            rows.append(row + [Button.inline("🚫 Отозвать", data=f"unshare:{sid}")])
        rows.append([Button.inline("◀️ Назад", data="service")])
        await event.edit("\n".join(lines), buttons=rows, parse_mode=None)
        return

    if data_s == "reminders":
        lst = sorted(_reminders(), key=lambda r: r.get("due", 0))
        rows, lines = [], ["⏰ Напоминания:" if lst else "⏰ Напоминаний нет. Поставить — кнопкой «⏰ Напомнить» в карточке поста."]
        for r in lst[:20]:
            it = db.get_item(int(r["id"]))
            if not it:
                continue
            lines.append(f"• {_remind_label(r['due'])} — {_fmt_short(strip_markdown(it.get('summary') or ''), 50)}")
            rows.append([Button.inline(f"👁 {_fmt_short(it.get('summary') or '', 30)}", data=f"view:{it['id']}"),
                         Button.inline("✖️ Отменить", data=f"remdel:{it['id']}")])
        rows.append([Button.inline("◀️ Назад", data="service")])
        await event.edit("\n".join(lines), buttons=rows, parse_mode=None)
        return

    if data_s == "triage" or data_s.startswith(("tri_", "tri:")):
        await handle_triage(event, data_s)
        return

    if data_s == "unseen":
        ids = sorted(unseen_ids(), reverse=True)
        rows = [[Button.inline("📥 Разобрать по одному", data="triage")]] if ids else []
        for iid in ids[:30]:
            it = db.get_item(iid)
            if it:
                rows.append([Button.inline(f"🆕{theme_icon(it['category'], '📦')} {_fmt_short(it.get('summary') or '', 40)}",
                                           data=f"lcitem:{iid}")])
        rows.append([Button.inline("◀️ Назад", data="service")])
        head = (f"🆕 Ещё не смотрел: {len(ids)}. Пост считается просмотренным, когда открываешь его карточку "
                "(в боте или в архиве)." if ids else "🆕 Всё просмотрено.")
        await event.edit(head, buttons=rows, parse_mode=None)
        return

    if data_s == "webarchive":
        await cmd_web(event)
        return

    if data_s == "service":
        try:
            await event.edit(SERVICE_TEXT, buttons=_service_buttons())
        except Exception:
            await event.respond(SERVICE_TEXT, buttons=_service_buttons())
        return

    if await handle_dups(event, data_s):
        return

    if await handle_topic_rule_cb(event, data_s):
        return

    if data_s == "svc_order":
        await event.edit(ORDER_TEXT, buttons=_kb.order_keyboard(), parse_mode=None)
        return

    if data_s == "svc_assist":
        await assistant_open(event, request="наведи порядок: дубли, лишние папки, папки с одной подпапкой, "
                                            "разбросанные родственные папки")
        return

    if data_s == "btn_help":
        text = (
            "ℹ️ Что делают кнопки:\n\n"
            "• 📂 — каждая строка с папкой открывает её содержимое\n"
            "• ➕ Папка / ➕ Подпапка — создать папку\n"
            "• ☑️ Выбрать — режим выбора нескольких постов (массовое перемещение/теги/удаление)\n"
            "• 🗑 Корзина — удалённые посты, можно вернуть\n\n"
            "🛠 Сервис:\n"
            "• 📥 Разобрать новое — новые посты по одному: верно / другая папка / позже / в корзину\n"
            "• ⏰ Напоминания — что и когда бот пришлёт снова\n"
            "• 🗂 Порядок в папках — помощник, авто-порядок, подпапки, объединить, перераспознать\n"
            "• 🔁 Дубли — одно и то же, сохранённое дважды: по одному, лишнее — в корзину\n"
            "• ⭐ Избранное — кнопка в карточке поста; список — вверху «📂 Папки», /fav\n"
            "• 🎮 /deals — скидки на игры из ссылок на Steam (и сам пишет раз в день)\n"
            "• 🏷 Темы — все посты про одну игру/аниме/бренд из разных папок (вверху «📂 Папки», /topics)\n"
            "• 📺 /anime и ⬆️ /releases — новые серии аниме (Shikimori) и версии программ (GitHub) из ссылок\n"
            "• 📖 /read — что почитать: одна сохранённая статья или видео\n"
            "• 🔎 Поиск по картинке — пришли фото с подписью «найди»: покажу её и похожие из архива\n"
            "• 🧹 /cleanup — уборка: мёртвые ссылки, копии и давно забытые посты, разбор по одному\n"
            "• 📅 /calendar — календарь: сколько сохранял по дням, посты за любой день\n"
            "• 📚 Архив с картинками и 🔗 Мои ссылки — архив в браузере и ссылки на папки для друзей\n"
            "• 🏷 Теги, 📡 Правила каналов («канал X всегда в папку Y»), 🔒 Заблокированные\n"
            "• 📊 Лимиты ИИ — сколько осталось бесплатного ИИ на сегодня\n\n"
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
            await event.answer("Папок нет", alert=True)
            return
        try:
            await event.answer()
        except Exception:
            pass
        await event.edit("⏳ Анализирую папки... (может занять до 60 сек)")
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

    if data_s.startswith("ao_skip_d:"):
        plan = auto_order_plan.get(event.chat_id)
        if not plan:
            await event.answer("План уже неактуален", alert=True)
            return
        try:
            i = int(data_s.split(":", 1)[1])
        except ValueError:
            return
        plan["skip_dis"] = set(plan.get("skip_dis", set())) ^ {i}
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
        await event.respond("📂 Папки:", buttons=root_categories_keyboard(db.get_tree()))
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
        await event.respond("📂 Папки:", buttons=root_categories_keyboard(db.get_tree()))
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
            await event.answer("Нужно минимум 2 незаблокированные папки", alert=True)
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
            await event.answer("🔒 Папка заблокирована", alert=True)
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
            await event.answer(f"📂 Перемещено папок: {moved} → «{dst or 'корень'}»")
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
            event.chat_id, act, f"✏️ Переименовать папку «{category}»",
            replay={"kind": "text", "text": f"✏️ Новое название папки «{category}»:"},
        )
        msg = await _prompt(event, f"✏️ Новое название папки «{category}»:")
        pending.set_msg(event.chat_id, sid, msg.id)
        return

    if data_s.startswith(("pvset|", "pvdel|", "pvpin")):
        if data_s == "pvpin" or (data_s.startswith("pvset|") and not db.has_private_pin()):
            path = data_s.split("|", 1)[1] if "|" in data_s else ""
            act = {"kind": "set_pin", "target": path}
            sid = pending.push(event.chat_id, act, "🔐 Код для личных папок",
                               replay={"kind": "text", "text": "🔐 Придумай код из 4–8 цифр:"})
            msg = await _prompt(event, "🔐 Придумай код из 4–8 цифр. Он открывает личные папки в мини-приложении.\n"
                                       "Сообщение с кодом я сразу удалю из чата.")
            pending.set_msg(event.chat_id, sid, msg.id)
            return
        path = data_s.split("|", 1)[1]
        paths = db.private_paths()
        if data_s.startswith("pvset|"):
            db.set_private_paths(paths + [path])
            await event.answer(f"🔐 «{path}» теперь личная", alert=True)
        else:
            db.set_private_paths([p for p in paths if p != path])
            await event.answer(f"🔓 «{path}» больше не личная")
        await show_folder_page(event, path)
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

    if data_s.startswith("subcat|"):
        # у «папки с постами» нет своей папки — заводим её с тем же именем, посты остаются
        # в ней же, и дальше — как обычное «➕ Подпапка»
        category = data_s.split("|", 1)[1]
        row = db.get_category_row(category)
        if not row:
            await event.answer("Папка не найдена", alert=True)
            return
        group = (row["group"] or "").strip("/")
        new_path = f"{group}/{category}" if group else category
        if db.folder_twin(group) != category:   # уже не двойник своей папки
            db.set_category_path(category, new_path)
        else:
            new_path = group
        data_s = f"addcat|{new_path}"

    if data_s.startswith("addcat|"):
        path = data_s.split("|", 1)[1] if "|" in data_s else ""
        act = {"kind": "add_cat", "path": path}
        where = f" в «{path}»" if path else ""
        what = "подпапки" if path else "папки"
        sid = pending.push(
            event.chat_id, act, f"➕ Создать {'подпапку' if path else 'папку'}{where}",
            replay={"kind": "text", "text": f"➕ Название новой {what}{where}:"},
        )
        msg = await _prompt(event, f"➕ Название новой {what}{where}:")
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

    if data_s == "search_smart":
        pending_search[event.chat_id] = "smart"
        await event.edit("🤖 Опиши, что ищешь, своими словами. Например: «мем где кот в коробке», "
                         "«видео с крысами», «что я сохранял про PS5 в октябре».",
                         buttons=[[Button.inline("❌ Отмена", data="search_cancel")]])
        return

    if data_s == "ssearch":
        q = smart_query.get(event.chat_id)
        if not q:
            await event.answer("Запрос устарел — поищи ещё раз", alert=True)
            return
        await _do_smart_search(event, q)
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
        await show_recat_choice(event)
        return

    if data_s == "recatall_go":
        ids = db.get_item_ids()
        if ids:
            await run_bulk_recat(event, ids, "все элементы")
        else:
            await event.answer("Нет элементов", alert=True)
        return

    if data_s == "recat_smart":
        ids = _smart_recat_ids()
        if ids:
            await run_bulk_recat(event, ids, "где ИИ гадал", smart=True)
        else:
            await event.answer("Таких постов не осталось 👍", alert=True)
        return

    if data_s.startswith("ai_limits"):
        try:
            await event.edit(_limits_text(), buttons=_limits_buttons(from_service=data_s.endswith("|svc")))
        except Exception:
            pass
        await event.answer()
        return

    if data_s == "recat_stop":
        await _recat_stop(event)
        return

    if data_s == "purge_nodata":
        ids = nodata_batch.pop(event.chat_id, None)
        if not ids:
            await event.answer("Устарело", alert=True)
            return
        trashed = sum(1 for iid in ids if db.trash_item(iid))
        try:
            await event.edit(
                f"✅ Убрано {trashed} из {len(ids)} пустых постов в корзину.\n"
                f"🎣 Вернуть можно в разделе «🗑 Корзина»."
            )
        except Exception:
            pass
        await event.answer("Готово!")
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

    if data_s.startswith("fav:"):
        try:
            item_id = int(data_s.split(":", 1)[1])
        except ValueError:
            return
        item = db.get_item(item_id)
        if not item:
            await event.answer("Пост не найден", alert=True)
            return
        on = item_id not in db.favorites()
        db.set_favorite(item_id, on)
        await event.answer("⭐ Добавил в избранное" if on else "Убрал из избранного")
        rem = _reminder_for(item_id)
        try:
            await event.edit(buttons=view_keyboard(item_id, item["category"], item["locked"],
                                                   _remind_label(rem["due"]) if rem else None,
                                                   article=_has_article(item_id), fav=on))
        except Exception:
            pass
        return

    if data_s.startswith("topics:"):
        await show_topics(event, int(data_s.split(":", 1)[1] or 0))
        return

    if data_s.startswith("tpc|"):
        _, page, name = data_s.split("|", 2)
        await show_topic(event, name, int(page or 0))
        return

    if data_s.startswith("ghb|"):
        repo = data_s.split("|", 1)[1]
        st = _watch_state("github").get(repo) or {}
        if not st.get("body"):
            await event.answer("У этой версии нет описания", alert=True)
            return
        await event.answer("🧠 Читаю…")
        text = st.get("brief") or await _cat.article_brief(f"{repo} {st.get('tag', '')}: что нового", st["body"])
        if not text:
            await client.send_message(event.chat_id, "🧠 Бесплатный ИИ сейчас занят — попробуй чуть позже.")
            return
        state = _watch_state("github")
        if repo in state:
            state[repo]["brief"] = text
            _watch_save("github", state)
        msg = f"🧠 {repo} {st.get('tag', '')} — что нового:\n\n{text}"
        try:
            await client.send_message(event.chat_id, msg, link_preview=False)
        except Exception:
            await client.send_message(event.chat_id, strip_markdown(msg), parse_mode=None, link_preview=False)
        return

    if data_s in ("rdnext", "rdoff"):
        if data_s == "rdoff":
            db.set_setting("daily_read", "0")
            await event.answer("Больше не присылаю. Включить: ⚙️ Настройки → Дополнительно", alert=True)
            return
        await event.answer()
        await send_daily_read(db.current_user_id() or event.chat_id, chat_id=event.chat_id, edit_event=event)
        return

    if data_s.startswith("favlist:"):
        await show_favorites(event, int(data_s.split(":", 1)[1] or 0))
        return

    if data_s.startswith("artb:"):
        try:
            item_id = int(data_s.split(":", 1)[1])
        except ValueError:
            return
        brief = db.article_get(item_id)
        if not brief or brief["status"] != "ok":
            await event.answer("Текста статьи нет", alert=True)
            return
        await event.answer("🧠 Читаю статью…" if not brief["brief"] else "")
        text = await article_brief_for(item_id)
        if not text:
            await client.send_message(event.chat_id, "🧠 Бесплатный ИИ сейчас занят — попробуй чуть позже.")
            return
        msg = f"🧠 Кратко: {brief['title'] or 'статья'}\n\n{text}"
        try:
            await client.send_message(event.chat_id, msg, link_preview=False)
        except Exception:
            await client.send_message(event.chat_id, strip_markdown(msg), parse_mode=None, link_preview=False)
        return

    if data_s.startswith("trn:"):
        try:
            item_id = int(data_s.split(":", 1)[1])
        except ValueError:
            return
        await event.answer("🌐 Перевожу…")
        text = await translate_item(item_id)
        if not text:
            await client.send_message(event.chat_id, "🌐 Бесплатный ИИ сейчас занят — попробуй чуть позже.")
            return
        await client.send_message(event.chat_id, f"🌐 Перевод (#{item_id}):\n\n{text}", parse_mode=None, link_preview=False)
        return

    if data_s.startswith("simi:"):
        try:
            item_id = int(data_s.split(":", 1)[1])
        except ValueError:
            return
        await show_similar(event, item_id)
        return

    if data_s.startswith("art:"):
        try:
            item_id = int(data_s.split(":", 1)[1])
        except ValueError:
            return
        art = db.article_get(item_id)
        if not art or art["status"] != "ok":
            await event.answer("Текста статьи нет", alert=True)
            return
        await event.answer()
        head = f"📄 {art['title'] or 'Статья'}\n🔗 {art['url']}\n\n"
        body = art["text"]
        parts = [head + body[:3800]] + [body[i:i + 3900] for i in range(3800, len(body), 3900)]
        for part in parts[:3]:
            await client.send_message(event.chat_id, part, parse_mode=None, link_preview=False)
        return

    if data_s.startswith("aiview:"):
        it = db.get_item(int(data_s.split(":", 1)[1]))
        if not it:
            await event.answer("Не найдено", alert=True)
            return
        await event.answer()
        await client.send_message(event.chat_id, _ai_view_text(it), buttons=[[Button.inline("❌ Скрыть", data="dismiss")]])
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
            await event.answer("🔒 Папка заблокирована", alert=True)
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
        await _note_manual_move(event, ids, target)
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
            f"🔁 Перераспознать {n} постов с этим тегом?\nИИ заново разложит их по папкам.",
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
            await event.answer("Сначала выбери папки", alert=True)
        else:
            await show_tag_editor(event, "cf", root)
        return

    if data_s.startswith("cf_del|"):
        root = data_s.split("|", 1)[1]
        st = cf_sel.get(event.chat_id)
        if not st or not st["sel"]:
            await event.answer("Сначала выбери папки", alert=True)
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
                         buttons=[[Button.inline("📂 К папкам", data="back_to_cats")]])
        return

    if data_s.startswith("catdel:"):
        category = data_s.split(":", 1)[1]
        count = db.get_total_items(category)
        lock = db.get_category_row(category) and db.get_category_row(category).get("locked")
        if lock:
            await event.answer("🔒 Папка заблокирована — удаление запрещено", alert=True)
            return
        await event.edit(
            f"⚠️ Удалить папку «{category}»?\nПостов внутри: {count}. Они уйдут в корзину.",
            buttons=[
                [Button.inline("🗑 Да, удалить", data=f"catdelgo:{category}")],
                [Button.inline("❌ Отмена", data=f"cat:{category}")],
            ],
        )
        return

    if data_s.startswith("caticon:"):
        category = data_s.split(":", 1)[1]
        await event.edit("🎨 Иконка папки:", buttons=category_icon_keyboard(category))
        return

    if data_s.startswith("caticon_pick:"):
        rest = data_s.split(":", 1)[1]
        idx_s, category = rest.split("|", 1)
        if idx_s == "custom":
            sid = pending.push(
                event.chat_id, {"kind": "cat_icon_custom", "target": category}, "🎨 Иконка папки",
                replay={"kind": "text", "text": "Пришли эмодзи-иконку папки:"},
            )
            msg = await _prompt(event, "Пришли эмодзи-иконку папки:")
            pending.set_msg(event.chat_id, sid, msg.id)
            return
        db.set_category_icon(category, DEFAULT_TAG_CIRCLES[int(idx_s)])
        _refresh_icon_overrides()
        await show_category_page(event, category, 0)
        return

    if data_s.startswith("catdelgo:"):
        category = data_s.split(":", 1)[1]
        n = db.delete_category(category)
        await event.edit(f"🗑 Папка «{category}» удалена. Постов ушло в корзину: {n}.",
                         buttons=[[Button.inline("📂 К папкам", data="back_to_cats")]])
        return

    if data_s.startswith(("folddel:", "folddel|")):
        # кнопка на странице папки шлёт «folddel|…» — раньше её никто не ловил, и она молчала
        path = data_s.split(data_s[7], 1)[1]
        if db.folder_locked(path):
            await event.answer("🔒 Папка заблокирована — удаление запрещено", alert=True)
            return
        count = db.folder_post_count(path)
        parent = path.rpartition("/")[0]
        await event.edit(
            f"⚠️ Что сделать с папкой «{path}»? Постов внутри: {count}.\n\n"
            f"• 📤 Убрать только папку — посты и подпапки останутся, переедут в «{parent or 'корень'}».\n"
            "• 🗑 Удалить вместе с постами — посты уйдут в корзину (вернуть можно оттуда).",
            buttons=[
                [Button.inline("📤 Убрать папку, посты оставить", data=f"foldflat|{path}")],
                [Button.inline("🗑 Удалить вместе с постами", data=f"folddelgo:{path}")],
                [Button.inline("❌ Отмена", data=f"fold|{path}")],
            ],
        )
        return

    if data_s.startswith("foldflat|"):
        path = data_s.split("|", 1)[1]
        ok, msg = db.dissolve_folder(path)
        parent = path.rpartition("/")[0]
        await event.edit(f"📤 Папка «{path}» убрана, содержимое — в «{parent or 'корне'}»." if ok else f"❌ {msg}",
                         buttons=[[Button.inline("📂 К папкам", data=f"fold|{parent}" if parent else "back_to_cats")]])
        return

    if data_s.startswith("folddelgo:"):
        path = data_s.split(":", 1)[1]
        n = db.delete_folder(path)
        await event.edit(f"🗑 Папка «{path}» удалена. Постов ушло в корзину: {n}.",
                         buttons=[[Button.inline("📂 К папкам", data="back_to_cats")]])
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
        # Карточка сразу показывает, что работа идёт (раньше нажатие выглядело как «ничего не произошло»)
        visual = item["content_type"] in ("photo", "video", "animation", "video_note", "voice", "audio")
        step = "смотрю картинку и " if visual else ""
        try:
            await event.answer("🔁 Перераспознаю…")
            await event.edit(f"⏳ Перераспознаю пост #{item_id}: {step}определяю папку…\n\n"
                             f"Было: «{item['category']}» — {_fmt_short(item.get('summary') or '', 120)}",
                             buttons=None)
        except Exception:
            pass
        old_cat = item["category"]
        txt = await _recat_text(item)
        if not txt:
            await show_item_view(event, item_id)
            await event.respond(f"❌ Пост #{item_id}: не из чего распознавать — нет текста, а картинку описать "
                                "не удалось. Попробуй позже или «✍️ Уточнить».")
            return
        result = await _ai_categorize(txt, item["content_type"], item["source_channel"], exclude_id=item_id)
        if result is None:
            # раньше при сбое Groq пост молча улетал в «Другое»
            await show_item_view(event, item_id)
            await event.respond(f"⏳ Пост #{item_id}: ИИ сейчас перегружен или кончился бесплатный запас — "
                                "попробуй через минуту. Пост остался как был.")
            return
        db.update_item_category(item_id, result["category"], result["summary"])
        try:
            await show_item_view(event, item_id)
            moved = (f"перенёс из «{old_cat}» в «{result['category']}»" if result["category"] != old_cat
                     else f"остался в «{old_cat}», обновил описание")
            await event.respond(f"✅ Пост #{item_id} перераспознан: {moved}.", buttons=main_keyboard())
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
        act = {
            "kind": "comment", "target": item_id, "msg_id": None,
            "back": f"view:{item_id}", "reply_id": event.message_id,
        }
        sid = pending.push(
            event.chat_id, act, "📝 Заметка к посту",
            replay={"kind": "text", "text": "📝 Напиши заметку."},
        )
        try:
            await event.answer()
        except Exception:
            pass
        cur = item.get("comment") or ""
        q = ("📝 Напиши заметку к посту или скажи её голосовым — зачем сохранил, что попробовать. "
             "Она видна в карточке и находится поиском.\nЧтобы удалить заметку — отправь «-».")
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
            await event.answer("Нет доступных папок", alert=True)
            return
        await event.edit("📂 Переместить в папку:", buttons=move_keyboard(item_id, cats))
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
            await event.answer("🔒 Папка заблокирована", alert=True)
            return
        db.update_item_category(int(item_id_str), category)
        if db.should_lock("move"):
            db.set_item_locked(int(item_id_str), True)
        await show_item_view(event, int(item_id_str))
        await _note_manual_move(event, [int(item_id_str)], category)
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

    if data_s == "dups_clean":
        groups = db.find_archive_dups()
        extra_ids: list[int] = []
        removed = 0
        for _tok, its in groups:
            for it in its[1:]:
                if not it.get("locked"):
                    if db.trash_item(it["id"]):
                        extra_ids.append(it["id"])
                        removed += 1
        if not removed:
            await event.answer("✅ Лишних дублей нет", alert=True)
            return
        _set_undo(event.chat_id, {"kind": "untrash", "ids": extra_ids})
        await event.edit(
            f"🗑 Удалено лишних дублей: {removed} пост(ов). Первые из групп остались в архиве.",
            buttons=_undo_button(),
        )
        await event.answer("✅")
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


def _ai_view_text(item: dict) -> str:
    """Что бот передал ИИ про этот пост: текст без подвалов + распознанное с медиа."""
    stored = item.get("original_text") or ""
    clean = postctx.clean_post_text(strip_aux(stored))
    aux = postctx.aux_part(stored) if has_aux(stored) else ""
    lines = [f"👁 Что увидел ИИ в посте #{item['id']}", ""]
    txt = strip_markdown(clean["text"]).strip()
    if txt:
        lines.append(f"📝 Текст поста: {_fmt_short(txt, 400)}")
    else:
        lines.append("📝 Подписи нет (или только подвал канала — его ИИ не учитывает)")
    if aux:
        icons = {"Изображение": "🖼", "Кадр из видео": "🎞", "Речь": "🎙", "Превью ссылки": "🔗",
                 "По ссылке": "🔗", "Кнопки": "🔘", "Опрос": "📊", "Музыка": "🎵", "Файл": "📎"}
        lines.append("")
        for line in aux.splitlines():
            line = line.strip()
            if not line:
                continue
            ico = next((v for k, v in icons.items() if line.startswith(k)), "•")
            lines.append(f"{ico} {_fmt_short(line, 600)}")
    elif item.get("content_type") in ("photo", "video", "animation", "voice", "video_note", "audio"):
        lines += ["", "🙈 Картинку/звук ИИ ещё не разбирал — пост раскладывался только по тексту. "
                      "Нажми «🔁 Перераспознать» или дождись ночного добора."]
    rule = _channel_rule(item.get("source_channel"))
    if rule:
        lines += ["", f"📡 Канал «{item['source_channel']}» по правилу всегда идёт в «{rule}»."]
    return "\n".join(lines)


# ── ⏰ Напоминания и 🆕 «ещё не смотрел» ────────────────────────────────────
REMIND_HOUR_UTC = 6   # 09:00 по Москве
REMIND_CHOICES = [("tom", "Завтра утром"), ("d3", "Через 3 дня"), ("sat", "В субботу"), ("w", "Через неделю")]
_WEEKDAYS = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]


def _reminders() -> list[dict]:
    try:
        data = json.loads(db.get_setting("reminders", "[]") or "[]")
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _reminders_set(lst: list[dict]) -> None:
    db.set_setting("reminders", json.dumps(lst[-200:], ensure_ascii=False))


def _reminder_for(item_id: int) -> dict | None:
    return next((r for r in _reminders() if r.get("id") == item_id), None)


def _remind_due(kind: str, now: datetime | None = None) -> float:
    # дни считаем по Москве: «завтра утром», выбранное в 02:30 МСК, — это завтра, а не через 6 часов
    now = (now or datetime.utcnow()) + timedelta(hours=3)
    base = now.replace(hour=REMIND_HOUR_UTC + 3, minute=0, second=0, microsecond=0) - timedelta(hours=3)
    if kind == "d3":
        day = base + timedelta(days=3)
    elif kind == "w":
        day = base + timedelta(days=7)
    elif kind == "sat":
        ahead = (5 - now.weekday()) % 7 or 7
        day = base + timedelta(days=ahead)
    else:
        day = base + timedelta(days=1)
    from datetime import timezone as _tz
    return day.replace(tzinfo=_tz.utc).timestamp()


def _remind_label(due: float) -> str:
    d = datetime.utcfromtimestamp(due) + timedelta(hours=3)
    return f"{_WEEKDAYS[d.weekday()]} {d:%d.%m}"


def _seen_state() -> tuple[int, set]:
    """Всё, что было в архиве до появления отметки, считаем просмотренным (base) —
    иначе 765 старых постов разом стали бы «новыми»."""
    base = db.get_setting("seen_base", "")
    if not base:
        ids = db.get_item_ids()
        base = str(max(ids) if ids else 0)
        if ids:   # пустая база = ещё не восстановлена из бэкапа — ничего не запоминаем
            db.set_setting("seen_base", base)
    try:
        seen = set(json.loads(db.get_setting("seen_ids", "[]") or "[]"))
    except Exception:
        seen = set()
    return int(base), seen


def _opened_at() -> dict:
    try:
        v = json.loads(db.get_setting("opened_at", "{}") or "{}")
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _note_opened(item_id: int) -> None:
    """Запоминаем день, когда пост открывали (для «🧹 Уборки»: давно не открывал)."""
    day = datetime.utcnow().strftime("%Y-%m-%d")
    d = _opened_at()
    if d.get(str(item_id)) == day:
        return
    d.pop(str(item_id), None)
    d[str(item_id)] = day
    while len(d) > 6000:
        d.pop(next(iter(d)))
    db.set_setting("opened_at", json.dumps(d))


def mark_seen(item_id: int) -> None:
    try:
        _note_opened(item_id)
    except Exception:
        logger.debug("opened_at", exc_info=True)
    base, seen = _seen_state()
    if item_id > base and item_id not in seen:
        seen.add(item_id)
        ordered = sorted(seen)
        if len(ordered) > 3000:
            # самые старые отметки убираем, сдвигая «всё до этого номера — просмотрено»:
            # иначе выпавшие из списка посты снова стали бы 🆕
            cut = ordered[-3000 - 1]
            db.set_setting("seen_base", str(max(base, cut)))
            ordered = ordered[-3000:]
        db.set_setting("seen_ids", json.dumps(ordered))


def unseen_ids() -> set:
    base, seen = _seen_state()
    return {i for i in db.get_item_ids() if i > base and i not in seen}


def _mark_new(items: list[dict]) -> list[dict]:
    new = unseen_ids()
    for it in items:
        it["new"] = it.get("id") in new
    return items


def _shares() -> dict:
    try:
        data = json.loads(db.get_setting("shares", "{}") or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _shares_set(d: dict) -> None:
    db.set_setting("shares", json.dumps(d, ensure_ascii=False))


def get_share(sid: str) -> dict | None:
    sh = _shares().get(sid)
    return sh if isinstance(sh, dict) and sh.get("kind") in ("folder", "cat") else None


def _share_target_title(sh: dict) -> str:
    return sh["val"].rpartition("/")[2] if sh["kind"] == "folder" else sh["val"]


async def share_folder(event, kind: str, val: str) -> None:
    """«🔗 Поделиться»: ссылка на страницу только с этой папкой (одна ссылка на папку)."""
    if (kind == "folder" and db.in_private(val)) or (kind == "cat" and val in db.private_categories()):
        await event.answer("🔐 Это личная папка — ссылкой для друзей ей не поделиться", alert=True)
        return
    shares = _shares()
    sid = next((k for k, v in shares.items() if v.get("kind") == kind and v.get("val") == val), None)
    if not sid:
        import secrets as _secrets
        sid = _secrets.token_urlsafe(9)
        shares[sid] = {"kind": kind, "val": val, "created": int(time.time())}
        _shares_set(shares)
    uid = db.current_user_id() or event.sender_id
    link = webarchive.share_link(uid, sid)
    if not link:
        await event.respond("🔗 Не получилось: у сервера не задан адрес (PUBLIC_URL).")
        return
    if kind == "folder":
        groups = {c["category"]: (c["group"] or "").strip("/") for c in db.get_categories()}
        n = sum(c["count"] for c in db.get_categories() if webarchive.in_scope(c["category"], groups.get(c["category"], ""), {"kind": kind, "val": val}))
    else:
        n = db.get_total_items(val)
    await event.respond(
        f"🔗 Ссылка на папку «{_share_target_title({'kind': kind, 'val': val})}» ({n} пост.):\n{link}\n\n"
        "Кто откроет — увидит только эту папку с подпапками: плитку, превью и тексты постов. "
        "Остальной архив, «что увидел ИИ» и каналы не видны. Отозвать — кнопкой ниже, ссылка сразу перестанет работать.",
        parse_mode=None, link_preview=False,
        buttons=[[Button.url("🌐 Открыть", link)], [Button.inline("🚫 Отозвать ссылку", data=f"unshare:{sid}")]])


# ── Мост «бот ↔ мини-приложение» ─────────────────────────────────────────────
# В приложении доступно всё, что есть в боте: нажатие кнопки в приложении вызывает тот же
# обработчик (on_callback), а его ответы (edit/respond/send_message в этот чат) не уходят в
# Telegram, а возвращаются приложению экранами «текст + кнопки». Пока идёт вызов — перехват
# включён только для этого пользователя и этой задачи (contextvar); фоновые задачи, начатые
# внутри (перераспознавание и т.п.), после ответа пишут в Telegram как обычно.

import contextvars as _contextvars
import itertools as _itertools
from types import SimpleNamespace as _NS

_web_cap: "_contextvars.ContextVar[_WebCap | None]" = _contextvars.ContextVar("web_cap", default=None)
_web_ids = _itertools.count(1)


def _web_buttons(buttons) -> list[list[dict]]:
    """Inline-кнопки Telethon → [[{t, d}|{t, u}]]. Нижнее меню (Button.text) и мини-приложение — пропускаем."""
    if not buttons:
        return []
    rows = buttons if isinstance(buttons, (list, tuple)) else [buttons]
    if rows and not isinstance(rows[0], (list, tuple)):
        rows = [rows]
    out = []
    for row in rows:
        r = []
        for b in row if isinstance(row, (list, tuple)) else [row]:
            t = getattr(b, "type", None)
            kind = type(t).__name__ if t is not None else ""
            text = getattr(b, "text", "") or ""
            if kind == "InlineButtonTypeCallback":
                data = getattr(t, "data", b"") or b""
                r.append({"t": text, "d": data.decode("utf-8", "ignore")})
            elif kind == "InlineButtonTypeUrl":
                r.append({"t": text, "u": getattr(t, "url", "")})
        if r:
            out.append(r)
    return out


class _WebCap:
    def __init__(self, uid: int):
        self.uid = uid
        self.out: list[dict] = []
        self.toasts: list[dict] = []
        self.open = True

    def own(self, entity) -> bool:
        if not self.open:
            return False
        try:
            eid = entity if isinstance(entity, int) else getattr(entity, "id", None) or getattr(entity, "user_id", None)
            return int(eid) == self.uid
        except Exception:
            return False

    def new(self, text, buttons=None) -> "_WebMsg":
        mid = -next(_web_ids)
        self.out.append({"op": "new", "id": mid, "text": text if isinstance(text, str) else "",
                         "rows": _web_buttons(buttons)})
        return _WebMsg(self, mid)

    def edit(self, mid: int, text=None, buttons=None, keep_buttons: bool = False) -> None:
        self.out.append({"op": "edit", "id": mid, "text": text if isinstance(text, str) else None,
                         "rows": None if keep_buttons else _web_buttons(buttons)})

    def toast(self, text, alert=False) -> None:
        if text:
            self.toasts.append({"t": str(text), "alert": bool(alert)})


class _WebMsg:
    def __init__(self, cap: _WebCap, mid: int):
        self.cap, self.id, self.chat_id = cap, mid, cap.uid

    async def edit(self, text=None, **kw):
        if not self.cap.open:   # ответ приложению уже ушёл — пишем в чат
            if text:
                keep = {k: v for k, v in kw.items() if k in ("buttons", "parse_mode", "link_preview")}
                return await client.send_message(self.cap.uid, text, **keep)
            return self
        self.cap.edit(self.id, text, kw.get("buttons"), keep_buttons="buttons" not in kw)
        return self

    async def delete(self):
        self.cap.out.append({"op": "delete", "id": self.id})

    async def respond(self, message="", **kw):
        return self.cap.new(message, kw.get("buttons"))

    reply = respond


class _WebEvent:
    """Подмена события Telegram для обработчиков бота (кнопка или текст из приложения)."""

    def __init__(self, cap: _WebCap, data: bytes = b"", src: int | None = None, text: str = ""):
        self.cap, self.data = cap, data
        self.chat_id = self.sender_id = cap.uid
        self.client = client
        self.message_id = self.id = src
        self.text = self.raw_text = text
        self.message = None

    async def answer(self, message=None, cache_time=0, *, url=None, alert=False):
        self.cap.toast(message, alert)

    async def edit(self, text=None, **kw):
        if self.message_id:
            self.cap.edit(self.message_id, text, kw.get("buttons"), keep_buttons="buttons" not in kw)
            return _WebMsg(self.cap, self.message_id)
        return self.cap.new(text, kw.get("buttons"))

    async def respond(self, message="", **kw):
        return self.cap.new(message, kw.get("buttons"))

    reply = respond

    async def delete(self):
        if self.message_id:
            self.cap.out.append({"op": "delete", "id": self.message_id})

    async def get_message(self):
        return None

    async def get_sender(self):
        return _NS(id=self.cap.uid, first_name="", last_name="", username=None, bot=False)


def _install_web_capture() -> None:
    """Перехват отправки в чат на время вызова из приложения (только для вызывающей задачи)."""
    if client is None or getattr(client, "_web_cap_on", False):
        return
    orig_send, orig_edit = client.send_message, client.edit_message
    orig_file, orig_del = client.send_file, client.delete_messages

    async def send_message(entity, message="", *a, **kw):
        cap = _web_cap.get()
        if cap is not None and cap.own(entity):
            return cap.new(message, kw.get("buttons"))
        return await orig_send(entity, message, *a, **kw)

    async def edit_message(entity, message=None, text=None, *a, **kw):
        cap = _web_cap.get()
        mid = getattr(message, "id", message)
        if isinstance(mid, int) and mid < 0:
            if cap is not None and cap.open:
                cap.edit(mid, text, kw.get("buttons"), keep_buttons="buttons" not in kw)
                return _WebMsg(cap, mid)
            # «сообщение» было экраном приложения, а ответ уже ушёл — фоновый итог шлём в чат новым сообщением
            if text:
                keep = {k: v for k, v in kw.items() if k in ("buttons", "parse_mode", "link_preview")}
                return await orig_send(entity, text, **keep)
            return None
        return await orig_edit(entity, message, text, *a, **kw)

    async def send_file(entity, file=None, *a, **kw):
        res = await orig_file(entity, file, *a, **kw)
        cap = _web_cap.get()
        if cap is not None and cap.own(entity):
            cap.toast("📎 Отправил файл в чат с ботом")
        return res

    async def delete_messages(entity, message_ids, *a, **kw):
        cap = _web_cap.get()
        ids = message_ids if isinstance(message_ids, (list, tuple)) else [message_ids]
        ids = [getattr(i, "id", i) for i in ids]
        if ids and all(isinstance(i, int) and i < 0 for i in ids):
            if cap is not None and cap.open:
                for i in ids:
                    cap.out.append({"op": "delete", "id": i})
            return []
        return await orig_del(entity, message_ids, *a, **kw)

    client.send_message, client.edit_message = send_message, edit_message
    client.send_file, client.delete_messages = send_file, delete_messages
    client._web_cap_on = True


# Разделы, у которых в боте нет своей кнопки-колбэка (команды и нижнее меню)
async def _web_cmd(ev: "_WebEvent", name: str) -> None:
    uid = ev.cap.uid
    if name == "service":
        await ev.respond(SERVICE_TEXT, buttons=_service_buttons())
    elif name == "settings":
        await cmd_settings(ev)
    elif name == "assistant":
        await assistant_open(ev)
    elif name == "stats":
        await cmd_stats(ev)
    elif name == "help":
        await cmd_help(ev)
    elif name == "export":
        await cmd_export(ev)
    elif name == "pending":
        await cmd_pending(ev)
    elif name == "dups":
        await show_dups(ev)
    elif name == "trash":
        await show_trash(ev)
    elif name == "limits":
        await cmd_limits(ev)
    elif name == "recent":
        await _do_recent(ev, 15)
    elif name == "topics":
        await show_topics(ev)
    elif name == "cleanup":
        await show_cleanup(ev, uid)
    elif name == "calendar":
        await show_calendar(ev)
    elif name == "fav":
        await show_favorites(ev)
    elif name == "month":
        text, unseen = month_digest_text()
        await ev.respond(text, buttons=[[Button.inline(f"📥 Разобрать новое ({unseen})", data="unseen")]]
                         if unseen else None)
    elif name == "deals":
        await check_game_deals(uid, show_all=True, chat_id=uid)
    elif name == "anime":
        await check_watch(uid, "anime", show_all=True, chat_id=uid)
    elif name == "releases":
        await check_watch(uid, "github", show_all=True, chat_id=uid)
    elif name == "read":
        await send_daily_read(uid, chat_id=uid)
    else:
        ev.cap.toast("Неизвестный раздел")


_pv_fails: dict[int, list[float]] = {}


def web_private_unlock(uid: int, pin: str) -> dict:
    """Код личных папок из приложения: 5 ошибок за 10 минут — пауза на 10 минут."""
    now = time.time()
    fails = [t for t in _pv_fails.get(uid, []) if now - t < 600]
    if len(fails) >= 5:
        return {"ok": False, "error": "Слишком много попыток — подожди 10 минут"}
    if db.check_private_pin(str(pin or "")):
        _pv_fails.pop(uid, None)
        return {"ok": True}
    fails.append(now)
    _pv_fails[uid] = fails
    return {"ok": False, "error": f"Неверный код (осталось попыток: {5 - len(fails)})"}


IMG_SEARCH_RE = re.compile(r"^(?:найди|найти|ищи|поиск|есть ли|было|было ли|уже есть|\?)\s*[?!.]*$", re.I)
IMG_SAME_BITS = 6


def search_by_hash(h: str | None, limit: int = 20) -> list[tuple[int, int]]:
    """[(id поста, отличие в битах)] — от самого похожего. До 26 бит из 256 — «похоже»."""
    if not h:
        return []
    out = []
    for iid, hh in db.image_hashes():
        d = imghash.distance(h, hh)
        if d is not None and d <= imghash.SIMILAR_MAX_BITS + 6:
            out.append((iid, d))
    return sorted(out, key=lambda x: x[1])[:limit]


async def image_search_reply(event, msg) -> None:
    note = await event.respond("🔎 Ищу эту картинку в архиве…")
    h = await imghash.message_hash(client, msg)
    found = await asyncio.to_thread(search_by_hash, h)
    rows = []
    for iid, d in found[:10]:
        it = db.get_item(iid)
        if it:
            mark = "🟰" if d <= IMG_SAME_BITS else "≈"
            rows.append([Button.inline(f"{mark} {theme_icon(it['category'], '📦')} {_fmt_short(it.get('summary') or '', 44)}",
                                       data=f"view:{iid}")])
    if not h:
        text = "🔎 Не смог прочитать картинку — пришли её ещё раз."
    elif not rows:
        text = "🔎 Такой картинки в архиве нет. Чтобы сохранить — перешли её без подписи «найди»."
    else:
        same = sum(1 for _, d in found if d <= IMG_SAME_BITS)
        text = (f"🔎 Нашёл: {len(rows)}" + (f", из них та же самая — {same}" if same else "") +
                ".\n🟰 та же картинка · ≈ похожая (может быть другой мем на том же шаблоне)")
    try:
        await note.edit(text, buttons=rows or None)
    except Exception:
        await event.respond(text, buttons=rows or None)


def web_image_search(data: bytes) -> list[dict]:
    """Фото с телефона большое: сначала уменьшаем (как превью в Telegram), потом считаем отпечаток."""
    import io

    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            if im.width * im.height > 60_000_000:
                return []
            im.draft("RGB", (640, 640))
            im = im.convert("RGB")
            im.thumbnail((640, 640))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=85)
            data = buf.getvalue()
    except Exception:
        return []
    h = imghash.dhash(data)
    return [{"id": i, "d": d} for i, d in search_by_hash(h, 40)]


async def web_voice(uid: int, data: bytes, ctype: str, raw: bool = False) -> dict:
    """🎙 в приложении: запись → Whisper (бесплатно) → как будто текст написали в поле (помощник/ответ боту)."""
    ext = "mp4" if "mp4" in ctype or "aac" in ctype else ("ogg" if "ogg" in ctype else "webm")
    text = await transcribe_audio(data, language="ru", filename=f"voice.{ext}",
                                  content_type=ctype or "application/octet-stream")
    text = (text or "").strip()
    if raw:
        return {"heard": text}
    if not text:
        return {"out": [], "toasts": [{"t": "🎙 Не расслышал — попробуй ещё раз, ближе к микрофону", "alert": True}],
                "ask": None, "assistant": _assistant_active(uid), "heard": ""}
    res = await web_bridge(uid, {"kind": "text", "text": text})
    res["heard"] = text
    return res


async def web_bridge(uid: int, body: dict) -> dict:
    """kind=cb (нажатие кнопки бота), cmd (раздел), text (ответ на вопрос бота / помощнику)."""
    _install_web_capture()
    cap = _WebCap(uid)
    token = _web_cap.set(cap)
    kind = str(body.get("kind") or "")
    try:
        if kind == "cb":
            src = body.get("src")
            ev = _WebEvent(cap, str(body.get("data") or "").encode()[:256], int(src) if src else None)
            await on_callback(ev)
        elif kind == "cmd":
            await _web_cmd(_WebEvent(cap), str(body.get("name") or ""))
        elif kind == "text":
            text = str(body.get("text") or "").strip()[:2000]
            ev = _WebEvent(cap, text=text)
            top = pending.top(uid)
            if not text:
                pass
            elif top and (top["act"] or {}).get("kind") != "dup":
                entry = pending.pop(uid)
                await _handle_pending_action(ev, entry["act"], text)
            else:
                # как в чате: текст без открытого вопроса — просьба помощнику
                _assistant_session(uid)
                await assistant_ask(ev, text)
        else:
            cap.toast("Неизвестный запрос")
    except Exception:
        logger.exception("Мост приложения: %s %s", kind, str(body)[:120])
        cap.toast("Что-то пошло не так — попробуй ещё раз", True)
    finally:
        cap.open = False
        _web_cap.reset(token)
    top = pending.top(uid)
    ask = (top or {}).get("label") if top and (top["act"] or {}).get("kind") != "dup" else None
    return {"out": cap.out, "toasts": cap.toasts, "ask": ask, "assistant": _assistant_active(uid)}


def _twin_name_taken(twin: str, new: str) -> bool:
    new = (new or "").strip().rpartition("/")[2]
    return bool(new) and new != twin and db.get_category_row(new) is not None


def web_move(ids: list[int], to: str) -> int:
    """Перенос из архива: как «📂 Переместить» в боте (замки, автозамок, правила каналов)."""
    if not to or not db.get_category_row(to) and to not in _existing_category_names():
        return 0
    trow = db.get_category_row(to)
    if trow and trow["locked"]:
        return 0
    moved = 0
    for iid in ids:
        it = db.get_item(iid)
        if not it or it["locked"] or it["category"] == to:
            continue
        db.update_item_category(iid, to)
        if db.should_lock("move"):
            db.set_item_locked(iid, True)
        moved += 1
    if moved:
        uid = db.current_user_id()
        if uid:
            asyncio.ensure_future(_note_topic_moves(uid, [i for i in ids if (db.get_item(i) or {}).get("category") == to], to))
    return moved


def web_rename(item_id: int, summary: str) -> bool:
    it = db.get_item(item_id)
    if not it or it["locked"] or not summary:
        return False
    return bool(db.rename_item(item_id, summary)[0])


def web_unlock(item_id: int) -> bool:
    if not db.get_item(item_id):
        return False
    db.set_item_locked(item_id, False)
    return True


def web_trash(ids: list[int]) -> int:
    n = 0
    for iid in ids:
        it = db.get_item(iid)
        if it and not it["locked"] and db.trash_item(iid):
            n += 1
    return n


def web_remind(uid: int, item_id: int, kind: str) -> str:
    if not db.get_item(item_id):
        return ""
    due = _remind_due(kind if kind in dict(REMIND_CHOICES) else "tom")
    lst = [r for r in _reminders() if r.get("id") != item_id]
    lst.append({"id": item_id, "due": due, "chat": uid})
    _reminders_set(lst)
    return _remind_label(due)


# ── 📥 Разбор новых постов по одному ──
_triage_skip: dict[int, set] = {}   # chat_id → что пропустили в этом заходе


def _triage_next(chat_id: int) -> int | None:
    skip = _triage_skip.get(chat_id, set())
    ids = sorted(i for i in unseen_ids() if i not in skip)
    return ids[0] if ids else None


def _triage_suggest(item: dict, limit: int = 6) -> list[str]:
    """Куда ещё мог бы пойти пост: упомянутые в тексте папки, куда обычно ходит канал,
    самые большие папки — без текущей."""
    out: list[str] = []
    text = f"{item.get('summary') or ''} {item.get('original_text') or ''}"
    cands = _suggest_names(text, exclude=item["category"])
    cands += [c for c, _ in db.channel_category_counts(item.get("source_channel"), limit=4)]
    cands += [c["category"] for c in db.get_categories()]
    for c in cands:
        if c and c != item["category"] and c not in out:
            out.append(c)
        if len(out) >= limit:
            break
    return out


async def show_triage(event, item_id: int | None = None) -> None:
    chat_id = event.chat_id
    item_id = item_id or _triage_next(chat_id)
    item = db.get_item(item_id) if item_id else None
    if not item:
        _triage_skip.pop(chat_id, None)
        left = len(unseen_ids())
        text = ("🎉 Всё разобрано — новых постов нет." if not left else
                f"📥 Разбор закончен. Пропущено и осталось новыми: {left}.")
        await event.edit(text, buttons=[[Button.inline("📂 К папкам", data="back_to_cats")]], parse_mode=None)
        return
    left = len([i for i in unseen_ids() if i not in _triage_skip.get(chat_id, set())])
    chan = f"\n📡 {item['source_channel']}" if item.get("source_channel") else ""
    text = (f"📥 Разбор новых · осталось {left}\n\n"
            f"{theme_icon(item['category'], '📦')} Лежит в «{item['category']}»{chan}\n\n"
            f"{_fmt_short(strip_markdown(item.get('summary') or ''), 400)}\n\nПапка верная?")
    rows = [
        [Button.inline("✅ Да, верно", data=f"tri_ok:{item_id}"), Button.inline("📂 Другая папка", data=f"tri_mv:{item_id}")],
        [Button.inline("👁 Показать пост", data=f"tri_show:{item_id}"), Button.inline("⏰ Через неделю", data=f"tri_later:{item_id}")],
        [Button.inline("⏭ Пропустить", data=f"tri_skip:{item_id}"), Button.inline("🗑 В корзину", data=f"tri_del:{item_id}")],
        [Button.inline("✖️ Хватит", data="tri_stop")],
    ]
    await event.edit(text, buttons=rows, parse_mode=None)


async def handle_triage(event, data_s: str) -> None:
    chat_id = event.chat_id
    if data_s == "triage":
        _triage_skip.pop(chat_id, None)
        await show_triage(event)
        return
    if data_s == "tri_stop":
        _triage_skip.pop(chat_id, None)
        await event.edit(f"📥 Остановил разбор. Новых осталось: {len(unseen_ids())}.",
                         buttons=[[Button.inline("📥 Продолжить", data="triage"),
                                   Button.inline("📂 К папкам", data="back_to_cats")]], parse_mode=None)
        return
    op, _, rest = data_s.partition(":")
    parts = rest.split(":", 1)
    try:
        item_id = int(parts[0])
    except ValueError:
        return
    item = db.get_item(item_id)
    if not item:
        mark_seen(item_id)
        await show_triage(event)
        return
    if op == "tri_show":
        await event.answer("Пост — ниже")
        await send_item_to_chat(chat_id, item_id)
        return
    if op == "tri_mv":
        rows = [[Button.inline(f"📁 {theme_icon(c, '📦')} {c}", data=f"tri_set:{item_id}:{c}")]
                for c in _triage_suggest(item)]
        rows.append([Button.inline("◀️ Назад", data=f"tri_card:{item_id}")])
        await event.edit(f"📂 Куда перенести?\n\n{_fmt_short(strip_markdown(item.get('summary') or ''), 200)}\n\n"
                         "Нужной нет в списке — открой пост и нажми «📂 Переместить».", buttons=rows, parse_mode=None)
        return
    if op == "tri_card":
        await show_triage(event, item_id)
        return
    if op == "tri_skip":
        _triage_skip.setdefault(chat_id, set()).add(item_id)
    elif op == "tri_ok":
        mark_seen(item_id)
    elif op == "tri_set" and len(parts) > 1:
        if item["locked"]:
            await event.answer("🔒 Пост заблокирован — снять замок можно в его карточке", alert=True)
            return
        trow = db.get_category_row(parts[1])
        if trow and trow["locked"]:
            await event.answer("🔒 Эта папка заблокирована — в неё ничего не переносится", alert=True)
            return
        db.update_item_category(item_id, parts[1])
        if db.should_lock("move"):
            db.set_item_locked(item_id, True)
        mark_seen(item_id)
        await event.answer(f"📂 Перенёс в «{parts[1]}»")
    elif op == "tri_later":
        web_remind(chat_id, item_id, "w")
        mark_seen(item_id)
        await event.answer("⏰ Напомню через неделю")
    elif op == "tri_del":
        if item["locked"] or not db.trash_item(item_id):
            await event.answer("🔒 Не удалилось: пост заблокирован", alert=True)
            return
        await event.answer("🗑 В корзине")
    await show_triage(event)


# ── 🗓 «В этот день» ──
MEMORY_OFFSETS = [(365, "Год назад"), (182, "Полгода назад"), (91, "Три месяца назад"), (30, "Месяц назад")]
_VISUAL_FIRST = ("photo", "video", "animation")


def pick_memories(now: datetime | None = None, limit: int = 3) -> tuple[str, list[int]]:
    """(заголовок, id постов) — самый давний срок, за который что-то нашлось (±3 дня).
    Картинки и видео вперёд, «Другое/Заметки» — в конец; выбор постоянен внутри недели."""
    import random

    now = now or datetime.utcnow()
    rows = db.items_for_smart_search()
    week = f"{now.isocalendar()[0]}-{now.isocalendar()[1]}"
    for days, title in MEMORY_OFFSETS:
        lo = (now - timedelta(days=days + 3)).strftime("%Y-%m-%d")
        hi = (now - timedelta(days=days - 3)).strftime("%Y-%m-%d")
        found = [r for r in rows if lo <= (r["created_at"] or "")[:10] <= hi]
        if not found:
            continue
        rnd = random.Random(week + title)
        rnd.shuffle(found)
        found.sort(key=lambda r: (r["category"] in _STUB_CATS, r["content_type"] not in _VISUAL_FIRST))
        return title, [r["id"] for r in found[:limit]]
    return "", []


async def send_memories(uid: int) -> bool:
    title, ids = pick_memories()
    if not ids:
        return False
    await client.send_message(uid, f"🗓 {title} в твой архив попало:", buttons=main_keyboard(), silent=True)
    for iid in ids:
        await send_item_to_chat(uid, iid)
    return True


def _has_article(item_id: int) -> bool:
    try:
        a = db.article_get(item_id)
        return bool(a and a["status"] == "ok")
    except Exception:
        return False


_SIMI_STOP = set("""которые который которая потому сейчас теперь только можно через после более очень этого этой
этому также чтобы когда where which their there about would could should these those with from this that have will your
""".split())


def _simi_words(text: str) -> set[str]:
    text = re.sub(r"https?://\S+", " ", (text or "").casefold().replace("ё", "е"))
    return {w[:7] for w in re.findall(r"[a-zа-я0-9]{5,}", text) if w not in _SIMI_STOP}


def similar_items(item_id: int, limit: int = 12) -> list[tuple[int, str]]:
    """Похожие посты: [(id, почему)] — «🖼» похожая картинка, «🏷» те же темы, «🔤» общие редкие слова,
    «📡» тот же канал. Без ИИ."""
    import math

    rows = {r["id"]: r for r in db.items_for_smart_search()}
    me = rows.get(item_id)
    if not me:
        return []
    words = {i: _simi_words(f"{r['summary']} {postctx.strip_aux(r['original_text']) if has_aux(r['original_text']) else r['original_text']}")
             for i, r in rows.items()}
    df: dict[str, int] = {}
    for ws in words.values():
        for w in ws:
            df[w] = df.get(w, 0) + 1
    n = max(1, len(rows))
    hashes = dict(db.image_hashes())
    my_hash, my_topics = hashes.get(item_id), set(me.get("topics") or [])
    scored = []
    for i, r in rows.items():
        if i == item_id:
            continue
        score, why = 0.0, []
        if my_hash and hashes.get(i):
            d = imghash.distance(my_hash, hashes[i])
            if d is not None and d <= imghash.SIMILAR_MAX_BITS + 6:
                score += 4 + (imghash.SIMILAR_MAX_BITS + 6 - d)
                why.append("🖼")
        shared = my_topics & set(r.get("topics") or [])
        if not shared and my_topics:   # тема не размечена, но названа в тексте («GTA 6» в описании)
            hay = f"{r['summary']} {r['original_text'][:600]}".casefold()
            shared = {t for t in my_topics if len(t) >= 3 and t.casefold() in hay}
        if shared:
            score += 12 * len(shared)
            why.append("🏷 " + ", ".join(sorted(shared)[:2]))
        # общие РЕДКИЕ слова: минимум два (одно «обновление» — не повод), вклад ограничен
        rare = [w for w in words[item_id] & words[i] if df[w] <= max(3, n // 60)]
        if len(rare) >= 2:
            score += min(sum(math.log(n / df[w]) for w in rare), 9)
            why.append("🔤")
        if me["source_channel"] and r["source_channel"] == me["source_channel"] and score:
            score += 1.5
        if score >= 6:
            scored.append((score, i, " ".join(why)))
    scored.sort(key=lambda x: (-x[0], -x[1]))
    return [(i, why) for _, i, why in scored[:limit]]


async def show_similar(event, item_id: int):
    it = db.get_item(item_id)
    if not it:
        await event.answer("Пост не найден", alert=True)
        return
    found = similar_items(item_id)
    rows = []
    for iid, why in found:
        o = db.get_item(iid)
        if o:
            rows.append([Button.inline(f"{why.split(' ')[0] if why else '•'} {theme_icon(o['category'], '📦')} "
                                       f"{_fmt_short(o.get('summary') or '', 44)}", data=f"view:{iid}")])
    head = f"🖼 Похожие на «{_fmt_short(it.get('summary') or '', 60)}»"
    if rows:
        text = head + f" — {len(rows)}.\n\n🖼 похожая картинка · 🏷 те же темы · 🔤 общие слова"
    else:
        text = head + ": ничего похожего не нашёл."
    rows.append([Button.inline("◀️ К посту", data=f"view:{item_id}")])
    await event.respond(text, buttons=rows, parse_mode=None)


def _translations() -> dict:
    try:
        v = json.loads(db.get_setting("translations", "{}") or "{}")
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


async def translate_item(item_id: int) -> str | None:
    """🌐 Перевод поста на русский: из кэша или у ИИ (кэш — последние 300 переводов)."""
    cache = _translations()
    if str(item_id) in cache:
        return cache[str(item_id)]
    it = db.get_item(item_id)
    if not it:
        return None
    src = postctx.strip_footer_md(strip_aux(it.get("original_text") or "")) or it.get("summary") or ""
    text = await _cat.translate_ru(src)
    if text:
        cache.pop(str(item_id), None)
        cache[str(item_id)] = text
        while len(cache) > 300:
            cache.pop(next(iter(cache)))
        db.set_setting("translations", json.dumps(cache, ensure_ascii=False))
    return text


async def article_brief_for(item_id: int) -> str | None:
    """«Кратко по-русски» к статье поста: из кэша или у ИИ (и в кэш — второй раз бесплатно)."""
    art = db.article_get(item_id)
    if not art or art["status"] != "ok":
        return None
    if art["brief"]:
        return art["brief"]
    text = await _cat.article_brief(art["title"], art["text"])
    if text:
        db.article_set_brief(item_id, text)
    return text


TOPIC_PAGE = 12


async def show_topics(event, page: int = 0):
    tps = db.topic_counts()
    rows = []
    if not tps:
        text = ("🏷 Тем пока нет. ИИ размечает посты названиями игр, аниме, брендов в фоне — "
                "загляни позже.")
    else:
        pages = (len(tps) + TOPIC_PAGE - 1) // TOPIC_PAGE
        page = max(0, min(page, pages - 1))
        text = (f"🏷 Темы — {len(tps)}" + (f" · стр. {page + 1} из {pages}" if pages > 1 else "")
                + "\n\nВсе посты про одну игру, аниме или бренд — из любых папок.")
        chunk = tps[page * TOPIC_PAGE:(page + 1) * TOPIC_PAGE]
        for i in range(0, len(chunk), 2):
            rows.append([Button.inline(f"{t} ({n})", data=f"tpc|0|{t}") for t, n in chunk[i:i + 2]])
        nav = []
        if page > 0:
            nav.append(Button.inline("⬅️", data=f"topics:{page - 1}"))
        if page < pages - 1:
            nav.append(Button.inline("➡️", data=f"topics:{page + 1}"))
        if nav:
            rows.append(nav)
    rows.append([Button.inline("◀️ К папкам", data="back_to_cats")])
    try:
        await event.edit(text, buttons=rows, parse_mode=None)
    except Exception:
        await event.respond(text, buttons=rows, parse_mode=None)


async def show_topic(event, name: str, page: int = 0):
    ids = db.topic_items(name)
    pages = max(1, (len(ids) + FAV_PAGE - 1) // FAV_PAGE)
    page = max(0, min(page, pages - 1))
    text = f"🏷 {name} — {len(ids)} пост." + (f" · стр. {page + 1} из {pages}" if pages > 1 else "")
    rows = []
    for iid in ids[page * FAV_PAGE:(page + 1) * FAV_PAGE]:
        it = db.get_item(iid)
        rows.append([Button.inline(f"{theme_icon(it['category'], '📦')} {_fmt_short(it.get('summary') or '', 48)}",
                                   data=f"view:{iid}")])
    nav = []
    if page > 0:
        nav.append(Button.inline("⬅️", data=f"tpc|{page - 1}|{name}"))
    if page < pages - 1:
        nav.append(Button.inline("➡️", data=f"tpc|{page + 1}|{name}"))
    if nav:
        rows.append(nav)
    rows.append([Button.inline("◀️ Все темы", data="topics:0"), Button.inline("📂 К папкам", data="back_to_cats")])
    try:
        await event.edit(text, buttons=rows, parse_mode=None)
    except Exception:
        await event.respond(text, buttons=rows, parse_mode=None)


def _watch_state(kind: str) -> dict:
    try:
        v = json.loads(db.get_setting(f"{kind}_state", "{}") or "{}")
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def _watch_save(kind: str, state: dict) -> None:
    db.set_setting(f"{kind}_state", json.dumps(state, ensure_ascii=False))


async def check_watch(uid: int, kind: str, show_all: bool = False, chat_id: int | None = None) -> int:
    """kind: anime (Shikimori) | github (релизы). Пишет только о новом; show_all — всё отслеживаемое.
    Первый проход лишь запоминает состояние. Возвращает, сколько отслеживается."""
    chat = chat_id or uid
    if kind == "anime":
        targets = watchers.anime_ids(db.article_links("shikimori"))
        got = await watchers.fetch_anime(list(targets))
    else:
        targets = watchers.github_repos(db.article_links("github.com"))
        got = await watchers.fetch_releases(list(targets))
    title = "📺 Аниме из архива" if kind == "anime" else "⬆️ Программы из архива (GitHub)"
    if not targets:
        if show_all:
            what = "Shikimori" if kind == "anime" else "GitHub"
            await client.send_message(chat, f"{title}: в архиве нет ссылок на {what} — следить не за чем.")
        return 0
    state = _watch_state(kind)
    lines, rows = [], []
    for key, cur in got.items():
        skey = str(key)
        prev = state.get(skey) or {}
        line = watchers.anime_news(prev, cur) if kind == "anime" else watchers.release_news(prev, cur, skey)
        if kind == "github" and prev.get("tag") == cur.get("tag") and prev.get("brief"):
            cur = dict(cur, brief=prev["brief"])
        state[skey] = cur
        if show_all and not line:
            if kind == "anime":
                st = watchers.STATUS_RU.get(cur.get("status"), cur.get("status") or "?")
                ep = (f", {cur.get('episodes_aired')}/{cur.get('episodes') or '?'} сер."
                      if cur.get("status") == "ongoing" else "")
                line = f"• {cur.get('russian') or cur.get('name')}: {st}{ep}"
            else:
                line = f"• {skey}: {cur.get('tag')}" + (f" от {cur['at']}" if cur.get("at") else "")
        if line:
            lines.append(line)
            if len(rows) < 8:
                if kind == "anime":
                    rows.append([Button.url(f"📺 {(cur.get('russian') or cur.get('name') or '')[:26]}",
                                            f"https://shikimori.one/animes/{key}"),
                                 Button.inline(f"👀 пост #{targets[key]}", data=f"view:{targets[key]}")])
                else:
                    row = [Button.url(f"📦 {skey.split('/')[-1][:24]}", cur.get("url") or f"https://github.com/{skey}")]
                    if cur.get("body"):
                        row.append(Button.inline("🧠 Что нового", data=f"ghb|{skey}"))
                    rows.append(row)
    keep = {str(k) for k in targets}
    state = {k: v for k, v in state.items() if k in keep}
    for v in state.values():
        if isinstance(v, dict) and v.get("body"):
            v["body"] = v["body"][:2000]
    _watch_save(kind, state)
    logger.info("Слежка %s (user=%s): отслеживаю %s, ответили %s, новостей %s", kind, uid, len(targets), len(got),
                len(lines))
    if lines:
        head = title + (":" if show_all else " — новое:")
        await client.send_message(chat, head + "\n\n" + "\n".join(lines[:25]), buttons=rows or None,
                                  parse_mode=None, link_preview=False)
    elif show_all:
        await client.send_message(chat, f"{title}: отслеживаю {len(targets)}, сайт сейчас не ответил — попробую позже.",
                                  parse_mode=None)
    return len(targets)


_YT_RE = re.compile(r"(youtube\.com/watch|youtu\.be/|youtube\.com/shorts)", re.I)


def pick_daily_read() -> int | None:
    """Пост «почитать сегодня»: сохранённая статья или видео-ссылка, которые ещё не предлагались;
    избранное и свежее — чаще. Когда всё предложено — круг начинается заново."""
    import random

    try:
        shown = set(json.loads(db.get_setting("read_shown", "[]") or "[]"))
    except Exception:
        shown = set()
    arts = set(db.articles_brief())
    vids = {r["id"] for r in db.article_links("youtu") if _YT_RE.search(r["text"])}
    pool = [i for i in (arts | vids) if db.get_item(i)]
    fresh = [i for i in pool if i not in shown]
    if not fresh and pool:
        db.set_setting("read_shown", "[]")
        fresh = pool
    if not fresh:
        return None
    favs = set(db.favorites())
    weights = [3 if i in favs else 1 for i in fresh]
    pick = random.choices(fresh, weights=weights, k=1)[0]
    db.set_setting("read_shown", json.dumps(sorted(shown | {pick})[-3000:]))
    return pick


async def send_daily_read(uid: int, chat_id: int | None = None, edit_event=None) -> bool:
    iid = pick_daily_read()
    chat = chat_id or uid
    if iid is None:
        text = "📖 Пока нечего предложить: нет сохранённых статей и видео по ссылкам."
        await (edit_event.edit(text) if edit_event else client.send_message(chat, text))
        return False
    it = db.get_item(iid)
    art = db.article_get(iid)
    summary = strip_markdown(it.get("summary") or "")
    lines = ["📖 Что почитать сегодня" if art and art["status"] == "ok" else "🎬 Что посмотреть сегодня", "",
             f"{theme_icon(it['category'], '📦')} {summary}"]
    if art and art["status"] == "ok":
        mins = max(1, round(len(art["text"].split()) / 180))
        lines.append(f"\n📄 {art['title'] or 'Статья'} · ~{mins} мин чтения")
    rows = [[Button.inline("👀 Открыть пост", data=f"view:{iid}")]]
    if art and art["status"] == "ok":
        rows[0].append(Button.inline("🧠 Кратко", data=f"artb:{iid}"))
    rows.append([Button.inline("🔄 Другое", data="rdnext"), Button.inline("⏰ Через неделю", data=f"remset:{iid}:w")])
    rows.append([Button.inline("🔕 Не присылать", data="rdoff")])
    text = "\n".join(lines)
    if edit_event:
        try:
            await edit_event.edit(text, buttons=rows, parse_mode=None)
            return True
        except Exception:
            pass
    await client.send_message(chat, text, buttons=rows, parse_mode=None, silent=True)
    return True


FAV_PAGE = 10


async def show_favorites(event, page: int = 0):
    ids = [i for i in db.favorites() if db.get_item(i)]
    rows = []
    if not ids:
        text = "⭐ Избранное пусто.\n\nОтметь пост кнопкой «☆ В избранное» в его карточке — он появится здесь."
    else:
        pages = (len(ids) + FAV_PAGE - 1) // FAV_PAGE
        page = max(0, min(page, pages - 1))
        text = f"⭐ Избранное ({len(ids)})" + (f" · стр. {page + 1} из {pages}" if pages > 1 else "")
        for iid in ids[page * FAV_PAGE:(page + 1) * FAV_PAGE]:
            it = db.get_item(iid)
            label = _fmt_short(it.get("summary") or "", 48)
            rows.append([Button.inline(f"{theme_icon(it['category'], '📦')} {label}", data=f"view:{iid}")])
        nav = []
        if page > 0:
            nav.append(Button.inline("⬅️", data=f"favlist:{page - 1}"))
        if page < pages - 1:
            nav.append(Button.inline("➡️", data=f"favlist:{page + 1}"))
        if nav:
            rows.append(nav)
    rows.append([Button.inline("◀️ К папкам", data="back_to_cats")])
    try:
        await event.edit(text, buttons=rows, parse_mode=None)
    except Exception:
        await event.respond(text, buttons=rows, parse_mode=None)


async def check_game_deals(uid: int, show_all: bool = False, chat_id: int | None = None) -> int:
    """Цены игр из ссылок на Steam. Пишет только о новых/выросших скидках (show_all — все текущие).
    Возвращает, сколько игр отслеживается."""
    apps = gamedeals.app_ids(db.article_links())
    if not apps:
        if show_all:
            await client.send_message(chat_id or uid, "🎮 В архиве нет ссылок на игры в Steam — следить не за чем.")
        return 0
    try:
        state = json.loads(db.get_setting("steam_prices", "{}") or "{}")
    except Exception:
        state = {}
    prices = await gamedeals.fetch_prices(list(apps))
    lines, rows = [], []
    for appid, g in prices.items():
        prev = state.get(str(appid)) or {}
        kind = gamedeals.news(prev, g)
        if show_all and not kind and g["discount"] > 0:
            kind = "sale"
        if kind:
            lines.append(gamedeals.deal_line(g, kind))
            if len(rows) < 8:
                rows.append([Button.url(f"🛒 {g['name'][:28]}", f"https://store.steampowered.com/app/{appid}/"),
                             Button.inline(f"👀 пост #{apps[appid]}", data=f"view:{apps[appid]}")])
        state[str(appid)] = {"discount": g["discount"], "free": g["free"], "priced": g["priced"]}
    keep = {str(a) for a in apps}
    state = {k: v for k, v in state.items() if k in keep}      # игры, которых уже нет в архиве, забываем
    db.set_setting("steam_prices", json.dumps(state))
    logger.info("Скидки Steam (user=%s): игр %s, ответили %s, сообщаю %s", uid, len(apps), len(prices), len(lines))
    if lines:
        head = "🎮 Скидки на игры из твоего архива:" if not show_all else "🎮 Сейчас со скидкой:"
        await client.send_message(chat_id or uid, head + "\n\n" + "\n".join(lines[:20]), buttons=rows or None,
                                  parse_mode=None, link_preview=False)
    elif show_all:
        await client.send_message(chat_id or uid, f"🎮 Слежу за {len(apps)} играми из архива — скидок сейчас нет. "
                                                  "Как появятся — напишу.", parse_mode=None)
    return len(apps)


def _link_health() -> dict:
    try:
        v = json.loads(db.get_setting("link_health", "{}") or "{}")
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def dead_links() -> dict[int, str]:
    """{id поста: с какого дня ссылка мертва (ДД.ММ.ГГГГ)}."""
    out = {}
    for k, v in _link_health().items():
        if isinstance(v, dict) and v.get("dead"):
            try:
                out[int(k)] = v["dead"]
            except ValueError:
                continue
    return out


LINK_CHECK_BATCH = 60


async def check_links_once(uid: int, batch: int = LINK_CHECK_BATCH) -> list[int]:
    """Проверяет до batch ссылок (давно не проверенные — первыми). «Умерла» — только после двух
    провалов с разницей ≥ 20 ч (сайт мог лежать минуту). Возвращает id постов, умерших сейчас."""
    health = _link_health()
    live_ids = set(db.get_item_ids())
    for k in [k for k in health if k.isdigit() and int(k) not in live_ids]:
        health.pop(k, None)          # пост удалили — его ссылку больше не проверяем
    cands = []
    with db.get_connection() as conn:
        rows = conn.execute("SELECT s.id, s.original_text, a.url FROM saved_items s "
                            "LEFT JOIN item_articles a ON a.item_id = s.id "
                            "WHERE s.original_text LIKE '%http%'").fetchall()
    for iid, text, aurl in rows:
        url = linkmeta.checkable_url(text or "") or (aurl or None)
        if url:
            cands.append((float((health.get(str(iid)) or {}).get("c", 0)), iid, url))
    cands.sort()
    now = time.time()
    today = (datetime.utcnow() + timedelta(hours=3)).strftime("%d.%m.%Y")
    sem = asyncio.Semaphore(4)
    newly: list[int] = []

    async def one(iid, url):
        async with sem:
            res = await linkmeta.check_link(url)
        h = health.setdefault(str(iid), {})
        h.update({"u": url[:300], "c": now})
        if res == "ok":
            h.pop("f", None), h.pop("t", None)
            if h.pop("dead", None):
                logger.info("Ссылка #%s снова открывается", iid)
        elif res == "dead":
            h["f"] = int(h.get("f", 0)) + 1
            h.setdefault("t", now)
            if h["f"] >= 2 and now - float(h["t"]) >= 20 * 3600 and not h.get("dead"):
                h["dead"] = today
                newly.append(iid)

    await asyncio.gather(*(one(i, u) for _, i, u in cands[:batch]))
    # провалившие первую проверку — перепроверить завтра, а не через неделю
    for _, iid, _u in cands[:batch]:
        h = health.get(str(iid)) or {}
        if h.get("f") == 1 and not h.get("dead"):
            h["c"] = now - 6 * 86400
    db.set_setting("link_health", json.dumps(health, ensure_ascii=False))
    logger.info("Проверка ссылок (user=%s): %s проверено, умерло сейчас %s, всего мёртвых %s",
                uid, min(batch, len(cands)), len(newly), sum(1 for v in health.values() if v.get("dead")))
    return newly


async def notify_dead_links(uid: int, ids: list[int]) -> None:
    if not ids:
        return
    arts = db.articles_brief()
    lines = [f"🪦 Ссылки больше не открываются ({len(ids)}) — страницы удалены:", ""]
    rows = []
    for iid in ids[:8]:
        it = db.get_item(iid)
        if not it:
            continue
        saved = " — 📄 текст статьи сохранён" if iid in arts else ""
        lines.append(f"• {_fmt_short(it.get('summary') or '', 60)}{saved}")
        rows.append([Button.inline(f"👀 {_fmt_short(it.get('summary') or '', 40)}", data=f"view:{iid}")])
    if len(ids) > 8:
        lines.append(f"…и ещё {len(ids) - 8}. Все — в архиве: «🪦 Умершие ссылки».")
    await client.send_message(uid, "\n".join(lines), buttons=rows or None, parse_mode=None, silent=True)


def _media_mirror() -> dict:
    try:
        v = json.loads(db.get_setting("media_mirror", "{}") or "{}")
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


def mirror_ids(item_id: int) -> list[int]:
    """id сообщений копии поста в канале-хранилище (пусто — копии нет)."""
    v = _media_mirror().get(str(item_id))
    return [int(x) for x in v] if isinstance(v, list) else []


def _no_channel_access(e: Exception) -> bool:
    """Нет доступа к каналу-хранилищу — только по настоящим ошибкам Telegram, а не по словам в тексте
    (раньше «Peer» без учёта регистра находилось внутри «TypeError», и обычный сбой файла
    принимался за «нет доступа»)."""
    from telethon import errors as _e

    kinds = tuple(k for k in (getattr(_e, n, None) for n in (
        "ChatWriteForbiddenError", "ChannelPrivateError", "ChatAdminRequiredError", "ChannelInvalidError",
        "PeerIdInvalidError", "UserBannedInChannelError", "ChatRestrictedError")) if k)
    return isinstance(e, kinds) or (isinstance(e, ValueError) and "input entity" in str(e))


_mirror_pause_until = 0.0
MIRROR_BATCH = 20
MIRROR_PAUSE = 4.0     # Telegram пускает в канал ~20 сообщений в минуту


async def mirror_media_once(uid: int, batch: int = MIRROR_BATCH) -> int:
    """Копирует до batch постов с файлами в канал-хранилище (новые — первыми). Возвращает сколько."""
    global _mirror_pause_until
    if not BACKUP_CHANNEL_ID or time.time() < _mirror_pause_until:
        return 0
    mirror = _media_mirror()
    with db.get_connection() as conn:
        rows = conn.execute("SELECT id FROM saved_items WHERE file_ids IS NOT NULL AND file_ids NOT IN ('', '[]') "
                            "ORDER BY id DESC").fetchall()
    todo = [r[0] for r in rows if str(r[0]) not in mirror][:batch]
    done_n = 0
    for iid in todo:
        item = db.get_item(iid)
        if not item:
            continue
        refs, _cap = await _resend(item)
        refs = [r for r in refs if r is not None]   # ссылку на файл не удалось разобрать — такой файл пропускаем
        if not refs:
            mirror[str(iid)] = []
            continue
        cap = f"#u{uid} #п{iid} · {item.get('category') or ''}\n{_fmt_short(item.get('summary') or '', 300)}"[:1024]
        try:
            sent = await client.send_file(BACKUP_CHANNEL_ID, refs if len(refs) > 1 else refs[0], caption=cap,
                                          silent=True, parse_mode=None)
            sent = sent if isinstance(sent, list) else [sent]
            mirror[str(iid)] = [m.id for m in sent if m]
            done_n += 1
        except FloodWaitError as e:
            _mirror_pause_until = time.time() + e.seconds + 5
            logger.info("Копия медиа: Telegram просит подождать %s с", e.seconds)
            break
        except Exception as e:
            err = f"{type(e).__name__}: {e}"
            if _no_channel_access(e):
                _mirror_pause_until = time.time() + 6 * 3600
                logger.warning("Копия медиа: нет доступа к каналу-хранилищу — %s", err)
                if db.get_setting("media_copy_err", "") != "1":
                    db.set_setting("media_copy_err", "1")
                    try:
                        await client.send_message(uid, "📦 Не получается копировать фото и видео в канал-хранилище: "
                                                       "похоже, бот не админ канала или канал удалён. Добавь бота "
                                                       "администратором канала (право «публиковать сообщения») — "
                                                       "копирование продолжится само.", parse_mode=None)
                    except Exception:
                        pass
                break
            if isinstance(e, (TypeError, ValueError)) or re.search(
                    r"FileReference|MediaEmpty|FileIdInvalid|MediaInvalid|FilePart|WebpageMedia", type(e).__name__):
                logger.info("Копия медиа #%s: файл не отправить (%s) — пропускаю", iid, err)
                mirror[str(iid)] = []      # битый файл — не повторяем бесконечно
            else:
                logger.info("Копия медиа #%s: временный сбой (%s) — продолжу позже", iid, err)
                break                      # сеть/сервер: эти посты попробуем в следующий раз
        await asyncio.sleep(MIRROR_PAUSE)
    if todo:
        db.set_setting("media_mirror", json.dumps(mirror))
        if done_n:
            db.set_setting("media_copy_err", "")
        logger.info("Копия медиа (user=%s): скопировано %s, всего в канале %s", uid, done_n,
                    sum(1 for v in mirror.values() if v))
    return done_n


async def media_mirror_loop():
    """Тихо копирует фото и видео архива в канал-хранилище: порциями, без спешки."""
    await asyncio.sleep(420)
    while True:
        for uid in db.all_users():
            prev = db.current_user_id()
            try:
                db.set_current_user(uid)
                if db.get_setting("media_copy", "1") == "1":
                    await mirror_media_once(uid)
            except Exception as e:
                logger.warning("Копия медиа (user=%s): %s", uid, e)
            finally:
                db.set_current_user(prev)
        await asyncio.sleep(600)


async def link_check_loop():
    """Ночью (02–05 МСК) — порция проверки ссылок; весь архив проходит примерно за неделю."""
    await asyncio.sleep(300)
    while True:
        hour = datetime.utcnow().hour
        if 23 <= hour or hour < 2:
            for uid in db.all_users():
                prev = db.current_user_id()
                try:
                    db.set_current_user(uid)
                    day = datetime.utcnow().strftime("%Y-%m-%d")
                    if db.get_setting("link_check", "1") == "1" and db.get_setting("last_link_check", "") != day:
                        db.set_setting("last_link_check", day)
                        newly = await check_links_once(uid)
                        db.set_current_user(uid)
                        await notify_dead_links(uid, newly)
                except Exception as e:
                    logger.warning("Проверка ссылок (user=%s): %s", uid, e)
                finally:
                    db.set_current_user(prev)
        await asyncio.sleep(1800)


async def articles_loop():
    """Забирает текст статей по ссылкам из постов: по 4 поста раз в 2 минуты (новые — первыми).
    ИИ не тратит; пост без подходящей ссылки помечается, чтобы не проверять снова."""
    await asyncio.sleep(180)
    while True:
        for uid in db.all_users():
            prev = db.current_user_id()
            try:
                db.set_current_user(uid)
                if db.get_setting("article_text", "1") != "1":
                    continue
                for it in db.items_needing_article(4):
                    url = linkmeta.article_url(it["text"])
                    if not url:
                        db.article_set(it["id"], "", "", "", "none")
                        continue
                    res = await linkmeta.fetch_article(url)
                    db.article_set(it["id"], res["url"], res["title"], res["text"], res["status"])
                    logger.info("Статья для #%s: %s, %s симв.", it["id"], res["status"], len(res["text"]))
            except Exception as e:
                logger.warning("Статьи (user=%s): %s", uid, e)
            finally:
                db.set_current_user(prev)
        await asyncio.sleep(120)


_topics_miss: dict[int, int] = {}


async def topics_backfill_loop():
    """Размечает темами старые посты пачками по 25 (≈3 тыс. токенов на пачку): днём, пока у
    модели есть запас; ~770 постов — за сутки-двое."""
    await asyncio.sleep(240)
    while True:
        pause = 20 * 60
        for uid in db.all_users():
            prev = db.current_user_id()
            try:
                db.set_current_user(uid)
                # днём на gpt-oss-120b (точнее), при нехватке — на 20b; ночью не мешаем ночному добору
                if _is_night():
                    continue
                model = next((m for m, need in ((_cat.SMART_MODEL, 60_000), (_cat.SERVICE_MODEL, 30_000))
                              if _cat.day_headroom(m) >= need), None)
                if not model:
                    continue
                done = db.item_topics()
                todo = [r for r in db.items_for_smart_search() if r["id"] not in done][:25]
                if not todo:
                    continue
                _cat.USAGE_SOURCE.set("bg")
                got = await _cat.extract_topics(todo, model=model)
                if got is None:
                    continue
                # пост, который ИИ пропустил в ответе, пробуем ещё раз (до 3 попыток), а не
                # записываем «тем нет» навсегда
                res = {}
                for r in todo:
                    if r["id"] in got:
                        res[r["id"]] = got[r["id"]]
                    else:
                        _topics_miss[r["id"]] = _topics_miss.get(r["id"], 0) + 1
                        if _topics_miss[r["id"]] >= 3:
                            res[r["id"]] = []
                if res:
                    db.set_item_topics(res)
                logger.info("ИИ-темы (user=%s): размечено %s, с темами %s", uid, len(todo), sum(1 for v in got.values() if v))
                pause = 3 * 60
            except Exception as e:
                logger.warning("ИИ-темы (user=%s): %s", uid, e)
            finally:
                db.set_current_user(prev)
        await asyncio.sleep(pause)


async def reminders_loop():
    """Раз в 5 минут присылает посты, о которых просили напомнить."""
    await asyncio.sleep(60)
    while True:
        for uid in db.all_users():
            prev = db.current_user_id()
            try:
                db.set_current_user(uid)
                due = [r for r in _reminders() if float(r.get("due", 0)) <= time.time()]
                finished, failed = [], []
                for r in due:
                    item = db.get_item(int(r["id"]))
                    chat = int(r.get("chat") or uid)
                    if not item:
                        finished.append(r)
                        continue
                    try:
                        await client.send_message(
                            chat, f"⏰ Ты просил напомнить: {strip_markdown(item.get('summary') or '')[:300]}",
                            parse_mode=None, buttons=[
                                [Button.inline("👁 Открыть пост", data=f"view:{item['id']}"),
                                 Button.inline("⏰ Ещё через неделю", data=f"remset:{item['id']}:w")],
                            ])
                        finished.append(r)
                        await send_item_to_chat(chat, item["id"])
                    except Exception as e:
                        # одна неудача не съедает остальные напоминания; эта повторится позже
                        logger.warning("Напоминание #%s не отправилось: %s", r.get("id"), e)
                        failed.append(r)
                if due:
                    key = lambda r: (r.get("id"), r.get("due"))
                    gone = {key(r) for r in finished}
                    retry = {key(r) for r in failed}
                    keep = []
                    for r in _reminders():   # перечитываем: пока слали, могли добавить новые
                        if key(r) in gone:
                            continue
                        if key(r) in retry:
                            r = dict(r, tries=int(r.get("tries", 0)) + 1)
                            if r["tries"] > 12:   # ~час попыток — сдаёмся, но в лог
                                logger.warning("Напоминание #%s снято после 12 попыток", r.get("id"))
                                continue
                        keep.append(r)
                    _reminders_set(keep)
            except Exception as e:
                logger.warning("Напоминания (user=%s): %s", uid, e)
            finally:
                db.set_current_user(prev)
        await asyncio.sleep(300)


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
    cnote = f"\n\n📝 Заметка: {strip_markdown(item['comment'])}" if item.get("comment") else ""
    tp = db.item_topics().get(item_id) or []
    tline = f"\n🏷 {' · '.join(tp)}" if tp else ""
    dead = dead_links().get(item_id)
    dnote = (f"\n\n🪦 Ссылка не открывается с {dead}" + (" — текст статьи сохранён: «📄 Текст статьи»"
                                                         if _has_article(item_id) else "")) if dead else ""
    text = f"{tags}{emoji} {item['category']}{lock}\n\n{summary}{chan}{tline}{cnote}{dnote}\n\n📅 ID: {item['id']}"
    rem = _reminder_for(item_id)
    buttons = view_keyboard(item_id, item["category"], item["locked"], _remind_label(rem["due"]) if rem else None,
                            article=_has_article(item_id), fav=item_id in db.favorites(),
                            foreign=postctx.is_foreign(item.get("original_text") or ""))
    mark_seen(item_id)
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
        await _nav_send(event, f"📁 Папка «{category}» пока пустая. Перешли сюда посты, перенеси их из других "
                               "папок или попроси помощника: «перенеси … в " + category + "».",
                        buttons=[[Button.inline("◀️ К папкам", data="back_to_cats")]])
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


async def _recat_text(item, refetch: bool | None = None, report: dict | None = None) -> str:
    """Вход для ИИ при перераспознавании поста.

    Если в сохранённом тексте уже есть содержательная подпись или распознанное
    (картинка, речь, превью) — берём его. Иначе (старые посты: видео без подписи,
    фото с одним подвалом канала) достаём оригинал из чата и разбираем заново
    новым анализатором; распознанное дописываем в пост, чтобы больше не качать."""
    stored = (item.get("original_text") or "").strip()
    base = analyzer.llm_text_from_stored(stored)
    clean = postctx.clean_post_text(strip_aux(stored))
    stored_has_aux = has_aux(stored)
    weak = postctx.content_letters(clean["text"]) < analyzer.VISION_IF_LETTERS_BELOW
    media_ct = item.get("content_type") in ("photo", "video", "animation", "voice", "video_note", "audio", "document")
    # распознанное есть, но картинку/кадр тогда описать не вышло (лимиты) — пробуем снова
    no_vision = (
        item.get("content_type") in ("photo", "video", "animation")
        and not re.search(r"^(Изображение|Кадр из видео)", postctx.aux_part(stored), re.M)
        and db.get_setting("photo_vision", "1") == "1"
    )
    if refetch is None:
        refetch = media_ct and weak and (not stored_has_aux or no_vision)
        if refetch and report is not None and report.get("no_refetch"):
            report["need_refetch"] = True
            return base
    if refetch and item.get("message_id") and item.get("chat_id"):
        try:
            if (item.get("media_group_id") or "").strip():
                msgs = await _origin_messages_checked(client, item)
            else:
                res = await client.get_messages(item["chat_id"], ids=[item["message_id"]])
                m = res[0] if isinstance(res, list) and res else res
                msgs = [m] if m is not None and getattr(m, "media", None) else []
            if msgs:
                analysis = await _analyze(client, msgs, item.get("content_type") or "text")
                if report is not None:
                    report["media_failed"] = bool(analysis.get("media_failed"))
                if analysis.get("aux"):
                    display = strip_aux(stored) if stored_has_aux else (analysis.get("display") or strip_aux(stored))
                    db.update_item_text(item["id"], join_stored(display, analysis["aux"]))
                if analysis.get("llm_text"):
                    return analysis["llm_text"]
        except Exception:
            logger.warning("Перераспознавание: не удалось разобрать оригинал id=%s", item.get("id"), exc_info=True)
    return base


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
            result = await _ai_categorize(txt, item["content_type"], item["source_channel"], exclude_id=item["id"])
            if result is None:
                skipped += 1
                continue
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


# ── Массовое перераспознавание: фоновая задача ───────────────────────────────
# Раньше посты гнались подряд без пауз: бесплатный Groq пускает ~8000 токенов в
# минуту на модель (≈3–4 поста), остальное падало с 429, и из 734 постов 522
# «не перезаписались». Теперь это очередь в настройке recat_job: идём с паузами,
# на лимите ждём сколько велит Groq и продолжаем, переживаем перезапуск Render.
# Основная модель тут — запасная (gpt-oss): у Groq лимиты на каждую модель свои,
# так qwen остаётся новым постам и расширению YouTube.
RECAT_GAP = 12.0          # обычная пауза между постами, с
RECAT_GAP_MAX = 180.0     # потолок паузы при частых 429
RECAT_MAX_TRIES = 6       # столько раз пробуем один пост, потом оставляем как был
_recat_tasks: dict[int, asyncio.Task] = {}
_recat_wake: dict[int, asyncio.Event] = {}


async def _recat_nap(uid: int, seconds: float) -> None:
    """Пауза, которую можно прервать (кнопка «Остановить»)."""
    ev = _recat_wake.setdefault(uid, asyncio.Event())
    try:
        await asyncio.wait_for(ev.wait(), max(0.0, seconds))
    except asyncio.TimeoutError:
        pass
    ev.clear()


def _recat_job_get() -> dict | None:
    try:
        job = json.loads(db.get_setting("recat_job", "") or "null")
    except Exception:
        return None
    return job if isinstance(job, dict) else None


def _recat_job_set(job: dict | None) -> None:
    db.set_setting("recat_job", json.dumps(job, ensure_ascii=False) if job else "")


RECAT_POST_COST = 3000    # токенов на пост с запасом (категория ~2 200, кадр 512px ещё ~1 200)
RECAT_VISION_DIM = 512    # в фоне картинки мельче: ≈ вдвое дешевле, архив доделывается быстрее


def _recat_keep(model: str) -> int:
    """Сколько суточных токенов модели не трогать фоновой очередью.
    qwen — половину: на неё же новые посты и расширение YouTube; gpt-oss — 15%
    (она запасная для новых постов)."""
    share = 0.5 if model == GROQ_CATEGORIZE_MODEL else 0.15
    keep = _cat.day_limit(model) * share
    if _is_night():
        # ночью можно ниже: к 08:00 МСК запас пополнится до прежнего уровня
        keep = max(0.0, keep - _cat.refill_per_sec(model) * _secs_until_morning())
    return int(keep)


def _recat_has_budget(model: str | None) -> bool:
    return bool(model) and _cat.day_headroom(model) - _recat_keep(model) >= RECAT_POST_COST


def _recat_models() -> tuple[str | None, str | None]:
    """(основная, запасная) для фоновой очереди с учётом суточного остатка; (None, None) — ждать."""
    order = [GROQ_FALLBACK_MODEL, _cat.SERVICE_MODEL, GROQ_CATEGORIZE_MODEL]
    usable = [m for i, m in enumerate(order) if m and m not in order[:i] and _recat_has_budget(m)]
    if not usable:
        return None, None
    return usable[0], (usable[1] if len(usable) > 1 else None)


def _recat_progress_text(job: dict) -> str:
    total = job.get("total", 0)
    left = len(job.get("ids") or []) + len(job.get("vision_wait") or [])
    lines = [f"🔁 Перераспознаю в фоне ({job.get('title') or 'выбранное'}): {total - left}/{total}"]
    stats = [f"✅ обновлено {job.get('done', 0)}"]
    if job.get("locked"):
        stats.append(f"🔒 {job['locked']}")
    if job.get("nodata"):
        stats.append(f"∅ без данных {len(job['nodata'])}")
    if job.get("failed"):
        stats.append(f"⚠️ не вышло {len(job['failed'])}")
    lines.append(" · ".join(stats))
    wait = float(job.get("paused_until") or 0) - time.time()
    if wait > 90:
        mins = int(wait // 60) + 1
        if job.get("pause_daily"):
            lines.append(
                f"⏸ Суточный запас бесплатного ИИ на фон исчерпан (часть берегу для новых постов"
                f" и расширения) — продолжу сам примерно через {mins} мин."
            )
        else:
            lines.append(f"⏸ ИИ перегружен — продолжу сам примерно через {mins} мин.")
    lines.append("\nБотом можно пользоваться как обычно: новые посты идут вне очереди.")
    return "\n".join(lines)


def _recat_stop_buttons():
    return [[Button.inline("⏹ Остановить", data="recat_stop")]]


# ── Умный отбор, ночной добор, лимиты ───────────────────────────────────────
_STUB_CATS = set(DEFAULT_CATEGORY.values()) | {"Другое"}
_VISUAL_CT = ("photo", "video", "animation")
NIGHT_START_UTC, NIGHT_END_UTC = 22, 5   # 01:00–08:00 по Москве
RECAT_POSTS_PER_DAY = 90                 # честная оценка для бесплатного Groq


def _is_night(now: datetime | None = None) -> bool:
    h = (now or datetime.utcnow()).hour
    return h >= NIGHT_START_UTC or h < NIGHT_END_UTC


def _secs_until_morning() -> float:
    now = datetime.utcnow()
    morning = now.replace(hour=NIGHT_END_UTC, minute=0, second=0, microsecond=0)
    if morning <= now:
        morning += timedelta(days=1)
    return (morning - now).total_seconds()


def _needs_recat(item: dict, vision_on: bool) -> bool:
    """Пост, который ИИ раскладывал вслепую: медиа без подписи, которое он не видел,
    или категория-заглушка («Другое», «Фото», «Видео», «Заметки»…)."""
    if item.get("locked"):
        return False
    stored = item.get("original_text") or ""
    clean = postctx.clean_post_text(strip_aux(stored))
    weak = postctx.content_letters(clean["text"]) < analyzer.VISION_IF_LETTERS_BELOW
    ct = item.get("content_type")
    aux = postctx.aux_part(stored) if has_aux(stored) else ""
    if weak and ct in _VISUAL_CT and vision_on and not re.search(r"^(Изображение|Кадр из видео)", aux, re.M):
        return True
    if weak and ct in ("voice", "video_note", "audio") and not aux:
        return True
    return item.get("category") in _STUB_CATS


def _recat_seen() -> set:
    try:
        return set(json.loads(db.get_setting("recat_job_seen", "[]") or "[]"))
    except Exception:
        return set()


def _recat_mark_seen(item_id: int) -> None:
    seen = _recat_seen()
    seen.add(int(item_id))
    db.set_setting("recat_job_seen", json.dumps(sorted(seen)))


def _smart_recat_ids() -> list[int]:
    vision_on = db.get_setting("photo_vision", "1") == "1"
    seen = _recat_seen()
    items = db.items_for_recat_scan()
    # свежие посты первыми — их чаще открывают
    return [it["id"] for it in sorted(items, key=lambda x: -x["id"])
            if it["id"] not in seen and _needs_recat(it, vision_on)]


def _days_hint(n: int) -> str:
    if n <= RECAT_POSTS_PER_DAY:
        return "до суток"
    d = -(-n // RECAT_POSTS_PER_DAY)
    return f"~{d} дн."


async def show_recat_choice(event):
    smart = _smart_recat_ids()
    total = len(db.get_item_ids())
    text = (
        "🔁 **Перераспознать архив**\n\n"
        f"🎯 **Умно** — только посты, где ИИ раньше гадал вслепую: фото и видео без подписи, "
        f"которые он ни разу не «видел», и посты в «Другое», «Фото», «Видео», «Заметки». "
        f"Сейчас таких: **{len(smart)}** ({_days_hint(len(smart))} фоном).\n\n"
        f"🔁 **Всё** — все {total} постов ({_days_hint(total)}). Посты с нормальным текстом "
        f"обычно и так разложены верно — на них уйдёт лимит впустую.\n\n"
        f"Бесплатный ИИ тянет примерно {RECAT_POSTS_PER_DAY} постов в сутки — остальное "
        f"бот доделает в фоне сам, пользоваться им можно как обычно."
    )
    rows = []
    if smart:
        rows.append([Button.inline(f"🎯 Умно ({len(smart)})", data="recat_smart")])
    rows.append([Button.inline(f"🔁 Всё ({total})", data="recatall_go")])
    rows.append([Button.inline("❌ Отмена", data="dismiss")])
    try:
        await event.edit(text, buttons=rows)
    except Exception:
        await event.respond(text, buttons=rows)


def _bar(frac: float, width: int = 8) -> str:
    k = max(0, min(width, round(frac * width)))
    fill = "🟩" if frac >= 0.5 else ("🟨" if frac >= 0.2 else "🟥")
    return fill * k + "⬜" * (width - k)


def _short_model(m: str) -> str:
    return (m or "").split("/")[-1]


def _limits_text() -> str:
    snap = _cat.usage_snapshot()
    roles = {}
    for m, role in ((GROQ_VISION_MODEL, "картинки"), (GROQ_CATEGORIZE_MODEL, "новые посты"), (GROQ_FALLBACK_MODEL, "запасная, фоновая очередь")):
        if m:
            roles.setdefault(m, []).append(role)
    if GROQ_MODEL and GROQ_MODEL in snap:
        roles.setdefault(GROQ_MODEL, []).append("расширение YouTube")
    lines = [
        "📊 **Лимиты бесплатного ИИ (Groq)**",
        "",
        "У каждой модели свой запас — 200 000 токенов в сутки. Он пополняется равномерно "
        "(~8 300 в час). Пост стоит ~2 200 токенов, картинка — ещё ~2 100 (в фоновом доборе ~1 200).",
        "",
    ]
    unsynced = False
    by_src = _cat.usage_by_source()
    if by_src:
        names = {"posts": "🆕 новые посты", "bg": "🔁 фон и ночной добор", "retry": "⏳ дораспознавание",
                 "cf": "☁️ Cloudflare",
                 "ext": "🧩 расширение YouTube", "service": "🛠 авто-порядок, подпапки, «Уточнить»"}
        lines.append("**Кто тратил за сутки** (с запуска бота):")
        total = sum(v[0] for v in by_src.values()) or 1
        for src, (tok, req) in sorted(by_src.items(), key=lambda x: -x[1][0]):
            tok_s = f"{tok:,}".replace(",", " ")
            lines.append(f"{names.get(src, src)} — {tok * 100 // total}% · {tok_s} токенов, запросов {req}")
        lines.append("")
    for m, info in snap.items():
        if m not in roles:
            continue
        cap = max(1, info["limit"])
        left = max(0, info["left"])
        lines.append(f"🧠 **{_short_model(m)}** — {', '.join(dict.fromkeys(roles[m]))}")
        line = f"{_bar(left / cap)} {left * 100 // cap}% · ≈ {left // 2200} {_plural_posts(left // 2200)}"
        deficit = cap - left
        if deficit > 500:
            hrs = deficit / _cat.refill_per_sec(m) / 3600
            line += f" · полный через ~{hrs:.0f} ч" if hrs >= 1 else f" · полный через ~{max(1, round(hrs * 60))} мин"
        lines.append(line)
        if info["requests"]:
            spent = f"{info['spent']:,}".replace(",", " ")
            lines.append(f"   с запуска бота: запросов {info['requests']}, токенов {spent}")
        if not info["synced"]:
            unsynced = True
        lines.append("")
    for name, title, reset in (("cloudflare", "☁️ **Cloudflare**", "03:00 МСК"),):
        if not _cat._extra_enabled(name):
            continue
        stt = _cat._EXTRA_STATE[name]
        ready = "✅ готов" if _cat.extra_available(name) else f"⏸ запас на сегодня кончился, снова с {reset}"
        lines.append(f"{title} — запасной, когда у Groq пусто: {ready}")
        if stt["ok"] or stt["fail"]:
            lines.append(f"   выручил раз: {stt['ok']}, не смог: {stt['fail']}")
        lines.append("")
    started = datetime.utcfromtimestamp(_cat.STARTED_AT) + timedelta(hours=3)
    if unsynced:
        lines.append(
            f"ℹ️ Бот запущен {started:%d.%m %H:%M} МСК и считает расход с этого момента. "
            "Точный остаток Groq сообщает только при упоре в лимит — тогда цифры выравниваются."
        )
        lines.append("")
    job = _recat_job_get()
    if job:
        total = job.get("total", 0)
        left = len(job.get("ids") or []) + len(job.get("vision_wait") or [])
        kind = "🌙 Ночной добор" if job.get("night") else "🔁 Перераспознавание"
        st = f"{kind}: {total - left}/{total}, обновлено {job.get('done', 0)}"
        wait = float(job.get("paused_until") or 0) - time.time()
        if wait > 60:
            st += f" · ⏸ пауза ~{int(wait // 60) + 1} мин"
        lines.append(st)
    try:
        q = len(db.retry_queue_items())
    except Exception:
        q = 0
    if q:
        lines.append(f"⏳ Ждут дораспознавания (ИИ был занят): {q}")
    smart = len(_smart_recat_ids())
    night = "вкл" if db.get_setting("night_recat", "1") == "1" else "выкл"
    lines.append(f"🎯 Постов, где ИИ гадал вслепую: {smart} · 🌙 ночной добор: {night}")
    return "\n".join(lines)


def _limits_buttons(from_service: bool = False):
    if from_service:
        return [[Button.inline("🔄 Обновить", data="ai_limits|svc"), Button.inline("◀️ Назад", data="service")]]
    return [[Button.inline("🔄 Обновить", data="ai_limits"), Button.inline("👌 Ок", data="dismiss")]]


# ── 🤖 ИИ-помощник по архиву ───────────────────────────────────────────────
ASSISTANT_TTL = 15 * 60   # сколько минут после последней реплики текст идёт помощнику, а не в заметки

ASSISTANT_INTRO = (
    "🤖 Помощник по архиву\n\n"
    "Напиши обычными словами, что сделать. Например:\n"
    "• наведи порядок: найди дубли и лишние папки\n"
    "• сложи Кино и Сериалы в папку «Кино и сериалы»\n"
    "• Shikimori влей в Новости аниме, а папку Реклама убери\n"
    "• перенеси все трейлеры из Кино в Сериалы\n"
    "• создай папку Дорамы в Развлечениях\n"
    "• поставь Gunpla иконку 🤖\n"
    "• что у меня лишнее?\n"
    "• найди мем, где кот в коробке\n\n"
    "Сначала я покажу план, менять буду только после «✅ Применить». "
    "Посты не удаляю никогда, а последнее изменение можно отменить.\n\n"
    "🎙 Можно и голосом — запиши голосовое или кружок.\n\n"
    "Пока разговор открыт, обычный текст и голосовые идут мне, а не в «Заметки». "
    "Пересланные посты сохраняются как всегда."
)


def _assistant_load(chat_id: int) -> dict | None:
    """Сессия помощника: из памяти или из БД (переживает перезапуск Render).
    until — до каких пор работают кнопки плана, talk_until — до каких пор текст идёт помощнику."""
    sess = assistant_sessions.get(chat_id)
    if sess is None:
        try:
            raw = json.loads(db.get_setting("assistant_session", "") or "null")
        except Exception:
            raw = None
        if isinstance(raw, dict) and raw.get("chat") == chat_id:
            raw["off"] = set(raw.get("off") or [])
            sess = raw
            assistant_sessions[chat_id] = sess
    if sess and sess.get("until", 0) < time.time():
        assistant_sessions.pop(chat_id, None)
        return None
    return sess


def _assistant_save(chat_id: int, sess: dict) -> None:
    data = dict(sess, off=sorted(sess.get("off") or []), chat=chat_id)
    try:
        db.set_setting("assistant_session", json.dumps(data, ensure_ascii=False))
    except Exception:
        logger.warning("Сессия помощника не сохранена", exc_info=True)


def _assistant_active(chat_id: int) -> bool:
    sess = _assistant_load(chat_id)
    return bool(sess and sess.get("talk_until", 0) > time.time())


def _assistant_session(chat_id: int, ids: list[int] | None = None, talk: bool = True) -> dict:
    sess = _assistant_load(chat_id)
    if not sess:
        sess = {"history": [], "ids": [], "plan": [], "bad": [], "off": set(), "msg_id": None,
                "until": 0, "talk_until": 0}
        assistant_sessions[chat_id] = sess
    if ids:
        sess["ids"] = list(dict.fromkeys(list(ids) + sess["ids"]))[:20]
    sess["until"] = max(sess.get("until", 0), time.time() + ASSISTANT_TTL)
    if talk:
        sess["talk_until"] = time.time() + ASSISTANT_TTL
    _assistant_save(chat_id, sess)
    return sess


def _assistant_close_btn() -> list:
    return [Button.inline("✖️ Закрыть помощника", data="as_x")]


def _assistant_view(sess: dict, reply: str) -> tuple[str, list]:
    lines = [f"🤖 {reply}" if reply else "🤖 Готово."]
    plan = sess.get("plan") or []
    off = sess.get("off") or set()
    if plan:
        lines.append("\n📋 План:")
        for i, a in enumerate(plan, 1):
            lines.append(f"{'⬜' if i - 1 in off else '✅'} {i}. {a['text']}")
    if sess.get("bad"):
        lines.append("\n⚠️ Не получится: " + "; ".join(sess["bad"][:5]))
    if plan:
        lines.append("\nНажми номер, чтобы убрать пункт. Или напиши, что поправить.")
    else:
        lines.append("\nМожно написать ещё одну просьбу.")
    rows = []
    if plan:
        btns = [Button.inline(f"{'⬜' if i in off else '✅'} {i + 1}", data=f"as_t:{i}") for i in range(len(plan))]
        rows += [btns[k:k + 5] for k in range(0, len(btns), 5)]
        n = len(plan) - len(off)
        rows.append([Button.inline(f"✅ Применить ({n})", data="as_go")])
    rows.append(_assistant_close_btn())
    return "\n".join(lines), rows


async def assistant_open(event, ids: list[int] | None = None, request: str | None = None):
    chat_id = event.chat_id
    sess = _assistant_session(chat_id, ids)
    if request:
        await assistant_ask(event, request)
        return
    note = ""
    if sess["ids"]:
        note = f"\n\n📌 Речь о постах: " + ", ".join(f"#{i}" for i in sess["ids"][:8])
    await event.respond(ASSISTANT_INTRO + note, buttons=[_assistant_close_btn()], parse_mode=None)


async def assistant_ask(event, text: str):
    chat_id = event.chat_id
    sess = _assistant_session(chat_id)
    progress = await event.respond("🤖 Думаю…", parse_mode=None)
    allowed = list(sess["ids"])
    history = list(sess["history"])
    if sess.get("plan"):
        history.append(("assistant", "Мой текущий план: " + "; ".join(a["text"] for a in sess["plan"])))
    try:
        res = await _cat.assistant_plan(text, assistant.structure_text(), history, assistant.posts_text(allowed))
    except Exception:
        logger.exception("Помощник: ошибка запроса")
        res = None
    if res is None:
        err = _cat.LLM_LAST_ERROR or ""
        why = "кончился бесплатный запас ИИ, попробуй позже" if re.search(r"429|limit|лимит", err, re.I) else \
            "ИИ не ответил, попробуй ещё раз"
        await _MsgProxy(chat_id, progress.id).edit(f"🤖 Не получилось: {why}.", buttons=[_assistant_close_btn()])
        return
    actions = await assistant.resolve_matching(res["actions"], allowed)
    ok, bad = assistant.validate(actions, allowed)
    found: list[dict] = []
    if res.get("find"):
        try:
            found, _note = await smart_search(res["find"])
        except Exception:
            logger.warning("Помощник: поиск упал", exc_info=True)
        sess["ids"] = list(dict.fromkeys([f["id"] for f in found] + sess["ids"]))[:20]
    sess.update({"plan": ok, "bad": bad, "off": set(), "msg_id": progress.id})
    sess["history"] = (sess["history"] + [("user", text), ("assistant", res["reply"])])[-8:]
    _assistant_save(chat_id, sess)
    body, rows = _assistant_view(sess, res["reply"])
    if res.get("find"):
        body += (f"\n\n🔍 Нашёл по смыслу: {len(found)}. Нажми, чтобы открыть; можно попросить, "
                 "например, «перенеси их в Смешное»." if found else "\n\n🔍 По смыслу ничего не нашёл.")
        rows = _found_rows(found[:12]) + rows
    try:
        await client.edit_message(chat_id, progress.id, body, buttons=rows, parse_mode=None)
    except Exception:
        await event.respond(body, buttons=rows, parse_mode=None)
    logger.info("Помощник: %s действий, %s отказов (%r)", len(ok), len(bad), text[:60])


async def _voice_to_text(msg) -> str:
    """Голосовое или кружок → текст (Whisper, бесплатно). Пусто — не расслышал."""
    is_round = bool(getattr(msg, "video_note", None))
    try:
        data = await asyncio.wait_for(client.download_media(msg, file=bytes), 60)
        text = await transcribe_audio(data, language="ru",
                                      filename="video_note.mp4" if is_round else "voice.ogg",
                                      content_type="video/mp4" if is_round else "application/octet-stream")
    except Exception:
        logger.warning("Голосовое не распозналось", exc_info=True)
        text = ""
    return (text or "").strip()


def web_set_note(item_id: int, text: str) -> dict:
    """📝 Заметка из приложения ("" — удалить)."""
    if not db.get_item(item_id):
        return {"ok": False}
    return {"ok": True, "note": db.set_item_comment(item_id, (text or "")[:2000])}


async def assistant_voice(event, msg) -> None:
    """Голосовое или кружок, пока открыт помощник: Whisper → текст → помощник.
    (Пересланные голосовые по-прежнему сохраняются как посты.)"""
    is_round = bool(getattr(msg, "video_note", None))
    note = await event.respond("🎙 Слушаю…", parse_mode=None)
    try:
        data = await asyncio.wait_for(client.download_media(msg, file=bytes), 60)
        text = await transcribe_audio(data, language="ru",
                                      filename="video_note.mp4" if is_round else "voice.ogg",
                                      content_type="video/mp4" if is_round else "application/octet-stream")
    except Exception:
        logger.warning("Помощник: голосовое не распозналось", exc_info=True)
        text = ""
    text = (text or "").strip()
    if not text:
        await _MsgProxy(event.chat_id, note.id).edit(
            "🎙 Не расслышал — скажи ещё раз или напиши текстом.", buttons=[_assistant_close_btn()])
        return
    await _MsgProxy(event.chat_id, note.id).edit(f"🎙 Ты сказал: «{_fmt_short(text, 300)}»")
    await assistant_ask(event, text)


WEEKLY_CLEANUP_REQUEST = (
    "Еженедельная проверка архива. Наведи порядок: настоящие дубли категорий, категории по названию "
    "сайта или канала, папки с одной категорией, разбросанные родственные категории. Ничего не "
    "переименовывай и не переноси без явной пользы. Если всё в порядке — actions пустой и одной фразой "
    "скажи, что порядок."
)


async def assistant_weekly(uid: int) -> bool:
    """Раз в неделю: помощник смотрит архив и присылает план (кнопки живут 3 дня). True — прислал."""
    if len(db.get_item_ids()) < 10:
        return False   # пустой/крошечный архив — убирать нечего, бесплатный ИИ не тратим
    res = await _cat.assistant_plan(WEEKLY_CLEANUP_REQUEST, assistant.structure_text())
    if not res:
        return False
    allowed: list[int] = []
    ok, bad = assistant.validate(await assistant.resolve_matching(res["actions"], allowed), allowed)
    if not ok:
        logger.info("Еженедельная уборка (user=%s): порядок, писать нечего", uid)
        return False
    sess = {"history": [("assistant", res["reply"])], "ids": [], "plan": ok, "bad": [], "off": set(),
            "msg_id": None, "until": time.time() + 3 * 86400, "talk_until": 0}
    assistant_sessions[uid] = sess
    body, rows = _assistant_view(sess, res["reply"])
    msg = await client.send_message(
        uid, "🧹 Еженедельная уборка (выключается в ⚙️ Настройки → Дополнительно)\n\n" + body,
        buttons=rows, parse_mode=None, silent=True)
    sess["msg_id"] = msg.id
    _assistant_save(uid, sess)
    return True


async def handle_assistant_callback(event, data_s: str):
    chat_id = event.chat_id
    if data_s == "assistant":
        await assistant_open(event)
        return
    if data_s == "as_x":
        assistant_sessions.pop(chat_id, None)
        db.set_setting("assistant_session", "")
        try:
            await event.edit("🤖 Помощник закрыт. Позвать снова — кнопка «🤖 Помощник» или /ai.", buttons=None)
        except Exception:
            pass
        return
    if data_s == "as_undo":
        undo = assistant_undo.pop(chat_id, None)
        if not undo:
            await event.answer("Отменять нечего", alert=True)
            return
        if time.time() - undo.get("t", 0) > ASSISTANT_UNDO_TTL:
            await event.answer("Отменить уже нельзя: прошло больше суток. Попроси помощника вернуть как было.",
                               alert=True)
            return
        # отменяется только сделанное помощником; переносы руками и замки после него — остаются
        n = db.restore_structure(undo["before"], undo["after"])
        _refresh_icon_overrides()
        try:
            await event.edit(f"↩️ Вернул как было. Постов вернулось на прежние места: {n}.",
                             buttons=[[Button.inline("📂 К папкам", data="back_to_cats")]], parse_mode=None)
        except Exception:
            pass
        return
    sess = _assistant_load(chat_id)
    if not sess or not sess.get("plan"):
        await event.answer("План устарел — напиши просьбу ещё раз", alert=True)
        return
    sess["until"] = max(sess.get("until", 0), time.time() + ASSISTANT_TTL)
    sess["talk_until"] = time.time() + ASSISTANT_TTL
    if data_s.startswith("as_t:"):
        try:
            i = int(data_s.split(":", 1)[1])
        except ValueError:
            return
        sess["off"] ^= {i}
        _assistant_save(chat_id, sess)
        reply = sess["history"][-1][1] if sess["history"] else ""
        body, rows = _assistant_view(sess, reply)
        try:
            await event.edit(body, buttons=rows, parse_mode=None)
        except Exception:
            pass
        return
    if data_s == "as_go":
        chosen = [a for i, a in enumerate(sess["plan"]) if i not in sess["off"]]
        if not chosen:
            await event.answer("Все пункты сняты", alert=True)
            return
        snap = db.structure_snapshot()
        lines, recat_ids = assistant.apply_plan(chosen, cleanup=db.get_setting("cleanup_empty", "1") == "1")
        assistant_undo[chat_id] = {"before": snap, "after": db.structure_snapshot(), "t": time.time()}
        _refresh_icon_overrides()
        asyncio.create_task(_icons_for_new())
        sess.update({"plan": [], "bad": [], "off": set()})
        sess["history"] = (sess["history"] + [("assistant", "Применил: " + "; ".join(a["text"] for a in chosen))])[-8:]
        _assistant_save(chat_id, sess)
        tail = "\n\nМожно написать ещё одну просьбу или отменить сделанное."
        if recat_ids:
            tail = "\n\n🔁 Перераспознавание идёт в фоне, его отмена не вернёт." + tail
        try:
            await event.edit("🤖 Сделал:\n" + "\n".join(lines) + tail, parse_mode=None, buttons=[
                [Button.inline("↩️ Отменить", data="as_undo"), Button.inline("📂 К папкам", data="back_to_cats")],
                _assistant_close_btn(),
            ])
        except Exception:
            pass
        if recat_ids:
            await run_bulk_recat(event, recat_ids, "по просьбе помощнику")
        return


SERVICE_TEXT = (
    "🛠 **Сервис**\n\n"
    "Разобрать новое, напоминания, архив с картинками и ссылки на папки. "
    "Всё для наведения порядка в папках — в «🗂 Порядок в папках». "
    "«🧹 Уборка архива» — выбросить ненужное, «📅 Календарь» — что сохранял по дням."
)

ORDER_TEXT = (
    "🗂 Порядок в папках\n\n"
    "Проще всего — попросить помощника: он сам найдёт дубли, лишние папки и разбросанные "
    "родственные папки и покажет план. Ничего не меняется без «✅ Применить».\n\n"
    "Или вручную:\n"
    "• ⚡ Авто-порядок — ИИ предложит слить дубли и разложить по общим папкам\n"
    "• 🧩 Подпапки — похожие посты внутри папки → подпапки\n"
    "• 🔀 Объединить папки — слить посты двух папок\n"
    "• 🔁 Перераспознать… — заново разложить посты"
)


def _service_buttons():
    try:
        new, rem, sh = len(unseen_ids()), len(_reminders()), len(_shares())
    except Exception:
        new = rem = sh = 0
    return service_keyboard(new, rem, sh)


async def cmd_limits(event):
    await event.respond(_limits_text(), buttons=_limits_buttons())


async def night_recat_loop():
    """Ночью (01–08 МСК) дораспознаёт «слепые» посты на запасе, который иначе сгорит:
    ведро Groq полное — пополнение пропадает. К утру запас снова наполняется."""
    await asyncio.sleep(90)
    while True:
        for uid in db.all_users():
            prev = db.current_user_id()
            try:
                db.set_current_user(uid)
                job = _recat_job_get()
                if job and job.get("night") and not _is_night():
                    job["stop"] = True
                    _recat_job_set(job)
                    _recat_wake.setdefault(uid, asyncio.Event()).set()
                    _ensure_recat_worker(uid)
                elif not job and _is_night() and db.get_setting("night_recat", "1") == "1":
                    ids = _smart_recat_ids()
                    if ids:
                        _recat_job_set({
                            "ids": ids, "total": len(ids), "done": 0, "locked": 0,
                            "nodata": [], "failed": [], "tries": {}, "title": "ночной добор",
                            "chat_id": uid, "msg_id": None, "paused_until": 0,
                            "smart": True, "night": True,
                        })
                        logger.info("Ночной добор (user=%s): %s постов", uid, len(ids))
                        _ensure_recat_worker(uid)
            except Exception as e:
                logger.warning("Ночной добор (user=%s): %s", uid, e)
            finally:
                db.set_current_user(prev)
        await asyncio.sleep(600)


# ── Правила каналов ──────────────────────────────────────────────────────────
# «Канал X всегда в категорию Y». Появляются, когда ты 3 раза вручную переносишь
# посты канала в одну категорию (бот спрашивает), или из «🛠 Сервис → 📡 Правила
# каналов» по статистике архива. Пост такого канала с нормальным текстом
# раскладывается вообще без ИИ (не тратит лимит), остальные — ИИ пишет только описание.
CHRULE_VOTES_TO_ASK = 3
CHRULE_TEXT_LETTERS = 60
_chrule_offers: dict[str, tuple[str, str]] = {}


def _channel_rules() -> dict:
    try:
        r = json.loads(db.get_setting("channel_rules", "{}") or "{}")
        return r if isinstance(r, dict) else {}
    except Exception:
        return {}


def _channel_rules_set(rules: dict) -> None:
    db.set_setting("channel_rules", json.dumps(rules, ensure_ascii=False))


def _channel_rule(source: str | None) -> str | None:
    """Категория по правилу канала (если категория ещё существует)."""
    if not source:
        return None
    cat = _channel_rules().get(source)
    if cat and cat in set(_existing_category_names()):
        return cat
    return None


TRULE_VOTES_TO_ASK = 3
_trule_offers: dict[str, tuple[str, str]] = {}


def _topic_rules() -> dict:
    try:
        r = json.loads(db.get_setting("topic_rules", "{}") or "{}")
        return r if isinstance(r, dict) else {}
    except Exception:
        return {}


def _topic_rules_set(rules: dict) -> None:
    db.set_setting("topic_rules", json.dumps(rules, ensure_ascii=False))


def _topic_rule(topics: list[str]) -> tuple[str, str] | None:
    """(тема, папка) по правилу темы, если папка ещё существует и не заблокирована."""
    rules = _topic_rules()
    names = set(_existing_category_names())
    for t in topics or []:
        cat = rules.get(t)
        if cat and cat in names:
            row = db.get_category_row(cat)
            if not (row and row["locked"]):
                return t, cat
    return None


def _trule_token(topic: str, category: str) -> str:
    import hashlib

    tok = hashlib.sha1(f"t\x00{topic}\x00{category}".encode("utf-8")).hexdigest()[:10]
    _trule_offers[tok] = (topic, category)
    return tok


async def _note_topic_moves(chat_id: int, item_ids: list[int], target: str) -> None:
    """Считает ручные переносы по темам; на 3-м — предлагает правило «всё про тему → папка»."""
    try:
        votes = json.loads(db.get_setting("trule_votes", "{}") or "{}")
    except Exception:
        votes = {}
    rules, tps, ask = _topic_rules(), db.item_topics(), None
    for iid in item_ids:
        for t in tps.get(iid) or []:
            if rules.get(t) == target:
                continue
            per = votes.setdefault(t, {})
            if per.get(target, 0) < 0:
                continue
            per[target] = per.get(target, 0) + 1
            if per[target] >= TRULE_VOTES_TO_ASK and t not in rules:
                ask = (t, target)
    db.set_setting("trule_votes", json.dumps(votes, ensure_ascii=False))
    if not ask:
        return
    t, cat = ask
    tok = _trule_token(t, cat)
    try:
        await client.send_message(
            chat_id, f"🏷 Посты про «{t}» ты уже несколько раз переносил в «{cat}».\n"
                     f"Класть всё про «{t}» туда сразу?",
            buttons=[[Button.inline("✅ Да, всегда", data=f"tr_y:{tok}"), Button.inline("Нет", data=f"tr_n:{tok}")]],
            parse_mode=None)
    except Exception:
        logger.warning("Не удалось предложить правило темы", exc_info=True)


async def show_topic_rules(event) -> None:
    rules = _topic_rules()
    lines = ["🏷 Правила тем", "",
             "Всё про тему сразу кладётся в нужную папку — например, «GTA 6 → GTA». "
             "Бот сам предложит правило, когда заметит, что ты переносишь посты одной темы в одну папку."]
    rows = []
    if rules:
        lines.append("")
        for t, cat in sorted(rules.items()):
            lines.append(f"• {t} → {theme_icon(cat, '📦')} {cat}")
            rows.append([Button.inline(f"🗑 Убрать «{t}»", data=f"tr_d:{_trule_token(t, cat)}")])
    else:
        lines.append("\nПравил пока нет.")
    rows.append([Button.inline("📡 Правила каналов", data="chrules"), Button.inline("◀️ Сервис", data="service")])
    text = "\n".join(lines)
    try:
        await event.edit(text, buttons=rows, parse_mode=None)
    except Exception:
        await event.respond(text, buttons=rows, parse_mode=None)


async def handle_topic_rule_cb(event, data_s: str) -> bool:
    if data_s == "trules":
        await show_topic_rules(event)
        return True
    if not data_s.startswith(("tr_y:", "tr_n:", "tr_d:")):
        return False
    kind, tok = data_s.split(":", 1)
    offer = _trule_offers.get(tok)
    if not offer:
        await event.answer("Устарело — открой «🛠 Сервис → 📡 Правила каналов → 🏷 Правила тем»", alert=True)
        return True
    t, cat = offer
    rules = _topic_rules()
    if kind == "tr_y":
        rules[t] = cat
        _topic_rules_set(rules)
        await event.answer(f"Готово: всё про «{t}» → «{cat}»")
        try:
            await event.edit(f"✅ Правило: всё про «{t}» → «{cat}». Убрать — «🛠 Сервис → 📡 Правила каналов → 🏷 Правила тем».",
                             parse_mode=None)
        except Exception:
            pass
    elif kind == "tr_n":
        try:
            votes = json.loads(db.get_setting("trule_votes", "{}") or "{}")
        except Exception:
            votes = {}
        votes.setdefault(t, {})[cat] = -1
        db.set_setting("trule_votes", json.dumps(votes, ensure_ascii=False))
        try:
            await event.edit("👌 Хорошо, не буду.")
        except Exception:
            pass
    else:
        rules.pop(t, None)
        _topic_rules_set(rules)
        await event.answer(f"Правило «{t}» убрано")
        await show_topic_rules(event)
    return True


def _chrule_token(channel: str, category: str) -> str:
    import hashlib

    tok = hashlib.sha1(f"{channel}\x00{category}".encode("utf-8")).hexdigest()[:10]
    _chrule_offers[tok] = (channel, category)
    return tok


async def _note_manual_move(event, item_ids: list[int], target: str) -> None:
    """Считает ручные переносы по каналам; на 3-м предлагает правило."""
    try:
        await _note_topic_moves(event.chat_id, item_ids, target)
    except Exception:
        logger.warning("Голоса правил тем", exc_info=True)
    try:
        votes = json.loads(db.get_setting("chrule_votes", "{}") or "{}")
    except Exception:
        votes = {}
    rules = _channel_rules()
    ask = None
    for iid in item_ids:
        it = db.get_item(iid)
        ch = (it or {}).get("source_channel") or ""
        if not ch or rules.get(ch) == target:
            continue
        per = votes.setdefault(ch, {})
        if per.get(target, 0) < 0:  # отказался от такого правила — больше не спрашиваем
            continue
        per[target] = per.get(target, 0) + 1
        if per[target] >= CHRULE_VOTES_TO_ASK and ch not in rules:
            ask = (ch, target)
    db.set_setting("chrule_votes", json.dumps(votes, ensure_ascii=False))
    if not ask:
        return
    ch, cat = ask
    tok = _chrule_token(ch, cat)
    try:
        await client.send_message(
            event.chat_id,
            f"📡 Ты уже не раз переносил посты из «{ch}» в «{cat}».\n"
            f"Класть этот канал в «{cat}» всегда? Такие посты будут раскладываться сразу, "
            f"без траты лимита ИИ.",
            buttons=[[Button.inline("✅ Да, всегда", data=f"chr_y:{tok}"), Button.inline("Нет", data=f"chr_n:{tok}")]],
        )
    except Exception:
        logger.warning("Не удалось предложить правило канала", exc_info=True)


def _chrule_candidates() -> list[tuple[str, str, int]]:
    """Каналы, которые в архиве почти целиком лежат в одной категории (≥5 постов, ≥90%)."""
    rules = _channel_rules()
    per: dict[str, dict[str, int]] = {}
    for ch, cat, n in db.channel_category_matrix():
        per.setdefault(ch, {})[cat] = n
    out = []
    for ch, cats in per.items():
        if ch in rules:
            continue
        total = sum(cats.values())
        cat, n = max(cats.items(), key=lambda x: x[1])
        if total >= 5 and n / total >= 0.9 and cat not in _STUB_CATS:
            out.append((ch, cat, total))
    return sorted(out, key=lambda x: -x[2])[:8]


async def show_channel_rules(event) -> None:
    rules = _channel_rules()
    cands = _chrule_candidates()
    lines = ["📡 **Правила каналов**", ""]
    if rules:
        lines.append("Посты этих каналов сразу кладутся в свою папку:")
        for ch, cat in sorted(rules.items()):
            lines.append(f"• {ch} → {theme_icon(cat, '📦')} {cat}")
    else:
        lines.append("Правил пока нет. Бот предложит правило сам, когда ты 3 раза перенесёшь "
                     "посты одного канала в одну папку.")
    rows = []
    for ch, cat in sorted(rules.items()):
        rows.append([Button.inline(f"❌ {_fmt_short(ch, 22)} → {_fmt_short(cat, 16)}", data=f"chr_d:{_chrule_token(ch, cat)}")])
    if cands:
        lines += ["", "💡 По архиву эти каналы почти всегда в одной папке — можно закрепить:"]
        for ch, cat, n in cands:
            lines.append(f"• {ch} → {cat} ({n} постов)")
            rows.append([Button.inline(f"＋ {_fmt_short(ch, 22)} → {_fmt_short(cat, 16)}", data=f"chr_y:{_chrule_token(ch, cat)}")])
    rows.append([Button.inline("🏷 Правила тем", data="trules"), Button.inline("◀️ Назад", data="service")])
    try:
        await event.edit("\n".join(lines), buttons=rows)
    except Exception:
        await event.respond("\n".join(lines), buttons=rows)


async def handle_chrule_callback(event, data_s: str) -> bool:
    if data_s == "chrules":
        await show_channel_rules(event)
        return True
    if not data_s.startswith(("chr_y:", "chr_n:", "chr_d:")):
        return False
    kind, tok = data_s.split(":", 1)
    offer = _chrule_offers.get(tok)
    if not offer:
        await event.answer("Устарело — открой «🛠 Сервис → 📡 Правила каналов»", alert=True)
        return True
    ch, cat = offer
    rules = _channel_rules()
    if kind == "chr_y":
        rules[ch] = cat
        _channel_rules_set(rules)
        await event.answer(f"Готово: «{ch}» → «{cat}»")
    elif kind == "chr_n":
        try:
            votes = json.loads(db.get_setting("chrule_votes", "{}") or "{}")
        except Exception:
            votes = {}
        votes.setdefault(ch, {})[cat] = -1
        db.set_setting("chrule_votes", json.dumps(votes, ensure_ascii=False))
        try:
            await event.edit("👌 Хорошо, не буду.")
        except Exception:
            pass
        await event.answer()
        return True
    else:
        rules.pop(ch, None)
        _channel_rules_set(rules)
        await event.answer("Правило убрано")
    # нажали на экране правил — перерисуем его; в предложении из чата — заменим текст
    try:
        msg = await event.get_message()
    except Exception:
        msg = None
    if msg is not None and "Правила каналов" in (msg.text or ""):
        await show_channel_rules(event)
    else:
        try:
            await event.edit(f"✅ «{ch}» теперь всегда кладу в «{cat}».")
        except Exception:
            pass
    return True


# ── Похожие картинки ─────────────────────────────────────────────────────────
def _similar_note(item_id: int, phash: str | None) -> tuple[str, list]:
    """Запоминает отпечаток и возвращает (заметка, ряд кнопок), если похожая уже есть."""
    if not phash:
        return "", []
    try:
        known = db.image_hashes()
        db.set_image_hash(item_id, phash)
    except Exception:
        logger.warning("Отпечаток картинки не сохранён", exc_info=True)
        return "", []
    if db.get_setting("dedup_check", "1") != "1":
        return "", []
    sim = imghash.find_similar(phash, known, exclude=item_id, limit=1)
    if not sim:
        return "", []
    other = db.get_item(sim[0][0])
    if not other:
        return "", []
    note = (f"\n\n🖼 Похоже, эта картинка уже есть: «{_fmt_short(other.get('summary') or '', 60)}» "
            f"в «{other['category']}» (#{other['id']}). Может быть тот же мем или тот же шаблон.")
    return note, [Button.inline(f"👀 Похожая #{other['id']}", data=f"view:{other['id']}"),
                  Button.inline("🗑 Этот не нужен", data=f"del:{item_id}")]


def _similar_pairs() -> list[tuple[int, int, int]]:
    """Пары похожих картинок в архиве: [(id1, id2, бит отличия)], самые похожие первыми."""
    hs = db.image_hashes()
    out = []
    for i in range(len(hs)):
        for j in range(i + 1, len(hs)):
            d = imghash.distance(hs[i][1], hs[j][1])
            if d is not None and d <= imghash.SIMILAR_MAX_BITS:
                out.append((hs[i][0], hs[j][0], d))
    return sorted(out, key=lambda x: x[2])


def _icon_targets() -> list[dict]:
    """Категории и папки без иконки (ни ручной, ни от ИИ)."""
    have = {**_ai_icons(), **db.get_icon_overrides()}
    samples = db.category_samples(2)
    out, seen = [], set()
    names = [c["category"] for c in db.get_categories()]
    with db.get_connection() as conn:
        names += [r[0] for r in conn.execute("SELECT name FROM categories").fetchall()]
    for n in names:
        if n and n not in have and n not in seen:
            seen.add(n)
            out.append({"name": n, "kind": "категория", "samples": samples.get(n, []),
                        "current": _kb_theme_icon(n, "")})
    for path in db.get_folder_paths():
        if path not in have and path not in seen:
            seen.add(path)
            inside = [c["category"] for c in db.get_categories() if (c["group"] or "").strip("/") == path][:5]
            out.append({"name": path, "kind": "папка", "samples": ["внутри: " + ", ".join(inside)] if inside else [],
                        "current": _kb_theme_icon(path.rpartition("/")[2], "")})
    return out


async def _icons_for_new(names: list[str] | None = None) -> int:
    """Подбирает ИИ-иконки тем, у кого их нет (или только names). Возвращает, сколько подобрано."""
    todo = _icon_targets()
    if names is not None:
        todo = [t for t in todo if t["name"] in names]
    if not todo:
        return 0
    got = 0
    for k in range(0, len(todo), 30):
        part = todo[k:k + 30]
        res = await _cat.suggest_icons(part)
        if res is None:
            break
        icons = _ai_icons()
        for t in part:
            ic = res.get(t["name"])
            if ic:
                icons[t["name"]] = ic
                if t["kind"] == "папка" and "/" in t["name"]:
                    icons.setdefault(t["name"].rpartition("/")[2], ic)
                got += 1
        db.set_setting("ai_icons", json.dumps(icons, ensure_ascii=False))
    _refresh_icon_overrides()
    return got


async def structure_migrations_loop():
    """Разовые правки структуры после обновления (флаг в настройках, переживает бэкап)."""
    await asyncio.sleep(90)   # после восстановления из GitHub
    for uid in db.all_users():
        prev = db.current_user_id()
        try:
            db.set_current_user(uid)
            if db.get_setting("migr_flatten_double", "") != "1":
                done = flatten_double_folders()
                db.set_setting("migr_flatten_double", "1")
                if done:
                    logger.info("Двойные папки убраны (user=%s): %s", uid, done)
        except Exception as e:
            logger.warning("Правка структуры (user=%s): %s", uid, e)
        finally:
            db.set_current_user(prev)


async def icon_backfill_loop():
    """Раз в несколько часов подбирает ИИ-иконки новым категориям и папкам (≈1 запрос на 30 штук)."""
    await asyncio.sleep(180)
    while True:
        for uid in db.all_users():
            prev = db.current_user_id()
            try:
                db.set_current_user(uid)
                n = await _icons_for_new()
                if n:
                    logger.info("ИИ-иконки (user=%s): подобрано %s", uid, n)
            except Exception as e:
                logger.warning("ИИ-иконки (user=%s): %s", uid, e)
            finally:
                db.set_current_user(prev)
                _refresh_icon_overrides()
        await asyncio.sleep(3 * 3600)


async def hash_backfill_loop():
    """Считает отпечатки для старых фото/видео (качает только превью ~320px, без ИИ).
    Медленно, порциями — чтобы не дёргать Telegram."""
    await asyncio.sleep(120)
    while True:
        for uid in db.all_users():
            prev = db.current_user_id()
            try:
                db.set_current_user(uid)
                todo = db.items_missing_hash(40)
                by_chat: dict[int, list[dict]] = {}
                for it in todo:
                    by_chat.setdefault(it["chat_id"] or uid, []).append(it)
                done = 0
                for chat_id, its in by_chat.items():
                    try:
                        msgs = await client.get_messages(chat_id, ids=[i["message_id"] for i in its])
                    except FloodWaitError as e:
                        await asyncio.sleep(e.seconds + 1)
                        continue
                    except Exception as e:
                        logger.info("Отпечатки: не получил сообщения чата %s: %s", chat_id, e)
                        msgs = [None] * len(its)
                    for it, m in zip(its, msgs if isinstance(msgs, list) else [msgs]):
                        db.set_current_user(uid)
                        try:
                            h = await imghash.message_hash(client, m) if m is not None else None
                        except FloodWaitError as e:
                            await asyncio.sleep(e.seconds + 1)
                            h = None
                        db.set_image_hash(it["id"], h or "-")
                        done += 1
                        await asyncio.sleep(1.0)
                if done:
                    logger.info("Отпечатки картинок (user=%s): +%s", uid, done)
            except Exception as e:
                logger.warning("Отпечатки картинок (user=%s): %s", uid, e)
            finally:
                db.set_current_user(prev)
        await asyncio.sleep(300)


async def _recat_one(item_id: int) -> str:
    """ok | locked | gone | nodata | busy | daily — что вышло с одним постом."""
    item = db.get_item(item_id)
    if not item:
        return "gone"
    if item["locked"]:
        return "locked"
    model, fallback = _recat_models()
    if not model:
        return "budget"
    # картинки описывает qwen; если её запас на исходе — Cloudflare (с суточным потолком),
    # а если и его нет — пост откладываем в конец, очередь идёт дальше по текстовым
    qwen_ok = _recat_has_budget(GROQ_VISION_MODEL)
    cf_ok = not qwen_ok and _cat.cf_bg_vision_ok()
    rep: dict = {"no_refetch": not (qwen_ok or cf_ok)}
    _cat.VISION_DIM.set(RECAT_VISION_DIM)  # действует только внутри фоновой задачи
    _cat.VISION_VIA_CF.set(cf_ok)
    txt = await _recat_text(item, report=rep)
    if rep.get("need_refetch"):
        return "need_vision"
    if not txt:
        return "nodata"
    if rep.get("media_failed"):
        return "daily" if _cat.is_daily_limit(_cat.LLM_LAST_ERROR) else "busy"
    result = await _ai_categorize(
        txt, item["content_type"], item["source_channel"], exclude_id=item_id,
        model=model, fallback_model=fallback,
    )
    if result is None:
        return "daily" if _cat.is_daily_limit(_cat.LLM_LAST_ERROR) else "busy"
    fresh = db.get_item(item_id)
    if not fresh:
        return "gone"
    if fresh["locked"]:
        return "locked"
    db.update_item_category(item_id, result["category"], result["summary"])
    if result.get("topics") is not None:
        db.set_item_topics({item_id: result.get("topics") or []})
    return "ok"


async def _recat_finish(uid: int, job: dict) -> None:
    db.set_current_user(uid)
    _recat_job_set(None)
    notes = []
    if job.get("locked"):
        notes.append(f"{job['locked']} 🔒")
    nodata = job.get("nodata") or []
    if nodata:
        notes.append(f"{len(nodata)} без данных")
    if job.get("failed"):
        notes.append(f"{len(job['failed'])} не удалось (оставил как были)")
    suffix = f" (пропущено: {', '.join(notes)})" if notes else ""
    left = len(job.get("ids") or []) + len(job.get("vision_wait") or [])
    if job.get("night"):
        if not job.get("done"):
            logger.info("Ночной добор (user=%s): ничего не обновлено", uid)
            return
        waiting = len(job.get("vision_wait") or [])
        why = (f" ({waiting} из них — картинки и видео: на их разбор у бесплатного ИИ кончился запас)"
               if waiting else "")
        more = f"\nОсталось {left}{why} — продолжу следующей ночью." if left else ""
        text = f"🌙 За ночь дораспознал {job['done']} {_plural_posts(job['done'])}, где ИИ раньше гадал вслепую{suffix}.{more}"
    else:
        head = "⏹ Остановлено" if job.get("stop") else "✅ Готово"
        tail = f"\nНе тронуто: {left}." if left else ""
        text = f"{head}: перераспознано {job.get('done', 0)} из {job.get('total', 0)}{suffix} ({job.get('title') or ''}){tail}"
    btns = None
    chat_id = job.get("chat_id") or uid
    if nodata:
        nodata_batch[chat_id] = nodata
        btns = [[Button.inline(f"🗑 Убрать {len(nodata)} пустых в корзину", data="purge_nodata")]]
    try:
        if not job.get("msg_id"):
            raise LookupError
        await _MsgProxy(chat_id, job["msg_id"]).edit(text, buttons=btns)
    except Exception:
        try:
            await client.send_message(chat_id, text, buttons=btns or main_keyboard(), silent=bool(job.get("night")))
        except Exception:
            pass
    logger.info("Массовое перераспознавание (user=%s) завершено: %s", uid, text)


async def _recat_worker(uid: int) -> None:
    _cat.USAGE_SOURCE.set("bg")
    gap = RECAT_GAP
    last_edit = 0.0
    last_text = ""
    try:
        while True:
            db.set_current_user(uid)
            job = _recat_job_get()
            if not job:
                return
            if job.get("night") and not _is_night():
                job["stop"] = True
            if job.get("stop") or not (job.get("ids") or job.get("vision_wait")):
                await _recat_finish(uid, job)
                return
            wait = float(job.get("paused_until") or 0) - time.time()
            if wait > 0:
                await _recat_nap(uid, min(wait, 60))
                continue
            if not job.get("ids"):
                # остались только посты, которым нужно зрение ИИ
                if _recat_has_budget(GROQ_VISION_MODEL) or _cat.cf_bg_vision_ok():
                    job["ids"] = job.pop("vision_wait")
                else:
                    job["paused_until"] = time.time() + 30 * 60
                    job["pause_daily"] = True
                _recat_job_set(job)
                continue
            item_id = job["ids"][0]
            try:
                outcome = await _recat_one(item_id)
            except Exception:
                logger.exception("Перераспознавание item=%s упало", item_id)
                outcome = "error"
            db.set_current_user(uid)
            job = _recat_job_get()  # перечитываем: могли нажать «Остановить» или добавить посты
            if not job:
                return
            ids = job.get("ids") or []
            tries = job.setdefault("tries", {})
            key = str(item_id)
            job["pause_daily"] = False
            if outcome == "need_vision":
                # без зрения ИИ этот пост не разобрать — в конец, очередь не стоит
                if item_id in ids:
                    ids.remove(item_id)
                job.setdefault("vision_wait", []).append(item_id)
                tries.pop(key, None)
            elif outcome == "budget":
                # бережём суточный запас: ждём, пока скользящие сутки освободят токены
                job["paused_until"] = time.time() + 30 * 60
                job["pause_daily"] = True
            elif outcome in ("busy", "daily", "error"):
                tries[key] = tries.get(key, 0) + 1
                if outcome == "daily":
                    hint = _cat.retry_after_seconds(_cat.LLM_LAST_ERROR) or 0
                    job["paused_until"] = time.time() + min(max(hint + 30, 15 * 60), 3 * 3600)
                    job["pause_daily"] = True
                    tries[key] -= 1  # суточный лимит — не вина поста
                elif outcome == "busy":
                    gap = min(gap * 2, RECAT_GAP_MAX)
                    job["paused_until"] = time.time() + gap
                if tries[key] >= RECAT_MAX_TRIES or (outcome == "error" and tries[key] >= 2):
                    if item_id in ids:
                        ids.remove(item_id)
                    job.setdefault("failed", []).append(item_id)
                    tries.pop(key, None)
                    if job.get("smart"):
                        _recat_mark_seen(item_id)
            else:
                if item_id in ids:
                    ids.remove(item_id)
                tries.pop(key, None)
                if job.get("smart") and outcome != "gone":
                    _recat_mark_seen(item_id)
                if outcome == "ok":
                    job["done"] = job.get("done", 0) + 1
                elif outcome == "locked":
                    job["locked"] = job.get("locked", 0) + 1
                elif outcome == "nodata":
                    job.setdefault("nodata", []).append(item_id)
                gap = max(RECAT_GAP, gap * 0.7)
            _recat_job_set(job)
            text = _recat_progress_text(job)
            paused = float(job.get("paused_until") or 0) > time.time()
            if job.get("msg_id") and text != last_text and (time.time() - last_edit > 15 or paused):
                try:
                    await _MsgProxy(job.get("chat_id") or uid, job["msg_id"]).edit(text, buttons=_recat_stop_buttons())
                    last_edit, last_text = time.time(), text
                except FloodWaitError as e:
                    last_edit = time.time() + e.seconds
                except Exception:
                    pass
            if not paused:
                await _recat_nap(uid, gap)
    finally:
        _recat_tasks.pop(uid, None)


def _ensure_recat_worker(uid: int) -> None:
    t = _recat_tasks.get(uid)
    if t and not t.done():
        return
    _recat_tasks[uid] = asyncio.create_task(_recat_worker(uid))


async def recat_resume_loop():
    """После перезапуска Render продолжает недоделанное массовое перераспознавание."""
    await asyncio.sleep(30)
    while True:
        for uid in db.all_users():
            prev = db.current_user_id()
            try:
                db.set_current_user(uid)
                if _recat_job_get():
                    _ensure_recat_worker(uid)
            except Exception as e:
                logger.warning("Возобновление перераспознавания (user=%s): %s", uid, e)
            finally:
                db.set_current_user(prev)
        await asyncio.sleep(300)


async def _recat_stop(event) -> None:
    job = _recat_job_get()
    if not job:
        await event.answer("Уже не идёт")
        return
    job["stop"] = True
    job["paused_until"] = 0
    _recat_job_set(job)
    uid = db.current_user_id() or event.sender_id
    _recat_wake.setdefault(uid, asyncio.Event()).set()
    _ensure_recat_worker(uid)
    await event.answer("Останавливаю…")


async def run_bulk_recat(event, ids: list[int], title: str = "", smart: bool = False) -> int:
    """Ставит посты в фоновую очередь перераспознавания (см. _recat_worker)."""
    uid = db.current_user_id() or event.sender_id
    ids = list(dict.fromkeys(ids))
    job = _recat_job_get()
    if job and job.get("night") and not job.get("stop"):
        known = set(job["ids"])
        job["ids"] = [i for i in ids if i not in known] + job["ids"]
        job["total"] = job.get("done", 0) + job.get("locked", 0) + len(job.get("nodata") or []) \
            + len(job.get("failed") or []) + len(job["ids"])
        job.update({"night": False, "title": title, "chat_id": event.chat_id, "smart": smart and job.get("smart")})
        progress = await event.respond(_recat_progress_text(job), buttons=_recat_stop_buttons())
        job["msg_id"] = progress.id
        _recat_job_set(job)
        _ensure_recat_worker(uid)
        try:
            await event.answer("Запустил в фоне")
        except Exception:
            pass
        return 0
    if job and job.get("ids") and not job.get("stop"):
        known = set(job["ids"])
        add = [i for i in ids if i not in known]
        job["ids"].extend(add)
        job["total"] = job.get("total", 0) + len(add)
        _recat_job_set(job)
        _ensure_recat_worker(uid)
        try:
            await event.answer(f"Уже перераспознаю — добавил ещё {len(add)} в очередь", alert=True)
        except Exception:
            pass
        return 0
    job = {
        "ids": ids, "total": len(ids), "done": 0, "locked": 0,
        "nodata": [], "failed": [], "tries": {}, "title": title,
        "chat_id": event.chat_id, "msg_id": None, "paused_until": 0, "smart": smart,
    }
    progress = await event.respond(_recat_progress_text(job), buttons=_recat_stop_buttons())
    job["msg_id"] = progress.id
    _recat_job_set(job)
    _ensure_recat_worker(uid)
    try:
        await event.answer("Запустил в фоне")
    except Exception:
        pass
    return 0


async def show_category_page(event, category: str, page: int):
    total = db.get_total_items(category)
    total_pages = max(1, (total + ITEMS_PER_PAGE - 1) // ITEMS_PER_PAGE)
    page = max(0, min(page, total_pages - 1))

    items = db.get_items_by_category(category, limit=ITEMS_PER_PAGE, offset=page * ITEMS_PER_PAGE)
    if not items:
        await _nav_send(event, f"📁 Папка «{category}» пока пустая. Перешли сюда посты, перенеси их из других "
                               "папок или попроси помощника: «перенеси … в " + category + "».",
                        buttons=[[Button.inline("◀️ К папкам", data="back_to_cats")]])
        return

    for it in items:
        it["tags"] = db.get_item_tags(it["id"])
    _mark_new(items)
    cat_row = db.get_category_row(category)
    cat_locked = bool(cat_row["locked"]) if cat_row else False
    folder_path = (cat_row["group"] or "").strip("/") if cat_row else ""
    tags = _tags_line(db.get_category_tags(category))
    emoji = theme_icon(category, "📦")
    lock = " 🔒" if cat_locked else ""
    header = f"{tags}📁 {emoji} {category}{lock}  — постов: {total}" + (f" (стр. {page + 1}/{total_pages})" if total_pages > 1 else "")
    await _nav_send(event, header, buttons=items_keyboard(items, category, page, total_pages, category_locked=cat_locked, folder_path=folder_path))


async def watchdog():
    """Следит за соединением: если обновления перестали доходить — переподнимает связь."""
    while True:
        await asyncio.sleep(60)
        try:
            if not client.is_connected():
                logger.warning("Watchdog: соединение потеряно, завершаю процесс (перезапустит планировщик)")
                try:
                    await client.disconnect()
                except Exception:
                    pass
                raise SystemExit(3)
            await asyncio.wait_for(client.get_me(), timeout=15)
        except asyncio.TimeoutError:
            logger.warning("Watchdog: Telegram молчит >15с, завершаю процесс (перезапустит планировщик)")
            try:
                await client.disconnect()
            except Exception:
                pass
            raise SystemExit(3)
        except Exception as e:
            logger.warning("Watchdog: ошибка %s, завершаю процесс (перезапустит планировщик)", e)
            try:
                await client.disconnect()
            except Exception:
                pass
            raise SystemExit(3)


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
        raw = open(SEED_FILE, "rb").read()
        res = db.import_json_stream(raw)
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

    _API_CHAT_SEM = asyncio.Semaphore(4)
    _CORS_HEADERS = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-Sec-Token",
    }

    async def handler_options(_request):
        return web.Response(status=204, headers=_CORS_HEADERS)

    async def handler_api_limits(request):
        """Запас бесплатного ИИ для расширения: сколько дневных токенов Groq осталось у
        основной и запасной модели и доступен ли Cloudflare. Оценка: ведро считается в
        памяти бота и уточняется по ответам Groq с «Used N»; после перезапуска — «полное»."""
        token = request.headers.get("X-Sec-Token", "")
        if not EXT_SECRET or not hmac.compare_digest(token, EXT_SECRET):
            return web.json_response({"error": "forbidden"}, status=403, headers=_CORS_HEADERS)
        main_model = str(request.query.get("model") or GROQ_MODEL).strip()
        return web.json_response(_cat.ext_limits(main_model), headers=_CORS_HEADERS)

    async def handler_api_chat(request):
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"error": "json body expected"}, status=400, headers=_CORS_HEADERS)

        token = request.headers.get("X-Sec-Token", "")
        if not EXT_SECRET or not hmac.compare_digest(token, EXT_SECRET):
            return web.json_response({"error": "forbidden"}, status=403, headers=_CORS_HEADERS)

        messages = body.get("messages")
        if not isinstance(messages, list) or not messages:
            return web.json_response({"error": "messages list required"}, status=400, headers=_CORS_HEADERS)
        if len(messages) > 50:
            return web.json_response({"error": "too many messages"}, status=400, headers=_CORS_HEADERS)

        model = str(body.get("model") or GROQ_MODEL).strip()
        if EXT_MODELS_ALLOW and model not in EXT_MODELS_ALLOW:
            return web.json_response({"error": "model not allowed"}, status=400, headers=_CORS_HEADERS)

        try:
            max_tokens = int(body.get("max_tokens", 800))
        except (TypeError, ValueError):
            max_tokens = 800
        max_tokens = min(max(1, max_tokens), 2000)

        try:
            temperature = float(body.get("temperature", 0.1))
        except (TypeError, ValueError):
            temperature = 0.1
        temperature = min(max(temperature, 0.0), 1.0)

        is_stream = bool(body.get("stream", False))
        if is_stream:
            from categorizer import llm_chat_stream

            async def _sse():
                import categorizer as _cat
                sent_any = False
                try:
                    async with _API_CHAT_SEM:
                        async for delta in llm_chat_stream(
                            messages,
                            model=model,
                            json_mode=bool(body.get("json_mode", False)),
                            temperature=temperature,
                            max_tokens=max_tokens,
                            timeout=300,
                        ):
                            sent_any = True
                            yield f"data: {json.dumps({'delta': delta}, ensure_ascii=False)}\n\n".encode("utf-8")
                    if not sent_any:
                        err = getattr(_cat, "LLM_LAST_ERROR", "")
                        payload = {"error": "Модель Groq не ответила", "code": "llm_no_reply"}
                        if err:
                            payload["detail"] = err
                        yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")
                except Exception as e:
                    # Реальное исключение ВАЖНЕЕ глобальной LLM_LAST_ERROR: её мог
                    # затереть параллельный запрос бота, и клиент получал ЧУЖУЮ
                    # (часто протухшую) причину отказа вместо своей — вплоть до
                    # «превышен лимит токенов» там, где лимита не было вовсе.
                    detail = str(e) or getattr(_cat, "LLM_LAST_ERROR", "")
                    payload = {"error": "Модель Groq не ответила", "code": "llm_no_reply"}
                    if detail:
                        payload["detail"] = detail
                    logger.warning("LLM stream сбой: %s", detail[:400])
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n".encode("utf-8")
                yield b"data: [DONE]\n\n"

            resp = web.StreamResponse(
                status=200,
                headers={
                    **_CORS_HEADERS,
                    "Content-Type": "text/event-stream; charset=utf-8",
                    "Cache-Control": "no-cache",
                },
            )
            await resp.prepare(request)
            async for chunk in _sse():
                try:
                    await resp.write(chunk)
                except Exception:
                    break
            try:
                await resp.write_eof()
            except Exception:
                pass
            return resp

        async with _API_CHAT_SEM:
            content = await llm_chat(
                messages,
                model=model,
                json_mode=bool(body.get("json_mode", False)),
                temperature=temperature,
                max_tokens=max_tokens,
                timeout=300,
            )
        if not content:
            import categorizer as _cat
            err = getattr(_cat, "LLM_LAST_ERROR", "")
            payload = {
                "error": "Модель Groq не ответила",
                "code": "llm_no_reply",
            }
            if err:
                payload["detail"] = err
            return web.json_response(payload, status=502, headers=_CORS_HEADERS)
        return web.json_response({"content": content}, headers=_CORS_HEADERS)

    app = web.Application()
    app.router.add_get("/healthz", handler_ready)
    app.router.add_get("/", handler_health)
    app.router.add_post("/api/chat", handler_api_chat)
    app.router.add_get("/api/limits", handler_api_limits)
    app.router.add_options("/api/limits", handler_options)
    app.router.add_options("/api/chat", handler_options)
    app.router.add_get("/api/download", handler_api_download)
    app.router.add_options("/api/download", handler_options)
    app.router.add_post("/api/download-cookies", handler_api_download_cookies)
    app.router.add_options("/api/download-cookies", handler_options)
    app.router.add_post("/api/save-video", handler_api_save_video)
    app.router.add_options("/api/save-video", handler_options)
    app.router.add_post("/api/save-links", handler_api_save_links)
    app.router.add_options("/api/save-links", handler_options)
    app.router.add_post("/api/backup", handler_api_backup)
    app.router.add_options("/api/backup", handler_options)
    app.router.add_post("/api/yq-sync", handler_api_yq_sync)
    app.router.add_options("/api/yq-sync", handler_options)
    app.router.add_post("/api/weekly-report", handler_api_weekly_report)
    app.router.add_options("/api/weekly-report", handler_options)
    ytqueue.register(app, owner=_cached_owner, check_init_data=webarchive.check_init_data,
                     check_token=webarchive.check_token, activate=_activate_user,
                     on_change=_yq_backup_soon)
    share_summary.setup(app, activate_owner=lambda: _activate_user(_cached_owner()), get_secret=lambda: EXT_SECRET)
    webarchive.setup(app, get_client=lambda: client, activate_user=_activate_user,
                     get_icons=lambda: dict(_icon_overrides), smart_search=smart_search,
                     get_bot_username=lambda: BOT_USERNAME, send_item=send_item_to_chat,
                     get_unseen=unseen_ids, mark_seen=mark_seen, get_share=get_share, get_dead=dead_links,
                     get_mirror=lambda iid: (BACKUP_CHANNEL_ID, mirror_ids(iid)),
                     edit={"move": web_move, "rename": web_rename, "trash": web_trash, "remind": web_remind,
                           "unlock": web_unlock, "brief": article_brief_for, "similar": similar_items,
                           "add": web_add, "aistats": _cat.usage_snapshot,
                           "pv_unlock": web_private_unlock, "translate": translate_item,
                           "note": web_set_note, "cleanup": web_cleanup, "cleanup_keep": cleanup_keep_add},
                     bridge=web_bridge, voice=web_voice, image_search=web_image_search)
    runner = web.AppRunner(app)
    try:
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", HTTP_PORT)
        await site.start()
        logger.info("HTTP health-сервер запущен на порту %s", HTTP_PORT)
    except Exception as e:
        logger.warning("Не удалось поднять HTTP-сервер: %s", e)


# ---------------------------- Видео YouTube из расширения ----------------------------
# POST /api/save-video (X-Sec-Token): расширение «Сводка комментариев YouTube» кладёт
# видео в архив владельца. Бот сам присылает в чат обложку с подписью и сохраняет её
# тем же _save(), что и пересланный пост: категория от ИИ, папки, кнопки, бэкап.

SAVE_VIDEO_RATE = (20, 600)          # не больше 20 новых видео за 10 минут
_save_video_times: list[float] = []
_save_video_locks: dict = {}         # loop → asyncio.Lock: сохраняем по одному (дубли, TPM Groq)


def _save_video_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    lock = _save_video_locks.get(loop)
    if lock is None:
        _save_video_locks.clear()
        lock = _save_video_locks[loop] = asyncio.Lock()
    return lock


async def save_video(v: dict) -> tuple[int, dict]:
    """v — из ytvideo.parse_request(). Возвращает (HTTP-статус, ответ)."""
    owner = _cached_owner()
    if not owner:
        return 503, {"error": "Бот не знает владельца архива (OWNER_ID)", "code": "no_owner"}
    if client is None or not getattr(client, "is_connected", lambda: False)():
        return 503, {"error": "Бот сейчас не подключён к Telegram", "code": "tg_offline"}
    async with _save_video_lock():
        _activate_user(owner)
        dup = db.find_by_text_fragments(ytvideo.link_fragments(v["video_id"]))
        if dup:
            return 200, {"ok": True, "status": "exists", "id": dup["id"], "category": dup["category"],
                         "icon": theme_icon(dup["category"], "📦")}
        now = time.time()
        _save_video_times[:] = [t for t in _save_video_times if now - t < SAVE_VIDEO_RATE[1]]
        if len(_save_video_times) >= SAVE_VIDEO_RATE[0]:
            return 429, {"error": "Слишком много видео подряд", "code": "rate"}
        _save_video_times.append(now)
        _cat.USAGE_SOURCE.set("ext")   # в /limits — строкой «расширение YouTube»

        chat_id = owner
        post = None
        thumb = await ytvideo.fetch_thumb(v["video_id"])
        try:
            if thumb:
                import io
                f = io.BytesIO(thumb)
                f.name = "cover.jpg"
                try:
                    post = await client.send_file(chat_id, f, caption=ytvideo.post_text(v, ytvideo.CAPTION_MAX),
                                                  parse_mode=None)
                except Exception:
                    logger.warning("Видео %s: обложка не отправилась — шлю текстом", v["video_id"], exc_info=True)
            if post is None:
                post = await client.send_message(chat_id, ytvideo.post_text(v, 4000), parse_mode=None,
                                                 link_preview=True)
            processing = await client.send_message(chat_id, "⏳ Сохраняю видео…", reply_to=post.id)
        except Exception:
            logger.warning("Видео %s: не отправилось в Telegram", v["video_id"], exc_info=True)
            return 502, {"error": "Не получилось отправить в Telegram", "code": "tg_send"}

        media = _msg_media(post)
        res = await _save(
            client,
            chat_id=chat_id,
            processing=processing,
            content_type=_primary_content_type(media),
            analysis={"display": ytvideo.post_text(v), "llm_text": ytvideo.llm_text(v), "aux": ""},
            file_ids=media,
            message_id=post.id,
            source_channel=v["channel"] or None,
            default_category="Видео",
        )
    return 200, {"ok": True, "status": "saved", "id": res["id"], "category": res["category"],
                 "icon": theme_icon(res["category"], "📦"), "queued": bool(res["failed"])}


async def handler_api_save_video(request):
    from aiohttp import web

    cors = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-Sec-Token",
    }

    def reply(payload, status=200):
        return web.json_response(payload, status=status, headers=cors)

    token = request.headers.get("X-Sec-Token", "")
    if not EXT_SECRET or not hmac.compare_digest(token.encode("utf-8"), EXT_SECRET.encode("utf-8")):
        return reply({"error": "forbidden", "code": "forbidden"}, 403)
    if (request.content_length or 0) > ytvideo.MAX_BODY:
        return reply({"error": "body too large", "code": "too_large"}, 413)
    raw = await ytvideo.read_limited(request.content, ytvideo.MAX_BODY)
    if raw is None:
        return reply({"error": "body too large", "code": "too_large"}, 413)
    try:
        v = ytvideo.parse_request(json.loads(raw.decode("utf-8")))
    except ytvideo.BadRequest as e:
        return reply({"error": str(e), "code": "bad_request"}, 400)
    except Exception:
        return reply({"error": "json body expected", "code": "bad_request"}, 400)
    try:
        status, payload = await save_video(v)
    except Exception:
        logger.warning("save-video: сбой", exc_info=True)
        return reply({"error": "Сбой в боте", "code": "internal"}, 500)
    logger.info("save-video %s → %s %s", v["video_id"], status, payload.get("status") or payload.get("code"))
    return reply(payload, status)


# ---------------------------- Ссылки из «Сборщика вкладок» ----------------------------
# POST /api/save-links (X-Sec-Token): расширение «Сборщик вкладок» кладёт ссылки в архив.
# mode=each — каждая ссылка своим постом (ИИ раскладывает по папкам, как пересланный пост);
# mode=list — один пост-список (длинный — несколько сообщений, каждая часть — свой пост
# архива с той же категорией, чтобы поиск по ссылке вёл к нужной части).
# Лимит — на НОВЫЕ посты (каждый = вызов ИИ); повторы и ошибки его не тратят.

SAVE_LINKS_RATE = (30, 600)          # не больше 30 новых постов за 10 минут (~3/мин — в TPM Groq)
SAVE_LINKS_DAY = (150, 86400)        # и 150 за сутки — Groq free ≈ 90–180 постов/сутки на модель
_save_links_times: list[float] = []
SAVE_LINKS_RESEND_PAUSE = 2.0       # пауза перед повтором части списка, которую Telegram не принял
# Повтор поста-списка (браузер упал после ответа бота и прислал тот же запрос ещё раз):
# с batchKey — тот же ключ и те же ссылки за сутки; без ключа — тот же заголовок и набор
# ссылок за 10 минут. Отвечаем прежним результатом со status "exists", без нового поста.
# Помним в settings архива владельца (переживает перезапуск), не больше 50 последних.
SAVE_LINKS_BATCH_TTL = 86400
SAVE_LINKS_SAME_LIST_TTL = 600
_LIST_SEEN_KEY = "ext_list_seen"
_LIST_SEEN_MAX = 50


def _list_seen(now: float) -> list[dict]:
    try:
        rows = json.loads(db.get_setting(_LIST_SEEN_KEY, "") or "[]")
    except Exception:
        rows = []
    out = []
    for r in rows if isinstance(rows, list) else []:
        try:
            if isinstance(r, dict) and now - float(r.get("t") or 0) < SAVE_LINKS_BATCH_TTL:
                out.append(r)
        except (TypeError, ValueError):
            pass
    return out


def _list_part_alive(item_id, msg_id, url) -> dict | None:
    """Пост части списка, если он всё ещё тот же: id жив, сообщение то же и первая ссылка
    части на месте (страховка от id, занятого заново после восстановления архива)."""
    if not isinstance(item_id, int):
        return None
    item = db.get_item(item_id)
    if not item or item.get("message_id") != msg_id:
        return None
    key = savelinks.url_key(url) if url else None
    if key and not savelinks.text_has_url(item.get("original_text") or "", key):
        return None
    return item


def _list_repeat(batch_key: str, fp: str, now: float) -> dict | None:
    """Прежний ответ на этот же список, если он уже сохранён целиком (и посты не удалены).
    Решает самая свежая подходящая запись: если тогда часть не легла в архив (unsaved),
    повтор — не повтор, список сохраняется заново."""
    for r in reversed(_list_seen(now)):
        if r.get("f") != fp:
            continue
        if batch_key:
            if r.get("k") != batch_key:
                continue
        elif now - float(r["t"]) >= SAVE_LINKS_SAME_LIST_TTL:
            continue
        if r.get("unsaved"):
            return None
        raw_ids = r.get("ids") or []
        msgs = r.get("m") or [None] * len(raw_ids)
        urls = r.get("u") or [None] * len(raw_ids)
        alive = [it for it in (_list_part_alive(i, m, u) for i, m, u in zip(raw_ids, msgs, urls)) if it]
        if not alive:
            return None  # посты списка удалены — сохраняем заново
        ids = [it["id"] for it in alive]
        category = alive[0].get("category") or r.get("category") or ""
        # parts — сколько постов списка живо сейчас (= len(ids)); удалённые части не считаем
        payload = {"ok": True, "mode": "list", "status": "exists", "ids": ids, "category": category,
                   "icon": theme_icon(category, "📦"), "parts": len(ids),
                   "queued": bool(r.get("queued"))}
        if r.get("unsent"):
            payload["unsent"] = r["unsent"]
        return payload
    return None


def _list_remember(batch_key: str, fp: str, now: float, payload: dict, msgs: list, urls: list) -> None:
    """msgs/urls — сообщение и первая ссылка поста каждой части (в порядке ids)."""
    try:
        rows = _list_seen(now)
        rows.append({"t": now, "k": batch_key, "f": fp, "ids": payload["ids"], "m": msgs, "u": urls,
                     "category": payload["category"], "parts": payload["parts"], "queued": payload["queued"],
                     "unsent": payload.get("unsent", 0), "unsaved": payload.get("unsaved", 0)})
        db.set_setting(_LIST_SEEN_KEY, json.dumps(rows[-_LIST_SEEN_MAX:], ensure_ascii=False))
    except Exception:
        logger.warning("Список ссылок: не запомнил для защиты от повтора", exc_info=True)


def _links_retry_after(now: float) -> int:
    """0 — можно сохранять; иначе сколько секунд ждать до свободного места."""
    _save_links_times[:] = [t for t in _save_links_times if now - t < SAVE_LINKS_DAY[1]]
    waits = []
    for limit, window in (SAVE_LINKS_RATE, SAVE_LINKS_DAY):
        recent = sorted(t for t in _save_links_times if now - t < window)
        if len(recent) >= limit:
            waits.append(recent[len(recent) - limit] + window - now)
    return max(1, int(max(waits)) + 1) if waits else 0


def _links_offline() -> tuple[int, dict] | None:
    if not _cached_owner():
        return 503, {"error": "Бот не знает владельца архива (OWNER_ID)", "code": "no_owner"}
    if client is None or not getattr(client, "is_connected", lambda: False)():
        return 503, {"error": "Бот сейчас не подключён к Telegram", "code": "tg_offline"}
    return None


def _link_found(row: dict) -> dict:
    return {"status": "exists", "id": row["id"], "category": row["category"],
            "icon": theme_icon(row["category"], "📦")}


async def _send_link_post(chat_id: int, it: dict):
    """Пост ссылки в чат: картинка с подписью (если скачалась), иначе текст с превью."""
    post = None
    caption = savelinks.post_text(it, savelinks.CAPTION_MAX) if it["image"] else None
    if caption:
        img = await savelinks.fetch_image(it["image"])
        if img:
            import io
            f = io.BytesIO(img)
            f.name = "cover.jpg"
            try:
                post = await client.send_file(chat_id, f, caption=caption, parse_mode=None)
            except Exception:
                logger.warning("Ссылка: картинка не отправилась — шлю текстом", exc_info=True)
    if post is None:
        post = await client.send_message(chat_id, savelinks.post_text(it), parse_mode=None,
                                         link_preview=True)
    return post


async def _save_one_link(chat_id: int, it: dict) -> dict:
    """Одна ссылка (mode=each) под общим локом. Ответ — элемент results без url."""
    async with _save_video_lock():
        _activate_user(chat_id)
        dup = savelinks.find_saved(it["url"], db.items_with_text_fragments)
        if dup:
            return _link_found(dup)
        now = time.time()
        wait = _links_retry_after(now)
        if wait:
            return {"status": "error", "code": "rate", "retryAfter": wait}
        _save_links_times.append(now)
        _cat.USAGE_SOURCE.set("ext")
        try:
            post = await _send_link_post(chat_id, it)
            processing = await client.send_message(chat_id, "⏳ Сохраняю ссылку…", reply_to=post.id)
        except Exception:
            logger.warning("Ссылка %s: не отправилась в Telegram", it["url"][:200], exc_info=True)
            return {"status": "error", "code": "tg_send"}
        media = _msg_media(post)
        res = await _save(
            client,
            chat_id=chat_id,
            processing=processing,
            content_type=_primary_content_type(media),
            analysis={"display": savelinks.post_text(it), "llm_text": savelinks.llm_text(it), "aux": ""},
            file_ids=media,
            message_id=post.id,
            source_channel=it["site"] or None,
            default_category=savelinks.default_category([it], _existing_category_names()),
        )
    return {"status": "saved", "id": res["id"], "category": res["category"],
            "icon": theme_icon(res["category"], "📦"), "queued": bool(res["failed"])}


async def web_add(uid: int, text: str) -> dict:
    """«➕ Добавить» в приложении. Одна ссылка — как из расширения (заголовок и описание со страницы,
    текст статьи потом заберёт фон); иначе — заметка, как будто написал боту. Пост появляется и в чате."""
    text = (text or "").strip()[:4000]
    if not text:
        return {"status": "error", "error": "Пусто — вставь ссылку или напиши заметку"}
    if client is None or not getattr(client, "is_connected", lambda: False)():
        return {"status": "error", "error": "Бот сейчас не подключён к Telegram — попробуй через минуту"}
    urls = re.findall(r"https?://\S+", text)
    if len(urls) == 1 and len(text) - len(urls[0]) <= 3:
        url = urls[0].rstrip(".,;)")
        meta = await linkmeta.fetch_article(url)
        it = savelinks.parse_item({"url": url, "title": meta.get("title") or "",
                                   "description": (meta.get("text") or "")[:300]})
        if not it:
            return {"status": "error", "error": "Ссылка не похожа на адрес страницы"}
        r = await _save_one_link(uid, it)
        if r.get("status") == "error":
            why = {"rate": "Слишком много подряд — подожди пару минут", "tg_send": "Telegram не принял сообщение"}
            return {"status": "error", "error": why.get(r.get("code"), "Не получилось сохранить")}
        return r
    async with _save_video_lock():
        _activate_user(uid)
        dup = db.find_dup_text(text)
        if dup:
            return {"status": "exists", "id": dup["id"], "category": dup["category"],
                    "icon": theme_icon(dup["category"], "📦")}
        post = await client.send_message(uid, text, link_preview=bool(urls))
        processing = await client.send_message(uid, "⏳ Сохраняю заметку…", reply_to=post.id)
        res = await _save(client, chat_id=uid, processing=processing, content_type="text",
                          analysis={"display": text, "llm_text": text, "aux": ""}, file_ids=[],
                          message_id=post.id, source_channel=None)
    return {"status": "saved", "id": res["id"], "category": res["category"],
            "icon": theme_icon(res["category"], "📦")}


async def save_links_each(req: dict) -> tuple[int, dict]:
    off = _links_offline()
    if off:
        return off
    owner = _cached_owner()
    results, retry_after = [], 0
    for raw_url, it in zip(req["raw_urls"], req["items"]):
        if it is None:
            results.append({"url": raw_url, "status": "error", "code": "bad_url"})
            continue
        try:
            r = await _save_one_link(owner, it)
        except Exception:
            logger.warning("save-links: сбой на %s", it["url"][:200], exc_info=True)
            r = {"status": "error", "code": "internal"}
        if r.get("code") == "rate":
            retry_after = max(retry_after, r.pop("retryAfter"))
            if not any(x["status"] in ("saved", "exists") for x in results):
                # Ничего ещё не сделано — весь запрос «приходите позже».
                return 429, {"error": "Слишком много ссылок подряд", "code": "rate", "retryAfter": retry_after}
        results.append({"url": raw_url, **r})
    payload = {"ok": True, "mode": "each", "results": results}
    if retry_after:
        payload["retryAfter"] = retry_after
    return 200, payload


async def save_links_list(req: dict) -> tuple[int, dict]:
    off = _links_offline()
    if off:
        return off
    chat_id = _cached_owner()
    items = [it for it in req["items"] if it]
    parts = savelinks.list_parts(req["title"], items)
    batch_key = req.get("batch_key") or ""
    # С batchKey заголовок в отпечаток не входит: при повторе расширение может прислать ту же
    # часть под другим номером («Сбор (2/2)» → «Сбор») — это всё равно тот же список.
    fp = savelinks.list_fingerprint("" if batch_key else req["title"], items)
    async with _save_video_lock():
        _activate_user(chat_id)
        now = time.time()
        again = _list_repeat(batch_key, fp, now)
        if again:
            logger.info("Список ссылок: повтор (%s) — прежние посты %s", "batchKey" if batch_key else "те же ссылки",
                        again["ids"])
            return 200, again
        wait = _links_retry_after(now)
        if wait:
            return 429, {"error": "Слишком много ссылок подряд", "code": "rate", "retryAfter": wait}
        _save_links_times.append(now)
        _cat.USAGE_SOURCE.set("ext")
        posts = []                       # сообщение каждой части (None — не отправилось)
        for text in parts:
            post = None
            for attempt in range(2):
                try:
                    post = await client.send_message(chat_id, text, parse_mode=None, link_preview=False)
                    break
                except Exception:
                    logger.warning("Список ссылок: часть не отправилась (попытка %s)", attempt + 1, exc_info=True)
                    await asyncio.sleep(SAVE_LINKS_RESEND_PAUSE)
            posts.append(post)
        if not any(posts):
            return 502, {"error": "Не получилось отправить в Telegram", "code": "tg_send"}
        # Дальше части уже в чате: сбой сохранения не даёт 500 — 200 с честными unsent/unsaved
        # (иначе расширение прислало бы список ещё раз — второй пост в чате). Исключение — часть,
        # которой нет ни в чате, ни в архиве: её ссылки пропали бы, поэтому 500 "partial",
        # чтобы они остались в «Не отправлены». При unsaved повтор сохраняет список заново.
        payload, msgs, urls, lost = await _save_list_parts(chat_id, req["title"], items, parts, posts)
        _list_remember(batch_key, fp, now, payload, msgs, urls)
    if lost:
        logger.warning("Список ссылок: %s частей нет ни в чате, ни в архиве — отвечаю partial", lost)
        return 500, {**payload, "ok": False, "status": "partial", "code": "partial", "lost": lost,
                     "error": "Часть списка не сохранилась — отправьте его ещё раз"}
    return 200, payload


async def _save_list_parts(chat_id: int, title: str, items: list[dict], parts: list[str], posts: list):
    """Части списка (уже отправленные в чат) → посты архива. Не бросает исключений.
    Возвращает (ответ, сообщение и первая ссылка каждого сохранённого поста — в порядке ids,
    сколько частей нет ни в чате, ни в архиве)."""
    total = len(parts)
    first_urls = [savelinks.first_url(text) for text in parts]
    msgs, urls, lost = [], [], 0
    category = fallback = "Ссылки"
    summary, failed = f"Список ссылок: {savelinks.links_word(len(items))}", True
    source = None
    try:
        source = savelinks.list_source(items)
        category = fallback = savelinks.default_category(items, _existing_category_names())
    except Exception:
        logger.warning("Список ссылок: не выбрал запасную категорию", exc_info=True)
    ids, first, unsaved = [], 0, 0
    processing = None
    if posts[0] is not None:
        try:
            processing = await client.send_message(chat_id, "⏳ Сохраняю список ссылок…", reply_to=posts[0].id)
        except Exception:
            logger.warning("Список ссылок: «⏳» не отправилось", exc_info=True)
    if processing is not None:
        try:
            res = await _save(
                client,
                chat_id=chat_id,
                processing=processing,
                content_type="text",
                analysis={"display": parts[0], "llm_text": savelinks.list_llm_text(title, items), "aux": ""},
                file_ids=[],
                message_id=posts[0].id,
                source_channel=source,
                default_category=fallback,
            )
            category, summary, failed = res["category"], res["summary"], bool(res["failed"])
            ids.append(res["id"])
            first = 1
        except Exception:
            logger.warning("Список ссылок: первая часть не сохранилась через ИИ — сохраняю без него", exc_info=True)
            try:
                await processing.delete()      # «⏳ Сохраняю…» не должно висеть вечно
            except Exception:
                pass
            # _save() мог успеть записать пост и упасть позже — не дублируем его
            # (тот же текст и то же сообщение в чате)
            try:
                row = next((r for r in db.items_with_text_fragments([items[0]["url"]], limit=20)
                            if r["original_text"] == parts[0]
                            and (db.get_item(r["id"]) or {}).get("message_id") == posts[0].id), None)
            except Exception:
                row = None
            if row:
                ids.append(row["id"])
                category, first = row["category"], 1
        if first:
            msgs.append(posts[0].id)
            urls.append(first_urls[0])
    # Остальные части — отдельные посты той же категории (без лишних вызовов ИИ):
    # у каждой своё сообщение, «прислать в чат» и поиск по ссылке ведут к нужной части.
    # Часть, которую Telegram так и не принял, всё равно ложится в архив (без
    # сообщения — как пост, чей оригинал удалён), чтобы ни одна ссылка не пропала.
    # Без «⏳» _save() некуда писать итог — тогда и первая часть здесь, без ИИ (дораспознаем позже).
    for k in range(first, total):
        post = posts[k]
        try:
            iid = db.save_item(category=category, content_type="text",
                               summary=f"{summary} (часть {k + 1}/{total})" if total > 1 else summary,
                               original_text=parts[k], file_id=None,
                               message_id=post.id if post is not None else None,
                               chat_id=chat_id, source_channel=source)
            ids.append(iid)
            msgs.append(post.id if post is not None else None)
            urls.append(first_urls[k])
        except Exception:
            unsaved += 1
            if post is None:
                lost += 1
            logger.warning("Список ссылок: часть %s/%s не сохранилась в архив", k + 1, total, exc_info=True)
            continue
        if failed:
            try:
                db.queue_llm_retry(iid, category)
            except Exception:
                logger.warning("Список ссылок: часть %s/%s не встала в очередь ИИ", k + 1, total, exc_info=True)
    if not first:
        try:
            _maybe_auto_backup_tg(db.current_user_id() or 0)   # иначе бэкап позовёт _save()
        except Exception:
            logger.warning("Список ссылок: автокопия архива не запустилась", exc_info=True)
    unsent = sum(1 for p in posts if p is None)
    if unsent:
        logger.warning("Список ссылок: %s из %s частей не дошли до чата, но сохранены в архив", unsent, total)
    payload = {"ok": True, "mode": "list", "status": "saved", "ids": ids, "category": category,
               "icon": theme_icon(category, "📦"), "parts": total, "queued": failed}
    if unsent:
        payload["unsent"] = unsent
    if unsaved:
        payload["unsaved"] = unsaved
    return payload, msgs, urls, lost


async def handler_api_save_links(request):
    from aiohttp import web

    cors = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-Sec-Token",
    }

    def reply(payload, status=200):
        return web.json_response(payload, status=status, headers=cors)

    token = request.headers.get("X-Sec-Token", "")
    if not EXT_SECRET or not hmac.compare_digest(token.encode("utf-8"), EXT_SECRET.encode("utf-8")):
        return reply({"error": "forbidden", "code": "forbidden"}, 403)
    if (request.content_length or 0) > savelinks.MAX_BODY:
        return reply({"error": "body too large", "code": "too_large"}, 413)
    raw = await ytvideo.read_limited(request.content, savelinks.MAX_BODY)
    if raw is None:
        return reply({"error": "body too large", "code": "too_large"}, 413)
    try:
        req = savelinks.parse_request(json.loads(raw.decode("utf-8")))
    except ytvideo.BadRequest as e:
        return reply({"error": str(e), "code": "bad_request"}, 400)
    except Exception:
        return reply({"error": "json body expected", "code": "bad_request"}, 400)
    try:
        if req["mode"] == "each":
            status, payload = await save_links_each(req)
        else:
            status, payload = await save_links_list(req)
    except Exception:
        logger.warning("save-links: сбой", exc_info=True)
        return reply({"error": "Сбой в боте", "code": "internal"}, 500)
    if req["mode"] == "each" and status == 200:
        summary = ",".join(r["status"] for r in payload["results"])
    else:
        summary = payload.get("status") or payload.get("code")
    logger.info("save-links %s ×%s → %s %s", req["mode"], len(req["items"]), status, summary)
    return reply(payload, status)


async def handler_api_backup(request):
    """POST /api/backup: автокопия расширения (gzip) — файлом владельцу в Telegram."""
    from aiohttp import web

    cors = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-Sec-Token",
    }

    def reply(payload, status=200):
        return web.json_response(payload, status=status, headers=cors)

    token = request.headers.get("X-Sec-Token", "")
    if not EXT_SECRET or not hmac.compare_digest(token.encode("utf-8"), EXT_SECRET.encode("utf-8")):
        return reply({"error": "forbidden", "code": "forbidden"}, 403)
    if (request.content_length or 0) > extbackup.MAX_BODY:
        return reply({"error": "Копия слишком большая", "code": "too_large"}, 413)
    raw = await ytvideo.read_limited(request.content, extbackup.MAX_BODY)
    if raw is None:
        return reply({"error": "Копия слишком большая", "code": "too_large"}, 413)
    bad = extbackup.check_body(raw)
    if bad:
        return reply({"error": bad, "code": "bad_request"}, 400)
    off = _links_offline()
    if off:
        return reply(off[1], off[0])
    app = extbackup.app_of(request.query)   # "" — «Сводка комментариев YouTube», как раньше
    wait = extbackup.too_soon(app=app)
    if wait:
        return reply({"error": "Копию только что отправляли", "code": "rate", "retryAfter": wait}, 429)
    extbackup.mark_sent(app=app)
    try:
        msg = await client.send_file(
            _cached_owner(),
            extbackup.as_file(raw, extbackup.filename(app=app)),
            caption=extbackup.caption(request.query),
            force_document=True,
        )
    except Exception:
        logger.warning("backup: не отправилось в Telegram", exc_info=True)
        extbackup.mark_sent(0.0, app=app)  # не отправилось — повторить можно сразу
        return reply({"error": "Не получилось отправить в Telegram", "code": "tg_send"}, 502)
    logger.info("backup %s: копия %s КБ отправлена владельцу", app or "ytcs", len(raw) // 1024)
    if app:
        # копия другого расширения — только файл, очереди YouTube в ней нет
        return reply({"ok": True, "messageId": getattr(msg, "id", None), "size": len(raw)})
    # очередь из копии — для страницы «Очередь YouTube» на телефоне (/yt). Пишем в архив
    # владельца и сразу обновляем тихую копию архива в GitHub: база на Render при каждом
    # перезапуске восстанавливается из неё, и без этого очередь пропадала после выкладки.
    try:
        snap = ytqueue.extract(raw)
        if snap is not None:
            owner = _activate_user(_cached_owner())
            ytqueue.save(snap)
            asyncio.create_task(_send_tg_backup(owner, reason="очередь YouTube"))
    except Exception:
        logger.warning("backup: очередь из копии не сохранилась", exc_info=True)
    return reply({"ok": True, "messageId": getattr(msg, "id", None), "size": len(raw)})


YQ_BACKUP_EVERY = 30 * 60   # тихая копия архива из-за очереди — не чаще раза в 30 мин
YQ_SYNC_MAX_BODY = 2 * 1024 * 1024
YQ_SYNC_LIMIT = (60, 600)    # не больше 60 запросов за 10 минут
_yq_backup = {"last": 0.0, "task": None}
_yq_sync_hits: list[float] = []


def _yq_backup_soon() -> None:
    """Очередь или отметки изменились — обновить тихую копию архива в GitHub (иначе после
    перезапуска Render, который восстанавливает базу из копии, изменения пропадут). Первый
    раз после старта — сразу, потом не чаще раза в YQ_BACKUP_EVERY: всё, что пришло в
    промежутке, уйдёт одной отложенной копией в конце окна."""
    owner = _cached_owner()
    if not owner:
        return
    st = _yq_backup
    if st["task"] is not None and not st["task"].done():
        return  # отложенная копия уже ждёт — она возьмёт и это изменение
    wait = st["last"] + YQ_BACKUP_EVERY - time.time()

    async def run(delay: float):
        if delay > 0:
            await asyncio.sleep(delay)
        while _backup_in_flight:  # идёт другая копия — она могла снять базу до изменения
            await asyncio.sleep(5)
        st["last"] = time.time()
        _activate_user(owner)
        await _send_tg_backup(owner, reason="очередь YouTube")

    st["task"] = asyncio.create_task(run(max(0.0, wait)))


async def handler_api_yq_sync(request):
    """POST /api/yq-sync: лёгкая отправка очереди расширения (без полной копии) и обмен
    отметками с телефона. Тело JSON: {rev, ver, items?, acks?}.
    items — очередь целиком ([{id,title,channel,len,short,pos,added}]); нет items — только
    узнать отметки. acks — [{id, at}] отметок, которые расширение применило или отбросило.
    Ответ: {ok, marks: [{id, act, at}], want} — want: у бота не та очередь, пришлите items."""
    from aiohttp import web

    cors = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-Sec-Token",
    }

    def reply(payload, status=200):
        return web.json_response(payload, status=status, headers=cors)

    token = request.headers.get("X-Sec-Token", "")
    if not EXT_SECRET or not hmac.compare_digest(token.encode("utf-8"), EXT_SECRET.encode("utf-8")):
        return reply({"error": "forbidden", "code": "forbidden"}, 403)
    if (request.content_length or 0) > YQ_SYNC_MAX_BODY:
        return reply({"error": "Очередь слишком большая", "code": "too_large"}, 413)
    raw = await ytvideo.read_limited(request.content, YQ_SYNC_MAX_BODY)
    if raw is None:
        return reply({"error": "Очередь слишком большая", "code": "too_large"}, 413)
    try:
        body = json.loads(raw.decode("utf-8"))
        if not isinstance(body, dict):
            raise ValueError
    except Exception:
        return reply({"error": "json body expected", "code": "bad_request"}, 400)
    owner = _cached_owner()
    if not owner:
        return reply({"error": "Бот не знает владельца архива (OWNER_ID)", "code": "no_owner"}, 503)
    now = time.time()
    n, window = YQ_SYNC_LIMIT
    _yq_sync_hits[:] = [t for t in _yq_sync_hits if now - t < window]
    if len(_yq_sync_hits) >= n:
        return reply({"error": "Слишком часто", "code": "rate", "retryAfter": int(window - (now - _yq_sync_hits[0])) + 1}, 429)
    _yq_sync_hits.append(now)
    _activate_user(owner)
    changed = False
    if ytqueue.ack(body.get("acks")):
        changed = True
    rev = str(body.get("rev") or "")[:40]
    if isinstance(body.get("items"), list):
        snap = ytqueue.from_sync(body, now)
        ytqueue.save(snap)
        changed = True
        logger.info("yq-sync: очередь %s видео (ext %s)", len(snap["items"]), snap["ver"] or "?")
    if changed:
        _yq_backup_soon()
    snap = ytqueue.load()
    want = bool(rev) and (not snap or snap.get("rev") != rev)
    return reply({"ok": True, "marks": ytqueue.marks_list(ytqueue.load_marks()), "want": want})


def _yq_buttons(uid: int) -> list:
    """Кнопки страницы «Очередь YouTube»: в мини-приложении и ссылкой для браузера."""
    base = webarchive.public_url()
    if not base:
        return []
    rows = []
    if hasattr(types, "InlineButtonTypeWebView"):
        rows.append([types.KeyboardInlineButton("📱 Очередь YouTube", types.InlineButtonTypeWebView(f"{base}/yq/"))])
    elif hasattr(types, "KeyboardButtonWebView"):
        rows.append([types.KeyboardButtonWebView("📱 Очередь YouTube", f"{base}/yq/")])
    rows.append([Button.url("🔗 Для браузера", f"{base}/yq/{webarchive.make_token(uid, days=8)}/")])
    return rows


_weekly_last = [0.0]


async def handler_api_weekly_report(request):
    """POST /api/weekly-report {text}: «Отчёт недели» от расширения — сообщением владельцу."""
    from aiohttp import web

    cors = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-Sec-Token",
    }

    def reply(payload, status=200):
        return web.json_response(payload, status=status, headers=cors)

    token = request.headers.get("X-Sec-Token", "")
    if not EXT_SECRET or not hmac.compare_digest(token.encode("utf-8"), EXT_SECRET.encode("utf-8")):
        return reply({"error": "forbidden", "code": "forbidden"}, 403)
    raw = await ytvideo.read_limited(request.content, 16 * 1024)
    if raw is None:
        return reply({"error": "Отчёт слишком большой", "code": "too_large"}, 413)
    try:
        text = str(json.loads(raw.decode("utf-8")).get("text") or "").strip()
    except Exception:
        return reply({"error": "json body expected", "code": "bad_request"}, 400)
    if not text or len(text) > 3000:
        return reply({"error": "нужен текст до 3000 знаков", "code": "bad_request"}, 400)
    off = _links_offline()
    if off:
        return reply(off[1], off[0])
    now = time.time()
    if now - _weekly_last[0] < 60:
        return reply({"error": "Отчёт только что отправляли", "code": "rate"}, 429)
    _weekly_last[0] = now
    owner = _cached_owner()
    try:
        await client.send_message(owner, text, parse_mode=None, link_preview=False, buttons=_yq_buttons(owner) or None)
    except Exception:
        _weekly_last[0] = 0.0
        logger.warning("weekly-report: не отправился", exc_info=True)
        return reply({"error": "Не получилось отправить в Telegram", "code": "tg_send"}, 502)
    return reply({"ok": True})


async def cmd_yt(event):
    """/yt — очередь YouTube из расширения на телефоне."""
    uid = db.current_user_id() or event.sender_id
    btns = _yq_buttons(uid)
    if not btns:
        await event.respond("📱 Страница недоступна: у сервера не задан PUBLIC_URL.")
        return
    _activate_user(uid)
    snap = ytqueue.load()
    head = (f"📱 Очередь YouTube: {len(snap['items'])} видео — расширение само присылает её при каждом изменении."
            if snap else "📱 Очередь YouTube из расширения. Её ещё не было — откройте YouTube в браузере "
            "с расширением, она придёт сама.")
    await event.respond(head + "\nНажмёшь на видео — откроется в YouTube с того места, где остановился. "
                        "«Досмотрел» и «Убрать» уйдут обратно в расширение.",
                        buttons=btns, parse_mode=None, link_preview=False)


# Куки-сессии для /api/download: {sid: {cookies: str(netscape), expires: ts}}
_DL_SESSIONS: dict[str, dict] = {}
_DL_SESSION_TTL = 60 * 30  # 30 минут


def _net_time():
    return int(time.time())


def _cookies_to_netscape(raw_cookies: list[dict]) -> str:
    lines = ["# Netscape HTTP Cookie File"]
    for c in raw_cookies:
        name = c.get("name", "")
        value = c.get("value", "")
        domain = (c.get("domain") or "").lstrip(".") or "youtube.com"
        path = c.get("path") or "/"
        secure = "TRUE" if c.get("secure") else "FALSE"
        exp = c.get("expirationDate")
        exp = int(exp) if exp else _net_time() + 86400 * 365
        include = "TRUE"
        host = "#HttpOnly_." + domain if c.get("httpOnly") else "." + domain
        if value:
            lines.append("\t".join([host, include, path, secure, str(exp), name, value]))
    return "\n".join(lines)


async def handler_api_download_cookies(request):
    from aiohttp import web

    _cors = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, X-Sec-Token",
    }

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "json body expected"}, status=400, headers=_cors)

    token = request.headers.get("X-Sec-Token", "")
    if not EXT_SECRET or not hmac.compare_digest(token, EXT_SECRET):
        return web.json_response({"error": "forbidden"}, status=403, headers=_cors)

    cookies = body.get("cookies")
    if not isinstance(cookies, list) or not cookies:
        return web.json_response({"error": "cookies list required"}, status=400, headers=_cors)
    if len(cookies) > 500:
        return web.json_response({"error": "too many cookies"}, status=400, headers=_cors)

    sid = secrets.token_urlsafe(24)
    _DL_SESSIONS[sid] = {"cookies": _cookies_to_netscape(cookies), "expires": _net_time() + _DL_SESSION_TTL}
    return web.json_response({"session": sid}, headers=_cors)


def _get_session(sid: str) -> str | None:
    if not sid:
        return None
    entry = _DL_SESSIONS.get(sid)
    if not entry:
        return None
    if entry["expires"] < _net_time():
        _DL_SESSIONS.pop(sid, None)
        return None
    return entry["cookies"]


async def handler_api_download(request):
    from aiohttp import web

    try:
        token = request.headers.get("X-Sec-Token", "")
        # Расширение качает через chrome.downloads (не может слать headers),
        # поэтому авторизуем по валидной cookie-сессии — она создаётся только
        # через /api/download-cookies, который требует X-Sec-Token.
        sid = request.query.get("session") or request.headers.get("X-Sec-Session", "")
        session_ok = bool(sid) and bool(_get_session(sid))
        if not EXT_SECRET or (not session_ok and not hmac.compare_digest(token, EXT_SECRET)):
            return web.json_response({"error": "forbidden"}, status=403)

        video_id = (request.query.get("id") or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{6,20}", video_id or ""):
            return web.json_response({"error": "invalid id"}, status=400)

        quality = (request.query.get("quality") or "best").strip().lower()
        if quality not in EXT_DL_QUALITIES:
            return web.json_response({"error": "bad quality"}, status=400)

        # cookie-сессия (браузерные куки, экспортированные расширением) — обход ботозащиты YouTube.
        cookies_netscape = _get_session(sid) if sid else None
        cookie_file = None
        if sid and not cookies_netscape:
            return web.json_response({"error": "session expired"}, status=401)
        if cookies_netscape:
            cookie_file = tempfile.mktemp(suffix=".txt", prefix="ytcookies_")
            with open(cookie_file, "w", encoding="utf-8") as f:
                f.write(cookies_netscape)

        try:
            import yt_dlp
        except ImportError:
            if cookie_file:
                try:
                    os.remove(cookie_file)
                except OSError:
                    pass
            return web.json_response({"error": "yt-dlp not installed"}, status=501)

        url = f"https://www.youtube.com/watch?v={video_id}"

        def make_opts(with_outtmpl=False):
            # YouTube больше не отдаёт прогрессивные mp4 — склеиваем bestvideo+bestaudio через ffmpeg.
            if quality == "best":
                fmt = "bestvideo+bestaudio/best"
            else:
                q = int(quality)
                fmt = (
                    f"bestvideo[height<={q}]+bestaudio/bestvideo[height<={q}]/"
                    f"best[height<={q}]/best"
                )
            opts = {
                "format": fmt,
                "no_warnings": True,
                "quiet": True,
                "noplaylist": True,
                "geo_bypass": True,
                "noprogress": True,
                "merge_output_format": "mp4",
                # yt-dlp 2026: клиент web отдаёт только SABR и требует PO token.
                # tv тебе PO token не нужен (см. PO Token Guide), а с переданными
                # куками форматы не DRM — на датацентре это наиболее живучий путь.
                "extractor_args": {"youtube": {"player_client": ["tv", "default"]}},
                # node — JS-рантайм для декодирования сигнатур YouTube (без него часть форматов недоступна).
                "js_runtimes": {"node": {}},
            }
            if cookie_file:
                opts["cookiefile"] = cookie_file
            if with_outtmpl:
                opts["outtmpl"] = f"{video_id}.%(ext)s"
            return opts

        def probe_info():
            with yt_dlp.YoutubeDL(make_opts()) as ydl:
                return ydl.extract_info(url, download=False)

        try:
            info = await asyncio.to_thread(probe_info)
        except Exception as e:
            logger.warning("/api/download extract fail id=%s q=%s: %s", video_id, quality, e)
            return web.json_response({"error": "not found", "detail": str(e)[:400]}, status=404)

        if not info or info.get("_type") == "playlist":
            return web.json_response({"error": "not found"}, status=404)

        base = info.get("title") or video_id
        filename = re.sub(r'[\\/:*?"<>|]', "_", base)[:80] or video_id

        # Прикидываем размер: берём max filesize по близким форматам.
        size = info.get("filesize") or 0
        for fmt in info.get("formats") or []:
            fs = fmt.get("filesize") or 0
            if fs and abs((fmt.get("width") or 0) - (info.get("width") or 0)) < 400:
                size = max(size, fs)
        if not size:
            size = 0
        if size > EXT_DL_MAX_BYTES:
            return web.json_response(
                {"error": f"too large ({size // 1024 // 1024} MB)"}, status=413
            )

        # Режим проверки: возвращаем метаданные без потока. Расширение вызывает
        # его ДО скачивания — если тут ошибка, надо показать текст, а не дать
        # chrome.downloads сохранить тело JSON-ответа как файл.
        if request.query.get("probe") == "1":
            return web.json_response(
                {"ok": True, "title": filename, "size": size, "quality": quality},
                status=200,
            )

        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Disposition": f'attachment; filename="{filename}.mp4"',
                "Access-Control-Allow-Origin": "*",
            },
        )
        await resp.prepare(request)

        def stream_to_disk():
            with yt_dlp.YoutubeDL(make_opts(with_outtmpl=True)) as ydl:
                return ydl.download([url])

        file_path = f"{video_id}.mp4"
        try:
            await asyncio.to_thread(stream_to_disk)
            if not os.path.exists(file_path):
                import glob

                hits = glob.glob(f"{video_id}.*")
                if hits:
                    file_path = hits[0]

            chunk_size = 64 * 1024 if quality in ("360", "480", "720") else 32 * 1024
            with open(file_path, "rb") as f:
                while True:
                    chunk = f.read(chunk_size)
                    if not chunk:
                        break
                    await resp.write(chunk)
                    await resp.drain()
        except Exception as e:
            logger.warning("/api/download stream fail id=%s q=%s: %s", video_id, quality, e)
        finally:
            try:
                await resp.write_eof()
            except Exception:
                pass
            try:
                import glob

                for p in glob.glob(f"{video_id}.*"):
                    os.remove(p)
            except OSError:
                pass
            if cookie_file:
                try:
                    os.remove(cookie_file)
                except OSError:
                    pass
        return resp
    except Exception as e:
        logger.warning("/api/download fail: %s", e)
        return web.json_response({"error": "download failed"}, status=500)


async def backup_loop():
    """Каждые 6 часов делает локальные копии .db. Отправку снимков (TG/GitHub/канал)
    не делает — этим занимается ежедневная копия (scheduler_loop, 04:00 UTC) и
    копия «каждые N постов», чтобы не спамить канал-хранилище."""
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
            except Exception as e:
                logger.warning("Бэкап для user=%s не удался: %s", uid, e)
            finally:
                db.set_current_user(prev)


_MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь",
           "октябрь", "ноябрь", "декабрь"]
_KIND_RU = {"photo": "🖼 фото", "video": "🎬 видео", "text": "📝 текст", "link": "🔗 ссылки",
            "animation": "🎞 гифки", "document": "📄 файлы", "audio": "🎵 аудио", "voice": "🎙 голосовые",
            "video_note": "⭕ кружки", "album": "🗂 альбомы"}


def _posts_word(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "пост"
    return "поста" if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else "постов"


def month_digest_text(now: datetime | None = None) -> tuple[str, int]:
    """Итоги прошлого месяца (текст, сколько из них ещё не смотрел). Без ИИ."""
    now = now or datetime.utcnow()
    end = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    start = (end - timedelta(days=1)).replace(day=1)
    prev_start = (start - timedelta(days=1)).replace(day=1)
    st = db.month_stats(start, end)
    n = len(st["ids"])
    name = _MONTHS[start.month - 1]
    if not n:
        return f"📊 Итоги месяца ({name}): новых постов не было. Всего в архиве: {st['total']}.", 0
    before = len(db.month_stats(prev_start, start)["ids"])
    trend = ""
    if before:
        arrow = "↑" if n > before else "↓" if n < before else "="
        trend = f" {arrow} (месяцем раньше: {before})"
    lines = [f"📊 Итоги месяца — {name} {start.year}", "",
             f"Сохранено: {n} {_posts_word(n)}{trend}"]
    top = st["cats"][:3]
    if top:
        lines.append("Чаще всего: " + " · ".join(f"{theme_icon(c, '📦')} {c} {k}" for c, k in top))
    kinds = [f"{_KIND_RU.get(t, t)} {k}" for t, k in st["kinds"][:4]]
    if kinds:
        lines.append("Что сохранял: " + " · ".join(kinds))
    tp = db.item_topics()
    counts: dict[str, int] = {}
    for i in st["ids"]:
        for t in tp.get(i, []):
            counts[t] = counts.get(t, 0) + 1
    best = [t for t, c in sorted(counts.items(), key=lambda x: -x[1]) if c >= 2][:5]
    if best:
        lines.append("Главные темы: " + " · ".join(best))
    try:
        unseen = len(unseen_ids() & set(st["ids"]))
    except Exception:
        unseen = 0
    if unseen:
        lines.append(f"Ещё не смотрел: {unseen} из них")
    lines += ["", f"Всего в архиве: {st['total']} {_posts_word(st['total'])}."]
    return "\n".join(lines), unseen


async def send_month_digest(user_id: int) -> bool:
    text, unseen = month_digest_text()
    rows = []
    if unseen:
        rows.append([Button.inline(f"📥 Разобрать новое ({unseen})", data="unseen")])
    rows.append([Button.inline("◀️ Сервис", data="service")])
    await client.send_message(user_id, text, buttons=rows, parse_mode=None, silent=True)
    logger.info("Итоги месяца отправлены (user=%s)", user_id)
    return True


async def send_weekly_file(user_id: int) -> bool:
    """Файл с полной копией архива — владельцу в чат, без звука. Отдельно от GitHub и канала:
    даже если пропадёт всё остальное, этот файл у тебя в Telegram."""
    if user_id in _backup_blocked or db.count_all_items() == 0:
        return False   # архив не восстановился после старта — не шлём пустышку
    dump = db.export_json()
    n_items, n_cats = len(dump.get("items", [])), len(dump.get("categories") or [])
    raw = _gzip_export(dump)
    del dump
    fname = f"Архив_{datetime.utcnow() + timedelta(hours=3):%Y-%m-%d}.json.gz"
    await client.send_file(
        user_id, file=_named_file(raw, fname), silent=True, buttons=[[Button.inline("◀️ Сервис", data="service")]],
        caption=(f"💾 Копия архива на {datetime.utcnow() + timedelta(hours=3):%d.%m}: {n_items} постов, "
                 f"{n_cats} папок.\n\nЕсли архив когда-нибудь пропадёт — просто перешли этот файл мне, "
                 "и я всё верну. Отключить: ⚙️ Настройки → Дополнительно."))
    logger.info("Еженедельный файл архива отправлен (user=%s, items=%s, %s КБ)", user_id, n_items, len(raw) // 1024)
    return True


async def _daily_channel_snapshot(uid: int) -> bool:
    """Раз в сутки — снимок архива в канал-хранилище (с закрепом: по нему бот восстанавливается),
    если архив изменился с прошлого снимка. Не зависит от счётчика GitHub-копий."""
    if not BACKUP_CHANNEL_ID or uid in _backup_blocked or db.count_all_items() == 0:
        return False
    dump = await asyncio.to_thread(db.export_json)
    fp = _export_fingerprint(dump)
    if fp == db.get_setting("last_channel_hash", ""):
        return False
    n_items, n_cats = len(dump.get("items", [])), len(dump.get("categories") or [])
    raw = await asyncio.to_thread(_gzip_export, dump)
    del dump
    await _send_backup_to_channel(raw, "Ежедневная", uid, n_items, n_cats, pin=True)
    db.set_setting("last_channel_hash", fp)
    return True


def _named_file(raw: bytes, fname: str):
    """Байты с именем файла: иначе Telethon 1.46 шлёт «unnamed» (file_name= он молча игнорирует),
    и пересланный обратно файл архива бот не узнаёт как бэкап."""
    import io

    f = io.BytesIO(raw)
    f.name = fname
    return f


async def _send_weekly_digest(user_id: int) -> None:
    since = datetime.utcnow() - timedelta(days=7)
    rows = db.stats_since(since)
    total = sum(r["count"] for r in rows)
    if total == 0:
        await client.send_message(user_id, "📬 Дайджест за неделю: новых постов нет.", buttons=main_keyboard())
        return
    lines = [f"📬 Дайджест за неделю (всего {total} постов):", ""]
    for r in rows[:20]:
        lines.append(f"{theme_icon(r['category'], '📦')} {r['category']}: {r['count']}")
    if len(rows) > 20:
        lines.append(f"… и ещё {len(rows) - 20} папок.")
    await client.send_message(user_id, "\n".join(lines), buttons=main_keyboard())


_backup_in_flight = False


def _refresh_seed(dump: dict) -> None:
    """Перезаписывает запасной seed-файл свежим экспортом, чтобы фолбэк никогда не был устаревшим."""
    try:
        with open(SEED_FILE, "w", encoding="utf-8") as f:
            json.dump(dump, f, ensure_ascii=False, indent=1)
        logger.info("Запасной файл %s обновлён: %s постов", SEED_FILE, len(dump.get("items", [])))
    except Exception as e:
        logger.warning("Не удалось обновить запасной файл %s: %s", SEED_FILE, e)


def _gzip_export(dump: dict) -> bytes:
    """Сериализует экспорт в JSON и сжимает gzip.

    json.dump стримится прямо в gzip-буфер — в памяти не держится ни полная
    JSON-строка, ни её UTF-8 копия (у большого экспорта это десятки МБ).
    Сжатый payload сильно меньше исходного, поэтому отправка в Telegram/канал
    и пуши в GitHub потребляют меньше памяти и реже ловят OOM на Render."""
    import io

    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6) as gz:
        with io.TextIOWrapper(gz, encoding="utf-8") as tw:
            json.dump(dump, tw, ensure_ascii=False)
    return buf.getvalue()


def _maybe_gunzip(raw: bytes) -> bytes:
    """Распаковывает gzip, если данные сжаты (магические байты 1f 8b)."""
    if raw[:2] == b"\x1f\x8b":
        return gzip.decompress(raw)
    return raw


_backup_blocked: set = set()


def _export_fingerprint(dump: dict) -> str:
    """Отпечаток содержимого архива (без служебных last_* настроек)."""
    import hashlib

    d = dict(dump)
    d["settings"] = [
        s for s in dump.get("settings", [])
        if not str(s.get("key", "")).startswith(("last_", "llm_retry", "recat_job", "assistant_session"))
    ]
    return hashlib.sha1(
        json.dumps(d, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


async def _backup_if_changed(user_id: int, reason: str) -> bool:
    """Копия в GitHub, только если архив изменился с прошлой копии (любые правки:
    новые посты, перемещения, переименования, теги). Раньше копия шла лишь после
    каждых N новых постов, и ручные правки пропадали при перезапуске Render."""
    if db.count_all_items() == 0:
        return False
    try:
        fp = _export_fingerprint(db.export_json())
    except Exception:
        return False
    if fp == db.get_setting("last_export_hash", ""):
        return False
    await _send_tg_backup(user_id, reason=reason)
    return True


_shutting_down = False


async def _on_sigterm():
    global _shutting_down
    if _shutting_down:
        return
    _shutting_down = True
    logger.info("SIGTERM: сохраняю свежие правки перед остановкой...")
    try:
        await asyncio.wait_for(_backup_on_shutdown(), 25)
    except Exception as e:
        logger.warning("Копия перед остановкой не успела: %s", e)
    logging.shutdown()
    os._exit(0)


async def changes_backup_loop():
    """Раз в 10 минут: если архив поменялся — тихая копия в GitHub."""
    while True:
        await asyncio.sleep(600)
        for uid in db.all_users():
            prev = db.current_user_id()
            try:
                db.set_current_user(uid)
                await _backup_if_changed(uid, "изменения в архиве")
            except Exception as e:
                logger.warning("Копия изменений (user=%s) не удалась: %s", uid, e)
            finally:
                db.set_current_user(prev)


async def _backup_on_shutdown():
    """Render перед перезапуском шлёт SIGTERM и даёт ~30 с — успеваем сохранить свежие правки."""
    for uid in db.all_users():
        prev = db.current_user_id()
        try:
            db.set_current_user(uid)
            if await asyncio.wait_for(_backup_if_changed(uid, "перед остановкой"), 20):
                logger.info("Копия перед остановкой отправлена (user=%s)", uid)
        except Exception as e:
            logger.warning("Копия перед остановкой (user=%s) не удалась: %s", uid, e)
        finally:
            db.set_current_user(prev)


async def _send_tg_backup(user_id: int, reason: str = "Ежедневная") -> None:
    """Тихая копия экспорта: складывает снимок в запасной файл, GitHub, канал-хранилище —
    без отправки файла и пояснений в чат (это не спам, а страховка)."""
    global _backup_in_flight
    if _backup_in_flight:
        return
    if user_id in _backup_blocked and not reason.startswith("Ручн"):
        # Восстановление при старте не удалось: архив пуст/неполон. Автокопия
        # перезаписала бы хороший бэкап в GitHub почти пустым — не даём.
        logger.warning("Автокопия user=%s пропущена: архив не восстановлен после старта", user_id)
        return
    _backup_in_flight = True
    try:
        dump = db.export_json()
        fingerprint = _export_fingerprint(dump)
        _refresh_seed(dump)
        raw = _gzip_export(dump)
        del dump
        github_ok = await _push_github_file(_github_backup_path(user_id), raw)
        if github_ok:
            db.set_setting("last_export_hash", fingerprint)
        db.set_setting("last_export_count", str(db.count_all_items()))
        db.set_setting("last_export_time", datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
        db.set_setting("last_backup_reason", reason)
        if github_ok:
            db.set_setting("last_github_backup_time", datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"))
        logger.info("Тихая копия экспорта (user=%s, reason=%s, github=%s)", user_id, reason, github_ok)
    finally:
        _backup_in_flight = False


def _maybe_forward_backup_to_channel(raw: bytes, reason: str, user_id: int | None = None,
                                     n_items: int = 0, n_cats: int = 0) -> None:
    """Кладёт копию экспорта в приватный канал-хранилище (бот = админ → может прочитать обратно)."""
    if not BACKUP_CHANNEL_ID:
        return
    try:
        asyncio.create_task(_send_backup_to_channel(raw, reason, user_id, n_items, n_cats))
    except Exception as e:
        logger.warning("Копия в канал-хранилище не поставлена: %s", e)


async def _send_backup_to_channel(raw: bytes, reason: str, user_id: int | None = None,
                                 n_items: int = 0, n_cats: int = 0, pin: bool = False) -> None:
    try:
        uid = int(user_id or 0) or 0
        fname = f"tg_saver_export_user{uid}_{datetime.utcnow():%Y%m%d_%H%M}.json.gz" if uid else "tg_saver_export.json.gz"
        caption = (
            f"🗄 Снимок архива (user{uid}) · {reason}\n"
            f"📦 Постов: {n_items} · 📂 Папок: {n_cats}\n"
            f"🕓 {datetime.utcnow():%d.%m.%Y %H:%M} (UTC)"
        )
        msg = await client.send_file(BACKUP_CHANNEL_ID, file=_named_file(raw, fname), caption=caption)
        logger.info("Снимок экспорта отправлен в канал-хранилище (%s, user=%s, items=%s)", reason, uid or "-", n_items)
        if pin and msg:
            try:
                await client.pin_message(BACKUP_CHANNEL_ID, msg, notify=False)
            except Exception as e:
                logger.info("Закрепить снимок в канале не вышло: %s", e)
    except Exception as e:
        logger.warning("Не удалось отправить снимок в канал-хранилище: %s", e)


def _github_backup_path(user_id: int | None) -> str:
    uid = int(user_id or 0) or 0
    fname = f"tg_saver_export_user{uid}.json" if uid else "tg_saver_export.json"
    return f"{GITHUB_PATH}/{fname}" if GITHUB_PATH else fname


SESSION_REMOTE_PATH = f"{GITHUB_PATH}/session_bot.b64" if GITHUB_PATH else "session_bot.b64"
DAILY_MARKER_PATH = f"{GITHUB_PATH}/daily_marker" if GITHUB_PATH else "daily_marker"


async def _daily_marker() -> str:
    """Дата последней ежедневной копии (ISO), из GitHub-синка. '' если не было."""
    if not GITHUB_TOKEN:
        return ""
    got = await _fetch_github_raw(DAILY_MARKER_PATH)
    if not got:
        return ""
    try:
        return got.decode("utf-8").strip()
    except Exception:
        return ""


async def _set_daily_marker() -> bool:
    if not GITHUB_TOKEN:
        return False
    return await _push_github_file(DAILY_MARKER_PATH, datetime.utcnow().strftime("%Y-%m-%d").encode())


async def _restore_telegram_session() -> None:
    """Если сессия потеряна (эфемерный диск) — достаёт её из GitHub-синка (base64).

    Без этого каждый рестарт делает новую авторизацию Telegram, что рано или поздно
    ловит FloodWait на ImportBotAuthorizationRequest и кладёт бот надолго."""
    if not GITHUB_TOKEN or os.path.exists(SESSION_FILE):
        return
    try:
        got = await _fetch_github_raw(SESSION_REMOTE_PATH)
        if not got:
            logger.info("GitHub-сессии нет — будет создана новая авторизация Telegram.")
            return
        raw = base64.b64decode(got)
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


async def _fetch_github_raw(path: str) -> bytes | None:
    """Полное содержимое файла (raw media type).

    GitHub Contents API молча отдаёт пустое поле content для файлов больше ~1 МБ
    (а экспорту это обычный размер), поэтому читаем через raw media type,
    который отдаёт байты напрямую."""
    token = GITHUB_TOKEN
    if not token:
        return None
    try:
        import aiohttp

        url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
        headers = {
            "Accept": "application/vnd.github.raw",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"Bearer {token}",
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=60)) as resp:
                if resp.status == 404:
                    return None
                if resp.status != 200:
                    logger.warning("GitHub RAW %s: HTTP %s", path, resp.status)
                    return None
                return await resp.read()
    except Exception as e:
        logger.warning("GitHub RAW %s не удалось: %s", path, e)
        return None


async def _push_github_file(path: str, raw: bytes) -> bool:
    """Создаёт/обновляет файл через Git Data API (blob → tree → commit → ref).

    Contents API не подходит: у PUT есть лимит (~1 МБ), и он не переживает рост
    экспорта. Git Data API пушит файл любого размера. При конкурентной правке
    ref (409/422) делает повторную попытку со свежим head."""
    token = GITHUB_TOKEN
    if not token:
        return False
    try:
        import base64
        import aiohttp

        repo = GITHUB_REPO
        branch = GITHUB_BRANCH
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }

        async def _blob_sha(session) -> str | None:
            blob = {"content": base64.b64encode(raw).decode(), "encoding": "base64"}
            async with session.post(
                f"https://api.github.com/repos/{repo}/git/blobs",
                headers=headers, json=blob, timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status not in (200, 201):
                    logger.warning("GitHub blob %s: HTTP %s: %s", path, resp.status, (await resp.text())[:300])
                    return None
                return (await resp.json())["sha"]

        async def _commit_once(session, blob_sha) -> bool:
            async with session.get(
                f"https://api.github.com/repos/{repo}/git/refs/heads/{branch}",
                headers=headers, timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    logger.warning("GitHub ref %s: HTTP %s", branch, resp.status)
                    return False
                head_sha = (await resp.json())["object"]["sha"]

            async with session.get(
                f"https://api.github.com/repos/{repo}/git/commits/{head_sha}",
                headers=headers, timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    logger.warning("GitHub commit %s: HTTP %s", head_sha, resp.status)
                    return False
                base_tree = (await resp.json())["tree"]["sha"]

            tree_payload = {
                "base_tree": base_tree,
                "tree": [{"path": path, "mode": "100644", "type": "blob", "sha": blob_sha}],
            }
            async with session.post(
                f"https://api.github.com/repos/{repo}/git/trees",
                headers=headers, json=tree_payload, timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status not in (200, 201):
                    logger.warning("GitHub tree %s: HTTP %s: %s", path, resp.status, (await resp.text())[:300])
                    return False
                new_tree = (await resp.json())["sha"]

            commit_payload = {
                "message": f"backup: обновление экспорта ({datetime.utcnow():%Y-%m-%d %H:%M} UTC)",
                "tree": new_tree,
                "parents": [head_sha],
            }
            async with session.post(
                f"https://api.github.com/repos/{repo}/git/commits",
                headers=headers, json=commit_payload, timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status not in (200, 201):
                    logger.warning("GitHub commit %s: HTTP %s: %s", path, resp.status, (await resp.text())[:300])
                    return False
                commit_sha = (await resp.json())["sha"]

            async with session.patch(
                f"https://api.github.com/repos/{repo}/git/refs/heads/{branch}",
                headers=headers, json={"sha": commit_sha, "force": False},
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status != 200:
                    logger.warning("GitHub ref update %s: HTTP %s: %s", branch, resp.status, (await resp.text())[:200])
                    return False
            return True

        async with aiohttp.ClientSession() as session:
            blob_sha = await _blob_sha(session)
            if not blob_sha:
                return False
            for attempt in range(1, 3):
                if await _commit_once(session, blob_sha):
                    break
                if attempt == 2:
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
    alt = path[:-5] + ".json.gz" if path.endswith(".json") else ""
    last_reason = ""
    for p in (path, alt) if alt else (path,):
        got = await _fetch_github_raw(p)
        if not got:
            last_reason = f"в репо нет файла {p} или ошибка чтения"
            continue
        try:
            _t0 = time.monotonic()
            res = await asyncio.to_thread(db.import_json_stream, got)
            logger.info("GitHub-импорт %s занял %.1f с (фоновый поток, healthz жив)", p, time.monotonic() - _t0)
        except Exception as e:
            last_reason = f"файл {p} не читается как JSON: {e}"
            continue
        if not isinstance(res, dict) or not res.get("items"):
            last_reason = f"файл {p} не похож на экспорт"
            continue
        logger.info("Восстановлено из GitHub (user=%s, %s): items=%s cats=%s tags=%s",
                    uid, p, res["items"], res["categories"], res["tags"])
        return True, p
    return False, last_reason or "источников для восстановления не найдено"


async def _restore_from_channel() -> bool:
    """Импортирует последний снимок экспорта пользователя из канала-хранилища
    (бот-админ может читать историю канала). Снимки помечены id пользователя в имени файла."""
    if not BACKUP_CHANNEL_ID:
        logger.info("_restore_from_channel: BACKUP_CHANNEL_ID не задан")
        return False, "BACKUP_CHANNEL_ID не задан"
    uid = db.current_user_id()
    want = f"user{uid}" if uid else ""
    logger.info("_restore_from_channel: user=%s want=%r", uid, want)
    msgs = []
    try:
        # боту нельзя листать историю канала, но можно взять закреплённое сообщение
        full = await client(functions.channels.GetFullChannelRequest(await client.get_input_entity(BACKUP_CHANNEL_ID)))
        pinned = getattr(full.full_chat, "pinned_msg_id", None)
        if pinned:
            msgs = [m for m in await client.get_messages(BACKUP_CHANNEL_ID, ids=[pinned]) if m]
    except Exception as e:
        logger.info("_restore_from_channel: закреплённый снимок не получен: %s", e)
    if not msgs:
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
        is_snapshot = fname.endswith(".json") or fname.endswith(".json.gz") or "снимок архива" in caption
        if not is_snapshot:
            skipped.append(f"ид{m.id}:файл{fname!r}:не снимок")
            continue
        suid = None
        imported = re.search(r"user(\d+)_", fname)
        if imported:
            suid = imported.group(1)
        elif fname.startswith("tg_saver_export_user") and (fname.endswith(".json") or fname.endswith(".json.gz")):
            imported = re.match(r"tg_saver_export_user(\d+)(\.json(\.gz)?)?$", fname)
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
            res = await asyncio.to_thread(db.import_json_stream, raw)
        except Exception as e:
            logger.warning("_restore_from_channel: %s не читается как JSON: %s", fname, e)
            skipped.append(f"ид{m.id}:не json: {e}")
            continue
        if not isinstance(res, dict) or not res.get("items"):
            logger.warning("_restore_from_channel: %s не похож на экспорт (ключ items отсутствует)", fname)
            skipped.append(f"ид{m.id}:нет items")
            continue
        if res.get("items"):
            logger.info(
                "Восстановлено из канала-хранилища (user=%s): items=%s cats=%s tags=%s",
                uid, res["items"], res["categories"], res["tags"],
            )
            try:
                age_days = (datetime.now(m.date.tzinfo) - m.date).total_seconds() / 86400 if m.date else 0
            except Exception:
                age_days = 0
            if age_days > 2 and uid:
                # снимок старый: пока владелец не проверит, автокопии не перезаписывают GitHub
                _backup_blocked.add(uid)
                logger.warning("Снимок из канала старый (%.1f дн.) — автокопии приостановлены до /export", age_days)
                try:
                    await client.send_message(uid, f"⚠️ Архив восстановлен из канала-хранилища, но снимку {age_days:.0f} дн. "
                                                   "Чтобы не затереть более свежую копию, автокопии приостановлены. "
                                                   "Если всё на месте — нажми /export, и копии пойдут снова. "
                                                   "Если чего-то не хватает — перешли мне свежий файл архива.",
                                              parse_mode=None)
                except Exception:
                    pass
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


async def llm_retry_loop():
    """Дораспознаёт посты, сохранённые, пока ИИ был недоступен (лимиты/сбой Groq).
    Раньше такие посты навсегда оставались в «Фото»/«Видео»/«Заметки»."""
    _cat.USAGE_SOURCE.set("retry")
    while True:
        await asyncio.sleep(600)
        for uid in db.all_users():
            prev = db.current_user_id()
            try:
                db.set_current_user(uid)
                queue = db.retry_queue_items()
                for key, meta in list(queue.items())[:10]:
                    try:
                        iid = int(key)
                    except ValueError:
                        continue
                    item = db.get_item(iid)
                    if not item or item["locked"] or item["category"] != meta.get("cat"):
                        db.retry_queue_done(iid)  # удалён или пользователь уже разложил сам
                        continue
                    rep = {}
                    txt = await _recat_text(item, report=rep)
                    if not txt:
                        db.retry_queue_done(iid)
                        continue
                    if rep.get("media_failed") and not db.retry_queue_items().get(str(iid), {}).get("n", 0) >= 5:
                        db.retry_queue_done(iid, failed=True)  # картинка всё ещё не описывается
                        break
                    result = await _ai_categorize(txt, item["content_type"], item["source_channel"], exclude_id=iid)
                    if result is None:
                        db.retry_queue_done(iid, failed=True)
                        break  # ИИ всё ещё лежит — подождём следующего круга
                    db.update_item_category(iid, result["category"], result["summary"])
                    db.retry_queue_done(iid)
                    logger.info("Дораспознан item=%s → %s", iid, result["category"])
                    try:
                        await client.send_message(
                            item.get("chat_id") or uid,
                            f"🔁 Дораспознал: {theme_icon(result['category'], '📦')} «{result['category']}»\n\n{result['summary']}",
                            reply_to=item.get("message_id") or None,
                        )
                    except Exception:
                        pass
            except Exception as e:
                logger.warning("Дораспознавание (user=%s): ошибка %s", uid, e)
            finally:
                db.set_current_user(prev)


async def scheduler_loop():
    """Дайджест (воскресенье 07:00 UTC) и ежедневная копия экспорта (04:00 UTC) — для каждого юзера."""
    while True:
        await asyncio.sleep(3600 - time.time() % 3600 + 30)   # до начала следующего часа
        now = datetime.utcnow()
        for uid in db.all_users():
            try:
                prev = db.current_user_id()
                db.set_current_user(uid)
                _refresh_icon_overrides()
                if now.weekday() == 6 and now.hour == 5:
                    week = f"{now.isocalendar()[0]:04d}-{now.isocalendar()[1]:02d}"
                    if db.get_setting("last_backup_check_week", "") != week:
                        db.set_setting("last_backup_check_week", week)
                        await run_backup_selfcheck(uid)
                        db.set_current_user(uid)
                if db.get_setting("weekly_cleanup", "1") == "1" and now.weekday() == 5 and now.hour == 9:
                    week_key = f"{now.isocalendar()[0]:04d}-{now.isocalendar()[1]:02d}"
                    if db.get_setting("last_cleanup_week", "") != week_key:
                        db.set_setting("last_cleanup_week", week_key)
                        await assistant_weekly(uid)
                        db.set_current_user(uid)
                if db.get_setting("on_this_day", "1") == "1" and now.weekday() == 6 and now.hour == 8:
                    week_key = f"{now.isocalendar()[0]:04d}-{now.isocalendar()[1]:02d}"
                    if db.get_setting("last_memories_week", "") != week_key:
                        db.set_setting("last_memories_week", week_key)
                        await send_memories(uid)
                        db.set_current_user(uid)
                if db.get_setting("weekly_digest", "1") == "1" and now.weekday() == 6 and now.hour == 7:
                    week_key = f"{now.isocalendar()[0]:04d}-{now.isocalendar()[1]:02d}"
                    if db.get_setting("last_digest_week", "") != week_key:
                        db.set_setting("last_digest_week", week_key)
                        await _send_weekly_digest(uid)
                for wkind, whour in (("github", 11), ("anime", 16)):
                    if db.get_setting(f"{wkind}_watch", "1") == "1" and now.hour == whour:
                        day = now.strftime("%Y-%m-%d")
                        if db.get_setting(f"last_{wkind}_day", "") != day:
                            db.set_setting(f"last_{wkind}_day", day)
                            await check_watch(uid, wkind)
                            db.set_current_user(uid)
                if db.get_setting("daily_read", "1") == "1" and now.hour == 17:
                    day = now.strftime("%Y-%m-%d")
                    if db.get_setting("last_read_day", "") != day:
                        db.set_setting("last_read_day", day)
                        await send_daily_read(uid)
                        db.set_current_user(uid)
                if db.get_setting("game_deals", "1") == "1" and now.hour == 10:
                    day = now.strftime("%Y-%m-%d")
                    if db.get_setting("last_deals_day", "") != day:
                        db.set_setting("last_deals_day", day)
                        await check_game_deals(uid)
                        db.set_current_user(uid)
                if db.get_setting("monthly_digest", "1") == "1" and now.day == 1 and now.hour == 8:
                    month_key = now.strftime("%Y-%m")
                    if db.get_setting("last_month_digest", "") != month_key:
                        db.set_setting("last_month_digest", month_key)
                        await send_month_digest(uid)
                        db.set_current_user(uid)
                if db.get_setting("cleanup_monthly", "1") == "1" and now.day == 15 and now.hour == 9:
                    month_key = now.strftime("%Y-%m")
                    if db.get_setting("last_cleanup_month", "") != month_key:
                        db.set_setting("last_cleanup_month", month_key)
                        await send_monthly_cleanup(uid)
                        db.set_current_user(uid)
                if db.get_setting("weekly_file", "1") == "1" and now.weekday() == 6 and now.hour == 7:
                    week_key = f"{now.isocalendar()[0]:04d}-{now.isocalendar()[1]:02d}"
                    if db.get_setting("last_weekly_file", "") != week_key:
                        db.set_setting("last_weekly_file", week_key)
                        await send_weekly_file(uid)
                        db.set_current_user(uid)
                if db.get_setting("backup_tg_daily", "1") == "1" and now.hour == 4:
                    cday = now.strftime("%Y-%m-%d")
                    if db.get_setting("last_channel_day", "") != cday:
                        db.set_setting("last_channel_day", cday)
                        try:
                            await _daily_channel_snapshot(uid)
                        except Exception as e:
                            logger.warning("Снимок в канал (user=%s): %s", uid, e)
                        db.set_current_user(uid)
                if db.get_setting("backup_tg_daily", "1") == "1" and now.hour == 4:
                    today = now.strftime("%Y-%m-%d")
                    local_day = db.get_setting("last_backup_tg_day", "")
                    if local_day != today:
                        db.set_setting("last_backup_tg_day", today)
                        marker = await _daily_marker()
                        already_sent = bool(marker and marker >= today)
                        if not already_sent and _posts_since_export() > 0:
                            await _send_tg_backup(uid, reason="Ежедневная")
                            await _set_daily_marker()
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
    if TG_TRANSPORT == "socks" and TG_SOCKS_HOST:
        # SOCKS5 через xray (127.0.0.1:10808 — тот же контур, что у ботов hh/demo).
        # python-socks уже в venv; Telethon умеет SOCKS5 «из коробки».
        from python_socks import ProxyType

        client = TelegramClient(
            SESSION_FILE,
            API_ID,
            API_HASH,
            proxy=(ProxyType.SOCKS5, TG_SOCKS_HOST, TG_SOCKS_PORT),
        )
    elif TG_TRANSPORT == "mtproxy" and MT_PROXY_HOST:
        # MTProxy (TgWsProxy 127.0.0.1:1080) — резерв на случай смерти SOCKS-контура.
        client = TelegramClient(
            SESSION_FILE,
            API_ID,
            API_HASH,
            connection=ConnectionTcpMTProxyAbridged,
            proxy=(MT_PROXY_HOST, MT_PROXY_PORT, MT_PROXY_SECRET),
        )
    else:
        # Прямое соединение (Render/зарубежный хост).
        client = TelegramClient(SESSION_FILE, API_ID, API_HASH)

    client.add_event_handler(_safe_handler(on_new_message, _update_sem, watchdog=300), events.NewMessage(incoming=True))
    client.add_event_handler(_safe_handler(on_album, _update_sem, watchdog=300), events.Album())
    client.add_event_handler(_safe_handler(on_callback), events.CallbackQuery())
    client.add_event_handler(_safe_handler(on_inline), events.InlineQuery())
    client.sequential_updates = False

    _health_started = False

    async def start():
        nonlocal _health_started
        if not _health_started:
            asyncio.create_task(health_http())
            await asyncio.sleep(0.3)
            _health_started = True
            try:
                import signal

                asyncio.get_running_loop().add_signal_handler(
                    signal.SIGTERM, lambda: asyncio.ensure_future(_on_sigterm())
                )
            except (NotImplementedError, RuntimeError, ValueError):
                pass  # Windows: сигналы в asyncio не поддерживаются

        # Подключение к Telegram. FloodWait ждём через asyncio.sleep (event loop
        # жив, /healthz отвечает) — иначе Render убивает деплой по «no open ports»,
        # а чередой рестартов мы сами продлеваем FloodWait.
        while True:
            try:
                await _restore_telegram_session()
                await client.start(bot_token=BOT_TOKEN)
                me = await client.get_me()
                _transport_label = (
                    f"SOCKS5 {TG_SOCKS_HOST}:{TG_SOCKS_PORT}"
                    if TG_TRANSPORT == "socks"
                    else f"MTProxy {MT_PROXY_HOST}:{MT_PROXY_PORT}"
                    if TG_TRANSPORT == "mtproxy"
                    else "напрямую"
                )
                logger.info("Telethon-бот %s подключён через %s", getattr(me, "username", "?"), _transport_label)
                global BOT_USERNAME
                BOT_USERNAME = getattr(me, "username", "") or ""
                await setup_menu_button()
                await _store_telegram_session()
                break
            except FloodWaitError as e:
                wait = int(getattr(e, "seconds", 60) or 60) + 10
                logger.error(
                    "Telegram FloodWait(%s) при авторизации — жду %s c, чтобы не продлевать лимит.",
                    e.seconds, wait,
                )
                await asyncio.sleep(wait)
            except Exception as e:
                logger.exception("Не удалось подключиться к Telegram: %s. Повтор через 5 с.", e)
                await asyncio.sleep(5)
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
                    restored_channel = False
                    gh_reason = ""
                    if db.count_all_items() == 0:
                        ok, gh_reason = await _restore_from_github()
                        if ok:
                            restored_gh = True
                        else:
                            if db.count_all_items() == 0:
                                ok_ch, _ = await _restore_from_channel()
                                if ok_ch:
                                    restored_channel = True
                            if db.count_all_items() == 0 and await asyncio.to_thread(_maybe_restore_db, owner):
                                restored_seed = True
                    if _recover_enabled:
                        n = db.count_all_items()
                        try:
                            if n == 0:
                                _backup_blocked.add(owner)
                                await client.send_message(
                                    owner,
                                    "🆘 Архив пуст (судя по всему, после перезапуска файлы БД не сохранились).\n\n"
                                    "Чтобы восстановить историю: перешли мне сюда последний файл "
                                    "«tg_saver_export.json.gz» из этого чата — я импортирую его автоматически.",
                                )
                            elif restored_gh:
                                await client.send_message(
                                    owner,
                                    f"✅ БД была потеряна — восстановлено из GitHub-копии: {n} постов.",
                                )
                            elif restored_channel:
                                await client.send_message(
                                    owner,
                                    f"✅ БД была потеряна — восстановлено из канала-хранилища: {n} постов.",
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
                                logger.info("Старт без восстановления: архив в порядке (%s постов).", n)
                        except Exception:
                            pass
                    try:
                        for _tok, its in db.find_archive_dups(limit=50):
                            logger.info(
                                "ARCHIVE-DUP tok=%s n=%s ids=%s cts=%s",
                                (_tok or "")[:60], len(its),
                                [x["id"] for x in its],
                                [x["content_type"] for x in its],
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
            llm_retry_loop(),
            changes_backup_loop(),
            recat_resume_loop(),
            night_recat_loop(),
            hash_backfill_loop(),
            icon_backfill_loop(),
            reminders_loop(),
            topics_backfill_loop(),
            articles_loop(),
            link_check_loop(),
            media_mirror_loop(),
            structure_migrations_loop(),
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