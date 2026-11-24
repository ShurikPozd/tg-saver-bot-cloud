"""🔗 Ссылка на сводку комментариев YouTube (расширение «Сводка комментариев YouTube»).

POST   /api/share-summary              (X-Sec-Token) — сводка → ссылка /sum/<id>/
DELETE /api/share-summary/<id>         (X-Sec-Token) — отозвать ссылку
POST   /api/share-summary/<id>/revoke  (X-Sec-Token) — то же для клиентов без DELETE
GET    /sum/<id>/                      — страница для друга: без входа, без ИИ, без JS

Хранится в базе владельца архива: одна строка settings на ссылку (`sumshare:<id>`),
поэтому попадает в /export и ежедневные бэкапы без новых таблиц. Отзыв удаляет строку —
страница сразу отвечает 404. В ссылке нет номера владельца: id случайный.
"""

import exticon
import hashlib
import hmac
import html
import json
import logging
import re
import secrets
import time

import database as db

logger = logging.getLogger(__name__)

PREFIX = "sumshare:"            # settings: sumshare:<id> → JSON сводки
LOG_KEY = "sumshare_log"        # settings: времена создания ссылок за последние сутки
MAX_BODY = 64 * 1024            # байт в теле запроса
DAY_LIMIT = 30                  # новых ссылок в сутки (повтор той же сводки не считается)
TITLE_MAX = 300
CHANNEL_MAX = 120
SUMMARY_MAX = 6000
TOPICS_MAX = 12
TOPIC_MAX = 80
POINTS_MAX = 10
POINT_MAX = 500
COMMENTS_MAX = 6
COMMENT_MAX = 800
AUTHOR_MAX = 80

_ID_RE = re.compile(r"[A-Za-z0-9_-]{11}")
_SID_RE = re.compile(r"[A-Za-z0-9_-]{8,32}")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f​-‏‪-‮⁦-⁩]")
_MONTHS = ("января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
           "сентября", "октября", "ноября", "декабря")


class BadRequest(ValueError):
    """Запрос не годится — текст уходит клиенту в поле error."""


# ------------------------------ разбор запроса ------------------------------

def _line(v, limit: int) -> str:
    s = _CTRL_RE.sub("", str(v if v is not None else ""))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit].rstrip()


def _text(v, limit: int) -> str:
    s = _CTRL_RE.sub("", str(v if v is not None else "")).replace("\r\n", "\n").replace("\r", "\n")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


def _int(v, lo: int = 0, hi: int = 10 ** 12) -> int:
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return 0
    return n if lo <= n <= hi else 0


def _date(v) -> int:
    """Дата сводки → секунды. Принимает мс/с от эпохи или ISO-строку; иначе — «сейчас»."""
    now = int(time.time())
    if isinstance(v, (int, float)) and v > 0:
        sec = int(v / 1000) if v > 10 ** 11 else int(v)
    elif isinstance(v, str) and v.strip():
        from datetime import datetime
        try:
            sec = int(datetime.fromisoformat(v.strip().replace("Z", "+00:00")).timestamp())
        except ValueError:
            sec = now
    else:
        sec = now
    return sec if 1_100_000_000 < sec <= now + 86400 else now


def percents(sent) -> dict:
    """{positive, neutral, negative} (числа или проценты) → целые проценты с суммой 100."""
    keys = ("positive", "neutral", "negative")
    if not isinstance(sent, dict):
        return {}
    vals = []
    for k in keys:
        try:
            x = float(sent.get(k) or 0)
        except (TypeError, ValueError):
            x = 0.0
        vals.append(x if x > 0 and x < 10 ** 9 else 0.0)
    total = sum(vals)
    if total <= 0:
        return {}
    raw = [v * 100 / total for v in vals]
    out = [int(r) for r in raw]
    # метод наибольших остатков: в сумме ровно 100
    for i in sorted(range(3), key=lambda i: raw[i] - out[i], reverse=True)[: 100 - sum(out)]:
        out[i] += 1
    return dict(zip(keys, out))


