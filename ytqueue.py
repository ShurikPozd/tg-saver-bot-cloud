"""Очередь YouTube на телефоне: что лежит в очереди расширения «Сводка комментариев
YouTube» — по последней автокопии (её раз в неделю или по кнопке присылает расширение,
/api/backup). Только просмотр: сама очередь живёт в браузере, менять её отсюда нельзя.

Страница: /yq/ — в мини-приложении Telegram (вход по initData), /yq/<token>/ — по личной
ссылке (подписанный токен веб-архива). Видна только владельцу архива.
"""

from __future__ import annotations

import gzip
import json
import time

import database as db
import exticon

SETTING = "yt_queue"
MAX_ITEMS = 2000
MAX_UNPACKED = 60 * 1024 * 1024
ID_OK = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")


def _vid(v) -> str | None:
    v = str(v or "")
    return v if len(v) == 11 and set(v) <= ID_OK else None


def extract(raw_gzip: bytes) -> dict | None:
    """Из сжатой копии — только очередь: [{id, title, channel, len, short, pos}], when, ver."""
    try:
        d = gzip.GzipFile(fileobj=__import__("io").BytesIO(raw_gzip))
        data = d.read(MAX_UNPACKED + 1)
        if len(data) > MAX_UNPACKED:
            return None
        backup = json.loads(data.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(backup, dict) or backup.get("format") != "ytcs-backup":
        return None
    pos = backup.get("queuePos") if isinstance(backup.get("queuePos"), dict) else {}
    items = []
    for r in backup.get("queue") or []:
        if not isinstance(r, dict):
            continue
        vid = _vid(r.get("id"))
        if not vid:
            continue
        try:
            ln = max(0, int(float(r.get("len") or 0)))
            p = max(0, int(float(pos.get(vid) or 0)))
        except Exception:
            ln, p = 0, 0
        items.append({
            "id": vid,
            "title": str(r.get("title") or "")[:300],
            "channel": str(r.get("channel") or "")[:120],
            "len": ln,
            "short": bool(r.get("short")),
            "pos": p if p > 5 else 0,
        })
        if len(items) >= MAX_ITEMS:
            break
    try:
        when = int(float(backup.get("at") or 0) / 1000) or int(time.time())
    except Exception:
        when = int(time.time())
    return {"items": items, "at": when, "ver": str(backup.get("extVersion") or "")[:20]}


def save(snapshot: dict) -> None:
    db.set_setting(SETTING, json.dumps(snapshot, ensure_ascii=False))


def load() -> dict | None:
    try:
        d = json.loads(db.get_setting(SETTING, "") or "null")
    except Exception:
        return None
    return d if isinstance(d, dict) and isinstance(d.get("items"), list) else None


def register(app, *, owner, check_init_data, check_token, activate=None) -> None:
    """Маршруты страницы. owner() — id владельца архива (или None); activate(uid) — открыть
    его архив (у бота архив свой у каждого пользователя), прежде чем читать очередь."""
    from aiohttp import web

    headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex"}

    def page_html(mode: str) -> str:
        tg = '<script src="https://telegram.org/js/telegram-web-app.js"></script>' if mode == "app" else ""
        return (PAGE_HTML.replace("<!--TG-->", tg, 1).replace("<!--ICON-->", exticon.HEAD, 1)
                .replace('const MODE="";', f'const MODE="{mode}";', 1))

    def payload():
        if activate:
            activate(owner())
        snap = load()
        if not snap:
            return {"items": [], "at": 0, "empty": True}
        return snap

    async def app_page(request):
        if not request.path.endswith("/"):
            raise web.HTTPFound(request.path + "/")
        return web.Response(text=page_html("app"), content_type="text/html", charset="utf-8", headers=headers)

    async def app_data(request):
        uid = check_init_data((await request.text())[:4096])
        if uid is None or uid != owner():
            return web.json_response({"error": "Открой кнопкой в боте"}, status=403, headers=headers)
        return web.json_response(payload(), headers=headers)

    def _token_uid(request):
        uid = check_token(request.match_info.get("token", ""))
        if uid is None or uid != owner():
            raise web.HTTPNotFound(text="Ссылка устарела — попроси новую командой /yt в боте")
        return uid

    async def link_page(request):
        _token_uid(request)
        if not request.path.endswith("/"):
            raise web.HTTPFound(request.path + "/")
        return web.Response(text=page_html("link"), content_type="text/html", charset="utf-8", headers=headers)

    async def link_data(request):
        _token_uid(request)
        return web.json_response(payload(), headers=headers)

    app.router.add_get("/yq", app_page)
    app.router.add_get("/yq/", app_page)
    app.router.add_post("/yq/data", app_data)
    app.router.add_get("/yq/{token}", link_page)
    app.router.add_get("/yq/{token}/", link_page)
    app.router.add_get("/yq/{token}/data.json", link_data)


PAGE_HTML = r"""<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex">
<title>Очередь YouTube</title>
<!--ICON-->
<!--TG-->
<style>
  :root { --bg:#0f0f12; --bg2:#1a1a1f; --bg3:#26262c; --fg:#f1f1f3; --fg2:#a7a7b0; --acc:#ff3b5c; --line:rgba(255,255,255,.08); }
  @media (prefers-color-scheme: light) { :root { --bg:#f6f6f8; --bg2:#fff; --bg3:#ececf0; --fg:#111114; --fg2:#5f5f6a; --acc:#e8002d; --line:rgba(0,0,0,.08); } }
  * { box-sizing: border-box; }
  body { margin:0; background:var(--bg); color:var(--fg); font:16px/1.4 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }
  header { position:sticky; top:0; z-index:2; background:var(--bg); padding:16px 16px 12px; border-bottom:1px solid var(--line); }
  h1 { margin:0; font-size:22px; font-weight:700; }
  .sub { margin-top:4px; color:var(--fg2); font-size:14px; }
  .q { margin-top:12px; width:100%; height:44px; padding:0 16px; border-radius:22px; border:1px solid var(--line); background:var(--bg2); color:var(--fg); font:inherit; outline:none; }
  .q:focus { border-color:var(--acc); }
  main { max-width:760px; margin:0 auto; padding:12px 12px 32px; display:grid; gap:10px; }
  a.v { display:grid; grid-template-columns:132px minmax(0,1fr); gap:12px; align-items:center; padding:8px; border-radius:14px; background:var(--bg2); color:inherit; text-decoration:none; }
  a.v:active { background:var(--bg3); }
  .th { position:relative; aspect-ratio:16/9; border-radius:10px; overflow:hidden; background:var(--bg3); }
  .th img { width:100%; height:100%; object-fit:cover; display:block; }
  .len { position:absolute; right:5px; bottom:5px; padding:1px 5px; border-radius:4px; background:rgba(0,0,0,.8); color:#fff; font-size:12px; font-weight:600; }
  .bar { position:absolute; left:0; bottom:0; height:3px; background:var(--acc); }
  .t { display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; font-size:15px; font-weight:600; }
  .m { margin-top:4px; color:var(--fg2); font-size:13px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .empty { padding:48px 16px; text-align:center; color:var(--fg2); }
  @media (min-width: 560px) { a.v { grid-template-columns:200px minmax(0,1fr); } .t { font-size:16px; } }
</style></head>
<body>
<header><h1>Очередь YouTube</h1><div class="sub" id="sub">Загружаю…</div>
<input class="q" id="q" type="search" placeholder="Поиск по названию и каналу" autocomplete="off"></header>
<main id="list"></main>
<script>
const MODE="";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" })[c]);
const clock = (s) => { const h=Math.floor(s/3600), m=Math.floor(s%3600/60), x=String(s%60).padStart(2,"0"); return h?`${h}:${String(m).padStart(2,"0")}:${x}`:`${m}:${x}`; };
const dur = (s) => { const m=Math.round(s/60); if (m<60) return `${Math.max(1,m)} мин`; const h=Math.floor(m/60), mm=m%60; return mm?`${h} ч ${mm} мин`:`${h} ч`; };
let items = [];
function draw() {
  const q = $("q").value.trim().toLowerCase();
  const rows = q ? items.filter((r) => (r.title + " " + r.channel).toLowerCase().includes(q)) : items;
  $("list").innerHTML = rows.length ? rows.map((r) => {
    const pct = r.len && r.pos ? Math.min(100, Math.round(r.pos / r.len * 100)) : 0;
    const url = r.short ? `https://www.youtube.com/shorts/${r.id}` : `https://www.youtube.com/watch?v=${r.id}${r.pos ? `&t=${r.pos}s` : ""}`;
    const meta = [r.channel, r.pos && r.len ? `осталось ${dur(r.len - r.pos)}` : r.short ? "Shorts" : ""].filter(Boolean).join(" · ");
    return `<a class="v" href="${url}" target="_blank" rel="noopener"><span class="th"><img loading="lazy" alt="" src="https://i.ytimg.com/vi/${r.id}/mqdefault.jpg">${r.len ? `<span class="len">${clock(r.len)}</span>` : ""}${pct ? `<span class="bar" style="width:${pct}%"></span>` : ""}</span><span><span class="t">${esc(r.title || "Видео")}</span><span class="m">${esc(meta)}</span></span></a>`;
  }).join("") : `<div class="empty">${q ? "Ничего не нашлось" : "Очередь пуста"}</div>`;
}
async function start() {
  let r;
  try {
    if (MODE === "app") {
      const tg = window.Telegram && Telegram.WebApp;
      if (tg) { tg.ready(); tg.expand(); }
      r = await fetch("data", { method: "POST", body: (tg && tg.initData) || "" });
    } else {
      r = await fetch("data.json");
    }
  } catch (e) { $("sub").textContent = "Нет связи с ботом — попробуйте ещё раз."; return; }
  if (!r.ok) { $("sub").textContent = "Открой эту страницу кнопкой в боте."; return; }
  const d = await r.json();
  if (d.empty) { $("sub").textContent = "Копии ещё не было: в библиотеке расширения — «Резервная копия» → «Отправить сейчас»."; draw(); return; }
  items = d.items || [];
  const left = items.reduce((a, x) => a + Math.max(0, (x.len || 0) - (x.pos || 0)), 0);
  const when = new Date(d.at * 1000).toLocaleString("ru-RU", { day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" });
  $("sub").textContent = `${items.length} видео${left >= 60 ? ` · ≈ ${dur(left)}` : ""} · по копии от ${when}`;
  draw();
}
$("q").addEventListener("input", draw);
start();
</script>
</body></html>
"""
