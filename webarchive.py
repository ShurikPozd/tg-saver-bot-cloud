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
import re
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
                 scope: dict | None = None, *, dead: dict | None = None) -> dict:
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
            "dd": (dead or {}).get(r["id"], "") if not scope else "",
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
        return await _text_cover(client, uid, item, path, folder)
    data = None
    async with _dl_sem:
        try:
            msg = await asyncio.wait_for(client.get_messages(item["chat_id"], ids=item["message_id"]), 20)
            if (msg is None or not getattr(msg, "media", None)) and _GET_MIRROR:
                # исходное сообщение удалено из чата — берём страховую копию из канала-хранилища
                chan, mids = _GET_MIRROR(item_id)
                if chan and mids:
                    msg = await asyncio.wait_for(client.get_messages(chan, ids=mids[0]), 20)
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


_YT_ID = re.compile(r"(?:youtu\.be/|youtube\.com/(?:watch\?v=|shorts/|live/|embed/))([\w-]{11})", re.I)
_URL_RE = re.compile(r"https?://[^\s)\]>\"'«»]+", re.I)


def _small_jpeg(data: bytes, width: int = 480) -> bytes | None:
    """Обложка → JPEG шириной до 480 px (в сетке больше не нужно, а грузится быстро)."""
    import io

    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as im:
            im = im.convert("RGB")
            if im.width < 60 or im.height < 40:
                return None
            im.thumbnail((width, width * 2))
            out = io.BytesIO()
            im.save(out, "JPEG", quality=82)
            return out.getvalue()
    except Exception:
        return None


async def _text_cover(client, uid: int, item: dict, path: str, folder: str) -> bytes | None:
    """Обложка поста-ссылки: превью ссылки из сообщения Telegram → кадр YouTube → картинка страницы.
    Результат (и «картинки нет») кэшируется на диске, как у остальных превью."""
    import linkmeta
    import savelinks

    text = postctx.strip_aux(item.get("original_text") or "") if postctx.has_aux(item.get("original_text") or "") \
        else (item.get("original_text") or "")
    urls = [u.rstrip(".,;:!?…") for u in _URL_RE.findall(text)]
    if not urls:
        return None
    data = None
    async with _dl_sem:
        try:
            msg = await asyncio.wait_for(client.get_messages(item["chat_id"], ids=item["message_id"]), 20) \
                if client is not None and item.get("chat_id") and item.get("message_id") else None
            wp = getattr(getattr(msg, "media", None), "webpage", None)
            photo = getattr(wp, "photo", None)
            if photo is not None:
                sizes = [s for s in (getattr(photo, "sizes", None) or []) if getattr(s, "w", 0) >= 300]
                thumb = min(sizes, key=lambda s: s.w) if sizes else None
                data = await asyncio.wait_for(client.download_media(photo, file=bytes, thumb=thumb), 30)
            if not data:
                yt = next((m.group(1) for u in urls for m in [_YT_ID.search(u)] if m), None)
                if yt:
                    data = await savelinks.fetch_image(f"https://i.ytimg.com/vi/{yt}/mqdefault.jpg")
            if not data:
                for u in urls[:2]:
                    if re.search(r"(^|//)(www\.)?(t\.me|telegram\.me)/", u):
                        continue
                    img = await linkmeta.fetch_og_image(u)
                    if img:
                        data = await savelinks.fetch_image(img)
                        if data:
                            break
        except Exception as e:
            logger.info("Веб-архив: обложка #%s не скачалась: %s", item["id"], e)
            return None
    data = await asyncio.to_thread(_small_jpeg, data) if data else None
    os.makedirs(folder, exist_ok=True)
    with open(path, "wb") as f:
        f.write(data or b"")
    return data or None


_GET_MIRROR = None   # (канал, [id копий]) по id поста — ставит setup()


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


