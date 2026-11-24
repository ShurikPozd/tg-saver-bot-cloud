import sqlite3
import json
import os
import re
import logging
import time

import contextvars
from config import DB_PATH

logger = logging.getLogger(__name__)

# Каждый пользователь получает собственную БД: data/user_<id>.db.
# contextvar задаёт «активного» пользователя на время обработки его события.
_current_user_id: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "tg_saver_current_user", default=None
)


def _base_dir() -> str:
    return os.path.dirname(DB_PATH) or "data"


def user_db_path(user_id) -> str:
    return os.path.join(_base_dir(), f"user_{user_id}.db")


def current_user_id():
    return _current_user_id.get()


def set_current_user(user_id) -> None:
    _current_user_id.set(user_id)


def reset_current_user() -> None:
    _current_user_id.set(None)


# Подмена пути БД на время задачи (проверка бэкапа восстанавливает его во
# временный файл, не трогая живую базу). contextvar — действует только в своей задаче.
_db_path_override: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "tg_saver_db_path_override", default=None
)


def use_db_path(path: str):
    return _db_path_override.set(path)


def reset_db_path(token) -> None:
    _db_path_override.reset(token)


def get_active_db_path() -> str:
    ov = _db_path_override.get()
    if ov:
        return ov
    uid = _current_user_id.get()
    return user_db_path(uid) if uid is not None else DB_PATH


def get_connection():
    path = get_active_db_path()
    parent = os.path.dirname(path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)
    return sqlite3.connect(path, timeout=15)


USERS_FILE = os.path.join(_base_dir(), "users.json")


def _load_users() -> list:
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [int(u) for u in data] if isinstance(data, list) else []
    except Exception:
        return []


def _save_users(users: list) -> None:
    os.makedirs(_base_dir(), exist_ok=True)
    with open(USERS_FILE, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False)


def all_users() -> list:
    return _load_users()


def legacy_owner_id():
    """Владелец из старой единой БД (settings: owner_user / owner_id), если они сохранены."""
    try:
        if not os.path.exists(DB_PATH):
            return None
        con = sqlite3.connect(DB_PATH)
        try:
            for key in ("owner_user", "owner_id"):
                row = con.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
                if row and row[0]:
                    val = str(row[0]).strip()
                    if val.isdigit():
                        return int(val)
        finally:
            con.close()
    except Exception:
        pass
    return None


def db_item_count(path: str) -> int:
    """Сколько постов в БД по пути (0 при ошибке/отсутствии)."""
    try:
        if not os.path.exists(path):
            return 0
        con = sqlite3.connect(path)
        try:
            row = con.execute("SELECT COUNT(*) FROM saved_items").fetchone()
            return row[0] if row else 0
        finally:
            con.close()
    except Exception:
        return 0


def migrate_legacy(owner_id=None) -> bool:
    """Переносит старую единую БД (saved_items.db) в личную БД владельца.

    Владелец берётся из параметра, либо сам определяется из settings старой БД
    (owner_user / owner_id). Миграция выполняется, только если legacy-БД содержит
    посты, а БД владельца пуста или отсутствует.
    """
    if not owner_id:
        owner_id = legacy_owner_id()
    if not owner_id:
        return False
    uid = int(owner_id)
    if not os.path.exists(DB_PATH):
        return False
    if db_item_count(DB_PATH) == 0:
        return False
    target = user_db_path(uid)
    if os.path.exists(target) and db_item_count(target) > 0:
        return False
    try:
        import shutil

        os.makedirs(_base_dir(), exist_ok=True)
        shutil.copy2(DB_PATH, target)
        if db_item_count(target) == 0:
            return False
        register_user(uid)
        return True
    except Exception:
        return False


def register_user(user_id) -> None:
    """Регистрирует нового пользователя: создаёт его БД и добавляет в реестр."""
    uid = int(user_id)
    prev = _current_user_id.get()
    try:
        _current_user_id.set(uid)
        init_db()
        if uid not in _load_users():
            users = _load_users()
            users.append(uid)
            _save_users(users)
    finally:
        _current_user_id.set(prev)


BROKEN_LEGACY_ITEM_IDS = set(range(73, 82))


def migrate_delete_broken_message_ids() -> list:
    """Одноразовая миграция: удаляет посты, чьи telegram_message_id были испорчены
    локальным ботом при переносе архива на сервер (под этими номерами в живом чате
    Render-бота другие сообщения). Удаление из saved_items + очистка из корзины.
    Возвращает список удалённых id."""
    removed = []
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id FROM saved_items WHERE id IN (%s)"
            % ",".join("?" * len(BROKEN_LEGACY_ITEM_IDS)),
            tuple(BROKEN_LEGACY_ITEM_IDS),
        ).fetchall()
        ids = [r[0] for r in rows]
        if ids:
            marks = ",".join("?" * len(ids))
            conn.execute("DELETE FROM saved_items WHERE id IN (%s)" % marks, ids)
            conn.execute("DELETE FROM trash WHERE id IN (%s)" % marks, ids)
            for iid in ids:
                add_history(iid, "migrate", None, "deleted", conn=conn)
            conn.commit()
            removed = ids
    return removed


def init_db():
    """Инициализирует схему активной БД пользователя.

    Hot path (вызывается на каждое сообщение) — только read-only проверка,
    без write-lock: если схема уже готова, DDL не выполняем вовсе. Полный DDL
    идёт лишь для новых БД, и при конкурентном захвате файла (фоновый импорт)
    ретраимся вместо падения с «database is locked»."""
    path = get_active_db_path()
    try:
        with get_connection() as conn:
            ok = _schema_ok(conn)
        if ok:
            _repair_once()
            return
    except sqlite3.OperationalError:
        pass
    last_err = None
    for attempt in range(60):
        try:
            _init_db_ddl()
            _repair_once()
            return
        except sqlite3.OperationalError as e:
            last_err = e
            if "locked" not in str(e).lower():
                raise
            time.sleep(0.5)
    raise last_err


def _repair_once() -> None:
    """Разовые лечилки данных активной БД (один раз на процесс и файл)."""
    try:
        repair_file_ids()
    except Exception as e:
        logger.warning("Лечение file_ids не удалось: %s", e)
    path = get_active_db_path()
    if path not in _seq_checked:
        _seq_checked.add(path)
        try:
            bump_item_seq()
        except Exception as e:
            logger.warning("Счётчик номеров постов: %s", e)


_seq_checked: set = set()


