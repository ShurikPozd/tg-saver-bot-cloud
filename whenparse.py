"""«Когда напомнить» своими словами → дата и время по Москве. Без ИИ: частые фразы
(«завтра в 18:00», «в пятницу вечером», «3 декабря», «через 2 недели», «на выходных»).
Что не разобрали — бот спросит бесплатный ИИ (categorizer.parse_when_ai)."""

import re
from datetime import datetime, timedelta

DEFAULT_HOUR = 9
PARTS = {"утром": 9, "с утра": 9, "днем": 13, "в обед": 13, "вечером": 19, "ночью": 22}
WEEKDAYS = {
    "понедельник": 0, "пн": 0, "вторник": 1, "вт": 1, "среду": 2, "среда": 2, "ср": 2, "четверг": 3, "чт": 3,
    "пятницу": 4, "пятница": 4, "пт": 4, "субботу": 5, "суббота": 5, "сб": 5, "воскресенье": 6, "вс": 6,
}
MONTHS = {"янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6, "июл": 7, "авг": 8, "сен": 9,
          "окт": 10, "ноя": 11, "дек": 12}
NUMS = {"один": 1, "одну": 1, "одна": 1, "пару": 2, "два": 2, "две": 2, "три": 3, "четыре": 4, "пять": 5,
        "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "пятнадцать": 15, "двадцать": 20,
        "тридцать": 30, "сорок": 40}

_NUM = r"(\d+|" + "|".join(NUMS) + r")"
_REL = re.compile(_NUM.join([r"через\s+(?:", r"\s+)?"]) +
                  r"(минут\w*|мин\b|час\w*|дн\w*|день|сут\w*|недел\w*|месяц\w*|год\w*|лет)")
_HALF = re.compile(r"через\s+пол\s*(?:-\s*)?(часа|года|дня)|через\s+полчаса")
_TIME = re.compile(r"(?<![\d.])(\d{1,2})[:.](\d{2})(?![\d.])")
_HOUR = re.compile(r"\bв\s+(\d{1,2})(?:\s*(?:ч\b|час\w*))?(?:\s+(утра|дня|вечера|ночи))?(?![\d.:])")
_DATE_NUM = re.compile(r"(?<![\d:])(\d{1,2})\.(\d{1,2})(?:\.(\d{2,4}))?(?![\d:])")
_DATE_WORD = re.compile(r"(?<!\d)(\d{1,2})\s+(" + "|".join(MONTHS) + r")[а-я]*(?:\s+(\d{4}))?")


def _num(s: str) -> int:
    return int(s) if s.isdigit() else NUMS.get(s, 1)


