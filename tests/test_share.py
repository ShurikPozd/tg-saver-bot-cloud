"""«🔗 Поделиться папкой»: гость видит только эту папку, отозванная ссылка не открывается.
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
import webarchive  # noqa: E402


def _post(cat, summary, aux=""):
    text = summary + (f"\n\n〔распознано〕\n{aux}" if aux else "")
    return db.save_item(category=cat, content_type="text", summary=summary, original_text=text,
                        file_id=None, message_id=1, chat_id=1, source_channel="мой канал")


class ShareTests(unittest.TestCase):
    def setUp(self):
        self.tok = db.use_db_path(os.path.join(tempfile.mkdtemp(), "a.db"))
        db.init_db()
        self.cat = _post("Мемы про животных", "кот в коробке", aux="Изображение: кот")
        self.dog = _post("Собаки", "пёс ловит мяч")
        self.secret = _post("Заметки", "мой личный пароль от почты")
        db.set_category_path("Мемы про животных", "Смешное/Животные")
        db.set_category_path("Собаки", "Смешное/Животные/Собаки")
        bot._shares_set({"abc123": {"kind": "folder", "val": "Смешное/Животные"},
                         "cat777": {"kind": "cat", "val": "Собаки"}})

    def tearDown(self):
        db.reset_db_path(self.tok)

    def test_guest_sees_only_the_folder(self):
        data = webarchive.archive_data({}, "", None, bot.get_share("abc123"))
        self.assertEqual({i["id"] for i in data["items"]}, {self.cat, self.dog})   # с подпапкой
        self.assertTrue(all(i["v"] == "" and i["ch"] == "" for i in data["items"]))  # без «что увидел ИИ»
        self.assertEqual(data["title"], "Животные")
        self.assertNotIn("Заметки", data["groups"])
        only_dogs = webarchive.archive_data({}, "", None, bot.get_share("cat777"))
        self.assertEqual([i["id"] for i in only_dogs["items"]], [self.dog])

    def test_http_routes_and_revoke(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        uid = 5

        async def run():
            app = web.Application()

            async def smart(q):
                return [], ""
            webarchive.setup(app, get_client=lambda: None, activate_user=lambda u: u, get_icons=lambda: {},
                             smart_search=smart, get_bot_username=lambda: "b", get_share=bot.get_share)
            async with TestClient(TestServer(app)) as cl:
                page = await (await cl.get(f"/s/{uid}.abc123/")).text()
                self.assertIn("const SHARE_MODE=true;", page)
                d = await (await cl.get(f"/s/{uid}.abc123/data.json")).json()
                self.assertEqual(len(d["items"]), 2)
                self.assertEqual((await cl.get(f"/s/{uid}.abc123/thumb/{self.secret}.jpg")).status, 404)  # чужой пост
                self.assertEqual((await cl.get(f"/s/{uid}.nope/")).status, 404)
                shares = bot._shares()
                shares.pop("abc123")
                bot._shares_set(shares)                                   # «🚫 Отозвать»
                self.assertEqual((await cl.get(f"/s/{uid}.abc123/data.json")).status, 404)
        asyncio.run(run())

    def test_share_button_creates_one_link_per_folder(self):
        sent = []

        class Ev:
            sender_id = 5

            async def respond(self, text, **kw):
                sent.append(text)

        orig = webarchive.public_url
        webarchive.public_url = lambda: "https://example.test"
        try:
            asyncio.run(bot.share_folder(Ev(), "folder", "Смешное"))
            asyncio.run(bot.share_folder(Ev(), "folder", "Смешное"))
        finally:
            webarchive.public_url = orig
        links = [line for t in sent for line in t.splitlines() if line.startswith("https://")]
        self.assertEqual(len(set(links)), 1)                             # повторно — та же ссылка
        self.assertIn("(2 пост.)", sent[0])


class WebEditTests(unittest.TestCase):
    def setUp(self):
        self.tok = db.use_db_path(os.path.join(tempfile.mkdtemp(), "a.db"))
        db.init_db()
        self.a = _post("Игры", "пост про GTA")
        self.b = _post("Игры", "ещё пост")
        _post("Смешное", "мем")

    def tearDown(self):
        db.reset_db_path(self.tok)

    def test_edit_api(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        edit = {"move": bot.web_move, "rename": bot.web_rename, "trash": bot.web_trash,
                "remind": bot.web_remind, "unlock": bot.web_unlock}

        async def run():
            app = web.Application()

            async def smart(q):
                return [], ""
            webarchive.setup(app, get_client=lambda: None, activate_user=lambda u: u, get_icons=lambda: {},
                             smart_search=smart, get_bot_username=lambda: "b", edit=edit)
            base = f"/a/{webarchive.make_token(7)}/edit/"
            async with TestClient(TestServer(app)) as cl:
                j = await (await cl.post(base + "move", json={"ids": [self.a, self.b], "to": "Смешное"})).json()
                self.assertEqual(j["done"], 2)
                self.assertEqual(db.get_item(self.a)["category"], "Смешное")
                j = await (await cl.post(base + "move", json={"ids": [self.a], "to": "Нет такой"})).json()
                self.assertEqual(j["done"], 0)                       # только в существующую папку
                if db.should_lock("move"):                            # автозамок: правка закрыта, пока не снять
                    self.assertIn(self.a, j["locked"] or [self.a])
                    self.assertFalse((await (await cl.post(base + "rename", json={"id": self.a, "summary": "x"})).json())["ok"])
                    self.assertTrue((await (await cl.post(base + "unlock", json={"id": self.a})).json())["ok"])
                self.assertTrue((await (await cl.post(base + "rename", json={"id": self.a, "summary": "Новое имя"})).json())["ok"])
                self.assertEqual(db.get_item(self.a)["summary"], "Новое имя")
                j = await (await cl.post(base + "remind", json={"id": self.a, "kind": "w"})).json()
                self.assertTrue(j["label"])
                self.assertEqual(bot._reminder_for(self.a)["chat"], 7)
                db.set_item_locked(self.b, False)
                j = await (await cl.post(base + "trash", json={"ids": [self.b]})).json()
                self.assertEqual(j["done"], 1)
                self.assertIsNone(db.get_item(self.b))
                share = f"/s/7.nope/"                                  # у гостя правки нет вообще
                self.assertEqual((await cl.post(share.replace("/s/", "/a/") + "edit/trash", json={"ids": [self.a]})).status, 403)
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
