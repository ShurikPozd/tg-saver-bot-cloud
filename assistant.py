"""ИИ-помощник по архиву: свободная просьба → план изменений → применение → отмена.

ИИ (categorizer.assistant_plan) предлагает действия JSON-ом. Здесь они проверяются
по настоящей структуре архива (с «прогоном» по порядку: папка, созданная пунктом 1,
уже есть для пункта 2), превращаются в понятные строки плана и применяются.
Посты помощник никогда не удаляет: «удалить папку» = распустить её, категорию — влить.
"""

import re

import categorizer
import database as db

MAX_ACTIONS = 15


def _clean(name) -> str:
    name = re.sub(r"\s+", " ", str(name or "")).strip().strip("«»\"'`").strip()
    return name[:60]


def _path(p) -> str:
    p = "/".join(seg.strip() for seg in str(p or "").split("/") if seg.strip())
    return _clean(p) if "/" not in p else "/".join(_clean(x) for x in p.split("/"))


def _key(s: str) -> str:
    return (s or "").casefold().replace("ё", "е").strip()


# ── структура для ИИ ─────────────────────────────────────────────────────────
def structure_text(samples_per_cat: int = 2) -> str:
    tree = db.get_tree()
    samples = db.category_samples(samples_per_cat)
    lines: list[str] = []

    def walk(nodes, depth):
        pad = "  " * depth
        for n in nodes:
            if n["type"] == "folder":
                lock = " [заблокирована]" if n.get("locked") else ""
                lines.append(f"{pad}ПАПКА {n['path']} — {n['count']} постов{lock}")
                walk(n["children"], depth + 1)
            else:
                lock = " [заблокирована]" if n.get("locked") else ""
                ex = "; ".join(f"«{categorizer._clip_summary(x, 70)}»" for x in samples.get(n["category"], []))
                lines.append(f"{pad}• {n['category']} — {n['count']}{lock}" + (f": {ex}" if ex else ""))

    walk(tree, 0)
    return "\n".join(lines) or "(архив пуст)"


def posts_text(ids: list[int]) -> str:
    out = []
    for iid in ids[:20]:
        it = db.get_item(iid)
        if it:
            out.append(f"#{iid} [{it['category']}] {categorizer._clip_summary(it.get('summary') or '', 120)}")
    return "\n".join(out)


# ── прогон плана по копии структуры ──────────────────────────────────────────
class _Sim:
    def __init__(self):
        self.cats: dict[str, dict] = {}
        for c in db.get_categories():
            self.cats[c["category"]] = {"group": (c["group"] or "").strip("/"), "locked": c["locked"],
                                        "count": c["count"]}
        with db.get_connection() as conn:
            for name, grp, lk in conn.execute(
                "SELECT name, COALESCE(group_name,''), COALESCE(locked,0) FROM categories"
            ).fetchall():
                self.cats.setdefault(name, {"group": (grp or "").strip("/"), "locked": bool(lk), "count": 0})
        self.folders: set[str] = set(db.get_folder_paths())
        self.locked_folders: set[str] = set(db.get_locked_folders())

    def _all_folders(self) -> set[str]:
        out = set(self.folders)
        for c in self.cats.values():
            cur = ""
            for seg in (c["group"] or "").split("/"):
                if seg:
                    cur = f"{cur}/{seg}" if cur else seg
                    out.add(cur)
        return out

    def cat(self, name) -> str | None:
        name = _clean(name)
        if name in self.cats:
            return name
        k = _key(name)
        for c in self.cats:
            if _key(c) == k:
                return c
        return None

    def folder(self, path) -> str | None:
        path = _path(path)
        if not path:
            return None
        fl = self._all_folders()
        if path in fl:
            return path
        k = _key(path)
        exact = [f for f in fl if _key(f) == k]
        if exact:
            return exact[0]
        tail = [f for f in fl if _key(f.rpartition("/")[2]) == k]
        return tail[0] if len(tail) == 1 else None

    def folder_or_new(self, path) -> str:
        """Существующая папка (в т.ч. по последнему имени) или новый путь как есть."""
        return self.folder(path) or _path(path)

    def folder_locked(self, path: str) -> bool:
        return any(path == lf or path.startswith(lf + "/") for lf in self.locked_folders)


