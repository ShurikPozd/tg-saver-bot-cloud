"""Очередь «висящих» вопросов бота (сохраняется в data/pending.json).

Каждое место, где бот задаёт вопрос и ждёт ответ (подтверждение дубля,
уточнение, переименование и т.п.), добавляет запись. Кнопка в корневом
меню показывает количество таких записей и позволяет повторить вопрос.
"""

import json
import os
import time

_PATH = os.path.join("data", "pending.json")
_entries: list[dict] = []
_loaded = False


def _load() -> None:
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        with open(_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, list):
            _entries[:] = raw
    except (FileNotFoundError, ValueError):
        _entries[:] = []
    except Exception:
        _entries[:] = []


def _save() -> None:
    try:
        with open(_PATH, "w", encoding="utf-8") as f:
            json.dump(_entries, f, ensure_ascii=False)
    except Exception:
        pass


def total() -> int:
    _load()
    return len(_entries)


def count_for(chat_id: int) -> int:
    return len(entries(chat_id))


def entries(chat_id: int) -> list[dict]:
    _load()
    return [e for e in _entries if e.get("chat") == chat_id]


def push(chat_id: int, act: dict, label: str, replay: dict | None = None) -> str:
    _load()
    sid = f"{int(time.time() * 1000)}-{len(_entries)}"
    entry = {"sid": sid, "chat": chat_id, "label": label, "act": act, "replay": replay or {}}
    _entries.append(entry)
    _save()
    return sid


def top(chat_id: int) -> dict | None:
    es = entries(chat_id)
    return es[-1] if es else None


def pop(chat_id: int) -> dict | None:
    es = entries(chat_id)
    if not es:
        return None
    last = es[-1]
    _entries.remove(last)
    _save()
    return last


def set_msg(chat_id: int, sid: str, msg_id) -> None:
    _load()
    for e in _entries:
        if e.get("chat") == chat_id and e.get("sid") == sid:
            e["act"]["msg_id"] = msg_id
            _save()
            return


def remove(chat_id: int, sid: str) -> dict | None:
    _load()
    for e in _entries:
        if e.get("chat") == chat_id and e.get("sid") == sid:
            _entries.remove(e)
            _save()
            return e
    return None


def remove_for_chat(chat_id: int) -> int:
    _load()
    keep = [e for e in _entries if e.get("chat") != chat_id]
    removed = len(_entries) - len(keep)
    if removed:
        _entries[:] = keep
        _save()
    return removed