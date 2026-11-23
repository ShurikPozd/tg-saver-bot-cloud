"""POST /api/save-links: ссылки из «Сборщика вкладок» → посты архива.
Без сети: Telegram, Groq и загрузка картинок подменяются. Запуск: python -m unittest discover tests"""

import asyncio
import io
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
import savelinks  # noqa: E402

from test_save_video import FakeClient, SECRET, OWNER  # noqa: E402

FRIEREN = "https://shikimori.one/animes/52991-sousou-no-frieren"


def link(url=FRIEREN, **kw):
    it = {"url": url, "site": "shikimori.one", "kind": "anime", "kindLabel": "Аниме", "icon": "🎬",
          "title": "Фрирен, провожающая в последний путь",
          "meta": "2023 · TV, 28 эп. · ★ 9.1 · приключения, фэнтези, драма",
          "description": "Эльфийка-маг после победы над Королём демонов отправляется в путь."}
    it.update(kw)
    return it


class Base(unittest.TestCase):
    def setUp(self):
        self.tok = db.use_db_path(os.path.join(tempfile.mkdtemp(), "a.db"))
        db.init_db()
        self.saved = {}
        self.calls = {"categorize": [], "image": []}
        self.client = FakeClient()
        self.stub(bot, "client", self.client)
        self.stub(bot, "EXT_SECRET", SECRET)
        self.stub(bot, "OWNER_ID", str(OWNER))
        self.stub(bot, "_maybe_auto_backup_tg", lambda uid: None)
        self.stub(bot, "_arm_auto_order", lambda *a: None)
        bot._save_links_times.clear()

        async def categorize(text, **kw):
            self.calls["categorize"].append(text)
            return {"category": "Аниме", "summary": "Аниме Фрирен", "confidence": 0.9,
                    "alternatives": [], "llm_ok": True}

        async def image(url, timeout=10):
            self.calls["image"].append(url)
            return b"\xff\xd8" + b"x" * 5000
        self.stub(bot, "categorize", categorize)
        self.stub(savelinks, "fetch_image", image)

    def tearDown(self):
        for (obj, name), val in self.saved.items():
            setattr(obj, name, val)
        db.reset_db_path(self.tok)

    def stub(self, obj, name, val):
        self.saved.setdefault((obj, name), getattr(obj, name))
        setattr(obj, name, val)

    def post(self, body, secret=SECRET, raw=None, method="POST"):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        async def run():
            app = web.Application()
            app.router.add_post("/api/save-links", bot.handler_api_save_links)
            async with TestClient(TestServer(app)) as cl:
                headers = {"Content-Type": "application/json"}
                if secret is not None:
                    headers["X-Sec-Token"] = secret
                data = raw if raw is not None else json.dumps(body, ensure_ascii=False).encode("utf-8")
                r = await cl.post("/api/save-links", data=data, headers=headers)
                return r.status, await r.json(), r.headers
        st, d, h = asyncio.run(run())
        self.assertEqual(h.get("Access-Control-Allow-Origin"), "*")
        return st, d

    def each(self, *items, **kw):
        return self.post({"mode": "each", "items": list(items)}, **kw)


