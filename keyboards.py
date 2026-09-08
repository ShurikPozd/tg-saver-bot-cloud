from telethon import Button

from categorizer import strip_markdown
import pending

DEFAULT_TAG_CIRCLES = ["🔴", "🟠", "🟡", "🟢", "🔵", "🟣", "⚪", "⚫"]

REPLY_BUTTONS = {
    "categories": "📂 Категории",
    "stats": "📊 Статистика",
    "search": "🔍 Поиск",
    "help": "❓ Помощь",
    "settings": "⚙️ Настройки",
    "export": "💾 Экспорт",
    "recent": "🕘 Последние",
}


def maybe_pending_label() -> str:
    n = pending.total()
    return f"📬 Висящих: {n}" if n else "📭 Вопросов нет"


def maybe_dups_label() -> str:
    try:
        import database as db
        n = len(db.find_archive_dups(limit=100))
    except Exception:
        n = 0
    return f"🔁 Дубли: {n}"


def main_keyboard():
    rows = [
        [Button.text(REPLY_BUTTONS["categories"]), Button.text(REPLY_BUTTONS["stats"])],
        [Button.text(REPLY_BUTTONS["search"]), Button.text(REPLY_BUTTONS["recent"])],
        [Button.text(REPLY_BUTTONS["settings"]), Button.text(REPLY_BUTTONS["export"])],
        [Button.text(maybe_pending_label())],
    ]
    try:
        import database as db
        if db.find_archive_dups(limit=1):
            rows.append([Button.text(maybe_dups_label())])
    except Exception:
        pass
    return rows


def settings_keyboard(state: dict):
    def on(key: str) -> str:
        return "Вкл" if state.get(key) == "1" else "Выкл"

    eff_auto = state.get("auto_lock") == "1" and any(
        state.get(k) == "1" for k in ("lock_move", "lock_rename", "lock_create")
    )

    rows = [
        [
            Button.inline(
                f"🔒 Автозамок: {'Вкл' if eff_auto else 'Выкл'}",
                data="settoggle|auto_lock",
            )
        ],
        [
            Button.inline(
                f"🖼️ Распознавать фото: {on('photo_vision')}",
                data="settoggle|photo_vision",
            )
        ],
        [
            Button.inline(
                f"⚡️ Быстрые действия: {on('quick_actions')}",
                data="settoggle|quick_actions",
            )
        ],
        [
            Button.inline(
                f"🧹 Чистить пустые категории: {on('cleanup_empty')}",
                data="settoggle|cleanup_empty",
            )
        ],
    ]
    if state.get("auto_lock") == "1":
        rows.append(
            [
                Button.inline(
                    f"🔒 Замок при перемещении: {on('lock_move')}",
                    data="settoggle|lock_move",
                )
            ]
        )
        rows.append(
            [
                Button.inline(
                    f"🔒 Замок при переименовании: {on('lock_rename')}",
                    data="settoggle|lock_rename",
                )
            ]
        )
        rows.append(
            [
                Button.inline(
                    f"🔒 Замок при создании: {on('lock_create')}",
                    data="settoggle|lock_create",
                )
            ]
        )

    ao = state.get("auto_order_posts", "10")
    ao_label = "Выкл" if ao in ("", "0") else f"каждые {ao} постов"
    rows.append(
        [
            Button.inline("◀️", data="setcycle|auto_order_posts|down"),
            Button.inline(f"⚡️ Авто-порядок: {ao_label}", data="noop"),
            Button.inline("▶️", data="setcycle|auto_order_posts|up"),
        ]
    )
    rows.append(
        [
            Button.inline(
                f"🧩 Подпапки: {on('subfolders_enabled')}",
                data="settoggle|subfolders_enabled",
            )
        ]
    )
    sm = state.get("subfolders_min", "5")
    rows.append(
        [
            Button.inline("◀️", data="setcycle|subfolders_min|down"),
            Button.inline(f"🧩 Мин. постов: {sm}", data="noop"),
            Button.inline("▶️", data="setcycle|subfolders_min|up"),
        ]
    )
    rows.append(
        [
            Button.inline(
                f"🧩 В авто-порядке: {on('subfolders_in_auto')}",
                data="settoggle|subfolders_in_auto",
            )
        ]
    )
    rows.append(
        [
            Button.inline(
                f"🔒 Гостевой режим: {on('guest_readonly')}",
                data="settoggle|guest_readonly",
            )
        ]
    )
    rows.append(
        [
            Button.inline(
                f"🎙️ Распознавать голосовые: {on('audio_vision')}",
                data="settoggle|audio_vision",
            )
        ]
    )
    rows.append(
        [
            Button.inline(
                f"🔁 Проверка дублей: {on('dedup_check')}",
                data="settoggle|dedup_check",
            )
        ]
    )
    rows.append(
        [
            Button.inline(
                f"📬 Дайджест за неделю: {on('weekly_digest')}",
                data="settoggle|weekly_digest",
            )
        ]
    )
    rows.append(
        [
            Button.inline(
                f"🗄 Копия экспорта в TG: {on('backup_tg_daily')}",
                data="settoggle|backup_tg_daily",
            )
        ]
    )
    return rows


