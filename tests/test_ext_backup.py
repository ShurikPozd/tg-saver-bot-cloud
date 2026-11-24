"""POST /api/backup: автокопия расширения → файлом владельцу в Telegram (не в архив постов).
Без сети: Telegram подменяется. Запуск: python -m unittest discover tests"""

import asyncio
import gzip
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "smoke.db"))

import bot  # noqa: E402
import extbackup  # noqa: E402
from test_save_video import FakeClient, OWNER, SECRET  # noqa: E402

BODY = gzip.compress(b'{"format":"ytcs-backup","v":1,"queue":[]}')


class BackupTests(unittest.TestCase):
    def setUp(self):
        self.saved = {}
        self.client = FakeClient()
        self.stub(bot, "client", self.client)
        self.stub(bot, "EXT_SECRET", SECRET)
        self.stub(bot, "OWNER_ID", str(OWNER))
        self.stub(extbackup, "_last_at", 0.0)
        self.backups = []

        async def fake_backup(uid, reason="Ежедневная"):
            self.backups.append((uid, reason))

        self.stub(bot, "_send_tg_backup", fake_backup)  # в тестах — никакого GitHub

    def tearDown(self):
        for (obj, name), val in self.saved.items():
            setattr(obj, name, val)

    def stub(self, obj, name, val):
        self.saved.setdefault((obj, name), getattr(obj, name))
        setattr(obj, name, val)

    def post(self, data=BODY, secret=SECRET, query="?queue=187&playlists=12&videos=900&ver=0.29.18"):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        async def run():
            app = web.Application()
            app.router.add_post("/api/backup", bot.handler_api_backup)
            async with TestClient(TestServer(app)) as cl:
                headers = {"Content-Type": "application/gzip"}
                if secret is not None:
                    headers["X-Sec-Token"] = secret
                r = await cl.post("/api/backup" + query, data=data, headers=headers)
                return r.status, await r.json()

        return asyncio.run(run())

    def test_sends_file_to_owner_with_caption(self):
        st, d = self.post()
        self.assertEqual(st, 200, d)
        self.assertTrue(d["ok"])
        self.assertEqual(len(self.client.files), 1)
        chat, name, caption, _post = self.client.files[0]
        self.assertEqual(chat, OWNER)
        self.assertTrue(name.startswith("ytcs-backup-") and name.endswith(".json.gz"))
        self.assertIn("очередь: 187 видео", caption)
        self.assertIn("0.29.18", caption)
        self.assertEqual(self.client.messages, [])  # в архив постов ничего не идёт

    def test_auth_and_body_checks(self):
        self.assertEqual(self.post(secret=None)[0], 403)
        self.assertEqual(self.post(secret="wrong")[0], 403)
        self.assertEqual(self.post(data=b'{"not":"gzip"}')[1]["code"], "bad_request")
        self.assertEqual(self.client.files, [])

    def test_rate_limit_and_offline(self):
        self.assertEqual(self.post()[0], 200)
        st, d = self.post()
        self.assertEqual((st, d["code"]), (429, "rate"))
        self.stub(extbackup, "_last_at", 0.0)
        self.client.connected = False
        st, d = self.post()
        self.assertEqual((st, d["code"]), (503, "tg_offline"))

    def test_caption_ignores_junk(self):
        cap = extbackup.caption({"ver": "1.2<script>", "queue": "-5", "manual": "1"})
        self.assertNotIn("script", cap)
        self.assertNotIn("очередь", cap)
        self.assertIn("по кнопке", cap)

    def test_route_registered(self):
        import inspect

        src = inspect.getsource(bot.health_http)
        self.assertIn('add_post("/api/backup", handler_api_backup)', src)


if __name__ == "__main__":
    unittest.main()