class ParseTests(unittest.TestCase):
    def test_item_cleaned_and_text_format(self):
        it = savelinks.parse_item(link(title="  Фрирен‮\n ", meta="М" * 500, description="a\r\n\r\n\r\nb",
                                       image="http://insecure/x.jpg", extra="игнор"))
        self.assertEqual(it["title"], "Фрирен")
        self.assertEqual(len(it["meta"]), savelinks.META_MAX)
        self.assertEqual(it["description"], "a\n\nb")
        self.assertEqual(it["image"], "")                       # только https
        t = savelinks.post_text(savelinks.parse_item(link()))
        self.assertEqual(t.split("\n"), [
            "🎬 Аниме · Фрирен, провожающая в последний путь",
            "2023 · TV, 28 эп. · ★ 9.1 · приключения, фэнтези, драма",
            "Эльфийка-маг после победы над Королём демонов отправляется в путь.",
            FRIEREN])

    def test_empty_lines_skipped_and_title_fallback(self):
        it = savelinks.parse_item({"url": "https://example.com/a"})
        self.assertEqual(savelinks.post_text(it), "https://example.com/a\nhttps://example.com/a")
        it = savelinks.parse_item({"url": "https://example.com/a", "title": "T", "kindLabel": "Страница"})
        self.assertEqual(savelinks.post_text(it), "Страница · T\nhttps://example.com/a")

    def test_bad_urls(self):
        for bad in ("", "javascript:alert(1)", "ftp://x/y", "https://", "https://a b", "x" * 3000, None, 5):
            self.assertIsNone(savelinks.parse_item({"url": bad}), bad)
        self.assertIsNone(savelinks.parse_item("not a dict"))

    def test_request_limits(self):
        with self.assertRaises(savelinks.BadRequest):
            savelinks.parse_request({"mode": "both", "items": [link()]})
        with self.assertRaises(savelinks.BadRequest):
            savelinks.parse_request({"mode": "each", "items": []})
        with self.assertRaises(savelinks.BadRequest):
            savelinks.parse_request({"mode": "each", "items": [link()] * 11})
        with self.assertRaises(savelinks.BadRequest):
            savelinks.parse_request({"mode": "list", "items": [link()] * 101})
        with self.assertRaises(savelinks.BadRequest):
            savelinks.parse_request({"mode": "list", "items": [{"url": "bad"}]})
        r = savelinks.parse_request({"mode": "list", "title": "Т" * 300, "items": [link(), {"url": "bad"}]})
        self.assertEqual(len(r["title"]), savelinks.LIST_TITLE_MAX)
        self.assertIsNone(r["items"][1])

    def test_caption_keeps_url(self):
        it = savelinks.parse_item(link(description="слово " * 200, meta="м" * 300, title="т" * 300))
        cap = savelinks.post_text(it, savelinks.CAPTION_MAX)
        self.assertLessEqual(len(cap), savelinks.CAPTION_MAX)
        self.assertTrue(cap.endswith(FRIEREN))
        self.assertIn("…", cap)
        long_url = "https://example.com/" + "a" * 1500
        self.assertIsNone(savelinks.post_text(savelinks.parse_item(link(url=long_url)), savelinks.CAPTION_MAX))
        self.assertLessEqual(len(savelinks.llm_text(it)), 1200)          # бережём токены Groq

    def test_url_key(self):
        k = savelinks.url_key
        self.assertEqual(k(FRIEREN), k("https://shikimori.me/animes/z52991"))
        self.assertEqual(k(FRIEREN), k("http://www.shikimori.one/animes/52991-sousou-no-frieren/"))
        self.assertNotEqual(k(FRIEREN), k("https://shikimori.one/animes/529910"))
        self.assertNotEqual(k(FRIEREN), k("https://shikimori.one/mangas/52991"))
        self.assertEqual(k("https://youtu.be/dQw4w9WgXcQ"), k("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=5"))
        self.assertEqual(k("https://Example.com/a/?utm_source=x&b=2&a=1#top"), k("https://example.com/a?a=1&b=2"))
        self.assertNotEqual(k("https://example.com/a?id=1"), k("https://example.com/a?id=2"))
        self.assertEqual(k("https://ru.wikipedia.org/wiki/%D0%90%D0%BD%D0%B8%D0%BC%D0%B5"),
                         k("https://ru.wikipedia.org/wiki/Аниме"))

    def test_list_split_by_links(self):
        items = [savelinks.parse_item(link(url=f"https://example.com/{i}", description="о" * 600))
                 for i in range(100)]
        parts = savelinks.list_parts("Вкладки", items)
        self.assertGreater(len(parts), 1)
        joined = "\n".join(parts)
        for i in range(100):
            self.assertEqual(joined.count(f"https://example.com/{i}\n") + joined.endswith(f"/{i}"), 1)
        for k, p in enumerate(parts, 1):
            self.assertLessEqual(len(p), savelinks.MSG_MAX)
            self.assertTrue(p.startswith(f"🔗 Вкладки · часть {k}/{len(parts)}\n100 ссылок\n\n"))
        self.assertIn("о" * 139 + "…", parts[0])                        # описание — ~140 символов
        one = savelinks.list_parts("", items[:2])
        self.assertEqual(len(one), 1)
        self.assertTrue(one[0].startswith("🔗 Список ссылок\n2 ссылки\n\n"))
        self.assertEqual([savelinks.links_word(n) for n in (1, 3, 5, 11, 21, 112)],
                         ["1 ссылка", "3 ссылки", "5 ссылок", "11 ссылок", "21 ссылка", "112 ссылок"])

    def test_default_category(self):
        anime = [savelinks.parse_item(link())]
        page = [savelinks.parse_item({"url": "https://habr.com/ru/articles/1/", "kind": "article"})]
        self.assertEqual(savelinks.default_category(anime, ["Игры", "Аниме и манга"]), "Аниме и манга")
        self.assertEqual(savelinks.default_category(anime, ["Игры"]), "Аниме")
        self.assertEqual(savelinks.default_category(page, ["Игры"]), "Ссылки")
        self.assertEqual(savelinks.default_category(page, ["Ссылки и закладки"]), "Ссылки и закладки")
        vid = [savelinks.parse_item({"url": "https://youtu.be/dQw4w9WgXcQ", "kind": "video"})]
        self.assertEqual(savelinks.default_category(vid, []), "Видео")
        self.assertEqual(savelinks.default_category(anime + page, ["Аниме"]), "Ссылки")

    def test_image_only_public_https(self):
        async def run():
            self.assertFalse(await savelinks._host_is_public("localhost"))
            self.assertFalse(await savelinks._host_is_public("127.0.0.1"))
            self.assertFalse(await savelinks._host_is_public("10.0.0.5"))
            self.assertFalse(await savelinks._host_is_public("[::1]"))
            self.assertFalse(await savelinks._host_is_public("169.254.169.254"))
            self.assertTrue(await savelinks._host_is_public("8.8.8.8"))
            self.assertIsNone(await savelinks.fetch_image("http://8.8.8.8/x.jpg"))
            self.assertIsNone(await savelinks.fetch_image("https://127.0.0.1/x.jpg"))
        asyncio.run(run())

    def test_photo_bytes(self):
        from PIL import Image

        def img(fmt, size=(64, 48)):
            b = io.BytesIO()
            Image.new("RGB", size, "red").save(b, fmt)
            return b.getvalue()
        jpg = img("JPEG")
        self.assertEqual(savelinks._photo_bytes(jpg), jpg)                # как есть
        webp = savelinks._photo_bytes(img("WEBP"))
        self.assertTrue(webp.startswith(b"\xff\xd8"))                     # → JPEG
        self.assertTrue(savelinks._photo_bytes(img("PNG", (3000, 100))).startswith(b"\xff\xd8"))
        self.assertIsNone(savelinks._photo_bytes(b"<html>not an image</html>"))


