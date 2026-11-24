"""Скидки на игры из архива: ссылки на Steam в постах → раз в день цена через открытый API Steam.
Сообщаем только о новой или выросшей скидке (и о том, что игра стала бесплатной) — без спама.
ИИ не нужен, ключи не нужны."""

import asyncio
import logging
import re

import aiohttp

from config import GROQ_PROXY

log = logging.getLogger(__name__)

STEAM_APP_RE = re.compile(r"store\.steampowered\.com/app/(\d{1,8})", re.I)
API = "https://store.steampowered.com/api/appdetails?appids={id}&cc={cc}&l=russian&filters=basic,price_overview"
MAX_APPS = 80


def app_ids(rows: list[dict]) -> dict[int, int]:
    """{appid: id поста} по текстам постов (и ссылкам статей). Первым — самый свежий пост."""
    out: dict[int, int] = {}
    for r in sorted(rows, key=lambda r: -int(r["id"])):
        for m in STEAM_APP_RE.finditer(f"{r.get('text') or ''} {r.get('url') or ''}"):
            out.setdefault(int(m.group(1)), int(r["id"]))
    return dict(list(out.items())[:MAX_APPS])


def parse_app(payload: dict, appid: int) -> dict | None:
    """Ответ appdetails → {name, free, discount, final, initial} или None (игры нет / регион закрыт)."""
    ent = (payload or {}).get(str(appid)) or {}
    if not ent.get("success"):
        return None
    d = ent.get("data") or {}
    p = d.get("price_overview") or {}
    return {
        "name": (d.get("name") or "").strip() or f"Игра {appid}",
        "free": bool(d.get("is_free")),
        "discount": int(p.get("discount_percent") or 0),
        "final": p.get("final_formatted") or "",
        "initial": p.get("initial_formatted") or "",
        "priced": bool(p),
    }


def news(prev: dict, cur: dict) -> str | None:
    """Что сообщить: 'sale' — скидка появилась или выросла, 'free' — стала бесплатной."""
    if not cur:
        return None
    if cur["free"] and prev and not prev.get("free") and prev.get("priced"):
        return "free"
    if cur["discount"] > int((prev or {}).get("discount") or 0):
        return "sale"
    return None


def _connector():
    if not GROQ_PROXY:
        return None
    try:
        if GROQ_PROXY.startswith("socks"):
            from aiohttp_socks import ProxyConnector

            return ProxyConnector.from_url(GROQ_PROXY)
        return aiohttp.ProxyConnector.from_url(GROQ_PROXY)
    except Exception:
        return None


async def fetch_prices(appids: list[int], cc: str = "ru", pause: float = 1.2) -> dict[int, dict]:
    """{appid: parse_app(...)}; по одному запросу на игру (Steam не отдаёт пачкой basic+цену). Не бросает."""
    out: dict[int, dict] = {}
    try:
        async with aiohttp.ClientSession(connector=_connector(), timeout=aiohttp.ClientTimeout(total=20)) as s:
            for i, appid in enumerate(appids):
                if i:
                    await asyncio.sleep(pause)
                try:
                    async with s.get(API.format(id=appid, cc=cc), headers={"User-Agent": "tg-saver-bot"}) as r:
                        if r.status == 429:
                            log.info("Steam: слишком часто, остановился на %s из %s", i, len(appids))
                            break
                        if r.status != 200:
                            continue
                        got = parse_app(await r.json(content_type=None), appid)
                        if got:
                            out[appid] = got
                except Exception as e:
                    log.info("Steam %s: %s", appid, e)
    except Exception as e:
        log.info("Steam: %s", e)
    return out


def deal_line(g: dict, kind: str) -> str:
    if kind == "free":
        return f"🎁 {g['name']} — сейчас бесплатно!"
    was = f" (было {g['initial']})" if g.get("initial") else ""
    return f"🔥 {g['name']} — −{g['discount']}%: {g['final']}{was}"