def _ddl_articles(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS item_articles (
            item_id INTEGER PRIMARY KEY,
            url TEXT,
            title TEXT,
            text TEXT,
            status TEXT,
            fetched_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    if "brief" not in {r[1] for r in conn.execute("PRAGMA table_info(item_articles)").fetchall()}:
        conn.execute("ALTER TABLE item_articles ADD COLUMN brief TEXT")


def _schema_ok(conn) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='saved_items'"
    ).fetchone()
    if not row:
        return False
    cols = {r[1] for r in conn.execute("PRAGMA table_info(saved_items)").fetchall()}
    if not conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='item_articles'").fetchone():
        return False
    if "brief" not in {r[1] for r in conn.execute("PRAGMA table_info(item_articles)").fetchall()}:
        return False
    return {"file_ids", "media_group_id", "locked", "source_channel", "file_unique", "comment"}.issubset(cols)


def _init_db_ddl():
    with get_connection() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS saved_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                content_type TEXT NOT NULL,
                summary TEXT,
                original_text TEXT,
                file_id TEXT,
                file_ids TEXT,
                media_group_id TEXT,
                telegram_message_id INTEGER,
                telegram_chat_id INTEGER,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(saved_items)").fetchall()]
        if "file_ids" not in cols:
            conn.execute("ALTER TABLE saved_items ADD COLUMN file_ids TEXT")
        if "media_group_id" not in cols:
            conn.execute("ALTER TABLE saved_items ADD COLUMN media_group_id TEXT")
        if "locked" not in cols:
            conn.execute("ALTER TABLE saved_items ADD COLUMN locked INTEGER DEFAULT 0")
        if "source_channel" not in cols:
            conn.execute("ALTER TABLE saved_items ADD COLUMN source_channel TEXT")
        if "file_unique" not in cols:
            conn.execute("ALTER TABLE saved_items ADD COLUMN file_unique TEXT")
        if "comment" not in cols:
            conn.execute("ALTER TABLE saved_items ADD COLUMN comment TEXT")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                name TEXT PRIMARY KEY,
                group_name TEXT,
                locked INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS folders (
                name TEXT PRIMARY KEY,
                locked INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        cat_cols = [r[1] for r in conn.execute("PRAGMA table_info(categories)").fetchall()]
        if "manual" not in cat_cols:
            conn.execute("ALTER TABLE categories ADD COLUMN manual INTEGER DEFAULT 0")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS trash (
                id INTEGER PRIMARY KEY,
                category TEXT NOT NULL,
                content_type TEXT NOT NULL,
                summary TEXT,
                original_text TEXT,
                file_id TEXT,
                file_ids TEXT,
                media_group_id TEXT,
                telegram_message_id INTEGER,
                telegram_chat_id INTEGER,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                locked INTEGER DEFAULT 0,
                source_channel TEXT,
                deleted_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        trash_cols = [r[1] for r in conn.execute("PRAGMA table_info(trash)").fetchall()]
        if "file_unique" not in trash_cols:
            conn.execute("ALTER TABLE trash ADD COLUMN file_unique TEXT")
        if "comment" not in trash_cols:
            conn.execute("ALTER TABLE trash ADD COLUMN comment TEXT")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS item_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                item_id INTEGER NOT NULL,
                action TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                icon TEXT NOT NULL,
                name TEXT DEFAULT ''
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS item_tags (
                item_id INTEGER NOT NULL,
                tag_id INTEGER NOT NULL,
                PRIMARY KEY (item_id, tag_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS folder_tags (
                path TEXT NOT NULL,
                tag_id INTEGER NOT NULL,
                PRIMARY KEY (path, tag_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS category_tags (
                category TEXT NOT NULL,
                tag_id INTEGER NOT NULL,
                PRIMARY KEY (category, tag_id)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS icon_overrides (
                category TEXT PRIMARY KEY,
                icon TEXT NOT NULL
            )
        """)
        _ddl_articles(conn)
        conn.commit()


def get_setting(key: str, default: str = "") -> str:
    with get_connection() as conn:
        row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default


def set_setting(key: str, value: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        conn.commit()


def should_lock(kind: str) -> bool:
    if get_setting("auto_lock", "1") != "1":
        return False
    return get_setting(f"lock_{kind}", "1") == "1"


def save_item(
    category: str,
    content_type: str,
    summary: str,
    original_text: str,
    file_id: str | None,
    message_id: int,
    chat_id: int,
    media_group_id: str | None = None,
    file_ids: list | None = None,
    source_channel: str | None = None,
    file_unique: str | None = None,
) -> int:
    file_ids_json = json.dumps(file_ids, ensure_ascii=False) if file_ids else None
    with get_connection() as conn:
        cursor = conn.execute(
            """INSERT INTO saved_items
               (category, content_type, summary, original_text, file_id, file_ids, media_group_id, telegram_message_id, telegram_chat_id, source_channel, file_unique)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (category, content_type, summary, original_text, file_id, file_ids_json, media_group_id, message_id, chat_id, source_channel, file_unique),
        )
        conn.execute("INSERT OR IGNORE INTO categories(name) VALUES (?)", (category,))
        conn.commit()
        return cursor.lastrowid


def ensure_category(name: str) -> None:
    with get_connection() as conn:
        conn.execute("INSERT OR IGNORE INTO categories(name) VALUES (?)", (name,))
        conn.commit()


def get_category_row(name: str) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT name, COALESCE(group_name,''), COALESCE(locked,0) FROM categories WHERE name = ?",
            (name,),
        ).fetchone()
        if not row:
            return None
        return {"category": row[0], "group": row[1], "locked": bool(row[2])}


def name_taken(name: str) -> str | None:
    """Проверяет имя без учёта регистра и по категориям, и по папкам."""
    name = (name or "").strip()
    if not name:
        return "Имя не может быть пустым"
    with get_connection() as conn:
        cats = conn.execute(
            "SELECT name FROM categories WHERE lower(name) = lower(?)", (name,)
        ).fetchall()
        flds = conn.execute(
            "SELECT name FROM folders WHERE lower(name) = lower(?)", (name,)
        ).fetchall()
    for (r,) in cats:
        if r != name:
            return f"Категория «{r}»"
    for (r,) in flds:
        return f"Папка «{r}»"
    return None


def create_category(name: str, path: str = ""):
    name = (name or "").strip()
    path = (path or "").strip("/")
    if not name:
        return False, "Имя не может быть пустым"
    if "/" in name:
        return False, "В имени категории не может быть «/»"
    if get_category_row(name):
        return False, f"Категория «{name}» уже существует"
    taken = name_taken(name)
    if taken:
        return False, f"Занято: {taken}"
    with get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO categories(name, group_name, manual) VALUES (?, ?, 1)",
            (name, path or None),
        )
        conn.commit()
    return True, name


def rename_category(old: str, new: str):
    new = (new or "").strip()
    if not new:
        return False, "Имя не может быть пустым"
    if "/" in new:
        return False, "В имени категории не может быть «/»"
    if new == old:
        return False, "Имя то же самое"
    row = get_category_row(old)
    if not row:
        return False, "Категория не найдена"
    if row["locked"]:
        return False, "Категория заблокирована"
    if get_category_row(new):
        return False, f"Категория «{new}» уже существует"
    taken = name_taken(new)
    if taken:
        return False, f"Занято: {taken}"
    with get_connection() as conn:
        conn.execute("UPDATE categories SET name = ?, manual = 1 WHERE name = ?", (new, old))
        conn.execute("UPDATE saved_items SET category = ? WHERE category = ?", (new, old))
        conn.execute("UPDATE OR IGNORE icon_overrides SET category = ? WHERE category = ?", (new, old))
        conn.execute("UPDATE OR IGNORE category_tags SET category = ? WHERE category = ?", (new, old))
        conn.commit()
    return True, new


def folder_twin(path: str) -> str | None:
    """Категория с постами, которая для пользователя и есть сама папка: имя = последнему
    сегменту пути, лежит прямо в этой папке («Технологии» в «Технологии»)."""
    path = (path or "").strip("/")
    if not path:
        return None
    name = path.rpartition("/")[2]
    row = get_category_row(name)
    return name if row and (row["group"] or "").strip("/") == path else None


def rename_twin(old: str, new: str) -> bool:
    """Переименовать категорию-двойника вслед за папкой (имя папки ей не помеха)."""
    if not new or old == new or get_category_row(new):
        return False
    with get_connection() as conn:
        conn.execute("UPDATE categories SET name = ? WHERE name = ?", (new, old))
        conn.execute("UPDATE saved_items SET category = ? WHERE category = ?", (new, old))
        conn.execute("UPDATE OR IGNORE icon_overrides SET category = ? WHERE category = ?", (new, old))
        conn.execute("UPDATE OR IGNORE category_tags SET category = ? WHERE category = ?", (new, old))
        conn.commit()
    return True


def rename_item(item_id: int, new_summary: str):
    new_summary = (new_summary or "").strip()
    if not new_summary:
        return False, "Заголовок не может быть пустым"
    with get_connection() as conn:
        old = conn.execute("SELECT summary FROM saved_items WHERE id = ?", (item_id,)).fetchone()
        conn.execute("UPDATE saved_items SET summary = ? WHERE id = ?", (new_summary, item_id))
        if old and (old[0] or "") != new_summary:
            add_history(item_id, "summary", old[0] or "", new_summary, conn=conn)
        conn.commit()
    return True, new_summary


def get_folder(path: str) -> dict | None:
    path = (path or "").strip("/")
    if not path:
        return None
    with get_connection() as conn:
        row = conn.execute(
            "SELECT name, COALESCE(locked,0) FROM folders WHERE name = ?", (path,)
        ).fetchone()
    return {"path": row[0], "locked": bool(row[1])} if row else None


def folder_locked(path: str) -> bool:
    f = get_folder(path)
    return bool(f and f["locked"])


def set_folder_locked(path: str, locked: bool) -> None:
    path = (path or "").strip("/")
    if not path:
        return
    with get_connection() as conn:
        conn.execute("INSERT OR IGNORE INTO folders(name) VALUES (?)", (path,))
        conn.execute("UPDATE folders SET locked = ? WHERE name = ?", (1 if locked else 0, path))
        conn.commit()


def create_folder(path: str):
    path = (path or "").strip("/")
    if not path:
        return False, "Пустое имя"
    if get_folder(path):
        return False, f"Папка «{path}» уже существует"
    taken = name_taken(path)
    if taken:
        return False, f"Занято: {taken}"
    with get_connection() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO folders(name) VALUES (?)", (path,))
        conn.commit()
    if cur.rowcount == 0:
        return False, f"Папка «{path}» уже существует"
    return True, path


def rename_folder(path: str, new_name: str):
    path = (path or "").strip("/")
    new_name = (new_name or "").strip().strip("/")
    if not path or not new_name:
        return False, "Пустое имя"
    if "/" in new_name:
        return False, "Новое имя папки должно быть без «/»"
    parent, _, old_seg = path.rpartition("/")
    new_path = f"{parent}/{new_name}" if parent else new_name
    if new_path == path:
        return False, "Имя то же самое"
    if folder_locked(path):
        return False, "Папка заблокирована"
    if get_folder(new_path):
        return False, f"Папка «{new_path}» уже существует"
    taken = name_taken(new_path)
    if taken:
        return False, f"Занято: {taken}"
    with get_connection() as conn:
        locked_count = conn.execute(
            "SELECT COUNT(*) FROM categories WHERE locked = 1 "
            "AND (group_name = ? OR group_name LIKE ? || '/%')",
            (path, path),
        ).fetchone()[0]
        if locked_count:
            return False, "В папке есть заблокированные категории"
        conn.execute(
            """UPDATE categories
               SET group_name = CASE
                   WHEN group_name = ? THEN ?
                   ELSE ? || SUBSTR(group_name, length(?) + 1)
               END
               WHERE group_name = ? OR group_name LIKE ? || '/%'""",
            (path, new_path, new_path, path, path, path),
        )
        conn.execute(
            """UPDATE folders
               SET name = CASE
                   WHEN name = ? THEN ?
                   ELSE ? || SUBSTR(name, length(?) + 1)
               END
               WHERE name = ? OR name LIKE ? || '/%'""",
            (path, new_path, new_path, path, path, path),
        )
        conn.commit()
    return True, new_path


def get_categories() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT i.category, COUNT(i.id) cnt, COALESCE(c.group_name,'') grp, COALESCE(c.locked,0) lk
               FROM saved_items i LEFT JOIN categories c ON c.name = i.category
               GROUP BY i.category ORDER BY cnt DESC""",
        ).fetchall()
        return [{"category": r[0], "count": r[1], "group": r[2], "locked": bool(r[3])} for r in rows]


def get_groups() -> list[dict]:
    cats = get_categories()
    groups = {}
    for c in cats:
        groups.setdefault(c["group"], []).append(c)
    return [{"group": g, "categories": lst} for g, lst in groups.items()]


def set_category_locked(name: str, locked: bool) -> None:
    with get_connection() as conn:
        conn.execute("INSERT OR IGNORE INTO categories(name) VALUES (?)", (name,))
        conn.execute("UPDATE categories SET locked = ? WHERE name = ?", (1 if locked else 0, name))
        conn.commit()


def set_category_group(name: str, group: str) -> None:
    with get_connection() as conn:
        conn.execute("INSERT OR IGNORE INTO categories(name) VALUES (?)", (name,))
        conn.execute("UPDATE categories SET group_name = ? WHERE name = ?", (group, name))
        conn.commit()


def set_category_path(name: str, path: str) -> None:
    set_category_group(name, path.strip("/"))


def get_folder_paths() -> list[str]:
    seen = set()
    for c in get_categories():
        path = (c.get("group") or "").strip("/")
        if not path:
            continue
        cur = ""
        for seg in path.split("/"):
            cur = seg if not cur else f"{cur}/{seg}"
            seen.add(cur)
    with get_connection() as conn:
        for (name,) in conn.execute("SELECT name FROM folders").fetchall():
            cur = ""
            for seg in name.split("/"):
                cur = seg if not cur else f"{cur}/{seg}"
                seen.add(cur)
    return sorted(seen, key=lambda p: (p.count("/"), p))


def move_folder_path(from_path: str, to_path: str) -> int:
    from_path = from_path.strip("/")
    to_path = to_path.strip("/")
    if not from_path or from_path == to_path:
        return 0
    if to_path == from_path or to_path.startswith(from_path + "/"):
        return 0
    if folder_locked(from_path) or (to_path and folder_locked(to_path)):
        return 0
    with get_connection() as conn:
        cursor = conn.execute(
            """UPDATE categories
               SET group_name = CASE
                   WHEN group_name = ? THEN ?
                   ELSE ? || SUBSTR(group_name, length(?) + 1)
               END
               WHERE locked = 0 AND (group_name = ? OR group_name LIKE ? || '/%')""",
            (from_path, to_path, to_path, from_path, from_path, from_path),
        )
        conn.execute(
            """UPDATE folders
               SET name = CASE
                   WHEN name = ? THEN ?
                   ELSE ? || SUBSTR(name, length(?) + 1)
               END
               WHERE name = ? OR name LIKE ? || '/%'""",
            (from_path, to_path, to_path, from_path, from_path, from_path),
        )
        conn.commit()
        return cursor.rowcount


def _tree_total(node: dict) -> int:
    total = 0
    for child in node["children"]:
        if child["type"] == "cat":
            total += child["count"]
        else:
            total += _tree_total(child)
    return total


def get_tree() -> list[dict]:
    cats = get_categories()
    root_folders = {}
    root_cats = []
    nodes = {}

    with get_connection() as conn:
        folder_locks = {
            r[0]: bool(r[1]) for r in conn.execute("SELECT name, COALESCE(locked,0) FROM folders").fetchall()
        }

    def ensure(segments):
        parent_path = ""
        for seg in segments:
            cur = seg if not parent_path else f"{parent_path}/{seg}"
            if cur not in nodes:
                node = {"type": "folder", "name": seg, "path": cur, "children": [], "locked": False}
                nodes[cur] = node
                if not parent_path:
                    root_folders[cur] = node
                else:
                    nodes[parent_path]["children"].append(node)
            parent_path = cur

    for c in cats:
        cat_node = {
            "type": "cat",
            "category": c["category"],
            "count": c["count"],
            "locked": c["locked"],
            "path": c["group"],
        }
        path = (c["group"] or "").strip("/")
        if not path:
            root_cats.append(cat_node)
            continue
        ensure(path.split("/"))
        nodes[path]["children"].append(cat_node)

    for fpath in sorted(folder_locks, key=lambda p: (p.count("/"), p)):
        ensure(fpath.split("/"))
        if folder_locks[fpath]:
            nodes[fpath]["locked"] = True

    for node in nodes.values():
        node["count"] = _tree_total(node)

    root_cats_sorted = sorted(root_cats, key=lambda x: -x["count"])
    root_folders_sorted = sorted(root_folders.values(), key=lambda n: -n["count"])
    return root_folders_sorted + root_cats_sorted


def _lift_path(p: str, path: str, parent: str) -> str:
    """Путь p внутри папки path → тот же путь на уровень выше (в parent)."""
    if p == path:
        return parent
    rest = p[len(path) + 1:]
    return f"{parent}/{rest}" if parent else rest


def dissolve_folder(path: str):
    """Убирает папку, НЕ трогая посты: её категории и подпапки поднимаются на уровень выше.
    Возвращает (ok, текст)."""
    path = (path or "").strip("/")
    if not path:
        return False, "Пустое имя папки"
    if folder_locked(path):
        return False, "Папка заблокирована"
    parent = path.rpartition("/")[0]
    with get_connection() as conn:
        # точное совпадение префикса: LIKE понимал «_» и «%» в имени как шаблон и
        # не различал регистр латиницы — задевал чужие папки (X_Y → XzY)
        pre = path + "/"
        cats = conn.execute(
            "SELECT name, group_name, COALESCE(locked,0) FROM categories "
            "WHERE group_name = ? OR substr(group_name, 1, ?) = ?",
            (path, len(pre), pre),
        ).fetchall()
        flds = conn.execute(
            "SELECT name, COALESCE(locked,0) FROM folders WHERE name = ? OR substr(name, 1, ?) = ?",
            (path, len(pre), pre),
        ).fetchall()
        if not cats and not flds:
            return False, "Папка не найдена"
        if any(r[2] for r in cats):
            return False, "В папке есть заблокированные категории"
        if any(r[1] for r in flds):
            return False, "В папке есть заблокированные подпапки"
        for name, grp, _ in cats:
            conn.execute("UPDATE categories SET group_name = ? WHERE name = ?",
                         (_lift_path(grp, path, parent) or None, name))
        for fname, _ in flds:
            if fname != path:
                new = _lift_path(fname, path, parent)
                # такая папка уже есть — сливаемся с ней (раньше старая строка оставалась
                # и убранная папка «воскресала»)
                conn.execute("INSERT OR IGNORE INTO folders (name) VALUES (?)", (new,))
                conn.execute("UPDATE OR IGNORE folder_tags SET path = ? WHERE path = ?", (new, fname))
                conn.execute("DELETE FROM folder_tags WHERE path = ?", (fname,))
            conn.execute("DELETE FROM folders WHERE name = ?", (fname,))
        conn.execute("DELETE FROM folder_tags WHERE path = ?", (path,))
        conn.commit()
    return True, f"{len(cats)} категор. перенесено {'в «' + parent + '»' if parent else 'в корень'}"


# Что снимает «↩️ Отменить» помощника: (таблица, SELECT, длина ключа, INSERT, DELETE)
_SNAP_TABLES = (
    ("categories", "SELECT name, group_name, locked, created_at, manual FROM categories ORDER BY name", 1,
     "INSERT OR REPLACE INTO categories (name, group_name, locked, created_at, manual) VALUES (?, ?, ?, ?, ?)",
     "DELETE FROM categories WHERE name = ?"),
    ("folders", "SELECT name, locked, created_at FROM folders ORDER BY name", 1,
     "INSERT OR REPLACE INTO folders (name, locked, created_at) VALUES (?, ?, ?)",
     "DELETE FROM folders WHERE name = ?"),
    ("icons", "SELECT category, icon FROM icon_overrides ORDER BY category", 1,
     "INSERT OR REPLACE INTO icon_overrides (category, icon) VALUES (?, ?)",
     "DELETE FROM icon_overrides WHERE category = ?"),
    ("category_tags", "SELECT category, tag_id FROM category_tags ORDER BY category, tag_id", 2,
     "INSERT OR IGNORE INTO category_tags (category, tag_id) VALUES (?, ?)",
     "DELETE FROM category_tags WHERE category = ? AND tag_id = ?"),
    ("folder_tags", "SELECT path, tag_id FROM folder_tags ORDER BY path, tag_id", 2,
     "INSERT OR IGNORE INTO folder_tags (path, tag_id) VALUES (?, ?)",
     "DELETE FROM folder_tags WHERE path = ? AND tag_id = ?"),
)


def structure_snapshot() -> dict:
    """Всё, что меняет ИИ-помощник: категории, папки, иконки, теги папок и категории постов.
    Нужен для кнопки «↩️ Отменить»."""
    with get_connection() as conn:
        snap = {name: [list(r) for r in conn.execute(sql).fetchall()] for name, sql, *_ in _SNAP_TABLES}
        snap["items"] = [list(r) for r in conn.execute("SELECT id, category FROM saved_items ORDER BY id").fetchall()]
        return snap


def restore_structure(before: dict, after: dict | None = None) -> int:
    """Отменяет ровно то, что изменил помощник: before — снимок до «Применить», after — сразу после.
    То, что поменялось уже потом (переносы руками, перераспознавание, новые папки, замки),
    не трогается; заблокированные посты не двигаются. Без after — откат всего к before
    (кроме заблокированных постов). Возвращает, сколько постов вернулось на прежние места."""
    with get_connection() as conn:
        for name, sql, k, ins, dele in _SNAP_TABLES:
            if name not in before:
                continue
            cur = {tuple(r[:k]): list(r) for r in conn.execute(sql).fetchall()}
            b = {tuple(r[:k]): list(r) for r in before.get(name, [])}
            a = cur if after is None else {tuple(r[:k]): list(r) for r in after.get(name, [])}
            for key in set(b) | set(a):
                bv, av = b.get(key), a.get(key)
                if bv == av or cur.get(key) != av:
                    continue          # помощник не трогал или уже поменяли после него
                if bv is None:
                    conn.execute(dele, key)
                else:
                    conn.execute(ins, tuple(bv))
        now = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT id, category, COALESCE(locked, 0) FROM saved_items")}
        b = {r[0]: r[1] for r in before.get("items", [])}
        a = {iid: v[0] for iid, v in now.items()} if after is None else {r[0]: r[1] for r in after.get("items", [])}
        back = []
        for iid, cat in b.items():
            if iid not in now or now[iid][1] or cat == a.get(iid) or now[iid][0] != a.get(iid):
                continue
            back.append((cat, iid))
        conn.executemany("UPDATE saved_items SET category = ? WHERE id = ?", back)
        conn.commit()
    return len(back)


def category_samples(per_cat: int = 2) -> dict[str, list[str]]:
    """Свежие описания постов по категориям — ИИ по ним понимает, что внутри."""
    out: dict[str, list[str]] = {}
    with get_connection() as conn:
        for cat, summ in conn.execute("SELECT category, summary FROM saved_items ORDER BY id DESC").fetchall():
            lst = out.setdefault(cat, [])
            if len(lst) < per_cat and summ:
                lst.append(summ)
    return out


def merge_categories(source: str, target: str, lock_items: bool = True) -> int:
    with get_connection() as conn:
        if lock_items:
            cursor = conn.execute(
                "UPDATE saved_items SET category = ?, locked = 1 WHERE category = ? AND locked = 0",
                (target, source),
            )
        else:
            cursor = conn.execute(
                "UPDATE saved_items SET category = ? WHERE category = ? AND locked = 0",
                (target, source),
            )
        conn.execute("INSERT OR IGNORE INTO categories(name) VALUES (?)", (target,))
        conn.commit()
        return cursor.rowcount


def cleanup_empty(include_manual: bool = False) -> None:
    """Удаляет пустые категории и пустые папки. Пустые = нет ни одного сохранённого поста."""
    with get_connection() as conn:
        conn.execute(
            """DELETE FROM categories
               WHERE locked = 0 AND (? OR manual = 0)
                 AND name NOT IN (SELECT DISTINCT category FROM saved_items)""",
            (1 if include_manual else 0,),
        )
        cur = conn.execute("SELECT name FROM folders WHERE locked = 0").fetchall()
        for (name,) in cur:
            pref = name + "/"
            has_child = conn.execute(
                "SELECT 1 FROM categories WHERE group_name = ? OR group_name LIKE ? LIMIT 1",
                (name, pref + "%"),
            ).fetchone()
            has_folder = conn.execute(
                "SELECT 1 FROM folders WHERE name LIKE ? LIMIT 1", (pref + "%",)
            ).fetchone()
            if not has_child and not has_folder:
                conn.execute("DELETE FROM folders WHERE name = ?", (name,))
        conn.commit()


def cleanup_empty_categories() -> None:
    """Обратная совместимость: без учёта ручных категорий."""
    cleanup_empty(include_manual=False)


def set_item_locked(item_id: int, locked: bool) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE saved_items SET locked = ? WHERE id = ?", (1 if locked else 0, item_id))
        add_history(item_id, "lock" if locked else "unlock", "0" if locked else "1", "1" if locked else "0", conn=conn)
        conn.commit()


def add_history(item_id: int, action: str, old_value=None, new_value=None, conn=None) -> None:
    sql = "INSERT INTO item_history (item_id, action, old_value, new_value) VALUES (?, ?, ?, ?)"
    if conn is not None:
        conn.execute(sql, (item_id, action, old_value, new_value))
        return
    with get_connection() as conn:
        conn.execute(sql, (item_id, action, old_value, new_value))
        conn.commit()


def get_history(item_id: int, limit: int = 20) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT action, old_value, new_value, created_at FROM item_history "
            "WHERE item_id = ? ORDER BY id DESC LIMIT ?",
            (item_id, limit),
        ).fetchall()
    return [
        {"action": r[0], "old_value": r[1] or "", "new_value": r[2] or "", "at": r[3]}
        for r in rows
    ]


def set_item_comment(item_id: int, comment: str) -> str:
    comment = (comment or "").strip()
    with get_connection() as conn:
        old = conn.execute("SELECT comment FROM saved_items WHERE id = ?", (item_id,)).fetchone()
        old_c = (old[0] if old else None) or ""
        if old_c == comment:
            return old_c
        conn.execute("UPDATE saved_items SET comment = ? WHERE id = ?", (comment or None, item_id))
        add_history(item_id, "comment", old_c, comment, conn=conn)
        conn.commit()
    return comment


def _unwrap_file_ids(raw) -> list:
    """file_ids в любом виде → список вложений.

    Исторический баг: export_json клал в бэкап уже готовую JSON-строку, а импорт
    кодировал её ещё раз — после каждого восстановления на Render строка
    заворачивалась в новый слой кавычек (до 18 слоёв, 24 МБ на пост). Здесь
    разворачиваем все слои, пока не получим список."""
    v = raw
    for _ in range(64):
        if isinstance(v, (bytes, bytearray)):
            v = v.decode("utf-8", "replace")
        if not isinstance(v, str):
            break
        s = v.strip()
        if not s:
            return []
        try:
            v = json.loads(s)
        except Exception:
            return []
    return v if isinstance(v, list) else []


def _file_ids_db_value(raw) -> str | None:
    """Каноничное значение колонки file_ids: JSON-массив ровно в один слой (или None)."""
    lst = _unwrap_file_ids(raw)
    return json.dumps(lst, ensure_ascii=False) if lst else None


def _parse_file_ids(raw) -> list:
    if not raw:
        return []
    return _unwrap_file_ids(raw)


_FILE_IDS_REPAIRED: set[str] = set()


def repair_file_ids() -> int:
    """Разовое лечение многократно закодированных file_ids в активной БД.

    Испорченные строки начинаются с кавычки ('"\\"\\\\\\"...'), нормальные — с '['.
    Ничего не удаляет: только разворачивает слои. Если что-то починили — VACUUM,
    чтобы файл БД сжался (было 233 МБ на 563 поста). Возвращает число строк."""
    path = get_active_db_path()
    if path in _FILE_IDS_REPAIRED:
        return 0
    fixed = 0
    with get_connection() as conn:
        for table in ("saved_items", "trash"):
            try:
                rows = conn.execute(
                    f"SELECT id, file_ids FROM {table} WHERE file_ids LIKE '\"%'"
                ).fetchall()
            except sqlite3.OperationalError:
                continue
            for iid, raw in rows:
                conn.execute(
                    f"UPDATE {table} SET file_ids = ? WHERE id = ?",
                    (_file_ids_db_value(raw), iid),
                )
                fixed += 1
        conn.commit()
    if fixed:
        logger.info("file_ids: развёрнуто %s многократно закодированных записей — VACUUM", fixed)
        try:
            with get_connection() as conn:
                conn.execute("VACUUM")
        except sqlite3.OperationalError as e:
            logger.warning("VACUUM после лечения file_ids не удался: %s", e)
    _FILE_IDS_REPAIRED.add(path)
    return fixed


def get_items_by_category(category: str, limit: int = 20, offset: int = 0) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT id, content_type, summary, telegram_message_id, telegram_chat_id, created_at, file_ids, locked
               FROM saved_items WHERE category = ? ORDER BY created_at DESC LIMIT ? OFFSET ?""",
            (category, limit, offset),
        ).fetchall()
        return [
            {
                "id": r[0],
                "content_type": r[1],
                "summary": r[2],
                "message_id": r[3],
                "chat_id": r[4],
                "created_at": r[5],
                "media_count": len(_parse_file_ids(r[6])),
                "locked": bool(r[7]),
            }
            for r in rows
        ]


def get_item(item_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute(
            """SELECT id, category, content_type, summary, original_text, file_id, file_ids, media_group_id,
                      telegram_message_id, telegram_chat_id, locked, source_channel, comment, created_at
               FROM saved_items WHERE id = ?""",
            (item_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "category": row[1],
            "content_type": row[2],
            "summary": row[3],
            "original_text": row[4],
            "file_id": row[5],
            "file_ids": _parse_file_ids(row[6]),
            "media_group_id": row[7],
            "message_id": row[8],
            "chat_id": row[9],
            "locked": bool(row[10]),
            "source_channel": row[11],
            "comment": row[12] or "",
            "created_at": row[13] or "",
        }


def get_total_items(category: str) -> int:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM saved_items WHERE category = ?", (category,)
        ).fetchone()
        return row[0]


_RU2LAT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh", "з": "z", "и": "i", "й": "y",
    "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y", "ь": "", "э": "e",
    "ю": "yu", "я": "ya",
}
_LAT2RU = [("sch", "щ"), ("sh", "ш"), ("ch", "ч"), ("zh", "ж"), ("kh", "х"), ("ts", "ц"), ("ya", "я"),
           ("yu", "ю"), ("yo", "е"), ("ph", "ф"), ("th", "т"), ("ck", "к"), ("oo", "у"), ("ee", "и"),
           ("a", "а"), ("b", "б"), ("c", "к"), ("d", "д"), ("e", "е"), ("f", "ф"), ("g", "г"), ("h", "х"),
           ("i", "и"), ("j", "дж"), ("k", "к"), ("l", "л"), ("m", "м"), ("n", "н"), ("o", "о"), ("p", "п"),
           ("q", "к"), ("r", "р"), ("s", "с"), ("t", "т"), ("u", "у"), ("v", "в"), ("w", "в"), ("x", "кс"),
           ("y", "и"), ("z", "з")]
_EN_KEYS = "qwertyuiop[]asdfghjkl;'zxcvbnm,."
_RU_KEYS = "йцукенгшщзхъфывапролджэячсмитьбю"
_KEY_EN2RU = dict(zip(_EN_KEYS, _RU_KEYS))
_KEY_RU2EN = dict(zip(_RU_KEYS, _EN_KEYS))


def word_variants(word: str) -> set[str]:
    """Как ещё могли написать слово: транслитом (гта ↔ gta, ведьмак ↔ vedmak)
    и в другой раскладке (пеф → gta, lfhr → дарк). Все — в нижнем регистре, ё → е."""
    w = (word or "").casefold().replace("ё", "е")
    out = {w}
    if not w:
        return out
    if any("а" <= ch <= "я" for ch in w):
        out.add("".join(_RU2LAT.get(ch, ch) for ch in w))
        out.add("".join(_KEY_RU2EN.get(ch, ch) for ch in w))
    if any("a" <= ch <= "z" for ch in w):
        s, i, res = w, 0, []
        while i < len(s):
            for lat, ru in _LAT2RU:
                if s.startswith(lat, i):
                    res.append(ru)
                    i += len(lat)
                    break
            else:
                res.append(s[i])
                i += 1
        out.add("".join(res))
        out.add("".join(_KEY_EN2RU.get(ch, ch) for ch in w))
    return {v for v in out if len(v) >= min(2, len(w))}


def search_items(query: str) -> list[dict]:
    """Поиск по описанию, тексту поста (включая то, что ИИ увидел на картинке и
    услышал в видео) и тегам. Без учёта регистра — SQLite LIKE кириллицу не умеет
    («Крысы» не находил «крысы»), — и по всем словам запроса в любом порядке."""
    words = [w for w in re.split(r"\s+", (query or "").casefold().replace("ё", "е")) if w]
    if not words:
        return []
    variants = [word_variants(w) for w in words]
    topics = item_topics()
    arts = articles_brief()
    with get_connection() as conn:
        tag_text: dict[int, str] = {}
        for item_id, name, icon in conn.execute(
            "SELECT it.item_id, t.name, t.icon FROM item_tags it JOIN tags t ON t.id = it.tag_id"
        ).fetchall():
            tag_text[item_id] = tag_text.get(item_id, "") + f" {name or ''} {icon or ''}"
        rows = conn.execute(
            "SELECT id, category, content_type, summary, telegram_message_id, telegram_chat_id, original_text "
            "FROM saved_items ORDER BY created_at DESC, id DESC"
        ).fetchall()
    # Насколько хорошо совпало слово: 3 — целиком («кот»), 2 — форма слова («кота», «котик»),
    # 1 — просто внутри другого («которые»). Лучшие совпадения — первыми, при равенстве — свежие.
    pats = [[(v, re.compile(rf"(?<!\w){re.escape(v)}(?!\w)"), re.compile(rf"(?<!\w){re.escape(v)}\w{{0,3}}(?!\w)"))
             for v in vs] for vs in variants]

    def level(hay: str, vs) -> int:
        best = 0
        for v, whole, form in vs:
            if v not in hay:
                continue
            best = max(best, 3 if whole.search(hay) else 2 if form.search(hay) else 1)
            if best == 3:
                break
        return best

    found = []
    for pos, r in enumerate(rows):
        art = arts.get(r[0])
        hay = (f"{r[3] or ''} {r[6] or ''} {tag_text.get(r[0], '')} {' '.join(topics.get(r[0], []))}"
               f" {art[0] + ' ' + art[1] if art else ''}")
        hay = hay.casefold().replace("ё", "е")
        levels = [level(hay, vs) for vs in pats]
        if all(levels):
            found.append((-sum(levels), pos, r))
    favs = set(favorites())
    found.sort(key=lambda x: (x[2][0] not in favs, x[0], x[1]))   # ⭐ избранное — первым
    return [{"id": r[0], "category": r[1], "content_type": r[2], "summary": r[3],
             "message_id": r[4], "chat_id": r[5]} for _, _, r in found[:30]]


# ---------------------------- Статьи по ссылкам ----------------------------
# Текст страницы по ссылке из поста: ищется поиском и не пропадёт, если сайт удалит статью.
# status: ok — текст есть; none — в посте нет подходящей ссылки; fail — страница не отдалась.

ARTICLE_MAX = 8000


def article_get(item_id: int) -> dict | None:
    with get_connection() as conn:
        r = conn.execute("SELECT url, title, text, status, fetched_at, brief FROM item_articles WHERE item_id = ?",
                         (item_id,)).fetchone()
    if not r:
        return None
    return {"url": r[0] or "", "title": r[1] or "", "text": r[2] or "", "status": r[3] or "", "at": r[4] or "",
            "brief": r[5] or ""}


def article_set_brief(item_id: int, brief: str) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE item_articles SET brief = ? WHERE item_id = ?", ((brief or "")[:2000], item_id))
        conn.commit()


def article_links() -> list[dict]:
    """[{id, text, url}] всех постов (с адресом сохранённой статьи) — для поиска ссылок на Steam."""
    with get_connection() as conn:
        rows = conn.execute("SELECT s.id, s.original_text, a.url FROM saved_items s "
                            "LEFT JOIN item_articles a ON a.item_id = s.id "
                            "WHERE s.original_text LIKE '%steampowered%' OR a.url LIKE '%steampowered%'").fetchall()
    return [{"id": r[0], "text": r[1] or "", "url": r[2] or ""} for r in rows]


# ---------------------------- Избранное ----------------------------
# Список id в настройках (значит, и в бэкапе). Новые — в начале.

def favorites() -> list[int]:
    try:
        v = json.loads(get_setting("favorites", "[]") or "[]")
        return [int(x) for x in v if str(x).lstrip("-").isdigit()]
    except Exception:
        return []


def set_favorite(item_id: int, on: bool) -> None:
    cur = [i for i in favorites() if i != item_id]
    if on:
        cur.insert(0, item_id)
    set_setting("favorites", json.dumps(cur[:3000]))


def article_set(item_id: int, url: str, title: str, text: str, status: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO item_articles (item_id, url, title, text, status) VALUES (?, ?, ?, ?, ?)",
            (item_id, (url or "")[:1000], (title or "")[:300], (text or "")[:ARTICLE_MAX], status))
        conn.commit()


def articles_brief() -> dict[int, tuple[str, str]]:
    """{id: (заголовок, текст)} постов, у которых сохранён текст статьи — для поиска и архива."""
    with get_connection() as conn:
        return {r[0]: (r[1] or "", r[2] or "") for r in conn.execute(
            "SELECT item_id, title, text FROM item_articles WHERE status = 'ok'").fetchall()}


def items_needing_article(limit: int = 5) -> list[dict]:
    """Посты со ссылкой, по которым ещё не пробовали забрать статью (новые — первыми)."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT s.id, s.original_text FROM saved_items s LEFT JOIN item_articles a ON a.item_id = s.id "
            "WHERE a.item_id IS NULL AND s.original_text LIKE '%http%' ORDER BY s.id DESC LIMIT ?",
            (limit,)).fetchall()
    return [{"id": r[0], "text": r[1] or ""} for r in rows]


def count_articles() -> dict:
    with get_connection() as conn:
        rows = conn.execute("SELECT status, COUNT(*) FROM item_articles GROUP BY status").fetchall()
    return {r[0]: r[1] for r in rows}


def _norm_dup_text(t: str) -> str:
    t = re.sub(r"https?://\S+", " ", (t or "").split("\n\n〔", 1)[0])
    return re.sub(r"\W+", " ", t.casefold().replace("ё", "е")).strip()


def find_text_dups(min_letters: int = 40) -> list[list[int]]:
    """Посты с одинаковым текстом (тот же пост переслан дважды) — группы id, старые первыми."""
    with get_connection() as conn:
        # только посты без картинок/видео: у мемов одного канала подпись часто одинаковая,
        # а картинки разные — их сравнивают по файлу и по похожести картинки
        rows = conn.execute("SELECT id, original_text FROM saved_items WHERE COALESCE(file_ids, '') IN ('', '[]') "
                            "AND (file_id IS NULL OR file_id = '') ORDER BY id").fetchall()
    groups: dict[str, list[int]] = {}
    for iid, text in rows:
        key = _norm_dup_text(text)
        if len(key.replace(" ", "")) >= min_letters:
            groups.setdefault(key, []).append(iid)
    return [ids for ids in groups.values() if len(ids) >= 2]


def month_stats(start_dt, end_dt) -> dict:
    """Итоги периода: сколько сохранено, по папкам и типам."""
    a, b = start_dt.strftime("%Y-%m-%d %H:%M:%S"), end_dt.strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        ids = [r[0] for r in conn.execute(
            "SELECT id FROM saved_items WHERE created_at >= ? AND created_at < ?", (a, b)).fetchall()]
        cats = conn.execute(
            "SELECT category, COUNT(*) FROM saved_items WHERE created_at >= ? AND created_at < ? "
            "GROUP BY category ORDER BY 2 DESC", (a, b)).fetchall()
        kinds = conn.execute(
            "SELECT content_type, COUNT(*) FROM saved_items WHERE created_at >= ? AND created_at < ? "
            "GROUP BY content_type ORDER BY 2 DESC", (a, b)).fetchall()
        total = conn.execute("SELECT COUNT(*) FROM saved_items").fetchone()[0]
    return {"ids": ids, "cats": [(r[0], r[1]) for r in cats], "kinds": [(r[0], r[1]) for r in kinds],
            "total": total}


def item_topics() -> dict[int, list[str]]:
    """ИИ-темы постов {id: ["GTA 6", ...]} (хранятся в настройках — попадают в бэкап).
    Пустой список = пост уже размечен, конкретики нет."""
    try:
        raw = json.loads(get_setting("topics", "{}") or "{}")
        return {int(k): v for k, v in raw.items() if isinstance(v, list)}
    except Exception:
        return {}


def set_item_topics(updates: dict[int, list[str]]) -> None:
    if not updates:
        return
    cur = item_topics()
    cur.update({int(k): list(v)[:3] for k, v in updates.items()})
    set_setting("topics", json.dumps({str(k): v for k, v in cur.items()}, ensure_ascii=False))


def items_for_smart_search() -> list[dict]:
    """Всё, по чему ищем «по смыслу»: описание, текст с распознанным, канал, теги, дата."""
    with get_connection() as conn:
        tag_text: dict[int, str] = {}
        for item_id, name, icon in conn.execute(
            "SELECT it.item_id, t.name, t.icon FROM item_tags it JOIN tags t ON t.id = it.tag_id"
        ).fetchall():
            tag_text[item_id] = tag_text.get(item_id, "") + f" {name or ''} {icon or ''}"
        rows = conn.execute(
            "SELECT id, category, content_type, summary, original_text, source_channel, created_at, locked "
            "FROM saved_items ORDER BY id DESC"
        ).fetchall()
    topics = item_topics()
    arts = articles_brief()
    return [{"id": r[0], "category": r[1], "content_type": r[2], "summary": r[3] or "",
             "original_text": r[4] or "", "source_channel": r[5] or "", "created_at": r[6] or "",
             "locked": bool(r[7]), "tags": tag_text.get(r[0], ""), "topics": topics.get(r[0], []),
             "article": arts.get(r[0], ("", ""))} for r in rows]


def search_items_between(start_dt, end_dt) -> list[dict]:
    """Посты, сохранённые между двумя датами (UTC)."""
    s = start_dt.strftime("%Y-%m-%d %H:%M:%S")
    e = end_dt.strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT id, category, content_type, summary, telegram_message_id, telegram_chat_id, locked
               FROM saved_items
               WHERE created_at BETWEEN ? AND ?
               ORDER BY created_at DESC LIMIT 1000""",
            (s, e),
        ).fetchall()
        return [
            {
                "id": r[0],
                "category": r[1],
                "content_type": r[2],
                "summary": r[3],
                "message_id": r[4],
                "chat_id": r[5],
                "locked": bool(r[6]),
            }
            for r in rows
        ]


def get_stats() -> dict:
    with get_connection() as conn:
        total = conn.execute("SELECT COUNT(*) FROM saved_items").fetchone()[0]
        by_type = conn.execute(
            "SELECT content_type, COUNT(*) FROM saved_items GROUP BY content_type"
        ).fetchall()
        return {"total": total, "by_type": {r[0]: r[1] for r in by_type}}


def count_all_items() -> int:
    with get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM saved_items").fetchone()[0]


def admin_stats() -> dict:
    """Статистика для владельца по всем пользователям: число уникальных пользователей
    и суммарное число постов в их БД."""
    users = _load_users()
    total_posts = 0
    per_user = {}
    for uid in users:
        path = user_db_path(uid)
        if not os.path.exists(path):
            continue
        try:
            con = sqlite3.connect(path)
            try:
                n = con.execute("SELECT COUNT(*) FROM saved_items").fetchone()[0]
            finally:
                con.close()
        except Exception:
            n = 0
        total_posts += n
        per_user[uid] = n
    return {"users": len(users), "total_posts": total_posts, "per_user": per_user}


def update_item_category(item_id: int, category: str, summary: str | None = None) -> None:
    with get_connection() as conn:
        old = conn.execute("SELECT category, summary FROM saved_items WHERE id = ?", (item_id,)).fetchone()
        old_cat = old[0] if old else None
        old_sum = old[1] if old else None
        if not old or (old_cat == category and (summary is None or old_sum == summary)):
            return
        conn.execute(
            "UPDATE saved_items SET category = ?, summary = COALESCE(?, summary) WHERE id = ?",
            (category, summary, item_id),
        )
        if old_cat != category:
            add_history(item_id, "category", old_cat, category, conn=conn)
        if summary is not None and old_sum != summary:
            add_history(item_id, "summary", old_sum, summary, conn=conn)
        conn.commit()


def get_icon_overrides() -> dict:
    with get_connection() as conn:
        return {r[0]: r[1] for r in conn.execute("SELECT category, icon FROM icon_overrides").fetchall()}


def set_category_icon(category: str, icon: str) -> None:
    with get_connection() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO icon_overrides (category, icon) VALUES (?, ?)",
            (category, icon),
        )
        conn.commit()


def recent_items(limit: int = 10) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, category, content_type, summary, file_ids, locked, created_at "
            "FROM saved_items ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "id": r[0],
                "category": r[1],
                "content_type": r[2],
                "summary": r[3] or "",
                "media_count": len(_parse_file_ids(r[4])),
                "locked": bool(r[5]),
                "created_at": r[6],
            }
            for r in rows
        ]


