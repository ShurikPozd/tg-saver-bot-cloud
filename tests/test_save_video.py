"""POST /api/save-video: видео YouTube из расширения → пост в архиве.
Без сети: Telegram, Groq и i.ytimg.com подменяются. Запуск: python -m unittest discover tests"""

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
import ytvideo  # noqa: E402

SECRET = "test-secret-not-real"
OWNER = 4242
VID = "dQw4w9WgXcQ"


class FakeMsg:
    _next = [100]

    def __init__(self, text="", photo=False):
        FakeMsg._next[0] += 1
        self.id = FakeMsg._next[0]
        self.text = text
        self.photo = _Photo() if photo else None
        self.edits = []

    async def edit(self, text, **kw):
        self.edits.append((text, kw))

    async def delete(self):
        pass


class _Photo:
    id = 777


class FakeClient:
    def __init__(self, connected=True, file_fails=False):
        self.connected = connected
        self.file_fails = file_fails
        self.files, self.messages = [], []

    def is_connected(self):
        return self.connected

    async def send_file(self, chat, f, caption=None, **kw):
        if self.file_fails:
            raise RuntimeError("PHOTO_INVALID")
        m = FakeMsg(caption or "", photo=True)
        self.files.append((chat, getattr(f, "name", None), caption, m))
        return m

    async def send_message(self, chat, text, **kw):
        m = FakeMsg(text)
        self.messages.append((chat, text, kw, m))
        return m


class Base(unittest.TestCase):
    def setUp(self):
        self.tok = db.use_db_path(os.path.join(tempfile.mkdtemp(), "a.db"))
        db.init_db()
        db.create_category("Музыка")
        db.create_category("Игры")
        self.saved = {}
        self.calls = {"categorize": [], "thumb": 0}
        self.client = FakeClient()
        self.stub(bot, "client", self.client)
        self.stub(bot, "EXT_SECRET", SECRET)
        self.stub(bot, "OWNER_ID", str(OWNER))
        self.stub(bot, "_maybe_auto_backup_tg", lambda uid: None)
        self.stub(bot, "_arm_auto_order", lambda *a: None)
        bot._save_video_times.clear()

        async def categorize(text, **kw):
            self.calls["categorize"].append(text)
            return {"category": "Музыка", "summary": "Клип Рика Эстли", "confidence": 0.9,
                    "alternatives": [], "llm_ok": True}

        async def thumb(video_id, timeout=10):
            self.calls["thumb"] += 1
            return b"\xff\xd8" + b"x" * 5000
        self.stub(bot, "categorize", categorize)
        self.stub(ytvideo, "fetch_thumb", thumb)

    def tearDown(self):
        for (obj, name), val in self.saved.items():
            setattr(obj, name, val)
        db.reset_db_path(self.tok)

    def stub(self, obj, name, val):
        self.saved.setdefault((obj, name), getattr(obj, name))
        setattr(obj, name, val)

    def body(self, **kw):
        b = {"videoId": VID, "title": "Rick Astley - Never Gonna Give You Up", "channel": "Rick Astley",
             "len": 213, "tldr": "Клип 1987 года. Певец обещает никогда не бросать."}
        b.update(kw)
        return b

    def post(self, body, secret=SECRET, raw=None):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        async def run():
            app = web.Application()
            app.router.add_post("/api/save-video", bot.handler_api_save_video)
            async with TestClient(TestServer(app)) as cl:
                headers = {"Content-Type": "application/json"}
                if secret is not None:
                    headers["X-Sec-Token"] = secret
                data = raw if raw is not None else json.dumps(body, ensure_ascii=False).encode("utf-8")
                r = await cl.post("/api/save-video", data=data, headers=headers)
                return r.status, await r.json()
        return asyncio.run(run())