class EachTests(Base):
    def test_saves_like_a_post_with_cover(self):
        st, d = self.each(link(image="https://shikimori.one/system/animes/original/52991.jpg"))
        self.assertEqual(st, 200, d)
        self.assertEqual((d["ok"], d["mode"]), (True, "each"))
        r = d["results"][0]
        self.assertEqual((r["url"], r["status"], r["category"], r["queued"]), (FRIEREN, "saved", "Аниме", False))
        self.assertTrue(r["icon"])
        item = db.get_item(r["id"])
        self.assertEqual(item["content_type"], "photo")
        self.assertEqual(item["source_channel"], "shikimori.one")
        self.assertIn(FRIEREN, item["original_text"])
        self.assertIn("Эльфийка-маг", item["original_text"])
        chat, name, caption, post = self.client.files[0]
        self.assertEqual((chat, name), (OWNER, "cover.jpg"))
        self.assertTrue(caption.endswith(FRIEREN))
        self.assertEqual(item["message_id"], post.id)
        text, kw = self.client.messages[-1][3].edits[-1]
        self.assertIn("Сохранено в «Аниме»", text)
        self.assertTrue(kw.get("buttons"))
        self.assertEqual(len(self.calls["categorize"]), 1)
        self.assertIn("Название: Фрирен", self.calls["categorize"][0])

    def test_no_image_is_text_with_preview(self):
        st, d = self.each(link())
        self.assertEqual(self.calls["image"], [])
        item = db.get_item(d["results"][0]["id"])
        self.assertEqual(item["content_type"], "text")
        self.assertEqual(self.client.messages[0][2].get("link_preview"), True)

    def test_image_failures_fall_back_to_text(self):
        async def none(url, timeout=10):
            return None
        self.stub(savelinks, "fetch_image", none)
        st, d = self.each(link(image="https://example.com/x.jpg"))
        self.assertEqual(db.get_item(d["results"][0]["id"])["content_type"], "text")
        self.client.file_fails = True

        async def some(url, timeout=10):
            return b"\xff\xd8" + b"x" * 5000
        self.stub(savelinks, "fetch_image", some)
        st, d = self.each(link(url="https://example.com/other", image="https://example.com/x.jpg"))
        self.assertEqual((st, d["results"][0]["status"]), (200, "saved"))
        self.assertEqual(db.get_item(d["results"][0]["id"])["content_type"], "text")

    def test_repeats_without_ai_and_message(self):
        self.each(link())
        n_msgs = len(self.client.messages)
        st, d = self.each(link(url="https://shikimori.me/animes/z52991", title="другое"),
                          link(url="https://example.com/new"), link(url="https://example.com/new/"))
        self.assertEqual([r["status"] for r in d["results"]], ["exists", "saved", "exists"])
        self.assertEqual(d["results"][0]["url"], "https://shikimori.me/animes/z52991")
        self.assertEqual(d["results"][0]["category"], "Аниме")
        self.assertEqual(len(self.calls["categorize"]), 2)
        self.assertEqual(len(self.client.messages), n_msgs + 2)          # только новая ссылка + «⏳»

    def test_link_inside_old_post_counts_and_prefix_does_not(self):
        iid = db.save_item(category="Игры", content_type="text", summary="ссылка",
                           original_text="смотри https://example.com/a/b и https://example.com/q?id=1).",
                           file_id=None, message_id=1, chat_id=1)
        st, d = self.each(link(url="https://example.com/a"), link(url="https://example.com/q?id=1"),
                          link(url="https://example.com/q?id=2"))
        self.assertEqual([r["status"] for r in d["results"]], ["saved", "exists", "saved"])
        self.assertEqual(d["results"][1]["id"], iid)

    def test_bad_item_is_error_others_saved(self):
        st, d = self.each({"url": "javascript:void(0)"}, link())
        self.assertEqual(st, 200)
        self.assertEqual([(r["url"], r["status"], r.get("code")) for r in d["results"]],
                         [("javascript:void(0)", "error", "bad_url"), (FRIEREN, "saved", None)])

    def test_ai_down_goes_to_default_and_retry_queue(self):
        async def down(text, **kw):
            return {"llm_ok": False}
        self.stub(bot, "categorize", down)
        db.create_category("Аниме и манга")
        db.save_item(category="Аниме и манга", content_type="text", summary="x", original_text="x",
                     file_id=None, message_id=2, chat_id=1)
        st, d = self.each(link(), link(url="https://habr.com/ru/articles/1/", site="habr.com", kind="article",
                                            kindLabel="Статья"))
        r1, r2 = d["results"]
        self.assertEqual((r1["category"], r1["queued"]), ("Аниме и манга", True))
        self.assertEqual((r2["category"], r2["queued"]), ("Ссылки", True))
        q = db.retry_queue_items()
        self.assertIn(str(r1["id"]), q)
        self.assertIn(str(r2["id"]), q)

    def test_rate_limit(self):
        self.stub(bot, "SAVE_LINKS_RATE", (2, 600))
        urls = [f"https://example.com/{i}" for i in range(4)]
        st, d = self.each(*[link(url=u) for u in urls[:3]])
        self.assertEqual(st, 200)
        self.assertEqual([r["status"] for r in d["results"]], ["saved", "saved", "error"])
        self.assertEqual(d["results"][2]["code"], "rate")
        self.assertGreater(d["retryAfter"], 500)
        # лимит исчерпан и ничего не сделано — весь запрос 429
        st, d = self.each(link(url=urls[3]))
        self.assertEqual((st, d["code"]), (429, "rate"))
        self.assertGreater(d["retryAfter"], 500)
        # повторы лимит не тратят
        st, d = self.each(link(url=urls[0]), link(url=urls[3]))
        self.assertEqual(st, 200)
        self.assertEqual([r["status"] for r in d["results"]], ["exists", "error"])

    def test_day_limit(self):
        self.stub(bot, "SAVE_LINKS_DAY", (1, 86400))
        self.each(link())
        st, d = self.each(link(url="https://example.com/x"))
        self.assertEqual(st, 429)
        self.assertGreater(d["retryAfter"], 86000)