def root_categories_keyboard(tree: list[dict]):
    rows = []
    for node in tree:
        tags = _tags_prefix(node.get("tags"))
        if node["type"] == "folder":
            lock = "🔒 " if node.get("locked") else ""
            thematic = theme_icon(node["path"], "")
            prefix = f"📁 {thematic}" if thematic else "📁"
            rows.append(
                [Button.inline(f"{tags}{prefix} {lock}{node['path']} ({node['count']})", data=f"fold|{node['path']}")]
            )
        else:
            emoji = theme_icon(node["category"], "📦")
            lock = "🔒 " if node["locked"] else ""
            rows.append(
                [
                    Button.inline(
                        f"{tags}📁 {emoji} {lock}{node['category']} ({node['count']})", data=f"cat:{node['category']}"
                    )
                ]
            )
    rows.append([Button.inline("➕ Папка", data="addfolder|"), Button.inline("➕ Категория", data="addcat|")])
    rows.append([Button.inline("⚡ Авто-порядок", data="auto_order"), Button.inline("🔀 Объединить", data="merge")])
    rows.append([Button.inline("🔁 Перераспознать всё", data="recatall"), Button.inline("🔒 Заблокированные", data="locked_list")])
    rows.append([Button.inline("🏷 Теги", data="tags_section"), Button.inline("🧩 Подпапки", data="subfolders_preview")])
    rows.append([Button.inline("🧹 Завершить обрезанные", data="fix_truncated")])
    rows.append([Button.inline("☑️ Выбрать", data="cf_selmode|"), Button.inline("🗑 Корзина", data="trash_list")])
    rows.append([Button.inline("ℹ️ Про кнопки", data="btn_help")])
    return rows


def folder_page_keyboard(node: dict, parent_path: str = ""):
    rows = []
    for child in node["children"]:
        tags = _tags_prefix(child.get("tags"))
        if child["type"] == "folder":
            lock = "🔒 " if child.get("locked") else ""
            thematic = theme_icon(child["name"], "")
            prefix = f"📁 {thematic}" if thematic else "📁"
            rows.append(
                [Button.inline(f"{tags}{prefix} {lock}{child['name']} ({child['count']})", data=f"fold|{child['path']}")]
            )
        else:
            emoji = theme_icon(child["category"], "📦")
            lock = "🔒 " if child["locked"] else ""
            rows.append(
                [Button.inline(f"{tags}📁 {emoji} {lock}{child['category']} ({child['count']})", data=f"cat:{child['category']}")]
            )
    rows.append([Button.inline("✏️ Переименовать", data=f"renfolder|{node['path']}")])
    rows.append(
        [
            Button.inline("📂 Переместить папку", data=f"mvfolder|{node['path']}"),
            Button.inline("➕ Папка", data=f"addfolder|{node['path']}"),
        ]
    )
    rows.append([Button.inline("➕ Категория", data=f"addcat|{node['path']}")])
    rows.append(
        [
            Button.inline("☑️ Выбрать", data=f"cf_selmode|{node['path']}"),
            Button.inline("🗑 Удалить папку", data=f"folddel|{node['path']}"),
        ]
    )
    rows.append([Button.inline("🏷 Теги папки", data=f"ftag|{node['path']}")])
    if parent_path:
        rows.append([Button.inline("◀️ Назад", data=f"fold|{parent_path}")])
        if "/" in parent_path:
            rows.append([Button.inline("🏠 В начало", data="fold|")])
    else:
        rows.append([Button.inline("◀️ Назад", data="fold|")])
    return rows


