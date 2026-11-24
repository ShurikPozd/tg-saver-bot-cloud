"""Слежка за тем, что сохранено ссылками: новые серии аниме (Shikimori) и новые версии программ (GitHub).
Открытые API, без ИИ. Первый проход только запоминает состояние — о старом не пишем.
Сообщаем: вышла новая серия / начался показ / вышло целиком; вышла новая версия."""

import asyncio
import logging
import re

import aiohttp

from config import GITHUB_TOKEN
from gamedeals import _connector

log = logging.getLogger(__name__)

SHIKI_RE = re.compile(r"shikimori\.(?:one|me|io|org)/animes/[a-z]?(\d{1,7})", re.I)
GITHUB_RE = re.compile(r"github\.com/([A-Za-z0-9-]{1,39})/([A-Za-z0-9._-]{1,100})", re.I)
_GH_NOT_OWNERS = {"features", "settings", "orgs", "topics", "sponsors", "marketplace", "login", "about",
                  "pricing", "explore", "collections", "trending", "apps", "site", "security", "enterprise",
                  "customer-stories", "readme", "events", "notifications", "codespaces", "issues", "pulls"}
MAX_WATCH = 60
STATUS_RU = {"anons": "анонс", "ongoing": "выходит", "released": "вышло"}


def _ids(rows: list[dict], pick) -> dict:
    """{ключ: id поста} — по тексту поста и адресу сохранённой статьи, свежие посты — первыми."""
    out: dict = {}
    for r in sorted(rows, key=lambda r: -int(r["id"])):
        for key in pick(f"{r.get('text') or ''} {r.get('url') or ''}"):
            out.setdefault(key, int(r["id"]))
    return dict(list(out.items())[:MAX_WATCH])


def anime_ids(rows: list[dict]) -> dict[int, int]:
    return _ids(rows, lambda t: [int(m.group(1)) for m in SHIKI_RE.finditer(t)])


def github_repos(rows: list[dict]) -> dict[str, int]:
    def pick(t):
        out = []
        for m in GITHUB_RE.finditer(t):
            owner, repo = m.group(1), re.sub(r"\.git$", "", m.group(2).rstrip("."))
            if owner.lower() not in _GH_NOT_OWNERS and repo:
                out.append(f"{owner}/{repo}")
        return out
    return _ids(rows, pick)


# ── сравнение «было → стало» ──

def anime_news(prev: dict, cur: dict) -> str | None:
    """Строка-новость или None. prev пустой — первый проход, молчим."""
    if not prev or not cur:
        return None
    name = cur.get("russian") or cur.get("name") or "Аниме"
    if prev.get("status") == "anons" and cur.get("status") == "ongoing":
        return f"📺 {name}: начался показ!"
    if prev.get("status") != "released" and cur.get("status") == "released":
        return f"✅ {name}: вышло целиком ({cur.get('episodes') or '?'} сер.) — можно смотреть подряд"
    if cur.get("status") == "ongoing" and int(cur.get("episodes_aired") or 0) > int(prev.get("episodes_aired") or 0):
        total = f" из {cur['episodes']}" if cur.get("episodes") else ""
        return f"🆕 {name}: вышла {cur['episodes_aired']}-я серия{total}"
    return None


def release_news(prev: dict, cur: dict, repo: str) -> str | None:
    if not prev or not cur or not cur.get("tag") or cur.get("tag") == prev.get("tag"):
        return None
    return f"⬆️ {repo}: новая версия {cur['tag']}" + (f" (была {prev['tag']})" if prev.get("tag") else "")


def _ru_date(iso: str) -> str:
    return f"{iso[8:10]}.{iso[5:7]}.{iso[:4]}" if len(iso) >= 10 else ""


# ── сеть ──

async def _get_json(s, url: str, headers: dict) -> tuple[int, dict | list | None]:
    try:
        async with s.get(url, headers=headers) as r:
            if r.status != 200:
                return r.status, None
            return 200, await r.json(content_type=None)
    except Exception as e:
        log.info("watchers %s: %s", url[:80], e)
        return 0, None


async def fetch_anime(ids: list[int], pause: float = 1.0) -> dict[int, dict]:
    out: dict[int, dict] = {}
    hdr = {"User-Agent": "tg-saver-bot"}
    async with aiohttp.ClientSession(connector=_connector(), timeout=aiohttp.ClientTimeout(total=20)) as s:
        for i, aid in enumerate(ids):
            if i:
                await asyncio.sleep(pause)   # Shikimori: не больше 5 запросов в секунду
            st, d = await _get_json(s, f"https://shikimori.one/api/animes/{aid}", hdr)
            if st == 429:
                break
            if isinstance(d, dict) and d.get("id"):
                out[aid] = {k: d.get(k) for k in ("name", "russian", "status", "episodes", "episodes_aired",
                                                   "next_episode_at", "released_on")}
    return out


async def fetch_releases(repos: list[str], pause: float = 0.5) -> dict[str, dict]:
    """{repo: {tag, name, url, body, at}}; нет релизов — по последнему тегу (без описания)."""
    out: dict[str, dict] = {}
    hdr = {"User-Agent": "tg-saver-bot", "Accept": "application/vnd.github+json"}
    if GITHUB_TOKEN:
        hdr["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    async with aiohttp.ClientSession(connector=_connector(), timeout=aiohttp.ClientTimeout(total=20)) as s:
        for i, repo in enumerate(repos):
            if i:
                await asyncio.sleep(pause)
            st, d = await _get_json(s, f"https://api.github.com/repos/{repo}/releases/latest", hdr)
            if st == 403:
                log.info("GitHub: лимит запросов, остановился на %s", repo)
                break
            if isinstance(d, dict) and d.get("tag_name"):
                out[repo] = {"tag": d["tag_name"], "name": d.get("name") or d["tag_name"],
                             "url": d.get("html_url") or f"https://github.com/{repo}/releases",
                             "body": (d.get("body") or "")[:3000], "at": _ru_date(d.get("published_at") or "")}
                continue
            if st == 404:
                st2, tags = await _get_json(s, f"https://api.github.com/repos/{repo}/tags?per_page=1", hdr)
                if isinstance(tags, list) and tags and tags[0].get("name"):
                    out[repo] = {"tag": tags[0]["name"], "name": tags[0]["name"],
                                 "url": f"https://github.com/{repo}/tags", "body": "", "at": ""}
    return out
