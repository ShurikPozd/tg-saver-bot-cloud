"""Личные папки, поиск по картинке, перевод. Без сети и Telegram.
Запуск: python -m unittest discover tests"""

import asyncio
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "smoke.db"))

import bot  # noqa: E402
import database as db  # noqa: E402
import postctx  # noqa: E402
import webarchive  # noqa: E402


def _post(cat, text, ctype="text"):
    return db.save_item(category=cat, content_type=ctype, summary=text[:40], original_text=text,
                        file_id=None, message_id=1, chat_id=1)


class Base(unittest.TestCase):
    def setUp(self):
        self.tok = db.use_db_path(os.path.join(tempfile.mkdtemp(), "a.db"))
        db.init_db()

    def tearDown(self):
        db.reset_db_path(self.tok)


class PrivateTests(Base):
    def test_hidden_until_code_and_never_for_guests(self):
        a = _post("Дневник", "личное")
        b = _post("Мемы", "кот")
        db.set_category_path("Дневник", "Личное/Дневник")
        db.create_folder("Личное")
        db.set_private_paths(["Личное"])
        self.assertEqual(db.private_categories(), {"Дневник"})
        d = webarchive.archive_data({}, "", None)
        self.assertEqual([i["id"] for i in d["items"]], [b])          # без кода — не видно
        self.assertNotIn("Личное/Дневник", d["folders"])
        self.assertEqual(d["pv"], {"has": True, "open": False, "paths": []})
        d2 = webarchive.archive_data({}, "", None, show_private=True)
        self.assertEqual({i["id"] for i in d2["items"]}, {a, b})      # с кодом — видно
        g = webarchive.archive_data({}, "", None, {"kind": "folder", "val": "Личное"}, show_private=True)
        self.assertEqual(g["items"], [])                               # гостю — никогда

    def test_pin_and_token(self):
        db.set_private_pin("4815")
        self.assertTrue(db.check_private_pin("4815"))
        self.assertFalse(db.check_private_pin("0000"))
        self.assertNotIn("4815", db.get_setting("private_pin"))      # хранится только отпечаток
        t = webarchive.make_pv_token(5)
        self.assertTrue(webarchive.check_pv_token(5, t))
        self.assertFalse(webarchive.check_pv_token(6, t))            # чужому пользователю не подходит
        self.assertFalse(webarchive.check_pv_token(5, "1.abc"))

    def test_brute_force_pause(self):
        db.set_private_pin("4815")
        bot._pv_fails.clear()
        for _ in range(5):
            self.assertFalse(bot.web_private_unlock(9, "0000")["ok"])
        self.assertIn("10 минут", bot.web_private_unlock(9, "4815")["error"])   # даже верный — после 5 ошибок ждём


class ImageSearchTests(Base):
    def test_same_picture_found_first(self):
        from PIL import Image

        def png(color, size=(400, 300), stripe=False):
            from PIL import ImageDraw
            im = Image.new("RGB", size, color)
            if stripe:   # крупные фигуры, как на настоящей картинке (тонкие линии при уменьшении расплываются)
                w, h = size
                dr = ImageDraw.Draw(im)
                dr.rectangle([w * 0.1, h * 0.1, w * 0.45, h * 0.6], fill=(255, 255, 255))
                dr.ellipse([w * 0.55, h * 0.3, w * 0.9, h * 0.9], fill=(20, 20, 160))
                dr.rectangle([w * 0.2, h * 0.7, w * 0.5, h * 0.95], fill=(30, 160, 30))
            buf = io.BytesIO()
            im.save(buf, "PNG")
            return buf.getvalue()
        import imghash
        a = _post("Мемы", "мем", "photo")
        pic = png((200, 30, 30), stripe=True)
        with db.get_connection() as conn:
            conn.execute(db._IMG_HASH_DDL)
            conn.execute("INSERT INTO image_hashes (item_id, hash) VALUES (?, ?)", (a, imghash.dhash(pic)))
            conn.commit()
        big = png((200, 30, 30), size=(1600, 1200), stripe=True)        # та же картинка, но большая
        res = bot.web_image_search(big)
        self.assertEqual(res[0]["id"], a)
        self.assertLessEqual(res[0]["d"], bot.IMG_SAME_BITS)
        self.assertEqual(bot.web_image_search(b"not an image"), [])
        self.assertTrue(bot.IMG_SEARCH_RE.match("найди"))
        self.assertTrue(bot.IMG_SEARCH_RE.match("было?"))
        self.assertFalse(bot.IMG_SEARCH_RE.match("найди мне кота и собаку в архиве"))


class TranslateTests(Base):
    def test_foreign_detection_and_cache(self):
        self.assertTrue(postctx.is_foreign("When you finally fix the bug and three new ones appear #devlife"))
        self.assertFalse(postctx.is_foreign("Когда починил баг, а появилось три новых"))
        self.assertFalse(postctx.is_foreign("GTA 6"))                    # мало букв — не предлагаем
        a = _post("Мемы", "When you finally fix the bug and three new ones appear")
        calls = []

        async def fake(text):
            calls.append(text)
            return "Когда наконец починил баг, а появилось три новых"
        with mock.patch.object(bot._cat, "translate_ru", fake):
            self.assertIn("починил", asyncio.run(bot.translate_item(a)))
            asyncio.run(bot.translate_item(a))
        self.assertEqual(len(calls), 1)                                  # второй раз — из памяти
        d = webarchive.archive_data({}, "", None)
        self.assertTrue(next(i for i in d["items"] if i["id"] == a)["fl"])


if __name__ == "__main__":
    unittest.main()