def _where(folder: str) -> str:
    return f"папку «{folder}»" if folder else "корень"


def validate(actions: list[dict], allowed_ids: list[int] | None = None) -> tuple[list[dict], list[str]]:
    """Проверяет план по порядку. Возвращает (годные действия с полем text, причины отказов)."""
    sim = _Sim()
    allowed = set(allowed_ids or [])
    ok: list[dict] = []
    bad: list[str] = []
    deferred: list[dict] = []
    retry = False

    def reject(why: str):
        # ИИ иногда ставит «влей X в Y» раньше, чем «создай/переименуй в Y» —
        # такие пункты пробуем ещё раз в конце плана
        if not retry and why.startswith(("нет ", "не нашёл")):
            deferred.append(a)
        else:
            bad.append(why)

    def folder_takeover(target) -> tuple[str | None, str | None]:
        """Цель — ПАПКА, а не категория («перенеси в Видео», а «Видео» — папка).
        Возвращает (папка, её единственная категория) — если категория одна и подпапок нет."""
        fold = sim.folder(target)
        if not fold:
            return None, None
        inside = [c for c, v in sim.cats.items() if v["group"] == fold]
        nested = any(v["group"].startswith(fold + "/") for v in sim.cats.values()) or \
            any(f.startswith(fold + "/") for f in sim._all_folders())
        if len(inside) == 1 and not nested and not sim.folder_locked(fold) and not sim.cats[inside[0]]["locked"]:
            return fold, inside[0]
        return fold, None

    def flatten(fold: str, cat: str) -> str:
        """Папка «Видео» с единственной категорией «группа» → папку убрать,
        категорию назвать «Видео». Возвращает итоговое имя категории."""
        parent, _, new = fold.rpartition("/")
        _sim_move_folder(sim, fold, parent, dissolve=True)
        ok.append({"op": "dissolve_folder", "path": fold,
                   "text": f"📤 Убрать папку «{fold}» — в ней одна категория «{cat}»"})
        if cat != new and not sim.cat(new):
            sim.cats[new] = sim.cats.pop(cat)
            ok.append({"op": "rename_category", "name": cat, "to": new,
                       "text": f"✏️ Переименовать «{cat}» → «{new}»"})
            return new
        return cat

    def folder_not_category(target) -> str:
        return (f"«{_clean(target)}» — это папка, а не категория: скажи, в какую категорию внутри неё "
                f"перенести, или «убери папку {_clean(target)}»")

    queue = list(actions[:MAX_ACTIONS])
    while queue or (deferred and not retry):
        if not queue:  # основной проход окончен — повторяем отложенные
            retry = True
            queue, deferred = deferred, []
        a = queue.pop(0)
        op = str(a.get("op") or "").strip()
        if op == "create_category":
            name = _clean(a.get("name"))
            folder = sim.folder_or_new(a.get("folder")) if a.get("folder") else ""
            if not name or "/" in name:
                reject("пустое или странное имя новой категории")
                continue
            have = sim.cat(name)
            if have:
                if folder != sim.cats[have]["group"]:
                    a = {"op": "move_category", "name": have, "folder": folder}
                    op = "move_category"
                else:
                    reject(f"категория «{have}» уже есть")
                    continue
            else:
                if folder and sim.folder_locked(folder):
                    reject(f"папка «{folder}» заблокирована")
                    continue
                sim.cats[name] = {"group": folder, "locked": False, "count": 0}
                ok.append({"op": op, "name": name, "folder": folder,
                           "text": f"🆕 Создать категорию «{name}»" + (f" в папке «{folder}»" if folder else "")})
                continue

        if op == "rename_category":
            name, to = sim.cat(a.get("name")), _clean(a.get("to"))
            if not name:
                reject(f"нет категории «{_clean(a.get('name'))}»")
                continue
            if not to or "/" in to or to == name:
                reject(f"странное новое имя для «{name}»")
                continue
            if sim.cats[name]["locked"]:
                reject(f"«{name}» заблокирована")
                continue
            fold, single = folder_takeover(to)
            if sim.cat(to) and sim.cat(to) != name:
                a = {"op": "merge_categories", "from": name, "to": sim.cat(to)}
                op = "merge_categories"
            elif fold == to and single == name:
                flatten(fold, name)   # имя занято папкой, где только эта категория — папку убираем
                continue
            else:
                sim.cats[to] = sim.cats.pop(name)
                ok.append({"op": op, "name": name, "to": to, "text": f"✏️ Переименовать «{name}» → «{to}»"})
                continue

        if op == "merge_categories":
            src, dst = sim.cat(a.get("from")), sim.cat(a.get("to"))
            if src and not dst:
                fold, single = folder_takeover(a.get("to"))
                if single == src:
                    flatten(fold, src)   # «влей группу в Видео», а Видео — папка с одной «группой»
                    continue
                if single:
                    dst = single
                elif fold:
                    reject(folder_not_category(a.get("to")))
                    continue
            if not src or not dst:
                reject(f"не нашёл категорию «{_clean(a.get('from') if not src else a.get('to'))}»")
                continue
            if src == dst:
                continue
            if sim.cats[src]["locked"] or sim.cats[dst]["locked"]:
                reject(f"«{src}» или «{dst}» заблокирована")
                continue
            n = sim.cats[src]["count"]
            sim.cats[dst]["count"] += n
            sim.cats.pop(src)
            ok.append({"op": op, "from": src, "to": dst, "text": f"🔀 Влить «{src}» ({n} пост.) в «{dst}»"})
            continue

        if op == "move_category":
            name = sim.cat(a.get("name"))
            folder = sim.folder_or_new(a.get("folder")) if a.get("folder") else ""
            if not name:
                reject(f"нет категории «{_clean(a.get('name'))}»")
                continue
            if sim.cats[name]["locked"] or sim.folder_locked(sim.cats[name]["group"]) or (folder and sim.folder_locked(folder)):
                reject(f"«{name}» или папка заблокирована")
                continue
            if sim.cats[name]["group"] == folder:
                continue
            sim.cats[name]["group"] = folder
            ok.append({"op": op, "name": name, "folder": folder, "text": f"📁 «{name}» → {_where(folder)}"})
            continue

        if op == "create_folder":
            path = _path(a.get("path"))
            if not path:
                reject("пустое имя папки")
                continue
            if sim.folder(path) == path:
                continue
            if sim.cat(path):
                reject(f"имя «{path}» уже занято категорией")
                continue
            sim.folders.add(path)
            ok.append({"op": op, "path": path, "text": f"📁 Создать папку «{path}»"})
            continue

        if op == "rename_folder":
            path, to = sim.folder(a.get("path")), _clean(a.get("to"))
            if not path:
                reject(f"нет папки «{_path(a.get('path'))}»")
                continue
            if not to or "/" in to:
                reject(f"странное новое имя для папки «{path}»")
                continue
            if sim.folder_locked(path):
                reject(f"папка «{path}» заблокирована")
                continue
            parent = path.rpartition("/")[0]
            new = f"{parent}/{to}" if parent else to
            _sim_move_folder(sim, path, new)
            ok.append({"op": op, "path": path, "to": to, "text": f"✏️ Папку «{path}» назвать «{to}»"})
            continue

        if op == "move_folder":
            path = sim.folder(a.get("path"))
            parent = sim.folder_or_new(a.get("parent")) if a.get("parent") else ""
            if not path:
                reject(f"нет папки «{_path(a.get('path'))}»")
                continue
            if parent == path or parent.startswith(path + "/"):
                reject(f"папку «{path}» нельзя вложить саму в себя")
                continue
            if sim.folder_locked(path) or (parent and sim.folder_locked(parent)):
                reject(f"папка «{path}» заблокирована")
                continue
            new = f"{parent}/{path.rpartition('/')[2]}" if parent else path.rpartition("/")[2]
            if new == path:
                continue
            _sim_move_folder(sim, path, new)
            ok.append({"op": op, "path": path, "parent": parent, "text": f"📁 Папку «{path}» → {_where(parent)}"})
            continue

        if op == "dissolve_folder":
            path = sim.folder(a.get("path"))
            if not path:
                reject(f"нет папки «{_path(a.get('path'))}»")
                continue
            if sim.folder_locked(path) or any(c["locked"] and (c["group"] == path or c["group"].startswith(path + "/"))
                                              for c in sim.cats.values()):
                reject(f"в папке «{path}» есть заблокированное")
                continue
            parent = path.rpartition("/")[0]
            _sim_move_folder(sim, path, parent, dissolve=True)
            ok.append({"op": op, "path": path,
                       "text": f"📤 Убрать папку «{path}», её содержимое — " + (f"в «{parent}»" if parent else "в корень")})
            continue

        if op == "set_icon":
            icon = str(a.get("icon") or "").strip()
            name = sim.cat(a.get("name"))
            fold = None if name else sim.folder(a.get("name"))
            if not categorizer.is_emoji(icon):
                reject(f"«{icon}» не похоже на эмодзи")
                continue
            if not name and not fold:
                reject(f"нет категории или папки «{_clean(a.get('name'))}»")
                continue
            target = name or fold
            ok.append({"op": op, "name": target, "folder": bool(fold), "icon": icon,
                       "text": f"🎨 Иконка «{target}» → {icon}"})
            continue

        if op == "move_posts":
            dst = sim.cat(a.get("to"))
            ids = []
            for x in a.get("ids") or []:
                try:
                    x = int(x)
                except Exception:
                    continue
                if x in allowed and x not in ids:
                    ids.append(x)
            if not dst:
                fold, single = folder_takeover(a.get("to"))
                if single:
                    cats_of = {(db.get_item(i) or {}).get("category") for i in ids}
                    if ids and cats_of == {single}:
                        # «перенеси содержимое «группа» в Видео», а Видео — папка с одной «группой»
                        flatten(fold, single)
                        continue
                    dst = single
                elif fold:
                    reject(folder_not_category(a.get("to")))
                    continue
            if not dst:
                reject(f"нет категории «{_clean(a.get('to'))}»")
                continue
            if not ids:
                if a.get("_matched") is not None:
                    reject(f"в «{a.get('from')}» не нашёл постов «{a.get('query')}»")
                continue
            what = a.get("_label") or ("посты " + ", ".join(f"#{i}" for i in ids[:6]) + ("…" if len(ids) > 6 else ""))
            ok.append({"op": op, "ids": ids, "to": dst, "text": f"📂 {what} → «{dst}»"})
            continue

        if op == "recat_category":
            name = sim.cat(a.get("name"))
            if not name:
                reject(f"нет категории «{_clean(a.get('name'))}»")
                continue
            ok.append({"op": op, "name": name,
                       "text": f"🔁 Перераспознать «{name}» ({sim.cats[name]['count']} пост.) в фоне"})
            continue

        if op:
            reject(f"не умею «{op}»")
    return ok, bad


