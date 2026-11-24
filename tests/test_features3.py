"""Статьи по ссылкам, дубли по одному, итоги месяца, файл архива. Без сети и без Telegram.
Запуск: python -m unittest discover tests"""

import asyncio
import gzip
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "smoke.db"))

import bot  # noqa: E402
import database as db  # noqa: E402
import linkmeta  # noqa: E402


def _post(cat, text, ctype="text", file_id=None):
    return db.save_item(category=cat, content_type=ctype, summary=text[:40], original_text=text,
                        file_id=file_id, message_id=1, chat_id=1)


def _fresh():
    tok = db.use_db_path(os.path.join(tempfile.mkdtemp(), "a.db"))
    db.init_db()
    return tok


class FakeEvent:
    chat_id = 1

    def __init__(self):
        self.out = []

    async def edit(self, text, **kw):
        self.out.append((text, kw.get("buttons")))

    respond = edit

    async def answer(self, *a, **kw):
        pass


PAGE = """<html><head><title>T</title><meta property="og:title" content="Как собрать Gunpla"></head><body>
<nav><p>Меню сайта, ссылки навигации и прочее лишнее</p></nav><main><p>Похожие статьи: десять лучших наборов</p>
<article><h1>Как собрать Gunpla</h1><p>{body}</p><p>Второй абзац: краски, клей и терпение — всё, что нужно новичку.</p>
<script>var tracking = 1</script></article></main>
<footer><p>© Все права защищены, подписывайтесь на рассылку</p></footer></body></html>""".replace(
    "{body}", "Первый абзац о моделях и о том, как их собирать без клея. " * 6)


class ArticleTests(unittest.TestCase):
    def setUp(self):
        self.tok = _fresh()

    def tearDown(self):
        db.reset_db_path(self.tok)

    def test_extract_prefers_article_and_drops_menus(self):
        title, text = linkmeta.extract_article(PAGE)
        self.assertEqual(title, "Как собрать Gunpla")
        self.assertIn("Первый абзац", text)
        self.assertIn("Второй абзац", text)
        for junk in ("Меню сайта", "Похожие статьи", "Все права", "tracking"):
            self.assertNotIn(junk, text)

    def test_article_url_skips_media_and_shops(self):
        self.assertEqual(linkmeta.article_url("[читать](https://habr.com/ru/articles/1/). и https://t.me/x"),
                         "https://habr.com/ru/articles/1/")
        self.assertIsNone(linkmeta.article_url("https://youtu.be/abcdefghijk https://www.avito.ru/item"))
        self.assertIsNone(linkmeta.article_url("https://site.com/pic.jpg"))
        # ссылки из «распознанного» не берём — только из самого поста
        self.assertIsNone(linkmeta.article_url("текст\n\n〔распознано〕\nhttps://habr.com/x"))

    def test_private_addresses_are_never_fetched(self):
        for url in ("http://127.0.0.1:8080/", "http://localhost/", "http://10.0.0.5/admin"):
            self.assertEqual(asyncio.run(linkmeta.fetch_article(url))["status"], "fail")

    def test_article_text_is_searchable_and_in_backup(self):
        a = _post("Хобби", "Смотри https://example.com/gunpla")
        db.article_set(a, "https://example.com/gunpla", "Как собрать Gunpla", "кусачки и наждачка", "ok")
        self.assertEqual([r["id"] for r in db.search_items("наждачка")], [a])
        self.assertEqual(db.items_needing_article(), [])
        raw = gzip.compress(json.dumps(db.export_json(), ensure_ascii=False, default=str).encode())
        _fresh()
        db.import_json_stream(raw)
        self.assertEqual(db.article_get(a)["text"], "кусачки и наждачка")
        self.assertTrue(bot._has_article(a))


class DupTests(unittest.TestCase):
    def setUp(self):
        self.tok = _fresh()

    def tearDown(self):
        db.reset_db_path(self.tok)
        bot._dup_skip.clear()

    def test_text_dups_ignore_media_posts(self):
        cap = "Подписывайся на канал, тут лучшие мемы каждый день без остановки"
        _post("Мемы", cap, "photo", file_id="f1"), _post("Мемы", cap, "photo", file_id="f2")
        a = _post("Новости", "Вышла новая часть игры, разработчики показали геймплей и дату релиза")
        b = _post("Игры", "вышла новая часть игры — разработчики показали геймплей и дату релиза!")
        self.assertEqual(db.find_text_dups(), [[a, b]])

    def test_review_keeps_oldest_and_remembers_not_dups(self):
        text = "Один и тот же длинный пост, который переслали в бота два раза подряд"
        a, b = _post("A", text), _post("B", text)
        ev = FakeEvent()
        asyncio.run(bot.handle_dups(ev, "dupv"))
        self.assertIn("лишний", ev.out[-1][0])
        asyncio.run(bot.handle_dups(ev, f"dupx:{a}-{b}"))
        self.assertIsNotNone(db.get_item(a))
        self.assertIsNone(db.get_item(b))                       # лишний — в корзине, не удалён
        self.assertEqual(db.count_trashed(), 1)
        c, d = _post("C", text + " снова"), _post("D", text + " снова")
        asyncio.run(bot.handle_dups(ev, f"dupk:{c}-{d}"))
        self.assertEqual(bot._dup_groups(), [])                  # «это не дубли» больше не показываем

    def test_locked_post_is_kept(self):
        text = "Пост, который защищён замком, а копия у него обычная, без замка совсем"
        a, b = _post("A", text), _post("B", text)
        db.set_item_locked(b, True)
        asyncio.run(bot.handle_dups(FakeEvent(), f"dupx:{a}-{b}"))
        self.assertIsNone(db.get_item(a))
        self.assertIsNotNone(db.get_item(b))


class MonthTests(unittest.TestCase):
    def setUp(self):
        self.tok = _fresh()

    def tearDown(self):
        db.reset_db_path(self.tok)

    def _at(self, iid, when):
        with db.get_connection() as conn:
            conn.execute("UPDATE saved_items SET created_at = ? WHERE id = ?", (when, iid))
            conn.commit()

    def test_month_digest(self):
        for i in range(3):
            self._at(_post("Игры", f"игра {i}"), "2026-11-1%d 10:00:00" % i)
        self._at(_post("Кино", "фильм"), "2026-11-20 10:00:00")
        self._at(_post("Кино", "старое"), "2026-10-05 10:00:00")
        self._at(_post("Кино", "декабрь"), "2026-12-01 01:00:00")
        text, _ = bot.month_digest_text(datetime(2026, 12, 1, 8))
        self.assertIn("ноябрь 2026", text)
        self.assertIn("Сохранено: 4 поста ↑ (месяцем раньше: 1)", text)
        self.assertIn("Игры 3", text)
        self.assertIn("Всего в архиве: 6 постов", text)

    def test_empty_month(self):
        text, unseen = bot.month_digest_text(datetime(2026, 12, 1, 8))
        self.assertIn("новых постов не было", text)
        self.assertEqual(unseen, 0)


if __name__ == "__main__":
    unittest.main()
