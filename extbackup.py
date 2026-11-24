"""Автокопии расширений в Telegram владельцу.

«Сводка комментариев YouTube» (без параметра app) раз в неделю (и по кнопке) шлёт сжатую
резервную копию — очередь, где остановились, настройки (без секрета), каталог плейлистов.
«Сборщик вкладок» (?app=tab-collector) — раз в сутки, после крупного сбора и по кнопке:
ссылки и папки. Бот кладёт копию файлом в чат с владельцем: если браузер или компьютер
упадут, копия останется в Telegram. В архив постов копия НЕ попадает — это служебный файл.
У каждого расширения своя подпись, своё имя файла и свой ограничитель частоты.
"""

from __future__ import annotations

import io
import re
import time

MAX_BODY = 25 * 1024 * 1024          # Telegram-бот принимает документы до 50 МБ — берём с запасом
MIN_GAP_SEC = 60                      # чаще раза в минуту не принимаем (двойной клик, сбой в цикле)
GZIP_MAGIC = b"\x1f\x8b"
TAB_COLLECTOR = "tab-collector"
APPS = (TAB_COLLECTOR,)               # другие расширения; без app — «Сводка комментариев YouTube»

_last_at = 0.0                        # «Сводка комментариев YouTube»
_last_at_app: dict[str, float] = {}   # остальные — каждое своим счётчиком


def app_of(query) -> str:
    """Какое расширение прислало копию: "tab-collector" или "" (YouTube — как раньше)."""
    app = str(query.get("app") or "").strip().lower()
    return app if app in APPS else ""


def too_soon(now: float | None = None, app: str = "") -> int:
    """Сколько секунд ждать до следующей копии этого расширения (0 — можно)."""
    now = time.time() if now is None else now
    last = _last_at_app.get(app, 0.0) if app else _last_at
    left = MIN_GAP_SEC - (now - last)
    return int(left) + 1 if left > 0 else 0


def mark_sent(now: float | None = None, app: str = "") -> None:
    global _last_at
    t = time.time() if now is None else now
    if app:
        _last_at_app[app] = t
    else:
        _last_at = t


def check_body(raw: bytes) -> str | None:
    """Причина отказа или None. Принимаем только gzip (JSON внутри распакует владелец/расширение)."""
    if not raw:
        return "пустое тело"
    if len(raw) > MAX_BODY:
        return "слишком большая копия"
    if not raw.startswith(GZIP_MAGIC):
        return "ожидается gzip"
    return None


def _int(v, lo=0, hi=10**7) -> int | None:
    try:
        n = int(str(v))
    except Exception:
        return None
    return n if lo <= n <= hi else None


def _ver(query) -> str:
    ver = str(query.get("ver") or "")[:20]
    return ver if re.fullmatch(r"[0-9.]+", ver or "") else ""


def _day(now: float | None) -> str:
    return time.strftime("%d.%m.%Y", time.gmtime((time.time() if now is None else now) + 3 * 3600))


def caption(query, now: float | None = None) -> str:
    """Подпись к файлу из параметров запроса: для YouTube — версия, сколько в очереди,
    плейлистов, видео; для «Сборщика вкладок» — ссылок, папок, версия."""
    if app_of(query) == TAB_COLLECTOR:
        return _tc_caption(query, now)
    ver = _ver(query)
    queue = _int(query.get("queue"))
    pls = _int(query.get("playlists"))
    vids = _int(query.get("videos"))
    why = "по кнопке" if query.get("manual") in ("1", "true") else "раз в неделю"
    parts = [f"🗂 Резервная копия «Сводки комментариев YouTube» · {_day(now)} ({why})"]
    facts = []
    if queue is not None:
        facts.append(f"очередь: {queue} видео")
    if pls is not None:
        facts.append(f"плейлистов: {pls}")
    if vids is not None:
        facts.append(f"видео в каталоге: {vids}")
    if facts:
        parts.append(" · ".join(facts))
    if ver:
        parts.append(f"версия расширения {ver}")
    parts.append("Восстановить: «Моя библиотека» → «Резервная копия» → выбрать этот файл.")
    return "\n".join(parts)


def _tc_caption(query, now: float | None = None) -> str:
    ver = _ver(query)
    links = _int(query.get("links"))
    folders = _int(query.get("folders"))
    why = "по кнопке" if query.get("manual") in ("1", "true") else "автоматически"
    parts = [f"🗂 Резервная копия «Сборщика вкладок» · {_day(now)} ({why})"]
    facts = []
    if links is not None:
        facts.append(f"ссылок: {links}")
    if folders is not None:
        facts.append(f"папок: {folders}")
    if facts:
        parts.append(" · ".join(facts))
    if ver:
        parts.append(f"версия расширения {ver}")
    parts.append("Восстановить: «Настройки» → «Резервная копия» → «Загрузить копию» → выбрать этот файл.")
    return "\n".join(parts)


def filename(now: float | None = None, app: str = "") -> str:
    t = time.gmtime((time.time() if now is None else now) + 3 * 3600)
    prefix = "tab-collector-backup" if app == TAB_COLLECTOR else "ytcs-backup"
    return time.strftime(prefix + "-%Y-%m-%d.json.gz", t)


def as_file(raw: bytes, name: str) -> io.BytesIO:
    f = io.BytesIO(raw)
    f.name = name
    return f
