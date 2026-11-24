"""Очередь YouTube на телефоне: что лежит в очереди «Смотреть позже» расширения «Сводка
комментариев YouTube».

Откуда очередь: расширение само присылает её при каждом изменении (POST /api/yq-sync, не чаще
раза в 10 минут; см. bot.handler_api_yq_sync), а ещё она достаётся из полной копии
(/api/backup, раз в неделю или по кнопке).

Назад, с телефона: у каждого видео «Досмотрел» и «Убрать». Отметка ложится в настройку
`yt_queue_marks` ({id: {act, at}}, at — секунды по часам бота), видео сразу пропадает со
страницы. Расширение забирает отметки в ответе /api/yq-sync, убирает видео из очереди и
подтверждает (acks) — тогда бот их стирает. Отметка старше, чем видео снова добавили в очередь
(item.added), его не прячет и не убирает.

Страница: /yq/ — в мини-приложении Telegram (вход по initData), /yq/<token>/ — по личной
ссылке (подписанный токен веб-архива). Видна только владельцу архива.
"""

from __future__ import annotations

import gzip
import io
import json
import time

import database as db
import exticon

SETTING = "yt_queue"
MARKS = "yt_queue_marks"
MAX_ITEMS = 2000
MAX_MARKS = 2000
MAX_UNPACKED = 60 * 1024 * 1024
ACTS = ("done", "drop")
ID_OK = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-")


def _vid(v) -> str | None:
    v = str(v or "")
    return v if len(v) == 11 and set(v) <= ID_OK else None


def _num(v) -> int:
    try:
        return max(0, int(float(v or 0)))
    except Exception:
        return 0


def _item(r: dict, pos=None) -> dict | None:
    """Одно видео очереди — только нужные странице поля, длины обрезаны.
    pos — секунда, где остановились (из queuePos копии); иначе берётся r["pos"]."""
    if not isinstance(r, dict):
        return None
    vid = _vid(r.get("id"))
    if not vid:
        return None
    p = _num(r.get("pos") if pos is None else pos)
    added = _num(r.get("added"))
    if not added and r.get("addedAt"):
        added = _num(r.get("addedAt")) // 1000  # в расширении — миллисекунды
    return {
        "id": vid,
        "title": str(r.get("title") or "")[:300],
        "channel": str(r.get("channel") or "")[:120],
        "len": _num(r.get("len")),
        "short": bool(r.get("short")),
        "pos": p if p > 5 else 0,
        "added": added,
    }


def clean_items(rows, pos_map: dict | None = None) -> list[dict]:
    out = []
    for r in rows if isinstance(rows, list) else []:
        vid = _vid(r.get("id")) if isinstance(r, dict) else None
        it = _item(r, (pos_map or {}).get(vid, 0) if pos_map is not None else None)
        if it:
            out.append(it)
            if len(out) >= MAX_ITEMS:
                break
    return out


def extract(raw_gzip: bytes) -> dict | None:
    """Из сжатой копии — только очередь: [{id, title, channel, len, short, pos, added}], when, ver."""
    try:
        d = gzip.GzipFile(fileobj=io.BytesIO(raw_gzip))
        data = d.read(MAX_UNPACKED + 1)
        if len(data) > MAX_UNPACKED:
            return None
        backup = json.loads(data.decode("utf-8"))
    except Exception:
        return None
    if not isinstance(backup, dict) or backup.get("format") != "ytcs-backup":
        return None
    pos = backup.get("queuePos") if isinstance(backup.get("queuePos"), dict) else {}
    items = clean_items(backup.get("queue") or [], pos)
    try:
        when = int(float(backup.get("at") or 0) / 1000) or int(time.time())
    except Exception:
        when = int(time.time())
    return {"items": items, "at": when, "ver": str(backup.get("extVersion") or "")[:20]}


def from_sync(body: dict, now: float | None = None) -> dict:
    """Снимок из лёгкой отправки расширения (/api/yq-sync): items уже с pos и added."""
    return {
        "items": clean_items(body.get("items")),
        "at": int(now or time.time()),
        "ver": str(body.get("ver") or "")[:20],
        "rev": str(body.get("rev") or "")[:40],
    }


def save(snapshot: dict) -> None:
    db.set_setting(SETTING, json.dumps(snapshot, ensure_ascii=False))


def load() -> dict | None:
    try:
        d = json.loads(db.get_setting(SETTING, "") or "null")
    except Exception:
        return None
    return d if isinstance(d, dict) and isinstance(d.get("items"), list) else None


# ---------- отметки с телефона ----------

