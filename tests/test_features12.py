"""Пустые посты после перераспознавания и новые функции раунда 12. Без сети и Telegram.
Запуск: python -m unittest discover tests"""

import asyncio
import os
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "smoke.db"))

import bot  # noqa: E402
import database as db  # noqa: E402


def _post(cat, text, ctype="text", mid=1):
    return db.save_item(category=cat, content_type=ctype, summary=text[:40] or "Заметка", original_text=text,
                        file_id=None, message_id=mid, chat_id=1)


class Base(unittest.TestCase):
    def setUp(self):
        self.tok = db.use_db_path(os.path.join(tempfile.mkdtemp(), "a.db"))
        db.init_db()

    def tearDown(self):
        db.reset_db_path(self.tok)


class _Ev:
    def __init__(self, chat_id=5):
        self.chat_id = chat_id
        self.out = []

    async def edit(self, text, buttons=None, **kw):
        self.out.append((text, buttons))

    respond = edit

    async def answer(self, *a, **kw):
        self.out.append(("answer", a))


class NoDataTests(Base):
    def test_empty_text_post_reads_chat_message(self):
        a = _post("Заметки", "", mid=77)

        class FakeClient:
            async def get_messages(self, chat, ids=None):
                return [SimpleNamespace(media=None, text="Опрос: какую игру взять на выходные?")]

        async def fake_analyze(client, msgs, ct):
            return {"llm_text": msgs[0].text, "aux": "", "display": msgs[0].text}
        with mock.patch.object(bot, "client", FakeClient()), mock.patch.object(bot, "_analyze", fake_analyze):
            txt = asyncio.run(bot._recat_text(db.get_item(a), report={"no_refetch": True}))
        self.assertIn("какую игру", txt)

    def test_show_and_purge_survive_restart(self):
        a, b = _post("Заметки", "", mid=1), _post("Заметки", "", mid=2)
        bot._nodata_set(5, [a, b])
        bot.nodata_batch.clear()                        # «перезапуск» — список берётся из настроек
        ev = _Ev()
        asyncio.run(bot.show_nodata(ev))
        text, rows = ev.out[-1]
        self.assertIn("Пустые посты: 2", text)
        datas = [getattr(getattr(x, "type", x), "data", b"") for r in rows for x in r]
        self.assertIn(f"lcitem:{a}".encode(), datas)
        self.assertIn(b"purge_nodata", datas)
        db.trash_item(a)
        self.assertEqual(bot._nodata_get(5), [b])        # уже убранный не показываем

    def test_old_button_shows_first_and_cleanup_lists_empty(self):
        a = _post("Заметки", "", mid=3)
        full = _post("Разное", "нормальный пост с текстом", mid=4)
        self.assertEqual(bot.empty_posts(), [a])
        self.assertIn(a, dict(bot.cleanup_candidates()))
        self.assertNotIn(full, dict(bot.cleanup_candidates()))
        ev = _Ev(chat_id=9)
        ev.data, ev.sender_id, ev.client = b"purge_nodata", 9, None
        with mock.patch.object(bot, "_activate_user", lambda u: u):
            asyncio.run(bot.on_callback(ev))
        self.assertIn("Пустые посты: 1", ev.out[-1][0])     # старая кнопка: сначала список, не удаление
        self.assertTrue(db.get_item(a))


if __name__ == "__main__":
    unittest.main()
