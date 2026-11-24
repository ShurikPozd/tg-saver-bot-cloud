"""🔗 Ссылка на сводку комментариев YouTube: /api/share-summary и страница /sum/<id>/.
Без сети и без Telegram: поднимается только aiohttp-приложение. Запуск: python -m unittest discover tests"""

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "smoke.db"))

import database as db  # noqa: E402
import share_summary as ss  # noqa: E402

SECRET = "test-secret-not-real"
OWNER = 4242


def summary(**kw):
    s = {
        "videoId": "dQw4w9WgXcQ",
        "title": "Как устроен <b>чёрный</b> ящик",
        "channel": "Наука просто",
        "summary": "Зрители хвалят подачу.\n\nСпорят о третьей части.",
        "topics": [{"name": "Подача", "count": 120}, {"name": "Звук", "count": 31}],
        "points": ["Понятно объяснено", "Хотят продолжение"],
        "sentiment": {"positive": 61, "neutral": 25, "negative": 14},
        "comments": [{"author": "@viewer", "text": "Лучшее видео <script>alert(1)</script>", "likes": 1532}],
        "commentCount": 1234,
        "date": 1764000000000,
    }
    s.update(kw)
    return s


class Base(unittest.TestCase):
    owner = OWNER

    def setUp(self):
        self.tok = db.use_db_path(os.path.join(tempfile.mkdtemp(), "a.db"))
        db.init_db()
        os.environ.pop("PUBLIC_URL", None)

    def tearDown(self):
        db.reset_db_path(self.tok)

    def call(self, method, path, body=None, secret=SECRET, raw=None):
        from aiohttp import web
        from aiohttp.test_utils import TestClient, TestServer

        async def run():
            app = web.Application()
            ss.setup(app, activate_owner=lambda: self.owner, get_secret=lambda: SECRET)
            async with TestClient(TestServer(app)) as cl:
                headers = {"Content-Type": "application/json"}
                if secret is not None:
                    headers["X-Sec-Token"] = secret
                data = raw if raw is not None else (
                    json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None)
                r = await cl.request(method, path, data=data, headers=headers, allow_redirects=False)
                text = await r.text()
                return r.status, text, r.headers
        return asyncio.run(run())

    def create(self, body=None, **kw):
        st, text, h = self.call("POST", "/api/share-summary", body if body is not None else summary(), **kw)
        self.assertEqual(h.get("Access-Control-Allow-Origin"), "*")
        return st, json.loads(text)


class ApiTests(Base):
    def test_create_returns_link_and_same_link_for_same_summary(self):
        st, d = self.create()
        self.assertEqual(st, 200)
        self.assertTrue(d["ok"])
        self.assertFalse(d["existed"])
        self.assertRegex(d["url"], r"^http://127\.0\.0\.1:\d+/sum/[A-Za-z0-9_-]{12}/$")
        st2, d2 = self.create()
        self.assertEqual((st2, d2["id"], d2["existed"]), (200, d["id"], True))
        # другая сводка — другая ссылка
        st3, d3 = self.create(summary(summary="Совсем другое"))
        self.assertNotEqual(d3["id"], d["id"])

    def test_public_url_used(self):
        os.environ["PUBLIC_URL"] = "https://bot.example/"
        try:
            _, d = self.create()
        finally:
            os.environ.pop("PUBLIC_URL", None)
        self.assertEqual(d["url"], f"https://bot.example/sum/{d['id']}/")

    def test_secret_required(self):
        st, d = self.create(secret=None)
        self.assertEqual((st, d["code"]), (403, "forbidden"))
        st, d = self.create(secret="wrong")
        self.assertEqual(st, 403)
        st, text, _ = self.call("DELETE", "/api/share-summary/abcdefghijkl", secret="wrong")
        self.assertEqual(st, 403)
        self.assertEqual(ss._rows(), [])

    def test_bad_bodies(self):
        self.assertEqual(self.create(raw=b"{not json")[0], 400)
        self.assertEqual(self.create(summary(videoId="bad"))[1]["error"], "invalid videoId")
        self.assertEqual(self.create(summary(summary="   "))[1]["error"], "summary required")
        st, d = self.create(raw=b'{"a":"' + b"x" * (ss.MAX_BODY + 10) + b'"}')
        self.assertEqual((st, d["code"]), (413, "too_large"))

    def test_daily_limit(self):
        for i in range(ss.DAY_LIMIT):
            self.assertEqual(self.create(summary(summary=f"Сводка {i}"))[0], 200)
        st, d = self.create(summary(summary="Ещё одна"))
        self.assertEqual((st, d["code"]), (429, "rate"))
        # та же сводка ссылку отдаёт и после лимита
        self.assertEqual(self.create(summary(summary="Сводка 0"))[0], 200)
        # сутки прошли — снова можно
        db.set_setting(ss.LOG_KEY, json.dumps([int(time.time()) - 90000] * ss.DAY_LIMIT))
        self.assertEqual(self.create(summary(summary="Ещё одна"))[0], 200)

    def test_revoke_delete_and_post(self):
        _, d = self.create()
        path = f"/sum/{d['id']}/"
        self.assertEqual(self.call("GET", path, secret=None)[0], 200)
        st, text, _ = self.call("DELETE", f"/api/share-summary/{d['id']}")
        self.assertEqual((st, json.loads(text)["revoked"]), (200, True))
        st, text, h = self.call("GET", path, secret=None)
        self.assertEqual(st, 404)
        self.assertIn("Ссылка не работает", text)
        self.assertEqual(h.get("Cache-Control"), "no-store")
        # повторный отзыв — не ошибка
        st, text, _ = self.call("POST", f"/api/share-summary/{d['id']}/revoke")
        self.assertEqual((st, json.loads(text)["revoked"]), (200, False))
        # после отзыва та же сводка получает новую ссылку
        _, d2 = self.create()
        self.assertNotEqual(d2["id"], d["id"])
        st, text, _ = self.call("POST", f"/api/share-summary/{d2['id']}/revoke")
        self.assertTrue(json.loads(text)["revoked"])

    def test_cors_preflight(self):
        st, _, h = self.call("OPTIONS", "/api/share-summary/abcdefghijkl", secret=None)
        self.assertEqual(st, 204)
        self.assertIn("DELETE", h["Access-Control-Allow-Methods"])

    def test_no_owner(self):
        self.owner = None
        st, d = self.create()
        self.assertEqual((st, d["code"]), (503, "no_owner"))


