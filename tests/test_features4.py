"""«🧠 Кратко», ⭐ Избранное, скидки Steam. Без сети: ИИ и Steam подменяются.
Запуск: python -m unittest discover tests"""

import asyncio
import gzip
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "smoke.db"))

import bot  # noqa: E402
import database as db  # noqa: E402
import gamedeals  # noqa: E402
import webarchive  # noqa: E402


def _post(cat, text):
    return db.save_item(category=cat, content_type="text", summary=text[:40], original_text=text,
                        file_id=None, message_id=1, chat_id=1)


class Ev:
    chat_id = 1
    sender_id = 1

    def __init__(self):
        self.out, self.answers = [], []

    async def edit(self, text=None, **kw):
        self.out.append((text, kw.get("buttons")))

    respond = edit

    async def answer(self, *a, **kw):
        self.answers.append(a[0] if a else "")


class Base(unittest.TestCase):
    def setUp(self):
        self.tok = db.use_db_path(os.path.join(tempfile.mkdtemp(), "a.db"))
        db.init_db()

    def tearDown(self):
        db.reset_db_path(self.tok)


class FavoriteTests(Base):
    def test_toggle_list_search_and_backup(self):
        a = _post("Игры", "обзор кота в игре")
        b = _post("Игры", "ещё один кот")
        db.set_favorite(a, True)
        self.assertEqual(db.favorites(), [a])
        self.assertEqual(db.search_items("кот")[0]["id"], a)           # избранное — первым
        db.set_favorite(b, True)
        self.assertEqual(db.favorites(), [b, a])                      # новые — сверху
        db.set_favorite(b, False)
        self.assertEqual(db.favorites(), [a])
        raw = gzip.compress(json.dumps(db.export_json(), ensure_ascii=False, default=str).encode())
        tok = db.use_db_path(os.path.join(tempfile.mkdtemp(), "b.db"))
        db.init_db()
        db.import_json_stream(raw)
        self.assertEqual(db.favorites(), [a])                         # переживает восстановление
        db.reset_db_path(tok)

    def test_favorites_page_and_card_button(self):
        a = _post("Игры", "пост в избранном")
        db.set_favorite(a, True)
        ev = Ev()
        asyncio.run(bot.show_favorites(ev))
        text, rows = ev.out[-1]
        self.assertIn("⭐ Избранное (1)", text)
        btn = rows[0][0]
        data = getattr(btn, "data", None) or getattr(getattr(btn, "type", None), "data", b"")
        self.assertEqual(data, f"view:{a}".encode())
        kb = bot.view_keyboard(a, "Игры", fav=True)
        self.assertIn("⭐ В избранном", kb[0][1].text)

    def test_archive_marks_and_guest_never_sees(self):
        a = _post("Игры", "любимое")
        db.set_favorite(a, True)
        d = webarchive.archive_data({}, "", None)
        self.assertTrue(next(i for i in d["items"] if i["id"] == a)["fv"])
        db.set_category_path("Игры", "Хобби")
        g = webarchive.archive_data({}, "", None, {"kind": "folder", "val": "Хобби"})
        self.assertFalse(next(i for i in g["items"] if i["id"] == a)["fv"])


class BriefTests(Base):
    def test_brief_is_cached(self):
        a = _post("IT", "https://github.com/x/y")
        db.article_set(a, "https://github.com/x/y", "Repo", "long text " * 50, "ok")
        calls = []

        async def fake(title, text):
            calls.append(title)
            return "• Кратко о главном"
        with mock.patch.object(bot._cat, "article_brief", fake):
            self.assertEqual(asyncio.run(bot.article_brief_for(a)), "• Кратко о главном")
            self.assertEqual(asyncio.run(bot.article_brief_for(a)), "• Кратко о главном")
        self.assertEqual(calls, ["Repo"])                             # второй раз — из кэша, без ИИ
        self.assertIsNone(asyncio.run(bot.article_brief_for(999)))
        kb = bot.view_keyboard(a, "IT", article=True)
        self.assertTrue(any(b.text.startswith("🧠") for r in kb for b in r))


class DealsTests(Base):
    def test_parse_and_news(self):
        payload = {"367520": {"success": True, "data": {"name": "Hollow Knight", "is_free": False,
                   "price_overview": {"discount_percent": 50, "final_formatted": "275 руб.",
                                      "initial_formatted": "550 руб."}}}}
        g = gamedeals.parse_app(payload, 367520)
        self.assertEqual((g["name"], g["discount"]), ("Hollow Knight", 50))
        self.assertEqual(gamedeals.news({}, g), "sale")
        self.assertIsNone(gamedeals.news({"discount": 50}, g))             # о той же скидке — не повторяем
        self.assertEqual(gamedeals.news({"discount": 30}, g), "sale")      # выросла — сообщаем
        free = dict(g, free=True, discount=0)
        self.assertEqual(gamedeals.news({"priced": True, "free": False}, free), "free")
        self.assertIsNone(gamedeals.parse_app({"1": {"success": False}}, 1))
        self.assertIn("−50%: 275 руб. (было 550 руб.)", gamedeals.deal_line(g, "sale"))

    def test_app_ids_from_posts_and_articles(self):
        rows = [{"id": 1, "text": "https://store.steampowered.com/app/367520/Hollow_Knight/", "url": ""},
                {"id": 5, "text": "смотри", "url": "https://store.steampowered.com/app/570/"},
                {"id": 7, "text": "и снова https://store.steampowered.com/app/367520/", "url": ""}]
        self.assertEqual(gamedeals.app_ids(rows), {367520: 7, 570: 5})

    def test_daily_check_reports_only_new_sales(self):
        a = _post("Игры", "https://store.steampowered.com/app/367520/")
        sent = []

        async def fake_send(chat, text, **kw):
            sent.append(text)

        async def prices(ids, **kw):
            return {367520: {"name": "Hollow Knight", "free": False, "discount": 50, "final": "275 руб.",
                             "initial": "550 руб.", "priced": True}}
        fake_client = mock.Mock()
        fake_client.send_message = fake_send
        with mock.patch.object(gamedeals, "fetch_prices", prices), mock.patch.object(bot, "client", fake_client):
            self.assertEqual(asyncio.run(bot.check_game_deals(1)), 1)
            asyncio.run(bot.check_game_deals(1))
        self.assertEqual(len(sent), 1)                                # завтра та же скидка — молчим
        self.assertIn("Hollow Knight", sent[0])
        self.assertTrue(a)


if __name__ == "__main__":
    unittest.main()