def parse_request(body) -> dict:
    """Проверяет тело запроса и возвращает очищенную сводку для хранения."""
    if not isinstance(body, dict):
        raise BadRequest("json object expected")
    vid = str(body.get("videoId") or "").strip()
    if not _ID_RE.fullmatch(vid):
        raise BadRequest("invalid videoId")
    summary = _text(body.get("summary"), SUMMARY_MAX)
    if not summary:
        raise BadRequest("summary required")
    topics = []
    for t in body.get("topics") or []:
        if isinstance(t, dict) and _line(t.get("name"), TOPIC_MAX):
            topics.append({"name": _line(t.get("name"), TOPIC_MAX), "count": _int(t.get("count"), 0, 10 ** 7)})
        if len(topics) >= TOPICS_MAX:
            break
    points = [p for p in (_text(x, POINT_MAX) for x in (body.get("points") or [])[: POINTS_MAX * 2]
                          if isinstance(x, str)) if p][:POINTS_MAX]
    comments = []
    for c in body.get("comments") or []:
        if not isinstance(c, dict):
            continue
        text = _text(c.get("text"), COMMENT_MAX)
        if text:
            comments.append({"author": _line(c.get("author"), AUTHOR_MAX), "text": text,
                             "likes": _int(c.get("likes"), 0, 10 ** 9)})
        if len(comments) >= COMMENTS_MAX:
            break
    return {
        "videoId": vid,
        "title": _line(body.get("title"), TITLE_MAX),
        "channel": _line(body.get("channel"), CHANNEL_MAX),
        "summary": summary,
        "topics": topics,
        "points": points,
        "sentiment": percents(body.get("sentiment")),
        "comments": comments,
        "commentCount": _int(body.get("commentCount"), 0, 10 ** 8),
        "date": _date(body.get("date")),
    }


