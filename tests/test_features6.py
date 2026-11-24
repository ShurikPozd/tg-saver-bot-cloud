"""«🖼 Похожие», «➕ Добавить» из приложения. Без сети и без Telegram.
Запуск: python -m unittest discover tests"""

import asyncio
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "smoke.db"))

import bot  # noqa: E402
import database as db  # noqa: E402


def _post(cat, text, summary=None):
    return db.save_item(category=cat, content_type="text", summary=summary or text[:60], original_text=text,
                        file_id=None, message_id=1, chat_id=1)


class Base(unittest.TestCase):
    def setUp(self):
        self.tok = db.use_db_path(os.path.join(tempfile.mkdtemp(), "a.db"))
        db.init_db()

    def tearDown(self):
        db.reset_db_path(self.tok)


class SimilarTests(Base):
    def test_topics_and_rare_words_rank_above_noise(self):
        a = _post("Игры", "В GTA 6 нашли пасхалку с порталом")
        b = _post("Новости", "Трейлер GTA 6: Люсия и Джейсон")
        c = _post("Мемы", "Обновление приложения погоды")
        d = _post("Игры", "Пасхалка с порталом найдена: телепортация между городами")
        for i in range(200):   # как в живом архиве: сотни постов, частые слова встречаются много раз
            _post("Разное", f"обновление номер {i} без смысла")
        db.set_item_topics({a: ["GTA 6"], b: ["GTA 6"]})
        got = dict(bot.similar_items(a))
        self.assertIn(b, got)                         # общая тема
        self.assertTrue(got[b].startswith("🏷"))
        self.assertIn(d, got)                         # общие редкие слова «пасхалка», «портал»
        self.assertNotIn(c, got)                      # одно частое слово — не повод
        self.assertEqual(bot.similar_items(99999), [])

    def test_topic_named_in_text_counts(self):
        a = _post("Игры", "Обзор Hollow Knight Silksong")
        b = _post("Видео", "Спидран Hollow Knight за 30 минут")
        db.set_item_topics({a: ["Hollow Knight"]})
        self.assertIn(b, dict(bot.similar_items(a)))


class AddTests(Base):
    def _client(self):
        sent = []

        class Msg:
            def __init__(self, i):
                self.id = i

        class C:
            def is_connected(self):
                return True

            async def send_message(self, chat, text="", **kw):
                sent.append(text)
                return Msg(len(sent))
        return C(), sent

    def test_note_is_saved_like_a_message(self):
        cl, sent = self._client()

        async def fake_save(client, **kw):
            iid = db.save_item(category="Заметки", content_type="text", summary="заметка",
                               original_text=kw["analysis"]["display"], file_id=None,
                               message_id=kw["message_id"], chat_id=kw["chat_id"])
            return {"id": iid, "category": "Заметки", "summary": "заметка", "failed": False}
        with mock.patch.object(bot, "client", cl), mock.patch.object(bot, "_save", fake_save), \
                mock.patch.object(bot, "_activate_user", lambda *_: None):
            r = asyncio.run(bot.web_add(1, "купить краски для гандама"))
            self.assertEqual(r["status"], "saved")
            self.assertEqual(sent[0], "купить краски для гандама")       # пост появился и в чате
            again = asyncio.run(bot.web_add(1, "купить краски для гандама"))
        self.assertEqual(again["status"], "exists")
        self.assertEqual(asyncio.run(bot.web_add(1, "   "))["status"], "error")

    def test_single_link_goes_through_link_saver(self):
        cl, _ = self._client()
        calls = []

        async def fake_article(url):
            return {"status": "ok", "url": url, "title": "Хабр: статья", "text": "текст " * 100}

        async def fake_one(uid, it):
            calls.append(it)
            return {"status": "saved", "id": 5, "category": "IT", "icon": "💻"}
        with mock.patch.object(bot, "client", cl), mock.patch.object(bot.linkmeta, "fetch_article", fake_article), \
                mock.patch.object(bot, "_save_one_link", fake_one):
            r = asyncio.run(bot.web_add(1, "https://habr.com/ru/articles/1/"))
        self.assertEqual(r["status"], "saved")
        self.assertEqual(calls[0]["title"], "Хабр: статья")

    def test_offline_bot_reports_error(self):
        with mock.patch.object(bot, "client", None):
            self.assertIn("не подключён", asyncio.run(bot.web_add(1, "заметка"))["error"])


if __name__ == "__main__":
    unittest.main()
