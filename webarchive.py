"""🌐 Архив в браузере: плитка постов с превью, папки, поиск (по словам и по смыслу).

Доступ по личной ссылке /a/<токен>/ — токен подписан ключом бота и действует 30 дней
(новую ссылку даёт /web). Превью картинок бот качает из Telegram (~320px) и кэширует на диске.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time

import database as db
import imghash
import postctx
from config import API_HASH, BOT_TOKEN

logger = logging.getLogger(__name__)

TOKEN_DAYS = 30
THUMB_DIR = os.path.join("data", "thumbs")
_dl_sem = asyncio.Semaphore(4)


def _key() -> bytes:
    return hashlib.sha256(("web-archive:" + (BOT_TOKEN or API_HASH or "dev")).encode()).digest()


def make_token(uid: int, days: int = TOKEN_DAYS) -> str:
    body = f"{int(uid)}.{int(time.time()) + days * 86400}"
    sig = hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()[:24]
    return f"{body}.{sig}"


def check_token(token: str) -> int | None:
    try:
        uid_s, exp_s, sig = (token or "").split(".")
        body = f"{uid_s}.{exp_s}"
        good = hmac.new(_key(), body.encode(), hashlib.sha256).hexdigest()[:24]
        if not hmac.compare_digest(sig, good) or int(exp_s) < time.time():
            return None
        return int(uid_s)
    except Exception:
        return None


def check_init_data(init_data: str, max_age: int = 7 * 86400) -> int | None:
    """Проверка initData мини-приложения (подпись ключом бота, как велит Telegram).
    Возвращает id пользователя или None."""
    from urllib.parse import parse_qsl

    try:
        pairs = dict(parse_qsl(init_data or "", keep_blank_values=True, strict_parsing=True))
    except ValueError:
        return None
    got = pairs.pop("hash", "")
    if not got or not BOT_TOKEN:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    good = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(got, good):
        return None
    try:
        if time.time() - int(pairs.get("auth_date", "0")) > max_age:
            return None
        return int(json.loads(pairs.get("user", "{}"))["id"])
    except Exception:
        return None


def app_url() -> str:
    base = public_url()
    return f"{base}/app/" if base else ""


def public_url() -> str:
    return (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")


def archive_link(uid: int) -> str:
    base = public_url()
    return f"{base}/a/{make_token(uid)}/" if base else ""


def in_scope(category: str, group: str, scope: dict | None) -> bool:
    """Пост из этой папки? scope = {"kind": "folder"|"cat", "val": путь/имя} или None (весь архив)."""
    if not scope:
        return True
    if scope["kind"] == "cat":
        return category == scope["val"]
    path = scope["val"]
    return group == path or group.startswith(path + "/")


def archive_data(icons: dict[str, str], bot_username: str = "", unseen: set | None = None,
                 scope: dict | None = None) -> dict:
    """Всё для страницы одним JSON: дерево папок, посты, иконки. scope — только одна папка
    (страница «🔗 Поделиться»: ни остального архива, ни служебного)."""
    groups = {c["category"]: (c["group"] or "").strip("/") for c in db.get_categories()}
    if scope:
        groups = {c: g for c, g in groups.items() if in_scope(c, g, scope)}
    items = []
    arts = db.articles_brief()
    favs = set(db.favorites()) if not scope else set()
    for r in db.items_for_smart_search():
        if scope and r["category"] not in groups:
            continue
        stored = r["original_text"]
        aux = postctx.aux_part(stored) if postctx.has_aux(stored) else ""
        text = postctx.strip_aux(stored) if postctx.has_aux(stored) else stored
        items.append({
            "id": r["id"], "c": r["category"], "t": r["content_type"], "s": r["summary"],
            "x": (text or "")[:1500], "v": (aux or "")[:600], "d": (r["created_at"] or "")[:16],
            "ch": r["source_channel"], "tg": r["tags"].strip(), "l": r["locked"],
            "nw": r["id"] in (unseen or ()), "tp": r.get("topics") or [],
            "ar": (arts[r["id"]][0] or "Статья") if r["id"] in arts else "",
            "fv": r["id"] in favs,
        })
    folders = db.get_folder_paths()
    if scope:
        folders = [f for f in folders if scope["kind"] == "folder" and in_scope("", f, scope)]
        items = [dict(i, v="", ch="", nw=False, ar="") for i in items]   # гостю — без «что увидел ИИ», каналов, статей
        # иконки только своих папок: по ключам словаря видны были бы названия всего архива
        names = set(groups) | set(groups.values()) | set(folders) | {
            seg for g in list(groups.values()) + folders for seg in (g.split("/") if g else [])}
        icons = {k: v for k, v in (icons or {}).items() if k in names}
    return {"items": items, "groups": groups, "icons": icons, "bot": bot_username, "folders": folders,
            "title": (scope["val"].rpartition("/")[2] if scope else "")}


async def thumb_bytes(client, uid: int, item_id: int, big: bool = False) -> bytes | None:
    """Превью поста (≈320px; big — фото целиком). Кэш на диске, «нет картинки» тоже кэшируется."""
    folder = os.path.join(THUMB_DIR, str(uid))
    path = os.path.join(folder, f"{item_id}{'_big' if big else ''}.jpg")
    if os.path.exists(path):
        data = open(path, "rb").read()
        return data or None
    item = db.get_item(item_id)
    if not item or not item.get("chat_id") or not item.get("message_id") or client is None:
        return None
    if item.get("content_type") not in ("photo", "video", "animation", "video_note", "document", "audio"):
        return None
    data = None
    async with _dl_sem:
        try:
            msg = await asyncio.wait_for(client.get_messages(item["chat_id"], ids=item["message_id"]), 20)
            if msg is not None:
                if big and getattr(msg, "photo", None):
                    data = await asyncio.wait_for(client.download_media(msg, file=bytes), 60)
                else:
                    thumb = imghash._pick_thumb(msg)
                    if thumb is not None:
                        data = await asyncio.wait_for(client.download_media(msg, file=bytes, thumb=thumb), 30)
                    elif getattr(msg, "photo", None):
                        data = await asyncio.wait_for(client.download_media(msg, file=bytes), 60)
        except Exception as e:
            logger.info("Веб-архив: превью #%s не скачалось: %s", item_id, e)
            return None  # сеть/лимит — не кэшируем, попробуем в следующий раз
    os.makedirs(folder, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data or b"")
    return data or None


class _HideTokens(logging.Filter):
    """Ссылка на архив — это пароль: строки доступа к /a/<токен>/ не пишем в лог Render."""

    def filter(self, record):
        try:
            msg = record.getMessage()
            return "/a/" not in msg and "/s/" not in msg
        except Exception:
            return True


def share_link(uid: int, sid: str) -> str:
    base = public_url()
    return f"{base}/s/{int(uid)}.{sid}/" if base else ""


def setup(app, *, get_client, activate_user, get_icons, smart_search, get_bot_username, send_item=None,
          get_unseen=None, mark_seen=None, get_share=None, edit=None):
    from aiohttp import web

    logging.getLogger("aiohttp.access").addFilter(_HideTokens())

    def _uid(request):
        uid = check_token(request.match_info.get("token", ""))
        if uid is None:
            raise web.HTTPForbidden(text="Ссылка устарела или неверна. Попроси новую у бота: /web")
        activate_user(uid)
        return uid

    async def page(request):
        _uid(request)
        if not request.path.endswith("/"):
            raise web.HTTPFound(request.path + "/")
        return web.Response(text=PAGE_HTML, content_type="text/html", charset="utf-8",
                            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    async def data(request):
        _uid(request)
        body = json.dumps(archive_data(get_icons(), get_bot_username(), get_unseen() if get_unseen else None),
                          ensure_ascii=False)
        resp = web.Response(text=body, content_type="application/json", charset="utf-8",
                            headers={"Cache-Control": "no-store"})
        resp.enable_compression()
        return resp

    async def thumb(request):
        uid = _uid(request)
        try:
            item_id = int(request.match_info["id"])
        except ValueError:
            raise web.HTTPNotFound()
        img = await thumb_bytes(get_client(), uid, item_id, big=request.query.get("big") == "1")
        if not img:
            raise web.HTTPNotFound()
        return web.Response(body=img, content_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=604800"})

    async def article(request):
        _uid(request)
        try:
            item_id = int(request.match_info["id"])
        except ValueError:
            raise web.HTTPNotFound()
        art = db.article_get(item_id)
        if not art or art["status"] != "ok":
            raise web.HTTPNotFound()
        return web.json_response({"title": art["title"], "url": art["url"], "text": art["text"]},
                                 headers={"Cache-Control": "private, max-age=3600"})

    async def search(request):
        _uid(request)
        q = (request.query.get("q") or "").strip()[:300]
        if not q:
            return web.json_response({"ids": [], "note": ""})
        items, note = await smart_search(q)
        return web.json_response({"ids": [i["id"] for i in items], "note": note})

    async def app_page(request):
        if not request.path.endswith("/"):
            raise web.HTTPFound(request.path + "/")
        html = PAGE_HTML.replace("const APP_MODE=false;", "const APP_MODE=true;", 1).replace(
            "<!--TG-->", '<script src="https://telegram.org/js/telegram-web-app.js"></script>', 1)
        return web.Response(text=html,
                            content_type="text/html", charset="utf-8",
                            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})

    async def app_auth(request):
        uid = check_init_data((await request.text())[:4096])
        if uid is None:
            return web.json_response({"error": "Открой архив кнопкой в боте"}, status=403)
        activate_user(uid)
        return web.json_response({"base": f"/a/{make_token(uid, days=1)}/"})

    async def send(request):
        uid = _uid(request)
        try:
            item_id = int(request.match_info["id"])
        except ValueError:
            raise web.HTTPNotFound()
        ok = bool(send_item) and await send_item(uid, item_id)
        return web.json_response({"ok": bool(ok)}, status=200 if ok else 500)

    async def seen(request):
        _uid(request)
        try:
            item_id = int(request.match_info["id"])
        except ValueError:
            raise web.HTTPNotFound()
        if mark_seen:
            mark_seen(item_id)
        return web.json_response({"ok": True})

    def _share(request):
        """(uid, scope) по ссылке /s/<uid>.<sid>/ — или 404, если ссылку отозвали."""
        try:
            uid_s, sid = request.match_info.get("token", "").split(".", 1)
            uid = int(uid_s)
        except ValueError:
            raise web.HTTPNotFound(text="Ссылка неверна или отозвана")
        # чужой/выдуманный номер не регистрируем: иначе по /s/<любое число>.x/ заводились
        # пустые «пользователи», и фоновые задачи тратили на них бесплатный ИИ
        if uid not in db.all_users():
            raise web.HTTPNotFound(text="Ссылка неверна или отозвана")
        activate_user(uid)
        scope = get_share(sid) if get_share else None
        if not scope:
            raise web.HTTPNotFound(text="Ссылка неверна или отозвана")
        return uid, scope

    async def share_page(request):
        _share(request)
        if not request.path.endswith("/"):
            raise web.HTTPFound(request.path + "/")
        return web.Response(text=PAGE_HTML.replace("const SHARE_MODE=false;", "const SHARE_MODE=true;", 1),
                            content_type="text/html", charset="utf-8",
                            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                                     "X-Robots-Tag": "noindex"})

    async def share_data(request):
        _, scope = _share(request)
        body = json.dumps(archive_data(get_icons(), "", None, scope), ensure_ascii=False)
        resp = web.Response(text=body, content_type="application/json", charset="utf-8",
                            headers={"Cache-Control": "no-store"})
        resp.enable_compression()
        return resp

    async def share_thumb(request):
        uid, scope = _share(request)
        try:
            item_id = int(request.match_info["id"])
        except ValueError:
            raise web.HTTPNotFound()
        item = db.get_item(item_id)
        groups = {c["category"]: (c["group"] or "").strip("/") for c in db.get_categories()}
        if not item or not in_scope(item["category"], groups.get(item["category"], ""), scope):
            raise web.HTTPNotFound()   # только посты этой папки
        img = await thumb_bytes(get_client(), uid, item_id, big=request.query.get("big") == "1")
        if not img:
            raise web.HTTPNotFound()
        return web.Response(body=img, content_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    app.router.add_get("/s/{token}", share_page)
    app.router.add_get("/s/{token}/", share_page)
    app.router.add_get("/s/{token}/data.json", share_data)
    app.router.add_get("/s/{token}/thumb/{id}.jpg", share_thumb)
    async def edit_api(request):
        """Правка из архива: move / rename / trash / remind. edit — функции бота (замки и
        автозамок соблюдают они же)."""
        uid = _uid(request)
        if not edit:
            raise web.HTTPNotFound()
        op = request.match_info["op"]
        try:
            body = await request.json()
        except Exception:
            body = {}
        ids = [int(x) for x in (body.get("ids") or [body.get("id")]) if str(x).isdigit()][:500]
        if op == "move":
            n = edit["move"](ids, str(body.get("to") or ""))
            locked = [i for i in ids if (db.get_item(i) or {}).get("locked")]
            return web.json_response({"done": n, "locked": locked})
        if op == "fav" and ids:
            on = bool(body.get("on"))
            db.set_favorite(ids[0], on)
            return web.json_response({"ok": True, "on": on})
        if op == "brief" and ids and edit.get("brief"):
            text = await edit["brief"](ids[0])
            return web.json_response({"brief": text or ""})
        if op == "unlock" and ids:
            return web.json_response({"ok": edit["unlock"](ids[0])})
        if op == "rename":
            ok = bool(ids) and edit["rename"](ids[0], str(body.get("summary") or "").strip()[:300])
            return web.json_response({"ok": ok})
        if op == "trash":
            return web.json_response({"done": edit["trash"](ids)})
        if op == "remind" and ids:
            return web.json_response({"label": edit["remind"](uid, ids[0], str(body.get("kind") or "tom"))})
        raise web.HTTPNotFound()

    app.router.add_post("/a/{token}/edit/{op}", edit_api)
    app.router.add_post("/a/{token}/seen/{id}", seen)
    app.router.add_get("/app", app_page)
    app.router.add_get("/app/", app_page)
    app.router.add_post("/app/auth", app_auth)
    app.router.add_post("/a/{token}/send/{id}", send)
    app.router.add_get("/a/{token}", page)
    app.router.add_get("/a/{token}/", page)
    app.router.add_get("/a/{token}/data.json", data)
    app.router.add_get("/a/{token}/thumb/{id}.jpg", thumb)
    app.router.add_get("/a/{token}/search", search)
    app.router.add_get("/a/{token}/article/{id}", article)


PAGE_HTML = r"""<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex">
<title>Мой архив</title>
<!--TG-->
<style>
:root{
  --bg:#f4f6f9; --panel:#ffffff; --card:#ffffff; --ink:#151a21; --muted:#5d6b7a; --line:#dfe4ea;
  --accent:#2a7ae2; --accent-ink:#fff; --chip:#e8eef6; --thumb:#e9edf2; --shadow:0 1px 3px rgba(20,30,50,.08);
}
@media (prefers-color-scheme: dark){:root{
  --bg:#0e1621; --panel:#17212b; --card:#182533; --ink:#e9eef3; --muted:#8fa3b8; --line:#26364a;
  --accent:#4f9ef0; --accent-ink:#06121f; --chip:#22324a; --thumb:#1f2d3d; --shadow:none;
}}
*{box-sizing:border-box}
[hidden]{display:none!important}
html,body{margin:0;background:var(--bg);color:var(--ink);font:17px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
button,input,select{font:inherit;color:inherit}
*{scrollbar-width:thin;scrollbar-color:var(--line) transparent}
html:has(dialog[open]){overflow:hidden}  /* под открытой карточкой страница не крутится — один скролл, а не два */
.top{position:sticky;top:0;z-index:5;background:var(--panel);border-bottom:1px solid var(--line);
  padding:calc(12px + var(--tg-safe-area-inset-top,0px) + var(--tg-content-safe-area-inset-top,0px)) 16px 12px}
.bar{display:flex;gap:10px;align-items:center;max-width:1600px;margin:0 auto}
h1{font-size:22px;margin:0;white-space:nowrap}
.count{color:var(--muted);font-size:15px;white-space:nowrap}
.search{flex:1;display:flex;gap:8px;min-width:0}
.search input{flex:1;min-width:0;padding:12px 14px;border:1px solid var(--line);border-radius:12px;background:var(--bg);font-size:17px}
.btn{border:0;border-radius:12px;padding:12px 16px;background:var(--chip);cursor:pointer;white-space:nowrap}
.btn.primary{background:var(--accent);color:var(--accent-ink);font-weight:600}
.chips{display:flex;gap:8px;max-width:1600px;margin:10px auto 0}
.chip{border:0;border-radius:999px;padding:8px 14px;background:var(--chip);cursor:pointer;white-space:nowrap;font-size:15px;
  flex:1 1 0}  /* во всю ширину, поровну */
/* не влезают в строку — ровная сетка 3×2, а не «пять и один» */
@media (max-width: 620px){ .chips{display:grid;grid-template-columns:repeat(3,1fr)} .chip{padding:8px 6px} }
.chip.on{background:var(--accent);color:var(--accent-ink)}
.layout{display:grid;grid-template-columns:300px 1fr;gap:20px;max-width:1600px;margin:0 auto;padding:16px}
/* меню слева — ровно по окну под шапкой (её высоту меряет скрипт: в полноэкранном Telegram она выше),
   длинный список крутится внутри меню, страницу двигать не нужно */
.side{position:sticky;top:calc(var(--toph,120px) + 16px);align-self:start;
  max-height:calc(100dvh - var(--toph,120px) - 32px);overflow:auto;background:var(--panel);
  border-radius:14px;padding:8px;box-shadow:var(--shadow)}
.node{display:flex;align-items:center;gap:8px;width:100%;text-align:left;border:0;background:none;border-radius:10px;
  padding:9px 10px;cursor:pointer;font-size:16px}
.node:hover{background:var(--chip)}
.node.on{background:var(--accent);color:var(--accent-ink)}
.node .n{margin-left:auto;color:var(--muted);font-size:14px}
.node.on .n{color:inherit}
.kids{padding-left:16px}
.sidehead{color:var(--muted);font-size:14px;margin:14px 10px 4px}
.crumb{color:var(--muted);margin:0 0 12px;font-size:15px}
.note{background:var(--chip);border-radius:12px;padding:10px 14px;margin:0 0 12px;font-size:15px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:14px}
.card{background:var(--card);border-radius:14px;overflow:hidden;cursor:pointer;box-shadow:var(--shadow);display:flex;flex-direction:column;
  border:1px solid var(--line)}
.card:focus-visible{outline:3px solid var(--accent)}
.th{aspect-ratio:4/3;background:var(--thumb);display:flex;align-items:center;justify-content:center;font-size:46px;position:relative;overflow:hidden}
.th img{width:100%;height:100%;object-fit:cover;display:block}
.th.tx{flex-direction:column;gap:8px}
.th .big{font-size:52px;line-height:1}
.th .dom{font-size:14px;color:var(--muted);max-width:90%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.th .newb{position:absolute;left:8px;top:8px;font-size:15px;background:rgba(0,0,0,.55);border-radius:8px;padding:2px 6px}
.th .kind{position:absolute;right:8px;bottom:8px;font-size:14px;background:rgba(0,0,0,.55);color:#fff;border-radius:8px;padding:2px 7px}
.cb{padding:10px 12px 12px;display:flex;flex-direction:column;gap:6px;flex:1}
.sum{display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;font-size:16px}
.meta{color:var(--muted);font-size:14px;margin-top:auto;display:flex;gap:6px;flex-wrap:wrap}
.more{display:block;margin:20px auto;min-width:240px}
.empty{color:var(--muted);text-align:center;padding:60px 10px;font-size:18px}
.navbtn{display:none}
.fsbtn{padding:10px 14px;font-size:15px;order:4}  /* широкий экран — справа от поиска */
dialog{border:0;border-radius:18px;padding:0;max-width:760px;width:calc(100% - 32px);background:var(--panel);color:var(--ink);overflow:hidden}
dialog::backdrop{background:rgba(0,0,0,.6)}
.dlg{display:flex;flex-direction:column;max-height:90vh}
.dlg .scroll{flex:1;min-height:0;overflow:auto}  /* картинка и текст прокручиваются вместе */
.dlg .img{background:var(--thumb);display:flex;justify-content:center}
.dlg .img img{max-width:100%;max-height:60vh;display:block}
.dlg .body{padding:16px 18px}
.dlg h2{font-size:19px;margin:0 0 8px}
.dlg .full{white-space:pre-wrap;margin:10px 0}
.dlg .full a{color:var(--accent)}
.dlg .ai{background:var(--chip);border-radius:12px;padding:10px 12px;white-space:pre-wrap;font-size:15px}
.dlg .art{background:var(--chip);border-radius:12px;padding:10px 12px;margin-top:10px}
.dlg .art summary{cursor:pointer;font-weight:600;font-size:16px}
.dlg .art .full{font-size:16px;line-height:1.5}
.dlg .art #dbrief{margin-top:10px}
.dlg .art #dbriefx{background:var(--bg);border-radius:10px;padding:10px 12px}
.dlg .star{float:right;margin:0 0 8px 12px;padding:6px 12px;font-size:20px;line-height:1}
.dlg .star.on{background:#6b5200;color:#ffd54a}
.th .favb{position:absolute;right:6px;top:6px;font-size:18px;text-shadow:0 1px 3px #000}
.dlg .acts{display:flex;gap:10px;padding:12px 18px;border-top:1px solid var(--line);flex-wrap:wrap}
.dlg .acts a,.dlg .acts button{flex:1;text-align:center;text-decoration:none}
.sheet{display:none}
.editrow{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;padding:0 18px 12px}
.editrow .btn{padding:10px 6px;font-size:15px}
.editrow .btn.danger{background:#7a2b2b;color:#fff}
.inline{display:grid;gap:8px;margin:10px 0}
.inline input{padding:10px 12px;border:1px solid var(--line);border-radius:10px;background:var(--bg);font-size:16px}
.choices{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin:10px 0}
.picker .list{max-height:55vh;overflow:auto;display:grid;gap:6px;margin-top:10px}
.picker .node{background:var(--chip)}
.selbar{position:fixed;left:50%;transform:translateX(-50%);bottom:calc(16px + var(--tg-safe-area-inset-bottom,0px));z-index:15;
  display:flex;gap:8px;align-items:center;background:var(--panel);border:1px solid var(--line);border-radius:16px;
  padding:10px 12px;box-shadow:0 6px 24px rgba(0,0,0,.35);max-width:calc(100% - 24px);flex-wrap:wrap;justify-content:center}
.card.picked{outline:3px solid var(--accent);outline-offset:-3px}
.card .tick{position:absolute;left:8px;top:8px;width:28px;height:28px;border-radius:8px;background:rgba(0,0,0,.55);
  color:#fff;display:flex;align-items:center;justify-content:center;font-size:18px}
.crumbrow{display:flex;align-items:center;justify-content:space-between;gap:10px;margin:0 0 12px}
.crumbrow .crumb{margin:0}
.toast{position:fixed;left:50%;transform:translateX(-50%);top:calc(var(--toph,100px) + 12px);z-index:30;background:var(--accent);
  color:var(--accent-ink);padding:10px 16px;border-radius:12px;font-size:15px;box-shadow:0 6px 20px rgba(0,0,0,.3)}
@media (max-width: 480px){ .editrow{grid-template-columns:repeat(2,1fr)} }
@media (max-width: 900px){
  .layout{grid-template-columns:1fr;padding:12px}
  .side{display:none}
  .navbtn{display:inline-block}
  h1{font-size:19px}.count{display:none}
  .bar{flex-wrap:wrap}.search{flex-basis:100%;order:3}
  .fsbtn{order:2;margin-left:auto}  /* узкий — в первой строке, справа */
  .grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
  .sum{font-size:15.5px}.th{font-size:38px}
  .sheet.open{display:block;position:fixed;inset:0;z-index:20;background:var(--panel);overflow:auto;padding:12px 12px 40px}
  .sheet .close{position:sticky;top:0;width:100%;margin-bottom:8px}
  dialog{width:100%;max-width:none;height:100%;max-height:none;margin:0;border-radius:0}
  .dlg{max-height:100%;height:100%}
  .dlg .img img{max-height:55vh}
  .dlg .acts{padding-bottom:calc(12px + var(--tg-safe-area-inset-bottom,0px))}
}
@media (max-width: 340px){ .grid{grid-template-columns:1fr} }
@media (max-width: 480px){ .sum{font-size:15px;-webkit-line-clamp:4} .cb{padding:8px 10px 10px} .meta{font-size:13px}
  h1{font-size:18px} .bar{gap:8px} .navbtn,.fsbtn{padding:9px 11px;font-size:15px} }
</style></head><body>
<div class="top">
  <div class="bar">
    <h1>📚 Мой архив</h1><span class="count" id="count"></span>
    <button class="btn navbtn" id="navbtn">📂 Папки</button>
    <button class="btn fsbtn" id="fsbtn" hidden title="Во весь экран">⛶</button>
    <form class="search" id="sform">
      <input id="q" type="search" placeholder="Поиск по архиву" autocomplete="off">
      <button class="btn primary" type="button" id="smart" title="ИИ поймёт описание своими словами">🤖 По смыслу</button>
    </form>
  </div>
  <div class="chips" id="types"></div>
</div>
<div class="layout">
  <nav class="side" id="side"></nav>
  <main>
    <div class="crumbrow"><p class="crumb" id="crumb"></p>
      <button class="btn" id="selbtn" hidden>☑️ Выбрать</button></div>
    <div class="note" id="note" hidden></div>
    <div class="grid" id="grid"></div>
    <button class="btn more" id="more" hidden>Показать ещё</button>
    <div class="empty" id="empty" hidden>Ничего не нашлось</div>
  </main>
</div>
<div class="sheet" id="sheet"><button class="btn close" id="sheetclose">✕ Закрыть</button><div id="sheettree"></div></div>
<dialog id="dlg"><div class="dlg">
  <div class="scroll" id="dscroll"><div class="img" id="dimg"></div>
  <div class="body"><button class="btn star" id="efav" hidden>☆</button><h2 id="dsum"></h2><div class="meta" id="dmeta"></div><div class="full" id="dtext"></div>
    <div class="ai" id="dai" hidden></div>
    <details class="art" id="dart" hidden><summary id="dartt">📄 Текст статьи</summary>
      <button class="btn" id="dbrief">🧠 Кратко по-русски</button><div class="full" id="dbriefx" hidden></div>
      <div class="full" id="dartx"></div></details></div></div>
  <div class="acts"><a class="btn primary" id="dopen" target="_blank" rel="noopener">Открыть в Telegram</a>
    <button class="btn primary" id="dsend" hidden>📤 Прислать в чат</button>
    <button class="btn" id="dclose">Закрыть</button></div>
  <div class="editrow" id="editrow" hidden>
    <button class="btn" id="emove">📂 Перенести</button><button class="btn" id="eren">✏️ Название</button>
    <button class="btn" id="erem">⏰ Напомнить</button><button class="btn" id="etrash">🗑 В корзину</button>
    <button class="btn" id="eunlock" hidden>🔓 Снять замок</button>
  </div>
</div></dialog>
<dialog id="pick" class="picker"><div class="dlg"><div class="body">
  <h2 id="ptitle">📂 В какую папку?</h2>
  <input id="pq" type="search" placeholder="Найти папку" autocomplete="off" style="width:100%;padding:12px;border-radius:12px;border:1px solid var(--line);background:var(--bg);font-size:16px">
  <div class="list" id="plist"></div></div>
  <div class="acts"><button class="btn" id="pclose">Отмена</button></div></div></dialog>
<div class="selbar" id="selbar" hidden><b id="selcount">Выбрано: 0</b>
  <button class="btn primary" id="selmove">📂 Перенести</button><button class="btn" id="seltrash">🗑 В корзину</button>
  <button class="btn" id="selexit">✖ Готово</button></div>
<script>
const APP_MODE=false;
const SHARE_MODE=false;
const TG=(window.Telegram&&Telegram.WebApp&&Telegram.WebApp.initData)?Telegram.WebApp:null;
let BASE="";
const TYPES=[["","Все"],["photo","🖼 Фото"],["video","🎬 Видео"],["text","📝 Текст"],["audio","🎵 Аудио"],["document","📄 Файлы"]];
const KIND={photo:"🖼",video:"🎬",animation:"🎞",video_note:"⏺",text:"📝",audio:"🎵",voice:"🎤",document:"📄"};
const GROUP={video:["video","animation","video_note"],audio:["audio","voice"],photo:["photo"],text:["text"],document:["document"]};
const PAGE=60;
let D=null, sel={kind:"all",val:""}, type="", q="", smartIds=null, shown=0, list=[];
let selMode=false, picked=new Set(), cur=null;
const $=id=>document.getElementById(id);
const topEl=document.querySelector(".top");
const setTopH=()=>document.documentElement.style.setProperty("--toph",topEl.offsetHeight+"px");
new ResizeObserver(setTopH).observe(topEl);setTopH();
const norm=s=>(s||"").toLocaleLowerCase("ru").replace(/ё/g,"е");
function el(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=text;return e}
function icon(name,fb){return (D.icons&&D.icons[name])||fb}
function dateRu(d){if(!d)return"";const [y,m,dd]=d.slice(0,10).split("-");return `${dd}.${m}.${y}`}

function tree(){
  const root={folders:{},cats:[],count:0};
  const counts={};D.items.forEach(i=>counts[i.c]=(counts[i.c]||0)+1);
  const ensure=p=>{let n=root,cur="";for(const seg of p.split("/")){cur=cur?cur+"/"+seg:seg;
    n.folders[seg]=n.folders[seg]||{name:seg,path:cur,folders:{},cats:[],count:0};n=n.folders[seg]}return n};
  (D.folders||[]).forEach(p=>ensure(p));
  Object.keys(counts).forEach(c=>{const g=D.groups[c]||"";(g?ensure(g):root).cats.push({name:c,count:counts[c]})});
  const total=n=>{n.count=n.cats.reduce((a,c)=>a+c.count,0)+Object.values(n.folders).reduce((a,f)=>a+total(f),0);return n.count};
  total(root);return root;
}
function renderTree(box){
  box.textContent="";
  const all=el("button","node"+(sel.kind==="all"?" on":""));all.append("📚 Все посты",el("span","n",D.items.length));
  all.onclick=()=>pick("all","");box.append(all);
  const nn=D.items.filter(i=>i.nw).length;
  if(nn){const nb=el("button","node"+(sel.kind==="new"?" on":""));nb.append("🆕 Ещё не смотрел",el("span","n",nn));
    nb.onclick=()=>pick("new","");box.append(nb)}
  const nf=D.items.filter(i=>i.fv).length;
  if(nf){const fb=el("button","node"+(sel.kind==="fav"?" on":""));fb.append("⭐ Избранное",el("span","n",nf));
    fb.onclick=()=>pick("fav","");box.append(fb)}
  // 🏷 Темы: игры, тайтлы, бренды — по всем папкам сразу
  const tc={};D.items.forEach(i=>(i.tp||[]).forEach(t=>tc[t]=(tc[t]||0)+1));
  const top=Object.entries(tc).filter(([,n])=>n>=2).sort((a,b)=>b[1]-a[1]).slice(0,25);
  const walk=(n,parent)=>{
    Object.values(n.folders).sort((a,b)=>b.count-a.count).forEach(f=>{
      const b=el("button","node"+(sel.kind==="folder"&&sel.val===f.path?" on":""));
      b.append(`📁 ${icon(f.path,icon(f.name,""))} ${f.name}`.replace("  "," "),el("span","n",f.count));
      b.onclick=()=>pick("folder",f.path);parent.append(b);
      const k=el("div","kids");parent.append(k);walk(f,k);
    });
    // «Технологии» внутри папки «Технологии» — это посты самой папки, отдельной строкой не рисуем
    n.cats.filter(c=>!(n.name&&c.name===n.name)).sort((a,b)=>b.count-a.count).forEach(c=>{
      const b=el("button","node"+(sel.kind==="cat"&&sel.val===c.name?" on":""));
      b.append(`${icon(c.name,"📦")} ${c.name}`,el("span","n",c.count));b.onclick=()=>pick("cat",c.name);parent.append(b);
    });
  };
  walk(tree(),box);
  if(top.length){box.append(el("div","sidehead","🏷 Темы"));
    top.forEach(([t,n])=>{const b=el("button","node"+(sel.kind==="topic"&&sel.val===t?" on":""));
      b.append(`🏷 ${t}`,el("span","n",n));b.onclick=()=>pick("topic",t);box.append(b)})}
}
function pick(kind,val){sel={kind,val};$("sheet").classList.remove("open");apply();window.scrollTo({top:0})}
function inSel(i){
  if(sel.kind==="new")return i.nw;
  if(sel.kind==="fav")return i.fv;
  if(sel.kind==="topic")return (i.tp||[]).includes(sel.val);
  if(sel.kind==="cat")return i.c===sel.val;
  if(sel.kind==="folder"){const g=D.groups[i.c]||"";return g===sel.val||g.startsWith(sel.val+"/")}
  return true;
}
function apply(){
  const words=norm(q).split(/\s+/).filter(Boolean);
  const kinds=type?GROUP[type]:null;
  let arr=D.items.filter(i=>inSel(i)&&(!kinds||kinds.includes(i.t)));
  if(smartIds){const pos=new Map(smartIds.map((id,k)=>[id,k]));arr=arr.filter(i=>pos.has(i.id)).sort((a,b)=>pos.get(a.id)-pos.get(b.id))}
  else if(words.length){arr=arr.filter(i=>{const h=norm(`${i.s} ${i.x} ${i.v} ${i.c} ${i.ch} ${i.tg} ${(i.tp||[]).join(" ")}`);return words.every(w=>h.includes(w))})}
  list=arr;shown=0;$("grid").textContent="";
  $("crumb").textContent=(sel.kind==="topic"?"🏷 "+sel.val:sel.kind==="new"?"🆕 Ещё не смотрел":sel.kind==="fav"?"⭐ Избранное":sel.kind==="all"?"Все посты":(sel.kind==="folder"?"📁 "+sel.val:icon(sel.val,"📦")+" "+sel.val))+` · ${arr.length}`;
  $("empty").hidden=arr.length>0;
  renderTree($("side"));renderTree($("sheettree"));more();
}
function card(i){
  const c=el("div","card");c.tabIndex=0;c.setAttribute("role","button");
  const th=el("div","th");
  const media=["photo","video","animation","video_note","document","audio"].includes(i.t);
  if(media){const img=new Image();img.loading="lazy";img.alt="";img.src=`${BASE}thumb/${i.id}.jpg`;
    img.onerror=()=>{img.remove();th.prepend(KIND[i.t]||"📦")};th.append(img)}
  else{th.classList.add("tx");th.append(el("div","big",icon(i.c,KIND[i.t]||"📝")));
    const m=(i.x||"").match(/https?:\/\/(?:www\.)?([^\/\s)]+)/);if(m)th.append(el("div","dom","🔗 "+m[1]))}
  if(i.t!=="text"&&i.t!=="photo")th.append(el("span","kind",KIND[i.t]||""));
  if(i.nw)th.append(el("span","newb","🆕"));
  if(i.fv)th.append(el("span","favb","⭐"));
  if(selMode){th.append(el("span","tick",picked.has(i.id)?"✓":""));if(picked.has(i.id))c.classList.add("picked")}
  const cb=el("div","cb");cb.append(el("div","sum",i.s||"—"));
  cb.append(el("div","meta",`${icon(i.c,"📦")} ${i.c} · ${dateRu(i.d)}`));
  c.append(th,cb);c.onclick=()=>selMode?togglePick(i,c):open(i);c.onkeydown=e=>{if(e.key==="Enter")open(i)};
  return c;
}
function more(){const g=$("grid");list.slice(shown,shown+PAGE).forEach(i=>g.append(card(i)));shown+=PAGE;$("more").hidden=shown>=list.length}
function open(i){
  const img=$("dimg");img.textContent="";
  if(["photo","video","animation","video_note","document","audio"].includes(i.t)){
    const im=new Image();im.src=`${BASE}thumb/${i.id}.jpg${i.t==="photo"?"?big=1":""}`;im.alt="";im.onerror=()=>im.remove();img.append(im)}
  $("dsum").textContent=i.s||"—";
  $("dmeta").textContent=`${icon(i.c,"📦")} ${i.c} · ${dateRu(i.d)}${i.ch?" · 📡 "+i.ch:""}${i.tg?" · "+i.tg:""}${(i.tp||[]).length?" · 🏷 "+i.tp.join(", "):""} · #${i.id}`;
  renderText($("dtext"),i.x&&i.x!==i.s?i.x:"");
  $("dai").hidden=!i.v;$("dai").textContent=i.v?"👁 Что увидел ИИ:\n"+i.v:"";
  const da=$("dart");da.hidden=!i.ar;da.open=false;da.dataset.id=i.id;da.dataset.ok="";
  $("dartt").textContent="📄 Статья: "+(i.ar||"");$("dartx").textContent="";
  $("dbrief").hidden=!CAN_EDIT;$("dbrief").disabled=false;$("dbriefx").hidden=true;$("dbriefx").textContent="";
  const st=$("efav");st.hidden=!CAN_EDIT;st.textContent=i.fv?"⭐":"☆";st.classList.toggle("on",!!i.fv);
  st.title=i.fv?"Убрать из избранного":"В избранное";
  $("dopen").href=D.bot?`https://t.me/${D.bot}?start=i${i.id}`:"#";$("dopen").hidden=!D.bot||APP_MODE||SHARE_MODE;
  $("dsend").hidden=!APP_MODE;$("dsend").dataset.id=i.id;$("dsend").disabled=false;$("dsend").textContent="📤 Прислать в чат";
  cur=i;renderEdit();
  $("dlg").showModal();$("dscroll").scrollTop=0;
  if(i.nw){i.nw=false;fetch(BASE+"seen/"+i.id,{method:"POST"}).catch(()=>{})}
  if(TG){TG.BackButton.show()}
}
function closeDlg(){$("dlg").close()}
$("dart").addEventListener("toggle",()=>{const da=$("dart");if(!da.open||da.dataset.ok)return;
  const box=$("dartx");box.textContent="Загружаю…";
  fetch(BASE+"article/"+da.dataset.id).then(r=>r.ok?r.json():Promise.reject()).then(a=>{da.dataset.ok="1";
    box.textContent="";if(a.url){const l=document.createElement("a");l.href=a.url;l.target="_blank";l.rel="noopener";
      l.textContent="🔗 Открыть оригинал";box.append(l,"\n\n")}box.append(a.text)})
  .catch(()=>{box.textContent="Не удалось загрузить текст"})});
function renderText(box,md){
  // разметка Telegram → текст: [подпись](ссылка) — кликабельно, **жирный**/__курсив__ — без звёздочек
  box.textContent="";const re=/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g;let pos=0,m;
  const clean=s=>s.replace(/\*\*|__|~~|`/g,"");
  while((m=re.exec(md))){box.append(clean(md.slice(pos,m.index)));
    const a=el("a",null,clean(m[1]));a.href=m[2];a.target="_blank";a.rel="noopener";box.append(a);pos=re.lastIndex}
  box.append(clean(md.slice(pos)));
}
$("dlg").addEventListener("close",()=>{if(TG)TG.BackButton.hide()});
$("dsend").onclick=async()=>{
  const b=$("dsend");b.disabled=true;b.textContent="📤 Отправляю…";
  try{const r=await fetch(BASE+"send/"+b.dataset.id,{method:"POST"});
    b.textContent=r.ok?"✅ В чате с ботом":"Не вышло, попробуй ещё";
    if(r.ok&&TG&&TG.HapticFeedback)TG.HapticFeedback.notificationOccurred("success")}
  catch(e){b.textContent="Не вышло: "+e}finally{setTimeout(()=>b.disabled=false,1500)}
};
$("dclose").onclick=closeDlg;

// ── Правка из архива (только владелец; гостю по ссылке — нет) ──
const CAN_EDIT=!SHARE_MODE;
function toast(t){document.querySelectorAll(".toast").forEach(x=>x.remove());
  const e=el("div","toast",t);document.body.append(e);setTimeout(()=>e.remove(),2600)}
function setCount(){$("count").textContent=`${D.items.length} постов`}
async function api(op,body){const r=await fetch(BASE+"edit/"+op,{method:"POST",headers:{"Content-Type":"application/json"},
  body:JSON.stringify(body)});if(!r.ok)throw new Error(r.status);return r.json()}
function catLabel(c){const g=(D.groups[c]||"").split("/").filter(Boolean);
  if(g[g.length-1]===c)g.pop();return [...g,c].join(" / ")}
function renderEdit(){
  const row=$("editrow");row.hidden=!CAN_EDIT;if(!CAN_EDIT||!cur)return;
  $("etrash").textContent="🗑 В корзину";$("etrash").classList.remove("danger");
  // заблокированный пост (замок ставит и автозамок после ручного переноса) — сначала «🔓 Снять замок»
  ["emove","eren","etrash"].forEach(id=>$(id).hidden=!!cur.l);$("eunlock").hidden=!cur.l;
}
function rerender(){const y=scrollY;apply();scrollTo({top:y})}
function openPicker(ids){
  const cats=[...new Set(D.items.map(i=>i.c))].sort((a,b)=>catLabel(a).localeCompare(catLabel(b),"ru"));
  const draw=()=>{const f=norm($("pq").value);$("plist").textContent="";
    cats.filter(c=>norm(catLabel(c)).includes(f)).forEach(c=>{const b=el("button","node",`📁 ${icon(c,"📦")} ${catLabel(c)}`);
      b.onclick=async()=>{try{const j=await api("move",{ids,to:c});
        D.items.forEach(i=>{if(ids.includes(i.id)&&!i.l)i.c=c;if((j.locked||[]).includes(i.id))i.l=true});$("pick").close();
        toast(`📂 Перенесено: ${j.done} → «${c}»`+((j.locked||[]).length?" · 🔒 автозамок":""));renderEdit();
        if($("dlg").open&&cur){$("dmeta").textContent=`${icon(cur.c,"📦")} ${cur.c} · ${dateRu(cur.d)} · #${cur.id}`}
        exitSel(false);rerender()}catch(e){toast("Не вышло: "+e.message)}};$("plist").append(b)})};
  $("ptitle").textContent=ids.length>1?`📂 Перенести ${ids.length} постов в папку`:"📂 В какую папку?";
  $("pq").value="";$("pq").oninput=draw;draw();$("pick").showModal();
}
$("pclose").onclick=()=>$("pick").close();
$("efav").onclick=async()=>{if(!cur)return;const on=!cur.fv;
  try{await api("fav",{id:cur.id,on});cur.fv=on;const st=$("efav");st.textContent=on?"⭐":"☆";st.classList.toggle("on",on);
    toast(on?"⭐ В избранном":"Убрано из избранного");rerender()}catch(e){toast("Не вышло: "+e.message)}};
$("dbrief").onclick=async()=>{if(!cur)return;const b=$("dbrief"),x=$("dbriefx");b.disabled=true;b.textContent="🧠 Читаю…";
  try{const j=await api("brief",{id:cur.id});x.hidden=false;
    if(j.brief){renderText(x,j.brief);b.hidden=true}else{x.textContent="Бесплатный ИИ сейчас занят — попробуй чуть позже."}}
  catch(e){x.hidden=false;x.textContent="Не вышло: "+e.message}
  finally{b.disabled=false;b.textContent="🧠 Кратко по-русски"}};
$("eunlock").onclick=async()=>{try{const j=await api("unlock",{id:cur.id});
  if(j.ok){cur.l=false;renderEdit();toast("🔓 Замок снят")}}catch(e){toast("Не вышло: "+e.message)}};
$("emove").onclick=()=>cur&&openPicker([cur.id]);
$("eren").onclick=()=>{
  if(!cur||$("renbox"))return;const box=el("div","inline");box.id="renbox";
  const inp=el("input");inp.value=cur.s||"";const ok=el("button","btn primary","✅ Сохранить");box.append(inp,ok);
  $("dsum").after(box);inp.focus();
  ok.onclick=async()=>{const s=inp.value.trim();if(!s)return;
    try{const j=await api("rename",{id:cur.id,summary:s});
      if(!j.ok){toast("🔒 Не сохранилось: пост заблокирован");return}
      cur.s=s;$("dsum").textContent=s;box.remove();toast("✏️ Название сохранено");rerender()}
    catch(e){toast("Не вышло: "+e.message)}};
};
$("erem").onclick=()=>{
  if(!cur||$("rembox"))return;const box=el("div","choices");box.id="rembox";
  [["tom","Завтра утром"],["d3","Через 3 дня"],["sat","В субботу"],["w","Через неделю"]].forEach(([k,l])=>{
    const b=el("button","btn","⏰ "+l);b.onclick=async()=>{try{const j=await api("remind",{id:cur.id,kind:k});
      box.remove();toast(`⏰ Напомню ${j.label} в 9:00 — пришлю в чат с ботом`)}catch(e){toast("Не вышло: "+e.message)}};box.append(b)});
  $("dsum").after(box);
};
$("etrash").onclick=async()=>{
  const b=$("etrash");
  if(!b.classList.contains("danger")){b.classList.add("danger");b.textContent="Точно? Нажми ещё раз";return}
  try{const j=await api("trash",{ids:[cur.id]});
    if(j.done){D.items=D.items.filter(i=>i.id!==cur.id);setCount();closeDlg();toast("🗑 В корзине — вернуть можно в боте");rerender()}
    else toast("🔒 Не удалилось: пост заблокирован")}
  catch(e){toast("Не вышло: "+e.message)}
};
// выбор нескольких
function togglePick(i,c){if(i.l){toast("🔒 Пост заблокирован");return}
  picked.has(i.id)?picked.delete(i.id):picked.add(i.id);
  c.classList.toggle("picked",picked.has(i.id));c.querySelector(".tick").textContent=picked.has(i.id)?"✓":"";
  $("selcount").textContent="Выбрано: "+picked.size}
function exitSel(re=true){selMode=false;picked.clear();$("selbar").hidden=true;$("selbtn").textContent="☑️ Выбрать";if(re)rerender()}
$("selbtn").onclick=()=>{if(selMode){exitSel();return}selMode=true;$("selbar").hidden=false;
  $("selbtn").textContent="✖ Отменить выбор";$("selcount").textContent="Выбрано: 0";rerender()};
$("selexit").onclick=()=>exitSel();
$("selmove").onclick=()=>{if(!picked.size){toast("Отметь посты");return}openPicker([...picked])};
$("seltrash").onclick=async()=>{if(!picked.size){toast("Отметь посты");return}
  const b=$("seltrash");if(!b.classList.contains("danger")){b.classList.add("danger");b.textContent=`Точно ${picked.size}?`;return}
  b.classList.remove("danger");b.textContent="🗑 В корзину";
  try{const ids=[...picked];const j=await api("trash",{ids});D.items=D.items.filter(i=>!ids.includes(i.id));setCount();
    toast(`🗑 В корзине: ${j.done}`);exitSel()}catch(e){toast("Не вышло: "+e.message)}};
$("dlg").addEventListener("click",e=>{if(e.target===$("dlg"))$("dlg").close()});
$("more").onclick=more;
new IntersectionObserver(es=>{if(es[0].isIntersecting&&!$("more").hidden)more()}).observe($("more"));
let t=null;$("q").addEventListener("input",e=>{q=e.target.value;smartIds=null;$("note").hidden=true;clearTimeout(t);t=setTimeout(apply,150)});
$("sform").onsubmit=e=>{e.preventDefault();apply()};
$("smart").onclick=async()=>{
  if(!q.trim()){$("q").focus();return}
  const b=$("smart");b.disabled=true;b.textContent="🤖 Ищу…";
  try{const r=await fetch(BASE+"search?q="+encodeURIComponent(q));const j=await r.json();smartIds=j.ids;
    $("note").hidden=false;$("note").textContent=`🤖 По смыслу: «${q}» — найдено ${j.ids.length}.`+(j.note?" "+j.note:"")+" Измени текст, чтобы искать по словам.";
    sel={kind:"all",val:""};apply()}
  catch(e){$("note").hidden=false;$("note").textContent="Не получилось: "+e}
  finally{b.disabled=false;b.textContent="🤖 По смыслу"}
};
$("navbtn").onclick=()=>$("sheet").classList.add("open");$("sheetclose").onclick=()=>$("sheet").classList.remove("open");
TYPES.forEach(([k,l])=>{const c=el("button","chip"+(k===type?" on":""),l);c.onclick=()=>{type=k;
  document.querySelectorAll(".chip").forEach(x=>x.classList.toggle("on",x===c));apply()};$("types").append(c)});
function tgTheme(){
  // цвета берём у Telegram — архив выглядит как часть приложения (и меняется вместе с темой)
  const p=TG.themeParams||{},s=document.documentElement.style,set=(v,c)=>{if(c)s.setProperty(v,c)};
  set("--bg",p.secondary_bg_color||p.bg_color);set("--panel",p.bg_color);set("--card",p.section_bg_color||p.bg_color);
  set("--ink",p.text_color);set("--muted",p.hint_color||p.subtitle_text_color);set("--accent",p.button_color);
  set("--accent-ink",p.button_text_color);set("--chip",p.secondary_bg_color);set("--thumb",p.secondary_bg_color);
  set("--line",p.section_separator_color||"transparent");
}
async function boot(){
  if(APP_MODE){
    if(!TG){throw new Error("открой архив кнопкой «📚 Архив» в чате с ботом")}
    TG.ready();TG.expand();tgTheme();TG.onEvent("themeChanged",tgTheme);
    // «Во весь экран» (Telegram 8.0+). Режим запоминается отдельно для компьютера и телефона
    // (облачное хранилище Telegram + память устройства). Пока не выбирал: на компьютере —
    // весь экран (окно мини-приложения там маленькое), на телефоне — обычное окно.
    if(TG.requestFullscreen&&TG.isVersionAtLeast&&TG.isVersionAtLeast("8.0")){
      const fb=$("fsbtn");fb.hidden=false;
      const desktop=["tdesktop","macos","weba","webk","web","unigram"].includes(TG.platform);
      const KEY=desktop?"fs_desktop":"fs_mobile";
      const sync=()=>{const narrow=innerWidth<480;
        fb.textContent=TG.isFullscreen?(narrow?"⤡ В окно":"⤡ Свернуть в окно"):(narrow?"⤢ Весь экран":"⤢ На весь экран");
        fb.title=TG.isFullscreen?"Вернуть обычное окно Telegram":"Развернуть архив на весь экран"};
      const save=v=>{try{localStorage.setItem(KEY,v)}catch(e){}
        try{if(TG.CloudStorage&&TG.isVersionAtLeast("6.9"))TG.CloudStorage.setItem(KEY,v)}catch(e){}};
      const load=()=>new Promise(res=>{
        let local=null;try{local=localStorage.getItem(KEY)}catch(e){}
        try{if(TG.CloudStorage&&TG.isVersionAtLeast("6.9")){
          setTimeout(()=>res(local),1500);   // облако не ответило — берём память устройства
          TG.CloudStorage.getItem(KEY,(err,v)=>res(!err&&v?v:local));return}}catch(e){}
        res(local)});
      addEventListener("resize",sync);
      fb.onclick=()=>{try{TG.isFullscreen?TG.exitFullscreen():TG.requestFullscreen()}catch(e){}};
      let saveTimer=null;
      TG.onEvent("fullscreenChanged",()=>{sync();
        // сохраняем с задержкой: если окно закрывают, страница выгрузится раньше
        // и «вышел из полного экрана при закрытии» не запишется как выбор
        clearTimeout(saveTimer);saveTimer=setTimeout(()=>save(TG.isFullscreen?"1":"0"),800)});
      sync();
      load().then(v=>{const want=v===null||v===undefined||v===""?desktop:v==="1";
        if(want&&!TG.isFullscreen){try{TG.requestFullscreen()}catch(e){}}});
    }
    TG.BackButton.onClick(()=>{if($("dlg").open)closeDlg()});
    const r=await fetch("/app/auth",{method:"POST",body:TG.initData});
    if(!r.ok)throw new Error((await r.json().catch(()=>({}))).error||r.status);
    BASE=(await r.json()).base;
    document.querySelector("h1").textContent="📚 Архив";
  }
  const r=await fetch(BASE+"data.json");if(!r.ok)throw new Error(r.status===404?"ссылку отозвали":r.status);
  D=await r.json();$("count").textContent=`${D.items.length} постов`;
  $("selbtn").hidden=SHARE_MODE;
  if(SHARE_MODE){   // гостевая страница одной папки: без ИИ-поиска и кнопок Telegram
    $("smart").remove();document.querySelector("h1").textContent="📁 "+(D.title||"Папка");
    document.title=D.title||"Папка";
  }
  apply();
}
boot().catch(e=>{$("empty").hidden=false;
  $("empty").textContent="Не удалось загрузить архив: "+e.message+(APP_MODE?"":". Ссылка могла устареть — попроси новую у бота: /web")});
</script>
</body></html>
"""
