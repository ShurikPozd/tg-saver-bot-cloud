"""ИИ-помощник по архиву: проверка плана, применение, отмена; роспуск папок; иконки в бэкапе;
новая категория — предложением. Без сети: ИИ подменяется заглушками.
Запуск: python -m unittest discover tests"""

import asyncio
import gzip
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "smoke.db"))

import assistant  # noqa: E402
import bot  # noqa: E402
import categorizer  # noqa: E402
import database as db  # noqa: E402


def _post(cat, summary="s"):
    return db.save_item(category=cat, content_type="text", summary=summary, original_text=summary,
                        file_id=None, message_id=1, chat_id=1)


class ArchiveCase(unittest.TestCase):
    def setUp(self):
        self.tok = db.use_db_path(os.path.join(tempfile.mkdtemp(), "a.db"))
        db.init_db()
        # как у пользователя: папка «Кино и сериалы» есть, а «Кино» и «Сериалы» лежат рядом;
        # «Реклама» — папка с одной категорией; «Shikimori» — категория по названию сайта
        for cat, grp, n in [("Новости кино", "Развлечения/Кино и сериалы", 3), ("Кино", "Развлечения", 2),
                            ("Сериалы", "Развлечения", 2), ("Новости аниме", "Развлечения", 4),
                            ("Shikimori", "", 1), ("Продажи", "Реклама", 2), ("Игры", "Развлечения", 2)]:
            for i in range(n):
                _post(cat, f"{cat} пост {i}")
            db.set_category_path(cat, grp)
        db.create_folder("Реклама")
        self.locked_id = _post("Игры", "заблокированный")
        db.set_item_locked(self.locked_id, True)

    def tearDown(self):
        db.reset_db_path(self.tok)

    def groups(self):
        return {c["category"]: c["group"] for c in db.get_categories()}


class AssistantPlanTests(ArchiveCase):
    def test_structure_lists_folders_and_examples(self):
        text = assistant.structure_text()
        self.assertIn("ПАПКА Развлечения/Кино и сериалы", text)
        self.assertIn("• Shikimori — 1", text)
        self.assertIn("«Сериалы пост", text)

    def test_plan_is_checked_in_order_and_applied(self):
        ok, bad = assistant.validate([
            {"op": "create_folder", "path": "Хобби"},
            {"op": "move_category", "name": "кино", "folder": "Кино и сериалы"},  # регистр и короткий путь
            {"op": "move_category", "name": "Сериалы", "folder": "Развлечения/Кино и сериалы"},
            {"op": "merge_categories", "from": "Shikimori", "to": "Новости аниме"},
            {"op": "dissolve_folder", "path": "Реклама"},
            {"op": "set_icon", "name": "Игры", "icon": "🎮"},
            {"op": "set_icon", "name": "Игры", "icon": "abc"},
            {"op": "merge_categories", "from": "Нет такой", "to": "Игры"},
            {"op": "drop_database"},
        ])
        self.assertEqual(len(ok), 6)
        self.assertEqual(len(bad), 3)
        self.assertIn("Развлечения/Кино и сериалы", ok[1]["text"])

        before = db.archive_snapshot()
        snap = db.structure_snapshot()
        lines, recat = assistant.apply_plan(ok)
        self.assertTrue(all(line.startswith("✅") for line in lines), lines)
        g = self.groups()
        self.assertEqual(g["Кино"], "Развлечения/Кино и сериалы")
        self.assertEqual(g["Сериалы"], "Развлечения/Кино и сериалы")
        self.assertNotIn("Shikimori", g)
        self.assertEqual(g["Продажи"], "")
        self.assertNotIn("Реклама", db.get_folder_paths())
        self.assertIn("Хобби", db.get_folder_paths())          # пустую папку, о которой просили, не убирает
        self.assertEqual(db.get_icon_overrides().get("Игры"), "🎮")
        self.assertEqual(len(db.get_item_ids()), len(before["items"]))  # ни один пост не пропал

        n = db.restore_structure(snap)
        self.assertEqual(n, 1)                                  # пост из Shikimori вернулся
        self.assertEqual(db.structure_snapshot(), snap)

    def test_out_of_order_steps_are_retried(self):
        ok, bad = assistant.validate([
            {"op": "merge_categories", "from": "Кино", "to": "Фильмы"},
            {"op": "rename_category", "name": "Новости кино", "to": "Фильмы"},
        ])
        self.assertEqual([a["op"] for a in ok], ["rename_category", "merge_categories"])
        self.assertEqual(bad, [])

    def test_rename_onto_existing_becomes_merge(self):
        ok, _ = assistant.validate([{"op": "rename_category", "name": "Кино", "to": "Новости кино"}])
        self.assertEqual(ok[0]["op"], "merge_categories")

    def test_locked_and_foreign_posts_are_safe(self):
        ok, bad = assistant.validate([{"op": "move_posts", "ids": [self.locked_id, 99999], "to": "Кино"}],
                                     allowed_ids=[self.locked_id])
        lines, _ = assistant.apply_plan(ok)
        self.assertEqual(db.get_item(self.locked_id)["category"], "Игры")  # замок соблюдён
        self.assertIn("перенесено 0", lines[0])

    def test_move_matching_uses_ai_pick(self):
        async def fake_pick(query, items):
            return [it["id"] for it in items if it["summary"].endswith("0")]
        orig = categorizer.pick_matching
        categorizer.pick_matching = fake_pick
        try:
            allowed: list[int] = []
            acts = asyncio.run(assistant.resolve_matching(
                [{"op": "move_matching", "from": "Кино", "query": "трейлеры", "to": "Сериалы"}], allowed))
        finally:
            categorizer.pick_matching = orig
        ok, bad = assistant.validate(acts, allowed)
        self.assertEqual(len(ok[0]["ids"]), 1)
        assistant.apply_plan(ok)
        self.assertEqual(db.get_item(ok[0]["ids"][0])["category"], "Сериалы")

    def test_dissolve_nested_folder_lifts_children(self):
        db.set_category_path("Новости кино", "Развлечения/Кино и сериалы/Новое")
        ok, msg = db.dissolve_folder("Развлечения/Кино и сериалы")
        self.assertTrue(ok, msg)
        self.assertEqual(self.groups()["Новости кино"], "Развлечения/Новое")