def all_summaries() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, category, summary, original_text FROM saved_items ORDER BY id DESC"
        ).fetchall()
        return [
            {"id": r[0], "category": r[1], "summary": r[2] or "", "original_text": r[3] or ""}
            for r in rows
        ]


def set_item_summary(item_id: int, summary: str) -> None:
    with get_connection() as conn:
        old = conn.execute("SELECT summary FROM saved_items WHERE id = ?", (item_id,)).fetchone()
        conn.execute(
            "UPDATE saved_items SET summary = ? WHERE id = ?", (summary, item_id)
        )
        if old and (old[0] or "") != (summary or ""):
            add_history(item_id, "summary", old[0] or "", summary or "", conn=conn)
        conn.commit()


def archive_older_than(days: int, archive_cat: str = "Архив") -> int:
    from datetime import datetime, timedelta

    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        ids = [
            r[0]
            for r in conn.execute(
                "SELECT id FROM saved_items WHERE created_at < ?", (cutoff,)
            ).fetchall()
        ]
        if ids:
            marks = ",".join("?" * len(ids))
            conn.execute(
                f"UPDATE saved_items SET category = ? WHERE id IN ({marks})",
                [archive_cat] + ids,
            )
        conn.execute("INSERT OR IGNORE INTO categories(name) VALUES (?)", (archive_cat,))
        conn.commit()
        return len(ids)