def setup(app, *, get_client, activate_user, get_icons, smart_search, get_bot_username, send_item=None, bridge=None,
          voice=None, get_dead=None, get_mirror=None,
          get_unseen=None, mark_seen=None, get_share=None, edit=None):
    from aiohttp import web

    global _GET_MIRROR
    _GET_MIRROR = get_mirror
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
        body = json.dumps(archive_data(get_icons(), get_bot_username(), get_unseen() if get_unseen else None,
                                       dead=get_dead() if get_dead else None), ensure_ascii=False)
        etag = '"' + hashlib.sha1(body.encode("utf-8")).hexdigest()[:20] + '"'
        if request.headers.get("If-None-Match") == etag:
            return web.Response(status=304, headers={"ETag": etag, "Cache-Control": "no-cache"})
        resp = web.Response(text=body, content_type="application/json", charset="utf-8",
                            headers={"Cache-Control": "no-cache", "ETag": etag})
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
        if op == "restore" and ids:
            return web.json_response({"ok": bool(db.restore_item(ids[0]))})
        if op == "aistats" and edit.get("aistats"):
            return web.json_response(edit["aistats"]())
        if op == "add" and edit.get("add"):
            return web.json_response(await edit["add"](uid, str(body.get("text") or "")))
        if op == "similar" and ids and edit.get("similar"):
            return web.json_response({"items": [{"id": i, "why": w} for i, w in edit["similar"](ids[0])]})
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

    async def bot_api(request):
        """Экраны и кнопки бота в приложении (только владелец; гостю по ссылке — нет)."""
        uid = _uid(request)
        if not bridge:
            raise web.HTTPNotFound()
        try:
            body = await request.json()
        except Exception:
            body = {}
        return web.json_response(await bridge(uid, body if isinstance(body, dict) else {}))

    async def voice_api(request):
        """🎙 Голос помощнику: запись из приложения (до 2 МБ ≈ минута речи)."""
        uid = _uid(request)
        if not voice:
            raise web.HTTPNotFound()
        data = await request.content.read(2_000_001)
        if not data or len(data) > 2_000_000:
            return web.json_response({"out": [], "toasts": [{"t": "Запись слишком длинная — до минуты", "alert": True}]})
        return web.json_response(await voice(uid, data, request.headers.get("Content-Type", "")))

    app.router.add_post("/a/{token}/voice", voice_api)
    app.router.add_post("/a/{token}/bot", bot_api)
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
.dlg .dead{background:#5a2a2a;color:#ffd7d7;border-radius:12px;padding:10px 12px;margin:0 0 10px;font-size:15px}
.th .deadb{position:absolute;left:8px;bottom:8px;font-size:14px;background:rgba(90,42,42,.9);color:#fff;border-radius:8px;padding:2px 7px}
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
.toast .undo{margin-left:10px;padding:7px 12px;font-size:15px;background:rgba(255,255,255,.22);color:inherit;flex:none}
.toast:has(.undo){display:flex;align-items:center;white-space:nowrap;max-width:calc(100% - 24px)}
.toast{position:fixed;left:50%;transform:translateX(-50%);top:calc(var(--toph,100px) + 12px);z-index:30;background:var(--accent);
  color:var(--accent-ink);padding:10px 16px;border-radius:12px;font-size:15px;box-shadow:0 6px 20px rgba(0,0,0,.3)}
@media (max-width: 480px){ .editrow{grid-template-columns:repeat(2,1fr)} }
.editrow .wide{grid-column:1/-1}
.editrow .half{grid-column:span 2}
@media (max-width: 480px){ .editrow .half{grid-column:span 1} }
.note .btn{padding:6px 12px;margin-left:8px;font-size:14px}
.crumbrow{flex-wrap:wrap}
.artbtns{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
.dlg .art #dbrief{margin-top:0}
.reader{position:fixed;inset:0;z-index:30;display:flex;flex-direction:column;--rb:var(--bg);--ri:var(--ink);--rm:var(--muted);
  background:var(--rb);color:var(--ri)}
.reader[data-theme=light]{--rb:#fbfaf7;--ri:#1d1d1f;--rm:#6b6b70}
.reader[data-theme=sepia]{--rb:#f4ecd8;--ri:#3b2f20;--rm:#7a6650}
.reader[data-theme=dark]{--rb:#121417;--ri:#e6e6e6;--rm:#9aa0a6}
html:has(#reader:not([hidden])){overflow:hidden}
.rbar{display:flex;gap:8px;align-items:center;padding:calc(8px + var(--tg-safe-area-inset-top,0px) + var(--tg-content-safe-area-inset-top,0px)) 12px 8px;
  border-bottom:1px solid rgba(127,127,127,.25)}
.rbar b{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:16px}
.rbar .btn{padding:8px 12px;background:rgba(127,127,127,.16);color:var(--ri)}
.rprog{height:3px;background:rgba(127,127,127,.18)}.rprog i{display:block;height:100%;width:0;background:var(--accent)}
.rscroll{flex:1;overflow:auto;-webkit-overflow-scrolling:touch}
.rtext{max-width:68ch;margin:0 auto;padding:22px 20px calc(60px + var(--tg-safe-area-inset-bottom,0px));font-size:var(--rfs,19px);
  line-height:1.7;font-family:Georgia,"Iowan Old Style","PT Serif",serif;overflow-wrap:anywhere}
.rtext h1{font:700 1.45em/1.25 system-ui,-apple-system,"Segoe UI",sans-serif;margin:0 0 .3em;white-space:normal}
.rtext h2{font:700 1.12em/1.35 system-ui,-apple-system,"Segoe UI",sans-serif;margin:1.6em 0 .5em}
.rtext p{margin:0 0 1em}.rtext .src{color:var(--rm);font:14px system-ui,sans-serif;margin-bottom:1.6em}
.rtext .src a{color:var(--accent)}.rtext .brief{background:rgba(127,127,127,.12);border-radius:12px;padding:12px 14px;font-size:.9em;margin-bottom:1.4em}
.rtocbox{max-height:45vh;overflow:auto;padding:8px 12px;border-bottom:1px solid rgba(127,127,127,.25)}
.rtocbox button{display:block;width:100%;text-align:left;border:0;background:none;color:var(--ri);padding:8px 6px;font-size:16px;cursor:pointer;border-radius:8px}
.rtocbox button:hover{background:rgba(127,127,127,.15)}
@media (max-width: 480px){ .rtext{padding:16px 16px 60px} .rbar .btn{padding:8px 10px} }
.deck,.statp{position:fixed;inset:0;z-index:26;background:var(--bg);display:flex;justify-content:center}
html:has(#deck:not([hidden])),html:has(#statp:not([hidden])){overflow:hidden}
.deckin{width:min(640px,100%);height:100%;display:flex;flex-direction:column}
.deckn{color:var(--muted);font-size:15px;margin-right:auto;margin-left:10px}
.deckstage{flex:1;min-height:0;position:relative;padding:14px;display:flex;align-items:stretch;justify-content:center;overflow:hidden}
.dcard{position:relative;width:100%;max-width:560px;background:var(--panel);border:1px solid var(--line);border-radius:20px;
  box-shadow:0 10px 30px rgba(0,0,0,.25);display:flex;flex-direction:column;overflow:hidden;touch-action:none;user-select:none;
  cursor:grab;transition:transform .25s ease,opacity .25s ease}
.dcard.drag{transition:none;cursor:grabbing}
.dcard .dimg{flex:1;min-height:120px;background:var(--thumb);display:flex;align-items:center;justify-content:center;font-size:72px;overflow:hidden}
.dcard .dimg img{width:100%;height:100%;object-fit:contain;pointer-events:none}
.dcard .dbody{padding:14px 16px 16px}
.dcard .dsum{font-size:18px;font-weight:600;margin:0 0 8px;display:-webkit-box;-webkit-line-clamp:4;-webkit-box-orient:vertical;overflow:hidden}
.dcard .dfold{font-size:16px;background:var(--chip);border-radius:10px;padding:8px 10px;display:inline-block}
.dcard .dmeta2{color:var(--muted);font-size:14px;margin-top:8px}
.dcard .stamp{position:absolute;top:18px;padding:8px 14px;border-radius:12px;font-size:22px;font-weight:700;opacity:0;
  border:3px solid currentColor;background:rgba(0,0,0,.35);pointer-events:none}
.stamp.ok{right:18px;color:#3ddc84}.stamp.mv{left:18px;color:#4f9ef0}
.stamp.fv{left:50%;transform:translateX(-50%);color:#ffd54a}.stamp.dl{left:50%;top:auto;bottom:18px;transform:translateX(-50%);color:#ff6b6b}
.deckhint{text-align:center;color:var(--muted);font-size:14px;padding:0 12px 6px}
.deckbtns{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:8px;padding:8px 12px calc(12px + var(--tg-safe-area-inset-bottom,0px))}
.deckbtns .btn{display:flex;flex-direction:column;align-items:center;gap:2px;font-size:22px;padding:10px 2px;min-width:0}
.deckbtns small{font-size:13px;max-width:100%;overflow:hidden;text-overflow:ellipsis}
@media (max-width: 420px){ .deckbtns{gap:6px;padding-left:8px;padding-right:8px} .deckbtns small{font-size:11.5px}
  .deckstage{padding:10px} .deckhint{font-size:13px} }
.deckdone{align-self:center;text-align:center;font-size:19px;color:var(--muted);padding:30px}
.statin{width:min(980px,100%);height:100%;display:flex;flex-direction:column}
.statbody{flex:1;overflow:auto;padding:14px 14px calc(24px + var(--tg-safe-area-inset-bottom,0px))}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin-bottom:14px}
.stile{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:12px 14px}
.stile b{display:block;font-size:30px;line-height:1.1}
.stile span{color:var(--muted);font-size:14px}
.charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr));gap:14px}
.chart{background:var(--panel);border:1px solid var(--line);border-radius:16px;padding:14px 16px}
.chart h3{margin:0 0 2px;font-size:17px}
.chart .sub{color:var(--muted);font-size:13.5px;margin:0 0 12px}
.cols{display:flex;align-items:flex-end;gap:2px;height:150px;border-bottom:1px solid var(--line)}
.cols .c{flex:1;display:flex;flex-direction:column;justify-content:flex-end;height:100%;cursor:default}
.cols .c i{display:block;background:var(--accent);border-radius:4px 4px 0 0;min-height:2px}
.cols .c:hover i{filter:brightness(1.2)}
.xl{display:flex;gap:2px;margin-top:6px}
.xl span{flex:1;text-align:center;color:var(--muted);font-size:12px;overflow:hidden;white-space:nowrap}
.hb{display:grid;grid-template-columns:minmax(90px,38%) 1fr auto;align-items:center;gap:8px;margin:6px 0;font-size:14.5px}
.hb .l{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.hb .t{height:12px;background:var(--chip);border-radius:6px;overflow:hidden}
.hb .t i{display:block;height:100%;background:var(--accent);border-radius:0 4px 4px 0}
.hb .v{color:var(--muted);font-variant-numeric:tabular-nums;min-width:2.5em;text-align:right}
.ctip{position:fixed;z-index:40;background:var(--ink);color:var(--bg);padding:6px 10px;border-radius:8px;font-size:14px;
  pointer-events:none;white-space:nowrap}
.fab{position:fixed;right:calc(18px + var(--tg-safe-area-inset-right,0px));bottom:calc(20px + var(--tg-safe-area-inset-bottom,0px));
  z-index:14;width:62px;height:62px;border-radius:50%;border:0;background:var(--accent);color:var(--accent-ink);font-size:28px;
  box-shadow:0 6px 20px rgba(0,0,0,.35);cursor:pointer}
#addd textarea{width:100%;padding:12px 14px;border:1px solid var(--line);border-radius:12px;background:var(--bg);font:inherit;
  font-size:17px;resize:vertical;min-height:140px}
#addd .body{flex:1;overflow:auto}
.addhint{color:var(--muted);font-size:15px;margin:0 0 10px}
.addres{margin-top:10px;background:var(--chip);border-radius:12px;padding:10px 12px;font-size:16px}
.crumbacts{display:flex;gap:8px;flex-wrap:wrap;justify-content:flex-end;max-width:100%}
@media (max-width: 480px){ .crumbacts .btn{padding:9px 10px;font-size:15px} .crumbacts{gap:6px} }
.menubtn{order:5}
.botp{position:fixed;inset:0;z-index:25;background:rgba(0,0,0,.5);display:flex;justify-content:flex-end}
html:has(#botp:not([hidden])){overflow:hidden}   /* под меню архив не листается и нет второй полосы прокрутки */
.botin{width:min(560px,100%);height:100%;background:var(--bg);display:flex;flex-direction:column;box-shadow:-8px 0 30px rgba(0,0,0,.35)}
.bothead{display:flex;gap:8px;align-items:center;padding:calc(10px + var(--tg-safe-area-inset-top,0px) + var(--tg-content-safe-area-inset-top,0px)) 12px 10px;
  background:var(--panel);border-bottom:1px solid var(--line)}
.bothead b{flex:1;font-size:18px}
.botscroll{flex:1;min-height:0;overflow:auto;padding:12px;display:flex;flex-direction:column}
/* разделы — крупные плитки на всю высоту: значок сверху, подпись снизу */
.secs{flex:1;display:grid;grid-template-columns:repeat(3,1fr);grid-auto-rows:minmax(84px,1fr);gap:10px}
.secs .tile{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:8px;border:1px solid var(--line);
  border-radius:16px;background:var(--panel);cursor:pointer;padding:10px 6px;text-align:center;line-height:1.2;font-size:15.5px}
.secs .tile:hover{border-color:var(--accent)}
.secs .tile .ic{font-size:32px;line-height:1}
#botmsgs{flex:0 0 auto}
.bmsg{background:var(--panel);border-radius:14px;padding:12px 14px;margin:0 0 12px;box-shadow:var(--shadow);border:1px solid var(--line)}
.bmsg.busy{opacity:.55;pointer-events:none}
.bmsg .tx{white-space:pre-wrap;font-size:16px;overflow-wrap:anywhere}
.bmsg .tx a{color:var(--accent)}
.brows{display:grid;gap:6px;margin-top:10px}
.brow{display:grid;grid-auto-flow:column;grid-auto-columns:1fr;gap:6px}
.brow .btn,.brow a.btn{padding:10px 8px;font-size:15px;white-space:normal;line-height:1.25;text-align:center;text-decoration:none;min-width:0;overflow-wrap:anywhere}
.bsys{color:var(--muted);font-size:14px;text-align:center;margin:0 0 12px}
.botinput{display:flex;gap:8px;padding:10px 12px calc(10px + var(--tg-safe-area-inset-bottom,0px));background:var(--panel);border-top:1px solid var(--line)}
.botinput input{flex:1;min-width:0;padding:12px 14px;border:1px solid var(--line);border-radius:12px;background:var(--bg);font-size:16px}
.botinput.ask input{border-color:var(--accent);box-shadow:0 0 0 2px var(--accent)}
.botinput .mic{font-size:20px;padding:10px 14px}
.botinput .mic.rec{background:#c62828;color:#fff;animation:pulse 1.2s infinite}
@keyframes pulse{50%{box-shadow:0 0 0 6px rgba(198,40,40,.35)}}
@media (max-width: 900px){ .botin{width:100%;box-shadow:none} }
@media (min-width: 901px){ .secs{grid-template-columns:repeat(4,1fr)} }   /* боковая панель: 24 плитки = 6 рядов по 4 */   /* узкое окно — меню целиком, без полоски архива слева */
@media (max-width: 420px){ .secs{gap:8px;grid-auto-rows:minmax(70px,1fr)} .secs .tile{font-size:14px;padding:6px 4px;gap:6px}
  .secs .tile .ic{font-size:27px} }
@media (max-width: 480px){ .brow{grid-auto-flow:row;grid-auto-columns:auto}
  .brow:has(> :nth-child(2):last-child){grid-auto-flow:column;grid-auto-columns:1fr} }
@media (max-width: 900px){
  .layout{grid-template-columns:1fr;padding:12px}
  .side{display:none}
  .navbtn{display:inline-block}
  h1{font-size:19px}.count{display:none}
  .bar{flex-wrap:wrap}.search{flex-basis:100%;order:3}
  .fsbtn{order:2;margin-left:auto}  /* узкий — в первой строке, справа */
  .menubtn{order:0}                  /* «☰ Меню» — рядом с «📂 Папки», а не отдельной строкой */
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
    <button class="btn menubtn" id="menubtn" hidden>☰ Меню</button>
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
      <span class="crumbacts"><button class="btn" id="deckbtn" hidden>🃏 По одному</button>
      <button class="btn" id="foldbtn" hidden>⋯ Папка</button>
      <button class="btn" id="selbtn" hidden>☑️ Выбрать</button></span></div>
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
    <div class="dead" id="ddead" hidden></div>
    <div class="ai" id="dai" hidden></div>
    <details class="art" id="dart" hidden><summary id="dartt">📄 Текст статьи</summary>
      <span class="artbtns"><button class="btn primary" id="dread">📖 Читать</button>
      <button class="btn" id="dbrief">🧠 Кратко по-русски</button></span><div class="full" id="dbriefx" hidden></div>
      <div class="full" id="dartx"></div></details></div></div>
  <div class="acts"><a class="btn primary" id="dopen" target="_blank" rel="noopener">Открыть в Telegram</a>
    <button class="btn primary" id="dsend" hidden>📤 Прислать в чат</button>
    <button class="btn" id="dclose">Закрыть</button></div>
  <div class="editrow" id="editrow" hidden>
    <button class="btn" id="emove">📂 Перенести</button><button class="btn" id="eren">✏️ Название</button>
    <button class="btn" id="erem">⏰ Напомнить</button><button class="btn" id="etrash">🗑 В корзину</button>
    <button class="btn" id="eunlock" hidden>🔓 Снять замок</button>
    <button class="btn half" id="esimi">🖼 Похожие</button>
    <button class="btn half" id="eall">⋯ Все действия, как в боте</button>
  </div>
</div></dialog>
<dialog id="pick" class="picker"><div class="dlg"><div class="body">
  <h2 id="ptitle">📂 В какую папку?</h2>
  <input id="pq" type="search" placeholder="Найти папку" autocomplete="off" style="width:100%;padding:12px;border-radius:12px;border:1px solid var(--line);background:var(--bg);font-size:16px">
  <div class="list" id="plist"></div></div>
  <div class="acts"><button class="btn" id="pclose">Отмена</button></div></div></dialog>
<div class="deck" id="deck" hidden><div class="deckin">
  <div class="bothead"><b id="decktitle">🃏 По одному</b><span class="deckn" id="deckn"></span>
    <button class="btn" id="deckx" title="Закрыть">✕</button></div>
  <div class="deckstage" id="deckstage"></div>
  <div class="deckhint">👉 верно · 👈 другая папка<br>👆 в избранное · 👇 в корзину</div>
  <div class="deckbtns"><button class="btn" id="dkdel">🗑<small>Корзина</small></button>
    <button class="btn" id="dkmove">📂<small>Другая</small></button><button class="btn" id="dkskip">⏭<small>Позже</small></button>
    <button class="btn" id="dkfav">⭐<small>Избранное</small></button><button class="btn primary" id="dkok">✅<small>Верно</small></button></div>
</div></div>
<div class="reader" id="reader" hidden data-theme="auto">
  <div class="rbar"><button class="btn" id="rx" title="Закрыть">✕</button><b id="rtitle"></b>
    <button class="btn" id="rtoc" title="Оглавление">📑</button><button class="btn" id="rminus" title="Мельче">A−</button>
    <button class="btn" id="rplus" title="Крупнее">A+</button><button class="btn" id="rtheme" title="Тема">🌓</button></div>
  <div class="rprog"><i id="rprog"></i></div>
  <div class="rtocbox" id="rtocbox" hidden></div>
  <div class="rscroll" id="rscroll"><article class="rtext" id="rtext"></article></div>
</div>
<div class="statp" id="statp" hidden><div class="statin">
  <div class="bothead"><b>📊 Статистика</b><button class="btn" id="statx" title="Закрыть">✕</button></div>
  <div class="statbody" id="statbody"></div>
</div></div>
<div class="ctip" id="ctip" hidden></div>
<button class="fab" id="fab" hidden title="Добавить в архив ссылку или заметку">➕</button>
<dialog id="addd"><div class="dlg"><div class="body">
  <h2>➕ Добавить в архив</h2>
  <p class="addhint">Вставь ссылку — бот сохранит страницу с названием и описанием, а позже и текст статьи.
    Или напиши заметку — бот сам разложит её по папкам.</p>
  <textarea id="addt" rows="5" placeholder="https://… или текст заметки"></textarea>
  <div class="addres" id="addres" hidden></div></div>
  <div class="acts"><button class="btn primary" id="addgo">💾 Сохранить</button><button class="btn" id="addx">Закрыть</button></div>
</div></dialog>
<div class="botp" id="botp" hidden><div class="botin">
  <div class="bothead"><b id="bottitle">☰ Меню</b>
    <button class="btn" id="botsec" hidden>▦ Разделы</button><button class="btn" id="botclose" title="Закрыть">✕</button></div>
  <div class="botscroll" id="botscroll"><div class="secs" id="secs"></div><div id="botmsgs"></div></div>
  <form class="botinput" id="botform"><button class="btn mic" type="button" id="mic" title="Сказать голосом">🎙️</button>
    <input id="botq" autocomplete="off" placeholder="Напиши помощнику: «найди…», «перенеси…»">
    <button class="btn primary" type="submit">➤</button></form>
</div></div>
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
const EMO_RE=/([\u{1F3F7}\u{1F5D1}\u{1F6E0}\u{1F5C2}\u{1F5C3}\u{1F5DE}\u{1F441}\u{1F39E}\u{1F399}\u{1F4FD}\u{1F58C}\u{1F58D}\u{1F5BC}\u{1F5D2}\u{1F5D3}\u{1F570}\u{1F579}\u{1F5A5}\u{1F6CB}\u{1F3F3}\u{1F321}⚙✏☑⬆⬇⬅➡↩↪✍✂✉⏱⏲⏭⏮⏯⏸⏹⏺☎☺♻⚒⚔⚖⚛⚠✔✖✳✴❄❇❤☀☁☂☘☠☹♀♂♠♣♥♦♨ℹ↔↕▶◀™©®])(?!️)/gu;
const fx=s=>String(s).replace(EMO_RE,"$1️");
function el(tag,cls,text){const e=document.createElement(tag);if(cls)e.className=cls;if(text!=null)e.textContent=fx(text);return e}
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
  const nd=D.items.filter(i=>i.dd).length;
  const nf=D.items.filter(i=>i.fv).length;
  if(nf){const fb=el("button","node"+(sel.kind==="fav"?" on":""));fb.append("⭐ Избранное",el("span","n",nf));
    fb.onclick=()=>pick("fav","");box.append(fb)}
  if(nd){const db_=el("button","node"+(sel.kind==="dead"?" on":""));db_.append(fx("🪦 Умершие ссылки"),el("span","n",nd));
    db_.onclick=()=>pick("dead","");box.append(db_)}
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
  if(sel.kind==="dead")return !!i.dd;
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
  $("crumb").textContent=fx(sel.kind==="topic"?"🏷 "+sel.val:sel.kind==="new"?"🆕 Ещё не смотрел":sel.kind==="fav"?"⭐ Избранное":sel.kind==="dead"?"🪦 Умершие ссылки":sel.kind==="all"?"Все посты":(sel.kind==="folder"?"📁 "+sel.val:icon(sel.val,"📦")+" "+sel.val))+` · ${arr.length}`;
  $("empty").hidden=arr.length>0;
  $("foldbtn").hidden=!CAN_EDIT||!(sel.kind==="folder"||sel.kind==="cat");
  $("deckbtn").hidden=!CAN_EDIT||!arr.length;
  renderTree($("side"));renderTree($("sheettree"));more();
}
function card(i){
  const c=el("div","card");c.tabIndex=0;c.setAttribute("role","button");
  const th=el("div","th");
  const media=["photo","video","animation","video_note","document","audio"].includes(i.t);
  const lm=(i.x||"").match(/https?:\/\/(?:www\.)?([^\/\s)]+)/);
  const plain=()=>{th.classList.add("tx");th.append(el("div","big",icon(i.c,KIND[i.t]||"📝")));if(lm)th.append(el("div","dom","🔗 "+lm[1]))};
  if(media){const img=new Image();img.loading="lazy";img.alt="";img.src=`${BASE}thumb/${i.id}.jpg`;
    img.onerror=()=>{img.remove();th.prepend(fx(KIND[i.t]||"📦"))};th.append(img)}
  else if(lm&&!SHARE_MODE){const img=new Image(),dom=el("span","kind","🔗 "+lm[1]);img.loading="lazy";img.alt="";
    img.src=`${BASE}thumb/${i.id}.jpg`;img.onerror=()=>{img.remove();dom.remove();plain()};th.append(img,dom)}
  else plain();
  if(i.t!=="text"&&i.t!=="photo")th.append(el("span","kind",KIND[i.t]||""));
  if(i.nw)th.append(el("span","newb","🆕"));
  if(i.fv)th.append(el("span","favb","⭐"));
  if(i.dd)th.append(el("span","deadb","🪦 ссылка умерла"));
  if(selMode){th.append(el("span","tick",picked.has(i.id)?"✓":""));if(picked.has(i.id))c.classList.add("picked")}
  const cb=el("div","cb");cb.append(el("div","sum",i.s||"—"));
  cb.append(el("div","meta",`${icon(i.c,"📦")} ${i.c} · ${dateRu(i.d)}`));
  c.append(th,cb);c.onclick=()=>selMode?togglePick(i,c):open(i);c.onkeydown=e=>{if(e.key==="Enter")open(i)};
  return c;
}
function more(){const g=$("grid");list.slice(shown,shown+PAGE).forEach(i=>g.append(card(i)));shown+=PAGE;$("more").hidden=shown>=list.length}
function open(i){
  const img=$("dimg");img.textContent="";
  if(["photo","video","animation","video_note","document","audio"].includes(i.t)||(/https?:\/\//.test(i.x||"")&&!SHARE_MODE)){
    const im=new Image();im.src=`${BASE}thumb/${i.id}.jpg${i.t==="photo"?"?big=1":""}`;im.alt="";im.onerror=()=>im.remove();img.append(im)}
  $("dsum").textContent=i.s||"—";
  $("dmeta").textContent=fx(`${icon(i.c,"📦")} ${i.c} · ${dateRu(i.d)}${i.ch?" · 📡 "+i.ch:""}${i.tg?" · "+i.tg:""}${(i.tp||[]).length?" · 🏷 "+i.tp.join(", "):""} · #${i.id}`);
  renderText($("dtext"),i.x&&i.x!==i.s?i.x:"");
  $("dai").hidden=!i.v;$("dai").textContent=i.v?"👁 Что увидел ИИ:\n"+i.v:"";
  $("ddead").hidden=!i.dd;$("ddead").textContent=i.dd?fx(`🪦 Ссылка больше не открывается (с ${i.dd})`+(i.ar?" — но текст статьи сохранён, он ниже":"")):"";
  const da=$("dart");da.hidden=!i.ar;da.open=false;da.dataset.id=i.id;da.dataset.ok="";
  $("dartt").textContent="📄 Статья: "+(i.ar||"");$("dartx").textContent="";
  $("dread").hidden=SHARE_MODE;$("dbrief").hidden=!CAN_EDIT;$("dbrief").disabled=false;$("dbriefx").hidden=true;$("dbriefx").textContent="";
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
  const clean=s=>fx(s.replace(/\*\*|__|~~|`/g,""));
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
// сообщение с кнопкой «↩ Отменить» (7 секунд): промахнулся пальцем — вернул как было
function undoToast(t,fn){document.querySelectorAll(".toast").forEach(x=>x.remove());
  const e=el("div","toast",t),u=el("button","btn undo","↩ Отменить");e.append(u);document.body.append(e);
  const tm=setTimeout(()=>e.remove(),7000);
  u.onclick=async()=>{clearTimeout(tm);e.remove();try{await fn();toast("↩ Вернул как было")}catch(x){toast("Не вышло вернуть: "+x.message)}}}
const snap=ids=>ids.map(id=>{const it=D.items.find(x=>x.id===id)||{};return {id,c:it.c,s:it.s,l:!!it.l,fv:!!it.fv}});
async function unlockIf(id){const it=D.items.find(x=>x.id===id);if(it&&it.l){await api("unlock",{id});it.l=false}}
async function undoMove(before){   // вернуть посты в прежние папки (замки — как были)
  for(const b of before){await unlockIf(b.id)}
  const by={};before.forEach(b=>(by[b.c]=by[b.c]||[]).push(b.id));
  for(const [c,ids] of Object.entries(by)){await api("move",{ids,to:c});
    D.items.forEach(i=>{if(ids.includes(i.id))i.c=c})}
  for(const b of before){if(!b.l)await api("unlock",{id:b.id});const it=D.items.find(x=>x.id===b.id);if(it)it.l=b.l}
  if($("dlg").open&&cur&&before.some(b=>b.id===cur.id)){   // открытая карточка — тоже «как было»
    $("dmeta").textContent=fx(`${icon(cur.c,"📦")} ${cur.c} · ${dateRu(cur.d)} · #${cur.id}`);renderEdit()}
  rerender()}
async function undoTrash(ids){for(const id of ids)await api("restore",{ids:[id]});await reloadData()}
function setCount(){$("count").textContent=`${D.items.length} постов`}
async function api(op,body){const r=await fetch(BASE+"edit/"+op,{method:"POST",headers:{"Content-Type":"application/json"},
  body:JSON.stringify(body)});if(!r.ok)throw new Error(r.status);return r.json()}
function catLabel(c){const g=(D.groups[c]||"").split("/").filter(Boolean);
  if(g[g.length-1]===c)g.pop();return [...g,c].join(" / ")}
function renderEdit(){
  const row=$("editrow");row.hidden=!CAN_EDIT;if(!CAN_EDIT||!cur)return;
  $("etrash").textContent="🗑 В корзину";$("etrash").classList.remove("danger");
  // заблокированный пост (замок ставит и автозамок после ручного переноса) — сначала «🔓 Снять замок»
  ["emove","eren","etrash"].forEach(id=>$(id).hidden=!!cur.l);$("eunlock").hidden=!cur.l;$("eall").hidden=false;$("esimi").hidden=false;
}
function rerender(){const y=scrollY;apply();scrollTo({top:y})}
let pickAfter=null;
function openPicker(ids,after){pickAfter=after||null;
  const cats=[...new Set(D.items.map(i=>i.c))].sort((a,b)=>catLabel(a).localeCompare(catLabel(b),"ru"));
  const draw=()=>{const f=norm($("pq").value);$("plist").textContent="";
    cats.filter(c=>norm(catLabel(c)).includes(f)).forEach(c=>{const b=el("button","node",`📁 ${icon(c,"📦")} ${catLabel(c)}`);
      b.onclick=async()=>{try{const before=snap(ids).filter(x=>!x.l&&x.c!==c);const j=await api("move",{ids,to:c});
        D.items.forEach(i=>{if(ids.includes(i.id)&&!i.l)i.c=c;if((j.locked||[]).includes(i.id))i.l=true});$("pick").close();
        undoToast(fx(`📂 Перенесено: ${j.done} → «${c}»`),()=>undoMove(before));renderEdit();
        if($("dlg").open&&cur){$("dmeta").textContent=`${icon(cur.c,"📦")} ${cur.c} · ${dateRu(cur.d)} · #${cur.id}`}
        const cb=pickAfter;pickAfter=null;if(cb){cb();return}
        exitSel(false);rerender()}catch(e){toast("Не вышло: "+e.message)}};$("plist").append(b)})};
  $("ptitle").textContent=ids.length>1?`📂 Перенести ${ids.length} постов в папку`:"📂 В какую папку?";
  $("pq").value="";$("pq").oninput=draw;draw();$("pick").showModal();
}
$("pclose").onclick=()=>$("pick").close();
$("efav").onclick=async()=>{if(!cur)return;const on=!cur.fv;
  try{const me=cur;await api("fav",{id:me.id,on});me.fv=on;const st=$("efav");st.textContent=fx(on?"⭐":"☆");st.classList.toggle("on",on);
    rerender();undoToast(fx(on?"⭐ В избранном":"Убрано из избранного"),async()=>{await api("fav",{id:me.id,on:!on});me.fv=!on;
      if(cur===me){st.textContent=fx(!on?"⭐":"☆");st.classList.toggle("on",!on)}rerender()})}catch(e){toast("Не вышло: "+e.message)}};
$("dbrief").onclick=async()=>{if(!cur)return;const b=$("dbrief"),x=$("dbriefx");b.disabled=true;b.textContent="🧠 Читаю…";
  try{const j=await api("brief",{id:cur.id});x.hidden=false;
    if(j.brief){renderText(x,j.brief);b.hidden=true}else{x.textContent="Бесплатный ИИ сейчас занят — попробуй чуть позже."}}
  catch(e){x.hidden=false;x.textContent="Не вышло: "+e.message}
  finally{b.disabled=false;b.textContent="🧠 Кратко по-русски"}};
// ── ☰ Меню: все разделы и экраны бота ──
const SECTIONS=[["cmd","service","🛠️","Сервис"],["cmd","settings","⚙️","Настройки"],["cmd","assistant","🤖","Помощник"],
  ["app","deck","📥","Новые посты"],["cb","reminders","⏰","Напоминания"],["cmd","trash","🗑️","Корзина"],
  ["cmd","dups","🔁","Дубли"],["cb","shares","🔗","Мои ссылки"],["cmd","fav","⭐","Избранное"],
  ["cb","tags_section","🔖","Теги"],["cmd","topics","🏷️","Темы"],["cb","svc_order","🧹","Порядок"],
  ["cb","chrules","📡","Каналы"],["cb","locked_list","🔒","Заблокированные"],["cmd","pending","📬","Вопросы бота"],
  ["cmd","read","📖","Что почитать"],["cmd","deals","🎮","Скидки"],["cmd","anime","📺","Новые серии"],
  ["cmd","releases","⬆️","Программы"],["cmd","month","🗓️","Итоги месяца"],["app","stats","📊","Статистика"],
  ["cmd","limits","🔋","Лимиты ИИ"],["cmd","export","💾","Копия архива"],["cmd","help","❓","Помощь"]];
let botDirty=false,botBusy=false;
// два экрана: «разделы» (плитки) и «экран бота» (то, что открыл); «▦ Разделы» всегда возвращает к плиткам
function botPage(p){const onSecs=p==="secs";$("secs").hidden=!onSecs;$("botmsgs").hidden=onSecs;
  const b=$("botsec"),has=$("botmsgs").childElementCount>0;
  b.hidden=onSecs&&!has;b.textContent=onSecs?"↩ К экрану":"▦ Разделы";b.dataset.to=onSecs?"msgs":"secs";
  $("bottitle").textContent=onSecs?"☰ Меню":"☰ Меню";$("botscroll").scrollTop=0}
function botSection(body,label){$("botmsgs").textContent="";botPage("msgs");botCall(body,null,label)}
function botOpen(page){$("botp").hidden=false;if(TG)TG.BackButton.show();if(!$("secs").childElementCount){
  SECTIONS.forEach(([k,v,ic,l])=>{const b=el("button","tile");b.append(el("span","ic",ic),el("span","lb",l));
    b.onclick=()=>{if(k==="app"){botClose();
        if(v==="deck")openDeck(D.items.filter(i=>i.nw),"📥 Новые посты",true);else openStats();return}
      botSection(k==="cmd"?{kind:"cmd",name:v}:{kind:"cb",data:v},ic+" "+l)};$("secs").append(b)})}
  botPage(page||"secs")}
function botClose(){$("botp").hidden=true;if(TG&&!$("dlg").open)TG.BackButton.hide();
  if(botDirty){botDirty=false;reloadData()}}
const cacheKey=()=>"arch_v1_"+((BASE.match(/\/a\/(-?\d+)\./)||[])[1]||"web");
function cacheGet(){try{const c=JSON.parse(localStorage.getItem(cacheKey())||"null");return c&&c.etag&&c.body?c:null}catch(e){return null}}
function cachePut(etag,body){try{if(etag)localStorage.setItem(cacheKey(),JSON.stringify({etag,body}))}catch(e){}}
async function fetchData(etag){const r=await fetch(BASE+"data.json",etag?{headers:{"If-None-Match":etag}}:{});
  if(r.status===304)return null;if(!r.ok)throw new Error(r.status===404?"ссылку отозвали":r.status);
  const body=await r.text();cachePut(r.headers.get("ETag"),body);return JSON.parse(body)}
async function reloadData(){try{const d=await fetchData();if(d){D=d;setCount();rerender()}}catch(e){}}
function botMsg(m){const c=el("div","bmsg");c.dataset.id=m.id;botFill(c,m);return c}
function botFill(c,m){
  if(m.text!==null&&m.text!==undefined){let tx=c.querySelector(".tx");if(!tx){tx=el("div","tx");c.prepend(tx)}renderText(tx,m.text||"…")}
  if(m.rows!==null&&m.rows!==undefined){c.querySelector(".brows")?.remove();if(m.rows.length){const g=el("div","brows");
    m.rows.forEach(r=>{const row=el("div","brow");r.forEach(b=>{let x;
      if(b.u){x=el("a","btn",b.t);x.href=b.u;x.target="_blank";x.rel="noopener";
        if(TG&&TG.openLink)x.onclick=e=>{e.preventDefault();/t\.me\//.test(b.u)&&TG.openTelegramLink?TG.openTelegramLink(b.u):TG.openLink(b.u)}}
      else{x=el("button","btn",b.t);x.onclick=()=>botCall({kind:"cb",data:b.d,src:+c.dataset.id},c)}
      row.append(x)});g.append(row)});c.append(g)}}
}
async function botCall(body,card,label,req){
  if(botBusy)return;botBusy=true;botDirty=true;
  const box=$("botmsgs");if(card)card.classList.add("busy");
  let wait=null;if(!card){wait=el("div","bsys","⏳ "+(label||"…"));box.append(wait);$("botscroll").scrollTop=1e9}
  try{const r=await(req?req():fetch(BASE+"bot",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)}));
    if(!r.ok)throw new Error(r.status);const j=await r.json();wait?.remove();let last=null;
    if(j.heard)box.append(el("div","bsys","🎙 «"+j.heard+"»"));
    (j.out||[]).forEach(m=>{const ex=box.querySelector(`.bmsg[data-id="${m.id}"]`);
      if(m.op==="delete"){ex?.remove();return}
      if(m.op==="edit"&&ex){botFill(ex,m);last=ex;return}
      const c=botMsg(m);box.append(c);last=c});
    (j.toasts||[]).forEach(t=>{toast(t.t);if(t.alert)box.append(el("div","bsys","ℹ️ "+t.t))});
    while(box.children.length>40)box.firstChild.remove();
    const f=$("botform");f.classList.toggle("ask",!!j.ask);
    $("botq").placeholder=j.ask?"✍️ Ответ: "+j.ask:(j.assistant?"Напиши помощнику…":"Напиши помощнику: «найди…», «перенеси…»");
    if(j.ask)$("botq").focus();
    if(!$("botmsgs").hidden){$("botsec").hidden=false}
    if(last&&(!card||last!==card))last.scrollIntoView({block:"start",behavior:"smooth"});
    else if(!last&&!(j.toasts||[]).length&&wait===null){}
  }catch(e){wait?.remove();toast("Не вышло: "+e.message)}
  finally{card?.classList.remove("busy");botBusy=false}
}
// 🎙 голосом: нажал — говоришь, нажал ещё раз — отправил (до минуты)
let rec=null,recT=null;
$("mic").onclick=async()=>{
  if(rec){rec.stop();return}
  if(!navigator.mediaDevices||!window.MediaRecorder){toast("Микрофон здесь недоступен — отправь голосовое в чат с ботом, помощник поймёт");return}
  let stream;try{stream=await navigator.mediaDevices.getUserMedia({audio:true})}
  catch(e){toast("Нет доступа к микрофону — разреши его Telegram в настройках телефона");return}
  const type=["audio/webm;codecs=opus","audio/ogg;codecs=opus","audio/mp4"].find(t=>MediaRecorder.isTypeSupported&&MediaRecorder.isTypeSupported(t))||"";
  const chunks=[];rec=new MediaRecorder(stream,type?{mimeType:type}:{});const m=$("mic"),t0=Date.now();
  rec.ondataavailable=e=>{if(e.data.size)chunks.push(e.data)};
  rec.onstop=()=>{clearInterval(recT);stream.getTracks().forEach(x=>x.stop());m.classList.remove("rec");m.textContent=fx("🎙");rec=null;
    const blob=new Blob(chunks,{type:(chunks[0]&&chunks[0].type)||type||"audio/webm"});
    if(Date.now()-t0<700||blob.size<1500){toast("Слишком коротко — скажи фразу целиком");return}
    botPage("msgs");botCall(null,null,"Распознаю речь…",()=>fetch(BASE+"voice",{method:"POST",headers:{"Content-Type":blob.type},body:blob}))};
  rec.start();m.classList.add("rec");
  recT=setInterval(()=>{const s=Math.round((Date.now()-t0)/1000);m.textContent="⏹ "+s;if(s>=60)rec&&rec.stop()},250);
  if(TG&&TG.HapticFeedback)TG.HapticFeedback.impactOccurred("light")};
$("menubtn").onclick=botOpen;$("botclose").onclick=botClose;
$("botp").addEventListener("click",e=>{if(e.target===$("botp"))botClose()});
$("botsec").onclick=()=>botPage($("botsec").dataset.to||"secs");
$("botform").onsubmit=e=>{e.preventDefault();const v=$("botq").value.trim();if(!v)return;$("botq").value="";
  botPage("msgs");const me=el("div","bsys","🗨 "+v);$("botmsgs").append(me);botCall({kind:"text",text:v},null,"Думаю…")};
$("esimi").onclick=async()=>{if(!cur)return;const me=cur,b=$("esimi");b.disabled=true;b.textContent="🖼 Ищу…";
  try{const j=await api("similar",{id:me.id});closeDlg();
    smartIds=j.items.map(x=>x.id);sel={kind:"all",val:""};q="";$("q").value="";apply();window.scrollTo({top:0});
    const n=$("note");n.hidden=false;n.textContent=fx(j.items.length
      ?`🖼 Похожие на «${(me.s||"").slice(0,60)}» — ${j.items.length}. 🖼 похожая картинка · 🏷 те же темы · 🔤 общие слова`
      :`🖼 Похожего на «${(me.s||"").slice(0,60)}» не нашлось.`);
    const x=el("button","btn","✖ Показать все");x.onclick=()=>{smartIds=null;n.hidden=true;apply()};n.append(x)}
  catch(e){toast("Не вышло: "+e.message)}finally{b.disabled=false;b.textContent=fx("🖼 Похожие")}};
// 🃏 Разбор по одному: свайпы (телефон) и кнопки/стрелки (компьютер)
let deckQ=[],deckI=0,deckN=0,deckNew=false;
function openDeck(items,title,onlyNew){deckQ=items.slice();deckI=0;deckN=deckQ.length;deckNew=!!onlyNew;
  $("decktitle").textContent=fx(title||"🃏 По одному");$("deck").hidden=false;if(TG)TG.BackButton.show();deckShow()}
function closeDeck(){$("deck").hidden=true;if(TG&&!$("dlg").open&&$("botp").hidden)TG.BackButton.hide();rerender()}
function deckCur(){return deckQ[deckI]}
function deckShow(){
  const st=$("deckstage");st.textContent="";const i=deckCur();
  $("deckn").textContent=deckN?`${Math.min(deckI+1,deckN)} из ${deckN}`:"";
  document.querySelectorAll(".deckbtns .btn").forEach(b=>b.disabled=!i);
  if(!i){st.append(el("div","deckdone",deckN?"🎉 Всё разобрано!":(deckNew?"🎉 Новых постов нет — всё уже просмотрено":"Здесь пусто")));return}
  const c=el("div","dcard"),im=el("div","dimg");
  if(["photo","video","animation","video_note","document","audio"].includes(i.t)||/https?:\/\//.test(i.x||"")){const p=new Image();
    p.src=`${BASE}thumb/${i.id}.jpg${i.t==="photo"?"?big=1":""}`;p.alt="";p.onerror=()=>{p.remove();im.textContent=fx(KIND[i.t]||"📦")};im.append(p)}
  else im.textContent=fx(icon(i.c,KIND[i.t]||"📝"));
  const b=el("div","dbody");b.append(el("div","dsum",i.s||"—"),el("div","dfold",`${icon(i.c,"📦")} Папка: ${catLabel(i.c)}`),
    el("div","dmeta2",`${dateRu(i.d)}${i.ch?" · 📡 "+i.ch:""}${i.l?" · 🔒 заблокирован":""}`));
  c.append(im,b,el("div","stamp ok","✅ Верно"),el("div","stamp mv","📂 Другая"),el("div","stamp fv","⭐ В избранное"),el("div","stamp dl","🗑 В корзину"));
  st.append(c);deckDrag(c)}
function deckDrag(c){let x0=0,y0=0,dx=0,dy=0,on=false;
  const stamps={ok:c.querySelector(".ok"),mv:c.querySelector(".mv"),fv:c.querySelector(".fv"),dl:c.querySelector(".dl")};
  const dir=()=>Math.abs(dx)>Math.abs(dy)?(dx>0?"ok":"mv"):(dy<0?"fv":"dl");
  c.onpointerdown=e=>{on=true;x0=e.clientX;y0=e.clientY;dx=dy=0;c.classList.add("drag");c.setPointerCapture(e.pointerId)};
  c.onpointermove=e=>{if(!on)return;dx=e.clientX-x0;dy=e.clientY-y0;
    c.style.transform=`translate(${dx}px,${dy}px) rotate(${dx/18}deg)`;
    const d=dir(),k=Math.min(1,Math.max(Math.abs(dx),Math.abs(dy))/110);Object.entries(stamps).forEach(([n,s])=>s.style.opacity=n===d?k:0)};
  const up=()=>{if(!on)return;on=false;c.classList.remove("drag");
    if(Math.max(Math.abs(dx),Math.abs(dy))>110)deckAct(dir(),c);
    else{c.style.transform="";Object.values(stamps).forEach(s=>s.style.opacity=0)}};
  c.onpointerup=up;c.onpointercancel=up}
function deckFly(c,d){if(!c)return;const m={ok:"translate(140%,0) rotate(18deg)",mv:"translate(-140%,0) rotate(-18deg)",
  fv:"translate(0,-140%)",dl:"translate(0,140%)",sk:"scale(.85)"};c.style.transform=m[d]||m.sk;c.style.opacity=0}
async function deckAct(d,card){const i=deckCur();if(!i)return;const c=card||document.querySelector(".dcard");
  const next=()=>{deckI++;setTimeout(deckShow,card?160:220)};
  const seen=()=>{if(i.nw){i.nw=false;fetch(BASE+"seen/"+i.id,{method:"POST"}).catch(()=>{})}};
  try{
    if(d==="ok"){deckFly(c,"ok");seen();next()}
    else if(d==="sk"){deckFly(c,"sk");next()}
    else if(d==="fv"){if(!i.fv){await api("fav",{id:i.id,on:true});i.fv=true}deckFly(c,"fv");seen();toast("⭐ В избранном");next()}
    else if(d==="mv"){if(i.l){toast("🔒 Пост заблокирован");if(c)c.style.transform="";return}
      if(c){c.style.transform="";c.querySelectorAll(".stamp").forEach(s=>s.style.opacity=0)}
      openPicker([i.id],()=>{seen();deckFly(document.querySelector(".dcard"),"mv");next()})}
    else if(d==="dl"){if(i.l){toast("🔒 Пост заблокирован");if(c)c.style.transform="";return}
      const j=await api("trash",{ids:[i.id]});if(!j.done){toast("🔒 Не удалилось");if(c)c.style.transform="";return}
      D.items=D.items.filter(x=>x.id!==i.id);setCount();deckFly(c,"dl");next();
      const t=el("div","toast","🗑 В корзине ");const u=el("button","btn","↩ Вернуть");u.style.marginLeft="8px";
      u.onclick=async()=>{t.remove();try{await api("restore",{ids:[i.id]});toast("↩ Вернул");reloadData()}catch(e){toast("Не вышло")}};
      t.append(u);document.querySelectorAll(".toast").forEach(x=>x.remove());document.body.append(t);setTimeout(()=>t.remove(),5000)}
  }catch(e){toast("Не вышло: "+e.message);if(c)c.style.transform=""}}
$("deckx").onclick=closeDeck;
$("dkok").onclick=()=>deckAct("ok");$("dkmove").onclick=()=>deckAct("mv");$("dkfav").onclick=()=>deckAct("fv");
$("dkdel").onclick=()=>deckAct("dl");$("dkskip").onclick=()=>deckAct("sk");
addEventListener("keydown",e=>{if($("deck").hidden||$("pick").open)return;
  const k={ArrowRight:"ok",ArrowLeft:"mv",ArrowUp:"fv",ArrowDown:"dl"," ":"sk"}[e.key];if(k){e.preventDefault();deckAct(k)}
  else if(e.key==="Escape")closeDeck()});
$("deckbtn").onclick=()=>openDeck(list,"🃏 "+$("crumb").textContent.split(" · ")[0],sel.kind==="new");

// 📊 Статистика: графики по архиву (одна серия — один цвет, подсказка при наведении)
function tipOn(node,text){node.addEventListener("pointerenter",e=>{const t=$("ctip");t.textContent=fx(text);t.hidden=false});
  node.addEventListener("pointermove",e=>{const t=$("ctip");t.style.left=Math.min(e.clientX+12,innerWidth-t.offsetWidth-8)+"px";t.style.top=(e.clientY-36)+"px"});
  node.addEventListener("pointerleave",()=>$("ctip").hidden=true)}
function colChart(title,sub,data,label){const w=el("div","chart");w.append(el("h3",null,title),el("div","sub",sub));
  const mx=Math.max(1,...data.map(x=>x[1])),cols=el("div","cols"),xl=el("div","xl");
  data.forEach(([k,v,full],n)=>{const c=el("div","c"),bar=el("i");bar.style.height=(v/mx*100)+"%";c.append(bar);cols.append(c);
    tipOn(c,`${full||k}: ${v}`);xl.append(el("span",null,label?label(k,n):k))});
  w.append(cols,xl);return w}
function barChart(title,sub,data,unit){const w=el("div","chart");w.append(el("h3",null,title),el("div","sub",sub));
  const mx=Math.max(1,...data.map(x=>x[1]));
  data.forEach(([k,v,txt])=>{const r=el("div","hb"),t=el("div","t"),i=el("i");i.style.width=(v/mx*100)+"%";t.append(i);
    r.append(el("span","l",k),t,el("span","v",txt||String(v)));tipOn(r,`${k}: ${txt||v}${unit||""}`);w.append(r)});
  if(!data.length)w.append(el("div","sub","Пока нет данных"));return w}
function topN(map,n){return Object.entries(map).sort((a,b)=>b[1]-a[1]).slice(0,n)}
async function openStats(){
  const b=$("statbody");b.textContent="";$("statp").hidden=false;if(TG)TG.BackButton.show();
  const it=D.items,msk=d=>{const t=new Date(d.replace(" ","T")+"Z");return isNaN(t)?null:new Date(t.getTime()+3*3600e3)};
  const now=new Date(Date.now()+3*3600e3),ym=t=>`${t.getUTCFullYear()}-${String(t.getUTCMonth()+1).padStart(2,"0")}`;
  const months=[];for(let k=11;k>=0;k--){const t=new Date(Date.UTC(now.getUTCFullYear(),now.getUTCMonth()-k,1));months.push(ym(t))}
  const byM={},byW=[0,0,0,0,0,0,0],byH=Array(24).fill(0),folders={},topics={},chans={},kinds={};
  it.forEach(i=>{const t=msk(i.d||"");if(t){byM[ym(t)]=(byM[ym(t)]||0)+1;byW[(t.getUTCDay()+6)%7]++;byH[t.getUTCHours()]++}
    const f=(D.groups[i.c]||"").split("/")[0]||i.c;folders[f]=(folders[f]||0)+1;
    (i.tp||[]).forEach(x=>topics[x]=(topics[x]||0)+1);if(i.ch)chans[i.ch]=(chans[i.ch]||0)+1;
    const g=Object.entries(GROUP).find(([,v])=>v.includes(i.t));const k=g?g[0]:"text";kinds[k]=(kinds[k]||0)+1});
  const thisM=byM[ym(now)]||0,lastM=byM[months[10]]||0;
  const tiles=el("div","tiles");[[it.length,"постов в архиве"],[thisM,`за этот месяц${lastM?` (прошлый — ${lastM})`:""}`],
    [it.filter(i=>i.fv).length,"⭐ в избранном"],[it.filter(i=>i.ar).length,"📄 статей сохранено"],[Object.values(topics).filter(n=>n>=2).length,"🏷 тем (2+ поста)"]]
    .forEach(([v,l])=>{const s=el("div","stile");s.append(el("b",null,String(v)),el("span",null,l));tiles.append(s)});
  b.append(tiles);
  const MN=["янв","фев","мар","апр","май","июн","июл","авг","сен","окт","ноя","дек"];
  const charts=el("div","charts");b.append(charts);
  charts.append(colChart("Сохранено по месяцам","последние 12 месяцев",months.map(m=>[m,byM[m]||0,`${MN[+m.slice(5)-1]} ${m.slice(0,4)}`]),
    k=>MN[+k.slice(5)-1]));
  const WD=["пн","вт","ср","чт","пт","сб","вс"];
  charts.append(colChart("По дням недели","когда чаще сохраняешь",WD.map((d,n)=>[d,byW[n]])));
  charts.append(colChart("По времени суток","часы по Москве",byH.map((v,h)=>[String(h),v,`${h}:00–${h}:59`]),(k,n)=>n%3===0?k:""));
  const KN={photo:"🖼 Фото",video:"🎬 Видео",text:"📝 Текст и ссылки",audio:"🎵 Аудио",document:"📄 Файлы"};
  charts.append(barChart("Что сохраняешь","по типу",topN(kinds,6).map(([k,v])=>[KN[k]||k,v])));
  charts.append(barChart("Любимые папки","10 самых больших",topN(folders,10).map(([k,v])=>[`${icon(k,"📁")} ${k}`,v])));
  charts.append(barChart("Темы","игры, тайтлы, бренды — по всем папкам",topN(topics,10)));
  charts.append(barChart("Откуда пересылаешь","10 каналов и сайтов",topN(chans,10)));
  const ai=barChart("Бесплатный ИИ сегодня","сколько осталось у каждой модели; запас пополняется в течение суток",[]);
  ai.querySelector(".sub+.sub")?.remove();charts.append(ai);
  try{const j=await api("aistats",{});const NM={"qwen/qwen3.8-27b":"Основная (qwen)","openai/gpt-oss-120b":"Умная (gpt-oss-120b)",
      "openai/gpt-oss-20b":"Служебная (gpt-oss-20b)","whisper-large-v3-turbo":"Речь (whisper)"};
    const rows=Object.entries(j).filter(([m,r])=>r.limit>0&&!/whisper/.test(m)).map(([m,r])=>[NM[m]||m,Math.max(0,r.left),
      `${Math.round(Math.max(0,r.left)/1000)} из ${Math.round(r.limit/1000)} тыс.`]);
    ai.querySelectorAll(".sub").forEach((s,k)=>{if(k)s.remove()});
    const mx=Math.max(...Object.values(j).map(r=>r.limit||0),1);
    rows.forEach(([k,v,txt])=>{const r=el("div","hb"),t=el("div","t"),i=el("i");i.style.width=(v/mx*100)+"%";t.append(i);
      r.append(el("span","l",k),t,el("span","v",txt));tipOn(r,`${k}: осталось ${txt} токенов`);ai.append(r)})}
  catch(e){ai.append(el("div","sub","Не удалось узнать расход ИИ"))}
}
$("statx").onclick=()=>{$("statp").hidden=true;$("ctip").hidden=true;if(TG&&!$("dlg").open&&$("botp").hidden)TG.BackButton.hide()};

// ➕ Добавить: ссылка или заметка
$("fab").onclick=()=>{$("addres").hidden=true;$("addt").value="";$("addd").showModal();$("addt").focus();
  if(TG)TG.BackButton.show()};
$("addx").onclick=()=>$("addd").close();
$("addd").addEventListener("close",()=>{if(TG&&!$("dlg").open&&$("botp").hidden)TG.BackButton.hide()});
$("addgo").onclick=async()=>{const t=$("addt").value.trim();if(!t){$("addt").focus();return}
  const b=$("addgo"),r=$("addres");b.disabled=true;b.textContent="⏳ Сохраняю… (до минуты)";r.hidden=true;
  try{const j=await api("add",{text:t});r.hidden=false;
    if(j.status==="saved"){r.textContent=fx(`✅ Сохранено в «${j.category}» ${j.icon||""}`);$("addt").value="";
      if(TG&&TG.HapticFeedback)TG.HapticFeedback.notificationOccurred("success");reloadData()}
    else if(j.status==="exists"){r.textContent=fx(`ℹ️ Это уже есть в архиве — в «${j.category}» (#${j.id})`)}
    else r.textContent=fx("⚠️ "+(j.error||"Не получилось"))}
  catch(e){r.hidden=false;r.textContent="Не вышло: "+e.message}
  finally{b.disabled=false;b.textContent=fx("💾 Сохранить")}};
$("eall").onclick=()=>{if(!cur)return;const id=cur.id;closeDlg();botOpen("msgs");botSection({kind:"cb",data:"view:"+id},"Пост #"+id)};
$("foldbtn").onclick=()=>{botOpen("msgs");
  botSection({kind:"cb",data:sel.kind==="folder"?"fold|"+sel.val:"cat:"+sel.val},sel.val)};
// 📖 Режим чтения: крупный шрифт, тема, оглавление, продолжение с места
let rd={id:0,timer:null};
const rprefs=()=>{try{return JSON.parse(localStorage.getItem("reader_prefs")||"{}")}catch(e){return {}}};
const rsave=p=>{try{localStorage.setItem("reader_prefs",JSON.stringify(p))}catch(e){}};
function rApply(){const p=rprefs();$("reader").dataset.theme=p.theme||"auto";$("rtext").style.setProperty("--rfs",(p.size||19)+"px")}
async function openReader(i){
  const r=await fetch(BASE+"article/"+i.id);if(!r.ok){toast("Текст статьи не найден");return}
  const a=await r.json();rd.id=i.id;rApply();
  const box=$("rtext");box.textContent="";$("rtitle").textContent=a.title||i.s||"Статья";
  box.append(el("h1",null,a.title||i.s||"Статья"));
  const src=el("div","src");const mins=Math.max(1,Math.round((a.text||"").split(/\s+/).length/180));
  src.append(fx(`📖 ~${mins} мин чтения`));
  if(a.url){src.append(" · ");const l=el("a",null,"оригинал");l.href=a.url;l.target="_blank";l.rel="noopener";src.append(l)}
  if(i.dd)src.append(fx(` · 🪦 страница удалена с ${i.dd} — здесь сохранённая копия`));
  box.append(src);
  const heads=[];
  (a.text||"").split(/\n{2,}/).map(x=>x.trim()).filter(Boolean).forEach((par,n)=>{
    const head=par.length<90&&!/[.!?;:…,»"]$/.test(par)&&!/^[•\-–—]/.test(par)&&n>0;
    const e=el(head?"h2":"p");par.split("\n").forEach((ln,k)=>{if(k)e.append(document.createElement("br"));e.append(ln)});
    if(head){e.id="rh"+heads.length;heads.push([e.id,par])}box.append(e)});
  const tb=$("rtocbox");tb.textContent="";tb.hidden=true;$("rtoc").hidden=heads.length<2;
  heads.forEach(([id,t])=>{const b=el("button",null,t);b.onclick=()=>{tb.hidden=true;document.getElementById(id).scrollIntoView({block:"start"})};tb.append(b)});
  closeDlg();$("reader").hidden=false;if(TG)TG.BackButton.show();
  const sc=$("rscroll");sc.scrollTop=0;
  let pos=0;try{pos=+localStorage.getItem("reader_pos_"+i.id)||0}catch(e){}
  if(pos>0.03&&pos<0.97){requestAnimationFrame(()=>{sc.scrollTop=pos*(sc.scrollHeight-sc.clientHeight)});toast("📖 Продолжаю с того места, где остановился")}
  rProg()}
function rProg(){const sc=$("rscroll"),k=sc.scrollHeight>sc.clientHeight?sc.scrollTop/(sc.scrollHeight-sc.clientHeight):1;
  $("rprog").style.width=(k*100)+"%";clearTimeout(rd.timer);
  rd.timer=setTimeout(()=>{try{localStorage.setItem("reader_pos_"+rd.id,String(k>0.97?0:k))}catch(e){}},400)}
function closeReader(){$("reader").hidden=true;$("rtocbox").hidden=true;if(TG&&!$("dlg").open&&$("botp").hidden)TG.BackButton.hide()}
$("rscroll").addEventListener("scroll",rProg,{passive:true});
$("rx").onclick=closeReader;$("rtoc").onclick=()=>{$("rtocbox").hidden=!$("rtocbox").hidden};
$("rminus").onclick=()=>{const p=rprefs();p.size=Math.max(15,(p.size||19)-1);rsave(p);rApply()};
$("rplus").onclick=()=>{const p=rprefs();p.size=Math.min(28,(p.size||19)+1);rsave(p);rApply()};
$("rtheme").onclick=()=>{const order=["auto","light","sepia","dark"],p=rprefs();p.theme=order[(order.indexOf(p.theme||"auto")+1)%4];
  rsave(p);rApply();toast({auto:"🌓 Тема как в Telegram",light:"☀️ Светлая",sepia:"📜 Сепия",dark:"🌙 Тёмная"}[p.theme])};
$("dread").onclick=()=>{if(cur)openReader(cur)};
$("eunlock").onclick=async()=>{try{const j=await api("unlock",{id:cur.id});
  if(j.ok){cur.l=false;renderEdit();toast("🔓 Замок снят")}}catch(e){toast("Не вышло: "+e.message)}};
$("emove").onclick=()=>cur&&openPicker([cur.id]);
$("eren").onclick=()=>{
  if(!cur||$("renbox"))return;const box=el("div","inline");box.id="renbox";
  const inp=el("input");inp.value=cur.s||"";const ok=el("button","btn primary","✅ Сохранить");box.append(inp,ok);
  $("dsum").after(box);inp.focus();
  ok.onclick=async()=>{const s=inp.value.trim();if(!s)return;
    try{const me=cur,old=me.s,wasL=!!me.l;const j=await api("rename",{id:me.id,summary:s});
      if(!j.ok){toast("🔒 Не сохранилось: пост заблокирован");return}
      me.s=s;$("dsum").textContent=s;box.remove();rerender();
      undoToast(fx("✏️ Название сохранено"),async()=>{await api("unlock",{id:me.id});await api("rename",{id:me.id,summary:old});
        if(!wasL)await api("unlock",{id:me.id});else me.l=true;me.s=old;if(cur===me)$("dsum").textContent=old;rerender()})}
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
    if(j.done){const id=cur.id;D.items=D.items.filter(i=>i.id!==id);setCount();closeDlg();rerender();
      undoToast(fx("🗑 В корзине"),()=>undoTrash([id]))}
    else toast("🔒 Не удалилось: пост заблокирован")}
  catch(e){toast("Не вышло: "+e.message)}
};
// выбор нескольких
function togglePick(i,c){if(i.l){toast("🔒 Пост заблокирован");return}
  picked.has(i.id)?picked.delete(i.id):picked.add(i.id);
  c.classList.toggle("picked",picked.has(i.id));c.querySelector(".tick").textContent=picked.has(i.id)?"✓":"";
  $("selcount").textContent="Выбрано: "+picked.size}
function exitSel(re=true){selMode=false;$("fab").hidden=SHARE_MODE;picked.clear();$("selbar").hidden=true;$("selbtn").textContent="☑️ Выбрать";if(re)rerender()}
$("selbtn").onclick=()=>{if(selMode){exitSel();return}selMode=true;$("fab").hidden=true;$("selbar").hidden=false;
  $("selbtn").textContent="✖ Отменить выбор";$("selcount").textContent="Выбрано: 0";rerender()};
$("selexit").onclick=()=>exitSel();
$("selmove").onclick=()=>{if(!picked.size){toast("Отметь посты");return}openPicker([...picked])};
$("seltrash").onclick=async()=>{if(!picked.size){toast("Отметь посты");return}
  const b=$("seltrash");if(!b.classList.contains("danger")){b.classList.add("danger");b.textContent=`Точно ${picked.size}?`;return}
  b.classList.remove("danger");b.textContent="🗑 В корзину";
  try{const ids=[...picked],had=new Set(D.items.map(i=>i.id));const j=await api("trash",{ids});
    D.items=D.items.filter(i=>!ids.includes(i.id));setCount();exitSel();
    undoToast(fx(`🗑 В корзине: ${j.done}`),()=>undoTrash(ids.filter(i=>had.has(i))))}catch(e){toast("Не вышло: "+e.message)}};
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
        fb.textContent=TG.isFullscreen?(narrow?"⤡":"⤡ Свернуть в окно"):(narrow?"⤢":"⤢ На весь экран");
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
    TG.BackButton.onClick(()=>{if($("dlg").open)closeDlg();else if($("addd").open)$("addd").close();
      else if($("pick").open)$("pick").close();else if(!$("reader").hidden)closeReader();else if(!$("deck").hidden)closeDeck();
      else if(!$("statp").hidden)$("statx").click();else if(!$("botp").hidden)botClose()});
    const r=await fetch("/app/auth",{method:"POST",body:TG.initData});
    if(!r.ok)throw new Error((await r.json().catch(()=>({}))).error||r.status);
    BASE=(await r.json()).base;
    document.querySelector("h1").textContent="📚 Архив";
  }
  const cached=SHARE_MODE?null:cacheGet();
  if(cached){try{D=JSON.parse(cached.body)}catch(e){D=null}}
  if(D){   // показали сразу из памяти устройства — а свежее подтянем фоном
    fetchData(cached.etag).then(d=>{if(d){D=d;setCount();rerender()}}).catch(()=>{});
  }else D=await fetchData();
  $("count").textContent=`${D.items.length} постов`;
  $("selbtn").hidden=SHARE_MODE;$("menubtn").hidden=SHARE_MODE;$("fab").hidden=SHARE_MODE;
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
