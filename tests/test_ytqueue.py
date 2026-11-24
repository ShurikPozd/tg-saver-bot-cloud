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
        bot._yq_sync_hits.clear()
        self.backups = []
        self.soon = []
        self.stub(bot, "_yq_backup_soon", lambda: self.soon.append(1))

        async def fake_backup(uid, reason="Ежедневная"):
            self.backups.append((uid, reason))

        self.stub(bot, "_send_tg_backup", fake_backup)  # в тестах — никакого GitHub
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
            app.router.add_post("/api/yq-sync", bot.handler_api_yq_sync)
            ytqueue.register(app, owner=bot._cached_owner, check_init_data=webarchive.check_init_data,
                             check_token=webarchive.check_token, on_change=lambda: bot._yq_backup_soon())
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
        self.assertEqual(self.backups, [(OWNER, "очередь YouTube")])  # копия архива обновлена — переживёт перезапуск
        self.assertEqual(snap["items"][0]["pos"], 100)
        self.assertEqual(snap["items"][1]["pos"], 0)  # меньше 5 с — не «начато»
        token = webarchive.make_token(OWNER, days=1)
        st, page = self.call("GET", f"/yq/{token}/")
        self.assertEqual(st, 200)
        self.assertIn("Очередь YouTube", page)
        self.assertIn('const MODE="link";', page)
        self.assertIn('rel="icon"', page)  # значок расширения во вкладке
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


SYNC_ITEMS = [
    {"id": "dQw4w9WgXcQ", "title": "Rick", "channel": "R", "len": 213, "pos": 100, "added": 1795000000},
    {"id": "jNQXAC9IVRw", "title": "Zoo", "channel": "jawed", "len": 19, "pos": 0, "added": 1795000000},
    {"id": "bad", "title": "плохой id"},
]