def merge_source_keyboard(categories: list[dict]):
    rows = []
    for cat in categories:
        rows.append(
            [Button.inline(f"{theme_icon(cat['category'], '📦')} {cat['category']} ({cat['count']})", data=f"merge_src|{cat['category']}")]
        )
    rows.append([Button.inline("◀️ Назад", data="to_folders")])
    return rows


def merge_target_keyboard(categories: list[dict]):
    rows = []
    for cat in categories:
        rows.append(
            [Button.inline(f"{theme_icon(cat['category'], '📦')} {cat['category']} ({cat['count']})", data=f"merge_dst|{cat['category']}")]
        )
    rows.append([Button.inline("◀️ Назад", data="to_folders")])
    return rows


def category_keyboard_rows(categories: list[dict]):
    rows = []
    for cat in categories:
        rows.append(
            [Button.inline(f"{theme_icon(cat['category'], '📦')} {cat['category']} ({cat['count']})", data=f"merge_dst|{cat['category']}")]
        )
    rows.append([Button.inline("◀️ Назад", data="to_folders")])
    return rows


def _items_rows(items: list[dict], category: str, page: int, total_pages: int, selection=None):
    ids_sel = selection.get("ids", set()) if selection is not None else set()
    cat_emoji = theme_icon(category, "")
    rows = []
    for item in items:
        tags = _tags_prefix(item.get("tags"))
        emoji = cat_emoji or content_emoji(item["content_type"])
        lock = " 🔒" if item.get("locked") else ""
        text = strip_markdown(item["summary"])
        text = text[:50] + ("..." if len(text) > 50 else "")
        if selection is not None:
            mark = "☑️" if str(item["id"]) in ids_sel else "⬜️"
            rows.append([Button.inline(f"{mark} {tags}{text}", data=f"sel|{category}|{item['id']}")])
        else:
            rows.append([Button.inline(f"{tags}{emoji} {lock} {text}", data=f"view:{item['id']}")])
    return rows


def items_keyboard(items: list[dict], category: str, page: int, total_pages: int, selection=None, category_locked: bool = False, folder_path: str = ""):
    rows = _items_rows(items, category, page, total_pages, selection)

    nav = []
    if page > 0:
        nav.append(Button.inline("⬅️", data=f"page:{category}|{page - 1}"))
    if page < total_pages - 1:
        nav.append(Button.inline("➡️", data=f"page:{category}|{page + 1}"))
    if nav:
        rows.append(nav)

    if selection is not None:
        rows.append(
            [
                Button.inline("🔁 Перераспознать", data=f"selrecat|{category}"),
                Button.inline("📂 Переместить", data=f"selmv|{category}"),
                Button.inline("✍️ Уточнить", data=f"selclarify|{category}"),
            ]
        )
        rows.append(
            [
                Button.inline("🗑 Удалить", data=f"seldel|{category}"),
                Button.inline("🏷 Теги", data=f"seltags|{category}"),
                Button.inline("👌 Готово", data=f"selclear|{category}"),
            ]
        )
    else:
        rows.append(
            [
                Button.inline("🔁 Перераспознать все", data=f"recatcat|{category}"),
                Button.inline("☑️ Выбрать", data=f"selmode|{category}"),
            ]
        )
        rows.append(
            [
                Button.inline("✏️ Переименовать", data=f"rencat|{category}"),
                Button.inline("📁 В папку", data=f"mvcat|{category}"),
            ]
        )
        rows.append([Button.inline("🏷 Теги", data=f"ctag|{category}")])
        rows.append(
            [
                (
                    Button.inline("🔒 Разблокировать", data=f"catlock|{category}")
                    if category_locked
                    else Button.inline("🔓 Заблокировать", data=f"catlock|{category}")
                ),
            ]
        )
        rows.append([Button.inline("🎨 Сменить иконку", data=f"caticon:{category}")])
        rows.append([Button.inline("🗑 Удалить категорию", data=f"catdel:{category}")])

    if folder_path:
        rows.append([Button.inline("◀️ Назад", data=f"fold|{folder_path}")])
        rows.append([Button.inline("🏠 В начало", data="back_to_cats")])
    else:
        rows.append([Button.inline("◀️ Назад", data="back_to_cats")])
    return rows


def sel_move_keyboard(src_category: str, categories: list[dict]):
    rows = []
    for cat in categories:
        if cat["category"] == src_category:
            continue
        rows.append(
            [Button.inline(f"{theme_icon(cat['category'], '📦')} {cat['category']} ({cat['count']})", data=f"selmvgo|{src_category}|{cat['category']}")]
        )
    rows.append([Button.inline("◀️ Назад", data=f"selclear|{src_category}")])
    return rows


