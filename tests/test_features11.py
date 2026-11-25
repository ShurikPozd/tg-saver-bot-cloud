"""Напоминание своими словами, чек-лист из поста, импорт закладок. Без сети и Telegram.
Запуск: python -m unittest discover tests"""

import asyncio
import gzip
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "smoke.db"))

import bot  # noqa: E402
import bookmarks  # noqa: E402
import database as db  # noqa: E402
import keyboards  # noqa: E402
import webarchive  # noqa: E402
import whenparse  # noqa: E402


def _post(cat, text, ctype="text"):
    return db.save_item(category=cat, content_type=ctype, summary=text[:40], original_text=text,
                        file_id=None, message_id=1, chat_id=1)


class Base(unittest.TestCase):
    def setUp(self):
        self.tok = db.use_db_path(os.path.join(tempfile.mkdtemp(), "a.db"))
        db.init_db()

    def tearDown(self):
        db.reset_db_path(self.tok)


class WhenTests(unittest.TestCase):
    NOW = datetime(2026, 11, 25, 14, 30)   # среда, Москва

    def check(self, text, expect):
        got = whenparse.parse_when(text, self.NOW)
        self.assertEqual(got.strftime("%d.%m.%Y %H:%M") if got else None, expect, text)

    def test_phrases(self):
        self.check("завтра в 18:00", "26.11.2026 18:00")
        self.check("в пятницу вечером", "27.11.2026 19:00")
        self.check("3 декабря", "03.12.2026 09:00")
        self.check("3 декабря в 7 вечера", "03.12.2026 19:00")
        self.check("через 2 недели", "09.12.2026 09:00")
        self.check("через полчаса", "25.11.2026 15:00")
        self.check("на выходных", "28.11.2026 11:00")
        self.check("в 18:30", "25.11.2026 18:30")
        self.check("в 10", "26.11.2026 10:00")               # сегодня 10 уже прошло — завтра
        self.check("в среду", "02.12.2026 09:00")           # сегодня среда — следующая
        self.check("в среду в 20:00", "25.11.2026 20:00")   # …но если время сегодня ещё впереди — сегодня
        self.check("1 января", "01.01.2027 09:00")
        self.check("03.12.2027 10:15", "03.12.2027 10:15")
        self.check("вчера", None)
        self.check("абракадабра", None)


class RemindTests(Base):
    def test_text_reminder_and_ai_fallback(self):
        a = _post("Разное", "пост")
        with mock.patch.object(bot.whenparse if hasattr(bot, "whenparse") else whenparse, "parse_when",
                               wraps=whenparse.parse_when):
            res = asyncio.run(bot.web_remind_text(7, a, "через 2 недели"))
        self.assertIn("в 9:00", res["label"])
        self.assertEqual(bot._reminder_for(a)["chat"], 7)

        async def ai(text, now, wd):
            return "2027-05-01 12:30"
        with mock.patch.object(bot._cat, "parse_when_ai", ai):
            res = asyncio.run(bot.web_remind_text(7, a, "когда-нибудь на майские в обед"))
        self.assertIn("01.05 в 12:30", res["label"])
        self.assertTrue(bot._remind_label(bot._reminder_for(a)["due"]).endswith("12:30"))

        async def none(text, now, wd):
            return None
        with mock.patch.object(bot._cat, "parse_when_ai", none):
            self.assertIn("error", asyncio.run(bot.web_remind_text(7, a, "вчера")))


class ChecklistTests(Base):
    def test_make_toggle_cache(self):
        a = _post("Рецепты", "Паста: 200 г спагетти, 2 зубчика чеснока. Отварить, обжарить, смешать.")
        calls = []

        async def fake(title, text):
            calls.append(text)
            return {"title": "Паста", "groups": [{"name": "Продукты", "items": ["Спагетти 200 г", "Чеснок 2 зубчика"]},
                                                 {"name": "Шаги", "items": ["Отварить", "Смешать"]}]}
        with mock.patch.object(bot._cat, "make_checklist", fake):
            cl = asyncio.run(bot.checklist_for(a))
            asyncio.run(bot.checklist_for(a))
        self.assertEqual(len(calls), 1)                                   # второй раз — из памяти
        self.assertEqual([x[0] for x in cl["items"]][:2], ["Спагетти 200 г", "Чеснок 2 зубчика"])
        bot.checklist_toggle(a, 1, True)
        self.assertEqual(bot.checklist_get(a)["items"][1][1], True)
        self.assertIn("✅ Чеснок 2 зубчика", bot.checklist_text(bot.checklist_get(a)))
        webarchive._EDIT["checklists"] = bot._checklists
        try:
            d = webarchive.archive_data({}, "", None)
            self.assertEqual(next(i for i in d["items"] if i["id"] == a)["ck"], [1, 4])
        finally:
            webarchive._EDIT.pop("checklists", None)
        text, rows = bot._checklist_view(a, bot.checklist_get(a))
        self.assertIn("Отмечено 1 из 4", text)

    def test_keyboard_has_list_button(self):
        rows = keyboards.view_keyboard(5, "Рецепты", foreign=True)
        labels = [getattr(b, "text", "") for r in rows for b in r]
        self.assertIn("☑️ Список", labels)
        self.assertIn("📜 История", labels)
        self.assertIn("🌐 Перевести", labels)


