"""Отпечаток картинки (dHash 16×16) — ловит один и тот же мем/арт, пережатый или
уменьшенный в другом канале. Без ИИ и без трат лимита: считается по превью ~320px.

Формат отпечатка: «<ширина/высота>:<64 hex-символа>». Похожие = близкие пропорции
и мало отличающихся бит (порог подобран на пережатии/уменьшении против мемов
на одном шаблоне с разной подписью)."""

import logging
from io import BytesIO

logger = logging.getLogger(__name__)

HASH_SIDE = 16
SIMILAR_MAX_BITS = 20      # из 256
ASPECT_TOLERANCE = 0.12    # относительная разница пропорций


def dhash(image_bytes: bytes) -> str | None:
    try:
        from PIL import Image

        im = Image.open(BytesIO(image_bytes))
        w, h = im.size
        if not w or not h:
            return None
        g = im.convert("L").resize((HASH_SIDE + 1, HASH_SIDE), Image.LANCZOS)
        px = list(g.getdata())
        bits = 0
        for y in range(HASH_SIDE):
            row = px[y * (HASH_SIDE + 1):(y + 1) * (HASH_SIDE + 1)]
            for x in range(HASH_SIDE):
                bits = (bits << 1) | (1 if row[x] > row[x + 1] else 0)
        return f"{w / h:.3f}:{bits:064x}"
    except Exception as e:
        logger.info("Отпечаток картинки не посчитан: %s", e)
        return None


def distance(a: str, b: str) -> int | None:
    """Сколько бит отличается; None — несравнимы (другие пропорции или битые)."""
    try:
        ra, ha = a.split(":", 1)
        rb, hb = b.split(":", 1)
        ra, rb = float(ra), float(rb)
        if abs(ra - rb) / max(ra, rb) > ASPECT_TOLERANCE:
            return None
        return bin(int(ha, 16) ^ int(hb, 16)).count("1")
    except Exception:
        return None


def find_similar(h: str, known: list[tuple[int, str]], exclude: int | None = None, limit: int = 3) -> list[tuple[int, int]]:
    """[(item_id, бит отличия)] — самые похожие, не дальше порога."""
    out = []
    for iid, kh in known:
        if iid == exclude or not kh or kh == "-":
            continue
        d = distance(h, kh)
        if d is not None and d <= SIMILAR_MAX_BITS:
            out.append((iid, d))
    return sorted(out, key=lambda x: x[1])[:limit]


def _pick_thumb(msg):
    """Превью ~320px из сообщения (фото или видео/гифка); None — брать само фото."""
    photo = getattr(msg, "photo", None)
    doc = getattr(msg, "document", None)
    sizes = list(getattr(photo, "sizes", None) or []) if photo else list(getattr(doc, "thumbs", None) or [])
    sized = [s for s in sizes if getattr(s, "w", 0) and getattr(s, "h", 0)]
    if not sized:
        return None
    return min(sized, key=lambda s: abs(max(s.w, s.h) - 320))


async def message_hash(client, msg) -> str | None:
    """Отпечаток первой картинки/кадра сообщения (качает только маленькое превью)."""
    import asyncio

    if msg is None or not (getattr(msg, "photo", None) or getattr(msg, "video", None) or getattr(msg, "gif", None)):
        return None
    thumb = _pick_thumb(msg)
    try:
        if thumb is not None:
            data = await asyncio.wait_for(client.download_media(msg, file=bytes, thumb=thumb), 30)
        elif getattr(msg, "photo", None):
            data = await asyncio.wait_for(client.download_media(msg, file=bytes), 60)
        else:
            return None
    except Exception as e:
        logger.info("Превью для отпечатка не скачалось: %s", e)
        return None
    return dhash(data) if data else None
