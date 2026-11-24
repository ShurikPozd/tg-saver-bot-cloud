"""/api/limits: запас бесплатного ИИ для расширения. Запуск: python -m unittest discover tests"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import categorizer as c  # noqa: E402

QWEN = "qwen/qwen3.8-27b"


class ExtLimitsTests(unittest.TestCase):
    def setUp(self):
        self.saved = (dict(c._bucket), dict(c._day_limit), dict(c._synced))

    def tearDown(self):
        for d, v in zip((c._bucket, c._day_limit, c._synced), self.saved):
            d.clear()
            d.update(v)

    def test_reports_main_and_fallback_after_groq_said_used(self):
        c._note_limit_error(QWEN, "Rate limit reached ... tokens per day (TPD): Limit 200000, Used 197335, Requested 2826.")
        r = c.ext_limits(QWEN)
        self.assertEqual(r["main"]["model"], QWEN)
        self.assertEqual(r["main"]["limit"], 200000)
        self.assertTrue(2600 <= r["main"]["left"] <= 2800)  # пополняется ~2,3 ток/с
        self.assertTrue(r["main"]["synced"])
        self.assertEqual(r["fallback"]["model"], c.EXT_FALLBACK_MODEL)
        self.assertIn("cloudflare", r)

    def test_no_fallback_when_main_is_the_fallback(self):
        r = c.ext_limits(c.EXT_FALLBACK_MODEL)
        self.assertIsNone(r["fallback"])

    def test_route_registered(self):
        import inspect

        import bot

        src = inspect.getsource(bot.health_http)
        self.assertIn('add_get("/api/limits", handler_api_limits)', src)


if __name__ == "__main__":
    unittest.main()