def fingerprint(s: dict) -> str:
    """Одна и та же сводка → один отпечаток (повторная отправка отдаёт прежнюю ссылку)."""
    return hashlib.sha256(json.dumps(s, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:32]


# ------------------------------ хранение ------------------------------

def _rows() -> list[tuple[str, dict]]:
    with db.get_connection() as conn:
        rows = conn.execute("SELECT key, value FROM settings WHERE substr(key, 1, ?) = ?",
                            (len(PREFIX), PREFIX)).fetchall()
    out = []
    for key, value in rows:
        try:
            d = json.loads(value)
        except Exception:
            continue
        if isinstance(d, dict):
            out.append((key[len(PREFIX):], d))
    return out


def get(sid: str) -> dict | None:
    if not _SID_RE.fullmatch(sid or ""):
        return None
    try:
        d = json.loads(db.get_setting(PREFIX + sid, "") or "null")
    except Exception:
        return None
    return d if isinstance(d, dict) and d.get("videoId") else None


def revoke(sid: str) -> bool:
    if not _SID_RE.fullmatch(sid or ""):
        return False
    with db.get_connection() as conn:
        n = conn.execute("DELETE FROM settings WHERE key = ?", (PREFIX + sid,)).rowcount
        conn.commit()
    return bool(n)


def _recent(now: float) -> list[float]:
    try:
        log = json.loads(db.get_setting(LOG_KEY, "[]") or "[]")
    except Exception:
        log = []
    return [t for t in log if isinstance(t, (int, float)) and now - t < 86400]


def create(s: dict) -> tuple[str, bool]:
    """→ (id, existed). Бросает LimitError, если ссылок за сутки слишком много."""
    fp = fingerprint(s)
    for sid, d in _rows():
        if d.get("fp") == fp:
            return sid, True
    now = time.time()
    log = _recent(now)
    if len(log) >= DAY_LIMIT:
        raise LimitError()
    sid = secrets.token_urlsafe(9)
    db.set_setting(PREFIX + sid, json.dumps({**s, "fp": fp, "created": int(now)}, ensure_ascii=False))
    db.set_setting(LOG_KEY, json.dumps(log + [int(now)]))
    return sid, False


class LimitError(Exception):
    pass


def public_url() -> str:
    import os
    return (os.getenv("PUBLIC_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")


def link(sid: str, base: str = "") -> str:
    return f"{(base or public_url()).rstrip('/')}/sum/{sid}/"


# ------------------------------ страница ------------------------------

def _e(s) -> str:
    return html.escape(str(s or ""), quote=True)


def _num(n: int) -> str:
    return f"{int(n):,}".replace(",", " ")


def _plural(n: int, one: str, few: str, many: str) -> str:
    n = abs(int(n)) % 100
    if 11 <= n <= 19:
        return many
    n %= 10
    return one if n == 1 else few if 2 <= n <= 4 else many


def _fmt_date(sec: int) -> str:
    from datetime import datetime, timedelta, timezone
    d = datetime.fromtimestamp(int(sec), timezone(timedelta(hours=3)))   # МСК, как во всём боте
    return f"{d.day} {_MONTHS[d.month - 1]} {d.year}"


def _paras(text: str) -> str:
    return "".join(f"<p>{_e(p).replace(chr(10), '<br>')}</p>" for p in text.split("\n\n") if p.strip())


_ICONS = {
    "play": '<path d="M8 5.5v13l10.5-6.5z"/>',
    "yt": '<rect x="2.5" y="5" width="19" height="14" rx="4"/><path d="M10 9.2v5.6l4.8-2.8z"/>',
    "chat": '<path d="M20 15a2 2 0 0 1-2 2H8l-4 4V6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2z"/>',
    "mood": '<circle cx="12" cy="12" r="9"/><path d="M8.5 14.5a4.5 4.5 0 0 0 7 0"/><path d="M9 9.5h.01M15 9.5h.01"/>',
    "tag": '<path d="M20.6 13.4 13.4 20.6a2 2 0 0 1-2.8 0L3 13V3h10l7.6 7.6a2 2 0 0 1 0 2.8z"/><path d="M7.5 7.5h.01"/>',
    "list": '<path d="M9 6h11M9 12h11M9 18h11M4.5 6h.01M4.5 12h.01M4.5 18h.01"/>',
    "star": '<path d="m12 3 2.8 5.7 6.2.9-4.5 4.4 1.1 6.2L12 17.3 6.4 20.2l1.1-6.2L3 9.6l6.2-.9z"/>',
    "heart": '<path d="M12 20s-7.5-4.6-7.5-10.2A4.3 4.3 0 0 1 12 7a4.3 4.3 0 0 1 7.5 2.8C19.5 15.4 12 20 12 20z"/>',
    "link": '<path d="M10 14a4.5 4.5 0 0 0 6.4 0l3.2-3.2a4.5 4.5 0 0 0-6.4-6.4L12 5.6"/><path d="M14 10a4.5 4.5 0 0 0-6.4 0l-3.2 3.2a4.5 4.5 0 0 0 6.4 6.4l1.2-1.2"/>',
}


def _icon(name: str, cls: str = "ic") -> str:
    return (f'<svg class="{cls}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
            f'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{_ICONS[name]}</svg>')


CSS = """
:root{--bg:#0e0e10;--bg-2:#17171a;--bg-3:#212125;--fg:#f4f4f5;--fg-2:#d0d0d6;--muted:#94949c;
 --border:rgba(255,255,255,.075);--accent:#ff3b5c;--accent-hover:#ff2149;--accent-soft:rgba(255,59,92,.14);
 --accent-ink:#ff6b85;--pos:#3ccf8e;--neu:#8b8b95;--neg:#ff5f66;--shadow:0 10px 30px rgba(0,0,0,.45);color-scheme:dark}
@media (prefers-color-scheme: light){:root{--bg:#fff;--bg-2:#f6f6f8;--bg-3:#ececf0;--fg:#141416;--fg-2:#3c3c43;
 --muted:#66666e;--border:rgba(0,0,0,.07);--accent:#e8002d;--accent-hover:#c80027;--accent-soft:rgba(232,0,45,.08);
 --accent-ink:#d1002a;--pos:#12a35f;--neu:#8a8a93;--neg:#e5383e;--shadow:0 10px 30px rgba(0,0,0,.12);color-scheme:light}}
[hidden]{display:none!important}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--fg);
 font:400 clamp(16px,.55vw + 12px,20px)/1.55 "Segoe UI Variable Text","Segoe UI Variable","Segoe UI",system-ui,-apple-system,Roboto,sans-serif}
a{color:inherit}
.ic{width:1.15em;height:1.15em;flex:none}
.top{border-bottom:1px solid var(--border)}
.top-in,.wrap{width:100%;max-width:min(1600px,100%);margin:0 auto;padding-inline:clamp(16px,3.5vw,56px)}
.top-in{display:flex;align-items:center;gap:.6em;min-height:3.6em;font-weight:600;font-size:.95em}
.brand-dot{display:grid;place-items:center;width:1.9em;height:1.9em;border-radius:.6em;background:var(--accent);color:#fff}
.brand-dot .ic{width:1.1em;height:1.1em}
.top-in .sub{color:var(--muted);font-weight:400;margin-left:auto;text-align:right}
.wrap{padding-block:clamp(20px,3vw,48px) clamp(28px,4vw,64px);display:grid;gap:clamp(20px,2.6vw,40px)}
.wrap>*{min-width:0}
.hero{display:grid;gap:clamp(16px,1.8vw,24px);align-self:start}
@media (min-width:1000px){.wrap{grid-template-columns:minmax(0,5fr) minmax(0,7fr);gap:clamp(20px,2vw,32px)}}
.cover{position:relative;display:block;aspect-ratio:16/9;border-radius:16px;overflow:hidden;background:var(--bg-3);box-shadow:var(--shadow)}
.cover img{width:100%;height:100%;object-fit:cover;display:block}
.cover .play{position:absolute;inset:0;margin:auto;width:4.2em;height:4.2em;border-radius:50%;display:grid;place-items:center;
 background:rgba(0,0,0,.55);color:#fff;transition:background .14s ease-out,transform .14s ease-out}
.cover .play .ic{width:2em;height:2em;fill:currentColor;stroke:none}
.cover:hover .play{background:var(--accent);transform:scale(1.06)}
h1{margin:0;font-size:clamp(1.35em,1.1vw + 1em,2em);line-height:1.25;font-weight:700;overflow-wrap:anywhere}
.meta{margin:.35em 0 0;color:var(--muted);display:flex;flex-wrap:wrap;gap:.25em .9em}
.meta b{color:var(--fg-2);font-weight:600}
.btn{display:flex;align-items:center;justify-content:center;gap:.55em;min-height:3em;padding:.6em 1.4em;border-radius:999px;
 background:var(--accent);color:#fff;text-decoration:none;font-weight:600;transition:background .14s ease-out}
.btn:hover{background:var(--accent-hover)}
.btn:focus-visible,.cover:focus-visible{outline:3px solid var(--accent);outline-offset:3px}
.card{background:var(--bg-2);border:1px solid var(--border);border-radius:16px;padding:clamp(16px,1.6vw,24px)}
h2{display:flex;align-items:center;gap:.5em;margin:0 0 .75em;font-size:1.08em;font-weight:700}
h2 .ic{color:var(--accent-ink)}
.summary p{margin:0 0 .8em;color:var(--fg-2)}
.summary p:last-child{margin-bottom:0}
.bar{display:flex;height:.8em;border-radius:999px;overflow:hidden;gap:3px;background:var(--bg-3)}
.bar i{display:block;min-width:4px}
.legend{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:.5em;margin-top:.8em}
.mood{container-type:inline-size;display:flex;flex-direction:column}
.mood .bar{margin-top:auto}.mood .legend{margin-bottom:auto}
.legend span{display:flex;align-items:center;justify-content:center;gap:.45em;padding:.45em .3em;border-radius:12px;background:var(--bg-3);
 color:var(--fg-2);font-size:.92em;text-align:center;min-width:0}
.legend em{display:inline-flex;align-items:center;gap:.4em;font-style:normal}
.legend b{font-size:1.1em;color:var(--fg)}
@container (max-width:600px){.legend span{flex-direction:column;gap:.1em;padding:.5em .3em}}
.dot{width:.65em;height:.65em;border-radius:50%;flex:none}
.pos{background:var(--pos)}.neu{background:var(--neu)}.neg{background:var(--neg)}
.chips{display:flex;flex-wrap:wrap;gap:.5em}
.chip{display:inline-flex;align-items:center;gap:.5em;padding:.4em .9em;border-radius:999px;background:var(--bg-3);color:var(--fg);
 font-size:.95em;max-width:100%;overflow-wrap:anywhere}
.chip b{color:var(--accent-ink);font-weight:700}
ol.points{margin:0;padding:0;list-style:none;counter-reset:p;display:grid;gap:.75em}
ol.points li{counter-increment:p;display:grid;grid-template-columns:2em minmax(0,1fr);gap:.6em;align-items:start;color:var(--fg-2)}
ol.points li::before{content:counter(p);display:grid;place-items:center;width:2em;height:2em;border-radius:50%;
 background:var(--accent-soft);color:var(--accent-ink);font-weight:700;font-size:.9em}
.comments{display:grid;gap:.75em}
@media (min-width:1000px){.comments{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media (min-width:1400px){.comments{grid-template-columns:repeat(3,minmax(0,1fr))}}
.comment{background:var(--bg-3);border-radius:12px;padding:.85em 1em;min-width:0}
.c-meta{display:flex;align-items:center;gap:.6em;margin-bottom:.3em;font-size:.92em}
.c-author{font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.c-likes{display:inline-flex;align-items:center;gap:.3em;color:var(--muted);margin-left:auto;flex:none}
.c-likes .ic{width:1em;height:1em}
.comment p{margin:0;color:var(--fg-2);overflow-wrap:anywhere;white-space:pre-line}
.foot{border-top:1px solid var(--border);padding-top:clamp(16px,2vw,24px);color:var(--muted);font-size:.88em;text-align:center}
.gone{min-height:calc(100vh - 3.6em - 2px);display:grid;place-items:center;text-align:center;padding:24px 16px}
.gone .big{display:grid;place-items:center;width:4em;height:4em;margin:0 auto 1em;border-radius:50%;background:var(--accent-soft);color:var(--accent-ink)}
.gone .big .ic{width:2em;height:2em}
.gone h1{margin-bottom:.4em}
.gone p{margin:0 auto;max-width:32em;color:var(--muted)}
@media (max-width:560px){.top-in .sub{display:none}}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
"""

_HEAD = """<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="robots" content="noindex,nofollow"><meta name="referrer" content="no-referrer">
<meta name="color-scheme" content="dark light">
<title>{title}</title>{favicon}{og}<style>{css}</style></head><body>
<header class="top"><div class="top-in"><span class="brand-dot">{chat}</span><span>Сводка комментариев YouTube</span>
<span class="sub">{sub}</span></div></header>"""


def _areas(blocks: dict) -> str:
    """Сетка без пустот: на телефоне — столбик; на широком — обложка слева, сводка и мнения
    справа, ниже настроение | темы, комментарии и подпись — на всю ширину. Нет блока — его
    место занимает сосед, а не пустая клетка."""
    has = lambda k: k in blocks  # noqa: E731
    narrow = [k for k in ("hero", "mood", "topics", "sum", "pts", "cmts", "foot") if has(k)]
    wide = ['"hero sum"'] + (['"hero pts"'] if has("pts") else [])
    pair = [k for k in ("mood", "topics") if has(k)]
    if pair:
        wide.append(f'"{pair[0]} {pair[-1]}"')
    if has("cmts"):
        wide.append('"cmts cmts"')
    wide.append('"foot foot"')
    return (".wrap{grid-template-areas:" + " ".join(f'"{k}"' for k in narrow) + "}"
            "@media (min-width:1000px){.wrap{grid-template-areas:" + " ".join(wide) + "}}")


def render_page(s: dict) -> str:
    vid = s["videoId"]
    url = f"https://www.youtube.com/watch?v={vid}"
    img = f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
    title = s.get("title") or "Видео на YouTube"
    n = int(s.get("commentCount") or 0)
    by = f"по {_num(n)} {_plural(n, 'комментарию', 'комментариям', 'комментариям')}" if n else ""
    desc = (s.get("summary") or "")[:200]
    og = (f'<meta property="og:type" content="article"><meta property="og:title" content="{_e("Сводка комментариев: " + title)}">'
          f'<meta property="og:description" content="{_e(desc)}"><meta property="og:image" content="{_e(img)}">'
          '<meta name="twitter:card" content="summary_large_image">')
    out = [_HEAD.format(favicon=exticon.HEAD, title=_e("Сводка комментариев: " + title), og=og, css=CSS, chat=_icon("chat"),
                        sub=_e("составлена ИИ по комментариям"))]
    blocks = {}   # имя области сетки → разметка
    hero = [f'<a class="cover" href="{_e(url)}" target="_blank" rel="noopener noreferrer" aria-label="Смотреть на YouTube">'
            f'<img src="{_e(img)}" alt="" width="480" height="270" referrerpolicy="no-referrer">'
            f'<span class="play">{_icon("play")}</span></a>']
    meta = []
    if s.get("channel"):
        meta.append(f'<b>{_e(s["channel"])}</b>')
    meta.append(f'<span>сводка от {_e(_fmt_date(s.get("date") or s.get("created") or time.time()))}</span>')
    if by:
        meta.append(f"<span>{_e(by)}</span>")
    hero.append(f'<div><h1>{_e(title)}</h1><p class="meta">{"".join(meta)}</p></div>')
    hero.append(f'<a class="btn" href="{_e(url)}" target="_blank" rel="noopener noreferrer">{_icon("yt")}Открыть видео на YouTube</a>')
    blocks["hero"] = f'<section class="hero">{"".join(hero)}</section>'
    blocks["sum"] = (f'<section class="card summary"><h2>{_icon("chat")}Что говорят в комментариях</h2>'
                     f'{_paras(s.get("summary") or "")}</section>')
    if s.get("points"):
        lis = "".join(f"<li><span>{_e(p)}</span></li>" for p in s["points"])
        blocks["pts"] = f'<section class="card"><h2>{_icon("list")}Ключевые мнения</h2><ol class="points">{lis}</ol></section>'
    sp = s.get("sentiment") or {}
    if sp:
        parts = [("positive", "pos", "позитив"), ("neutral", "neu", "нейтрально"), ("negative", "neg", "негатив")]
        bar = "".join(f'<i class="{c}" style="flex:{sp.get(k, 0)} 1 0"></i>' for k, c, _ in parts if sp.get(k))
        legend = "".join(f'<span><em><i class="dot {c}"></i><b>{sp.get(k, 0)}%</b></em>{label}</span>' for k, c, label in parts)
        blocks["mood"] = (f'<section class="card mood"><h2>{_icon("mood")}Настроение зрителей</h2>'
                          f'<div class="bar" role="img" aria-label="позитив {sp.get("positive", 0)}%, нейтрально '
                          f'{sp.get("neutral", 0)}%, негатив {sp.get("negative", 0)}%">{bar}</div>'
                          f'<div class="legend">{legend}</div></section>')
    if s.get("topics"):
        chips = "".join(f'<span class="chip">{_e(t["name"])}' + (f' <b>{_num(t["count"])}</b>' if t.get("count") else "")
                        + "</span>" for t in s["topics"])
        blocks["topics"] = f'<section class="card"><h2>{_icon("tag")}Главные темы</h2><div class="chips">{chips}</div></section>'
    if s.get("comments"):
        cards = []
        for c in s["comments"]:
            likes = (f'<span class="c-likes" title="Лайки">{_icon("heart")}{_num(c["likes"])}</span>'
                     if c.get("likes") else "")
            cards.append(f'<article class="comment"><div class="c-meta"><span class="c-author">{_e(c.get("author") or "Зритель")}'
                         f'</span>{likes}</div><p>{_e(c["text"])}</p></article>')
        blocks["cmts"] = (f'<section class="card"><h2>{_icon("star")}Яркие комментарии</h2>'
                          f'<div class="comments">{"".join(cards)}</div></section>')
    blocks["foot"] = ('<footer class="foot">Сводку составил ИИ по открытым комментариям под видео — '
                      'возможны неточности. Сделано расширением «Сводка комментариев YouTube».</footer>')
    out.append(f"<style>{_areas(blocks)}</style>")
    # область сетки — у самого блока (карточка растягивается на всю клетку, без пустот под ней)
    out.append('<main class="wrap">' + "".join(re.sub(r"^<(\w+) ", lambda m: f'<{m.group(1)} style="grid-area:{k}" ', v, count=1)
                                               for k, v in blocks.items()) + "</main></body></html>")
    return "".join(out)


def render_gone() -> str:
    return (_HEAD.format(favicon=exticon.HEAD, title="Ссылка отозвана", og="", css=CSS, chat=_icon("chat"), sub="")
            + f'<main class="gone"><div><span class="big">{_icon("link")}</span><h1>Ссылка не работает</h1>'
              "<p>Её отозвали или в ней опечатка. Попросите у того, кто поделился, новую ссылку на сводку.</p>"
              "</div></main></body></html>")


# ------------------------------ HTTP ------------------------------

_CORS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "POST, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, X-Sec-Token",
}
# страница чужая для сайта бота: ни скриптов, ни форм, картинки — только обложка YouTube
_PAGE_HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": ("default-src 'none'; img-src https://i.ytimg.com data:; style-src 'unsafe-inline'; "
                                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
}


