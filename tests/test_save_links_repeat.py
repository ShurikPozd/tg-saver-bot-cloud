"""POST /api/save-links: половинки эмодзи в запросе и повтор того же поста-списка.
Без сети: Telegram и Groq подменяются (как в test_save_links). Запуск: python -m unittest discover tests"""

import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import bot  # noqa: E402
import database as db  # noqa: E402
import savelinks  # noqa: E402
from test_save_links import Base, link  # noqa: E402


class SurrogateTests(Base):
    """Одиночная половинка эмодзи (старое расширение резало строку посреди эмодзи):
    ссылка сохраняется, а не роняет бота на отправке в Telegram / записи в SQLite."""

    def raw(self, body):
        # json.dumps с ensure_ascii пишет половинку как \ud83d — ровно как браузер
        return self.post(None, raw=json.dumps(body).encode("ascii"))

    def test_parse_strips_lone_surrogates(self):
        it = savelinks.parse_item({"url": "https://example.com/a\ud83d", "title": "Фрирен \ud83d",
                                   "meta": "\udc00мета", "description": "опис\ud83dание",
                                   "icon": "\ud83c", "site": "ex\udfffample.com", "kindLabel": "\ud800Аниме",
                                   "image": "https://example.com/i.jpg\udc00"})
        self.assertEqual((it["url"], it["title"], it["meta"], it["description"], it["icon"], it["site"],
                          it["kind_label"], it["image"]),
                         ("https://example.com/a", "Фрирен", "мета", "описание", "", "example.com", "Аниме",
                          "https://example.com/i.jpg"))
        ok = savelinks.parse_item({"url": "https://example.com/b", "title": "Смайл 😀"})
        self.assertEqual(ok["title"], "Смайл 😀")                  # целые эмодзи не трогаем
        req = savelinks.parse_request({"mode": "list", "title": "Вкладки \ud83d", "batchKey": "k\udc00ey",
                                       "items": [{"url": "https://example.com/c"}]})
        self.assertEqual((req["title"], req["batch_key"]), ("Вкладки", "key"))
        req = savelinks.parse_request({"mode": "each", "items": [{"url": "ftp://x\ud83d"}]})
        self.assertEqual(req["raw_urls"], ["ftp://x"])
        for s in (it["url"], it["title"], req["raw_urls"][0]):
            s.encode("utf-8")                                     # не бросает

    def test_each_and_list_with_surrogates_saved(self):
        st, d = self.raw({"mode": "each", "items": [link(title="Фрирен \ud83d", description="конец\ud83d")]})
        self.assertEqual((st, d["results"][0]["status"]), (200, "saved"), d)
        st, d = self.raw({"mode": "list", "title": "Вкладки \ud83d",
                          "items": [link(url="https://example.com/x", title="X \ud83d")]})
        self.assertEqual((st, d["status"]), (200, "saved"), d)
        self.assertTrue(db.get_item(d["ids"][0])["original_text"].startswith("🔗 Вкладки\n"))
        for _chat, text, _kw, _m in self.client.messages:
            text.encode("utf-8")
        for _chat, _name, caption, _m in self.client.files:
            (caption or "").encode("utf-8")


