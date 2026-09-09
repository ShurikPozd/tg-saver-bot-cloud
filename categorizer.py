import aiohttp
import asyncio
import base64
import json
import re
from config import (
    GROQ_API_KEY,
    GROQ_BASE_URL,
    GROQ_MODEL,
    GROQ_VISION_MODEL,
    GROQ_PROXY,
    GROQ_TIMEOUT_SEC,
)

# Все вызовы ИИ сериализуются локом, чтобы не превышать rate-limits (RPM/OTPM).
_LLM_LOCK = asyncio.Lock()

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

ПЕРЕСЛАННЫЙ КОНТЕНТ: вычитывай приписки каналов («👉», «Подписаться», «©», счётчики) — категория и summary только о сути.

ПРАВИЛА ВЫВОДА:
- Весь вывод ТОЛЬКО на русском, без эмодзи, кавычек, точек в конце (категория), скобок.
- Summary: максимум 300 символов, точно по тексту (не пересочиняй факты, не меняй объекты).
- НИЧЕГО кроме валидного JSON, без пояснений.

Формат ответа ТОЛЬКО:
{"category": "Тема категории", "summary": "описание сути контента, максимум 300 символов"}
"""

CONTENT_TYPE_LABELS = {
    "photo": "фото",
    "video": "видео/гифка",
    "animation": "анимация/гифка",
    "audio": "аудио",
    "voice": "голосовое сообщение",
    "document": "файл/документ",
    "text": "текст",
}


def _extract_json(raw: str) -> dict | None:
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

    return {"category": category, "summary": summary}


def _connector():
    """Коннектор с учётом прокси (socks5/http) или None для прямого соединения."""
    if not GROQ_PROXY:
        return None
    if GROQ_PROXY.startswith("socks"):
        from aiohttp_socks import ProxyConnector

        return ProxyConnector.from_url(GROQ_PROXY)
    return aiohttp.ProxyConnector.from_url(GROQ_PROXY)


async def _groq_chat(
    messages: list[dict],
    *,
    model: str = GROQ_MODEL,
    json_mode: bool = False,
    temperature: float = 0.1,
    max_tokens: int = 800,
    timeout: int | None = None,
) -> str:
    """Вызов OpenAI-совместимого chat/completions (Groq). '' при ошибке."""
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "reasoning_effort": "none",
        "max_tokens": max_tokens,
        "stream": False,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    headers = {"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"}
    total = timeout or GROQ_TIMEOUT_SEC
    retries = 1 if timeout is not None else 2

    async def _post() -> str:
        connector = _connector()
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.post(
                f"{GROQ_BASE_URL}/chat/completions",
                json=payload,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=total),
            ) as resp:
                if resp.status != 200:
                    return ""
                data = await resp.json()
                content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                return content or ""

    for _ in range(retries):
        try:
            async with _LLM_LOCK:
                # Жёсткий потолок: через socks-прокси ClientTimeout может не сработать,
                # а зависший вызов держит _LLM_LOCK и вешает все последующие посты.
                content = await asyncio.wait_for(_post(), total + 20)
            if content:
                return content
        except (asyncio.TimeoutError, Exception):
            continue
    return ""


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

Формат ответа ТОЛЬКО:
{"folders": {"Папка": ["Категория1", "Категория2"], ...}, "merge": {"Источник": "Куда"}}
"""


async def organize(categories: list[str]) -> dict:
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

Формат ответа ТОЛЬКО:
{"группа": [айди1, айди2, ...], "другая группа": [...]}
"""


async def propose_subgroups(category: str, items: list[dict], min_count: int) -> dict:
    """Возвращает {"тема": [id, ...]}. Группы меньше min_count отбрасываются."""
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


def _downscale(image_bytes: bytes, max_dim: int = 1024) -> bytes:
    """Уменьшаем картинку для vision (быстрее, меньше расход ITPM). Оригинал при ошибке."""
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


async def describe_image(image_bytes: bytes) -> str:
    """Короткое русское описание картинки через vision-модель. '' при ошибке."""
    prompt = (
        "Опиши картинку одним предложением на русском. ОБЯЗАТЕЛЬНО начни с точного жанра изображения "
        "одним из слов: 'арт/рисунок' (нарисовано, в т.ч. стиль аниме), 'скриншот/кадр из аниме', "
        "'скриншот/кадр из сериала/фильма', 'реальное фото', 'скетч', 'мем', 'гиф'. "
        "Затем укажи сцену: объект, персонаж, место. Максимум 15 слов, без кавычек, скобок, эмодзи, иностранных слов."
    )
    b64 = base64.b64encode(_downscale(image_bytes)).decode()
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": prompt},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ],
        }
    ]
    raw = await _groq_chat(messages, model=GROQ_VISION_MODEL, json_mode=False, temperature=0.2, max_tokens=200, timeout=90)
    out = _clean(raw)
    return out[:120]


CLARIFY_ACTION_PROMPT = """Ты помощник по личной медиатеке пользователя. Тебе дадут ПРОСЬБУ пользователя и список СУЩЕСТВУЮЩИХ категорий.

Определи, ЧТО хочет пользователь, и верни одно из действий:

- «hint» — пользователь просто уточняет тему контента (рассказывает, о чём пост): «это носки, а не кроссовки», «биониклы это не аниме», «речь про вакансию на hh.ru», «здесь про консоль PSP». Поле "category" — если он назвал подходящую существующую категорию, иначе пусто.
- «move» — пользователь просит Что-ТО ПЕРЕЛОЖИТЬ: «перемести в X», «отправь/кинь это в X», «перенеси в X», «пусть лежит в X», «закинь в X». Поле "category" = имя категории.
- «create_move» — пользователь просит СОЗДАТЬ новую категорию/папку и положить туда контент: «создай категорию/папку X», «создай папку под комиксы», «заведи папку X и отправь это туда», «сделай отдельную категорию для X». Поле "category" = имя новой категории (короткое, по содержанию: «Комиксы», «Новости комиксов», «Семейные видео»), поле "folder" = куда вложить (если сказано: «в Развлечениях», «внутри Одежды») или пусто.
- «rename» — пользователь просит переименовать пост: «назови X», «переименуй в X». Поле "summary" = новое название.

ВАЖНО:
- «создай папку под комиксы» → create_move, category «Комиксы»; «создай папку в Развлечениях под комиксы» → create_move, category «Комиксы», folder «Развлечения».
- Если пользователь назвал категорию, которая УЖЕ есть в списке — это «move», а НЕ «create_move».
- Если сомневаешься и это скорее уточнение темы — «hint».

Формат ответа ТОЛЬКО JSON, без пояснений:
{"action": "hint|move|create_move|rename", "category": "", "folder": "", "summary": ""}
"""


async def interpret_clarify(text: str, categories: list[str] | None = None) -> dict:
    """Определяет, команда ли это (создать/переместить/переименовать) или уточнение темы."""
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
) -> dict:
    label = CONTENT_TYPE_LABELS.get(content_type or "", "контент")
    parts = [f"Тип контента: {label}"]
    if source:
        parts.append(f"Источник (канал/чат): {source}")
    if categories:
        parts.append(f"Существующие категории пользователя: {', '.join(categories)}")
    parts.append(f"\nКонтент:\n{text[:2000]}")
    user_content = "\n".join(parts)
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_content},
    ]

    fallback = normalize_summary(text, 300) if text else "Без описания"

    raw = await _groq_chat(messages, json_mode=True, max_tokens=500, timeout=45)
    if not raw:
        return {"category": "Другое", "summary": fallback, "llm_ok": False}
    return _parse(raw, fallback)