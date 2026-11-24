"""Автокопия расширения «Сводка комментариев YouTube» в Telegram владельцу.

Расширение раз в неделю (и по кнопке) шлёт сжатую резервную копию — очередь, где
остановились, настройки (без секрета), каталог плейлистов. Бот кладёт её файлом в
чат с владельцем: если браузер или компьютер упадут, копия останется в Telegram.
В архив постов копия НЕ попадает — это служебный файл.
"""

from __future__ import annotations

import io
import re
import time

MAX_BODY = 25 * 1024 * 1024          # Telegram-бот принимает документы до 50 МБ — берём с запасом
MIN_GAP_SEC = 60                      # чаще раза в минуту не принимаем (двойной клик, сбой в цикле)
GZIP_MAGIC = b"\x1f\x8b"

_last_at = 0.0


def too_soon(now: float | None = None) -> int:
    """Сколько секунд ждать до следующей копии (0 — можно)."""
    now = time.time() if now is None else now
    left = MIN_GAP_SEC - (now - _last_at)
    return int(left) + 1 if left > 0 else 0


def mark_sent(now: float | None = None) -> None:
    global _last_at
    _last_at = time.time() if now is None else now


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


def caption(query, now: float | None = None) -> str:
    """Подпись к файлу из параметров запроса: версия, сколько в очереди, плейлистов, видео."""
    ver = str(query.get("ver") or "")[:20]
    ver = ver if re.fullmatch(r"[0-9.]+", ver or "") else ""
    queue = _int(query.get("queue"))
    pls = _int(query.get("playlists"))
    vids = _int(query.get("videos"))
    why = "по кнопке" if query.get("manual") in ("1", "true") else "раз в неделю"
    day = time.strftime("%d.%m.%Y", time.gmtime((time.time() if now is None else now) + 3 * 3600))
    parts = [f"🗂 Резервная копия «Сводки комментариев YouTube» · {day} ({why})"]
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


def filename(now: float | None = None) -> str:
    t = time.gmtime((time.time() if now is None else now) + 3 * 3600)
    return time.strftime("ytcs-backup-%Y-%m-%d.json.gz", t)


def as_file(raw: bytes, name: str) -> io.BytesIO:
    f = io.BytesIO(raw)
    f.name = name
    return f