def _sim_move_folder(sim: _Sim, path: str, new: str, dissolve: bool = False):
    def lift(p):
        if p == path:
            return new
        if p.startswith(path + "/"):
            rest = p[len(path) + 1:]
            return f"{new}/{rest}" if new else rest
        return p
    for c in sim.cats.values():
        c["group"] = lift(c["group"])
    fl = set()
    for f in sim.folders:
        if dissolve and f == path:
            continue
        nf = lift(f)
        if nf:
            fl.add(nf)
    sim.folders = fl
    sim.locked_folders = {lift(f) for f in sim.locked_folders}


async def resolve_matching(actions: list[dict], allowed_ids: list[int]) -> list[dict]:
    """move_matching → move_posts с конкретными id (второй короткий запрос к ИИ).
    allowed_ids дополняется найденными постами."""
    out = []
    sim = _Sim()
    for a in actions[:MAX_ACTIONS]:
        if str(a.get("op")) != "move_matching":
            out.append(a)
            continue
        src = sim.cat(a.get("from"))
        query = str(a.get("query") or "").strip()
        if not src or not query:
            out.append({"op": "move_posts", "ids": [], "to": a.get("to"), "_matched": 0,
                        "from": _clean(a.get("from")), "query": query})
            continue
        items = db.get_items_by_category(src, limit=150)
        ids = await categorizer.pick_matching(query, items)
        ids = ids or []
        allowed_ids.extend(i for i in ids if i not in allowed_ids)
        out.append({"op": "move_posts", "ids": ids, "to": a.get("to"), "_matched": len(ids),
                    "from": src, "query": query,
                    "_label": f"{len(ids)} пост. из «{src}» ({categorizer._clip_summary(query, 50)})"})
    return out


