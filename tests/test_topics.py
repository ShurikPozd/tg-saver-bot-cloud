"""ИИ-темы: разбор ответа, хранение, поиск, пакетная доразметка, данные архива.
Запуск: python -m unittest discover tests"""

import asyncio
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DB_PATH", os.path.join(tempfile.mkdtemp(), "smoke.db"))

import categorizer  # noqa: E402
import database as db  # noqa: E402
import webarchive  # noqa: E402


def _post(cat, summary):
    return db.save_item(category=cat, content_type="text", summary=summary, original_text=summary,
                        file_id=None, message_id=1, chat_id=1)


class TopicsTests(unittest.TestCase):
    def setUp(self):
        self.tok = db.use_db_path(os.path.join(tempfile.mkdtemp(), "a.db"))
        db.init_db()
        self.gta = _post("Игры", "Rockstar показала новый трейлер")
        self.cat = _post("Смешное", "кот в коробке")

    def tearDown(self):
        db.reset_db_path(self.tok)

    def test_parse_keeps_names_drops_generic(self):
        raw = json.dumps({"category": "Игры", "summary": "Трейлер", "confidence": 0.9,
                          "topics": ["GTA 6", "игры", "«Rockstar Games»", "gta 6", "Мем", "Switch 2", "лишнее"]})
        self.assertEqual(categorizer._parse(raw, "x")["topics"], ["GTA 6", "Rockstar Games", "Switch 2"])
        self.assertEqual(categorizer._parse('{"category": "Игры", "summary": "s"}', "x")["topics"], [])

    def test_search_finds_by_topic(self):
        db.set_item_topics({self.gta: ["GTA 6", "Rockstar Games"], self.cat: []})
        self.assertEqual([i["id"] for i in db.search_items("gta")], [self.gta])   # в тексте «GTA» нет
        self.assertEqual([i["id"] for i in db.search_items("гта")], [self.gta])   # и транслитом
        self.assertEqual(db.item_topics()[self.cat], [])                          # размечен, без тем

    def test_batch_backfill(self):
        async def fake_chat(messages, **kw):
            return json.dumps({"topics": {str(self.gta): ["GTA 6"], str(self.cat): ["новости"], "999": ["X"]}})
        orig = categorizer._groq_chat
        categorizer._groq_chat = fake_chat
        try:
            got = asyncio.run(categorizer.extract_topics(db.items_for_smart_search()))
        finally:
            categorizer._groq_chat = orig
        self.assertEqual(got, {self.gta: ["GTA 6"], self.cat: []})               # чужой id и общее слово — мимо

    def test_archive_data_and_filter(self):
        db.set_item_topics({self.gta: ["GTA 6"]})
        data = webarchive.archive_data({}, "b")
        tp = {i["id"]: i["tp"] for i in data["items"]}
        self.assertEqual(tp[self.gta], ["GTA 6"])
        self.assertIn('sel.kind==="topic"', webarchive.PAGE_HTML)
        self.assertIn("🏷 Темы", webarchive.PAGE_HTML)


if __name__ == "__main__":
    unittest.main()