def items_with_tag(tag_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT i.id, i.category, i.content_type, i.summary, i.file_ids, i.locked
               FROM saved_items i JOIN item_tags it ON it.item_id = i.id
               WHERE it.tag_id = ? ORDER BY i.id""",
            (tag_id,),
        ).fetchall()
        return [
            {
                "id": r[0],
                "category": r[1],
                "content_type": r[2],
                "summary": r[3] or "",
                "media_count": len(_parse_file_ids(r[4])),
                "locked": bool(r[5]),
            }
            for r in rows
        ]


def find_dup_media(primary_file_id: str) -> dict | None:
    if not primary_file_id:
        return None
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, category, summary, locked FROM saved_items WHERE file_id = ? LIMIT 1",
            (primary_file_id,),
        ).fetchone()
    if not row:
        return None
    return {"id": row[0], "category": row[1], "summary": row[2] or "", "locked": bool(row[3])}


def find_by_text_fragments(fragments: list[str]) -> dict | None:
    """Последний пост, в тексте которого есть любой из кусков (с учётом регистра —
    например ссылка на одно видео YouTube в разных видах). Только чтение."""
    frags = [f for f in fragments if f]
    if not frags:
        return None
    where = " OR ".join("instr(original_text, ?) > 0" for _ in frags)
    with get_connection() as conn:
        row = conn.execute(
            f"SELECT id, category, summary, locked FROM saved_items WHERE {where} ORDER BY id DESC LIMIT 1",
            frags,
        ).fetchone()
    if not row:
        return None
    return {"id": row[0], "category": row[1], "summary": row[2] or "", "locked": bool(row[3])}


def items_with_text_fragments(fragments: list[str], limit: int = 200) -> list[dict]:
    """Посты (новые сначала), в тексте которых есть любой из кусков — кандидаты,
    которые вызывающий сверяет точнее (например ссылку целиком). Только чтение."""
    frags = [f for f in fragments if f]
    if not frags:
        return []
    where = " OR ".join("instr(original_text, ?) > 0" for _ in frags)
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT id, category, summary, original_text FROM saved_items WHERE {where} "
            f"ORDER BY id DESC LIMIT ?",
            [*frags, int(limit)],
        ).fetchall()
    return [{"id": r[0], "category": r[1], "summary": r[2] or "", "original_text": r[3] or ""} for r in rows]


def find_dup_text(original_text: str) -> dict | None:
    """Пост с тем же текстом. Сравниваем без распознанных приписок (превью ссылки,
    описание картинки): раньше они хранились в original_text, и дубль поста со
    ссылкой не находился никогда. lower() в SQLite не понимает кириллицу, поэтому
    нормализуем в Python."""
    from postctx import strip_aux

    norm = re.sub(r"\s+", " ", strip_aux(original_text or "").lower()).strip()
    if not norm:
        return None
    row = None
    with get_connection() as conn:
        for r in conn.execute(
            "SELECT id, category, summary, locked, original_text FROM saved_items ORDER BY id ASC"
        ):
            t = r[4] or ""
            if t.strip() and re.sub(r"\s+", " ", strip_aux(t).lower()).strip() == norm:
                row = r[:4]
                break
    if not row:
        return None
    return {"id": row[0], "category": row[1], "summary": row[2] or "", "locked": bool(row[3])}


_STEM_WORD = re.compile(r"[^\W\d_]{4,}")
_STOP_STEMS = {
    "кото", "этот", "этого", "были", "было", "будет", "можн", "очен", "свой", "свое", "всег", "посл",
    "тепе", "тольк", "сейч", "пото", "чтоб", "если", "когд", "где", "здес", "with", "that", "this",
    "from", "have", "your", "about", "пост", "подп", "кана", "ссыл", "изоб", "текс", "реал", "фото",
    "видео", "виде", "рису", "кадр", "teleg", "приват", "join", "group", "chat", "messa", "secur",
    "power", "описа", "автор", "превь", "youtu", "https", "изобр", "сцена", "жанр",
}


def _stems(text: str) -> set[str]:
    out = set()
    for w in _STEM_WORD.findall((text or "").lower()):
        s = w[:5]
        if s not in _STOP_STEMS and s[:4] not in _STOP_STEMS:
            out.add(s)
    return out


def similar_items(text: str, limit: int = 4, exclude_id: int | None = None) -> list[tuple[str, str]]:
    """Похожие посты из архива [(summary, category)] — как пользователь сам их разложил.

    Простое сходство по «основам» слов (первые 5 букв) с весом редкости (idf):
    без эмбеддингов и внешних API, на ~1000 постов считается за миллисекунды.
    Ручные перемещения (замок) в приоритете — это прямые указания пользователя."""
    import math

    q = _stems(text)
    if len(q) < 2:
        return []
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, category, summary, substr(original_text, 1, 600), locked FROM saved_items"
        ).fetchall()
    if not rows:
        return []
    docs = []
    df: dict[str, int] = {}
    for iid, cat, summ, orig, locked in rows:
        if exclude_id is not None and iid == exclude_id:
            continue
        st = _stems(f"{summ or ''} {orig or ''}")
        docs.append((iid, cat, summ or "", st, bool(locked)))
        for s in st:
            df[s] = df.get(s, 0) + 1
    n = max(len(docs), 1)
    # на большом архиве нужно совпадение хотя бы пары редких слов, иначе «похожие»
    # посты случайны и только сбивают модель
    threshold = 7.0 if n >= 50 else 3.0
    scored = []
    for iid, cat, summ, st, locked in docs:
        common = q & st
        if len(common) < 2:
            continue
        score = sum(math.log(1 + n / df[s]) for s in common)
        if locked:
            score *= 1.3
        scored.append((score, iid, summ, cat))
    scored.sort(reverse=True)
    out, seen = [], set()
    for score, iid, summ, cat in scored:
        if score < threshold:
            break
        key = summ[:60]
        if key in seen or not summ:
            continue
        seen.add(key)
        out.append((summ, cat))
        if len(out) >= limit:
            break
    return out


def channel_category_counts(source: str | None, limit: int = 3) -> list[tuple[str, int]]:
    """Куда раньше попадали посты из этого канала: [(category, n)], только n ≥ 2."""
    if not source:
        return []
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT category, COUNT(*) FROM saved_items WHERE source_channel = ? "
            "GROUP BY category ORDER BY 2 DESC LIMIT ?",
            (source, limit),
        ).fetchall()
    return [(c, n) for c, n in rows if n >= 2]


def archive_snapshot() -> dict:
    """Сводка архива для сверки с бэкапом: счётчики и «подпись» каждого поста."""
    out = {"counts": {}, "items": {}}
    with get_connection() as conn:
        for t in ("saved_items", "categories", "folders", "tags", "item_tags", "trash"):
            try:
                out["counts"][t] = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            except sqlite3.OperationalError:
                out["counts"][t] = 0
        for r in conn.execute(
            "SELECT id, category, summary, original_text, file_ids, locked, source_channel FROM saved_items"
        ).fetchall():
            out["items"][r[0]] = (
                r[1] or "", r[2] or "", r[3] or "",
                json.dumps(_parse_file_ids(r[4]), ensure_ascii=False, sort_keys=True), int(r[5] or 0), r[6] or "",
            )
    return out


# Отпечатки картинок (imghash) — отдельная таблица: не входит в экспорт/бэкап и
# после восстановления просто пересчитывается фоном. «-» = посчитать не удалось.
_IMG_HASH_DDL = "CREATE TABLE IF NOT EXISTS image_hashes (item_id INTEGER PRIMARY KEY, hash TEXT NOT NULL)"


def set_image_hash(item_id: int, h: str) -> None:
    with get_connection() as conn:
        conn.execute(_IMG_HASH_DDL)
        conn.execute("INSERT OR REPLACE INTO image_hashes (item_id, hash) VALUES (?, ?)", (item_id, h))
        conn.commit()


def image_hashes() -> list[tuple[int, str]]:
    """[(item_id, отпечаток)] только для живых постов."""
    with get_connection() as conn:
        conn.execute(_IMG_HASH_DDL)
        return [
            (r[0], r[1])
            for r in conn.execute(
                "SELECT h.item_id, h.hash FROM image_hashes h JOIN saved_items s ON s.id = h.item_id "
                "WHERE h.hash != '-'"
            ).fetchall()
        ]


def items_missing_hash(limit: int = 50) -> list[dict]:
    """Фото/видео без отпечатка (для фонового догона), свежие первыми."""
    with get_connection() as conn:
        conn.execute(_IMG_HASH_DDL)
        rows = conn.execute(
            "SELECT id, telegram_chat_id, telegram_message_id FROM saved_items "
            "WHERE content_type IN ('photo', 'video', 'animation') AND telegram_message_id IS NOT NULL "
            "AND id NOT IN (SELECT item_id FROM image_hashes) ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [{"id": r[0], "chat_id": r[1], "message_id": r[2]} for r in rows]


def channel_category_matrix() -> list[tuple[str, str, int]]:
    """[(канал, категория, n)] по всему архиву — для подсказок правил каналов."""
    with get_connection() as conn:
        return [
            (r[0], r[1], r[2])
            for r in conn.execute(
                "SELECT source_channel, category, COUNT(*) FROM saved_items "
                "WHERE source_channel IS NOT NULL AND source_channel != '' GROUP BY 1, 2"
            ).fetchall()
        ]


def update_item_text(item_id: int, original_text: str) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE saved_items SET original_text = ? WHERE id = ?", (original_text, item_id))
        conn.commit()


def queue_llm_retry(item_id: int, fallback_category: str) -> None:
    """Пост, который не удалось распознать (Groq лёг/лимиты) — дораспознаем позже."""
    q = _load_retry_queue()
    q[str(item_id)] = {"cat": fallback_category, "n": q.get(str(item_id), {}).get("n", 0)}
    set_setting("llm_retry", json.dumps(q, ensure_ascii=False))


def _load_retry_queue() -> dict:
    try:
        q = json.loads(get_setting("llm_retry", "{}") or "{}")
        return q if isinstance(q, dict) else {}
    except Exception:
        return {}


def retry_queue_items() -> dict:
    return _load_retry_queue()


def retry_queue_done(item_id: int, failed: bool = False, max_attempts: int = 6) -> None:
    q = _load_retry_queue()
    key = str(item_id)
    if key in q and failed and q[key].get("n", 0) + 1 < max_attempts:
        q[key]["n"] = q[key].get("n", 0) + 1
    else:
        q.pop(key, None)
    set_setting("llm_retry", json.dumps(q, ensure_ascii=False))


def find_dupes(file_unique_ids: list[str]) -> list[dict]:
    """Посты, в которых уже есть одно из вложений (по стабильным токенам файлов).

    Токены точные (например "photo:12345"), поэтому сопоставляем их как множества,
    а не через LIKE — иначе "photo:123" совпал бы с "photo:1234".
    """
    targets = {u for u in (file_unique_ids or []) if u}
    if not targets:
        return []
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, category, summary, locked, file_unique FROM saved_items"
        ).fetchall()
    seen: dict[int, dict] = {}
    for r in rows:
        own = {o for o in (r[4] or "").split() if o}
        if not own or not (targets & own):
            continue
        if r[0] not in seen:
            seen[r[0]] = {
                "id": r[0],
                "category": r[1],
                "summary": r[2] or "",
                "locked": bool(r[3]),
            }
    return list(seen.values())


def find_archive_dups(limit: int = 20) -> list[tuple[str, list[dict]]]:
    """Группы дублей во всём архиве: токен файла → посты с этим файлом (где >= 2 постов)."""
    with get_connection() as conn:
        items = conn.execute(
            "SELECT id, category, content_type, summary, locked, file_unique FROM saved_items ORDER BY id"
        ).fetchall()
    groups: dict[str, list[dict]] = {}
    for r in items:
        for tok in (r[5] or "").split():
            if not tok:
                continue
            lst = groups.setdefault(tok, [])
            if len(lst) < limit // 2 + 8:
                lst.append(
                    {
                        "id": r[0],
                        "category": r[1],
                        "content_type": r[2],
                        "summary": r[3] or "",
                        "locked": bool(r[4]),
                    }
                )
    raw = [(t, lst) for t, lst in groups.items() if len(lst) >= 2]
    raw.sort(key=lambda x: -len(x[1]))
    # Альбом=несколько файлов создаёт по одной группе на каждый токен с одним и тем же
    # набором постов — схлопываем группы с одинаковыми id: для одной сохранённой пары
    # (альбом video+фото) остаётся одна группа, а не N.
    merged: list[tuple[str, list[dict]]] = []
    seen_ids: set = set()
    for tok, lst in raw:
        id_set = tuple(sorted(x["id"] for x in lst))
        if id_set in seen_ids:
            continue
        seen_ids.add(id_set)
        merged.append((tok, lst))
        if len(merged) >= limit:
            break
    return merged


def stats_since(start_dt) -> list[dict]:
    """Количество новых постов по категориям с указанной даты (UTC)."""
    s = start_dt.strftime("%Y-%m-%d %H:%M:%S")
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT category, COUNT(*) FROM saved_items WHERE created_at >= ? "
            "GROUP BY category ORDER BY 2 DESC",
            (s,),
        ).fetchall()
        return [{"category": r[0], "count": r[1]} for r in rows]


def delete_item(item_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM saved_items WHERE id = ?", (item_id,))
        conn.commit()


def get_item_ids(category: str | None = None) -> list[int]:
    with get_connection() as conn:
        if category:
            rows = conn.execute(
                "SELECT id FROM saved_items WHERE category = ?", (category,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT id FROM saved_items").fetchall()
        return [r[0] for r in rows]


def items_for_recat_scan() -> list[dict]:
    """Лёгкая выборка всех постов для умного отбора на перераспознавание."""
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT id, category, content_type, original_text, locked FROM saved_items"
        ).fetchall()
    return [
        {"id": r[0], "category": r[1], "content_type": r[2], "original_text": r[3] or "", "locked": r[4]}
        for r in rows
    ]


def get_items_by_ids(ids) -> list[dict]:
    """Возвращает посты с указанными id в порядке возрастания id."""
    ids = [int(i) for i in ids]
    if not ids:
        return []
    marks = ",".join("?" * len(ids))
    with get_connection() as conn:
        rows = conn.execute(
            f"SELECT id, category, content_type, summary, telegram_message_id, created_at "
            f"FROM saved_items WHERE id IN ({marks}) ORDER BY id",
            ids,
        ).fetchall()
    return [
        {
            "id": r[0],
            "category": r[1],
            "content_type": r[2],
            "summary": r[3] or "",
            "message_id": r[4],
            "created_at": r[5],
        }
        for r in rows
    ]


def get_locked_folders() -> list[str]:
    with get_connection() as conn:
        rows = conn.execute("SELECT name FROM folders WHERE locked = 1 ORDER BY name").fetchall()
        return [r[0] for r in rows]


def get_locked_categories() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT c.name, COALESCE(c.group_name, ''), COUNT(i.id)
               FROM categories c
               LEFT JOIN saved_items i ON i.category = c.name
               WHERE c.locked = 1
               GROUP BY c.name ORDER BY c.name"""
        ).fetchall()
        return [{"category": r[0], "group": r[1], "count": r[2]} for r in rows]


