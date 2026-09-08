import sqlite3
import json
import os
import contextvars
from config import DB_PATH

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


def get_active_db_path() -> str:
    uid = _current_user_id.get()
    return user_db_path(uid) if uid is not None else DB_PATH


def get_connection():
    path = get_active_db_path()
    parent = os.path.dirname(path)
    if parent and not os.path.exists(parent):
        os.makedirs(parent, exist_ok=True)
    return sqlite3.connect(path)


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
        conn.commit()
    return True, new


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


def _parse_file_ids(raw) -> list:
    if not raw:
        return []
    try:
        data = json.loads(raw)
        if isinstance(data, list):
            return data
    except Exception:
        pass
    return []


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
                      telegram_message_id, telegram_chat_id, locked, source_channel, comment
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
        }


def get_total_items(category: str) -> int:
    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM saved_items WHERE category = ?", (category,)
        ).fetchone()
        return row[0]


def search_items(query: str) -> list[dict]:
    with get_connection() as conn:
        q = f"%{query}%"
        rows = conn.execute(
            """SELECT i.id, i.category, i.content_type, i.summary, i.telegram_message_id, i.telegram_chat_id
               FROM saved_items i
               WHERE i.summary LIKE ? OR i.original_text LIKE ?
                  OR EXISTS (SELECT 1 FROM item_tags it JOIN tags t ON t.id = it.tag_id
                             WHERE it.item_id = i.id AND (t.name LIKE ? OR t.icon LIKE ?))
               ORDER BY i.created_at DESC LIMIT 30""",
            (q, q, q, q),
        ).fetchall()
        return [
            {
                "id": r[0],
                "category": r[1],
                "content_type": r[2],
                "summary": r[3],
                "message_id": r[4],
                "chat_id": r[5],
            }
            for r in rows
        ]


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


def find_dup_text(original_text: str) -> dict | None:
    if not (original_text or "").strip():
        return None
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id, category, summary, locked FROM saved_items WHERE original_text = ? LIMIT 1",
            (original_text.strip(),),
        ).fetchone()
    if not row:
        return None
    return {"id": row[0], "category": row[1], "summary": row[2] or "", "locked": bool(row[3])}


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
    out = [(t, lst) for t, lst in groups.items() if len(lst) >= 2]
    out.sort(key=lambda x: -len(x[1]))
    return out[:limit]


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


# ---------------------------- Корзина ----------------------------

def trash_item(item_id: int) -> bool:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM saved_items WHERE id = ?", (item_id,)).fetchone()
        if not row:
            return False
        names = [d[1] for d in conn.execute("PRAGMA table_info(saved_items)").fetchall()]
        conn.execute(
            f"INSERT OR REPLACE INTO trash ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
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
        conn.execute(
            f"INSERT OR REPLACE INTO saved_items ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
            row[: len(names)],
        )
        conn.execute("INSERT OR IGNORE INTO categories(name) VALUES (?)", (row[1],))
        conn.execute("DELETE FROM trash WHERE id = ?", (item_id,))
        add_history(item_id, "restore", None, row[1], conn=conn)
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
                "file_ids": r[6],
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
        settings = [{"key": r[0], "value": r[1]} for r in conn.execute("SELECT key, value FROM settings").fetchall()]
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
        }


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
                    json.dumps(it["file_ids"], ensure_ascii=False) if it.get("file_ids") else None,
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
        for s in data.get("settings", []):
            conn.execute("INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)",
                         (s.get("key"), s.get("value")))
        conn.commit()
    return counts