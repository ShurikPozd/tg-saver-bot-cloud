"""Ревью 24.11 (вечер), сервер: имя файла архива, дубли с разными ссылками, копия медиа при сбое сети,
фоновые правки после ответа приложению, статьи из бэкапа, повтор неудачных статей. Без сети и Telegram.
Запуск: python -m unittest discover tests"""

import asyncio
import gzip
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

UID = 778001


def _post(text, media=False):
    return db.save_item(category="Разное", content_type="photo" if media else "text", summary=text[:40],
                        original_text=text, file_id="AgAC" if media else None, message_id=1, chat_id=1,
                        file_ids=[{"f": "AgACAgIAAxkBAAI", "u": "photo:1"}] if media else None)


class Base(unittest.TestCase):
    def setUp(self):
        self.tok = db.use_db_path(os.path.join(tempfile.mkdtemp(), "a.db"))
        db.init_db()

    def tearDown(self):
        db.reset_db_path(self.tok)


class ServerFixTests(Base):
    def test_archive_file_has_a_name(self):
        f = bot._named_file(b"\x1f\x8b123", "Архив_2026-11-24.json.gz")
        self.assertEqual(f.name, "Архив_2026-11-24.json.gz")
        self.assertEqual(f.read(), b"\x1f\x8b123")

    def test_same_caption_different_links_is_not_a_dup(self):
        cap = "Подписывайся на наш канал, тут лучшие видео каждый день без остановки"
        a = _post(cap + " https://youtu.be/aaaaaaaaaaa")
        b = _post(cap + " https://youtu.be/bbbbbbbbbbb")
        c = _post(cap + " https://youtu.be/aaaaaaaaaaa")
        self.assertEqual(db.find_text_dups(), [[a, c]])
        self.assertTrue(b)

    def test_network_error_does_not_mark_media_as_copied(self):
        a = _post("кот", media=True)

        class C:
            async def send_file(self, *a, **k):
                raise ConnectionError("disconnected")
        with mock.patch.object(bot, "client", C()), mock.patch.object(bot, "BACKUP_CHANNEL_ID", -1001), \
                mock.patch.object(bot, "MIRROR_PAUSE", 0), mock.patch.object(bot.utils, "resolve_bot_file_id", lambda f: "R"):
            self.assertEqual(asyncio.run(bot.mirror_media_once(1)), 0)
        self.assertNotIn(str(a), bot._media_mirror())          # попробует снова в следующий раз

    def test_background_edit_after_app_answer_goes_to_chat(self):
        sent = []

        class C:
            async def send_message(self, chat, text="", **kw):
                sent.append(("send", chat, text))

            async def edit_message(self, *a, **kw):
                sent.append(("edit",))

            async def send_file(self, *a, **kw):
                pass

            async def delete_messages(self, *a, **kw):
                pass
        with mock.patch.object(bot, "client", C()):
            bot._install_web_capture()
            cap = bot._WebCap(UID)
            token = bot._web_cap.set(cap)
            msg = cap.new("⏳ Перераспознаю…")
            cap.open = False                                     # ответ приложению ушёл
            asyncio.run(bot.client.edit_message(UID, msg.id, "✅ Готово"))
            asyncio.run(msg.edit("✅ Готово 2"))
            bot._web_cap.reset(token)
        self.assertEqual([s[2] for s in sent if s[0] == "send"], ["✅ Готово", "✅ Готово 2"])
        self.assertNotIn(("edit",), sent)                        # отрицательный id в Telegram не уходит

    def test_backup_article_text_beats_fresh_fail(self):
        a = _post("статья https://gone.example.com/x")
        db.article_set(a, "https://gone.example.com/x", "Заголовок", "сохранённый текст " * 30, "ok")
        raw = gzip.compress(json.dumps(db.export_json(), ensure_ascii=False, default=str).encode())
        db.article_set(a, "https://gone.example.com/x", "", "", "fail")   # фон успел отметить «не скачалось»
        db.import_json_stream(raw)
        self.assertEqual(db.article_get(a)["status"], "ok")
        self.assertIn("сохранённый текст", db.article_get(a)["text"])

    def test_failed_article_retried_after_a_week(self):
        a = _post("статья https://flaky.example.com/x")
        db.article_set(a, "https://flaky.example.com/x", "", "", "fail")
        self.assertEqual(db.items_needing_article(), [])
        with db.get_connection() as conn:
            conn.execute("UPDATE item_articles SET fetched_at = datetime('now', '-8 days')")
            conn.commit()
        self.assertEqual([x["id"] for x in db.items_needing_article()], [a])


if __name__ == "__main__":
    unittest.main()
