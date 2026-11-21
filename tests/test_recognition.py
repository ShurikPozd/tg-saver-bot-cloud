"""Тесты разбора постов и лечения базы. Запуск: python -m unittest discover tests"""

import json
import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import postctx as pc  # noqa: E402
import stt  # noqa: E402
from categorizer import _parse  # noqa: E402


class FooterTests(unittest.TestCase):
    def test_channel_footer_only_is_dropped(self):
        # реальные подписи из архива: от поста остаётся только подвал канала
        cases = [
            "[**Крохи**](https://t.me/+_gtc4yyi_oEwMjgy)** | **[**Сделать стикеры**](https://t.me/tinycatstik_bot)",
            "[ANIMUDA](https://t.me/+rolghcUTHyk2OGE6) | [private](https://t.me/elonmood_bot)",
            "😭 [**dankweeb**](https://t.me/+UyLBWzwfPMNjMzYy)** | **[**private**](https://t.me/+omPtU7Sz6WwxNjI6) 😏",
            "[Берлога скуфоф](https://t.me/berlogaskufof)",
        ]
        for md in cases:
            c = pc.clean_post_text(md)
            self.assertEqual(c["text"], "", md)
            self.assertTrue(pc.is_weak_text(c["text"]))
            self.assertEqual(c["links"], [])

    def test_news_keeps_body_and_content_links(self):
        md = (
            "**Razer анонсировала наушники Hammerhead V3** с подсветкой. [Подробнее](https://razer.com/hv3)\n\n"
            "[👉 Топор +18. Подписаться](https://t.me/+uS877FRTs6gzZWJi)"
        )
        c = pc.clean_post_text(md)
        self.assertIn("Razer анонсировала", c["text"])
        self.assertNotIn("Подписаться", c["text"])
        self.assertEqual(c["links"], [("Подробнее", "https://razer.com/hv3")])
        self.assertFalse(pc.is_weak_text(c["text"]))

    def test_post_link_is_not_footer(self):
        c = pc.clean_post_text("Смотри пост https://t.me/somechannel/123 очень интересный разбор игры")
        self.assertTrue(any("t.me/somechannel/123" in u for _, u in c["links"]))

    def test_hashtags_collected(self):
        c = pc.clean_post_text("Tags ► #Art #ElfGirl #art")
        self.assertEqual(c["hashtags"], ["Art", "ElfGirl"])

    def test_url_underscores_survive(self):
        c = pc.clean_post_text("https://www.youtube.com/watch?v=o_FJ1NIH9yw")
        self.assertEqual(c["links"][0][1], "https://www.youtube.com/watch?v=o_FJ1NIH9yw")


class AuxTests(unittest.TestCase):
    def test_roundtrip(self):
        stored = pc.join_stored("Текст поста", "Изображение: мем; кот")
        self.assertEqual(pc.strip_aux(stored), "Текст поста")
        self.assertEqual(pc.aux_part(stored), "Изображение: мем; кот")

    def test_media_without_caption(self):
        stored = pc.join_stored("", "Кадр из видео: кот")
        self.assertEqual(pc.strip_aux(stored), "")
        self.assertEqual(pc.aux_part(stored), "Кадр из видео: кот")

    def test_legacy_suffix_stripped(self):
        legacy = "https://youtu.be/x\n\n(по ссылке: «Видео» (Автор); Описание: что-то)"
        self.assertEqual(pc.strip_aux(legacy), "https://youtu.be/x")


class FileIdsRepairTests(unittest.TestCase):
    def setUp(self):
        import database as db

        self.db = db
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(self.path)
        self._orig = db.get_active_db_path
        db.get_active_db_path = lambda: self.path
        db._FILE_IDS_REPAIRED.discard(self.path)
        db._init_db_ddl()

    def tearDown(self):
        self.db.get_active_db_path = self._orig
        try:
            os.remove(self.path)
        except OSError:
            pass

    def test_nested_encoding_is_unwrapped(self):
        media = [{"t": "photo", "f": "AgAD_x", "u": "photo:1"}]
        v = json.dumps(media)
        for _ in range(7):  # 7 лишних слоёв, как после 7 перезапусков Render
            v = json.dumps(v)
        con = sqlite3.connect(self.path)
        con.execute(
            "INSERT INTO saved_items (id, category, content_type, file_ids) VALUES (1, 'Арты', 'photo', ?)", (v,)
        )
        con.commit()
        con.close()
        self.assertEqual(self.db.repair_file_ids(), 1)
        self.assertEqual(self.db.get_item(1)["file_ids"], media)

    def test_export_import_is_stable(self):
        media = [{"t": "video", "f": "BAAD", "u": "video:9"}]
        self.db.save_item("Игры", "video", "s", "t", "BAAD", 5, 6, file_ids=media)
        dump = self.db.export_json()
        base = self.path
        for i in range(3):  # три цикла «бэкап → восстановление» в новую пустую БД
            raw = json.dumps(dump, ensure_ascii=False).encode()
            fresh = f"{base}.{i}"
            self.db.get_active_db_path = lambda p=fresh: p
            self.db._init_db_ddl()
            self.db.import_json_stream(raw)
            dump = self.db.export_json()
        self.assertEqual(self.db.get_item(1)["file_ids"], media)
        self.assertEqual(json.loads(dump["items"][0]["file_ids"]), media)

    def test_dup_text_ignores_aux(self):
        stored = pc.join_stored("https://youtu.be/abc", "Превью ссылки: YouTube — Видео")
        self.db.save_item("Игры", "text", "s", stored, None, 1, 1)
        self.assertIsNotNone(self.db.find_dup_text("https://youtu.be/abc"))
        self.assertIsNotNone(self.db.find_dup_text("Https://YOUTU.be/abc"))

    def test_similar_items(self):
        self.db.save_item("Игры", "text", "Ремейк Silent Hill анонсирован для PS5", "", None, 1, 1)
        self.db.save_item("Кулинария", "text", "Рецепт борща с говядиной", "", None, 2, 1)
        sim = self.db.similar_items("Silent Hill: новый трейлер ремейка для PS5 и анонс даты", limit=2)
        self.assertTrue(sim and sim[0][1] == "Игры", sim)