def load_marks() -> dict:
    try:
        d = json.loads(db.get_setting(MARKS, "") or "{}")
    except Exception:
        return {}
    out = {}
    for k, v in (d.items() if isinstance(d, dict) else []):
        if _vid(k) and isinstance(v, dict) and v.get("act") in ACTS:
            out[k] = {"act": v["act"], "at": _num(v.get("at"))}
    return out


def save_marks(marks: dict) -> None:
    if len(marks) > MAX_MARKS:
        keep = sorted(marks.items(), key=lambda kv: kv[1]["at"], reverse=True)[:MAX_MARKS]
        marks = dict(keep)
    db.set_setting(MARKS, json.dumps(marks, separators=(",", ":")))


def set_mark(vid: str, act: str, now: float | None = None) -> dict | None:
    """«Досмотрел» / «Убрать» (act done|drop) или «Вернуть» (undo). None — негодный запрос."""
    vid = _vid(vid)
    if not vid or act not in (*ACTS, "undo"):
        return None
    marks = load_marks()
    if act == "undo":
        marks.pop(vid, None)
    else:
        marks[vid] = {"act": act, "at": int(now or time.time())}
    save_marks(marks)
    return marks


def ack(acks) -> int:
    """Расширение применило (или отбросило как устаревшие) отметки: стираем те, что не новее
    подтверждённых. Отметку, поставленную на телефоне уже после, не трогаем. → сколько стёрто."""
    if not isinstance(acks, list) or not acks:
        return 0
    marks = load_marks()
    gone = 0
    for a in acks[:MAX_MARKS]:
        if not isinstance(a, dict):
            continue
        vid = _vid(a.get("id"))
        m = marks.get(vid) if vid else None
        if m and m["at"] <= _num(a.get("at")):
            del marks[vid]
            gone += 1
    if gone:
        save_marks(marks)
    return gone


def hidden(item: dict, marks: dict) -> bool:
    """Видео спрятано отметкой — если отметка не старше того, когда видео добавили в очередь."""
    m = marks.get(item.get("id"))
    return bool(m) and m["at"] >= _num(item.get("added"))


def marks_list(marks: dict) -> list[dict]:
    return [{"id": k, "act": v["act"], "at": v["at"]} for k, v in sorted(marks.items(), key=lambda kv: kv[1]["at"])]


def page_payload(snap: dict | None, marks: dict) -> dict:
    if not snap:
        return {"items": [], "at": 0, "empty": True, "pending": len(marks)}
    items = [x for x in snap["items"] if not hidden(x, marks)]
    return {"items": items, "at": snap.get("at", 0), "pending": len(marks)}


def register(app, *, owner, check_init_data, check_token, activate=None, on_change=None) -> None:
    """Маршруты страницы. owner() — id владельца архива (или None); activate(uid) — открыть
    его архив (у бота архив свой у каждого пользователя), прежде чем читать очередь;
    on_change() — отметки изменились (бот обновит тихую копию архива, чтобы они пережили
    перезапуск)."""
    from aiohttp import web

    headers = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex"}

    def page_html(mode: str) -> str:
        tg = '<script src="https://telegram.org/js/telegram-web-app.js"></script>' if mode == "app" else ""
        return (PAGE_HTML.replace("<!--TG-->", tg, 1).replace("<!--ICON-->", exticon.HEAD, 1)
                .replace('const MODE="";', f'const MODE="{mode}";', 1))

    def payload():
        if activate:
            activate(owner())
        return page_payload(load(), load_marks())

    def do_mark(body: dict):
        if activate:
            activate(owner())
        marks = set_mark(body.get("id"), str(body.get("act") or ""))
        if marks is None:
            return web.json_response({"error": "bad request"}, status=400, headers=headers)
        if on_change:
            try:
                on_change()
            except Exception:
                pass
        return web.json_response({"ok": True, "pending": len(marks)}, headers=headers)

    async def read_json(request) -> dict:
        raw = await request.content.read(8192)
        try:
            d = json.loads(raw.decode("utf-8"))
        except Exception:
            return {}
        return d if isinstance(d, dict) else {}

    async def app_page(request):
        if not request.path.endswith("/"):
            raise web.HTTPFound(request.path + "/")
        return web.Response(text=page_html("app"), content_type="text/html", charset="utf-8", headers=headers)

    async def app_data(request):
        uid = check_init_data((await request.text())[:4096])
        if uid is None or uid != owner():
            return web.json_response({"error": "Откройте кнопкой в боте"}, status=403, headers=headers)
        return web.json_response(payload(), headers=headers)

    async def app_mark(request):
        body = await read_json(request)
        uid = check_init_data(str(body.get("init") or "")[:4096])
        if uid is None or uid != owner():
            return web.json_response({"error": "Откройте кнопкой в боте"}, status=403, headers=headers)
        return do_mark(body)

    def _token_uid(request):
        uid = check_token(request.match_info.get("token", ""))
        if uid is None or uid != owner():
            raise web.HTTPNotFound(text="Ссылка устарела — попросите новую командой /yt в боте")
        return uid

    async def link_page(request):
        _token_uid(request)
        if not request.path.endswith("/"):
            raise web.HTTPFound(request.path + "/")
        return web.Response(text=page_html("link"), content_type="text/html", charset="utf-8", headers=headers)

    async def link_data(request):
        _token_uid(request)
        return web.json_response(payload(), headers=headers)

    async def link_mark(request):
        _token_uid(request)
        return do_mark(await read_json(request))

    app.router.add_get("/yq", app_page)
    app.router.add_get("/yq/", app_page)
    app.router.add_post("/yq/data", app_data)
    app.router.add_post("/yq/mark", app_mark)
    app.router.add_get("/yq/{token}", link_page)
    app.router.add_get("/yq/{token}/", link_page)
    app.router.add_get("/yq/{token}/data.json", link_data)
    app.router.add_post("/yq/{token}/mark", link_mark)


