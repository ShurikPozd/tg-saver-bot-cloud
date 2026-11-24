"""Расширение (/api/chat): суточный лимит основной модели → запасная Groq (gpt-oss-20b) →
Cloudflare. И для обычного ответа, и для стрима. Локальная заглушка, без ключей и сети.
Запуск: python -m unittest discover tests"""

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import categorizer as c  # noqa: E402

DAILY = ("Rate limit reached for model `qwen/qwen3.8-27b` on tokens per day (TPD): Limit 200000, "
         "Used 197335, Requested 2826. Please try again in 1m9.552s.")


class ExtFallbackTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from aiohttp import web

        self.models = []
        self.cf = 0
        self.fb_ok = True

        async def groq(request):
            body = await request.json()
            m = body["model"]
            self.models.append(m)
            if m != c.EXT_FALLBACK_MODEL or not self.fb_ok:
                return web.json_response({"error": {"message": DAILY.replace("qwen/qwen3.8-27b", m)}}, status=429)
            # у gpt-oss рассуждения: запас сверх max_tokens
            self.assertGreater(body["max_tokens"], 500)
            self.assertEqual(body["reasoning_effort"], "low")
            if body.get("stream"):
                resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
                await resp.prepare(request)
                for part in ("Зрители ", "довольны."):
                    await resp.write(f"data: {json.dumps({'choices': [{'delta': {'content': part}}]})}\n\n".encode())
                await resp.write(b"data: [DONE]\n\n")
                return resp
            return web.json_response({"choices": [{"message": {"content": '{"ok": 1}'}}], "usage": {"total_tokens": 100}})

        async def cf(request):
            self.cf += 1
            return web.json_response({"choices": [{"message": {"content": '{"ok": "cf"}'}}], "usage": {"total_tokens": 50}})

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

    MSGS = [{"role": "system", "content": "Ответь JSON"}, {"role": "user", "content": "x"}]

    async def test_chat_switches_to_fallback_model(self):
        r = await c.llm_chat(self.MSGS, model="qwen/qwen3.8-27b", json_mode=True, max_tokens=300, timeout=10)
        self.assertEqual(r, '{"ok": 1}')
        self.assertEqual(self.models, ["qwen/qwen3.8-27b", c.EXT_FALLBACK_MODEL])
        self.assertEqual(self.cf, 0)

    async def test_chat_cloudflare_when_both_models_empty(self):
        self.fb_ok = False
        r = await c.llm_chat(self.MSGS, model="qwen/qwen3.8-27b", json_mode=True, max_tokens=300, timeout=10)
        self.assertIn("cf", r)
        self.assertEqual(self.cf, 1)

    async def test_stream_switches_to_fallback_model(self):
        out = [d async for d in c.llm_chat_stream(self.MSGS, model="qwen/qwen3.8-27b", max_tokens=300, timeout=10)]
        self.assertEqual("".join(out), "Зрители довольны.")
        self.assertEqual(self.models, ["qwen/qwen3.8-27b", c.EXT_FALLBACK_MODEL])

    async def test_stream_cloudflare_when_both_models_empty(self):
        self.fb_ok = False
        out = [d async for d in c.llm_chat_stream(self.MSGS, model="qwen/qwen3.8-27b", max_tokens=300, timeout=10)]
        self.assertIn("cf", "".join(out))
        self.assertEqual(self.cf, 1)
        # суточный лимит не ждём минутами: по одному запросу на модель
        self.assertEqual(self.models, ["qwen/qwen3.8-27b", c.EXT_FALLBACK_MODEL])


if __name__ == "__main__":
    unittest.main()