class ParseTests(unittest.TestCase):
    def test_valid_and_cleaned(self):
        v = ytvideo.parse_request({"videoId": VID, "title": "  Привет‮\n мир ", "channel": "К" * 500,
                                   "len": "61.9", "tldr": "a\r\n\r\n\r\n\r\nb"})
        self.assertEqual(v["title"], "Привет мир")
        self.assertEqual(len(v["channel"]), ytvideo.CHANNEL_MAX)
        self.assertEqual(v["seconds"], 61)
        self.assertEqual(v["tldr"], "a\n\nb")

    def test_bad_ids(self):
        for bad in ("", "short", "dQw4w9WgXcQx", "../../etc/pa", "dQw4w9WgXc!", None):
            with self.assertRaises(ytvideo.BadRequest):
                ytvideo.parse_request({"videoId": bad})
        with self.assertRaises(ytvideo.BadRequest):
            ytvideo.parse_request(["not", "a", "dict"])

    def test_text_and_caption_limit(self):
        v = ytvideo.parse_request({"videoId": VID, "title": "T", "channel": "C", "len": 3725,
                                   "tldr": "слово " * 600})
        full = ytvideo.post_text(v)
        self.assertIn("https://youtu.be/" + VID, full)
        self.assertIn("📺 C · 1:02:05", full)
        cap = ytvideo.post_text(v, ytvideo.CAPTION_MAX)
        self.assertLessEqual(len(cap), ytvideo.CAPTION_MAX)
        self.assertTrue(cap.endswith("…"))
        self.assertIn("https://youtu.be/" + VID, cap)          # ссылка не срезается
        self.assertLessEqual(len(ytvideo.llm_text(v)), 1400)   # бережём токены Groq

    def test_no_tldr_no_channel(self):
        v = ytvideo.parse_request({"videoId": VID})
        self.assertEqual(ytvideo.post_text(v), f"▶️ Видео YouTube\nhttps://youtu.be/{VID}")
        self.assertEqual(ytvideo.fmt_duration(59), "0:59")