class PageTests(Base):
    def test_page_content_and_escaping(self):
        _, d = self.create()
        st, text, h = self.call("GET", f"/sum/{d['id']}/", secret=None)
        self.assertEqual(st, 200)
        self.assertIn("text/html", h["Content-Type"])
        self.assertIn("img-src https://i.ytimg.com", h["Content-Security-Policy"])
        self.assertIn("noindex", h["X-Robots-Tag"])
        self.assertIn("https://i.ytimg.com/vi/dQw4w9WgXcQ/hqdefault.jpg", text)
        self.assertIn('rel="icon"', text)  # значок расширения во вкладке
        self.assertIn("data:", h["Content-Security-Policy"])  # и политика его пропускает
        self.assertIn("https://www.youtube.com/watch?v=dQw4w9WgXcQ", text)
        self.assertIn("Открыть видео на YouTube", text)
        self.assertIn("Как устроен &lt;b&gt;чёрный&lt;/b&gt; ящик", text)
        self.assertNotIn("<script", text)
        self.assertIn("61%", text)
        self.assertIn("по 1 234 комментариям", text)
        self.assertIn("1 532", text)
        self.assertIn("Подача <b>120</b>", text)
        self.assertIn("<p>Зрители хвалят подачу.</p><p>Спорят о третьей части.</p>", text)
        self.assertIn("24 ноября 2025", text)

    def test_layout_without_optional_blocks(self):
        # нет мнений, тем и комментариев — в сетке нет пустых клеток, настроение на всю ширину
        h = ss.render_page(ss.parse_request(summary(points=[], topics=[], comments=[])))
        self.assertIn('grid-template-areas:"hero sum" "mood mood" "foot foot"', h)
        self.assertNotIn("Ключевые мнения", h)
        self.assertNotIn("Яркие комментарии", h)
        h = ss.render_page(ss.parse_request(summary(sentiment={})))
        self.assertIn('"topics topics"', h)
        self.assertNotIn("Настроение зрителей", h)

    def test_no_slash_redirects_and_unknown_is_404(self):
        _, d = self.create()
        st, _, h = self.call("GET", f"/sum/{d['id']}", secret=None)
        self.assertEqual((st, h["Location"]), (302, f"/sum/{d['id']}/"))
        self.assertEqual(self.call("GET", "/sum/nope-nope-nope/", secret=None)[0], 404)
        self.assertEqual(self.call("GET", "/sum/../../etc/", secret=None)[0], 404)

    def test_stored_in_settings_so_export_has_it(self):
        _, d = self.create()
        keys = {r["key"] for r in db.export_json()["settings"]}
        self.assertIn(ss.PREFIX + d["id"], keys)
        self.assertIn(ss.LOG_KEY, keys)


class ParseTests(unittest.TestCase):
    def test_limits_and_cleaning(self):
        s = ss.parse_request(summary(
            title="  Т‮ест\n ", topics=[{"name": f"t{i}", "count": i} for i in range(30)] + ["мусор"],
            points=["a"] * 30 + [5], comments=[{"text": "x" * 5000, "author": "A" * 300, "likes": "7"}] * 10,
            sentiment={"positive": 1, "neutral": 1, "negative": 1}, date="мусор", extra="нет"))
        self.assertEqual(s["title"], "Тест")
        self.assertEqual(len(s["topics"]), ss.TOPICS_MAX)
        self.assertEqual(len(s["points"]), ss.POINTS_MAX)
        self.assertEqual(len(s["comments"]), ss.COMMENTS_MAX)
        self.assertEqual(len(s["comments"][0]["text"]), ss.COMMENT_MAX)
        self.assertEqual(len(s["comments"][0]["author"]), ss.AUTHOR_MAX)
        self.assertEqual(s["comments"][0]["likes"], 7)
        self.assertEqual(sum(s["sentiment"].values()), 100)
        self.assertNotIn("extra", s)
        self.assertLessEqual(abs(s["date"] - time.time()), 5)

    def test_percents(self):
        self.assertEqual(ss.percents({"positive": 61, "neutral": 25, "negative": 14}),
                         {"positive": 61, "neutral": 25, "negative": 14})
        self.assertEqual(ss.percents({"positive": 2, "neutral": 1, "negative": 0}),
                         {"positive": 67, "neutral": 33, "negative": 0})
        self.assertEqual(ss.percents({}), {})
        self.assertEqual(ss.percents({"positive": -5}), {})

    def test_date_forms(self):
        self.assertEqual(ss._date(1764000000), 1764000000)
        self.assertEqual(ss._date(1764000000000), 1764000000)
        self.assertEqual(ss._date("2025-11-24T16:00:00Z"), 1764000000)


class BotWiringTests(unittest.TestCase):
    def test_bot_registers_routes(self):
        import inspect

        import bot
        src = inspect.getsource(bot.health_http)
        self.assertIn("share_summary.setup(", src)


if __name__ == "__main__":
    unittest.main()