class ListRepeatTests(Base):
    """Повтор того же mode=list (браузер упал после ответа бота) не даёт второго поста."""

    def items(self, n=3, base="https://example.com/r/"):
        return [link(url=f"{base}{i}", title=f"Page {i}", description="") for i in range(n)]

    def posts(self):
        return [m for m in self.client.messages if m[1].startswith("🔗")]

    def shift_seen(self, sec):
        rows = json.loads(db.get_setting(bot._LIST_SEEN_KEY))
        for r in rows:
            r["t"] -= sec
        db.set_setting(bot._LIST_SEEN_KEY, json.dumps(rows))

    def test_batch_key_repeat_returns_same_ids(self):
        body = {"mode": "list", "title": "Сбор", "batchKey": "b-1", "items": self.items()}
        st, d = self.post(body)
        self.assertEqual((st, d["status"]), (200, "saved"))
        st, d2 = self.post(body)
        self.assertEqual((st, d2["ok"], d2["mode"], d2["status"], d2["ids"], d2["parts"], d2["category"],
                          d2["icon"], d2["queued"]),
                         (200, True, "list", "exists", d["ids"], d["parts"], d["category"], d["icon"], False))
        self.assertEqual(len(self.posts()), 1)
        self.assertEqual(len(self.calls["categorize"]), 1)
        # другой ключ — это другой сбор, даже с теми же ссылками
        st, d3 = self.post({**body, "batchKey": "b-2"})
        self.assertEqual(d3["status"], "saved")
        self.assertEqual(len(self.posts()), 2)

    def test_batch_key_with_other_links_is_new(self):
        self.post({"mode": "list", "batchKey": "same", "items": self.items()})
        st, d = self.post({"mode": "list", "batchKey": "same", "items": self.items(base="https://example.com/z/")})
        self.assertEqual(d["status"], "saved")
        self.assertEqual(len(self.posts()), 2)

    def test_without_key_same_links_in_10_minutes(self):
        items = self.items()
        d = self.post({"mode": "list", "title": "Сбор", "items": items})[1]
        # тот же набор в другом порядке и с метками utm — тот же список
        again = [dict(x, url=x["url"] + "?utm_source=tab") for x in reversed(items)]
        d2 = self.post({"mode": "list", "title": "Сбор", "items": again})[1]
        self.assertEqual((d2["status"], d2["ids"]), ("exists", d["ids"]))
        # другой заголовок — другой список
        self.assertEqual(self.post({"mode": "list", "title": "Другой", "items": items})[1]["status"], "saved")
        # через 10 минут без ключа — снова новый пост
        self.shift_seen(bot.SAVE_LINKS_SAME_LIST_TTL + 1)
        self.assertEqual(self.post({"mode": "list", "title": "Сбор", "items": items})[1]["status"], "saved")
        self.assertEqual(len(self.posts()), 3)

    def test_batch_key_kept_a_day_and_deleted_posts_saved_again(self):
        body = {"mode": "list", "batchKey": "day", "items": self.items()}
        d = self.post(body)[1]
        self.shift_seen(5 * 3600)
        self.assertEqual(self.post(body)[1]["status"], "exists")
        for i in d["ids"]:
            db.trash_item(i)
        self.assertEqual(self.post(body)[1]["status"], "saved")     # посты удалены — сохраняем заново
        self.shift_seen(bot.SAVE_LINKS_BATCH_TTL + 1)
        self.assertEqual(self.post(body)[1]["status"], "saved")     # больше суток — ключ забыт
        self.assertEqual(len(self.posts()), 3)

    def test_repeat_not_counted_in_rate(self):
        self.stub(bot, "SAVE_LINKS_RATE", (1, 600))
        body = {"mode": "list", "batchKey": "r", "items": self.items()}
        self.assertEqual(self.post(body)[1]["status"], "saved")
        st, d = self.post(body)
        self.assertEqual((st, d["status"]), (200, "exists"))
        st, d = self.post({**body, "batchKey": "r2"})
        self.assertEqual((st, d["code"]), (429, "rate"))

    def test_seen_list_is_bounded_and_survives_junk(self):
        n = bot._LIST_SEEN_MAX + 5
        for i in range(3):
            bot._list_remember(f"old{i}", "f", 1e9, {"ids": [i], "category": "C", "parts": 1, "queued": False})
        now = time.time()
        bot._list_remember("new", "f", now, {"ids": [7], "category": "C", "parts": 1, "queued": False})
        self.assertEqual(json.loads(db.get_setting(bot._LIST_SEEN_KEY)), [{
            "t": now, "k": "new", "f": "f", "ids": [7], "category": "C", "parts": 1,
            "queued": False, "unsent": 0}])                             # старше суток — выкинуты
        for i in range(n):
            bot._list_remember(f"k{i}", f"f{i}", now, {"ids": [i], "category": "C", "parts": 1, "queued": False})
        rows = json.loads(db.get_setting(bot._LIST_SEEN_KEY))
        self.assertEqual(len(rows), bot._LIST_SEEN_MAX)
        self.assertEqual(rows[-1]["k"], f"k{n - 1}")
        for junk in ("{broken", '{"a": 1}', '[1, "x", {"t": "nan?"}]'):
            db.set_setting(bot._LIST_SEEN_KEY, junk)
            self.assertEqual(bot._list_seen(now), [])
        st, d = self.post({"mode": "list", "items": self.items()})
        self.assertEqual((st, d["status"]), (200, "saved"))

    def test_failure_after_parts_sent_is_200_not_500(self):
        """Части уже в чате — сбой сохранения не даёт 500 (иначе расширение пришлёт список снова)."""
        async def boom(*a, **kw):
            raise RuntimeError("AI layer crashed")
        self.stub(bot, "_save", boom)
        real_save_item = db.save_item
        calls = {"n": 0}

        def flaky_save_item(**kw):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("disk I/O error")
            return real_save_item(**kw)
        self.stub(db, "save_item", flaky_save_item)
        items = [link(url=f"https://example.com/p/{i}", title="t" * 200, description="d" * 140)
                 for i in range(60)]
        st, d = self.post({"mode": "list", "batchKey": "fail", "items": items})
        self.assertEqual((st, d["ok"], d["status"], d["queued"], d["unsaved"]), (200, True, "saved", True, 1), d)
        self.assertGreater(d["parts"], 1)
        self.assertEqual(len(d["ids"]), d["parts"] - 1)
        self.assertNotIn("unsent", d)
        for i in d["ids"]:
            self.assertIn(str(i), db.retry_queue_items())            # без ИИ — в очередь дораспознавания
        n_posts = len(self.posts())
        self.assertEqual(n_posts, d["parts"])
        st, d2 = self.post({"mode": "list", "batchKey": "fail", "items": items})
        self.assertEqual((st, d2["status"], d2["ids"]), (200, "exists", d["ids"]))
        self.assertEqual(len(self.posts()), n_posts)

    def test_save_crash_after_item_written_does_not_duplicate(self):
        real = bot._save

        async def save_then_crash(*a, **kw):
            await real(*a, **kw)
            raise RuntimeError("crash after save_item")
        self.stub(bot, "_save", save_then_crash)
        st, d = self.post({"mode": "list", "items": self.items()})
        self.assertEqual((st, d["status"], len(d["ids"])), (200, "saved", 1), d)
        self.assertEqual(len(db.items_with_text_fragments(["https://example.com/r/0"])), 1)

    def test_all_parts_unsent_still_502_and_retry_allowed(self):
        async def down(chat, text, **kw):
            raise RuntimeError("FLOOD")
        self.client.send_message = down
        self.stub(bot, "SAVE_LINKS_RESEND_PAUSE", 0)
        body = {"mode": "list", "batchKey": "k", "items": self.items()}
        st, d = self.post(body)
        self.assertEqual((st, d["code"]), (502, "tg_send"))
        self.assertEqual(db.get_setting(bot._LIST_SEEN_KEY, ""), "")   # ничего не ушло — повтор не «exists»


if __name__ == "__main__":
    unittest.main()
