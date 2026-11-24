"""Находки ревью 24.11: номера постов после восстановления, точная отмена помощника,
роспуск папки без задевания чужих, ссылки для друзей. Без сети.
Запуск: python -m unittest discover tests"""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "smoke.db"))

import database as db  # noqa: E402


def _post(cat, summary="s"):
    return db.save_item(category=cat, content_type="text", summary=summary, original_text=summary,
                        file_id=None, message_id=1, chat_id=1)


def _fresh():
    db.use_db_path(os.path.join(tempfile.mkdtemp(), "a.db"))
    db.init_db()


class ItemNumbersTests(unittest.TestCase):
    def test_new_post_never_reuses_trashed_number(self):
        _fresh()
        _post("A"), _post("A")
        c = _post("A", "старый")
        db.set_item_topics({c: ["GTA 6"]})
        db.trash_item(c)
        raw = json.dumps(db.export_json(), ensure_ascii=False, default=str).encode()
        _fresh()
        db.import_json_stream(raw)
        n = _post("B", "новый")
        self.assertGreater(n, c)
        self.assertIsNone(db.item_topics().get(n))
        self.assertTrue(db.restore_item(c))
        self.assertEqual(db.get_item(n)["summary"], "новый")          # новый пост не затёрт
        self.assertEqual(len(db.get_item_ids()), 4)

    def test_restore_into_taken_number_gets_new_number(self):
        _fresh()
        x = _post("A", "в корзине")
        db.trash_item(x)
        # как после старого бага: живой пост с тем же номером
        with db.get_connection() as conn:
            conn.execute("INSERT INTO saved_items (id, category, content_type, summary, original_text) "
                         "VALUES (?, 'A', 'text', 'живой', 'живой')", (x,))
            conn.commit()
        self.assertTrue(db.restore_item(x))
        self.assertEqual(sorted(db.get_item(i)["summary"] for i in db.get_item_ids()), ["в корзине", "живой"])

    def test_trash_does_not_overwrite_older_trashed_copy(self):
        _fresh()
        x = _post("A", "первый")
        db.trash_item(x)
        with db.get_connection() as conn:
            conn.execute("INSERT INTO saved_items (id, category, content_type, summary, original_text) "
                         "VALUES (?, 'A', 'text', 'второй', 'второй')", (x,))
            conn.commit()
        db.trash_item(x)
        self.assertEqual(db.count_trashed(), 2)


class UndoTests(unittest.TestCase):
    def test_undo_keeps_changes_made_after_the_plan(self):
        _fresh()
        x, y = _post("Old"), _post("Old")
        before = db.structure_snapshot()
        db.rename_category("Old", "New")
        after = db.structure_snapshot()
        db.set_item_locked(x, True)                                   # потом пост заблокировали
        db.set_category_path("Fresh", "F") if db.get_category_row("Fresh") else None
        _post("Fresh")
        db.set_category_path("Fresh", "F")
        db.set_category_locked("Fresh", True)                         # потом создали папку
        n = db.restore_structure(before, after)
        self.assertEqual(n, 1)
        self.assertEqual(db.get_item(y)["category"], "Old")
        self.assertEqual(db.get_item(x)["category"], "New")           # заблокированный не тронут
        self.assertEqual(db.get_category_row("Fresh")["group"], "F")
        self.assertTrue(db.get_category_row("Fresh")["locked"])


class DissolveTests(unittest.TestCase):
    def test_underscore_is_not_a_wildcard(self):
        _fresh()
        _post("k1"), _post("k2")
        db.set_category_path("k1", "X_Y/sub")
        db.set_category_path("k2", "XzY/sub")
        ok, _ = db.dissolve_folder("X_Y")
        self.assertTrue(ok)
        self.assertEqual(db.get_category_row("k2")["group"], "XzY/sub")
        self.assertEqual(db.get_category_row("k1")["group"], "sub")

    def test_locked_subfolder_blocks_dissolve(self):
        _fresh()
        _post("c")
        db.set_category_path("c", "A/B/L")
        db.create_folder("A/B/L")
        db.set_folder_locked("A/B/L", True)
        ok, _ = db.dissolve_folder("A/B")
        self.assertFalse(ok)
        self.assertEqual(db.get_category_row("c")["group"], "A/B/L")

    def test_name_clash_merges_instead_of_ghost(self):
        _fresh()
        _post("c1"), _post("c2")
        db.set_category_path("c1", "A/B/C")
        db.set_category_path("c2", "A/C")
        db.create_folder("A/B/C")
        db.create_folder("A/C")
        ok, _ = db.dissolve_folder("A/B")
        self.assertTrue(ok)
        paths = db.get_folder_paths()
        self.assertNotIn("A/B/C", paths)
        self.assertNotIn("A/B", paths)


if __name__ == "__main__":
    unittest.main()
