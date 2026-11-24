"""Темы в боте, слежка за аниме (Shikimori) и релизами (GitHub), «Что почитать сегодня». Без сети.
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
import watchers  # noqa: E402


def _post(cat, text):
    return db.save_item(category=cat, content_type="text", summary=text[:60], original_text=text,
                        file_id=None, message_id=1, chat_id=1)


def _data(btn):
    return getattr(btn, "data", None) or getattr(getattr(btn, "type", None), "data", b"")


class Ev:
    chat_id = 1

    def __init__(self):
        self.out = []

    async def edit(self, text=None, **kw):
        self.out.append((text, kw.get("buttons")))

    respond = edit

    async def answer(self, *a, **kw):
        pass


class Base(unittest.TestCase):
    def setUp(self):
        self.tok = db.use_db_path(os.path.join(tempfile.mkdtemp(), "a.db"))
        db.init_db()
        self.sent = []

        async def send(chat, text, **kw):
            self.sent.append((text, kw.get("buttons")))
        self.client = mock.Mock()
        self.client.send_message = send
        p = mock.patch.object(bot, "client", self.client)
        p.start()
        self.addCleanup(p.stop)

    def tearDown(self):
        db.reset_db_path(self.tok)


class TopicTests(Base):
    def test_topics_pages(self):
        a, b, c = _post("Игры", "трейлер"), _post("Новости", "слух"), _post("Мемы", "мем")
        db.set_item_topics({a: ["GTA 6"], b: ["GTA 6", "Rockstar"], c: ["Rockstar"]})
        db.trash_item(c)                                          # удалённые не считаем
        self.assertEqual(db.topic_counts(), [("GTA 6", 2)])
        ev = Ev()
        asyncio.run(bot.show_topics(ev))
        self.assertEqual(_data(ev.out[-1][1][0][0]), "tpc|0|GTA 6".encode())
        asyncio.run(bot.show_topic(ev, "GTA 6"))
        self.assertIn("GTA 6 — 2", ev.out[-1][0])
        self.assertEqual({_data(r[0]) for r in ev.out[-1][1][:2]}, {f"view:{a}".encode(), f"view:{b}".encode()})


class WatchTests(Base):
    def test_links_parsing(self):
        rows = [{"id": 3, "text": "https://shikimori.io/animes/z57658-jujutsu и https://github.com/os6/nospeaker/tree/main",
                 "url": ""},
                {"id": 9, "text": "https://github.com/features/copilot https://shikimori.one/animes/59978-x", "url": ""}]
        self.assertEqual(watchers.anime_ids(rows), {59978: 9, 57658: 3})
        self.assertEqual(watchers.github_repos(rows), {"os6/nospeaker": 3})

    def test_anime_news(self):
        ong = {"russian": "Фрирен", "status": "ongoing", "episodes": 12, "episodes_aired": 5}
        self.assertIsNone(watchers.anime_news({}, ong))                       # первый проход — молчим
        self.assertIn("вышла 5-я серия из 12", watchers.anime_news(dict(ong, episodes_aired=4), ong))
        self.assertIsNone(watchers.anime_news(ong, ong))
        self.assertIn("начался показ", watchers.anime_news({"status": "anons"}, ong))
        self.assertIn("вышло целиком", watchers.anime_news(ong, dict(ong, status="released", episodes_aired=12)))

    def test_release_check_first_silent_then_reports(self):
        _post("Софт", "крутая утилита https://github.com/os6/nospeaker")
        ver = {"v": "v1.0"}

        async def rel(repos, **kw):
            return {"os6/nospeaker": {"tag": ver["v"], "name": ver["v"], "url": "https://github.com/os6/nospeaker",
                                      "body": "fixed bugs", "at": "2026-11-24"}}
        with mock.patch.object(watchers, "fetch_releases", rel):
            self.assertEqual(asyncio.run(bot.check_watch(1, "github")), 1)
            self.assertEqual(self.sent, [])                                    # о старой версии не пишем
            ver["v"] = "v1.1"
            asyncio.run(bot.check_watch(1, "github"))
        self.assertIn("новая версия v1.1 (была v1.0)", self.sent[0][0])
        self.assertEqual(_data(self.sent[0][1][0][1]), b"ghb|os6/nospeaker")


class DailyReadTests(Base):
    def test_cycles_through_articles_and_videos(self):
        a = _post("IT", "статья https://habr.com/x")
        db.article_set(a, "https://habr.com/x", "Хабр", "слово " * 400, "ok")
        v = _post("Видео", "посмотри https://youtu.be/abcdefghijk")
        _post("Мемы", "просто мем")
        got = {bot.pick_daily_read(), bot.pick_daily_read()}
        self.assertEqual(got, {a, v})                                         # без повторов, пока есть новое
        self.assertIn(bot.pick_daily_read(), {a, v})                         # потом — новый круг
        self.assertTrue(asyncio.run(bot.send_daily_read(1)))
        text, rows = self.sent[-1]
        self.assertTrue(text.startswith(("📖", "🎬")))
        flat = [_data(b) for r in rows for b in r]
        self.assertIn(b"rdnext", flat)
        self.assertIn(b"rdoff", flat)

    def test_nothing_to_offer(self):
        self.assertFalse(asyncio.run(bot.send_daily_read(1)))
        self.assertIn("нечего", self.sent[-1][0])


if __name__ == "__main__":
    unittest.main()