class ListTests(Base):
    def test_one_post_list(self):
        items = [link(), link(url="https://shikimori.one/animes/21-one-piece", title="Ван-Пис",
                              description="")]
        st, d = self.post({"mode": "list", "title": "Вкладки shikimori.one", "items": items})
        self.assertEqual(st, 200, d)
        self.assertEqual((d["mode"], d["status"], d["parts"], d["queued"], d["category"]),
                         ("list", "saved", 1, False, "Аниме"))
        self.assertEqual(len(d["ids"]), 1)
        text = self.client.messages[0][1]
        self.assertTrue(text.startswith("🔗 Вкладки shikimori.one\n2 ссылки\n\n🎬 Аниме · Фрирен"))
        self.assertIn("\n\n🎬 Аниме · Ван-Пис", text)
        self.assertEqual(self.client.messages[0][2].get("link_preview"), False)
        item = db.get_item(d["ids"][0])
        self.assertEqual((item["original_text"], item["source_channel"]), (text, "shikimori.one"))
        self.assertEqual(len(self.calls["categorize"]), 1)
        self.assertIn("Список ссылок (2 ссылки): Вкладки shikimori.one", self.calls["categorize"][0])
        # ссылка из списка — уже в архиве для mode=each
        st, d = self.each(link(url="https://shikimori.one/animes/21"))
        self.assertEqual(d["results"][0]["status"], "exists")

    def test_long_list_split_into_parts_all_saved(self):
        items = [link(url=f"https://example.com/p/{i}", title=f"Page {i} " + "t" * 150, meta="m" * 150,
                      description="d" * 140) for i in range(100)]
        st, d = self.post({"mode": "list", "items": items})
        self.assertEqual(st, 200, d)
        self.assertGreater(d["parts"], 1)
        self.assertEqual(len(d["ids"]), d["parts"])
        self.assertEqual(len(self.calls["categorize"]), 1)                # ИИ — один раз на весь список
        texts = [db.get_item(i)["original_text"] for i in d["ids"]]
        cats = {db.get_item(i)["category"] for i in d["ids"]}
        self.assertEqual(cats, {"Аниме"})
        for i in range(100):
            hits = [t for t in texts if f"https://example.com/p/{i}\n" in t + "\n"]
            self.assertEqual(len(hits), 1, i)
        posts = [m for m in self.client.messages if m[1].startswith("🔗")]
        self.assertEqual([db.get_item(i)["message_id"] for i in d["ids"]], [m[3].id for m in posts])
        found = bot.savelinks.find_saved("https://example.com/p/99", db.items_with_text_fragments)
        self.assertEqual(found["id"], d["ids"][-1])

    def test_unsent_part_still_saved(self):
        real = self.client.send_message

        async def flaky(chat, text, **kw):
            if text.startswith("🔗") and "часть 2/" in text:
                raise RuntimeError("FLOOD")
            return await real(chat, text, **kw)
        self.client.send_message = flaky
        self.stub(bot, "SAVE_LINKS_RESEND_PAUSE", 0)
        items = [link(url=f"https://example.com/p/{i}", title="t" * 200, description="d" * 140)
                 for i in range(60)]
        st, d = self.post({"mode": "list", "items": items})
        self.assertEqual((st, d["unsent"]), (200, 1))
        self.assertEqual(len(d["ids"]), d["parts"])
        second = db.get_item(d["ids"][1])
        self.assertIsNone(second["message_id"])
        self.assertIn("часть 2/", second["original_text"])

    def test_ai_down_all_parts_queued(self):
        async def down(text, **kw):
            return {"llm_ok": False}
        self.stub(bot, "categorize", down)
        items = [link(url=f"https://example.com/p/{i}", title="t" * 200, description="d" * 140)
                 for i in range(60)]
        st, d = self.post({"mode": "list", "items": items})
        self.assertEqual((st, d["queued"]), (200, True))
        q = db.retry_queue_items()
        for i in d["ids"]:
            self.assertIn(str(i), q)

    def test_list_counts_once_in_rate(self):
        self.stub(bot, "SAVE_LINKS_RATE", (1, 600))
        items = [link(url=f"https://example.com/{i}") for i in range(50)]
        self.assertEqual(self.post({"mode": "list", "items": items})[0], 200)
        st, d = self.post({"mode": "list", "items": items})
        self.assertEqual((st, d["code"]), (429, "rate"))