class SttCleanupTests(unittest.TestCase):
    def test_hallucination_dropped(self):
        data = {"segments": [{"text": "Продолжение следует...", "no_speech_prob": 0.2, "avg_logprob": -0.3}]}
        self.assertEqual(stt._clean_transcript(data), "")

    def test_silence_dropped(self):
        data = {"segments": [{"text": "Спасибо.", "no_speech_prob": 0.9, "avg_logprob": -1.2}]}
        self.assertEqual(stt._clean_transcript(data), "")

    def test_speech_kept(self):
        seg = {"text": "Сегодня разбираем новую видеокарту от Nvidia", "no_speech_prob": 0.01, "avg_logprob": -0.2}
        self.assertIn("видеокарту", stt._clean_transcript({"segments": [seg]}))


class ParseTests(unittest.TestCase):
    def test_confidence_and_alternatives(self):
        r = _parse('{"category": "Игры", "summary": "x", "confidence": 0.4, "alternatives": ["Технологии", "Игры"]}', "fb")
        self.assertEqual(r["category"], "Игры")
        self.assertAlmostEqual(r["confidence"], 0.4)
        self.assertEqual(r["alternatives"], ["Технологии"])

    def test_missing_confidence_defaults_high(self):
        r = _parse('{"category": "Игры", "summary": "x"}', "fb")
        self.assertGreaterEqual(r["confidence"], 0.6)


class WhisperNoiseTests(unittest.TestCase):
    def test_music_captions_are_not_speech(self):
        # так Whisper «озвучивает» видео без речи (крысы ушли в «Видео» как «Outro Music»)
        for t in ["Outro Music", "ВЕСЁЛАЯ МУЗЫКА", "[Music] [Applause]", "♪♪♪", "Тихая музыка играет", "(смех)"]:
            self.assertEqual(stt._clean_transcript({"text": t}), "", t)

    def test_real_speech_kept(self):
        self.assertEqual(stt._clean_transcript({"text": "Тихие крысы прыгают за едой"}), "Тихие крысы прыгают за едой")
        self.assertEqual(stt._clean_transcript({"text": "(смех) Ну ты даёшь, смотри что творит"}), "Ну ты даёшь, смотри что творит")

    def test_noise_regex_is_linear(self):
        import time

        t0 = time.time()
        stt._is_noise("музыка, " * 400 + "крыса")
        self.assertLess(time.time() - t0, 0.5)


class GroqLimitTests(unittest.TestCase):
    def test_retry_after_formats(self):
        from categorizer import retry_after_seconds as ra

        self.assertAlmostEqual(ra("Please try again in 6m2.4s."), 362.4)
        self.assertAlmostEqual(ra("try again in 1h2m3s"), 3723)
        self.assertAlmostEqual(ra("try again in 20.4s"), 20.4)
        self.assertAlmostEqual(ra("try again in 337.5ms"), 0.3375)
        self.assertIsNone(ra("no hint"))

    def test_daily_limit_learned_from_429(self):
        import categorizer as c

        m = "test/model-x"
        c._usage.pop(m, None)
        self.assertEqual(c.day_headroom(m), c.DAY_TOKENS_DEFAULT)
        err = ('Rate limit reached for model `x` on tokens per day (TPD): Limit 200000, '
               'Used 197813, Requested 2289. Please try again in 44.064s.')
        self.assertTrue(c.is_daily_limit(err))
        self.assertFalse(c.is_daily_limit("on tokens per minute (TPM): Limit 8000"))
        c._note_limit_error(m, err)
        self.assertEqual(c.day_headroom(m), 200000 - 197813)
        c._note_usage(m, 100)
        self.assertEqual(c.tokens_used_today(m), 197913)
        c._usage.pop(m, None)


if __name__ == "__main__":
    unittest.main()
