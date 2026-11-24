"""Очередь YouTube на телефоне (/yq) и «Отчёт недели» (/api/weekly-report).
Без сети: Telegram подменяется. Запуск: python -m unittest discover tests"""

import asyncio
import gzip
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "smoke.db"))

import bot  # noqa: E402
import database as db  # noqa: E402
import extbackup  # noqa: E402
import webarchive  # noqa: E402
import ytqueue  # noqa: E402
from test_save_video import FakeClient, OWNER, SECRET  # noqa: E402

BACKUP = {
    "format": "ytcs-backup", "v": 1, "at": 1795500000000, "extVersion": "0.29.22",
    "queue": [
        {"id": "dQw4w9WgXcQ", "title": "Rick Astley", "channel": "Rick", "len": 213},
        {"id": "jNQXAC9IVRw", "title": "Me at the zoo", "channel": "jawed", "len": 19, "short": False},
        {"id": "../../etc", "title": "плохой id"},
    ],
    "queuePos": {"dQw4w9WgXcQ": 100, "jNQXAC9IVRw": 3},
}


class Base(unittest.TestCase):
    def setUp(self):
        self.tok = db.use_db_path(os.path.join(tempfile.mkdtemp(), "a.db"))
        db.init_db()
        self.saved = {}
        self.client = FakeClient()
        self.stub(bot, "client", self.client)
        self.stub(bot, "EXT_SECRET", SECRET)
        self.stub(bot, "OWNER_ID", str(OWNER))
        self.stub(extbackup, "_last_at", 0.0)
        bot._weekly_last[0] = 0.0
        os.environ["PUBLIC_URL"] = "https://bot.example"

    def tearDown(self):
        for (obj, name), val in self.saved.items():
            setattr(obj, name, val)
        db.reset_db_path(self.tok)
        os.environ.pop("PUBLIC_URL", None)

    def stub(self, obj, name, val):
        self.saved.setdefault((obj, name), getattr(obj, name))
        setattr(obj, name, val)

    def call(self, method, path, data=None, headers=None):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        async def run():
            app = web.Application()
            app.router.add_post("/api/backup", bot.handler_api_backup)
            app.router.add_post("/api/weekly-report", bot.handler_api_weekly_report)
            ytqueue.register(app, owner=bot._cached_owner, check_init_data=webarchive.check_init_data,
                             check_token=webarchive.check_token)
            async with TestClient(TestServer(app)) as cl:
                r = await cl.request(method, path, data=data, headers=headers or {})
                body = await r.text()
                try:
                    body = json.loads(body)
                except Exception:
                    pass
                return r.status, body

        return asyncio.run(run())


class QueueTests(Base):
    def test_backup_saves_queue_and_page_shows_it(self):
        raw = gzip.compress(json.dumps(BACKUP).encode())
        st, d = self.call("POST", "/api/backup?queue=3", raw, {"X-Sec-Token": SECRET, "Content-Type": "application/gzip"})
        self.assertEqual(st, 200, d)
        snap = ytqueue.load()
        self.assertEqual([x["id"] for x in snap["items"]], ["dQw4w9WgXcQ", "jNQXAC9IVRw"])  # плохой id отброшен
        self.assertEqual(snap["items"][0]["pos"], 100)
        self.assertEqual(snap["items"][1]["pos"], 0)  # меньше 5 с — не «начато»
        token = webarchive.make_token(OWNER, days=1)
        st, page = self.call("GET", f"/yq/{token}/")
        self.assertEqual(st, 200)
        self.assertIn("Очередь YouTube", page)
        self.assertIn('const MODE="link";', page)
        st, d = self.call("GET", f"/yq/{token}/data.json")
        self.assertEqual((st, len(d["items"])), (200, 2))

    def test_foreign_or_bad_token_and_init_data(self):
        st, _ = self.call("GET", f"/yq/{webarchive.make_token(OWNER + 1, days=1)}/data.json")
        self.assertEqual(st, 404)  # чужой человек — не владелец
        st, _ = self.call("GET", "/yq/garbage/data.json")
        self.assertEqual(st, 404)
        st, _ = self.call("POST", "/yq/data", "hash=bad&user=%7B%22id%22%3A1%7D")
        self.assertEqual(st, 403)
        st, page = self.call("GET", "/yq/")
        self.assertIn("telegram-web-app.js", page)

    def test_empty_before_first_backup(self):
        st, d = self.call("GET", f"/yq/{webarchive.make_token(OWNER, days=1)}/data.json")
        self.assertTrue(d["empty"])

    def test_extract_rejects_junk(self):
        self.assertIsNone(ytqueue.extract(b"not gzip"))
        self.assertIsNone(ytqueue.extract(gzip.compress(b'{"format":"other"}')))


class ReportTests(Base):
    def test_report_goes_to_owner_with_queue_buttons(self):
        body = json.dumps({"text": "📊 Неделя на YouTube\n🎬 Смотрели: 2 ч"}).encode()
        st, d = self.call("POST", "/api/weekly-report", body, {"X-Sec-Token": SECRET, "Content-Type": "application/json"})
        self.assertEqual(st, 200, d)
        chat, text, kw = self.client.messages[-1][:3]
        self.assertEqual(chat, OWNER)
        self.assertTrue(text.startswith("📊 Неделя на YouTube"))
        urls = [getattr(b, "url", None) or getattr(getattr(b, "type", None), "url", None)
                for row in kw.get("buttons") for b in row]
        self.assertTrue(any(u and u.endswith("/yq/") for u in urls), urls)
        self.assertTrue(any(u and "/yq/" in u and not u.endswith("/yq/") for u in urls), urls)
        st, d = self.call("POST", "/api/weekly-report", body, {"X-Sec-Token": SECRET})
        self.assertEqual((st, d["code"]), (429, "rate"))

    def test_report_auth_and_size(self):
        st, _ = self.call("POST", "/api/weekly-report", b'{"text":"x"}', {"X-Sec-Token": "wrong"})
        self.assertEqual(st, 403)
        st, d = self.call("POST", "/api/weekly-report", json.dumps({"text": "я" * 3001}, ensure_ascii=False).encode(), {"X-Sec-Token": SECRET})
        self.assertEqual(d["code"], "bad_request")

    def test_routes_and_command_registered(self):
        import inspect

        src = inspect.getsource(bot.health_http)
        self.assertIn('add_post("/api/weekly-report", handler_api_weekly_report)', src)
        self.assertIn("ytqueue.register(app", src)
        self.assertIn('elif cmd == "/yt":', inspect.getsource(bot))


if __name__ == "__main__":
    unittest.main()
