"""Обложки ссылок, голос в приложении, быстрый запуск (ETag), мёртвые ссылки. Без сети и Telegram.
Запуск: python -m unittest discover tests"""

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "smoke.db"))

import bot  # noqa: E402
import database as db  # noqa: E402
import linkmeta  # noqa: E402
import webarchive  # noqa: E402


def _post(text):
    return db.save_item(category="Разное", content_type="text", summary=text[:50], original_text=text,
                        file_id=None, message_id=1, chat_id=1)


class Base(unittest.TestCase):
    def setUp(self):
        self.tok = db.use_db_path(os.path.join(tempfile.mkdtemp(), "a.db"))
        db.init_db()

    def tearDown(self):
        db.reset_db_path(self.tok)


class CoverTests(unittest.TestCase):
    def test_og_image_found_and_made_absolute(self):
        h = '<head><meta content="/img/cover.png" property="og:image"><title>x</title></head>'
        self.assertEqual(linkmeta.og_image_from_html(h, "https://site.ru/a/b"), "https://site.ru/img/cover.png")
        h2 = '<meta name="twitter:image" content="http://cdn.x.com/p.jpg">'
        self.assertEqual(linkmeta.og_image_from_html(h2, "https://x.com/"), "https://cdn.x.com/p.jpg")
        self.assertIsNone(linkmeta.og_image_from_html("<head></head>", "https://x.com/"))


class DeadLinkTests(Base):
    def test_dead_only_after_two_failures_a_day_apart(self):
        a = _post("статья https://gone.example.com/page")
        b = _post("живая https://ok.example.com/")
        _post("канал https://t.me/somechannel")             # Telegram не проверяем
        res = {"https://gone.example.com/page": "dead", "https://ok.example.com/": "ok"}
        checked = []

        async def fake(url):
            checked.append(url)
            return res[url]
        with mock.patch.object(linkmeta, "check_link", fake):
            self.assertEqual(asyncio.run(bot.check_links_once(1)), [])           # первый провал — не хороним
            self.assertNotIn("https://t.me/somechannel", checked)
            h = json.loads(db.get_setting("link_health"))
            h[str(a)]["t"] -= 21 * 3600                                         # «прошли сутки»
            db.set_setting("link_health", json.dumps(h))
            self.assertEqual(asyncio.run(bot.check_links_once(1)), [a])
        self.assertIn(a, bot.dead_links())
        self.assertNotIn(b, bot.dead_links())
        d = webarchive.archive_data({}, "", None, dead=bot.dead_links())
        self.assertTrue(next(i for i in d["items"] if i["id"] == a)["dd"])
        res["https://gone.example.com/page"] = "ok"                            # ожила — пометка снимается
        with mock.patch.object(linkmeta, "check_link", fake):
            h = json.loads(db.get_setting("link_health"))
            for v in h.values():
                v["c"] = 0
            db.set_setting("link_health", json.dumps(h))
            asyncio.run(bot.check_links_once(1))
        self.assertNotIn(a, bot.dead_links())

    def test_unknown_never_kills(self):
        a = _post("https://flaky.example.com/x")

        async def fake(url):
            return "unknown"
        with mock.patch.object(linkmeta, "check_link", fake):
            for _ in range(3):
                asyncio.run(bot.check_links_once(1))
        self.assertNotIn(a, bot.dead_links())


class VoiceTests(Base):
    def test_voice_goes_to_assistant_as_text(self):
        seen = []

        async def stt(data, **kw):
            return "найди мемы с котами"

        async def bridge(uid, body):
            seen.append(body)
            return {"out": [], "toasts": [], "ask": None, "assistant": True}
        with mock.patch.object(bot, "transcribe_audio", stt), mock.patch.object(bot, "web_bridge", bridge):
            r = asyncio.run(bot.web_voice(1, b"x" * 2000, "audio/webm"))
        self.assertEqual(r["heard"], "найди мемы с котами")
        self.assertEqual(seen, [{"kind": "text", "text": "найди мемы с котами"}])

    def test_silence(self):
        async def stt(data, **kw):
            return ""
        with mock.patch.object(bot, "transcribe_audio", stt), mock.patch.object(bot, "_assistant_active", lambda u: False):
            r = asyncio.run(bot.web_voice(1, b"x" * 2000, "audio/webm"))
        self.assertEqual(r["heard"], "")
        self.assertTrue(r["toasts"])


class EtagTests(Base):
    def test_data_304_when_unchanged(self):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer
        _post("пост")
        tok = webarchive.make_token(5)

        async def run():
            app = web.Application()

            async def smart(q):
                return [], ""
            webarchive.setup(app, get_client=lambda: None, activate_user=lambda u: u, get_icons=lambda: {},
                             smart_search=smart, get_bot_username=lambda: "b")
            async with TestClient(TestServer(app)) as cl:
                r1 = await cl.get(f"/a/{tok}/data.json")
                etag = r1.headers["ETag"]
                self.assertEqual(r1.status, 200)
                r2 = await cl.get(f"/a/{tok}/data.json", headers={"If-None-Match": etag})
                self.assertEqual(r2.status, 304)
                _post("ещё пост")
                r3 = await cl.get(f"/a/{tok}/data.json", headers={"If-None-Match": etag})
                self.assertEqual(r3.status, 200)
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
