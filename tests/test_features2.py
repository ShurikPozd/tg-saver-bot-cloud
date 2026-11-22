"""Поиск по смыслу, вопрос о новой категории после 3 постов, сессия помощника в БД,
веб-архив (токен, данные, страница). Без сети: ИИ и Telegram подменяются.
Запуск: python -m unittest discover tests"""

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "smoke.db"))

import bot  # noqa: E402
import categorizer  # noqa: E402
import database as db  # noqa: E402
import webarchive  # noqa: E402


def _post(cat, summary, text="", ct="text"):
    return db.save_item(category=cat, content_type=ct, summary=summary, original_text=text or summary,
                        file_id=None, message_id=1, chat_id=1)


class Base(unittest.TestCase):
    def setUp(self):
        self.tok = db.use_db_path(os.path.join(tempfile.mkdtemp(), "a.db"))
        db.init_db()
        self.cat_id = _post("Смешное", "Кот сидит в коробке", ct="photo")
        self.dog_id = _post("Смешное", "Собака ловит мяч", ct="video")
        self.ps_id = _post("Игры", "Новая прошивка для PS5")
        self.saved = {}

    def tearDown(self):
        for (obj, name), val in self.saved.items():
            setattr(obj, name, val)
        db.reset_db_path(self.tok)

    def stub(self, obj, name, fn):
        self.saved.setdefault((obj, name), getattr(obj, name))
        setattr(obj, name, fn)


class SmartSearchTests(Base):
    def test_words_from_ai_then_ranking(self):
        async def terms(q):
            return {"words": ["кот", "cat"], "date_from": "", "date_to": "", "type": ""}
        seen = {}

        async def rank(q, items, limit=15):
            seen["ids"] = [i["id"] for i in items]
            return [i["id"] for i in items]
        self.stub(categorizer, "search_terms", terms)
        self.stub(categorizer, "rank_posts", rank)
        items, note = asyncio.run(bot.smart_search("котик в коробке"))
        self.assertEqual([i["id"] for i in items], [self.cat_id])
        self.assertEqual(seen["ids"], [self.cat_id])   # кандидаты отобраны словами, PS5 не попал

    def test_type_filter_falls_back_to_any_type(self):
        async def terms(q):
            return {"words": ["кот"], "date_from": "", "date_to": "", "type": "video"}

        async def rank(q, items, limit=15):
            return [i["id"] for i in items if "Кот" in i["summary"]]
        self.stub(categorizer, "search_terms", terms)
        self.stub(categorizer, "rank_posts", rank)
        items, note = asyncio.run(bot.smart_search("видео с котом"))
        self.assertEqual([i["id"] for i in items], [self.cat_id])   # кот на фото, а не в видео
        self.assertIn("Такого типа не нашёл", note)

    def test_ai_down_uses_plain_words(self):
        async def none(*a, **k):
            return None
        self.stub(categorizer, "search_terms", none)
        self.stub(categorizer, "rank_posts", none)
        items, note = asyncio.run(bot.smart_search("собака"))
        self.assertEqual([i["id"] for i in items], [self.dog_id])
        self.assertTrue(note)


class NewCategoryTests(Base):
    def test_asks_once_after_three_posts(self):
        sent = []

        class FakeClient:
            async def send_message(self, chat, text, **kw):
                sent.append(text)
        self.stub(bot, "client", FakeClient())
        ids = [_post("Другое", f"Дорама {i}") for i in range(3)]
        for n, iid in enumerate(ids, 1):
            bot._note_newcat_offer("Дорамы", iid)
            asyncio.run(bot._maybe_ask_newcat(1, "Дорамы"))
            self.assertEqual(len(sent), 0 if n < 3 else 1)
        asyncio.run(bot._maybe_ask_newcat(1, "Дорамы"))
        self.assertEqual(len(sent), 1)                       # второй раз не спрашивает
        self.assertIn("Дорамы", sent[0])

    def test_declined_name_is_not_offered_again(self):
        db.set_setting("newcat_declined", json.dumps(["Shikimori"]))

        async def fake(text, **kw):
            return {"category": "Shikimori", "summary": "s", "confidence": 0.9,
                    "alternatives": ["Смешное"], "llm_ok": True}
        self.stub(bot, "categorize", fake)
        res = asyncio.run(bot._ai_categorize("x", "text", None, exclude_id=self.cat_id))
        self.assertNotIn("proposed_new", res)
        self.assertEqual(bot._newcat_offers(), {})


class AssistantSessionTests(Base):
    def test_session_survives_restart_and_talk_window(self):
        chat = 777
        sess = bot._assistant_session(chat, ids=[self.cat_id])
        sess["plan"] = [{"op": "set_icon", "name": "Игры", "icon": "🎮", "text": "t"}]
        sess["off"] = {0}
        bot._assistant_save(chat, sess)
        bot.assistant_sessions.clear()                       # «перезапуск»
        again = bot._assistant_load(chat)
        self.assertEqual(again["ids"], [self.cat_id])
        self.assertEqual(again["off"], {0})
        self.assertTrue(bot._assistant_active(chat))
        again["talk_until"] = 0                              # еженедельный план: кнопки есть, текст — нет
        bot._assistant_save(chat, again)
        bot.assistant_sessions.clear()
        self.assertFalse(bot._assistant_active(chat))
        self.assertIsNotNone(bot._assistant_load(chat))


