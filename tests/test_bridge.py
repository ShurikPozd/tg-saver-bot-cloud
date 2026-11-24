"""Мост «бот ↔ мини-приложение»: экраны и кнопки бота в приложении. Без Telegram — клиент подменён.
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
import pending  # noqa: E402

UID = 777000


class FakeClient:
    """Всё, что ушло бы в Telegram мимо моста, — сюда (в тесте такого быть не должно)."""

    def __init__(self):
        self.real = []

    async def send_message(self, entity, message="", *a, **kw):
        self.real.append(("send", entity, message))

    async def edit_message(self, entity, message=None, text=None, *a, **kw):
        self.real.append(("edit", entity, text))

    async def send_file(self, entity, file=None, *a, **kw):
        self.real.append(("file", entity))

    async def delete_messages(self, entity, ids, *a, **kw):
        self.real.append(("del", entity, ids))


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tok = db.use_db_path(os.path.join(tempfile.mkdtemp(), "a.db"))
        db.init_db()
        self.fake = FakeClient()
        p = mock.patch.object(bot, "client", self.fake)
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(lambda: [pending.pop(UID) for _ in range(20)])
        p2 = mock.patch.object(db, "set_current_user", lambda *_a, **_k: None)   # одна тестовая БД на всё
        p2.start()
        self.addCleanup(p2.stop)
        p3 = mock.patch.object(db, "register_user", lambda *_a, **_k: None)      # не трогать список юзеров
        p3.start()
        self.addCleanup(p3.stop)

    def tearDown(self):
        db.reset_db_path(self.tok)

    def call(self, **body):
        return asyncio.run(bot.web_bridge(UID, body))

    def test_service_screen_and_buttons(self):
        r = self.call(kind="cmd", name="service")
        self.assertEqual(r["out"][0]["op"], "new")
        self.assertIn("Сервис", r["out"][0]["text"])
        datas = [b.get("d") for row in r["out"][0]["rows"] for b in row]
        self.assertIn("svc_order", datas)
        self.assertEqual(self.fake.real, [])                               # в Telegram ничего не ушло

    def test_settings_toggle_edits_same_card(self):
        r = self.call(kind="cmd", name="settings")
        card = r["out"][-1]
        btn = next(b for row in card["rows"] for b in row if b.get("d", "").startswith("settoggle|daily_read"))
        before = db.get_setting("daily_read", "1")
        r2 = self.call(kind="cb", data=btn["d"], src=card["id"])
        self.assertNotEqual(db.get_setting("daily_read", "1"), before)
        self.assertTrue(any(m["op"] == "edit" and m["id"] == card["id"] for m in r2["out"]))
        self.assertEqual(self.fake.real, [])

    def test_post_card_and_rename_via_text(self):
        iid = db.save_item(category="Игры", content_type="text", summary="старое название", original_text="текст",
                           file_id=None, message_id=None, chat_id=None)
        r = self.call(kind="cb", data=f"view:{iid}")
        card = r["out"][-1]
        self.assertIn("старое название", card["text"])
        datas = [b.get("d") for row in card["rows"] for b in row]
        self.assertIn(f"renitem:{iid}", datas)
        self.assertIn(f"fav:{iid}", datas)
        r2 = self.call(kind="cb", data=f"renitem:{iid}", src=card["id"])
        self.assertTrue(r2["ask"])                                          # бот ждёт текст
        self.call(kind="text", text="новое название")
        self.assertEqual(db.get_item(iid)["summary"], "новое название")
        self.assertEqual(self.fake.real, [])

    def test_background_sends_after_call_go_to_telegram(self):
        self.call(kind="cmd", name="help")
        asyncio.run(bot.client.send_message(UID, "фон"))
        self.assertEqual(self.fake.real, [("send", UID, "фон")])


if __name__ == "__main__":
    unittest.main()
