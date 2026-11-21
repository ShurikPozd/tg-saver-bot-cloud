"""Полный разбор присланного поста перед категоризацией.

analyze(client, msgs, content_type) собирает из сообщения (или альбома) всё,
что помогает понять тему: чистый текст без подвалов, описание картинок с текстом
на них, кадр и речь из видео, превью ссылок, кнопки, опросы, файлы, музыку.
"""

from __future__ import annotations

import asyncio
import logging

import linkmeta
import media
import postctx
from categorizer import describe_image
from stt import STT_MAX_BYTES, transcribe_audio

logger = logging.getLogger(__name__)

# Подпись короче этого (в буквах) — смотрим ещё и на саму картинку/видео.
VISION_IF_LETTERS_BELOW = 150
IMAGE_DOC_MAX = 15 * 1024 * 1024
# Сколько букв речи достаточно, чтобы классифицировать видео без описания кадра.
SPEECH_ENOUGH_LETTERS = 60


def _file_size(msg) -> int:
    try:
        return int(getattr(getattr(msg, "file", None), "size", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _is_image_doc(msg) -> bool:
    mime = (getattr(getattr(msg, "file", None), "mime_type", "") or "").lower()
    return bool(getattr(msg, "document", None)) and mime.startswith("image/") and not getattr(msg, "sticker", None)


async def _download(client, msg, timeout: float = 60, **kw) -> bytes | None:
    try:
        return await asyncio.wait_for(client.download_media(msg, file=bytes, **kw), timeout)
    except Exception as e:
        logger.info("Не скачалось медиа (%s): %s", kw or "file", e)
        return None


async def _describe(client, msg, kind: str) -> str:
    data = await _download(client, msg)
    if not data:
        return ""
    try:
        return await asyncio.wait_for(describe_image(data, kind=kind), 110)
    except Exception:
        return ""


async def _video_bits(client, msg, want_frame: bool, want_audio: bool) -> tuple[str, str]:
    """(описание кадра, расшифровка речи) для видео/гифки."""
    frame_desc, speech = "", ""
    size = _file_size(msg)
    duration = getattr(getattr(msg, "file", None), "duration", None)
    frame = None
    if media.FFMPEG and 0 < size <= media.VIDEO_DOWNLOAD_MAX and (want_frame or want_audio):
        with media.TempFile(".mp4") as path:
            try:
                await asyncio.wait_for(client.download_media(msg, file=path), 120)
                if want_frame:
                    frame = await media.extract_frame(path, duration)
                if want_audio:
                    audio = await media.extract_audio(path)
                    if audio:
                        speech = await asyncio.wait_for(
                            transcribe_audio(audio, language=None, filename="audio.mp3", content_type="audio/mpeg"),
                            150,
                        )
            except Exception as e:
                logger.info("Видео не разобрано целиком: %s", e)
    elif want_audio and 0 < size <= STT_MAX_BYTES:
        # без ffmpeg: Whisper сам вытащит звук из небольшого mp4
        data = await _download(client, msg, timeout=90)
        if data:
            speech = await transcribe_audio(data, language=None, filename="video.mp4", content_type="video/mp4")
    if want_frame and not frame:
        frame = await _download(client, msg, thumb=-1)  # превью-кадр от Telegram
    if frame:
        try:
            frame_desc = await asyncio.wait_for(describe_image(frame, kind="кадре из видео"), 110)
        except Exception:
            frame_desc = ""
    return frame_desc, speech


async def analyze(
    client,
    msgs: list,
    content_type: str,
    *,
    vision_on: bool = True,
    audio_on: bool = True,
    link_enrich_on: bool = True,
) -> dict:
    """→ {"display": текст поста как есть (markdown), "llm_text": вход для ИИ,
    "aux": распознанное (для хранения), "source": название канала, "username": @канал}"""
    msgs = [m for m in msgs if m is not None]
    texts = []
    for m in msgs:
        t = (getattr(m, "text", None) or "").strip()
        if t and t not in texts:
            texts.append(t)
    display = "\n\n".join(texts)
    clean = postctx.clean_post_text(display)
    letters = postctx.content_letters(clean["text"])
    need_media = letters < VISION_IF_LETTERS_BELOW

    extras: list[str] = []
    for m in msgs:
        for line in postctx.message_extras(m):
            if line not in extras:
                extras.append(line)

    source, username = (None, None)
    if msgs:
        source, username = postctx.forward_source(msgs[0])

    vision: list[tuple[str, str]] = []
    speech = ""
    vision_wanted = False  # пытались описать картинку/кадр (для решения «сбой → повторить позже»)

    if vision_on and need_media:
        photos = [m for m in msgs if getattr(m, "photo", None) or _is_image_doc(m)]
        photos = [m for m in photos if not _is_image_doc(m) or _file_size(m) <= IMAGE_DOC_MAX]
        # в альбоме без подписи смотрим 2 картинки, иначе одну
        limit = 2 if (len(photos) > 1 and postctx.is_weak_text(clean["text"])) else 1
        vision_wanted = vision_wanted or bool(photos)
        for i, m in enumerate(photos[:limit]):
            desc = await _describe(client, m, "изображении")
            if desc:
                label = "Изображение" if limit == 1 else f"Изображение {i + 1}"
                vision.append((label, desc))

    videos = [
        m for m in msgs
        if (getattr(m, "video", None) or getattr(m, "gif", None)) and not getattr(m, "video_note", None)
    ]
    if videos and need_media and (vision_on or audio_on):
        v = videos[0]
        is_gif = bool(getattr(v, "gif", None))
        vision_wanted = vision_wanted or vision_on
        frame_desc, sp = await _video_bits(
            client, v,
            want_frame=vision_on,
            want_audio=audio_on and not is_gif,
        )
        if frame_desc:
            vision.append(("Кадр из видео", frame_desc))
        speech = sp or speech

    if audio_on and postctx.is_weak_text(clean["text"]):
        for m in msgs:
            if getattr(m, "voice", None) or getattr(m, "video_note", None):
                data = await _download(client, m, timeout=90)
                if data:
                    if getattr(m, "video_note", None):
                        speech = await transcribe_audio(data, filename="video_note.mp4", content_type="video/mp4")
                    else:
                        speech = await transcribe_audio(data)
                break
            if getattr(m, "audio", None):
                f = getattr(m, "file", None)
                if getattr(f, "performer", None) or getattr(f, "title", None):
                    break  # музыка: исполнитель/название уже в extras, Whisper не нужен
                size = _file_size(m)
                if size > STT_MAX_BYTES and media.FFMPEG and size <= media.VIDEO_DOWNLOAD_MAX:
                    with media.TempFile(".bin") as path:
                        try:
                            await asyncio.wait_for(client.download_media(m, file=path), 120)
                            audio = await media.extract_audio(path)
                        except Exception:
                            audio = None
                    if audio:
                        speech = await transcribe_audio(audio, language=None, filename="audio.mp3", content_type="audio/mpeg")
                elif size <= STT_MAX_BYTES:
                    data = await _download(client, m, timeout=90)
                    if data:
                        speech = await transcribe_audio(data, language=None)
                break

    link_lines: list[str] = []
    has_preview = any(x.startswith("Превью ссылки") for x in extras)
    if link_enrich_on and clean["links"] and not has_preview and letters < 400:
        try:
            link_lines = await asyncio.wait_for(
                linkmeta.fetch_link_meta([u for _, u in clean["links"]]), 25
            )
        except Exception:
            link_lines = []

    llm_text = postctx.build_llm_text(
        clean, extras + link_lines, vision, speech=speech, channel_username=username
    )
    aux_lines = [f"{label}: {desc}" for label, desc in vision if desc]
    if speech:
        aux_lines.append(f"Речь: {speech[:1200]}")
    aux_lines.extend(extras)
    aux_lines.extend(link_lines)
    return {
        "display": display,
        "llm_text": llm_text,
        "aux": "\n".join(aux_lines),
        "source": source,
        "username": username,
        "weak": postctx.is_weak_text(clean["text"]) and not vision and not speech,
        # подписи нет, а картинку описать не вышло (лимиты Groq) — классифицировать
        # вслепую бессмысленно: сохраним как есть и дораспознаем в фоне
        # (обрывок речи тоже не спасает: видео с крысами ушло в «Видео» как «Outro Music»)
        "media_failed": vision_wanted and not vision and postctx.is_weak_text(clean["text"])
        and postctx.content_letters(speech) < SPEECH_ENOUGH_LETTERS,
    }


def llm_text_from_stored(stored_text: str) -> str:
    """Вход для ИИ из уже сохранённого original_text (без повторного скачивания)."""
    stored_text = stored_text or ""
    if postctx.has_aux(stored_text):
        display = postctx.strip_aux(stored_text)
        aux = postctx.aux_part(stored_text)
    else:
        # старый формат: «(по ссылке: …)», «(изображение: …)» дописаны прямо в текст —
        # они полезны ИИ, поэтому оставляем их как есть
        display, aux = stored_text, ""
    clean = postctx.clean_post_text(display)
    parts = [postctx.build_llm_text(clean, [], [])]
    if aux:
        parts.append(aux)
    return "\n".join(p for p in parts if p).strip()
