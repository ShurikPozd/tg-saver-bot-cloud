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


class MemoriesTests(Base):
    def test_picks_oldest_period_media_first(self):
        def at(iid, day, ct="text"):
            with db.get_connection() as conn:
                conn.execute("UPDATE saved_items SET created_at = ?, content_type = ? WHERE id = ?",
                             (f"{day} 12:00:00", ct, iid))
                conn.commit()
        year_ago = _post("Игры", "год назад")
        at(year_ago, "2025-11-30")
        month_text = _post("Другое", "месяц назад текст")
        at(month_text, "2026-10-30")
        month_pic = _post("Смешное", "месяц назад мем")
        at(month_pic, "2026-10-31", "photo")
        title, ids = bot.pick_memories(datetime(2026, 11, 29, 8))
        self.assertEqual((title, ids), ("Год назад", [year_ago]))          # самый давний срок первым
        with db.get_connection() as conn:
            conn.execute("DELETE FROM saved_items WHERE id = ?", (year_ago,))
            conn.commit()
        title, ids = bot.pick_memories(datetime(2026, 11, 29, 8))
        self.assertEqual((title, ids), ("Месяц назад", [month_pic, month_text]))  # картинка вперёд «Другого»
        self.assertEqual(bot.pick_memories(datetime(2030, 1, 1)), ("", []))


class TriageTests(Base):
    def test_walk_through_new_posts(self):
        bot._seen_state()
        a = _post("Игры", "новость про GTA")
        b = _post("Другое", "мем с котом")
        db.ensure_category("Смешное")
        bot.mark_seen(_post("Смешное", "старый мем"))       # уже смотрел — в разбор не попадёт
        screens, answers = [], []

        class Ev:
            chat_id = 1

            async def edit(self, text, buttons=None, **kw):
                screens.append((text, [b.text for r in (buttons or []) for b in r]))

            async def answer(self, *a, **kw):
                answers.append(a[0] if a else "")

            async def respond(self, *a, **kw):
                pass

        ev = Ev()
        asyncio.run(bot.handle_triage(ev, "triage"))
        self.assertIn("новость про GTA", screens[-1][0])              # сначала старший из новых
        self.assertIn("✅ Да, верно", screens[-1][1])
        asyncio.run(bot.handle_triage(ev, f"tri_ok:{a}"))
        self.assertIn("мем с котом", screens[-1][0])
        asyncio.run(bot.handle_triage(ev, f"tri_mv:{b}"))
        self.assertTrue(any("Смешное" in t for t in screens[-1][1]))   # подсказка папки
        asyncio.run(bot.handle_triage(ev, f"tri_set:{b}:Смешное"))
        self.assertEqual(db.get_item(b)["category"], "Смешное")
        self.assertIn("Всё разобрано", screens[-1][0])
        self.assertEqual(bot.unseen_ids() & {a, b}, set())


class VoiceAssistantTests(unittest.TestCase):
    def _run(self, heard):
        asked, edits = [], []

        class Msg:
            id = 5

        class Ev:
            chat_id = 1

            async def respond(self, text, **kw):
                return Msg()

        class FakeClient:
            async def download_media(self, msg, file=None):
                return b"ogg"

            async def edit_message(self, chat, mid, text, **kw):
                edits.append(text)

        async def stt(data, **kw):
            return heard

        async def ask(event, text):
            asked.append(text)

        orig = (bot.client, bot.transcribe_audio, bot.assistant_ask)
        bot.client, bot.transcribe_audio, bot.assistant_ask = FakeClient(), stt, ask
        try:
            voice = type("Voice", (), {"voice": object(), "video_note": None})()
            asyncio.run(bot.assistant_voice(Ev(), voice))
        finally:
            bot.client, bot.transcribe_audio, bot.assistant_ask = orig
        return asked, edits

    def test_voice_goes_to_assistant_as_text(self):
        asked, edits = self._run("найди видео с крысами")
        self.assertEqual(asked, ["найди видео с крысами"])
        self.assertIn("Ты сказал: «найди видео с крысами»", edits[0])

    def test_silence_is_explained(self):
        asked, edits = self._run("")
        self.assertEqual(asked, [])
        self.assertIn("Не расслышал", edits[0])


if __name__ == "__main__":
    unittest.main()