class EndpointTests(Base):
    def test_saves_like_a_post(self):
        st, d = self.post(self.body())
        self.assertEqual(st, 200, d)
        self.assertEqual((d["status"], d["category"], d["queued"]), ("saved", "Музыка", False))
        item = db.get_item(d["id"])
        self.assertEqual(item["category"], "Музыка")
        self.assertEqual(item["content_type"], "photo")          # обложка — превью в веб-архиве
        self.assertEqual(item["source_channel"], "Rick Astley")
        self.assertIn("youtu.be/" + VID, item["original_text"])
        self.assertIn("Певец обещает", item["original_text"])
        # в чат владельца: обложка с подписью, затем «Сохранено в …» с обычными кнопками
        chat, name, caption, post = self.client.files[0]
        self.assertEqual((chat, name), (OWNER, "cover.jpg"))
        self.assertEqual(item["message_id"], post.id)
        proc = self.client.messages[-1][3]
        self.assertEqual(self.client.messages[-1][2].get("reply_to"), post.id)
        text, kw = proc.edits[-1]
        self.assertIn("Сохранено в «Музыка»", text)
        self.assertTrue(kw.get("buttons"))
        # ИИ получил короткую подсказку ровно один раз
        self.assertEqual(len(self.calls["categorize"]), 1)
        self.assertIn("Название: Rick Astley", self.calls["categorize"][0])

    def test_repeat_is_not_saved_twice(self):
        self.post(self.body())
        st, d = self.post(self.body(title="другое название"))
        self.assertEqual((st, d["status"], d["category"]), (200, "exists", "Музыка"))
        self.assertEqual(len(self.calls["categorize"]), 1)
        self.assertEqual(len(self.client.files), 1)

    def test_link_saved_by_hand_counts_as_repeat(self):
        iid = db.save_item(category="Игры", content_type="text", summary="ссылка",
                           original_text=f"смотри https://www.youtube.com/watch?v={VID}&t=5",
                           file_id=None, message_id=1, chat_id=1)
        st, d = self.post(self.body())
        self.assertEqual((d["status"], d["id"]), ("exists", iid))
        self.assertEqual(self.client.files, [])

    def test_ai_down_goes_to_video_and_retry_queue(self):
        async def down(text, **kw):
            return {"llm_ok": False}
        self.stub(bot, "categorize", down)
        st, d = self.post(self.body())
        self.assertEqual((st, d["category"], d["queued"]), (200, "Видео", True))
        self.assertIn(str(d["id"]), db.retry_queue_items())

    def test_no_thumbnail_falls_back_to_text(self):
        async def none(video_id, timeout=10):
            return None
        self.stub(ytvideo, "fetch_thumb", none)
        st, d = self.post(self.body())
        self.assertEqual(st, 200)
        self.assertEqual(db.get_item(d["id"])["content_type"], "text")
        self.assertEqual(self.client.messages[0][2].get("link_preview"), True)

    def test_photo_upload_error_falls_back_to_text(self):
        self.client.file_fails = True
        st, d = self.post(self.body())
        self.assertEqual((st, d["status"]), (200, "saved"))
        self.assertEqual(db.get_item(d["id"])["content_type"], "text")

    def test_auth_and_validation(self):
        self.assertEqual(self.post(self.body(), secret=None)[0], 403)
        self.assertEqual(self.post(self.body(), secret="wrong")[0], 403)
        self.assertEqual(self.post(self.body(), secret="кириллица")[0], 403)
        self.stub(bot, "EXT_SECRET", "")
        self.assertEqual(self.post(self.body())[0], 403)               # секрет не задан — закрыто
        self.stub(bot, "EXT_SECRET", SECRET)
        self.assertEqual(self.post(self.body(videoId="bad"))[0], 400)
        self.assertEqual(self.post(None, raw=b"{not json")[0], 400)
        big = self.body(tldr="я" * 20000)
        self.assertEqual(self.post(big)[0], 413)
        self.assertEqual(self.client.files, [])

    def test_body_read_whole_and_limited_without_length(self):
        # без Content-Length (chunked): тело читается целиком, но не больше лимита
        payload = json.dumps(self.body(tldr="ж" * 2000), ensure_ascii=False).encode("utf-8")

        def chunks(data):
            async def gen():
                for i in range(0, len(data), 700):
                    yield data[i:i + 700]
            return gen()
        st, d = self.post(None, raw=chunks(payload))
        self.assertEqual((st, d["status"]), (200, "saved"))
        self.assertIn("ж" * 1500, db.get_item(d["id"])["original_text"])
        st, _ = self.post(None, raw=chunks(b"{" + b" " * (ytvideo.MAX_BODY + 10) + b"}"))
        self.assertEqual(st, 413)

    def test_offline_and_no_owner(self):
        self.client.connected = False
        st, d = self.post(self.body())
        self.assertEqual((st, d["code"]), (503, "tg_offline"))
        self.client.connected = True
        self.stub(bot, "OWNER_ID", "")
        self.stub(db, "legacy_owner_id", lambda: None)
        st, d = self.post(self.body())
        self.assertEqual((st, d["code"]), (503, "no_owner"))

    def test_rate_limit(self):
        self.stub(bot, "SAVE_VIDEO_RATE", (2, 600))
        ids = ["aaaaaaaaaa1", "aaaaaaaaaa2", "aaaaaaaaaa3"]
        codes = [self.post(self.body(videoId=i))[0] for i in ids]
        self.assertEqual(codes, [200, 200, 429])
        # повтор уже сохранённого лимит не тратит
        self.assertEqual(self.post(self.body(videoId=ids[0]))[1]["status"], "exists")

    def test_route_registered(self):
        import inspect
        src = inspect.getsource(bot.health_http)
        self.assertIn('add_post("/api/save-video", handler_api_save_video)', src)


if __name__ == "__main__":
    unittest.main()
