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


def public_url() -> str:
    return (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")


def archive_link(uid: int) -> str:
    base = public_url()
    return f"{base}/a/{make_token(uid)}/" if base else ""


def archive_data(icons: dict[str, str], bot_username: str = "") -> dict:
    """Всё для страницы одним JSON: дерево папок, посты, иконки."""
    groups = {c["category"]: (c["group"] or "").strip("/") for c in db.get_categories()}
    items = []
    for r in db.items_for_smart_search():
        stored = r["original_text"]
        aux = postctx.aux_part(stored) if postctx.has_aux(stored) else ""
        text = postctx.strip_aux(stored) if postctx.has_aux(stored) else stored
        items.append({
            "id": r["id"], "c": r["category"], "t": r["content_type"], "s": r["summary"],
            "x": (text or "")[:1500], "v": (aux or "")[:600], "d": (r["created_at"] or "")[:16],
            "ch": r["source_channel"], "tg": r["tags"].strip(), "l": r["locked"],
        })
    return {"items": items, "groups": groups, "icons": icons, "bot": bot_username,
            "folders": db.get_folder_paths()}


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
            return "/a/" not in record.getMessage()
        except Exception:
            return True


def setup(app, *, get_client, activate_user, get_icons, smart_search, get_bot_username):
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
        body = json.dumps(archive_data(get_icons(), get_bot_username()), ensure_ascii=False)
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

    async def search(request):
        _uid(request)
        q = (request.query.get("q") or "").strip()[:300]
        if not q:
            return web.json_response({"ids": [], "note": ""})
        items, note = await smart_search(q)
        return web.json_response({"ids": [i["id"] for i in items], "note": note})

    app.router.add_get("/a/{token}", page)
    app.router.add_get("/a/{token}/", page)
    app.router.add_get("/a/{token}/data.json", data)
    app.router.add_get("/a/{token}/thumb/{id}.jpg", thumb)
    app.router.add_get("/a/{token}/search", search)


PAGE_HTML = r"""<!doctype html>
<html lang="ru"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="robots" content="noindex">
<title>Мой архив</title>
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
.top{position:sticky;top:0;z-index:5;background:var(--panel);border-bottom:1px solid var(--line);padding:12px 16px}
.bar{display:flex;gap:10px;align-items:center;max-width:1600px;margin:0 auto}
h1{font-size:22px;margin:0;white-space:nowrap}
.count{color:var(--muted);font-size:15px;white-space:nowrap}
.search{flex:1;display:flex;gap:8px;min-width:0}
.search input{flex:1;min-width:0;padding:12px 14px;border:1px solid var(--line);border-radius:12px;background:var(--bg);font-size:17px}
.btn{border:0;border-radius:12px;padding:12px 16px;background:var(--chip);cursor:pointer;white-space:nowrap}
.btn.primary{background:var(--accent);color:var(--accent-ink);font-weight:600}
.chips{display:flex;gap:8px;overflow-x:auto;max-width:1600px;margin:10px auto 0;padding-bottom:2px;scrollbar-width:none}
.chip{border:0;border-radius:999px;padding:8px 14px;background:var(--chip);cursor:pointer;white-space:nowrap;font-size:15px}
.chip.on{background:var(--accent);color:var(--accent-ink)}
.layout{display:grid;grid-template-columns:300px 1fr;gap:20px;max-width:1600px;margin:0 auto;padding:16px}
.side{position:sticky;top:132px;align-self:start;max-height:calc(100vh - 150px);overflow:auto;background:var(--panel);
  border-radius:14px;padding:8px;box-shadow:var(--shadow)}
.node{display:flex;align-items:center;gap:8px;width:100%;text-align:left;border:0;background:none;border-radius:10px;
  padding:9px 10px;cursor:pointer;font-size:16px}
.node:hover{background:var(--chip)}
.node.on{background:var(--accent);color:var(--accent-ink)}
.node .n{margin-left:auto;color:var(--muted);font-size:14px}
.node.on .n{color:inherit}
.kids{padding-left:16px}
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
.th .kind{position:absolute;right:8px;bottom:8px;font-size:14px;background:rgba(0,0,0,.55);color:#fff;border-radius:8px;padding:2px 7px}
.cb{padding:10px 12px 12px;display:flex;flex-direction:column;gap:6px;flex:1}
.sum{display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden;font-size:16px}
.meta{color:var(--muted);font-size:14px;margin-top:auto;display:flex;gap:6px;flex-wrap:wrap}
.more{display:block;margin:20px auto;min-width:240px}
.empty{color:var(--muted);text-align:center;padding:60px 10px;font-size:18px}
.navbtn{display:none}
dialog{border:0;border-radius:18px;padding:0;max-width:760px;width:calc(100% - 32px);background:var(--panel);color:var(--ink)}
dialog::backdrop{background:rgba(0,0,0,.6)}
.dlg{display:flex;flex-direction:column;max-height:90vh}
.dlg .img{background:var(--thumb);display:flex;justify-content:center}
.dlg .img img{max-width:100%;max-height:60vh;display:block}
.dlg .body{padding:16px 18px;overflow:auto}
.dlg h2{font-size:19px;margin:0 0 8px}
.dlg .full{white-space:pre-wrap;margin:10px 0}
.dlg .ai{background:var(--chip);border-radius:12px;padding:10px 12px;white-space:pre-wrap;font-size:15px}
.dlg .acts{display:flex;gap:10px;padding:12px 18px;border-top:1px solid var(--line);flex-wrap:wrap}
.dlg .acts a,.dlg .acts button{flex:1;text-align:center;text-decoration:none}
.sheet{display:none}
@media (max-width: 900px){
  .layout{grid-template-columns:1fr;padding:12px}
  .side{display:none}
  .navbtn{display:inline-block}
  h1{font-size:19px}.count{display:none}
  .bar{flex-wrap:wrap}.search{flex-basis:100%;order:3}
  .grid{grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
  .sum{font-size:15.5px}.th{font-size:38px}
  .sheet.open{display:block;position:fixed;inset:0;z-index:20;background:var(--panel);overflow:auto;padding:12px 12px 40px}
  .sheet .close{position:sticky;top:0;width:100%;margin-bottom:8px}
  dialog{width:100%;max-width:none;height:100%;max-height:none;margin:0;border-radius:0}
  .dlg{max-height:100%;height:100%}.dlg .body{flex:1}
}
@media (max-width: 380px){ .grid{grid-template-columns:1fr} }
</style></head><body>
<div class="top">
  <div class="bar">
    <h1>📚 Мой архив</h1><span class="count" id="count"></span>
    <button class="btn navbtn" id="navbtn">📂 Папки</button>
    <form class="search" id="sform">
      <input id="q" type="search" placeholder="Поиск: слова из поста, описания, тегов…" autocomplete="off">
      <button class="btn primary" type="button" id="smart" title="ИИ поймёт описание своими словами">🤖 По смыслу</button>
    </form>
  </div>
  <div class="chips" id="types"></div>
</div>
<div class="layout">
  <nav class="side" id="side"></nav>
  <main>
    <p class="crumb" id="crumb"></p>
    <div class="note" id="note" hidden></div>
    <div class="grid" id="grid"></div>
    <button class="btn more" id="more" hidden>Показать ещё</button>
    <div class="empty" id="empty" hidden>Ничего не нашлось</div>
  </main>
</div>
<div class="sheet" id="sheet"><button class="btn close" id="sheetclose">✕ Закрыть</button><div id="sheettree"></div></div>
<dialog id="dlg"><div class="dlg">
  <div class="img" id="dimg"></div>
  <div class="body"><h2 id="dsum"></h2><div class="meta" id="dmeta"></div><div class="full" id="dtext"></div>
    <div class="ai" id="dai" hidden></div></div>
  <div class="acts"><a class="btn primary" id="dopen" target="_blank" rel="noopener">Открыть в Telegram</a>
    <button class="btn" id="dclose">Закрыть</button></div>
</div></dialog>
<script>
const TYPES=[["","Все"],["photo","🖼 Фото"],["video","🎬 Видео"],["text","📝 Текст"],["audio","🎵 Аудио"],["document","📄 Файлы"]];
const KIND={photo:"🖼",video:"🎬",animation:"🎞",video_note:"⏺",text:"📝",audio:"🎵",voice:"🎤",document:"📄"};
const GROUP={video:["video","animation","video_note"],audio:["audio","voice"],photo:["photo"],text:["text"],document:["document"]};
const PAGE=60;
let D=null, sel={kind:"all",val:""}, type="", q="", smartIds=null, shown=0, list=[];
const $=id=>document.getElementById(id);
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
  const walk=(n,parent)=>{
    Object.values(n.folders).sort((a,b)=>b.count-a.count).forEach(f=>{
      const b=el("button","node"+(sel.kind==="folder"&&sel.val===f.path?" on":""));
      b.append(`📁 ${icon(f.path,icon(f.name,""))} ${f.name}`.replace("  "," "),el("span","n",f.count));
      b.onclick=()=>pick("folder",f.path);parent.append(b);
      const k=el("div","kids");parent.append(k);walk(f,k);
    });
    n.cats.sort((a,b)=>b.count-a.count).forEach(c=>{
      const b=el("button","node"+(sel.kind==="cat"&&sel.val===c.name?" on":""));
      b.append(`${icon(c.name,"📦")} ${c.name}`,el("span","n",c.count));b.onclick=()=>pick("cat",c.name);parent.append(b);
    });
  };
  walk(tree(),box);
}
function pick(kind,val){sel={kind,val};$("sheet").classList.remove("open");apply();window.scrollTo({top:0})}
function inSel(i){
  if(sel.kind==="cat")return i.c===sel.val;
  if(sel.kind==="folder"){const g=D.groups[i.c]||"";return g===sel.val||g.startsWith(sel.val+"/")}
  return true;
}
function apply(){
  const words=norm(q).split(/\s+/).filter(Boolean);
  const kinds=type?GROUP[type]:null;
  let arr=D.items.filter(i=>inSel(i)&&(!kinds||kinds.includes(i.t)));
  if(smartIds){const pos=new Map(smartIds.map((id,k)=>[id,k]));arr=arr.filter(i=>pos.has(i.id)).sort((a,b)=>pos.get(a.id)-pos.get(b.id))}
  else if(words.length){arr=arr.filter(i=>{const h=norm(`${i.s} ${i.x} ${i.v} ${i.c} ${i.ch} ${i.tg}`);return words.every(w=>h.includes(w))})}
  list=arr;shown=0;$("grid").textContent="";
  $("crumb").textContent=(sel.kind==="all"?"Все посты":(sel.kind==="folder"?"📁 "+sel.val:icon(sel.val,"📦")+" "+sel.val))+` · ${arr.length}`;
  $("empty").hidden=arr.length>0;
  renderTree($("side"));renderTree($("sheettree"));more();
}
function card(i){
  const c=el("div","card");c.tabIndex=0;c.setAttribute("role","button");
  const th=el("div","th");
  const media=["photo","video","animation","video_note","document","audio"].includes(i.t);
  if(media){const img=new Image();img.loading="lazy";img.alt="";img.src=`thumb/${i.id}.jpg`;
    img.onerror=()=>{img.remove();th.prepend(KIND[i.t]||"📦")};th.append(img)}
  else{th.classList.add("tx");th.append(el("div","big",icon(i.c,KIND[i.t]||"📝")));
    const m=(i.x||"").match(/https?:\/\/(?:www\.)?([^\/\s)]+)/);if(m)th.append(el("div","dom","🔗 "+m[1]))}
  if(i.t!=="text"&&i.t!=="photo")th.append(el("span","kind",KIND[i.t]||""));
  const cb=el("div","cb");cb.append(el("div","sum",i.s||"—"));
  cb.append(el("div","meta",`${icon(i.c,"📦")} ${i.c} · ${dateRu(i.d)}`));
  c.append(th,cb);c.onclick=()=>open(i);c.onkeydown=e=>{if(e.key==="Enter")open(i)};
  return c;
}
function more(){const g=$("grid");list.slice(shown,shown+PAGE).forEach(i=>g.append(card(i)));shown+=PAGE;$("more").hidden=shown>=list.length}
function open(i){
  const img=$("dimg");img.textContent="";
  if(["photo","video","animation","video_note","document","audio"].includes(i.t)){
    const im=new Image();im.src=`thumb/${i.id}.jpg${i.t==="photo"?"?big=1":""}`;im.alt="";im.onerror=()=>im.remove();img.append(im)}
  $("dsum").textContent=i.s||"—";
  $("dmeta").textContent=`${icon(i.c,"📦")} ${i.c} · ${dateRu(i.d)}${i.ch?" · 📡 "+i.ch:""}${i.tg?" · "+i.tg:""} · #${i.id}`;
  $("dtext").textContent=i.x&&i.x!==i.s?i.x:"";
  $("dai").hidden=!i.v;$("dai").textContent=i.v?"👁 Что увидел ИИ:\n"+i.v:"";
  $("dopen").href=D.bot?`https://t.me/${D.bot}?start=i${i.id}`:"#";$("dopen").hidden=!D.bot;
  $("dlg").showModal();
}
$("dclose").onclick=()=>$("dlg").close();
$("dlg").addEventListener("click",e=>{if(e.target===$("dlg"))$("dlg").close()});
$("more").onclick=more;
new IntersectionObserver(es=>{if(es[0].isIntersecting&&!$("more").hidden)more()}).observe($("more"));
let t=null;$("q").addEventListener("input",e=>{q=e.target.value;smartIds=null;$("note").hidden=true;clearTimeout(t);t=setTimeout(apply,150)});
$("sform").onsubmit=e=>{e.preventDefault();apply()};
$("smart").onclick=async()=>{
  if(!q.trim()){$("q").focus();return}
  const b=$("smart");b.disabled=true;b.textContent="🤖 Ищу…";
  try{const r=await fetch("search?q="+encodeURIComponent(q));const j=await r.json();smartIds=j.ids;
    $("note").hidden=false;$("note").textContent=`🤖 По смыслу: «${q}» — найдено ${j.ids.length}.`+(j.note?" "+j.note:"")+" Измени текст, чтобы искать по словам.";
    sel={kind:"all",val:""};apply()}
  catch(e){$("note").hidden=false;$("note").textContent="Не получилось: "+e}
  finally{b.disabled=false;b.textContent="🤖 По смыслу"}
};
$("navbtn").onclick=()=>$("sheet").classList.add("open");$("sheetclose").onclick=()=>$("sheet").classList.remove("open");
TYPES.forEach(([k,l])=>{const c=el("button","chip"+(k===type?" on":""),l);c.onclick=()=>{type=k;
  document.querySelectorAll(".chip").forEach(x=>x.classList.toggle("on",x===c));apply()};$("types").append(c)});
fetch("data.json").then(r=>{if(!r.ok)throw new Error(r.status);return r.json()}).then(j=>{D=j;
  $("count").textContent=`${D.items.length} постов`;apply()}).catch(e=>{$("empty").hidden=false;
  $("empty").textContent="Не удалось загрузить архив ("+e.message+"). Ссылка могла устареть — попроси новую у бота: /web"});
</script>
</body></html>
"""