BM_FILE = """<!DOCTYPE NETSCAPE-Bookmark-file-1>
<META HTTP-EQUIV="Content-Type" CONTENT="text/html; charset=UTF-8">
<TITLE>Bookmarks</TITLE>
<H1>Bookmarks</H1>
<DL><p>
    <DT><H3 ADD_DATE="1700000000" PERSONAL_TOOLBAR_FOLDER="true">Панель закладок</H3>
    <DL><p>
        <DT><A HREF="https://habr.com/ru/articles/1/" ADD_DATE="1700000100">Статья про Python &amp; тесты</A>
        <DT><H3>Еда</H3>
        <DL><p>
            <DT><A HREF="https://eda.ru/recepty/pasta" ADD_DATE="1600000000">Паста</A>
            <DT><A HREF="https://eda.ru/recepty/pasta#comments">Паста ещё раз</A>
            <DT><A HREF="javascript:alert(1)">Букмарклет</A>
        </DL><p>
        <DT><H3>Работа</H3>
        <DL><p>
            <DT><H3>Python</H3>
            <DL><p>
                <DT><A HREF="https://docs.python.org/3/">Docs</A>
            </DL><p>
        </DL><p>
    </DL><p>
    <DT><A HREF="https://example.com/old">Уже в архиве</A>
</DL><p>
""".encode("utf-8")


class BookmarksTests(Base):
    def test_parse(self):
        self.assertTrue(bookmarks.is_bookmarks_file(BM_FILE))
        self.assertFalse(bookmarks.is_bookmarks_file(b"<html><body>hi</body></html>"))
        bms = bookmarks.parse(BM_FILE)
        self.assertEqual([b["url"] for b in bms], ["https://habr.com/ru/articles/1/", "https://eda.ru/recepty/pasta",
                                                    "https://docs.python.org/3/", "https://example.com/old"])
        self.assertEqual(bms[0]["title"], "Статья про Python & тесты")
        self.assertEqual(bms[0]["folders"], [])                      # «Панель закладок» — корень браузера
        self.assertEqual(bms[1]["folders"], ["Еда"])
        self.assertEqual(bms[2]["folders"], ["Работа", "Python"])

    def test_import(self):
        _post("Разное", "Старое https://example.com/old")
        _post("Еда", "своя папка «Еда» уже есть")
        r = bot.bookmarks_prepare(BM_FILE)
        self.assertEqual((r["total"], r["new"], r["dups"]), (4, 3, 1))
        res = bot.bookmarks_import(r["tok"])
        self.assertEqual(res["saved"], 3)
        cats = {c["category"]: c["group"] for c in db.get_categories()}
        self.assertEqual(cats[bot.BM_ROOT], bot.BM_ROOT)                # без папки — в корень «Закладки браузера»
        self.assertIn("Еда", cats)                                       # совпала с папкой архива — дополнили её
        self.assertEqual(cats["Python"], f"{bot.BM_ROOT}/Работа")
        items = {it["summary"]: it for it in (db.get_item(i) for i in db.get_item_ids())}
        pasta = items["Паста"]
        self.assertIsNone(pasta["message_id"])
        self.assertEqual(pasta["category"], "Еда")
        self.assertTrue(pasta["created_at"].startswith("2020-09-13"))   # дата закладки
        self.assertNotIn(pasta["id"], bot.unseen_ids())                  # не «🆕 новые»
        self.assertNotIn(pasta["id"], dict(bot.cleanup_candidates()))
        self.assertIn("error", bot.bookmarks_import(r["tok"]))           # второй раз той же кнопкой — нет
        r2 = bot.bookmarks_prepare(BM_FILE)
        self.assertEqual(r2["new"], 0)                                   # повторный файл — всё уже есть
        # пост без сообщения в чате переживает бэкап
        raw = gzip.compress(json.dumps(db.export_json(), ensure_ascii=False, default=str).encode())
        tok = db.use_db_path(os.path.join(tempfile.mkdtemp(), "b.db"))
        db.init_db()
        db.import_json_stream(raw)
        self.assertIn("Паста", {db.get_item(i)["summary"] for i in db.get_item_ids()})
        db.reset_db_path(tok)


if __name__ == "__main__":
    unittest.main()