class HttpTests(Base):
    def test_auth_validation_and_size(self):
        self.assertEqual(self.each(link(), secret=None)[0], 403)
        self.assertEqual(self.each(link(), secret="wrong")[0], 403)
        self.assertEqual(self.each(link(), secret="кириллица")[0], 403)
        self.stub(bot, "EXT_SECRET", "")
        self.assertEqual(self.each(link())[0], 403)
        self.stub(bot, "EXT_SECRET", SECRET)
        self.assertEqual(self.post({"mode": "each", "items": [link()] * 11})[1]["code"], "bad_request")
        self.assertEqual(self.post(None, raw=b"{not json")[0], 400)
        self.assertEqual(self.post({"mode": "nope", "items": [link()]})[0], 400)
        big = {"mode": "list", "items": [link(url=f"https://example.com/{i}", description="я" * 600)
                                         for i in range(100)]}
        st, d = self.post(big)
        self.assertEqual((st, d["code"]), (413, "too_large"))
        self.assertEqual(self.client.messages, [])

    def test_offline_and_no_owner(self):
        self.client.connected = False
        st, d = self.each(link())
        self.assertEqual((st, d["code"]), (503, "tg_offline"))
        st, d = self.post({"mode": "list", "items": [link()]})
        self.assertEqual((st, d["code"]), (503, "tg_offline"))
        self.client.connected = True
        self.stub(bot, "OWNER_ID", "")
        self.stub(db, "legacy_owner_id", lambda: None)
        st, d = self.each(link())
        self.assertEqual((st, d["code"]), (503, "no_owner"))

    def test_route_registered(self):
        import inspect
        src = inspect.getsource(bot.health_http)
        self.assertIn('add_post("/api/save-links", handler_api_save_links)', src)
        self.assertIn('add_options("/api/save-links", handler_options)', src)


if __name__ == "__main__":
    unittest.main()
