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


class Base(unittest.TestCase):
    def setUp(self):
        self.saved = {}
        self.client = FakeClient()
        self.stub(bot, "client", self.client)
        self.stub(bot, "EXT_SECRET", SECRET)
        self.stub(bot, "OWNER_ID", str(OWNER))
        self.stub(extbackup, "_last_at", 0.0)
        self.stub(extbackup, "_last_at_app", {})
        self.backups = []
        self.extracted = []
        real_extract = bot.ytqueue.extract

        def extract(raw):
            self.extracted.append(raw)
            return real_extract(raw)

        self.stub(bot.ytqueue, "extract", extract)

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


class BackupTests(Base):
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

    def test_youtube_backup_still_extracts_queue(self):
        self.assertEqual(self.post()[0], 200)
        self.assertEqual(self.extracted, [BODY])


TC_BODY = gzip.compress(b'{"app":"tab-collector","version":1,"savedAt":1,"items":[],"folders":[]}')
TC_QUERY = "?app=tab-collector&ver=0.5.0&links=1234&folders=7&manual=0"


class TabCollectorBackupTests(Base):
    """?app=tab-collector — своя подпись, имя файла и ограничитель; очередь YouTube не извлекается."""

    def test_own_caption_and_filename(self):
        st, d = self.post(data=TC_BODY, query=TC_QUERY)
        self.assertEqual(st, 200, d)
        self.assertTrue(d["ok"])
        chat, name, caption, _post = self.client.files[0]
        self.assertEqual(chat, OWNER)
        self.assertRegex(name, r"^tab-collector-backup-\d{4}-\d{2}-\d{2}\.json\.gz$")
        self.assertIn("«Сборщика вкладок»", caption)
        self.assertIn("ссылок: 1234 · папок: 7", caption)
        self.assertIn("версия расширения 0.5.0", caption)
        self.assertIn("автоматически", caption)
        self.assertIn("Настройки", caption)
        self.assertNotIn("YouTube", caption)
        self.assertNotIn("очередь", caption)
        self.assertEqual(self.client.messages, [])   # в архив постов ничего не идёт
        self.assertEqual(self.extracted, [])         # очередь YouTube из такой копии не берём
        self.assertEqual(self.backups, [])

    def test_manual_and_junk(self):
        cap = extbackup.caption({"app": "tab-collector", "ver": "x<b>", "links": "-1", "folders": "abc",
                                 "manual": "1"})
        self.assertIn("по кнопке", cap)
        self.assertNotIn("ссылок", cap)
        self.assertNotIn("папок", cap)
        self.assertNotIn("<b>", cap)

    def test_rate_limits_are_separate(self):
        self.assertEqual(self.post(data=TC_BODY, query=TC_QUERY)[0], 200)
        st, d = self.post(data=TC_BODY, query=TC_QUERY)
        self.assertEqual((st, d["code"]), (429, "rate"))
        self.assertEqual(self.post()[0], 200)                    # копия YouTube не ждёт «Сборщик»
        self.assertEqual(self.post()[1]["code"], "rate")
        self.stub(extbackup, "_last_at", 0.0)                     # и наоборот
        self.assertEqual(self.post(data=TC_BODY, query=TC_QUERY)[1]["code"], "rate")
        self.assertEqual(self.post()[0], 200)

    def test_send_failure_frees_only_its_limiter(self):
        self.assertEqual(self.post()[0], 200)                    # YouTube только что отправлена
        self.client.file_fails = True
        st, d = self.post(data=TC_BODY, query=TC_QUERY)
        self.assertEqual((st, d["code"]), (502, "tg_send"))
        self.client.file_fails = False
        self.assertEqual(self.post(data=TC_BODY, query=TC_QUERY)[0], 200)   # можно повторить сразу
        self.assertEqual(self.post()[1]["code"], "rate")         # а YouTube всё ещё ждёт

    def test_unknown_or_empty_app_is_youtube(self):
        for q in ("?app=&queue=1", "?app=something&queue=1", "?app=TAB-COLLECTOR&links=1"):
            self.assertEqual(extbackup.app_of(dict(p.split("=") for p in q[1:].split("&"))),
                             "tab-collector" if "TAB" in q else "")
        st, d = self.post(query="?app=something&queue=3")
        self.assertEqual(st, 200)
        _chat, name, caption, _ = self.client.files[0]
        self.assertTrue(name.startswith("ytcs-backup-"))
        self.assertIn("очередь: 3 видео", caption)

    def test_filenames(self):
        t = 1764000000  # 2025-11-24 UTC
        self.assertEqual(extbackup.filename(t), "ytcs-backup-2025-11-24.json.gz")
        self.assertEqual(extbackup.filename(t, app="tab-collector"), "tab-collector-backup-2025-11-24.json.gz")

    def test_route_registered(self):
        import inspect

        src = inspect.getsource(bot.health_http)
        self.assertIn('add_post("/api/backup", handler_api_backup)', src)


if __name__ == "__main__":
    unittest.main()