class SyncTests(Base):
    def sync(self, body, token=SECRET):
        return self.call("POST", "/api/yq-sync", json.dumps(body).encode(),
                         {"X-Sec-Token": token, "Content-Type": "application/json"})

    def page(self):
        return self.call("GET", f"/yq/{webarchive.make_token(OWNER, days=1)}/data.json")[1]

    def mark(self, vid, act):
        return self.call("POST", f"/yq/{webarchive.make_token(OWNER, days=1)}/mark", json.dumps({"id": vid, "act": act}).encode())

    def test_sync_saves_queue_and_backs_up(self):
        st, d = self.sync({"rev": "h1", "ver": "0.29.26", "items": SYNC_ITEMS})
        self.assertEqual(st, 200, d)
        self.assertEqual((d["ok"], d["marks"], d["want"]), (True, [], False))
        snap = ytqueue.load()
        self.assertEqual([x["id"] for x in snap["items"]], ["dQw4w9WgXcQ", "jNQXAC9IVRw"])
        self.assertEqual((snap["rev"], snap["items"][0]["pos"], snap["items"][0]["added"]), ("h1", 100, 1795000000))
        self.assertEqual(len(self.soon), 1)  # тихая копия в GitHub — иначе очередь пропадёт при перезапуске
        st, d = self.sync({"rev": "h1"})  # только спросить отметки: очередь та же — ничего не пишем
        self.assertEqual((st, d["want"], len(self.soon)), (200, False, 1))
        st, d = self.sync({"rev": "h2"})  # у бота другая очередь (например, восстановлен из старой копии)
        self.assertTrue(d["want"])

    def test_sync_auth_and_junk(self):
        st, _ = self.sync({"rev": "x"}, token="wrong")
        self.assertEqual(st, 403)
        st, d = self.call("POST", "/api/yq-sync", b"[1,2]", {"X-Sec-Token": SECRET})
        self.assertEqual((st, d["code"]), (400, "bad_request"))
        bot._yq_sync_hits[:] = [__import__("time").time()] * bot.YQ_SYNC_LIMIT[0]
        st, d = self.sync({"rev": "x"})
        self.assertEqual((st, d["code"]), (429, "rate"))

    def test_marks_from_phone_round_trip(self):
        self.sync({"rev": "h1", "items": SYNC_ITEMS})
        st, d = self.mark("dQw4w9WgXcQ", "done")
        self.assertEqual((st, d["pending"]), (200, 1))
        self.assertEqual(len(self.soon), 2)  # отметка тоже попадает в тихую копию
        page = self.page()
        self.assertEqual([x["id"] for x in page["items"]], ["jNQXAC9IVRw"])  # пропало со страницы сразу
        self.assertEqual(page["pending"], 1)
        st, d = self.sync({"rev": "h1"})
        self.assertEqual([(m["id"], m["act"]) for m in d["marks"]], [("dQw4w9WgXcQ", "done")])
        at = d["marks"][0]["at"]
        self.assertIn("yt_queue_marks", [x["key"] for x in db.export_json()["settings"]])  # отметки — в экспорте
        st, d = self.sync({"rev": "h1", "acks": [{"id": "dQw4w9WgXcQ", "at": at - 1}]})
        self.assertEqual(len(d["marks"]), 1)  # подтверждение старее отметки — не стираем
        st, d = self.sync({"rev": "h1", "acks": [{"id": "dQw4w9WgXcQ", "at": at}]})
        self.assertEqual((d["marks"], self.page()["pending"]), ([], 0))

    def test_undo_and_bad_marks(self):
        self.sync({"rev": "h1", "items": SYNC_ITEMS})
        self.mark("jNQXAC9IVRw", "drop")
        st, d = self.mark("jNQXAC9IVRw", "undo")
        self.assertEqual((st, d["pending"]), (200, 0))
        self.assertEqual(len(self.page()["items"]), 2)
        self.assertEqual(self.mark("../../etc/x", "done")[0], 400)
        self.assertEqual(self.mark("jNQXAC9IVRw", "delete_all")[0], 400)
        st, _ = self.call("POST", f"/yq/{webarchive.make_token(OWNER + 1, days=1)}/mark", b'{"id":"jNQXAC9IVRw","act":"drop"}')
        self.assertEqual(st, 404)
        st, _ = self.call("POST", "/yq/mark", b'{"init":"hash=bad","id":"jNQXAC9IVRw","act":"drop"}')
        self.assertEqual(st, 403)
        self.assertEqual(ytqueue.load_marks(), {})

    def test_readded_after_mark_stays(self):
        self.sync({"rev": "h1", "items": SYNC_ITEMS})
        ytqueue.set_mark("dQw4w9WgXcQ", "drop", now=1795000500)
        self.assertEqual(len(self.page()["items"]), 1)
        again = [dict(SYNC_ITEMS[0], added=1795000900)] + SYNC_ITEMS[1:]  # на компьютере добавили заново
        self.sync({"rev": "h2", "items": again})
        self.assertEqual(len(self.page()["items"]), 2)  # отметка старше добавления — видео видно

    def test_backup_throttle(self):
        calls = []

        async def fake(uid, reason="Ежедневная"):
            calls.append((round(__import__("time").time(), 1), reason))

        self.stub(bot, "_send_tg_backup", fake)
        self.stub(bot, "YQ_BACKUP_EVERY", 0.3)
        soon = self.saved[(bot, "_yq_backup_soon")]
        bot._yq_backup.update(last=0.0, task=None)

        async def run():
            soon()  # первый раз после старта — сразу
            await asyncio.sleep(0.05)
            soon()  # в окне — одна отложенная
            soon()  # уже ждёт — не плодим
            await asyncio.sleep(0.5)

        asyncio.run(run())
        bot._yq_backup.update(last=0.0, task=None)
        self.assertEqual(len(calls), 2, calls)
        self.assertGreaterEqual(calls[1][0] - calls[0][0], 0.2)
        self.assertEqual(calls[0][1], "очередь YouTube")

    def test_extract_keeps_added(self):
        b = dict(BACKUP, queue=[dict(BACKUP["queue"][0], addedAt=1795000000123)])
        snap = ytqueue.extract(gzip.compress(json.dumps(b).encode()))
        self.assertEqual(snap["items"][0]["added"], 1795000000)


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
        self.assertIn('add_post("/api/yq-sync", handler_api_yq_sync)', src)
        self.assertIn("on_change=_yq_backup_soon", src)
        self.assertIn("ytqueue.register(app", src)
        self.assertIn('elif cmd == "/yt":', inspect.getsource(bot))


if __name__ == "__main__":
    unittest.main()