def cf_choice_keyboard(nodes: list[dict], selected: set, root: str):
    rows = []
    for node in nodes:
        key = f"cat|{node['category']}" if node["type"] == "cat" else f"fold|{node['path']}"
        mark = "☑️ " if key in selected else "⬜️ "
        if node["type"] == "folder":
            rows.append([Button.inline(f"{mark}📁 {node['path']} ({node['count']})", data=f"cf_tog|{root}|{key}")])
        else:
            emoji = theme_icon(node["category"], "📦")
            lock = " 🔒" if node.get("locked") else ""
            rows.append([Button.inline(f"{mark}{emoji} {node['category']}{lock} ({node['count']})", data=f"cf_tog|{root}|{key}")])
    rows.append(
        [
            Button.inline("☑️ Выбрать всё", data=f"cf_selall|{root}"),
            Button.inline("⬜️ Снять всё", data=f"cf_selnone|{root}"),
        ]
    )
    rows.append(
        [
            Button.inline("🗑 Удалить выбранные", data=f"cf_del|{root}"),
            Button.inline("🏷 Теги выбранных", data=f"cf_tags|{root}"),
        ]
    )
    rows.append([Button.inline("👌 Готово", data=f"cf_back|{root}")])
    return rows


def view_keyboard(item_id: int, category: str, locked: bool = False):
    lock_row = (
        [Button.inline("🔒 Разблокировать", data=f"itemlock:{item_id}")]
        if locked
        else [Button.inline("🔓 Заблокировать", data=f"itemlock:{item_id}")]
    )
    return [
        lock_row,
        [Button.inline("📨 Переслать себе", data=f"forward:{item_id}")],
        [
            Button.inline("✏️ Переименовать", data=f"renitem:{item_id}"),
            Button.inline("🔁 Перераспознать", data=f"recat:{item_id}"),
        ],
        [
            Button.inline("✍️ Уточнить", data=f"clarifyv:{item_id}"),
            Button.inline("📂 Переместить", data=f"move:{item_id}"),
        ],
        [Button.inline("🏷 Теги", data=f"itag:{item_id}")],
        [
            Button.inline("💬 Комментарий", data=f"cmt:{item_id}"),
            Button.inline("📜 История", data=f"hist:{item_id}"),
        ],
        [Button.inline("🗑 Удалить", data=f"del:{item_id}")],
        [
            Button.inline("◀️ Назад", data=f"back_to_cat:{category}"),
            Button.inline("🏠 В начало", data="back_to_cats"),
        ],
    ]


def move_keyboard(item_id: int, categories: list[dict]):
    rows = []
    for cat in categories:
        emoji = theme_icon(cat["category"], "📦")
        rows.append(
            [Button.inline(f"{emoji} {cat['category']} ({cat['count']})", data=f"mvset:{item_id}:{cat['category']}")]
        )
    rows.append([Button.inline("◀️ Назад", data=f"view:{item_id}")])
    return rows


def mvcat_pick_keyboard(paths: list[str], category: str):
    rows = [[Button.inline("📂 В корень", data=f"mvcat_dest|{category}|")]]
    for p in paths:
        indent = "   " * (p.count("/") + 1)
        rows.append([Button.inline(f"{indent}📁 {p}", data=f"mvcat_dest|{category}|{p}")])
    rows.append([Button.inline("◀️ Назад", data=f"back_to_cat:{category}")])
    return rows


def mvfolder_pick_keyboard(paths: list[str], src: str):
    rows = [[Button.inline("📂 В корень", data=f"mvfolder_dest|{src}|")]]
    for p in paths:
        if p == src or p.startswith(src + "/"):
            continue
        indent = "   " * (p.count("/") + 1)
        rows.append([Button.inline(f"{indent}📁 {p}", data=f"mvfolder_dest|{src}|{p}")])
    rows.append([Button.inline("◀️ Назад", data=f"fold|{src}")])
    return rows


def confirm_delete_keyboard(item_id: int):
    return [
        [Button.inline("🗑 В корзину", data=f"delgo:{item_id}")],
        [Button.inline("◀️ Назад", data=f"view:{item_id}")],
    ]


