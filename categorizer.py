import aiohttp
import asyncio
import base64
import contextvars
import json
import logging
import re
from config import (
    GROQ_API_KEY,
    GROQ_BASE_URL,
    GROQ_MODEL,
    GROQ_VISION_MODEL,
    GROQ_PROXY,
    GROQ_TIMEOUT_SEC,
    GROQ_CATEGORIZE_MODEL,
    GROQ_FALLBACK_MODEL,
    CF_ACCOUNT_ID,
    CF_API_TOKEN,
    CF_TEXT_MODEL,
    CF_VISION_MODEL,
    GEMINI_API_KEY,
    GEMINI_MODEL,
)

# Все вызовы ИИ сериализуются локом, чтобы не превышать rate-limits (RPM/OTPM).
logger = logging.getLogger(__name__)
_LLM_LOCK = asyncio.Lock()

# Последняя причина пустого ответа Groq (для диагностики в /api/chat).
LLM_LAST_ERROR: str = ""

# ── Учёт суточного запаса токенов ───────────────────────────────────────────
# Бесплатный Groq: 200 000 токенов в сутки НА КАЖДУЮ модель, а пост стоит
# ~2 200 токенов (≈90 постов в сутки на модель). Запас — «ведро»: тратится сразу,
# а пополняется равномерно (200 000 / 86 400 ≈ 2,3 токена в секунду; это видно по
# «try again in 12m48s» на дефицит ~1 800 токенов). Через этот процесс идут и бот,
# и расширение YouTube, поэтому ведём запас сами: фоновые задачи смотрят на остаток
# и не выедают всё. После перезапуска считаем ведро полным, а первая же ошибка
# «Limit 200000, Used 197813» выставляет точный уровень.
DAY_TOKENS_DEFAULT = 200_000
_DAY = 24 * 3600
_bucket: dict[str, tuple[float, float]] = {}   # модель → (уровень, когда замерили)
_day_limit: dict[str, int] = {}
_spent: dict[str, list] = {}                   # модель → [токенов, запросов] с запуска
_synced: dict[str, float] = {}                 # модель → когда Groq сам сообщил уровень
# Кто тратит: метка задачи (contextvar — у каждой задачи своя). posts — новые посты,
# bg — фоновое перераспознавание/ночной добор, retry — дораспознавание, ext —
# расширение YouTube (/api/chat), service — авто-порядок, подпапки, «Уточнить».
USAGE_SOURCE = contextvars.ContextVar("usage_source", default="posts")
_events: list[tuple[float, str, str, int]] = []   # (время, источник, модель, токены) за сутки
STARTED_AT = __import__("time").time()


def day_limit(model: str) -> int:
    return _day_limit.get(model, DAY_TOKENS_DEFAULT)


def refill_per_sec(model: str) -> float:
    return day_limit(model) / _DAY


def day_headroom(model: str, at: float | None = None) -> float:
    """Сколько токенов модели доступно сейчас (или к моменту at, если ничего не тратить)."""
    import time as _t

    now = _t.time()
    cap = day_limit(model)
    lvl, ts = _bucket.get(model, (cap, now))
    return min(cap, lvl + refill_per_sec(model) * ((at or now) - ts))


def tokens_used_today(model: str) -> int:
    """Дефицит ведра: сколько не хватает до полного запаса."""
    return int(day_limit(model) - day_headroom(model))


def _note_usage(model: str, tokens: int) -> None:
    if not model or tokens <= 0:
        return
    import time as _t

    _bucket[model] = (day_headroom(model) - int(tokens), _t.time())
    now = _t.time()
    _events.append((now, USAGE_SOURCE.get(), model, int(tokens)))
    while _events and now - _events[0][0] > _DAY:
        _events.pop(0)
    sp = _spent.setdefault(model, [0, 0])
    sp[0] += int(tokens)
    sp[1] += 1


def _note_limit_error(model: str, err: str) -> None:
    """Из 429 «tokens per day (TPD): Limit 200000, Used 197813» узнаём точный уровень."""
    m = re.search(r"per day \(TPD\): Limit (\d+), Used (\d+)", err or "")
    if not m:
        return
    import time as _t

    limit, used = int(m.group(1)), int(m.group(2))
    _day_limit[model] = limit
    _bucket[model] = (float(limit - used), _t.time())
    _synced[model] = _t.time()


def usage_snapshot() -> dict:
    """Для экрана «Лимиты ИИ»: {модель: {left, limit, spent, requests, synced}}."""
    models = set(_bucket) | set(_spent) | {GROQ_CATEGORIZE_MODEL, GROQ_VISION_MODEL}
    if GROQ_FALLBACK_MODEL:
        models.add(GROQ_FALLBACK_MODEL)
    out = {}
    for m in sorted(models):
        sp = _spent.get(m, [0, 0])
        out[m] = {
            "left": int(day_headroom(m)), "limit": day_limit(m),
            "spent": sp[0], "requests": sp[1], "synced": _synced.get(m),
        }
    return out


def usage_by_source() -> dict:
    """{источник: [токенов, запросов]} за последние сутки (с запуска бота)."""
    import time as _t

    now = _t.time()
    out: dict[str, list] = {}
    for ts, src, _m, n in _events:
        if now - ts <= _DAY:
            row = out.setdefault(src, [0, 0])
            row[0] += n
            row[1] += 1
    return out


