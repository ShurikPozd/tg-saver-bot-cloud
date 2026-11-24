"""Правила по темам, страховая копия медиа. Без сети и Telegram.
Запуск: python -m unittest discover tests"""

import asyncio
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "smoke.db"))

import bot  # noqa: E402
import database as db  # noqa: E402


def _post(cat, text, media=False):
    return db.save_item(category=cat, content_type="photo" if media else "text", summary=text, original_text=text,
                        file_id="AgAC" if media else None, message_id=1, chat_id=1,
                        file_ids=[{"f": "AgACAgIAAxkBAAI", "u": "photo:1"}] if media else None)


class Base(unittest.TestCase):
    def setUp(self):
        self.tok = db.use_db_path(os.path.join(tempfile.mkdtemp(), "a.db"))
        db.init_db()
        self.sent = []
        outer = self

        class C:
            async def send_message(self, chat, text="", **kw):
                outer.sent.append((chat, text, kw.get("buttons")))

            async def send_file(self, chat, refs, **kw):
                outer.sent.append((chat, "FILE", kw.get("caption")))

                class M:
                    id = 500 + len(outer.sent)
                return [M()] if isinstance(refs, list) else M()
        p = mock.patch.object(bot, "client", C())
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        db.reset_db_path(self.tok)


class TopicRuleTests(Base):
    def test_three_moves_offer_rule_then_new_posts_follow_it(self):
        ids = [_post("Новости", f"GTA 6 новость {i}") for i in range(3)]
        _post("GTA", "папка есть")
        db.set_item_topics({i: ["GTA 6"] for i in ids})
        for i in ids:
            asyncio.run(bot._note_topic_moves(1, [i], "GTA"))
        offers = [t for c, t, b in self.sent if "GTA 6" in t]
        self.assertEqual(len(offers), 1)                                     # спросил один раз
        tok = next(b for r in self.sent[-1][2] for b in r if "Да" in b.text)
        data = getattr(tok, "data", None) or tok.type.data

        class Ev:
            chat_id = 1

            async def answer(self, *a, **k):
                pass

            async def edit(self, *a, **k):
                pass
        asyncio.run(bot.handle_topic_rule_cb(Ev(), data.decode()))
        self.assertEqual(bot._topic_rules(), {"GTA 6": "GTA"})
        self.assertEqual(bot._topic_rule(["Steam", "GTA 6"]), ("GTA 6", "GTA"))
        db.set_category_locked("GTA", True)                                   # в заблокированную — нельзя
        self.assertIsNone(bot._topic_rule(["GTA 6"]))

    def test_refusal_is_remembered(self):
        ids = [_post("Новости", f"Claude {i}") for i in range(6)]
        _post("ИИ", "папка")
        db.set_item_topics({i: ["Claude"] for i in ids})
        for i in ids[:3]:
            asyncio.run(bot._note_topic_moves(1, [i], "ИИ"))
        votes = json.loads(db.get_setting("trule_votes"))
        votes["Claude"]["ИИ"] = -1
        db.set_setting("trule_votes", json.dumps(votes))
        n = len(self.sent)
        for i in ids[3:]:
            asyncio.run(bot._note_topic_moves(1, [i], "ИИ"))
        self.assertEqual(len(self.sent), n)                                    # больше не спрашивает


class MirrorTests(Base):
    def test_media_copied_once_to_storage_channel(self):
        a = _post("Мемы", "кот", media=True)
        _post("Заметки", "просто текст")
        with mock.patch.object(bot, "BACKUP_CHANNEL_ID", -100123), mock.patch.object(bot, "MIRROR_PAUSE", 0), \
                mock.patch.object(bot.utils, "resolve_bot_file_id", lambda f: "REF"):
            self.assertEqual(asyncio.run(bot.mirror_media_once(1)), 1)
            self.assertEqual(asyncio.run(bot.mirror_media_once(1)), 0)       # уже скопировано
        self.assertTrue(bot.mirror_ids(a))
        self.assertEqual([c for c, t, _ in self.sent if t == "FILE"], [-100123])
        self.assertIn(f"#п{a}", self.sent[0][2])

    def test_bad_file_does_not_stop_copying(self):
        a = _post("Мемы", "битый", media=True)
        b = _post("Мемы", "нормальный", media=True)
        refs = {a: None, b: "REF"}
        orig = bot._resend

        async def fake_resend(item):
            return [refs[item["id"]]], None
        with mock.patch.object(bot, "BACKUP_CHANNEL_ID", -100123), mock.patch.object(bot, "MIRROR_PAUSE", 0),                 mock.patch.object(bot, "_resend", fake_resend):
            self.assertEqual(asyncio.run(bot.mirror_media_once(1)), 1)
        self.assertEqual(bot.mirror_ids(a), [])          # битый пропущен
        self.assertTrue(bot.mirror_ids(b))               # нормальный скопирован
        self.assertFalse(bot._no_channel_access(TypeError("a TLObject was expected but found something else")))
        self.assertTrue(bot._no_channel_access(ValueError("Could not find the input entity for PeerChannel")))
        self.assertTrue(orig)

    def test_no_channel_no_copy(self):
        _post("Мемы", "кот", media=True)
        with mock.patch.object(bot, "BACKUP_CHANNEL_ID", None):
            self.assertEqual(asyncio.run(bot.mirror_media_once(1)), 0)


if __name__ == "__main__":
    unittest.main()