class _HideLinks(logging.Filter):
    """Ссылка на сводку — по сути пароль: строки доступа к /sum/<id>/ в лог не пишем."""

    def filter(self, record):
        try:
            return "/sum/" not in record.getMessage() and "/api/share-summary/" not in record.getMessage()
        except Exception:
            return True


async def _read_limited(stream, limit: int) -> bytes | None:
    buf = bytearray()
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            return bytes(buf)
        buf += chunk
        if len(buf) > limit:
            return None


def setup(app, *, activate_owner, get_secret) -> None:
    """activate_owner() → id владельца (и включает его базу) или None; get_secret() → EXT_SECRET."""
    from aiohttp import web

    logging.getLogger("aiohttp.access").addFilter(_HideLinks())

    def reply(payload, status=200):
        return web.json_response(payload, status=status, headers=_CORS)

    def authorized(request) -> bool:
        secret = get_secret() or ""
        token = request.headers.get("X-Sec-Token", "")
        return bool(secret) and hmac.compare_digest(token.encode("utf-8"), secret.encode("utf-8"))

    async def options(_request):
        return web.Response(status=204, headers=_CORS)

    async def create_api(request):
        if not authorized(request):
            return reply({"error": "forbidden", "code": "forbidden"}, 403)
        if (request.content_length or 0) > MAX_BODY:
            return reply({"error": "body too large", "code": "too_large"}, 413)
        raw = await _read_limited(request.content, MAX_BODY)
        if raw is None:
            return reply({"error": "body too large", "code": "too_large"}, 413)
        try:
            s = parse_request(json.loads(raw.decode("utf-8")))
        except BadRequest as e:
            return reply({"error": str(e), "code": "bad_request"}, 400)
        except Exception:
            return reply({"error": "json body expected", "code": "bad_request"}, 400)
        if not activate_owner():
            return reply({"error": "Бот не знает владельца архива (OWNER_ID)", "code": "no_owner"}, 503)
        try:
            sid, existed = create(s)
        except LimitError:
            return reply({"error": f"Не больше {DAY_LIMIT} ссылок в сутки", "code": "rate"}, 429)
        except Exception:
            logger.warning("share-summary: не сохранилось", exc_info=True)
            return reply({"error": "Сбой в боте", "code": "internal"}, 500)
        base = public_url() or f"{request.scheme}://{request.host}"
        logger.info("share-summary: %s ссылка на сводку %s", "та же" if existed else "новая", s["videoId"])
        return reply({"ok": True, "id": sid, "url": link(sid, base), "existed": existed})

    async def revoke_api(request):
        if not authorized(request):
            return reply({"error": "forbidden", "code": "forbidden"}, 403)
        if not activate_owner():
            return reply({"error": "Бот не знает владельца архива (OWNER_ID)", "code": "no_owner"}, 503)
        sid = request.match_info.get("sid", "")
        return reply({"ok": True, "revoked": revoke(sid)})

    async def page(request):
        sid = request.match_info.get("sid", "")
        s = get(sid) if activate_owner() else None
        if s is None:
            return web.Response(text=render_gone(), status=404, content_type="text/html", charset="utf-8",
                                headers=_PAGE_HEADERS)
        if not request.path.endswith("/"):
            raise web.HTTPFound(request.path + "/")
        return web.Response(text=render_page(s), content_type="text/html", charset="utf-8", headers=_PAGE_HEADERS)

    app.router.add_post("/api/share-summary", create_api)
    app.router.add_options("/api/share-summary", options)
    app.router.add_delete("/api/share-summary/{sid}", revoke_api)
    app.router.add_post("/api/share-summary/{sid}/revoke", revoke_api)
    app.router.add_options("/api/share-summary/{sid}", options)
    app.router.add_options("/api/share-summary/{sid}/revoke", options)
    app.router.add_get("/sum/{sid}", page)
    app.router.add_get("/sum/{sid}/", page)