class IconsAndOffersTests(ArchiveCase):
    def test_icons_survive_backup(self):
        db.set_category_icon("Игры", "🕹️")
        db.rename_category("Игры", "Видеоигры")
        data = gzip.compress(json.dumps(db.export_json(), ensure_ascii=False).encode())
        tok = db.use_db_path(os.path.join(tempfile.mkdtemp(), "b.db"))
        try:
            db.init_db()
            db.import_json_stream(data)
            self.assertEqual(db.get_icon_overrides(), {"Видеоигры": "🕹️"})
        finally:
            db.reset_db_path(tok)

    def test_lonely_folder_found_by_auto_order(self):
        cats = db.get_categories()
        alone = {d["path"] for d in bot._lonely_folders(cats, set(), {})}
        self.assertEqual(alone, {"Реклама", "Развлечения/Кино и сериалы"})
        # если авто-порядок досыпает в папку категории — она уже не одинокая
        fill = {"Развлечения/Кино и сериалы": ["Кино", "Сериалы"]}
        self.assertEqual([d["path"] for d in bot._lonely_folders(cats, set(), fill)], ["Реклама"])

    def test_new_category_is_offered_not_created(self):
        async def fake_categorize(text, **kw):
            return {"category": "Shikimori 2", "summary": "Описание аниме", "confidence": 0.9,
                    "alternatives": ["Новости аниме"], "llm_ok": True}
        orig = bot.categorize
        bot.categorize = fake_categorize
        try:
            item = _post("Новости аниме", "Описание аниме")
            res = asyncio.run(bot._ai_categorize("Описание аниме", "text", None, exclude_id=item))
        finally:
            bot.categorize = orig
        self.assertEqual(res["category"], "Новости аниме")
        self.assertEqual(res["proposed_new"], "Shikimori 2")
        self.assertEqual(bot._newcat_for_item(item), "Shikimori 2")
        self.assertIsNone(db.get_category_row("Shikimori 2"))

    def test_icon_targets_cover_categories_and_folders(self):
        db.set_setting("ai_icons", json.dumps({"Игры": "🎮"}))
        names = {t["name"] for t in bot._icon_targets()}
        self.assertNotIn("Игры", names)
        self.assertIn("Shikimori", names)
        self.assertIn("Развлечения/Кино и сериалы", names)


if __name__ == "__main__":
    unittest.main()
