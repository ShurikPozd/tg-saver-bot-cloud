"""Cloudflare как третий уровень: Groq (суточный лимит) → Cloudflare. Локальная заглушка,
без настоящих ключей и сети. Запуск: python -m unittest discover tests"""

import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import categorizer as c  # noqa: E402

DAILY = ("Rate limit reached for model `x` on tokens per day (TPD): Limit 200000, Used 199900, "
         "Requested 2000. Please try again in 9m0s.")


class CloudflareFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp import web

        self.hits = {"groq": 0, "cf": 0}
        self.cf_daily = False

        async def groq(request):
            self.hits["groq"] += 1
            return web.json_response({"error": {"message": DAILY}}, status=429)

        async def cf(request):
            self.hits["cf"] += 1
            if self.cf_daily:
                return web.json_response({"errors": [{"code": 4006, "message": "daily free allocation used"}]}, status=429)
            body = await request.json()
            if "qwen3" in body["model"]:
                self.assertTrue(body["messages"][-1]["content"].endswith("/no_think"))
            content = '<think>{черновик}</think>{"category": "Смешное", "summary": "Крысы на кухне", "confidence": 0.8}'
            return web.json_response({"choices": [{"message": {"content": content}}], "usage": {"total_tokens": 1900}})

        app = web.Application()
        app.router.add_post("/openai/v1/chat/completions", groq)
        app.router.add_post("/client/v4/accounts/acc/ai/v1/chat/completions", cf)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.saved = {k: getattr(c, k) for k in ("GROQ_BASE_URL", "GROQ_PROXY", "CF_ENABLED", "CF_ACCOUNT_ID",
                                                  "CF_API_TOKEN", "CF_BASE_URL")}
        c.GROQ_BASE_URL = f"http://127.0.0.1:{port}/openai/v1"
        c.GROQ_PROXY = None
        c.CF_ENABLED, c.CF_ACCOUNT_ID, c.CF_API_TOKEN = True, "acc", "tok"
        c.CF_BASE_URL = f"http://127.0.0.1:{port}/client/v4"
        for st in c._EXTRA_STATE.values():
            st["blocked_until"] = 0.0

    async def asyncTearDown(self):
        for k, v in self.saved.items():
            setattr(c, k, v)
        await self.runner.cleanup()

    async def test_new_post_uses_cloudflare_when_groq_is_empty(self):
        r = await c.categorize("Две крысы на кухне", content_type="video", categories=["Смешное", "Игры"])
        self.assertTrue(r["llm_ok"])
        self.assertEqual(r["category"], "Смешное")
        self.assertEqual(self.hits["cf"], 1)

    async def test_background_queue_does_not_touch_cloudflare(self):
        async def bg():
            c.USAGE_SOURCE.set("bg")
            return await c.categorize("x", content_type="text", categories=["Игры"])
        r = await asyncio.create_task(bg())
        self.assertFalse(r["llm_ok"])
        self.assertEqual(self.hits["cf"], 0)

    async def test_cloudflare_daily_limit_pauses_until_reset(self):
        self.cf_daily = True
        r = await c.categorize("x", content_type="text", categories=["Игры"])
        self.assertFalse(r["llm_ok"])
        self.assertFalse(c.cf_available())
        await c.categorize("y", content_type="text", categories=["Игры"])
        self.assertEqual(self.hits["cf"], 1)



if __name__ == "__main__":
    unittest.main()
