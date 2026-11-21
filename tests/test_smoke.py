"""Дымовые тесты: бот импортируется, меню строятся, сверка бэкапа ловит расхождения.
Без Telegram, Groq и GitHub. Запуск: python -m unittest discover tests"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "smoke.db"))

import bot  # noqa: E402
import database as db  # noqa: E402
import keyboards as kb  # noqa: E402


class SmokeTests(unittest.TestCase):
    def test_menus_build(self):
        self.assertTrue(kb.root_categories_keyboard([]))
        self.assertTrue(kb.service_keyboard())
        st = {k: v for k, v in bot.SETTINGS_DEFAULTS.items()}
        self.assertTrue(kb.settings_keyboard(st))
        self.assertIn("Настройки", bot._settings_text(st))

    def test_callback_data_fits_telegram_limit(self):
        rows = kb.root_categories_keyboard([]) + kb.service_keyboard() + kb.settings_keyboard(dict(bot.SETTINGS_DEFAULTS))
        for row in rows:
            for b in row:
                data = getattr(getattr(b, "type", None), "data", None) or getattr(b, "data", b"")
                self.assertLessEqual(len(data), 64, b.text)


class BackupCompareTests(unittest.TestCase):
    def _snap(self, items, trash=0):
        return {"counts": {"saved_items": len(items), "categories": 2, "folders": 0, "tags": 0,
                           "item_tags": 0, "trash": trash}, "items": dict(items)}

    def test_same_is_ok(self):
        s = self._snap({1: ("a",), 2: ("b",)})
        self.assertEqual(bot.compare_snapshots(s, s), [])

    def test_missing_and_changed_are_reported(self):
        live = self._snap({1: ("a",), 2: ("b",), 3: ("c",)})
        rest = self._snap({1: ("a",), 3: ("X",)})
        problems = " ".join(bot.compare_snapshots(live, rest))
        self.assertIn("#2", problems)
        self.assertIn("#3", problems)

    def test_post_saved_after_backup_is_fine(self):
        live = self._snap({1: ("a",), 2: ("b",), 9: ("new",)})
        rest = self._snap({1: ("a",), 2: ("b",)})
        self.assertEqual(bot.compare_snapshots(live, rest), [])

    def test_lost_trash_is_reported(self):
        live = self._snap({1: ("a",)}, trash=3)
        rest = self._snap({1: ("a",)}, trash=0)
        self.assertTrue(bot.compare_snapshots(live, rest))


class TrashRoundtripTests(unittest.TestCase):
    def test_trash_survives_export_import(self):
        import gzip
        import json

        d = tempfile.mkdtemp()
        src, dst = os.path.join(d, "a.db"), os.path.join(d, "b.db")
        tok = db.use_db_path(src)
        try:
            db.init_db()
            keep = db.save_item(category="Игры", content_type="text", summary="s1", original_text="t1",
                                file_id=None, message_id=1, chat_id=1)
            gone = db.save_item(category="Игры", content_type="text", summary="s2", original_text="t2",
                                file_id=None, message_id=2, chat_id=1)
            db.trash_item(gone)
            data = gzip.compress(json.dumps(db.export_json(), ensure_ascii=False).encode("utf-8"))
            live = db.archive_snapshot()
        finally:
            db.reset_db_path(tok)
        tok = db.use_db_path(dst)
        try:
            db.init_db()
            db.import_json_stream(data)
            restored = db.archive_snapshot()
        finally:
            db.reset_db_path(tok)
        self.assertEqual(restored["counts"]["trash"], 1)
        self.assertIn(keep, restored["items"])
        self.assertEqual(bot.compare_snapshots(live, restored), [])


if __name__ == "__main__":
    unittest.main()
