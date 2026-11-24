"""Страховка: все экраны бота собираются в настоящую разметку Telegram (длинные русские
названия, вложенные папки), а сбой приходит владельцу сообщением.
Запуск: python -m unittest discover tests"""

import asyncio
import logging
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "smoke.db"))

import bot  # noqa: E402
import database as db  # noqa: E402
import keyboards as kb  # noqa: E402
from telethon import TelegramClient  # noqa: E402


def _post(cat, summary):
    return db.save_item(category=cat, content_type="text", summary=summary, original_text=summary,
                        file_id=None, message_id=1, chat_id=1)


def _all_data(markup) -> list[bytes]:
    out = []
    for row in getattr(markup, "rows", []) or []:
        for b in row.buttons:
            for obj in (b, getattr(b, "type", None)):
                raw = getattr(obj, "data", None)
                if isinstance(raw, (bytes, bytearray)):
                    out.append(bytes(raw))
    return out


class AllScreensBuildTests(unittest.TestCase):
    def setUp(self):
        self.tok = db.use_db_path(os.path.join(tempfile.mkdtemp(), "a.db"))
        db.init_db()
        long1 = "Технологии и гаджеты будущего"
        long2 = "Вайбкодинг и генерация кода нейросетями"
        for cat, grp in [(long1, long1), (long2, f"{long1}/{long2}"), ("Игры", "Развлечения/Игры"),
                         ("GTA", "Развлечения/Игры"), ("Заметки", "")]:
            for i in range(10):
                _post(cat, f"{cat} пост {i} с довольно длинным описанием для кнопки")
            db.set_category_path(cat, grp)

    def tearDown(self):
        db.reset_db_path(self.tok)

    def test_every_page_builds_and_fits_telegram(self):
        tree = bot._tree_with_tags()
        screens = [kb.root_categories_keyboard(tree), kb.service_keyboard()]
        for path in db.get_folder_paths():
            node = bot._find_folder(tree, path)
            twin = db.folder_twin(path)
            items = db.get_items_by_category(twin, limit=8) if twin else []
            screens.append(kb.folder_page_keyboard(node, path.rpartition("/")[0], twin, items, len(items)))
        for c in db.get_categories():
            items = db.get_items_by_category(c["category"], limit=8)
            screens.append(kb.items_keyboard(items, c["category"], 0, 2, folder_path=c["group"]))
            screens.append(kb.items_keyboard(items, c["category"], 1, 2, selection={"ids": set()}))
        self.assertGreater(len(screens), 8)
        for rows in screens:
            markup = TelegramClient.build_reply_markup(rows)   # падает так же, как падал бот
            for raw in _all_data(markup):
                self.assertLessEqual(len(raw), 64, raw)


class OwnerAlertTests(unittest.TestCase):
    def test_error_reaches_owner_once_per_hour(self):
        sent = []

        async def fake_send(text):
            sent.append(text)

        orig = bot._send_owner_alert
        bot._send_owner_alert = fake_send
        bot._alert_last.clear()
        try:
            async def run():
                log = logging.getLogger("bot")
                for _ in range(3):
                    try:
                        raise ValueError("Too many bytes for the data")
                    except ValueError:
                        log.exception("Ошибка в обработчике on_callback (кнопка fold|Технологии)")
                await asyncio.sleep(0.05)
            asyncio.run(run())
        finally:
            bot._send_owner_alert = orig
        self.assertEqual(len(sent), 1)                              # три одинаковых — одно сообщение
        self.assertIn("ValueError: Too many bytes", sent[0])
        self.assertIn("fold|Технологии", sent[0])
        self.assertIn("test_robustness.py", sent[0])                # где именно упало


if __name__ == "__main__":
    unittest.main()