def trash_keyboard(items: list[dict], selected=None):
    selected = selected or set()
    rows = []
    for it in items:
        emoji = theme_icon(it["category"], "📦")
        mark = "☑️" if it["id"] in selected else "⬜️"
        label = f"{mark} {emoji} №{it['id']} {it['summary'][:30]}"
        rows.append([Button.inline(label, data=f"trash_tog:{it['id']}")])
    rows.append(
        [
            Button.inline(f"♻️ Вернуть выбранные ({len(selected)})", data="trash_selrestore"),
            Button.inline("🗑 Удалить выбранные", data="trash_selpurge"),
        ]
    )
    rows.append(
        [
            Button.inline("☑️ Выбрать всё", data="trash_selall"),
            Button.inline("⬜️ Снять всё", data="trash_selnone"),
        ]
    )
    rows.append(
        [
            Button.inline("♻️ Вернуть всё", data="trash_allrestore"),
            Button.inline("🧹 Удалить всё навсегда", data="trash_empty"),
        ]
    )
    rows.append([Button.inline("◀️ Назад", data="back_to_cats")])
    return rows


def confirm_keyboard():
    return [[Button.inline("✅ Сохранено!", data="noop")]]


def quick_actions_keyboard(item_id: int, category: str):
    return [
        [Button.inline("📂 Переместить", data=f"move:{item_id}"), Button.inline("✏️ Название", data=f"renitem:{item_id}")],
        [Button.inline("🏷 Теги", data=f"itag:{item_id}"), Button.inline("🔁 Переанализ", data=f"recat:{item_id}")],
        [Button.inline("✍️ Уточнить", data=f"clarify:{item_id}"), Button.inline("✅ Сохранено!", data=f"view:{item_id}")],
    ]


def category_emoji(cat: str) -> str:
    return {"Видео": "📹", "Фото": "📸", "Заметки": "📝", "Ссылки": "🔗", "Аудио": "🎵", "Идеи": "💡", "Другое": "📦"}.get(cat, "📦")


def content_emoji(ct: str) -> str:
    return {"video": "📹", "photo": "📸", "text": "📝", "audio": "🎵", "document": "📄", "voice": "🎤", "animation": "🎞️"}.get(ct, "📦")


THEME_EMOJI = [
    ("арт", "🎨"), ("рисун", "🎨"), ("скетч", "🎨"), ("художн", "🎨"), ("иллюстрац", "🎨"),
    ("фото", "📸"), ("фотограф", "📸"),
    ("видео", "🎬"), ("фильм", "🎬"), ("кино", "🎬"), ("мульт", "🎬"),
    ("сериал", "📺"),
    ("аниме", "🎌"), ("манга", "📕"), ("манхва", "📕"), ("манхв", "📕"),
    ("игр", "🎮"), ("гейм", "🎮"), ("steam", "🕹️"),
    ("музык", "🎵"), ("аудио", "🎵"), ("песн", "🎵"), ("трек", "🎵"), ("плейлист", "🎵"),
    ("книг", "📚"), ("чтени", "📖"), ("комик", "💬"),
    ("кулинар", "🍳"), ("рецепт", "🍳"), ("еда", "🍕"), ("выпечк", "🥐"), ("кондитер", "🧁"),
    ("путешеств", "✈️"), ("туризм", "✈️"), ("поездк", "🌍"), ("отпуск", "🌴"), ("города", "🏙️"),
    ("спорт", "🏆"), ("футбол", "⚽"), ("фитнес", "💪"), ("тренировк", "💪"), ("бег", "🏃"),
    ("кроссовк", "👟"), ("обувь", "👞"), ("nike", "👟"),
    ("мод", "👗"), ("одежд", "👔"), ("стиль", "👕"),
    ("технолог", "💻"), ("программ", "💻"), ("софт", "💻"), ("разработк", "👨‍💻"), ("код", "👨‍💻"),
    ("мышь", "🖱️"), ("мышки", "🖱️"), ("клавиатур", "⌨️"), ("компьютер", "🖥️"),
    ("гаджет", "📱"), ("телефон", "📱"), ("айфон", "📱"), ("apple", "🍎"),
    ("наука", "🔬"), ("астро", "🔭"), ("космос", "🚀"),
    ("авто", "🚗"), ("машин", "🚗"), ("мото", "🏍️"),
    ("дом", "🏠"), ("интерьер", "🛋️"), ("ремонт", "🔧"),
    ("природа", "🌿"), ("живот", "🐾"), ("кот", "🐱"), ("котик", "🐱"), ("котят", "🐱"), ("собак", "🐶"), ("питом", "🐾"),
    ("смеш", "😂"), ("мем", "😆"), ("юмор", "😄"), ("прикол", "😆"), ("шутк", "😜"),
    ("ваканс", "💼"), ("работа", "💼"), ("резюме", "📄"), ("карьер", "💼"), ("hh", "💼"),
    ("ссылки", "🔗"), ("ссыль", "🔗"), ("заметк", "📝"), ("идеи", "💡"), ("идея", "💡"),
    ("истори", "📜"), ("новост", "📰"), ("аналит", "📊"), ("бизнес", "💼"),
]


