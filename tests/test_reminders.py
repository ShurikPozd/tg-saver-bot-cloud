"""«⏰ Напомнить» и «🆕 ещё не смотрел». Запуск: python -m unittest discover tests"""

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "smoke.db"))

import bot  # noqa: E402
import database as db  # noqa: E402
import keyboards as kb  # noqa: E402


def _post(cat, summary):
    return db.save_item(category=cat, content_type="text", summary=summary, original_text=summary,
                        file_id=None, message_id=1, chat_id=1)


class Base(unittest.TestCase):
    def setUp(self):
        self.tok = db.use_db_path(os.path.join(tempfile.mkdtemp(), "a.db"))
        db.init_db()
        self.old = _post("Игры", "старый пост")

    def tearDown(self):
        db.reset_db_path(self.tok)


class RemindTests(Base):
    def test_due_dates_are_9am_moscow(self):
        wed = datetime(2026, 11, 25, 15, 0)          # среда, 18:00 МСК
        for kind, day in (("tom", 26), ("d3", 28), ("w", 2), ("sat", 28)):
            due = datetime.utcfromtimestamp(bot._remind_due(kind, wed))
            self.assertEqual((due.day, due.hour), (day, 6), kind)   # 06:00 UTC = 09:00 МСК
        sat = datetime(2026, 11, 28, 10, 0)
        self.assertEqual(datetime.utcfromtimestamp(bot._remind_due("sat", sat)).day, 5)  # уже суббота — следующая

    def test_card_button_shows_reminder(self):
        texts = lambda rows: [b.text for r in rows for b in r]
        self.assertIn("⏰ Напомнить", texts(kb.view_keyboard(1, "Игры")))
        self.assertIn("⏰ чт 26.11 · отменить", texts(kb.view_keyboard(1, "Игры", remind="чт 26.11")))

    def test_loop_sends_due_reminder_once(self):
        sent, forwarded = [], []

        class FakeClient:
            async def send_message(self, chat, text, **kw):
                sent.append((chat, text))

        async def fwd(chat, iid):
            forwarded.append(iid)
            return True

        bot._reminders_set([{"id": self.old, "due": time.time() - 5, "chat": 7},
                            {"id": self.old, "due": time.time() + 86400, "chat": 7}])
        orig = (bot.client, bot.send_item_to_chat, bot.asyncio.sleep, bot.db.all_users)
        calls = {"n": 0}

        async def fast_sleep(sec):
            calls["n"] += 1
            if calls["n"] > 1:
                raise asyncio.CancelledError

        bot.client, bot.send_item_to_chat, bot.asyncio.sleep = FakeClient(), fwd, fast_sleep
        bot.db.all_users = lambda: [db.current_user_id()]
        try:
            with self.assertRaises(asyncio.CancelledError):
                asyncio.run(bot.reminders_loop())
        finally:
            bot.client, bot.send_item_to_chat, bot.asyncio.sleep, bot.db.all_users = orig
        self.assertEqual(len(sent), 1)
        self.assertIn("старый пост", sent[0][1])
        self.assertEqual(forwarded, [self.old])
        self.assertEqual(len(bot._reminders()), 1)                 # будущее осталось


class SeenTests(Base):
    def test_old_posts_are_seen_new_ones_not(self):
        bot._seen_state()                                          # первая точка отсчёта
        new = _post("Игры", "новый пост")
        self.assertEqual(bot.unseen_ids(), {new})                  # старые не стали «новыми» разом
        items = bot._mark_new(db.get_items_by_category("Игры"))
        self.assertTrue(next(i for i in items if i["id"] == new)["new"])
        rows = kb._items_rows(items, "Игры", 0, 1)
        self.assertTrue(any(b.text.startswith("🆕") for r in rows for b in r))
        bot.mark_seen(new)
        self.assertEqual(bot.unseen_ids(), set())

    def test_web_data_flags_new(self):
        import webarchive
        bot._seen_state()
        new = _post("Игры", "новый пост")
        data = webarchive.archive_data({}, "b", bot.unseen_ids())
        flags = {i["id"]: i["nw"] for i in data["items"]}
        self.assertEqual(flags, {self.old: False, new: True})
        self.assertIn('"🆕 Ещё не смотрел"', webarchive.PAGE_HTML)


if __name__ == "__main__":
    unittest.main()
