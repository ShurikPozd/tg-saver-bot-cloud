"""Ревью 24.11 (вечер), веб: голос доходит целиком, ids только списком, гостю — без обложек чужих сайтов.
Запуск: python -m unittest discover tests"""

import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "smoke.db"))

import database as db  # noqa: E402
import webarchive  # noqa: E402


class WebFixTests(unittest.TestCase):
    def setUp(self):
        self.tok = db.use_db_path(os.path.join(tempfile.mkdtemp(), "a.db"))
        db.init_db()

    def tearDown(self):
        db.reset_db_path(self.tok)

    def _app(self, got):
        from aiohttp import web

        async def smart(q):
            return [], ""

        async def voice(uid, data, ctype):
            got["voice"] = len(data)
            return {"out": [], "toasts": []}

        def trash(ids):
            got["trash"] = list(ids)
            return 0
        app = web.Application()
        webarchive.setup(app, get_client=lambda: None, activate_user=lambda u: u, get_icons=lambda: {},
                         smart_search=smart, get_bot_username=lambda: "b", voice=voice,
                         edit={"trash": trash})
        return app

    def test_voice_body_read_fully_and_ids_must_be_list(self):
        from aiohttp.test_utils import TestClient, TestServer
        got = {}
        tok = webarchive.make_token(5)

        async def run():
            async with TestClient(TestServer(self._app(got))) as cl:
                async def body():
                    for _ in range(10):             # 10 × 60 КБ — приходит кусками
                        yield b"x" * 60_000
                        await asyncio.sleep(0)
                r = await cl.post(f"/a/{tok}/voice", data=body(), headers={"Content-Type": "audio/webm"})
                self.assertEqual(r.status, 200)
                big = await cl.post(f"/a/{tok}/voice", data=b"x" * 2_100_000)
                self.assertIn("длинная", (await big.json())["toasts"][0]["t"])
                await cl.post(f"/a/{tok}/edit/trash", json={"ids": "123"})
                self.assertEqual(got["trash"], [])  # строка «123» — не посты 1, 2, 3
                r2 = await cl.post(f"/a/{tok}/edit/trash", json=[1, 2])
                self.assertEqual(r2.status, 200)     # не 500
        asyncio.run(run())
        self.assertEqual(got["voice"], 600_000)


class ThumbCacheTests(unittest.TestCase):
    """Превью читаются и пишутся в кэш (переименование open → openCard однажды сломало это на сервере)."""

    def setUp(self):
        self.tok = db.use_db_path(os.path.join(tempfile.mkdtemp(), "a.db"))
        db.init_db()
        self.dir = tempfile.mkdtemp()
        self._old = webarchive.THUMB_DIR
        webarchive.THUMB_DIR = self.dir

    def tearDown(self):
        webarchive.THUMB_DIR = self._old
        db.reset_db_path(self.tok)

    def test_cache_read_and_write(self):
        iid = db.save_item(category="Мемы", content_type="photo", summary="кот", original_text="кот",
                           file_id="AgAC", message_id=5, chat_id=7)

        class C:
            async def get_messages(self, chat, ids=None):
                return None                       # сообщения нет — «картинки нет» кэшируется
        self.assertIsNone(asyncio.run(webarchive.thumb_bytes(C(), 1, iid)))
        path = os.path.join(self.dir, "1", f"{iid}.jpg")
        self.assertTrue(os.path.exists(path))
        with open(path, "wb") as f:
            f.write(b"JPEGDATA")
        self.assertEqual(asyncio.run(webarchive.thumb_bytes(C(), 1, iid)), b"JPEGDATA")


if __name__ == "__main__":
    unittest.main()