def _usage_tokens(data: dict) -> int:
    """Токены, которые идут в лимит: всё, кроме взятого из кэша Groq."""
    u = (data or {}).get("usage") or ((data or {}).get("x_groq") or {}).get("usage") or {}
    try:
        total = int(u.get("total_tokens") or 0)
        cached = int((u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    except (TypeError, ValueError, AttributeError):
        return 0
    return max(0, total - cached)

_MD_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_LEADING_NUM = re.compile(r"^\s*\[\d+\]\s*")


def strip_markdown(text: str) -> str:
    """Выкидывает markdown-разметку: ссылки [т](url)→т, **/__/``, лишние * _ и префикс [N]."""
    text = (text or "").strip()
    text = _MD_LINK.sub(r"\1", text)
    text = text.replace("**", "").replace("__", "")
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = text.replace("*", "_")  # объединяем, чтобы вырезать одним проходом
    text = text.replace("_", "")
    text = _MD_LEADING_NUM.sub("", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def normalize_summary(text: str, limit: int = 300) -> str:
    """summary без markdown, с обрезкой по границе слова."""
    return _clip_summary(strip_markdown(text), limit)


def _clip_summary(text: str, limit: int = 300) -> str:
    """Обрезает текст по границе слова, чтобы summary не обрывался на полуслове."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = text[: limit + 1]
    cut = head.rfind(" ")
    if cut > limit // 2:
        return text[:cut].rstrip(".,;:!?") + "..."
    return text[:limit].rstrip(".,;:!?") + "..."

SYSTEM_PROMPT = """Ты классификатор контента для личной библиотеки пользователя.
Задача: определить ТЕМАТИЧЕСКУЮ категорию (о чём контент) и краткое описание на русском.

ГЛАВНОЕ: Категория = СОДЕРЖАНИЕ, а НЕ тип файла (фото/видео/ссылка не тема). Бери точную категорию ИЗ СПИСКА существующих ниже. Новую создавай ТОЛЬКО если ни одна не подходит хотя бы примерно (синонимы не создавай: «Товары» вместо «Продажи» нельзя).

КАК РАСПОЗНАВАТЬ:
- ПРО АНИМЕ: релиз/сезон/студия/серия/эпизод конкретного аниме → «Новости аниме». ТОЛЬКО если в тексте ЯВНО про аниме/мангу/аниме-студию. В противном случае «Новости аниме» ЗАПРЕЩЕНО.
- ПРО КИНО: фильм, премьера кино, актёр, кинофраншиза (Гарри Поттер, Marvel, Star Wars как фильмы) → «Новости кино» / «Кино».
- ПРО КОМИКС: новость про комикс (DC/Marvel, ваншот, выход в комиксшопах) → «Новости» (комикс НЕ «Кино» и НЕ «Новости кино», если речь не о кино-экранизации).
- РИСУНОК/ТВОРЧЕСТВО: рисунок, арт, фан-арт, скетч (+ в аниме-стиле), фанатская анимация/работа (фанат «сделал/создал/нарисовал») → «Арты». Это НЕ «Новости» и НЕ «Новости аниме».
- ВИДЕОИГРЫ: видеоигра, консоль, моды, гейминг → «Игры».
- ТЕХНОЛОГИИ: гаджеты, софт, наука, роботы → «Технологии».
- ОБУВЬ/ОДЕЖДА: кроссовки/обувь → «Кроссовки»/«Обувь»; одежда/носки/гольфы → «Одежда». Это НЕ «Продажи».
- ПРОДАЖИ: только если пост = собственное объявление «продам/куплю/торг/б/у/доставка». Обзор/новость/подборка товара с ценой → тематическая категория по предмету.
- «Заметки» — ТОЛЬКО личные заметки пользователя (без пересланного контента).
- Всё прочее — обычные новости/товары/бренды/подборки → «Новости».

ПРИМЕРЫ-ЛОВУШКИ:
- БИОНИКЛЫ (Bionicle) — конструктор, НЕ аниме. «Фанат „Биониклов“ сделал анимации с Тоа Мата, работал 3 года» → «Арты», НЕ «Новости аниме».
- «Странный Эл появится в ваншоте World's Weirdest (DC Comics)» → «Новости», НЕ «Новости аниме», НЕ «Новости кино».
- Продается PSP за 6000₽ → «Продажи», НЕ «Обувь».
- Пост про Носки моделей No Show/Saucony → «Одежда»/«Обувь», НЕ «Продажи».
- «Нейросеть создала новую часть Гарри Поттера» → «Новости кино». «Нейросеть создала новую часть поттерианы» — манга/аниме НЕ «Новости кино».

- ЮМОР: мем, шутка, ироничный/абсурдный пост, смешное видео с животными или людьми → «Смешное» (если такая категория есть).
- ИИ И КОД: нейросети, ИИ-агенты, программирование, промпты → «программирование и ИИ» (если такая категория есть), гаджеты и железо → «Технологии».

ИЗ ЧЕГО СОСТОИТ ВХОД (поля могут отсутствовать):
- «Текст поста» — подпись автора, уже без подвалов каналов.
- «Изображение» / «Кадр из видео» — описание от модели зрения, «Текст на изображении» — надписи с картинки. Если подписи нет, классифицируй ПО КАРТИНКЕ: мем → юмор, рисунок → арты, скриншот игры → игры и т.д.
- «Речь в видео/аудио» — расшифровка звука.
- «Превью ссылки», «Кнопки», «Хэштеги», «Файл», «Опрос» — дополнительные подсказки о теме.
- «Похожие посты из архива» — как САМ пользователь разложил похожие посты. Это самый надёжный ориентир: если пост о том же — бери ту же категорию.
- «Канал раньше сохранялся в» — куда обычно попадали посты из этого канала (подсказка, не закон).

ПЕРЕСЛАННЫЙ КОНТЕНТ: ссылки на каналы/чаты/ботов, «Подписаться», «private», «Предложить пост», «©», счётчики — это мусор, категория и summary только о сути.

ПРАВИЛА ВЫВОДА:
- Весь вывод ТОЛЬКО на русском, без эмодзи, кавычек, точек в конце (категория), скобок.
- Summary: максимум 300 символов, точно по содержанию (не пересочиняй факты, не меняй объекты). Если есть только картинка — опиши, что на ней и о чём надпись. Не пиши «пост», «ссылка на канал», «пересланное сообщение».
- confidence: насколько уверен в категории, от 0 до 1 (0.9 — очевидно, 0.5 — сомневаешься между вариантами, 0.3 — почти не за что зацепиться).
- alternatives: 0–2 других категории из списка, которые тоже могли бы подойти (пусто, если уверен).
- НИЧЕГО кроме валидного JSON, без пояснений.

Формат ответа ТОЛЬКО JSON:
{"category": "Тема категории", "summary": "описание сути контента, максимум 300 символов", "confidence": 0.8, "alternatives": []}
"""

USE_COMPACT_PROMPT = __import__("os").getenv("GROQ_COMPACT_PROMPT", "0").strip() == "1"

# Сжатая версия того же промпта (~вдвое короче → больше постов в сутки на бесплатном
# лимите). Включается GROQ_COMPACT_PROMPT=1 после сравнения качества на архиве.
SYSTEM_PROMPT_COMPACT = """Классифицируй пост из личной библиотеки: тематическая категория (О ЧЁМ пост, не тип файла) + краткое описание на русском.
Категорию бери ИЗ СПИСКА пользователя; новую — только если ничего не подходит даже примерно (синонимы не создавай).

Темы:
- аниме/манга/аниме-студия явно упомянуты → «Новости аниме» (иначе эта категория запрещена; Бионикл — конструктор, не аниме)
- фильм, премьера, актёр, кинофраншиза → «Новости кино»/«Кино»; комикс (DC/Marvel, ваншот) → «Новости», если не про экранизацию
- рисунок, арт, фан-арт, фанатская работа/анимация («фанат сделал») → «Арты»
- видеоигры, консоли, моды → «Игры»; гаджеты, софт, наука, роботы → «Технологии»
- нейросети, ИИ-агенты, код, промпты → «программирование и ИИ» (если есть)
- мем, шутка, абсурд, смешное видео с животными/людьми → «Смешное» (если есть)
- обувь → «Кроссовки»/«Обувь», одежда/носки → «Одежда» (не «Продажи»)
- «Продажи» — только своё объявление «продам/куплю/б/у»; обзор товара с ценой — по теме предмета
- «Заметки» — только личные заметки без пересланного; прочие новости/бренды/подборки → «Новости»

Вход (поля могут отсутствовать): «Текст поста»; «Изображение»/«Кадр из видео» — описание картинки (без подписи классифицируй по ней: мем → юмор, рисунок → арты, скриншот игры → игры); «Речь» — расшифровка звука; превью ссылки, кнопки, хэштеги, файл, опрос — подсказки; «Похожие посты из архива» — как САМ пользователь разложил похожее, самый надёжный ориентир; «Канал раньше сохранялся в» — подсказка, не закон.
Ссылки на каналы/ботов, «Подписаться», «private», «©», счётчики — мусор, не тема.

Вывод — только валидный JSON на русском, без эмодзи и пояснений:
{"category": "Категория", "summary": "суть поста до 300 символов, без выдумок; для картинки — что на ней и о чём надпись; не пиши «пост», «пересланное»", "confidence": 0.9, "alternatives": []}
confidence: 0.9 очевидно, 0.5 сомневаешься, 0.3 не за что зацепиться. alternatives: 0–2 других категории из списка."""

CONTENT_TYPE_LABELS = {
    "photo": "фото",
    "video": "видео/гифка",
    "video_note": "видео-кружок",
    "animation": "анимация/гифка",
    "audio": "аудио",
    "voice": "голосовое сообщение",
    "document": "файл/документ",
    "text": "текст",
}


def _extract_json(raw: str) -> dict | None:
    raw = re.sub(r"<think>.*?</think>", "", raw or "", flags=re.S).strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
        raw = raw.rsplit("```", 1)[0].strip()

    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except Exception:
        return None


def _clean(value: str) -> str:
    value = value.strip()
    value = re.sub(r"[\"\'()\[\]]", "", value)
    value = re.sub(r"^[\s.\-=:;,!?]+|[\s.\-=:;,!?]+$", "", value)
    value = re.sub(r"[\U0001F300-\U0001FAFF\u2600-\u27BF]", "", value)
    return value.strip()


def _parse(content: str, fallback_text: str) -> dict:
    data = _extract_json(content)
    if not data:
        return {"category": "Другое", "summary": normalize_summary(fallback_text)}

    category = _clean(str(data.get("category", "")))
    if not category:
        category = "Другое"
    else:
        category = category[:40]

    summary = _clean(str(data.get("summary", "")))
    if not summary:
        summary = normalize_summary(fallback_text)
    else:
        summary = normalize_summary(summary)

    try:
        confidence = float(data.get("confidence", 0.8))
    except (TypeError, ValueError):
        confidence = 0.8
    confidence = max(0.0, min(1.0, confidence))
    alts = []
    raw_alts = data.get("alternatives") or []
    if isinstance(raw_alts, list):
        for a in raw_alts:
            a = _clean(str(a))[:40]
            if a and a != category and a not in alts:
                alts.append(a)
    return {"category": category, "summary": summary, "confidence": confidence, "alternatives": alts[:2]}


def _connector():
    """Коннектор с учётом прокси (socks5/http) или None для прямого соединения."""
    if not GROQ_PROXY:
        return None
    if GROQ_PROXY.startswith("socks"):
        from aiohttp_socks import ProxyConnector

        return ProxyConnector.from_url(GROQ_PROXY)
    return aiohttp.ProxyConnector.from_url(GROQ_PROXY)


# Groq на лимитах токенов прямо пишет, сколько ждать: «Please try again in 20.4s».
# Раньше мы это игнорировали и ждали фиксированные 30 с (а из-за бага с глобалкой
# не ждали вовсе) — отсюда каскад 429. Теперь уважаем подсказку Groq.
_RETRY_IN_RE = re.compile(
    r"try again in\s*(?:(\d+)h)?\s*(?:(\d+)m(?!s))?\s*(?:([\d.]+)s)?\s*(?:([\d.]+)ms)?", re.I
)


def retry_after_seconds(err_text: str) -> float | None:
    """«try again in 1h2m3.5s» / «6m2s» / «20.4s» / «337ms» → секунды; None, если подсказки нет."""
    m = _RETRY_IN_RE.search(err_text or "")
    if not m or not any(m.groups()):
        return None
    h, mi, s, ms = m.groups()
    try:
        return int(h or 0) * 3600 + int(mi or 0) * 60 + float(s or 0) + float(ms or 0) / 1000
    except ValueError:
        return None


def is_daily_limit(err_text: str) -> bool:
    """Суточный лимит Groq (запросы/токены в день) — ждать минутами-часами, а не секундами."""
    return bool(re.search(r"per day|\bTPD\b|\bRPD\b", err_text or "", re.I))


def _retry_after_sec(err_text: str, default: float) -> float:
    """Сколько ждать перед повтором: по подсказке Groq, иначе default."""
    val = retry_after_seconds(err_text)
    if val is None:
        return default
    # +2 c запаса и потолок 90 c: минутное окно всё равно может требовать полного.
    return max(3.0, min(val + 2.0, 90.0))


def _is_token_limit(err_text: str) -> bool:
    """Лимит токенов (ITPM/OTPM) — окно минутное, ждать заметно дольше."""
    t = err_text or ""
    return bool(
        re.search(r"tokens per minute|\bITPM\b|\bOTPM\b|Request too large|reduce max_tokens", t, re.I)
    )


def _reasoning_effort(model: str) -> str:
    """qwen3 умеет отключать рассуждения («none»), gpt-oss — нет (минимум «low»)."""
    return "low" if "gpt-oss" in (model or "") else "none"


# ── Запасные бесплатные провайдеры: Gemini, затем Cloudflare ────────────────
# Включаются, когда у обеих моделей Groq кончился запас или они перегружены.
# Денег не берут: без привязанной карты на лимите просто отвечают ошибкой —
# тогда провайдер молчит до своего суточного сброса.
CF_ENABLED = bool(CF_ACCOUNT_ID and CF_API_TOKEN)
CF_BASE_URL = "https://api.cloudflare.com/client/v4"
GEMINI_ENABLED = bool(GEMINI_API_KEY)
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
_EXTRA_STATE: dict[str, dict] = {
    "gemini": {"blocked_until": 0.0, "ok": 0, "fail": 0, "last_error": ""},
    "cloudflare": {"blocked_until": 0.0, "ok": 0, "fail": 0, "last_error": ""},
}
CF_STATS = _EXTRA_STATE["cloudflare"]
GEMINI_STATS = _EXTRA_STATE["gemini"]


def _next_reset(hour_utc: int) -> float:
    """Ближайшие сутки в hour_utc:01 UTC (Cloudflare — 00 UTC, Gemini — полночь по Тихоокеанскому ≈ 08 UTC)."""
    import datetime as _dt

    now = _dt.datetime.now(_dt.timezone.utc)
    nxt = now.replace(hour=hour_utc, minute=1, second=0, microsecond=0)
    if nxt <= now:
        nxt += _dt.timedelta(days=1)
    return nxt.timestamp()


def _extra_enabled(name: str) -> bool:
    return CF_ENABLED if name == "cloudflare" else GEMINI_ENABLED


def extra_available(name: str) -> bool:
    import time as _t

    return _extra_enabled(name) and _t.time() >= _EXTRA_STATE[name]["blocked_until"]


def cf_available() -> bool:
    return extra_available("cloudflare")


def _strip_think(text: str) -> str:
    return re.sub(r"<think>.*?</think>", "", text or "", flags=re.S).strip()


async def _extra_chat(name: str, messages: list[dict], *, json_mode: bool, temperature: float,
                      max_tokens: int, timeout: int) -> tuple[str, str]:
    """(content, err) от запасного провайдера (OpenAI-совместимый API)."""
    import time as _t

    st = _EXTRA_STATE[name]
    has_image = any(isinstance(m.get("content"), list) for m in messages)
    msgs = [dict(m) for m in messages]
    if name == "cloudflare":
        model = CF_VISION_MODEL if has_image else CF_TEXT_MODEL
        url = f"{CF_BASE_URL}/accounts/{CF_ACCOUNT_ID}/ai/v1/chat/completions"
        key = CF_API_TOKEN
        if "qwen3" in model and msgs and isinstance(msgs[-1].get("content"), str):
            msgs[-1]["content"] += " /no_think"  # qwen3: без рассуждений — быстрее и дешевле
        payload = {"model": model, "messages": msgs, "temperature": temperature, "max_tokens": max_tokens + 300}
    else:
        model = GEMINI_MODEL
        url = f"{GEMINI_BASE_URL}/chat/completions"
        key = GEMINI_API_KEY
        payload = {"model": model, "messages": msgs, "temperature": temperature, "max_tokens": max_tokens + 300}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
    label = "Cloudflare" if name == "cloudflare" else "Gemini"
    try:
        async with aiohttp.ClientSession(connector=_connector()) as session:
            async with session.post(url, json=payload, headers={"Authorization": f"Bearer {key}"},
                                    timeout=aiohttp.ClientTimeout(total=timeout)) as resp:
                body = await resp.text()
                if resp.status != 200:
                    err = f"{label} HTTP {resp.status}: {body[:400]}"
                    if name == "cloudflare" and (resp.status == 429 or re.search(r"neuron|daily|allocation|4006", body, re.I)):
                        st["blocked_until"] = _next_reset(0)       # суточный запас — до 00:00 UTC
                    elif name == "gemini" and resp.status == 429:
                        # суточная квота — до полуночи по Тихоокеанскому; минутная — подождать минуту
                        st["blocked_until"] = _next_reset(8) if re.search(r"PerDay|per day", body, re.I) else _t.time() + 60
                    st["fail"] += 1
                    st["last_error"] = err[:200]
                    logger.warning("%s", err)
                    return "", err
                data = json.loads(body)
    except Exception as e:
        st["fail"] += 1
        st["last_error"] = str(e)[:200]
        logger.warning("%s: ошибка %s", label, e)
        return "", f"{label}: {e}"
    content = _strip_think(((data.get("choices") or [{}])[0].get("message") or {}).get("content") or "")
    _events.append((_t.time(), USAGE_SOURCE.get(), f"{name}:{model}", _usage_tokens(data)))
    if not content:
        st["fail"] += 1
        return "", f"{label} вернул пустой ответ"
    st["ok"] += 1
    return content, ""


async def _cf_chat(messages, *, json_mode, temperature, max_tokens, timeout):
    return await _extra_chat("cloudflare", messages, json_mode=json_mode, temperature=temperature,
                             max_tokens=max_tokens, timeout=timeout)


async def _groq_chat(
    messages: list[dict],
    *,
    model: str = GROQ_MODEL,
    json_mode: bool = False,
    temperature: float = 0.1,
    max_tokens: int = 800,
    timeout: int | None = None,
    retries: int | None = None,
    fallback_model: str | None = None,
) -> str:
    """Вызов OpenAI-совместимого chat/completions (Groq). '' при ошибке.

    fallback_model: при лимите токенов (429/413) основной модели сразу пробуем
    эту модель — у Groq лимиты считаются отдельно на каждую модель, поэтому
    запасная почти всегда свободна, и пост не ждёт по 30–90 с."""
    global LLM_LAST_ERROR
    if json_mode and not any(
        "json" in str(m.get("content", "")).lower() for m in messages
    ):
        # Groq отклоняет json_object, если слова «json» нет в сообщениях (HTTP 400).
        messages = [{"role": "system", "content": "Отвечай строго валидным JSON."}] + list(messages)
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "reasoning_effort": _reasoning_effort(model),
        "max_tokens": max_tokens,
        "stream": False,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    total = timeout or GROQ_TIMEOUT_SEC
    # Groq часто падает транзиентно (429/502/пустой ответ) — пробуем 3 раза:
    # и для внешнего /api/chat (расширение), и для бота. С корректной паузой
    # на лимите токенов это 3 попытки за ~2 минуты, а не 3 выстрела за 4 с.
    if retries is None:
        retries = 3

    async def _post() -> tuple[str, str]:
        """Возвращает (content, err). err пустой при успехе.

        ВАЖНО: раньше ошибка писалась в глобальную LLM_LAST_ERROR прямо здесь, во
        вложенной функции БЕЗ `global` — то есть менялась локальная копия, и глобалка
        оставалась пустой. Из-за этого проверка «429/413» перед повтором всегда была
        ложной, пауза не делалась, и лимит токенов превращался в каскад 429. Теперь
        ошибка возвращается наружу и используется локально: её не может затереть
        параллельный запрос бота.
        """
        connector = _connector()
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.post(
                f"{GROQ_BASE_URL}/chat/completions",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=total),
            ) as resp:
                body = await resp.text()
                if resp.status != 200:
                    err = f"Groq HTTP {resp.status}: {body[:600]}"
                    logger.warning(
                        "Groq chat/completions %s (model=%s, status=%s): %.500s",
                        GROQ_BASE_URL, model, resp.status, body,
                    )
                    return "", err
                if not body:
                    logger.warning("Groq chat/completions вернул пустое тело (model=%s)", model)
                    return "", "Groq вернул пустое тело"
                try:
                    data = json.loads(body)
                except Exception:
                    logger.warning("Groq chat/completions: не-JSON ответ (model=%s): %.400s", model, body)
                    return "", f"Groq вернул не-JSON: {body[:300]}"
                _note_usage(payload["model"], _usage_tokens(data))
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                if not content:
                    err = (
                        f"Groq вернул пустой content (model={model}, "
                        f"finish={data.get('choices',[{}])[0].get('finish_reason','?')})"
                    )
                    logger.warning("Groq chat/completions: пустой content в ответе (model=%s)", model)
                    return "", err
                return content, ""

    last_err = ""
    used_fallback = False
    for attempt in range(retries):
        try:
            async with _LLM_LOCK:
                # Жёсткий потолок: через socks-прокси ClientTimeout может не сработать,
                # а зависший вызов держит _LLM_LOCK и вешает все последующие посты.
                content, err = await asyncio.wait_for(_post(), total + 20)
            if content:
                LLM_LAST_ERROR = ""
                return content
            last_err = err
            _note_limit_error(payload["model"], err)
            # 400/401/403/404 — ошибка запроса, повтор ничего не изменит
            # (раньше так впустую уходило по 3 запроса на каждую авто-раскладку).
            if re.search(r"Groq HTTP (400|401|403|404)\b", err or ""):
                logger.warning("Groq: ошибка запроса, без повторов: %.200s", err)
                break
            if (
                fallback_model
                and not used_fallback
                and fallback_model != payload["model"]
                and (_is_token_limit(err) or re.search(r"\b429\b|\b413\b", err or ""))
            ):
                logger.info("Groq: %s упёрлась в лимит — пробую запасную %s", payload["model"], fallback_model)
                used_fallback = True
                payload["model"] = fallback_model
                payload["reasoning_effort"] = _reasoning_effort(fallback_model)
                payload["max_tokens"] = max_tokens + 600  # запас на рассуждения gpt-oss
                continue
            if is_daily_limit(err):
                # суточный лимит за секунды не откроется — не тратим попытки и время
                logger.warning("Groq: суточный лимит модели %s: %.200s", payload["model"], err)
                break
        except asyncio.TimeoutError:
            last_err = "таймаут Groq"
            LLM_LAST_ERROR = last_err
            logger.warning("Groq chat/completions: таймаут (model=%s, attempt=%s/%s)", model, attempt + 1, retries)
        except Exception as e:
            last_err = f"исключение: {e}"
            LLM_LAST_ERROR = last_err
            logger.warning("Groq chat/completions: ошибка (model=%s, attempt=%s/%s): %s", model, attempt + 1, retries, e)
        if attempt + 1 >= retries:
            break
        # Лимит токенов (ITPM/OTPM) — окно минутное, повтор сразу бессмыслен.
        # Groq сам сообщает, сколько ждать («try again in 20.4s»); для токенных
        # лимитов берём максимум из его подсказки и 30 с, потолок 90 с.
        if _is_token_limit(last_err) or re.search(r"\b429\b|\b413\b", last_err):
            if _is_token_limit(last_err):
                wait = max(30.0, _retry_after_sec(last_err, 30.0))
            else:
                wait = _retry_after_sec(last_err, 5.0)
            logger.info("Groq: лимит токенов/rate-limit, жду %.0f с перед повтором (model=%s)", wait, model)
            await asyncio.sleep(wait)
        else:
            await asyncio.sleep(1.5)
    # Groq не смог (суточный лимит/перегрузка обеих моделей) — бесплатный третий
    # уровень. Фоновой очереди он не положен: его суточный запас — для новых постов.
    if USAGE_SOURCE.get() != "bg" and (
        is_daily_limit(last_err) or re.search(r"\b429\b|\b413\b|таймаут|HTTP 5\d\d", last_err or "")
    ):
        for name in ("gemini", "cloudflare"):
            if not extra_available(name):
                continue
            content, err = await _extra_chat(name, messages, json_mode=json_mode, temperature=temperature,
                                             max_tokens=max_tokens, timeout=min(total, 90))
            if content and (not json_mode or _extract_json(content) is not None):
                logger.info("%s выручил (Groq: %.120s)", name, last_err)
                LLM_LAST_ERROR = ""
                return content
            last_err = f"{last_err}; {err or name + ': не JSON'}"
    LLM_LAST_ERROR = last_err or "Groq не ответил"
    return ""


async def llm_chat(
    messages: list[dict],
    *,
    model: str = GROQ_MODEL,
    json_mode: bool = False,
    temperature: float = 0.1,
    max_tokens: int = 800,
    timeout: int | None = None,
) -> str:
    """Публичная обёртка над _groq_chat для внешних клиентов (/api/chat). Возвращает '' при ошибке."""
    USAGE_SOURCE.set("ext")
    return await _groq_chat(
        messages,
        model=model,
        json_mode=json_mode,
        temperature=temperature,
        max_tokens=max_tokens,
        timeout=timeout,
        retries=3,
    )


async def llm_chat_stream(
    messages: list[dict],
    *,
    model: str = GROQ_MODEL,
    json_mode: bool = False,
    temperature: float = 0.1,
    max_tokens: int = 800,
    timeout: int | None = None,
):
    """Стриминговый вызов chat/completions (Groq): асинхронный генератор фрагментов текста.
    Минимум 2 попытки; повторяемся, только если ошибка случилась ДО первого фрагмента
    (иначе клиент уже получил кусок ответа и дублировать его нельзя). При неудаче — RuntimeError."""
    USAGE_SOURCE.set("ext")
    global LLM_LAST_ERROR
    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    total = timeout or GROQ_TIMEOUT_SEC
    retries = 3

    async def _stream_once():
        connector = _connector()
        payload = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
            "reasoning_effort": "none",
            "max_tokens": max_tokens,
            "stream": True,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.post(
                f"{GROQ_BASE_URL}/chat/completions",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=total),
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    # Тело ошибки обязательно тащим дальше: в нём Groq пишет
                    # «try again in 20.4s» и какой именно лимит (ITPM/OTPM).
                    logger.warning("Groq stream status=%s (model=%s): %.400s", resp.status, model, body)
                    raise RuntimeError(f"Groq HTTP {resp.status}: {body[:600]}")
                async for line in resp.content:
                    line = line.decode("utf-8", "replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        chunk = json.loads(data)
                    except Exception:
                        continue
                    _note_usage(model, _usage_tokens(chunk))
                    try:
                        delta = chunk["choices"][0]["delta"].get("content")
                    except Exception:
                        delta = None
                    if delta:
                        LLM_LAST_ERROR = ""
                        yield delta

    last_err = ""
    for attempt in range(retries):
        started = False
        try:
            agen = _stream_once()
            async with _LLM_LOCK:
                try:
                    while True:
                        # Жёсткий потолок на один шаг стрима: через socks-прокси
                        # ClientTimeout может не сработать, а зависший вызов держит
                        # _LLM_LOCK и вешает все последующие посты. asyncio.wait_for
                        # принимает только корутину, поэтому берём по __anext__().
                        delta = await asyncio.wait_for(agen.__anext__(), total + 20)
                        started = True
                        yield delta
                except StopAsyncIteration:
                    pass
            if not started:
                last_err = "Groq stream вернул пустой ответ (model=%s)" % model
                LLM_LAST_ERROR = last_err
                logger.warning("Groq stream: пустой ответ (model=%s)", model)
                if attempt == retries - 1:
                    raise RuntimeError(last_err)
            else:
                LLM_LAST_ERROR = ""
                return
        except asyncio.TimeoutError:
            try:
                await agen.aclose()
            except Exception:
                pass
            last_err = "таймаут Groq (stream)"
            LLM_LAST_ERROR = last_err
            logger.warning("Groq stream: таймаут (model=%s, attempt=%s/%s)", model, attempt + 1, retries)
            if started or attempt == retries - 1:
                raise RuntimeError(last_err)
        except RuntimeError as e:
            # Сюда приходит и наш «Groq HTTP 429: {...try again in Ns...}»,
            # поэтому текст ошибки кладём в локальную переменную: глобальная
            # LLM_LAST_ERROR тут обновляется корректно (global объявлен выше),
            # но её могут затереть параллельные запросы бота — решение о паузе
            # принимаем по last_err.
            last_err = str(e)
            LLM_LAST_ERROR = last_err
            _note_limit_error(model, last_err)
            if started:
                raise
            if attempt == retries - 1:
                raise
        except Exception as e:
            last_err = f"исключение в stream: {e}"
            LLM_LAST_ERROR = last_err
            logger.warning("Groq stream: ошибка (model=%s, attempt=%s/%s): %s", model, attempt + 1, retries, e)
            if started or attempt == retries - 1:
                raise
        if attempt + 1 >= retries:
            break
        # 413/429 = минутное окно ITPM/OTPM закрыто: повтор сразу бессмыслен.
        # Ждём столько, сколько велит Groq (потолок 90 с), пауза вне _LLM_LOCK.
        if _is_token_limit(last_err) or re.search(r"\b429\b|\b413\b", last_err):
            if _is_token_limit(last_err):
                wait = max(30.0, _retry_after_sec(last_err, 30.0))
            else:
                wait = _retry_after_sec(last_err, 5.0)
            logger.info("Groq stream: лимит токенов/rate-limit, жду %.0f с перед повтором (model=%s)", wait, model)
            await asyncio.sleep(wait)
    LLM_LAST_ERROR = last_err or "Groq stream не ответил"
    raise RuntimeError(LLM_LAST_ERROR)


ORGANIZE_SYSTEM_PROMPT = """Ты органайзер личной медиатеки. Тебе дадут список категорий пользователя.

Задача: сгруппировать тематически близкие категории в папки и объединить почти-дубликаты.

ПРАВИЛА:
- «folders»: имя папки → список категорий внутри неё. Папка объединяет 2+ категории одной широкой темы (например «Развлечения»: «Кино», «Сериалы», «Игры»).
- ПАПКИ В ПАПКАХ — ЭТО НОРМАЛЬНО: иерархия любой глубины разрешена. Пример: папка «Развлечения» содержит «Игры», «Новости аниме», «Смешное» И вложенную папку «Кино и сериалы» (записывается слэшем «Развлечения/Кино и сериалы»), внутри которой отдельные категории «Кино», «Сериалы» и «Новости кино». Если есть «Развлечения» — кино, сериалы, игры и аниме-контент всегда клади ВНУТРЬ него.
- ОДЕЖДА/ОБУВЬ: «Кроссовки», «Обувь» и «Одежда» — близкие, их допустимо положить в ОДНУ папку «Одежда и обувь», но НЕ объединять в одну категорию (обувь≠одежда).
- АНИМЕ-НОВОСТИ: «Новости аниме» — отдельная категория, НЕ объединять с «Новости» (кино-новости и аниме-новости разные). «Новости кино» ≠ «Новости».
- Вложенность: имя папки можно записать с прямым слэшем («Развлечения/Онлайн») — это подпапка внутри «Развлечения».
- НЕ создавай папку ради одной категории: если категории не с кем группировать — вообще не включай её в «folders».
- Каждая категория упоминается максимум один раз (или в folders, или в merge, или нигде).
- «merge»: ТОЛЬКО почти-дубликаты, когда одна категория полностью поглощает другую и это не меняет смысл: («Игры ПК» → «Игры», «Новости кино» → «Кино» только если такая категория существует). Целевая категория должна быть из исходного списка.

ЗАПРЕЩЕНО:
- Объединять в «Другое» — категория «Другое» никогда не должна поглощать другие категории.
- Объединять категории РАЗНОЙ тематики: «Вакансии» и «Продажи» — РАЗНЫЕ темы, не объединять. «Кроссовки» и «Одежда» — РАЗНЫЕ темы (обувь и одежда): можно в одну папку, но не в одну категорию. «Модели Gunpla» и «Другое» — НЕ объединять.
- НЕ создавай папку, имя которой совпадает с одной из категорий пользователя: папка «Новости» при существующей категории «Новости» — запрещено (это вызывает путаницу).
- Если сомневаешься, что это действительно почти-дубликат — не объединяй. Лучше оставить как есть.
- Все значения ТОЛЬКО на русском, без кавычек, эмодзи, точек в конце.

Формат ответа ТОЛЬКО валидный JSON:
{"folders": {"Папка": ["Категория1", "Категория2"], ...}, "merge": {"Источник": "Куда"}}
"""


async def organize(categories: list[str]) -> dict:
    USAGE_SOURCE.set("service")
    result = {"folders": {}, "merge": {}}
    messages = [
        {"role": "system", "content": ORGANIZE_SYSTEM_PROMPT},
        {
            "role": "user",
            "content": "Категории пользователя:\n" + json.dumps(categories, ensure_ascii=False),
        },
    ]
    raw = await _groq_chat(messages, json_mode=True, max_tokens=1000, timeout=45)
    if not raw:
        return result

    parsed = _extract_json(raw)
    if not parsed:
        return result

    valid = set(categories)
    used = set()

    folders_raw = parsed.get("folders") or {}
    if isinstance(folders_raw, dict):
        for folder, lst in folders_raw.items():
            folder = _clean(str(folder)).strip("/")
            if not folder:
                continue
            members = []
            if isinstance(lst, list):
                for member in lst:
                    member = _clean(str(member))
                    if member in valid and member not in used:
                        members.append(member)
                        used.add(member)
            if members:
                result["folders"][folder] = members

    merge_raw = parsed.get("merge") or {}
    if isinstance(merge_raw, dict):
        for src, dst in merge_raw.items():
            src = _clean(str(src))
            dst = _clean(str(dst))
            if src in valid and dst in valid and src != dst and src not in used and dst not in used:
                result["merge"][src] = dst
                used.add(src)
                used.add(dst)

    return result


SUBGROUPS_SYSTEM_PROMPT = """Ты группируешь посты внутри одной категории пользователя по близким темам.
Тебе дадут категорию и список её постов в формате [{"id": число, "s": "краткое описание"}, ...].

Задача: объединить посты в тематические группы (группы = потенциальные подпапки внутри категории).

ПРАВИЛА:
- Одна группа = одна узкая тема (например для категории «Игры»: «PS5», «Retroid/эмуляция», «моды Cyberpunk», «One Piece» для аниме).
- В каждой группе должно быть НЕ МЕНЕЕ заданного минимума постов (MIN). Меньше не создавай.
- Посты, которые не подходят ни к одной группе, НЕ включай никуда.
- Каждый пост может быть максимум в одной группе.
- Название темы: короткое, на русском, без слэшей, кавычек, эмодзи, точек.

Формат ответа ТОЛЬКО валидный JSON:
{"группа": [айди1, айди2, ...], "другая группа": [...]}
"""


async def propose_subgroups(category: str, items: list[dict], min_count: int) -> dict:
    """Возвращает {"тема": [id, ...]}. Группы меньше min_count отбрасываются."""
    USAGE_SOURCE.set("service")
    result: dict = {}
    if not items or min_count < 2:
        return result
    limited = items[:120]
    truncated = len(items) > len(limited)
    user_content = (
        f"Категория: {category}\nMIN (минимум постов в группе): {min_count}\n"
        f"Посты: {json.dumps([{'id': it['id'], 's': (it.get('summary') or '')[:80]} for it in limited], ensure_ascii=False)}"
        + ("\n(показаны первые 120 постов из большего числа)" if truncated else "")
    )
    messages = [
        {"role": "system", "content": SUBGROUPS_SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]
    raw = await _groq_chat(messages, json_mode=True, max_tokens=1200, timeout=45)
    if not raw:
        return result

    parsed = _extract_json(raw)
    if not parsed:
        return result

    valid_ids = {it["id"] for it in items}
    used: set = set()
    for topic, ids in parsed.items():
        topic = _clean(str(topic)).strip("/")
        if not topic:
            continue
        clean_ids = []
        if isinstance(ids, list):
            for x in ids:
                try:
                    iid = int(x)
                except Exception:
                    continue
                if iid in valid_ids and iid not in used:
                    clean_ids.append(iid)
                    used.add(iid)
        if len(clean_ids) >= min_count:
            result[topic] = clean_ids
    return result


def _downscale(image_bytes: bytes, max_dim: int = 768) -> bytes:
    """Уменьшаем картинку для vision: у qwen на free-тарифе всего 7000 входных токенов/мин,
    а картинка 1024px съедает ~2000 из них. 768px вдвое дешевле, текст на мемах ещё читается."""
    try:
        from io import BytesIO
        from PIL import Image
        im = Image.open(BytesIO(image_bytes))
        im.thumbnail((max_dim, max_dim), Image.LANCZOS)
        if im.mode != "RGB":
            im = im.convert("RGB")
        buf = BytesIO()
        im.save(buf, format="JPEG", quality=82)
        return buf.getvalue()
    except Exception:
        return image_bytes


VISION_PROMPT = """Ты описываешь изображение из Telegram-поста, чтобы потом разложить пост по темам.
Ответь ТОЛЬКО валидным JSON:
{"genre": "...", "scene": "...", "text": "...", "known": "..."}

- genre — ровно одно из: арт/рисунок, кадр из аниме, кадр из фильма/сериала, скриншот игры, скриншот приложения/сайта/переписки, реальное фото, мем, комикс, инфографика/схема, товар/реклама, другое. Если на {kind} нарисованная картинка (в т.ч. в стиле аниме) — «арт/рисунок»; мем = картинка с шуточной подписью или шаблон мема.
- scene — что изображено, по-русски, до 25 слов: объекты, персонажи, место, действие, настроение.
- text — читаемый текст на изображении ДОСЛОВНО на языке оригинала (подписи мема, заголовки, текст скриншота), до 300 символов; пусто, если текста нет. Водяные знаки и ники авторов не нужны.
- known — узнаваемая игра, аниме, фильм, персонаж, бренд или устройство, только если уверен; иначе пусто."""


def _render_vision(data: dict) -> str:
    genre = _clean(str(data.get("genre", "")))[:40]
    scene = _clean(str(data.get("scene", "")))[:220]
    text = re.sub(r"\s+", " ", str(data.get("text", "") or "")).strip()[:300]
    known = _clean(str(data.get("known", "")))[:80]
    head = "; ".join(x for x in (genre, scene) if x)
    lines = [head] if head else []
    if text:
        lines.append(f"Текст на изображении: «{text}»")
    if known:
        lines.append(f"Узнаётся: {known}")
    return "\n".join(lines)


# Размер картинки для vision. Замер 2026-11-21: кадр 768px ≈ 2 100 токенов на запрос
# (из суточных 200 000 у qwen). Новые посты — 768 (мелкий текст мемов читается),
# фоновый добор архива ставит 512 (≈ вдвое дешевле: токены растут с площадью).
VISION_DIM = contextvars.ContextVar("vision_dim", default=768)


async def describe_image(image_bytes: bytes, kind: str = "изображении") -> str:
    """Описание картинки через vision-модель: жанр, сцена, текст на картинке (OCR),
    узнаваемые франшизы. Многострочная строка или '' при ошибке."""
    b64 = base64.b64encode(_downscale(image_bytes, VISION_DIM.get())).decode()
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": VISION_PROMPT.replace("{kind}", kind)},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ],
        }
    ]
    raw = await _groq_chat(
        messages, model=GROQ_VISION_MODEL, json_mode=True, temperature=0.2, max_tokens=400, timeout=90, retries=2
    )
    if not raw:
        return ""
    data = _extract_json(raw)
    if isinstance(data, dict):
        return _render_vision(data)
    return _clean(raw)[:200]


CLARIFY_ACTION_PROMPT = """Ты помощник по личной медиатеке пользователя. Тебе дадут ПРОСЬБУ пользователя и список СУЩЕСТВУЮЩИХ категорий.

Определи, ЧТО хочет пользователь, и верни одно из действий:

- «hint» — пользователь уточняет тему контента (рассказывает, о чём пост): «это носки, а не кроссовки», «биониклы это не аниме», «речь про вакансию на hh.ru», «здесь про консоль PSP», «тут про сериалы». Поле "category" заполняется так:
  1. СНАЧАЛА проверь, есть ли среди СУЩЕСТВУЮЩИХ категорий подходящая по смыслу (например, пользователь сказал «сериалы» — а в списке есть «Кино и сериалы» → верни «Кино и сериалы»; «кроссовки» → «Кроссовки» или «Обувь»). Если подходящая есть — верни ЕЁ ИМЯ (точное, как в списке).
  2. Если среди категорий НЕТ ничего подходящего — верни НОВОЕ имя темы, приведённое к единственному числу с заглавной буквы («сериалы» → «Сериалы», «кино и сериалы» → «Кино и сериалы»).
  3. Пусто — ТОЛЬКО если пользователь не назвал конкретную тему (например «ошибка, перераспознай», «это не то»).
- «move» — пользователь просит Что-ТО ПЕРЕЛОЖИТЬ: «перемести в X», «отправь/кинь это в X», «перенеси в X», «пусть лежит в X», «закинь в X». Поле "category" = имя категории.
- «create_move» — пользователь просит СОЗДАТЬ новую категорию/папку и положить туда контент: «создай категорию/папку X», «создай папку под комиксы», «заведи папку X и отправь это туда», «сделай отдельную категорию для X». Поле "category" = имя новой категории (короткое, по содержанию: «Комиксы», «Новости комиксов», «Семейные видео»), поле "folder" = куда вложить (если сказано: «в Развлечениях», «внутри Одежды») или пусто.
- «rename» — пользователь просит переименовать пост: «назови X», «переименуй в X». Поле "summary" = новое название.

ВАЖНО:
- «создай папку под комиксы» → create_move, category «Комиксы»; «создай папку в Развлечениях под комиксы» → create_move, category «Комиксы», folder «Развлечения».
- Если пользователь назвал категорию, которая УЖЕ есть в списке — это «move», а НЕ «create_move».
- Если пользователь сам назвал тему уточнения («это сериалы», «тут про сериалы», «речь про кино»), но такой категории в списке НЕТ — это всё равно «hint», и в поле "category" укажи эту тему: бот создаст новую категорию.
- Если сомневаешься и это скорее уточнение темы — «hint».

Формат ответа ТОЛЬКО JSON, без пояснений:
{"action": "hint|move|create_move|rename", "category": "", "folder": "", "summary": ""}
"""


async def interpret_clarify(text: str, categories: list[str] | None = None) -> dict:
    """Определяет, команда ли это (создать/переместить/переименовать) или уточнение темы."""
    USAGE_SOURCE.set("service")
    fallback = {"action": "hint", "category": "", "folder": "", "summary": ""}
    cats_line = "Список существующих категорий: " + (", ".join(categories) if categories else "нет данных") + "\n"
    messages = [
        {"role": "system", "content": CLARIFY_ACTION_PROMPT},
        {"role": "user", "content": cats_line + "Просьба пользователя: " + (text or "")[:600]},
    ]
    raw = await _groq_chat(messages, json_mode=True, max_tokens=300, timeout=45)
    if not raw:
        return fallback

    parsed = _extract_json(raw)
    if not isinstance(parsed, dict):
        return fallback
    action = str(parsed.get("action", "")).strip().lower()
    if action not in ("hint", "move", "create_move", "rename"):
        action = "hint"
    category = _clean(str(parsed.get("category", "")))
    folder = _clean(str(parsed.get("folder", ""))).strip("/")
    summary = _clean(str(parsed.get("summary", "")))
    return {"action": action, "category": category, "folder": folder, "summary": summary}


async def categorize(
    text: str,
    content_type: str | None = None,
    source: str | None = None,
    categories: list[str] | None = None,
    examples: list[tuple[str, str]] | None = None,
    channel_prior: list[tuple[str, int]] | None = None,
    model: str | None = None,
    fallback_model: str | None = "",
) -> dict:
    """Категория + summary поста.

    examples — похожие посты из архива [(summary, category)] (как их разложил сам
    пользователь); channel_prior — [(category, n)] куда раньше попадал этот канал.
    Возвращает {category, summary, confidence, alternatives}; при сбое ИИ — llm_ok=False."""
    label = CONTENT_TYPE_LABELS.get(content_type or "", "контент")
    # Порядок важен: Groq кэширует одинаковое НАЧАЛО запроса (у gpt-oss), и кэшированные
    # токены не идут в суточный лимит. Поэтому сначала неизменное (промпт, список
    # категорий), потом то, что меняется от поста к посту.
    parts = []
    if categories:
        parts.append(f"Существующие категории пользователя: {', '.join(categories)}")
    parts.append(f"Тип контента: {label}")
    if source:
        parts.append(f"Источник (канал/чат): {source}")
    if channel_prior:
        parts.append(
            "Канал раньше сохранялся в: "
            + ", ".join(f"{c} ×{n}" for c, n in channel_prior)
        )
    if examples:
        parts.append("Похожие посты из архива:")
        for s, c in examples:
            parts.append(f"- «{_clip_summary(s, 110)}» → {c}")
    parts.append(f"\nКонтент:\n{text[:2500]}")
    user_content = "\n".join(parts)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT_COMPACT if USE_COMPACT_PROMPT else SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    fallback = normalize_summary(text, 300) if text else "Без описания"

    raw = await _groq_chat(
        messages,
        model=model or GROQ_CATEGORIZE_MODEL,
        json_mode=True,
        max_tokens=500,
        timeout=45,
        fallback_model=GROQ_FALLBACK_MODEL if fallback_model == "" else fallback_model,
    )
    if not raw:
        return {"category": "Другое", "summary": fallback, "llm_ok": False}
    out = _parse(raw, fallback)
    out["llm_ok"] = _extract_json(raw) is not None
    return out