def get_locked_items() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT id, category, content_type, summary, file_ids
               FROM saved_items WHERE locked = 1 ORDER BY category, id"""
        ).fetchall()
        return [
            {
                "id": r[0],
                "category": r[1],
                "content_type": r[2],
                "summary": r[3] or "",
                "media_count": len(_parse_file_ids(r[4])),
            }
            for r in rows
        ]


# ---------------------------- Теги ----------------------------

def get_tags() -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("SELECT id, icon, name FROM tags ORDER BY id").fetchall()
        return [{"id": r[0], "icon": r[1], "name": r[2] or ""} for r in rows]


def add_tag(icon: str, name: str = "") -> int:
    with get_connection() as conn:
        cur = conn.execute("INSERT INTO tags (icon, name) VALUES (?, ?)", (icon, name or ""))
        return cur.lastrowid


def delete_tag(tag_id: int) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
        conn.execute("DELETE FROM item_tags WHERE tag_id = ?", (tag_id,))
        conn.execute("DELETE FROM folder_tags WHERE tag_id = ?", (tag_id,))
        conn.execute("DELETE FROM category_tags WHERE tag_id = ?", (tag_id,))


def set_tag_name(tag_id: int, name: str) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE tags SET name = ? WHERE id = ?", (name or "", tag_id))


def set_tag_icon(tag_id: int, icon: str) -> None:
    with get_connection() as conn:
        conn.execute("UPDATE tags SET icon = ? WHERE id = ?", (icon, tag_id))


def get_item_tags(item_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT t.id, t.icon, t.name FROM tags t
               JOIN item_tags it ON it.tag_id = t.id WHERE it.item_id = ? ORDER BY t.id""",
            (item_id,),
        ).fetchall()
        return [{"id": r[0], "icon": r[1], "name": r[2] or ""} for r in rows]