_icon_overrides: dict = {}


def set_icon_overrides(overrides: dict) -> None:
    global _icon_overrides
    _icon_overrides = overrides or {}


def theme_icon(name: str, fallback: str = "📂") -> str:
    icon = _icon_overrides.get(name)
    if icon:
        return icon
    lowered = (name or "").lower()
    for kw, emoji in THEME_EMOJI:
        if kw in lowered:
            return emoji
    return fallback


def _tags_prefix(tags) -> str:
    return "".join(t.get("icon", "") for t in (tags or []))


def tag_editor_keyboard(tags: list[dict], selected, tag_new_data: str, tag_ctrl: str, back_data: str):
    """Редактор тегов для элемента (checkboxes)."""
    rows = []
    for t in tags:
        mark = "☑️ " if t["id"] in selected else "⬜️ "
        name = f" {t['name']}" if t["name"] else ""
        rows.append([Button.inline(f"{mark}{t['icon']}{name}", data=f"{tag_ctrl}{t['id']}")])
    rows.append([Button.inline("➕ Новый тег", data=tag_new_data)])
    rows.append([Button.inline("⚙️ Управление тегами", data="tags_manage")])
    rows.append([Button.inline("◀️ Назад", data=back_data)])
    return rows


def tag_palette_keyboard(back_data: str):
    rows = []
    for i, c in enumerate(DEFAULT_TAG_CIRCLES):
        rows.append([Button.inline(c, data=f"tagpal:{i}")])
    rows.append([Button.inline("✏️ Свой символ (эмодзи)", data="tagpal:custom")])
    rows.append([Button.inline("◀️ Назад", data=back_data)])
    return rows


def category_icon_keyboard(category: str):
    rows = []
    for i, c in enumerate(DEFAULT_TAG_CIRCLES):
        rows.append([Button.inline(c, data=f"caticon_pick:{i}|{category}")])
    rows.append([Button.inline("✏️ Свой символ (эмодзи)", data=f"caticon_pick:custom|{category}")])
    rows.append([Button.inline("◀️ Назад", data=f"cat:{category}")])
    return rows


def tags_section_keyboard(tags: list[dict], selected: set, counts: dict):
    rows = []
    for t in tags:
        mark = "☑️ " if t["id"] in selected else ""
        name = f" {t['name']}" if t["name"] else ""
        rows.append([Button.inline(f"{mark}{t['icon']}{name}  ({counts.get(t['id'], 0)})", data=f"tagpick:{t['id']}")])
    rows.append(
        [
            Button.inline(f"🔍 Показать ({len(selected)})", data="tagshow"),
            Button.inline("✨ Сбросить", data="tagclear"),
        ]
    )
    rows.append([Button.inline("➕ Новый тег", data="tagnew")])
    rows.append([Button.inline("⚙️ Управление тегами", data="tags_manage")])
    rows.append([Button.inline("◀️ Назад", data="back_to_cats")])
    return rows


def manage_tags_keyboard(tags: list[dict], counts: dict):
    rows = []
    for t in tags:
        name = f" {t['name']}" if t["name"] else ""
        rows.append([Button.inline(f"⚙️ {t['icon']}{name}  ({counts.get(t['id'], 0)})", data=f"tagact:{t['id']}")])
    rows.append([Button.inline("➕ Новый тег", data="tagnew")])
    rows.append([Button.inline("◀️ Назад", data="tags_section")])
    return rows


def manage_tag_actions_keyboard(tag_id: int):
    return [
        [Button.inline("✏️ Переименовать", data=f"tagren:{tag_id}")],
        [Button.inline("🎨 Сменить иконку", data=f"tagicon:{tag_id}")],
        [Button.inline("🔁 Перераспознать посты с тегом", data=f"tagrecat:{tag_id}")],
        [Button.inline("🗑 Удалить тег", data=f"tagdel:{tag_id}")],
        [Button.inline("◀️ Назад", data="tags_manage")],
    ]