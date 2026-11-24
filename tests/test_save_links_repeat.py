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
        one = {"ids": [7], "category": "C", "parts": 1, "queued": False}
        for i in range(3):
            bot._list_remember(f"old{i}", "f", 1e9, one, [1], ["https://a.b/"])
        now = time.time()
        bot._list_remember("new", "f", now, {**one, "unsaved": 2}, [5], ["https://a.b/c"])
        self.assertEqual(json.loads(db.get_setting(bot._LIST_SEEN_KEY)), [{
            "t": now, "k": "new", "f": "f", "ids": [7], "m": [5], "u": ["https://a.b/c"], "category": "C",
            "parts": 1, "queued": False, "unsent": 0, "unsaved": 2}])    # старше суток — выкинуты
        for i in range(n):
            bot._list_remember(f"k{i}", f"f{i}", now, one, [None], [None])
        rows = json.loads(db.get_setting(bot._LIST_SEEN_KEY))
        self.assertEqual(len(rows), bot._LIST_SEEN_MAX)
        self.assertEqual(rows[-1]["k"], f"k{n - 1}")
        for junk in ("{broken", '{"a": 1}', '[1, "x", {"t": "nan?"}]'):
            db.set_setting(bot._LIST_SEEN_KEY, junk)
            self.assertEqual(bot._list_seen(now), [])
        st, d = self.post({"mode": "list", "items": self.items()})
        self.assertEqual((st, d["status"]), (200, "saved"))

    def big_items(self):
        return [link(url=f"https://example.com/p/{i}", title="t" * 200, description="d" * 140)
                for i in range(60)]

    def fail_save_item(self, which):
        """db.save_item падает на вызовах с номерами из which (1 — первая часть)."""
        real_save_item = db.save_item
        calls = {"n": 0}

        def flaky_save_item(**kw):
            calls["n"] += 1
            if calls["n"] in which:
                raise RuntimeError("disk I/O error")
            return real_save_item(**kw)
        self.stub(db, "save_item", flaky_save_item)

    def track_processing(self):
        """Сообщения «⏳ Сохраняю…» и удалённые из них."""
        real = self.client.send_message
        seen = {"processing": [], "deleted": []}

        async def send(chat, text, **kw):
            m = await real(chat, text, **kw)
            if text.startswith("⏳"):
                seen["processing"].append(m)

                async def delete(m=m):
                    seen["deleted"].append(m)
                m.delete = delete
            return m
        self.client.send_message = send
        return seen

    def test_failure_after_parts_sent_is_200_not_500(self):
        """Части уже в чате — сбой сохранения не даёт 500 (иначе расширение пришлёт список снова),
        но честный unsaved: такой список при повторе сохраняется заново, а не «exists»."""
        async def boom(*a, **kw):
            raise RuntimeError("AI layer crashed")
        self.stub(bot, "_save", boom)
        seen = self.track_processing()
        self.fail_save_item({2})
        items = self.big_items()
        st, d = self.post({"mode": "list", "batchKey": "fail", "items": items})
        self.assertEqual((st, d["ok"], d["status"], d["queued"], d["unsaved"]), (200, True, "saved", True, 1), d)
        self.assertGreater(d["parts"], 1)
        self.assertEqual(len(d["ids"]), d["parts"] - 1)
        self.assertNotIn("unsent", d)
        self.assertEqual(len(seen["processing"]), 1)
        self.assertEqual(seen["deleted"], seen["processing"])        # «⏳» убрано и при сбое _save
        for i in d["ids"]:
            self.assertIn(str(i), db.retry_queue_items())            # без ИИ — в очередь дораспознавания
        n_posts = len(self.posts())
        self.assertEqual(n_posts, d["parts"])
        rows = json.loads(db.get_setting(bot._LIST_SEEN_KEY))
        self.assertEqual(rows[-1]["unsaved"], 1)
        # повтор: часть не легла в архив — список сохраняется заново целиком
        st, d2 = self.post({"mode": "list", "batchKey": "fail", "items": items})
        self.assertEqual((st, d2["status"]), (200, "saved"), d2)
        self.assertNotIn("unsaved", d2)
        self.assertEqual(len(d2["ids"]), d2["parts"])
        self.assertEqual(len(self.posts()), n_posts * 2)
        # а уж этот, полный, — повтор
        st, d3 = self.post({"mode": "list", "batchKey": "fail", "items": items})
        self.assertEqual((st, d3["status"], d3["ids"]), (200, "exists", d2["ids"]))

    def test_part_neither_in_chat_nor_in_archive_is_partial(self):
        """Часть не ушла в чат И не легла в архив — ссылки пропали бы: не ok, а 500 partial,
        чтобы расширение оставило их в «Не отправлены»; повтор сохраняет список заново."""
        real = self.client.send_message

        async def flaky(chat, text, **kw):
            if text.startswith("🔗") and "часть 2/" in text:
                raise RuntimeError("FLOOD")
            return await real(chat, text, **kw)
        self.client.send_message = flaky
        self.stub(bot, "SAVE_LINKS_RESEND_PAUSE", 0)
        self.fail_save_item({2})       # 1-й save_item — первая часть (внутри _save), 2-й — часть 2
        items = self.big_items()
        st, d = self.post({"mode": "list", "batchKey": "lost", "items": items})
        self.assertEqual((st, d["ok"], d["code"], d["unsent"], d["unsaved"], d["lost"]),
                         (500, False, "partial", 1, 1, 1), d)
        self.assertEqual(len(d["ids"]), d["parts"] - 1)
        self.client.send_message = real
        st, d2 = self.post({"mode": "list", "batchKey": "lost", "items": items})
        self.assertEqual((st, d2["status"], len(d2["ids"])), (200, "saved", d2["parts"]))

    def test_exists_counts_only_live_parts(self):
        st, d = self.post({"mode": "list", "batchKey": "live", "items": self.big_items()})
        self.assertGreater(d["parts"], 2)
        db.trash_item(d["ids"][1])
        st, d2 = self.post({"mode": "list", "batchKey": "live", "items": self.big_items()})
        self.assertEqual((d2["status"], d2["parts"]), ("exists", len(d2["ids"])))
        self.assertEqual(d2["ids"], [d["ids"][0], *d["ids"][2:]])

    def test_reused_id_is_not_the_same_post(self):
        """Тот же id, но другой пост (архив восстановлен из копии, id заняли заново) — не повтор."""
        body = {"mode": "list", "batchKey": "reuse", "items": self.items()}
        d = self.post(body)[1]
        rows = json.loads(db.get_setting(bot._LIST_SEEN_KEY))
        rows[-1]["m"] = [rows[-1]["m"][0] + 1000]                    # другое сообщение
        db.set_setting(bot._LIST_SEEN_KEY, json.dumps(rows))
        self.assertEqual(self.post(body)[1]["status"], "saved")
        rows = json.loads(db.get_setting(bot._LIST_SEEN_KEY))
        rows[-1]["u"] = ["https://other.example/x"]                  # другая первая ссылка
        db.set_setting(bot._LIST_SEEN_KEY, json.dumps(rows))
        self.assertEqual(self.post(body)[1]["status"], "saved")
        self.assertEqual(self.post(body)[1]["status"], "exists")
        self.assertNotEqual(d["ids"], self.post(body)[1]["ids"])

    def test_save_crash_after_item_written_does_not_duplicate(self):
        real = bot._save

        async def save_then_crash(*a, **kw):
            await real(*a, **kw)
            raise RuntimeError("crash after save_item")
        self.stub(bot, "_save", save_then_crash)
        seen = self.track_processing()
        st, d = self.post({"mode": "list", "items": self.items()})
        self.assertEqual((st, d["status"], len(d["ids"])), (200, "saved", 1), d)
        self.assertEqual(len(db.items_with_text_fragments(["https://example.com/r/0"])), 1)
        self.assertEqual(seen["deleted"], seen["processing"])

    def test_crash_fallback_ignores_same_text_from_other_message(self):
        """Запасной поиск после сбоя _save сверяет и сообщение: старый пост с тем же текстом
        (другой message_id) не выдаётся за только что сохранённый."""
        d = self.post({"mode": "list", "items": self.items()})[1]
        old = db.get_item(d["ids"][0])

        async def boom(*a, **kw):
            raise RuntimeError("AI layer crashed")
        self.stub(bot, "_save", boom)
        self.stub(bot, "SAVE_LINKS_SAME_LIST_TTL", 0)               # без ключа — уже не повтор
        st, d2 = self.post({"mode": "list", "items": self.items()})
        self.assertEqual((st, d2["status"], len(d2["ids"])), (200, "saved", 1), d2)
        self.assertNotEqual(d2["ids"][0], old["id"])
        self.assertNotEqual(db.get_item(d2["ids"][0])["message_id"], old["message_id"])

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