def set_item_tags(item_id: int, tag_ids) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM item_tags WHERE item_id = ?", (item_id,))
        for tid in tag_ids:
            conn.execute("INSERT OR IGNORE INTO item_tags (item_id, tag_id) VALUES (?, ?)", (item_id, tid))


def get_folder_tags(path: str) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT t.id, t.icon, t.name FROM tags t
               JOIN folder_tags ft ON ft.tag_id = t.id WHERE ft.path = ? ORDER BY t.id""",
            (path,),
        ).fetchall()
        return [{"id": r[0], "icon": r[1], "name": r[2] or ""} for r in rows]


def set_folder_tags(path: str, tag_ids) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM folder_tags WHERE path = ?", (path,))
        for tid in tag_ids:
            conn.execute("INSERT OR IGNORE INTO folder_tags (path, tag_id) VALUES (?, ?)", (path, tid))


def get_category_tags(category: str) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT t.id, t.icon, t.name FROM tags t
               JOIN category_tags ct ON ct.tag_id = t.id WHERE ct.category = ? ORDER BY t.id""",
            (category,),
        ).fetchall()
        return [{"id": r[0], "icon": r[1], "name": r[2] or ""} for r in rows]


def set_category_tags(category: str, tag_ids) -> None:
    with get_connection() as conn:
        conn.execute("DELETE FROM category_tags WHERE category = ?", (category,))
        for tid in tag_ids:
            conn.execute("INSERT OR IGNORE INTO category_tags (category, tag_id) VALUES (?, ?)", (category, tid))


def _move_items_to_trash(conn, item_ids: list[int]) -> None:
    names = [d[1] for d in conn.execute("PRAGMA table_info(saved_items)").fetchall()]
    for iid in item_ids:
        row = conn.execute("SELECT * FROM saved_items WHERE id = ?", (iid,)).fetchone()
        if row:
            conn.execute(
                f"INSERT OR REPLACE INTO trash ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
                row,
            )
            conn.execute("DELETE FROM saved_items WHERE id = ?", (iid,))


def delete_category(category: str) -> int:
    """Удаляет категорию и её теги; все её посты уходят в корзину (теги постов сохраняются)."""
    with get_connection() as conn:
        con = conn.execute("SELECT id FROM saved_items WHERE category = ?", (category,)).fetchall()
        ids = [r[0] for r in con]
        _move_items_to_trash(conn, ids)
        conn.execute("DELETE FROM category_tags WHERE category = ?", (category,))
        conn.execute("DELETE FROM categories WHERE name = ?", (category,))
        conn.commit()
        return len(ids)


def folder_post_count(path: str) -> int:
    path = (path or "").strip("/")
    prefix = path + "/"
    with get_connection() as conn:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM categories WHERE group_name = ? OR group_name LIKE ? || '%'",
            (path, prefix),
        ).fetchall()]
        if not names:
            return 0
        marks = ",".join("?" * len(names))
        return conn.execute(
            f"SELECT COUNT(*) FROM saved_items WHERE category IN ({marks})", names
        ).fetchone()[0]


def delete_folder(path: str) -> int:
    """Удаляет папку (и вложенные); все посты внутри уходят в корзину (теги постов сохраняются)."""
    path = (path or "").strip("/")
    prefix = path + "/"
    total = 0
    with get_connection() as conn:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM categories WHERE group_name = ? OR group_name LIKE ? || '%'",
            (path, prefix),
        ).fetchall()]
        for name in names:
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM saved_items WHERE category = ?", (name,)
            ).fetchall()]
            _move_items_to_trash(conn, ids)
            conn.execute("DELETE FROM category_tags WHERE category = ?", (name,))
            conn.execute("DELETE FROM categories WHERE name = ?", (name,))
            total += len(ids)
        """Категории уровня корня/подпапок тоже переезжают в корзину — и таблицы папок чистим рекурсивно."""
        conn.execute("DELETE FROM folder_tags WHERE path = ? OR path LIKE ? || '%'", (path, prefix))
        conn.execute("DELETE FROM folders WHERE name = ? OR name LIKE ? || '%'", (path, prefix))
        conn.commit()
        return total