PAGE_HTML = r"""<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex">
<title>Очередь YouTube</title>
<!--ICON-->
<!--TG-->
<style>
  :root { --bg:#0f0f12; --bg2:#1a1a1f; --bg3:#26262c; --bg4:#303038; --fg:#f1f1f3; --fg2:#a7a7b0; --acc:#ff3b5c; --line:rgba(255,255,255,.08); --shadow:0 10px 30px rgba(0,0,0,.45); color-scheme:dark; }
  @media (prefers-color-scheme: light) { :root { --bg:#f6f6f8; --bg2:#fff; --bg3:#ececf0; --bg4:#e0e0e6; --fg:#111114; --fg2:#5f5f6a; --acc:#e8002d; --line:rgba(0,0,0,.08); --shadow:0 10px 30px rgba(0,0,0,.16); color-scheme:light; } }
  [hidden] { display:none !important; }
  * { box-sizing:border-box; }
  html { font-size:clamp(16px, .9rem + .3vw, 20px); }
  body { margin:0; background:var(--bg); color:var(--fg); font:1rem/1.4 "Segoe UI Variable","Segoe UI",system-ui,-apple-system,Roboto,sans-serif; -webkit-tap-highlight-color:transparent; }
  .wrap { width:100%; max-width:1800px; margin:0 auto; padding-left:max(16px, env(safe-area-inset-left)); padding-right:max(16px, env(safe-area-inset-right)); }
  header { position:sticky; top:0; z-index:2; background:var(--bg); border-bottom:1px solid var(--line); }
  header .wrap { padding-top:16px; padding-bottom:14px; text-align:center; }
  h1 { margin:0; font-size:1.5rem; font-weight:700; line-height:1.2; }
  .sub { margin-top:6px; color:var(--fg2); font-size:.9rem; }
  .note { margin-top:4px; color:var(--fg2); font-size:.85rem; }
  .q { display:block; margin:12px auto 0; width:100%; max-width:760px; height:2.9rem; padding:0 1.1rem; border-radius:1.45rem; border:1px solid var(--line); background:var(--bg2); color:var(--fg); font:inherit; outline:none; }
  .q:focus { border-color:var(--acc); }
  main.wrap { padding-top:14px; padding-bottom:calc(32px + env(safe-area-inset-bottom)); display:grid; gap:12px; grid-template-columns:repeat(auto-fill, minmax(min(100%, 470px), 1fr)); align-items:start; }
  .v { display:flex; flex-direction:column; gap:10px; padding:10px; border-radius:18px; background:var(--bg2); transition:opacity .2s, transform .2s; }
  .v.out { opacity:0; transform:scale(.97); }
  a.main { display:grid; grid-template-columns:132px minmax(0,1fr); gap:12px; align-items:center; color:inherit; text-decoration:none; border-radius:12px; }
  a.main:active .th { opacity:.85; }
  .th { position:relative; aspect-ratio:16/9; border-radius:10px; overflow:hidden; background:var(--bg3); }
  .th img { width:100%; height:100%; object-fit:cover; display:block; }
  .len { position:absolute; right:5px; bottom:5px; padding:1px 5px; border-radius:4px; background:rgba(0,0,0,.8); color:#fff; font-size:.75rem; font-weight:600; }
  .bar { position:absolute; left:0; bottom:0; height:3px; background:var(--acc); }
  .t { display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; font-size:.95rem; font-weight:600; }
  .m { display:block; margin-top:4px; color:var(--fg2); font-size:.82rem; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .acts { display:grid; grid-template-columns:1fr 1fr; gap:8px; }
  .b { display:inline-flex; align-items:center; justify-content:center; gap:8px; height:3rem; padding:0 14px; border:0; border-radius:1.5rem; background:var(--bg3); color:var(--fg); font:inherit; font-size:.95rem; font-weight:600; cursor:pointer; }
  .b svg { width:1.35rem; height:1.35rem; flex:none; display:block; }
  .b:active { background:var(--bg4); }
  .b.done svg { color:var(--acc); }
  @media (hover:hover) { .b:hover { background:var(--bg4); } a.main:hover .t { color:var(--acc); } }
  .b:disabled { opacity:.5; pointer-events:none; }
  .empty { grid-column:1 / -1; min-height:50vh; display:flex; align-items:center; justify-content:center; text-align:center; color:var(--fg2); font-size:1.15rem; padding:24px 16px; }
  .toast { position:fixed; left:50%; bottom:calc(16px + env(safe-area-inset-bottom)); transform:translateX(-50%); z-index:5; width:calc(100% - 32px); max-width:560px; display:flex; align-items:center; gap:12px; padding:8px 8px 8px 18px; border-radius:1.7rem; background:var(--bg2); border:1px solid var(--line); box-shadow:var(--shadow); font-size:.95rem; }
  .toast span { flex:1; min-width:0; }
  .toast .b { height:2.6rem; color:var(--acc); flex:none; }
  @media (min-width: 560px) { a.main { grid-template-columns:200px minmax(0,1fr); gap:14px; } .t { font-size:1rem; } }
  @media (prefers-reduced-motion: reduce) { .v { transition:none; } }
</style></head>
<body>
<header><div class="wrap"><h1>Очередь YouTube</h1><div class="sub" id="sub">Загружаю…</div><div class="note" id="note" hidden></div>
<input class="q" id="q" type="search" placeholder="Поиск по названию и каналу" autocomplete="off"></div></header>
<main class="wrap" id="list"></main>
<div class="toast" id="toast" hidden><span id="toastText"></span><button class="b" id="toastUndo" type="button" hidden>Вернуть</button></div>
<script>
const MODE="";
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&":"&amp;", "<":"&lt;", ">":"&gt;", '"':"&quot;", "'":"&#39;" })[c]);
const clock = (s) => { const h=Math.floor(s/3600), m=Math.floor(s%3600/60), x=String(s%60).padStart(2,"0"); return h?`${h}:${String(m).padStart(2,"0")}:${x}`:`${m}:${x}`; };
const dur = (s) => { const m=Math.round(s/60); if (m<60) return `${Math.max(1,m)} мин`; const h=Math.floor(m/60), mm=m%60; return mm?`${h} ч ${mm} мин`:`${h} ч`; };
const plural = (n, one, few, many) => { const a=n%10, b=n%100; return a===1&&b!==11?one:a>=2&&a<=4&&(b<12||b>14)?few:many; };
const ICON_DONE = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M4.5 12.5l5 5L19.5 7"/></svg>';
const ICON_DROP = '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M6 6l12 12M18 6L6 18"/></svg>';
let items = [], at = 0, pending = 0, toastTimer = 0, lastUndo = null;
const tg = MODE === "app" ? (window.Telegram && Telegram.WebApp) : null;
function head() {
  const left = items.reduce((a, x) => a + Math.max(0, (x.len || 0) - (x.pos || 0)), 0);
  const when = new Date(at * 1000).toLocaleString("ru-RU", { day: "numeric", month: "long", hour: "2-digit", minute: "2-digit" });
  $("sub").textContent = `${items.length} ${plural(items.length, "видео", "видео", "видео")}${left >= 60 ? ` · ≈ ${dur(left)}` : ""} · обновлено ${when}`;
  $("note").hidden = !pending;
  if (pending) $("note").textContent = `${pending} ${plural(pending, "отметка ждёт", "отметки ждут", "отметок ждут")}, когда на компьютере откроется YouTube`;
}
function card(r) {
  const pct = r.len && r.pos ? Math.min(100, Math.round(r.pos / r.len * 100)) : 0;
  const url = r.short ? `https://www.youtube.com/shorts/${r.id}` : `https://www.youtube.com/watch?v=${r.id}${r.pos ? `&t=${r.pos}s` : ""}`;
  const meta = [r.channel, r.pos && r.len ? `осталось ${dur(r.len - r.pos)}` : r.short ? "Shorts" : ""].filter(Boolean).join(" · ");
  return `<article class="v" data-id="${esc(r.id)}"><a class="main" href="${url}" target="_blank" rel="noopener"><span class="th"><img loading="lazy" alt="" src="https://i.ytimg.com/vi/${r.id}/mqdefault.jpg">${r.len ? `<span class="len">${clock(r.len)}</span>` : ""}${pct ? `<span class="bar" style="width:${pct}%"></span>` : ""}</span><span><span class="t">${esc(r.title || "Видео")}</span><span class="m">${esc(meta)}</span></span></a>`
    + `<div class="acts"><button class="b done" type="button" data-act="done">${ICON_DONE}Досмотрел</button><button class="b drop" type="button" data-act="drop">${ICON_DROP}Убрать</button></div></article>`;
}
function draw() {
  const q = $("q").value.trim().toLowerCase();
  const rows = q ? items.filter((r) => (r.title + " " + r.channel).toLowerCase().includes(q)) : items;
  $("list").innerHTML = rows.length ? rows.map(card).join("") : `<div class="empty">${q ? "Ничего не нашлось" : "Очередь пуста"}</div>`;
}
function toast(text, undo) {
  clearTimeout(toastTimer);
  $("toastText").textContent = text;
  lastUndo = undo || null;
  $("toastUndo").hidden = !undo;
  $("toast").hidden = false;
  toastTimer = setTimeout(() => { $("toast").hidden = true; lastUndo = null; }, 6000);
}
async function post(path, body) {
  if (MODE === "app") body = { ...body, init: (tg && tg.initData) || "" };
  const r = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  const d = await r.json().catch(() => ({}));
  if (!r.ok || !d.ok) throw new Error(d.error || String(r.status));
  return d;
}
async function mark(id, act) {
  const i = items.findIndex((x) => x.id === id);
  if (i < 0) return;
  const rec = items[i];
  rec.back = false;
  const el = document.querySelector(`.v[data-id="${id}"]`);
  if (el) { el.querySelectorAll("button").forEach((b) => (b.disabled = true)); el.classList.add("out"); }
  try {
    const d = await post("mark", { id, act });
    pending = d.pending;
  } catch (e) {
    if (el) { el.classList.remove("out"); el.querySelectorAll("button").forEach((b) => (b.disabled = false)); }
    toast("Не получилось — нет связи с ботом. Попробуйте ещё раз.");
    return;
  }
  tg?.HapticFeedback?.impactOccurred?.("light");
  setTimeout(() => {
    if (rec.back) return;
    const k = items.indexOf(rec);
    if (k >= 0) items.splice(k, 1);
    head();
    draw();
  }, 200);
  toast(act === "done" ? "Отмечено «Досмотрел»" : "Убрано из очереди", { rec, at: i });
}
async function undo() {
  const u = lastUndo;
  if (!u) return;
  $("toast").hidden = true;
  lastUndo = null;
  try {
    const d = await post("mark", { id: u.rec.id, act: "undo" });
    pending = d.pending;
  } catch (e) { toast("Не получилось вернуть — нет связи с ботом."); return; }
  u.rec.back = true;
  if (!items.includes(u.rec)) items.splice(Math.min(u.at, items.length), 0, u.rec);
  head();
  draw();
}
$("list").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-act]");
  if (!b) return;
  mark(b.closest(".v").dataset.id, b.dataset.act);
});
$("toastUndo").addEventListener("click", undo);
async function start() {
  let r;
  try {
    if (MODE === "app") {
      if (tg) { tg.ready(); tg.expand(); }
      r = await fetch("data", { method: "POST", body: (tg && tg.initData) || "" });
    } else {
      r = await fetch("data.json");
    }
  } catch (e) { $("sub").textContent = "Нет связи с ботом — попробуйте ещё раз."; return; }
  if (!r.ok) { $("sub").textContent = "Откройте эту страницу кнопкой в боте."; return; }
  const d = await r.json();
  pending = d.pending || 0;
  if (d.empty) { $("sub").textContent = "Очереди ещё нет: откройте YouTube в браузере с расширением — она придёт сама."; draw(); return; }
  items = d.items || [];
  at = d.at;
  head();
  draw();
}
$("q").addEventListener("input", draw);
start();
</script>
</body></html>
"""
