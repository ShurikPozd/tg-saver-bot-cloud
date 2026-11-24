"""Заметки к постам, уборка архива, календарь, выгрузка папки. Без сети и Telegram.
Запуск: python -m unittest discover tests"""

import asyncio
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "smoke.db"))

import bot  # noqa: E402
import database as db  # noqa: E402
import keyboards  # noqa: E402
import webarchive  # noqa: E402


def _post(cat, text, ctype="text"):
    return db.save_item(category=cat, content_type=ctype, summary=text[:40], original_text=text,
                        file_id=None, message_id=1, chat_id=1)


def _set_created(item_id, when):
    with db.get_connection() as conn:
        conn.execute("UPDATE saved_items SET created_at = ? WHERE id = ?", (when, item_id))
        conn.commit()


class Base(unittest.TestCase):
    def setUp(self):
        self.tok = db.use_db_path(os.path.join(tempfile.mkdtemp(), "a.db"))
        db.init_db()
        bot._clean_cache.clear()

    def tearDown(self):
        db.reset_db_path(self.tok)


class NoteTests(Base):
    def test_note_is_searchable_everywhere(self):
        a = _post("Рецепты", "Паста с грибами")
        _post("Рецепты", "Суп")
        self.assertTrue(bot.web_set_note(a, "попробовать на выходных")["ok"])
        self.assertEqual([r["id"] for r in db.search_items("выходных")], [a])       # поиск в боте
        d = webarchive.archive_data({}, "", None)
        self.assertEqual(next(i for i in d["items"] if i["id"] == a)["cm"], "попробовать на выходных")
        g = webarchive.archive_data({}, "", None, {"kind": "cat", "val": "Рецепты"})
        self.assertEqual(next(i for i in g["items"] if i["id"] == a)["cm"], "")      # гостю заметку не видно
        self.assertEqual(bot.web_set_note(a, "")["note"], "")                       # удалить
        self.assertFalse(bot.web_set_note(99999, "x")["ok"])

    def test_note_allowed_on_locked_post_and_button_label(self):
        a = _post("Рецепты", "Паста")
        db.set_item_locked(a, True)
        self.assertTrue(bot.web_set_note(a, "заметка")["ok"])
        labels = [getattr(b, "text", "") for row in keyboards.view_keyboard(a, "Рецепты") for b in row]
        self.assertIn("📝 Заметка", labels)


class CleanupTests(Base):
    def test_candidates_and_exclusions(self):
        text = "Очень длинный одинаковый текст поста, который переслали дважды из одного канала"
        d1, d2 = _post("Мемы", text), _post("Мемы", text)                 # копия: остаётся первый
        dead = _post("Статьи", "https://example.com/gone")
        old = _post("Разное", "старый пост")
        _set_created(old, "2025-01-01 10:00:00")
        fav = _post("Разное", "избранное старое")
        _set_created(fav, "2025-01-01 10:00:00")
        db.set_favorite(fav, True)
        locked = _post("Разное", "под замком")
        _set_created(locked, "2025-01-01 10:00:00")
        db.set_item_locked(locked, True)
        db.set_setting("link_health", json.dumps({str(dead): {"dead": "01.11.2026"}}))
        got = dict(bot.cleanup_candidates())
        self.assertTrue(got[dead].startswith("🪦"))
        self.assertTrue(got[d2].startswith("🔁") and f"#{d1}" in got[d2])
        self.assertNotIn(d1, got)
        self.assertTrue(got[old].startswith("🕰"))
        self.assertNotIn(fav, got)
        self.assertNotIn(locked, got)
        bot.cleanup_keep_add([old])                                          # «Оставить» — больше не спрашиваем
        self.assertNotIn(old, dict(bot.cleanup_candidates()))
        bot.mark_seen(d2)                                                    # открывал — запоминаем день
        self.assertIn(str(d2), bot._opened_at())

    def test_recently_opened_old_post_is_not_candidate(self):
        old = _post("Разное", "старый, но открывал")
        _set_created(old, "2025-01-01 10:00:00")
        bot.mark_seen(old)
        self.assertNotIn(old, dict(bot.cleanup_candidates()))

    def test_private_never_suggested(self):
        a = _post("Дневник", "старое личное")
        _set_created(a, "2025-01-01 10:00:00")
        db.set_category_path("Дневник", "Личное/Дневник")
        db.create_folder("Личное")
        db.set_private_paths(["Личное"])
        self.assertNotIn(a, dict(bot.cleanup_candidates()))


class CalendarTests(Base):
    def test_days_are_moscow_days(self):
        a = _post("Разное", "поздно вечером по Москве")
        _set_created(a, "2026-10-31 22:30:00")                               # 01:30 1 ноября по Москве
        b = _post("Разное", "днём")
        _set_created(b, "2026-11-15 09:00:00")
        nov = bot.day_ids_msk("2026-11")
        self.assertEqual(nov["2026-11-01"], [a])
        self.assertEqual(nov["2026-11-15"], [b])
        self.assertNotIn(a, sum(bot.day_ids_msk("2026-10").values(), []))
        self.assertIsNone(bot._valid_ym("2026-13"))
        self.assertEqual(bot._valid_ym("2026-02"), "2026-02")


class ExportTests(Base):
    def test_markdown_and_html(self):
        a = _post("Паста", "Паста с [грибами](https://example.com/r) **вкусно**")
        b = _post("Супы", "Борщ <script>alert(1)</script>")
        _post("Мемы", "не из этой папки")
        db.set_category_path("Паста", "Еда/Паста")
        db.set_category_path("Супы", "Еда/Супы")
        db.create_folder("Еда")
        bot.web_set_note(a, "на выходных")
        scope = {"kind": "folder", "val": "Еда"}
        self.assertEqual({r["id"] for r in webarchive.export_rows(scope)}, {a, b})
        raw, name, n, total = asyncio.run(webarchive.build_export(None, 1, scope, "md"))
        md = raw.decode()
        self.assertTrue(name.endswith(".md") and name.startswith("Еда_"))
        self.assertEqual((n, total), (2, 2))
        self.assertIn("## 📁 Еда/Паста", md)
        self.assertIn("[грибами](https://example.com/r)", md)
        self.assertIn("> 📝 на выходных", md)
        self.assertNotIn("не из этой папки", md)
        raw, name, n, _ = asyncio.run(webarchive.build_export(None, 1, scope, "html"))
        page = raw.decode()
        self.assertTrue(name.endswith(".html"))
        self.assertIn('<a href="https://example.com/r"', page)
        self.assertNotIn("<script>alert", page)                              # текст экранирован
        self.assertIn("📝 на выходных", page)
        one = webarchive.export_rows({"kind": "cat", "val": "Супы"})
        self.assertEqual([r["id"] for r in one], [b])

    def test_buttons_fit_telegram_limit(self):
        rows = keyboards.folder_page_keyboard({"path": "Еда", "children": []})
        datas = [getattr(getattr(b, "type", b), "data", b"") for row in rows for b in row]
        self.assertIn("xpf|Еда".encode(), datas)
        self.assertTrue(all(len(d) <= 64 for d in datas))


if __name__ == "__main__":
    unittest.main()
