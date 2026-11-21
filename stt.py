import aiohttp
import asyncio
import logging
import re

from config import (
    GROQ_API_KEY,
    GROQ_BASE_URL,
    GROQ_STT_MODEL,
    GROQ_PROXY,
    GROQ_TIMEOUT_SEC,
)

logger = logging.getLogger(__name__)

_LOCK = asyncio.Lock()

# Бесплатный Groq принимает файлы до 25 МБ.
STT_MAX_BYTES = 24 * 1024 * 1024

# Whisper на тишине/музыке «галлюцинирует» титрами из обучающих субтитров.
_HALLUCINATIONS = re.compile(
    r"продолжение следует|субтитры (сделал|создавал|подготов|делал)|редактор субтитров|"
    r"dimatorzok|спасибо за (просмотр|внимание)|подписывайтесь на (наш )?канал|"
    r"ставьте лайк|amara\.org|thanks for watching|please subscribe|"
    r"корректор [а-яё.]+|до новых встреч|игорь негода",
    re.IGNORECASE,
)

# Пометки вместо речи: «[Music]», «(смех)», «♪», «*аплодисменты*».
_TAGS = re.compile(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*|[♪♫🎵🎶]+")
# Строка целиком из «звуковых» слов: «Outro Music», «ВЕСЁЛАЯ МУЗЫКА», «Аплодисменты» —
# так Whisper подписывает видео без речи (было: «Видеофайл с Outro Music» вместо крыс).
_NOISE_WORD = (
    r"(?:outro|intro|music|musical|upbeat|background|soft|gentle|dramatic|epic|piano|"
    r"instrumental|applause|laughter|laughing|laughs|cheering|silence|noise|sound|sounds|"
    r"playing|plays|song|beat|bass|the|and|of|"
    r"музык\w*|музыкальн\w*|заставк\w*|весёл\w*|весел\w*|динамичн\w*|спокойн\w*|тих\w*|"
    r"грустн\w*|печальн\w*|напряж\w*|торжествен\w*|ритмичн\w*|энергичн\w*|лиричн\w*|"
    r"фонов\w*|мелоди\w*|песн\w*|играет|звучит|звук\w*|шум\w*|тишин\w*|аплодисмент\w*|"
    r"смех|смеётся|смеется|смеются|крик\w*|вздох\w*|и)"
)
# \b после слова и разделитель только справа — проверка линейная (без лавинного перебора).
_NOISE_ONLY = re.compile(rf"^[\W_]*(?:{_NOISE_WORD}\b[\W_]*)+$", re.IGNORECASE)


def _is_noise(txt: str) -> bool:
    rest = _TAGS.sub(" ", txt or "").strip()
    return not re.search(r"[^\W\d_]", rest) or bool(_NOISE_ONLY.match(rest))


def _connector():
    """Коннектор с учётом прокси (socks5/http) или None для прямого соединения."""
    if not GROQ_PROXY:
        return None
    if GROQ_PROXY.startswith("socks"):
        from aiohttp_socks import ProxyConnector

        return ProxyConnector.from_url(GROQ_PROXY)
    return aiohttp.ProxyConnector.from_url(GROQ_PROXY)


def _clean_transcript(data: dict) -> str:
    """Оставляет только сегменты с речью и выкидывает типовые галлюцинации Whisper."""
    segments = data.get("segments") or []
    if segments:
        kept = []
        for s in segments:
            txt = (s.get("text") or "").strip()
            if not txt:
                continue
            if float(s.get("no_speech_prob") or 0) > 0.6 and float(s.get("avg_logprob") or 0) < -0.5:
                continue
            if _HALLUCINATIONS.search(txt) or _is_noise(txt):
                continue
            kept.append(_TAGS.sub(" ", txt).strip())
        text = " ".join(kept)
    else:
        text = (data.get("text") or "").strip()
        if (_HALLUCINATIONS.search(text) and len(text) < 120) or _is_noise(text):
            text = ""
        text = _TAGS.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip()
    # Пара слов или повтор одного и того же — не речь, а шум.
    letters = re.findall(r"[^\W\d_]", text)
    words = re.findall(r"\w{2,}", text.lower())
    if len(letters) < 8 or (words and len(set(words)) <= 2 and len(words) > 4):
        return ""
    return text


async def transcribe_audio(
    audio_bytes: bytes,
    language: str | None = "ru",
    filename: str = "voice.ogg",
    content_type: str = "application/octet-stream",
) -> str:
    """Транскрибация голосового/аудио/кружка/звука видео через Groq Whisper.

    language=None — автоопределение (для видео из каналов: там бывает английский,
    а принудительный «ru» превращает его в кашу). '' при ошибке или если речи нет."""
    if not audio_bytes:
        return ""
    if len(audio_bytes) > STT_MAX_BYTES:
        logger.info("STT: файл %s МБ больше лимита Groq — пропускаю", len(audio_bytes) // 2**20)
        return ""
    async with _LOCK:
        try:
            form = aiohttp.FormData()
            form.add_field("model", GROQ_STT_MODEL)
            if language:
                form.add_field("language", language)
            form.add_field("response_format", "verbose_json")
            form.add_field("temperature", "0")
            form.add_field("file", audio_bytes, filename=filename, content_type=content_type)

            headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
            connector = _connector()
            async with aiohttp.ClientSession(connector=connector) as session:
                async with session.post(
                    f"{GROQ_BASE_URL}/audio/transcriptions",
                    data=form,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=120),
                ) as resp:
                    if resp.status != 200:
                        body = await resp.text()
                        logger.warning("Groq STT вернул статус %s: %.300s", resp.status, body)
                        return ""
                    data = await resp.json()
                    return _clean_transcript(data)[:1500]
        except Exception as e:
            logger.warning("Транскрибация не удалась: %s", e)
            return ""