def parse_when(text: str, now: datetime) -> datetime | None:
    """now и ответ — время по Москве (без часового пояса). None — не понял или уже прошло."""
    t = " " + re.sub(r"\s+", " ", (text or "").lower().replace("ё", "е")).strip(" .!?") + " "
    if not t.strip():
        return None
    day: datetime | None = None      # дата (полночь), если названа
    hour = minute = None             # время, если названо

    m = _HALF.search(t)
    if m:
        what = m.group(1) or "часа"
        if what == "часа":
            return now + timedelta(minutes=30)
        day = (now + (timedelta(hours=12) if what == "дня" else timedelta(days=182))).replace(hour=0, minute=0)
    m = _REL.search(t)
    if m:
        n, unit = _num(m.group(1) or "1"), m.group(2)
        if unit.startswith("мин"):
            return now + timedelta(minutes=n) if 0 < n <= 60 * 24 * 7 else None
        if unit.startswith("час"):
            return now + timedelta(hours=n) if 0 < n <= 24 * 14 else None
        days = n if unit.startswith(("дн", "день", "сут")) else 7 * n if unit.startswith("недел") \
            else 30 * n if unit.startswith("месяц") else 365 * n
        day = (now + timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)

    if day is None:
        if re.search(r"\bпослезавтра\b", t):
            day = now + timedelta(days=2)
        elif re.search(r"\bзавтра\b", t):
            day = now + timedelta(days=1)
        elif re.search(r"\bсегодня\b", t):
            day = now
        elif re.search(r"\b(?:на|в)\s+выходн", t):
            wd = now.weekday()   # ближайшая суббота 11:00; в выходной после 11 — следующий выходной день
            ahead = 5 - wd if wd < 5 else (0 if now.hour < 11 else 1) if wd == 5 else (0 if now.hour < 11 else 6)
            day, hour, minute = now + timedelta(days=ahead), 11, 0
        elif re.search(r"\bна следующей неделе\b", t):
            day = now + timedelta(days=7 - now.weekday())
        if day is None:
            m = _DATE_WORD.search(t)
            if m:
                d, mo, y = int(m.group(1)), MONTHS[m.group(2)[:3] if m.group(2)[:3] in MONTHS else m.group(2)], m.group(3)
                day = _mkdate(int(y) if y else None, mo, d, now)
            else:
                m = _DATE_NUM.search(t)
                if m and 1 <= int(m.group(2)) <= 12:
                    y = m.group(3)
                    yy = (2000 + int(y) if len(y) == 2 else int(y)) if y else None
                    day = _mkdate(yy, int(m.group(2)), int(m.group(1)), now)
                    t = t.replace(m.group(0), " ", 1)
        if day is None:
            for w, wd in WEEKDAYS.items():
                if re.search(rf"\b{w}\b", t):
                    ahead = (wd - now.weekday()) % 7
                    day = now + timedelta(days=ahead)
                    if ahead == 0:
                        day = None   # «в пятницу», а сегодня пятница — решим по времени ниже
                        same_day_wd = True
                    break
            else:
                same_day_wd = False
        else:
            same_day_wd = False
    else:
        same_day_wd = False

    m = _TIME.search(t)
    if m and int(m.group(1)) < 24 and int(m.group(2)) < 60:
        hour, minute = int(m.group(1)), int(m.group(2))
    else:
        m = _HOUR.search(t)
        if m and int(m.group(1)) <= 23:
            hour, minute = int(m.group(1)), 0
            part = m.group(2)
            if part in ("дня", "вечера") and hour < 12:
                hour += 12
            elif part == "ночи" and hour == 12:
                hour = 0
            elif not part and 1 <= hour <= 6 and not re.search(r"утр", t):
                hour += 12   # «в 5» — скорее вечер, чем 5 утра
    from_part = False
    if hour is None:
        for w, h in PARTS.items():
            if re.search(rf"(?:^|\s){w}(?:\s|$)", t):
                hour, minute, from_part = h, 0, True
                break
    if from_part and day is None and not same_day_wd and len(t.split()) > 2:
        return None   # «на майские в обед»: понятна только часть дня — пусть разберётся ИИ
    if day is None and same_day_wd:
        cand = now.replace(hour=hour if hour is not None else DEFAULT_HOUR, minute=minute or 0, second=0, microsecond=0)
        day = now if hour is not None and cand > now else now + timedelta(days=7)
    if day is None and hour is None:
        return None
    if day is None:   # только время: сегодня, а если уже прошло — завтра
        cand = now.replace(hour=hour, minute=minute or 0, second=0, microsecond=0)
        return cand if cand > now + timedelta(minutes=1) else cand + timedelta(days=1)
    res = day.replace(hour=hour if hour is not None else DEFAULT_HOUR, minute=minute or 0, second=0, microsecond=0)
    if res <= now or res > now + timedelta(days=3 * 365):
        return None
    return res


def _mkdate(year: int | None, month: int, day: int, now: datetime) -> datetime | None:
    try:
        d = datetime(year or now.year, month, day)
    except ValueError:
        return None
    if year is None and d.date() < now.date():
        try:
            d = datetime(now.year + 1, month, day)
        except ValueError:
            return None
    return d