def tag_counts() -> dict:
    """Число постов с каждым тегом (без учёта корзины)."""
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT it.tag_id, COUNT(DISTINCT it.item_id)
               FROM item_tags it JOIN saved_items s ON s.id = it.item_id
               GROUP BY it.tag_id"""
        ).fetchall()
        return {r[0]: r[1] for r in rows}


def items_by_tags(tag_ids, limit: int = 50) -> list[dict]:
    """Все элементы, имеющие ЛЮБОЙ из тегов (OR)."""
    if not tag_ids:
        return []
    marks = ",".join("?" * len(tag_ids))
    with get_connection() as conn:
        rows = conn.execute(
            f"""SELECT DISTINCT i.id, i.category, i.content_type, i.summary, i.file_ids, i.locked
                FROM saved_items i
                JOIN item_tags it ON it.item_id = i.id
                WHERE it.tag_id IN ({marks})
                ORDER BY i.category, i.id LIMIT ?""",
            (*tag_ids, limit),
        ).fetchall()
        return [
            {
                "id": r[0],
                "category": r[1],
                "content_type": r[2],
                "summary": r[3] or "",
                "media_count": len(_parse_file_ids(r[4])),
                "locked": bool(r[5]),
            }
            for r in rows
        ]


def cats_by_tags(tag_ids) -> list[dict]:
    if not tag_ids:
        return []
    marks = ",".join("?" * len(tag_ids))
    with get_connection() as conn:
        rows = conn.execute(
            f"""SELECT DISTINCT c.name, COALESCE(c.group_name, ''), COUNT(i.id)
                FROM categories c
                JOIN category_tags ct ON ct.category = c.name
                LEFT JOIN saved_items i ON i.category = c.name
                WHERE ct.tag_id IN ({marks})
                GROUP BY c.name ORDER BY c.name""",
            (*tag_ids,),
        ).fetchall()
        return [{"category": r[0], "group": r[1], "count": r[2]} for r in rows]


def folders_by_tags(tag_ids) -> list[dict]:
    if not tag_ids:
        return []
    marks = ",".join("?" * len(tag_ids))
    with get_connection() as conn:
        rows = conn.execute(
            f"""SELECT DISTINCT ft.path FROM folder_tags ft
                WHERE ft.tag_id IN ({marks}) ORDER BY ft.path""",
            (*tag_ids,),
        ).fetchall()
        return [r[0] for r in rows]


# ---------------------------- Номера постов ----------------------------

def _item_seq_floor(conn) -> int:
    """Самый большой номер поста, который когда-либо выдавался: живые, корзина, история,
    плюс запомненный в бэкапе. После восстановления из бэкапа SQLite считает с max(живых),
    и новый пост получал номер удалённого — с его темами, «просмотрено» и напоминанием,
    а возврат из корзины затирал новый пост (INSERT OR REPLACE)."""
    best = 0
    for sql in ("SELECT MAX(id) FROM saved_items", "SELECT MAX(id) FROM trash",
                "SELECT MAX(item_id) FROM item_history",
                "SELECT seq FROM sqlite_sequence WHERE name = 'saved_items'"):
        try:
            v = conn.execute(sql).fetchone()
            if v and v[0] is not None:
                best = max(best, int(v[0]))
        except Exception:
            continue
    try:
        v = conn.execute("SELECT value FROM settings WHERE key = 'item_seq'").fetchone()
        if v and v[0]:
            best = max(best, int(v[0]))
    except Exception:
        pass
    return best


def _bump_item_seq(conn) -> None:
    floor = _item_seq_floor(conn)
    if floor <= 0:
        return
    cur = conn.execute("SELECT seq FROM sqlite_sequence WHERE name = 'saved_items'").fetchone()
    if cur is None:
        conn.execute("INSERT INTO sqlite_sequence (name, seq) VALUES ('saved_items', ?)", (floor,))
    elif int(cur[0] or 0) < floor:
        conn.execute("UPDATE sqlite_sequence SET seq = ? WHERE name = 'saved_items'", (floor,))


def bump_item_seq() -> None:
    with get_connection() as conn:
        _bump_item_seq(conn)
        conn.commit()


# ---------------------------- Корзина ----------------------------

def trash_item(item_id: int) -> bool:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM saved_items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            return False
        names = [d[1] for d in conn.execute("PRAGMA table_info(saved_items)").fetchall()]
        if conn.execute("SELECT 1 FROM trash WHERE id = ?", (item_id,)).fetchone():
            # в корзине уже лежит другой пост с этим номером (старый баг нумерации) —
            # отодвигаем его на отрицательный номер, а не затираем
            low = conn.execute("SELECT MIN(id) FROM trash").fetchone()[0] or 0
            conn.execute("UPDATE trash SET id = ? WHERE id = ?", (min(low, 0) - 1, item_id))
        conn.execute(
            f"INSERT INTO trash ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
            row,
        )
        conn.execute("DELETE FROM saved_items WHERE id = ?", (item_id,))
        add_history(item_id, "trash", row[1] if row else None, "deleted", conn=conn)
        conn.commit()
        return True


def get_trashed(limit: int = 30) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT id, category, content_type, summary, file_ids, telegram_message_id, deleted_at
               FROM trash ORDER BY deleted_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [
            {
                "id": r[0],
                "category": r[1],
                "content_type": r[2],
                "summary": r[3] or "",
                "media_count": len(_parse_file_ids(r[4])),
                "message_id": r[5],
                "deleted_at": r[6],
            }
            for r in rows
        ]


def count_trashed() -> int:
    with get_connection() as conn:
        return conn.execute("SELECT COUNT(*) FROM trash").fetchone()[0]


def restore_item(item_id: int) -> bool:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM trash WHERE id = ?", (item_id,)).fetchone()
        if not row:
            return False
        names = [
            d[1] for d in conn.execute("PRAGMA table_info(trash)").fetchall() if d[1] != "deleted_at"
        ]
        vals = list(row[: len(names)])
        busy = item_id <= 0 or conn.execute(
            "SELECT 1 FROM saved_items WHERE id = ?", (item_id,)).fetchone() is not None
        if busy:
            # номер уже у другого поста: никогда не затираем живой пост, даём новый номер
            _bump_item_seq(conn)
            names, vals = names[1:], vals[1:]
        cur = conn.execute(
            f"INSERT INTO saved_items ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
            vals,
        )
        new_id = cur.lastrowid if busy else item_id
        conn.execute("INSERT OR IGNORE INTO categories(name) VALUES (?)", (row[1],))
        conn.execute("DELETE FROM trash WHERE id = ?", (item_id,))
        add_history(new_id, "restore", None, row[1], conn=conn)
        conn.commit()
        return True


def restore_all_trash() -> int:
    ids = [it["id"] for it in get_trashed(limit=100000)]
    for iid in ids:
        restore_item(iid)
    return len(ids)


def purge_trash_item(item_id: int) -> dict | None:
    """Удаляет пост из корзины навсегда. Возвращает данные -> dict|None (для undo)."""
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM trash WHERE id = ?", (item_id,)).fetchone()
        if not row:
            return None
        names = [d[1] for d in conn.execute("PRAGMA table_info(trash)").fetchall()]
        data = {"trash": {n: v for n, v in zip(names, row)}}
        data["tags"] = [
            r[0]
            for r in conn.execute("SELECT tag_id FROM item_tags WHERE item_id = ?", (item_id,)).fetchall()
        ]
        conn.execute("DELETE FROM trash WHERE id = ?", (item_id,))
        conn.execute("DELETE FROM item_tags WHERE item_id = ?", (item_id,))
        conn.commit()
        return data


def restore_purged(data: dict) -> None:
    """Возвращает навсегда удалённый пост обратно в корзину (для undo)."""
    t = data.get("trash") or {}
    if not t:
        return
    with get_connection() as conn:
        names = [d[1] for d in conn.execute("PRAGMA table_info(trash)").fetchall()]
        vals = [t.get(n) for n in names]
        conn.execute(
            f"INSERT OR REPLACE INTO trash ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
            vals,
        )
        for tid in data.get("tags", []):
            conn.execute("INSERT OR IGNORE INTO item_tags (item_id, tag_id) VALUES (?, ?)",
                         (t.get("id"), tid))
        conn.commit()


def empty_trash() -> list[dict]:
    """Полностью очищает корзину. Возвращает данные удалённых постов (для undo)."""
    out = []
    with get_connection() as conn:
        names = [d[1] for d in conn.execute("PRAGMA table_info(trash)").fetchall()]
        for iid, in conn.execute("SELECT id FROM trash").fetchall():
            row = conn.execute("SELECT * FROM trash WHERE id = ?", (iid,)).fetchone()
            tags = [
                r[0]
                for r in conn.execute("SELECT tag_id FROM item_tags WHERE item_id = ?", (iid,)).fetchall()
            ]
            out.append({"trash": {n: v for n, v in zip(names, row)}, "tags": tags})
        conn.execute("DELETE FROM trash")
        conn.execute("DELETE FROM item_tags WHERE item_id NOT IN (SELECT id FROM saved_items)")
        conn.commit()
    return out


# ---------------------------- Бэкап / экспорт ----------------------------

def backup_db() -> str:
    import shutil
    from datetime import datetime

    path = get_active_db_path()
    if not os.path.exists(path):
        return ""
    bk_dir = os.path.join(_base_dir(), "backups")
    os.makedirs(bk_dir, exist_ok=True)
    uid = _current_user_id.get()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    prefix = f"user_{uid}_" if uid is not None else "saved_items_"
    dst = os.path.join(bk_dir, f"{prefix}{stamp}.db")
    try:
        shutil.copy2(path, dst)
    except Exception:
        return ""
    backups = sorted(f for f in os.listdir(bk_dir) if f.startswith(prefix) and f.endswith(".db"))
    for old in backups[:-10]:
        try:
            os.remove(os.path.join(bk_dir, old))
        except OSError:
            pass
    return dst


def export_json() -> dict:
    with get_connection() as conn:
        items = [
            {
                "id": r[0],
                "category": r[1],
                "content_type": r[2],
                "summary": r[3],
                "original_text": r[4],
                "file_id": r[5],
                # Строка-JSON ровно в один слой (формат бэкапа не меняем —
                # его читает и стриминговый импорт, и старые версии бота).
                "file_ids": _file_ids_db_value(r[6]),
                "media_group_id": r[7],
                "telegram_message_id": r[8],
                "telegram_chat_id": r[9],
                "created_at": r[10],
                "locked": bool(r[11]),
                "source_channel": r[12],
                "file_unique": r[13],
            }
            for r in conn.execute(
                """SELECT id, category, content_type, summary, original_text, file_id, file_ids,
                          media_group_id, telegram_message_id, telegram_chat_id, created_at, locked, source_channel, file_unique
                   FROM saved_items"""
            ).fetchall()
        ]
        cats = [
            {"name": r[0], "group_name": r[1], "locked": bool(r[2])}
            for r in conn.execute("SELECT name, COALESCE(group_name,''), COALESCE(locked,0) FROM categories").fetchall()
        ]
        folders = [
            {"name": r[0], "locked": bool(r[1])}
            for r in conn.execute("SELECT name, COALESCE(locked,0) FROM folders").fetchall()
        ]
        tags = [{"id": r[0], "icon": r[1], "name": r[2]} for r in conn.execute("SELECT id, icon, name FROM tags").fetchall()]
        item_tags = [{"item_id": r[0], "tag_id": r[1]} for r in conn.execute("SELECT item_id, tag_id FROM item_tags").fetchall()]
        folder_tags = [{"path": r[0], "tag_id": r[1]} for r in conn.execute("SELECT path, tag_id FROM folder_tags").fetchall()]
        category_tags = [{"category": r[0], "tag_id": r[1]} for r in conn.execute("SELECT category, tag_id FROM category_tags").fetchall()]
        settings = [{"key": r[0], "value": r[1]} for r in conn.execute("SELECT key, value FROM settings").fetchall()
                    if r[0] != "item_seq"]
        settings.append({"key": "item_seq", "value": str(_item_seq_floor(conn))})
        # иконки, выбранные вручную: раньше не попадали в бэкап и терялись при перезапуске
        icon_overrides = [{"category": r[0], "icon": r[1]}
                          for r in conn.execute("SELECT category, icon FROM icon_overrides").fetchall()]
        # История правок — источник примеров «как пользователь раскладывает посты»;
        # раньше в бэкап не попадала и пропадала при каждом перезапуске на Render.
        history = [
            {"id": r[0], "item_id": r[1], "action": r[2], "old_value": r[3], "new_value": r[4], "created_at": r[5]}
            for r in conn.execute(
                "SELECT id, item_id, action, old_value, new_value, created_at FROM item_history "
                "WHERE action IN ('category', 'summary') ORDER BY id DESC LIMIT 2000"
            ).fetchall()
        ]
        # Корзина: раньше не попадала в бэкап и пропадала при каждом перезапуске Render,
        # хотя кнопка обещает «можно вернуть».
        tcols = [r[1] for r in conn.execute("PRAGMA table_info(trash)").fetchall()]
        trash = [dict(zip(tcols, r)) for r in conn.execute("SELECT * FROM trash").fetchall()]
        # тексты статей по ссылкам (и отметки «пробовали, не вышло», чтобы не качать заново)
        articles = [{"item_id": r[0], "url": r[1], "title": r[2], "text": r[3], "status": r[4],
                     "fetched_at": r[5], "brief": r[6]}
                    for r in conn.execute(
                        "SELECT item_id, url, title, text, status, fetched_at, brief FROM item_articles").fetchall()]
        return {
            "version": 1,
            "items": items,
            "categories": cats,
            "folders": folders,
            "tags": tags,
            "item_tags": item_tags,
            "folder_tags": folder_tags,
            "category_tags": category_tags,
            "settings": settings,
            "item_history": history,
            "trash": trash,
            "icon_overrides": icon_overrides,
            "articles": articles,
        }


def _import_articles(conn, rows: list) -> None:
    _ddl_articles(conn)
    for r in rows or []:
        try:
            if isinstance(r, dict) and r.get("item_id") is not None:
                conn.execute(
                    "INSERT OR IGNORE INTO item_articles (item_id, url, title, text, status, fetched_at, brief) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (int(r["item_id"]), r.get("url"), r.get("title"), (r.get("text") or "")[:ARTICLE_MAX],
                     r.get("status") or "ok", r.get("fetched_at"), r.get("brief")))
        except Exception:
            continue


def _import_icons(conn, rows: list) -> None:
    for r in rows or []:
        if isinstance(r, dict) and r.get("category") and r.get("icon"):
            conn.execute("INSERT OR IGNORE INTO icon_overrides (category, icon) VALUES (?, ?)",
                         (r["category"], r["icon"]))


def _seq_from_backup(conn, rows: list) -> None:
    """item_seq из бэкапа берём максимумом с уже записанным (а не «кто первый»)."""
    for s in rows or []:
        if isinstance(s, dict) and s.get("key") == "item_seq":
            try:
                v = int(s.get("value") or 0)
            except (TypeError, ValueError):
                return
            cur = conn.execute("SELECT value FROM settings WHERE key = 'item_seq'").fetchone()
            if not cur or int(cur[0] or 0) < v:
                conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('item_seq', ?)", (str(v),))
            return


def _import_trash(conn, rows: list) -> None:
    """Возвращает корзину из бэкапа (только то, чего нет среди живых постов)."""
    if not rows:
        return
    cols = {r[1] for r in conn.execute("PRAGMA table_info(trash)").fetchall()}
    live = {r[0] for r in conn.execute("SELECT id FROM saved_items").fetchall()}
    have = {r[0] for r in conn.execute("SELECT id FROM trash").fetchall()}
    for row in rows:
        try:
            if not isinstance(row, dict):
                continue
            row = dict(row)
            if int(row.get("id")) in live:
                # тот же номер у живого поста (старый баг нумерации): раньше такой пост из
                # корзины молча пропадал при восстановлении — кладём под отрицательный номер
                if any(row.get("original_text") == r[0] and row.get("created_at") == r[1]
                       for r in conn.execute("SELECT original_text, created_at FROM saved_items WHERE id = ?",
                                             (int(row["id"]),)).fetchall()):
                    continue   # это и есть живой пост — копия не нужна
                row["id"] = min(min(have, default=0), 0) - 1
            elif int(row["id"]) in have:
                continue
            have.add(int(row["id"]))
            keys = [k for k in row if k in cols]
            vals = [_file_ids_db_value(row[k]) if k == "file_ids" else row[k] for k in keys]
            conn.execute(
                f"INSERT OR IGNORE INTO trash ({', '.join(keys)}) VALUES ({', '.join('?' * len(keys))})", vals
            )
        except Exception:
            continue


def import_json_stream(data: bytes) -> dict:
    """Стриминговый импорт экспорта: байты (gzip или plain JSON).

    Обрабатывает массив «items» по одному посту — в памяти не держится весь
    JSON целиком.  Секции categories/tags/... (маленькие) собираются в
    обычных dict/list.  Возвращает {'items': N, 'categories': N, 'tags': N}."""
    import io
    import gzip as _gz
    try:
        import ijson
        try:
            from ijson.backends import yajl2_c as _ij_yajl  # C-бэкенд ~6x быстрее
            _parse = _ij_yajl.parse
        except Exception:
            _parse = ijson.parse
    except ImportError:
        # Фолбэк на обычный импорт (если ijson не установлен)
        raw = data
        if raw[:2] == b"\x1f\x8b":
            raw = _gz.decompress(raw)
        return import_json(json.loads(raw.decode("utf-8")))

    counts = {"items": 0, "categories": 0, "tags": 0}
    if data[:2] == b"\x1f\x8b":
        f = _gz.GzipFile(fileobj=io.BytesIO(data))
    else:
        f = io.BytesIO(data)

    _IT_FIELDS = (
        "id, category, content_type, summary, original_text, file_id, file_ids, "
        "media_group_id, telegram_message_id, telegram_chat_id, created_at, locked, source_channel, file_unique"
    )

    def _insert(conn, cur_item: dict) -> bool:
        try:
            itid = int(cur_item["id"])
        except Exception:
            return False
        fi_json = _file_ids_db_value(cur_item.get("file_ids"))
        conn.execute(
            f"INSERT OR REPLACE INTO saved_items ({_IT_FIELDS}) "
            f"VALUES ({', '.join('?' * 14)})",
            (
                itid,
                cur_item.get("category", "Заметки"),
                cur_item.get("content_type", "text"),
                cur_item.get("summary"),
                cur_item.get("original_text"),
                cur_item.get("file_id"),
                fi_json,
                cur_item.get("media_group_id"),
                cur_item.get("telegram_message_id"),
                cur_item.get("telegram_chat_id"),
                cur_item.get("created_at"),
                1 if cur_item.get("locked") else 0,
                cur_item.get("source_channel"),
                cur_item.get("file_unique"),
            ),
        )
        return True

    # small array-of-objects keys (all have one flat level of scalars)
    _SMALL_KEYS = (
        "categories", "folders", "tags",
        "item_tags", "folder_tags", "category_tags", "settings", "item_history", "trash",
        "icon_overrides", "articles",
    )
    small: dict[str, list] = {k: [] for k in _SMALL_KEYS}

    conn = get_connection()
    try:
        # импортное соединение: отключаем fsync на каждый commit (диск Render
        # медленный, иначе 133 поста импортируются 5+ минут). Восстановление
        # идемпотентно (INSERT OR REPLACE), переживает крэш частичной записью —
        # следующий запуск просто доимпортирует.
        conn.execute("PRAGMA synchronous = OFF")
    except sqlite3.DatabaseError:
        pass
    _pending = 0
    _logged_total = 0
    logger.info("Импорт стартовал (%s байт, gzip=%s)", len(data), data[:2] == b"\x1f\x8b")
    try:
        parser = _parse(f)
        while True:
            try:
                prefix, event, value = next(parser)
            except StopIteration:
                break
            if prefix != "" or event != "map_key":
                continue
            top_key = str(value)
            # --- items: stream each item to SQLite ---
            if top_key == "items":
                cur_item: dict | None = None
                cur_field: str | None = None
                while True:
                    try:
                        p, e, v = next(parser)
                    except StopIteration:
                        break
                    if p == "items" and e == "end_array":
                        break
                    if p == "items.item" and e == "start_map":
                        cur_item = {}
                    elif p == "items.item" and e == "map_key":
                        cur_field = v
                    elif p.startswith("items.item.") and e in ("string", "number", "boolean", "null"):
                        if cur_item is not None and cur_field:
                            cur_item[cur_field] = v
                    elif p == "items.item" and e == "end_map":
                        if cur_item is not None and _insert(conn, cur_item):
                            counts["items"] += 1
                            _pending += 1
                        cur_item = None
                        # не держим write-lock на весь импорт: коммит порциями,
                        # чтобы параллельные записи (register_user/init_db) не ловили
                        # "database is locked" во время фонового восстановления.
                        if _pending >= 5:
                            conn.commit()
                            _pending = 0
                        if counts["items"] - _logged_total >= 25:
                            _logged_total = counts["items"]
                            logger.info("Импорт: %s постов", counts["items"])
                    elif p == "items.item.file_ids" and e == "start_array":
                        if cur_item is not None:
                            cur_item["file_ids"] = []
                    elif p == "items.item.file_ids.item" and e == "string":
                        if cur_item is not None:
                            cur_item.setdefault("file_ids", []).append(v)
            # --- small sections: build array of flat dicts ---
            elif top_key in _SMALL_KEYS:
                cur_obj = None
                cur_key: str | None = None
                while True:
                    try:
                        p, e, v = next(parser)
                    except StopIteration:
                        break
                    if p == top_key and e == "end_array":
                        break
                    if p == top_key + ".item" and e == "start_map":
                        cur_obj = {}
                    elif p == top_key + ".item" and e == "map_key":
                        cur_key = v
                    elif p.startswith(top_key + ".item.") and e in ("string", "number", "boolean", "null"):
                        if cur_obj is not None:
                            cur_obj[cur_key] = v
                    elif p == top_key + ".item" and e == "end_map":
                        if cur_obj is not None:
                            small[top_key].append(cur_obj)
                        cur_obj = None
            else:
                # unknown top-level key — skip its value
                depth = 0
                while True:
                    try:
                        p, e, v = next(parser)
                    except StopIteration:
                        break
                    if e in ("start_array", "start_map"):
                        depth += 1
                    elif e in ("end_array", "end_map"):
                        if depth == 0:
                            break
                        depth -= 1
                    elif depth == 0:
                        break

        # insert categories/tags/etc (small) — та же транзакция
        for c in small.get("categories", []):
            name = (c.get("name") or "").strip()
            if not name:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO categories(name, group_name, locked) VALUES (?, ?, ?)",
                (name, c.get("group_name") or "", 1 if c.get("locked") else 0),
            )
            counts["categories"] += 1
        for t in small.get("tags", []):
            try:
                tid = int(t["id"])
            except Exception:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO tags (id, icon, name) VALUES (?, ?, ?)",
                (tid, t.get("icon") or "🏷", t.get("name") or ""),
            )
            counts["tags"] += 1
        for f_ in small.get("folders", []):
            name = (f_.get("name") or "").strip()
            if not name:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO folders(name, locked) VALUES (?, ?)",
                (name, 1 if f_.get("locked") else 0),
            )
        for link in small.get("item_tags", []):
            conn.execute(
                "INSERT OR IGNORE INTO item_tags (item_id, tag_id) VALUES (?, ?)",
                (link.get("item_id"), link.get("tag_id")),
            )
        for link in small.get("folder_tags", []):
            conn.execute(
                "INSERT OR IGNORE INTO folder_tags (path, tag_id) VALUES (?, ?)",
                (link.get("path"), link.get("tag_id")),
            )
        for link in small.get("category_tags", []):
            conn.execute(
                "INSERT OR IGNORE INTO category_tags (category, tag_id) VALUES (?, ?)",
                (link.get("category"), link.get("tag_id")),
            )
        for h in small.get("item_history", []):
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO item_history (id, item_id, action, old_value, new_value, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (int(h["id"]), int(h["item_id"]), h.get("action") or "", h.get("old_value"),
                     h.get("new_value"), h.get("created_at")),
                )
            except Exception:
                continue
        _import_trash(conn, small.get("trash", []))
        _import_icons(conn, small.get("icon_overrides", []))
        _import_articles(conn, small.get("articles", []))
        for s in small.get("settings", []):
            conn.execute(
                "INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                (s.get("key"), s.get("value")),
            )
        _seq_from_backup(conn, small.get("settings", []))
        _bump_item_seq(conn)
        conn.commit()
    finally:
        conn.close()

    return counts


def import_json(data: dict) -> dict:
    counts = {"items": 0, "categories": 0, "tags": 0}
    with get_connection() as conn:
        for c in data.get("categories", []):
            name = (c.get("name") or "").strip()
            if not name:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO categories(name, group_name, locked) VALUES (?, ?, ?)",
                (name, c.get("group_name") or "", 1 if c.get("locked") else 0),
            )
            counts["categories"] += 1
        for f in data.get("folders", []):
            name = (f.get("name") or "").strip()
            if not name:
                continue
            conn.execute(
                "INSERT OR IGNORE INTO folders(name, locked) VALUES (?, ?)",
                (name, 1 if f.get("locked") else 0),
            )
        for t in data.get("tags", []):
            try:
                tid = int(t["id"])
            except Exception:
                continue
            conn.execute(
                "INSERT OR REPLACE INTO tags (id, icon, name) VALUES (?, ?, ?)",
                (tid, t.get("icon") or "🏷", t.get("name") or ""),
            )
            counts["tags"] += 1
        it_fields = ("id, category, content_type, summary, original_text, file_id, file_ids, "
                     "media_group_id, telegram_message_id, telegram_chat_id, created_at, locked, source_channel, file_unique")
        for it in data.get("items", []):
            try:
                itid = int(it["id"])
            except Exception:
                continue
            conn.execute(
                f"INSERT OR REPLACE INTO saved_items ({it_fields}) VALUES ({', '.join('?' * 14)})",
                (
                    itid,
                    it.get("category", "Заметки"),
                    it.get("content_type", "text"),
                    it.get("summary"),
                    it.get("original_text"),
                    it.get("file_id"),
                    _file_ids_db_value(it.get("file_ids")),
                    it.get("media_group_id"),
                    it.get("telegram_message_id"),
                    it.get("telegram_chat_id"),
                    it.get("created_at"),
                    1 if it.get("locked") else 0,
                    it.get("source_channel"),
                    it.get("file_unique"),
                ),
            )
            counts["items"] += 1
        for link in data.get("item_tags", []):
            conn.execute("INSERT OR IGNORE INTO item_tags (item_id, tag_id) VALUES (?, ?)",
                         (link.get("item_id"), link.get("tag_id")))
        for link in data.get("folder_tags", []):
            conn.execute("INSERT OR IGNORE INTO folder_tags (path, tag_id) VALUES (?, ?)",
                         (link.get("path"), link.get("tag_id")))
        for link in data.get("category_tags", []):
            conn.execute("INSERT OR IGNORE INTO category_tags (category, tag_id) VALUES (?, ?)",
                         (link.get("category"), link.get("tag_id")))
        for h in data.get("item_history", []):
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO item_history (id, item_id, action, old_value, new_value, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (int(h["id"]), int(h["item_id"]), h.get("action") or "", h.get("old_value"),
                     h.get("new_value"), h.get("created_at")),
                )
            except Exception:
                continue
        _import_trash(conn, data.get("trash", []))
        _import_icons(conn, data.get("icon_overrides", []))
        _import_articles(conn, data.get("articles", []))
        for s in data.get("settings", []):
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                         (s.get("key"), s.get("value")))
        _seq_from_backup(conn, data.get("settings", []))
        _bump_item_seq(conn)
        conn.commit()
    return counts