# ── применение ───────────────────────────────────────────────────────────────
def _drop_if_empty(name: str) -> None:
    with db.get_connection() as conn:
        left = conn.execute("SELECT 1 FROM saved_items WHERE category = ? LIMIT 1", (name,)).fetchone()
        if not left:
            conn.execute("DELETE FROM categories WHERE name = ?", (name,))
            conn.execute("DELETE FROM icon_overrides WHERE category = ?", (name,))
            conn.commit()


def apply_plan(actions: list[dict], cleanup: bool = True) -> tuple[list[str], list[int]]:
    """Выполняет проверенный план. Возвращает (строки отчёта, id постов на перераспознавание)."""
    lines: list[str] = []
    recat: list[int] = []
    keep_folders: list[str] = []
    for a in actions:
        op = a["op"]
        try:
            if op == "create_category":
                good, err = db.create_category(a["name"], a.get("folder") or "")
                if a.get("folder"):
                    keep_folders.append(a["folder"])
                lines.append(("✅ " if good else "⚠️ ") + a["text"] + ("" if good else f" — {err}"))
            elif op == "rename_category":
                good, err = db.rename_category(a["name"], a["to"])
                lines.append(("✅ " if good else "⚠️ ") + a["text"] + ("" if good else f" — {err}"))
            elif op == "merge_categories":
                moved = db.merge_categories(a["from"], a["to"], lock_items=False)
                _drop_if_empty(a["from"])
                lines.append(f"✅ Влил «{a['from']}» в «{a['to']}» ({moved} пост.)")
            elif op == "move_category":
                db.set_category_path(a["name"], a.get("folder") or "")
                if a.get("folder"):
                    keep_folders.append(a["folder"])
                lines.append("✅ " + a["text"])
            elif op == "create_folder":
                db.create_folder(a["path"])
                keep_folders.append(a["path"])
                lines.append("✅ " + a["text"])
            elif op == "rename_folder":
                good, err = db.rename_folder(a["path"], a["to"])
                lines.append(("✅ " if good else "⚠️ ") + a["text"] + ("" if good else f" — {err}"))
            elif op == "move_folder":
                last = a["path"].rpartition("/")[2]
                new = f"{a['parent']}/{last}" if a.get("parent") else last
                db.move_folder_path(a["path"], new)
                keep_folders.append(new)
                lines.append("✅ " + a["text"])
            elif op == "dissolve_folder":
                good, msg = db.dissolve_folder(a["path"])
                lines.append(("✅ " if good else "⚠️ ") + a["text"] + ("" if good else f" — {msg}"))
            elif op == "set_icon":
                db.set_category_icon(a["name"], a["icon"])
                if a.get("folder") and "/" in a["name"]:
                    db.set_category_icon(a["name"].rpartition("/")[2], a["icon"])
                lines.append("✅ " + a["text"])
            elif op == "move_posts":
                db.ensure_category(a["to"])
                moved = 0
                for iid in a["ids"]:
                    it = db.get_item(iid)
                    if it and not it.get("locked") and it["category"] != a["to"]:
                        db.update_item_category(iid, a["to"])
                        moved += 1
                lines.append(f"✅ {a['text']} (перенесено {moved})")
            elif op == "recat_category":
                recat.extend(db.get_item_ids(a["name"]))
                lines.append("✅ " + a["text"])
        except Exception as e:  # один неудачный пункт не должен ронять остальные
            lines.append(f"⚠️ {a.get('text', op)} — ошибка: {e}")
    # убираем то, что опустело (влитые категории, пустые папки), но не папки, о которых просили
    if cleanup:
        db.cleanup_empty(include_manual=False)
    for f in keep_folders:
        if f and not db.get_folder(f) and f not in db.get_folder_paths():
            db.create_folder(f)
    return lines, recat