class WebArchiveTests(Base):
    def test_token(self):
        t = webarchive.make_token(42)
        self.assertEqual(webarchive.check_token(t), 42)
        uid, exp, sig = t.split(".")
        self.assertIsNone(webarchive.check_token(f"43.{exp}.{sig}"))           # чужой id
        self.assertIsNone(webarchive.check_token(webarchive.make_token(42, days=-1)))  # просрочен
        self.assertIsNone(webarchive.check_token("мусор"))

    def test_data_and_page(self):
        db.set_category_path("Смешное", "Развлечения")
        data = webarchive.archive_data({"Игры": "🎮"}, "my_bot")
        self.assertEqual(len(data["items"]), 3)
        self.assertEqual(data["groups"]["Смешное"], "Развлечения")
        self.assertIn("Развлечения", data["folders"])
        self.assertEqual(data["bot"], "my_bot")
        self.assertIn("data.json", webarchive.PAGE_HTML)
        self.assertIn('name="viewport"', webarchive.PAGE_HTML)

    def test_http_routes(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        async def run():
            app = web.Application()

            async def smart(q):
                return [{"id": self.ps_id}], ""
            webarchive.setup(app, get_client=lambda: None, activate_user=lambda uid: uid,
                             get_icons=lambda: {}, smart_search=smart, get_bot_username=lambda: "b")
            async with TestClient(TestServer(app)) as cl:
                tok = webarchive.make_token(5)
                self.assertEqual((await cl.get(f"/a/{tok}/")).status, 200)
                self.assertEqual((await cl.get("/a/5.1.bad/")).status, 403)
                d = await (await cl.get(f"/a/{tok}/data.json")).json()
                self.assertEqual(len(d["items"]), 3)
                s = await (await cl.get(f"/a/{tok}/search", params={"q": "ps"})).json()
                self.assertEqual(s["ids"], [self.ps_id])
                self.assertEqual((await cl.get(f"/a/{tok}/thumb/{self.cat_id}.jpg")).status, 404)
        asyncio.run(run())


def _init_data(uid, token, age=0, tamper=False):
    import hashlib
    import hmac
    from urllib.parse import urlencode
    pairs = {"auth_date": str(int(time.time()) - age), "query_id": "AAH",
             "user": json.dumps({"id": uid, "first_name": "Я"}, ensure_ascii=False)}
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if tamper:
        pairs["user"] = json.dumps({"id": uid + 1})
    return urlencode(pairs)


class MiniAppTests(Base):
    def setUp(self):
        super().setUp()
        self.stub(webarchive, "BOT_TOKEN", "123:test-token")

    def test_init_data_signature(self):
        self.assertEqual(webarchive.check_init_data(_init_data(7, "123:test-token")), 7)
        self.assertIsNone(webarchive.check_init_data(_init_data(7, "123:other")))           # чужой бот
        self.assertIsNone(webarchive.check_init_data(_init_data(7, "123:test-token", tamper=True)))
        self.assertIsNone(webarchive.check_init_data(_init_data(7, "123:test-token", age=8 * 86400)))
        self.assertIsNone(webarchive.check_init_data("мусор"))

    def test_app_routes(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        sent = []

        async def send_item(uid, iid):
            sent.append((uid, iid))
            return True

        async def run():
            app = web.Application()

            async def smart(q):
                return [], ""
            webarchive.setup(app, get_client=lambda: None, activate_user=lambda uid: uid, get_icons=lambda: {},
                             smart_search=smart, get_bot_username=lambda: "b", send_item=send_item)
            async with TestClient(TestServer(app)) as cl:
                page = await (await cl.get("/app/")).text()
                self.assertIn("const APP_MODE=true;", page)
                self.assertIn("telegram-web-app.js", page)
                self.assertEqual((await cl.post("/app/auth", data="hash=bad")).status, 403)
                r = await cl.post("/app/auth", data=_init_data(9, "123:test-token"))
                base = (await r.json())["base"]
                self.assertTrue(base.startswith("/a/9."))
                self.assertEqual(len((await (await cl.get(base + "data.json")).json())["items"]), 3)
                self.assertEqual((await cl.post(base + f"send/{self.ps_id}")).status, 200)
                self.assertEqual(sent, [(9, self.ps_id)])
                browser = await (await cl.get(base)).text()
                self.assertIn("const APP_MODE=false;", browser)
                self.assertNotIn("telegram-web-app.js", browser)   # обычная ссылка не ждёт telegram.org
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
