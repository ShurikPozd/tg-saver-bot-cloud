"""📥 Закладки браузера → посты архива. Формат файла — общий для Chrome, Яндекс Браузера, Edge,
Firefox и Safari («Экспорт закладок в HTML», он же Netscape Bookmark File)."""

from html.parser import HTMLParser

MAX_BOOKMARKS = 3000
# корневые папки браузеров — в путь не идут
ROOTS = {
    "панель закладок", "bookmarks bar", "bookmarks toolbar", "панель избранного", "favorites bar", "избранное",
    "другие закладки", "other bookmarks", "мобильные закладки", "mobile bookmarks", "закладки", "bookmarks",
    "bookmarks menu", "меню закладок", "закладки меню", "unsorted bookmarks", "неподшитые закладки",
    "панель закладок яндекс браузера", "imported", "импортированные",
}


def is_bookmarks_file(raw: bytes) -> bool:
    head = raw[:600].decode("utf-8", "ignore").upper()
    return "NETSCAPE-BOOKMARK-FILE" in head or ("<DL>" in raw[:20000].decode("utf-8", "ignore").upper()
                                                 and "HREF=" in raw[:20000].decode("utf-8", "ignore").upper())


class _P(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack: list[str | None] = []
        self.pending: str | None = None
        self.in_h3 = self.in_a = False
        self.h3 = self.title = ""
        self.href = ""
        self.added = 0
        self.out: list[dict] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag == "h3":
            self.in_h3, self.h3 = True, ""
        elif tag == "dl":
            self.stack.append(self.pending)
            self.pending = None
        elif tag == "a":
            self.in_a, self.title = True, ""
            self.href = (a.get("href") or "").strip()
            try:
                self.added = int(a.get("add_date") or 0)
            except ValueError:
                self.added = 0

    def handle_endtag(self, tag):
        if tag == "h3":
            self.in_h3 = False
            self.pending = " ".join(self.h3.split())[:60] or None
        elif tag == "dl":
            if self.stack:
                self.stack.pop()
        elif tag == "a" and self.in_a:
            self.in_a = False
            if self.href.lower().startswith(("http://", "https://")) and len(self.out) < MAX_BOOKMARKS:
                folders = [f for f in self.stack if f and f.casefold() not in ROOTS]
                self.out.append({"url": self.href[:2000], "title": " ".join(self.title.split())[:300],
                                 "folders": folders, "added": self.added})

    def handle_data(self, data):
        if self.in_h3:
            self.h3 += data
        elif self.in_a:
            self.title += data


def parse(raw: bytes) -> list[dict]:
    """[{url, title, folders: [путь папок без корня браузера], added: unix-время или 0}] без повторов ссылок."""
    text = raw.decode("utf-8", "ignore")
    p = _P()
    try:
        p.feed(text)
        p.close()
    except Exception:
        pass
    seen, out = set(), []
    for b in p.out:
        key = b["url"].split("#")[0].rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        out.append(b)
    return out
