#!/usr/bin/env python3
"""Собирает данные для дашборда в data.json.

Читает config.json, для каждого блока вызывает обработчик по полю "type"
и пишет результат в data.json. Только стандартная библиотека Python.

Как добавить новый тип блока:
  1. написать функцию вида  def мой_блок(card, tz): ... return [строки]
  2. добавить её в словарь HANDLERS внизу файла
  3. использовать "type": "мой_блок" в config.json
"""
import csv
import io
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from html import unescape
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
TSDB = "https://www.thesportsdb.com/api/v1/json/" + os.environ.get("THESPORTSDB_KEY", "123")
FD = "https://api.football-data.org/v4"
WD = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
MONTHS = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля",
          "августа", "сентября", "октября", "ноября", "декабря"]


# ---------- общие помощники ----------

def http_json(url, headers=None):
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (dashboard-collector)", **(headers or {})})
    with urlopen(req, timeout=30) as r:
        text = r.read().decode("utf-8")
    return json.loads(text) if text.strip() else {}


def parse_utc(s):
    dt = datetime.fromisoformat(s.replace("Z", ""))
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def day_label(dt):
    return f"{WD[dt.weekday()]}, {dt.day} {MONTHS[dt.month - 1]}"


def table_lines(rows, cutoffs):
    """rows: список (позиция, команда, И, В, Н, П, разница, очки)"""
    lines = ["#   Команда                    И  В  Н  П  РМ   О"]
    for pos, team, played, won, draw, lost, gd, pts in rows:
        gd_s = f"{'+' if gd > 0 else ''}{gd}"
        lines.append(f"{str(pos):<3} {str(team):<26}{played:>2} {won:>2} {draw:>2} {lost:>2} {gd_s:>4} {pts:>3}")
        for cpos, text in cutoffs:
            if pos == cpos:
                lines.append(f"--- {text} ---")
    return lines


# ---------- источник: TheSportsDB (ближайший матч команды) ----------

def badge_of(t):
    return (t or {}).get("strBadge") or (t or {}).get("strTeamBadge") or None


def team_badge(team_id):
    try:
        t = (http_json(f"{TSDB}/lookupteam.php?id={team_id}").get("teams") or [None])[0]
        return badge_of(t)
    except Exception as e:
        print(f"::warning::эмблема команды {team_id}: {e}")
        return None


def team_match(card, tz):
    """Ближайший матч команды: (строки текстом, словарь для карточки с эмблемами)."""
    name, country = card["team"], card.get("country")
    found = http_json(f"{TSDB}/searchteams.php?t={quote(name)}")
    teams = [t for t in (found.get("teams") or []) if t.get("strSport") == "Soccer"]
    team = next((t for t in teams if not country or t.get("strCountry") == country),
                teams[0] if teams else None)
    if not team:
        raise RuntimeError(f"команда не найдена: {name}")
    events = http_json(f"{TSDB}/eventsnext.php?id={team['idTeam']}").get("events") or []
    if not events:
        return ["Ближайших матчей в источнике нет."], None

    def when(e):
        if e.get("strTimestamp"):
            return parse_utc(e["strTimestamp"])
        if e.get("dateEvent"):
            return parse_utc(e["dateEvent"] + "T" + (e.get("strTime") or "00:00:00"))
        return None

    events = sorted(events, key=lambda e: when(e) or datetime.max.replace(tzinfo=timezone.utc))
    ev = events[0]
    w = when(ev)
    lines = [ev.get("strEvent") or name]
    when_txt = None
    if w:
        loc = w.astimezone(tz)
        when_txt = f"{day_label(loc)}, {loc:%H:%M}"
        lines.append(when_txt)
    where = ", ".join(x for x in (ev.get("strLeague"), ev.get("strVenue")) if x)
    if where:
        lines.append(where)

    own_home = str(ev.get("idHomeTeam")) == str(team["idTeam"])
    own_side, opp_side = ("Home", "Away") if own_home else ("Away", "Home")
    opp_id = ev.get(f"id{opp_side}Team")
    match = {
        "own": {"name": ev.get(f"str{own_side}Team") or team.get("strTeam") or name,
                "badge": ev.get(f"str{own_side}TeamBadge") or badge_of(team)},
        "opp": {"name": ev.get(f"str{opp_side}Team") or "?",
                "badge": ev.get(f"str{opp_side}TeamBadge") or (team_badge(opp_id) if opp_id else None)},
        "home": own_home, "when": when_txt, "where": where or None,
    }
    return lines, match


def team_next(card, tz):
    return team_match(card, tz)[0]


def teams_next(card, tz):
    """Несколько команд подряд: ближайший матч каждой (текстом и структурой для эмблем)."""
    lines, matches, fails = [], [], 0
    for t in card["teams"]:
        label = t.get("label") or t["name"]
        if lines:
            lines.append("")
        lines.append(label)
        try:
            tl, m = team_match({"team": t["name"], "country": t.get("country")}, tz)
            lines += ["  " + x for x in tl]
            if m:
                m["label"] = label
                if t.get("emblem"):
                    m["emblem"] = t["emblem"]
                matches.append(m)
            else:
                matches.append({"label": label, "error": tl[0]})
        except Exception as e:
            fails += 1
            print(f"::warning::{label}: {e}")
            lines.append(f"  не удалось получить данные ({e})")
            matches.append({"label": label, "error": f"не удалось получить данные ({e})"})
    if fails == len(card["teams"]):
        raise RuntimeError("не удалось получить данные ни по одной команде")
    return {"lines": lines, "matches": matches}


# ---------- источник: football-data.org (турниры) ----------

def fd_get(path):
    key = os.environ.get("FOOTBALL_DATA_KEY")
    if not key:
        raise RuntimeError("нет ключа: добавь секрет FOOTBALL_DATA_KEY в настройках репозитория")
    return http_json(FD + path, {"X-Auth-Token": key})


def fd_table(card, tz):
    j = fd_get(f"/competitions/{card['comp']}/standings")
    sts = j.get("standings") or []
    st = next((x for x in sts if x.get("type") == "TOTAL"), sts[0] if sts else None)
    rows = (st or {}).get("table") or []
    if not rows:
        return ["Таблица в источнике пока пуста."]
    data = [(r["position"], r["team"].get("shortName") or r["team"]["name"], r["playedGames"],
             r["won"], r["draw"], r["lost"], r["goalDifference"], r["points"]) for r in rows]
    lines = []
    md = (j.get("season") or {}).get("currentMatchday")
    if md:
        lines.append(f"Тур {md}")
    return lines + table_lines(data, [tuple(c) for c in card.get("cutoffs", [])])


def fd_next(card, tz):
    days = min(int(card.get("days", 9)), 60)
    first_only = bool(card.get("first_day_only"))  # показать только ближайший игровой день
    now = datetime.now(timezone.utc)
    ok = {"SCHEDULED", "TIMED", "IN_PLAY", "PAUSED"}
    seen, ms = set(), []
    for start in range(0, days, 10):  # источник отдаёт не больше 10 дней за один запрос
        frm = (now + timedelta(days=start)).strftime("%Y-%m-%d")
        to = (now + timedelta(days=min(start + 10, days))).strftime("%Y-%m-%d")
        j = fd_get(f"/competitions/{card['comp']}/matches?dateFrom={frm}&dateTo={to}")
        for m in j.get("matches") or []:
            key = m.get("id") or (m["utcDate"], m["homeTeam"].get("name"))
            if m.get("status") in ok and key not in seen:
                seen.add(key)
                ms.append(m)
        if first_only and ms:
            break  # нашли ближайшие матчи, дальше искать не нужно
    ms.sort(key=lambda m: m["utcDate"])
    if not ms:
        return [f"В ближайшие {days} дней матчей нет."]
    if first_only:
        first_date = parse_utc(ms[0]["utcDate"]).astimezone(tz).date()
        ms = [m for m in ms if parse_utc(m["utcDate"]).astimezone(tz).date() == first_date]
    lines = []
    cur = ""
    for m in ms:
        loc = parse_utc(m["utcDate"]).astimezone(tz)
        dl = day_label(loc)
        if dl != cur:
            cur = dl
            lines += ([""] if lines else []) + [dl.upper()]
        h, a = m["homeTeam"], m["awayTeam"]
        lines.append(f"  {loc:%H:%M}  {h.get('shortName') or h['name']} — {a.get('shortName') or a['name']}")
    lines += ["", f"время по {tz.key}"]
    return lines


# ---------- источник: ESPN (теннис, неофициальный открытый API, ключ не нужен) ----------

ESPN = "https://site.api.espn.com/apis/site/v2/sports/tennis"


def slam_russians(card, tz):
    """Ближайший шлем: невылетевшие игроки из топ-N рейтинга, родившиеся в нужной стране, и их ближайший матч."""
    today = datetime.now(tz).date()
    slam = next((s for s in card["slams"] if date.fromisoformat(s["end"]) >= today), None)
    if not slam:
        return ["Ближайший турнир не задан: добавь его в slams в config.json."]
    start, end = date.fromisoformat(slam["start"]), date.fromisoformat(slam["end"])
    lines = [f"{slam['name']}: {start.day} {MONTHS[start.month - 1]} — {end.day} {MONTHS[end.month - 1]} (основная сетка)"]
    if today < start - timedelta(days=4):
        lines.append(f"До старта {(start - today).days} дн., сетки пока нет.")
        return lines
    top = int(card.get("top", 100))
    born = [b.lower() for b in card.get("born_in", ["russia"])]

    # матчи одиночного разряда с первого дня до ближайших двух дней расписания
    last = min(end, max(today, start) + timedelta(days=2))
    games = []  # (время UTC, [(id, имя, победитель)], завершён, раунд)
    d = start
    while d <= last:
        for tour in ("atp", "wta"):
            j = http_json(f"{ESPN}/{tour}/scoreboard?dates={d:%Y%m%d}")
            for ev in j.get("events") or []:
                if slam["name"].lower() not in (ev.get("name") or "").lower():
                    continue
                for g in ev.get("groupings") or []:
                    if "singles" not in ((g.get("grouping") or {}).get("displayName") or "").lower():
                        continue
                    for c in g.get("competitions") or []:
                        pl = []
                        for x in c.get("competitors") or []:
                            a = x.get("athlete") or {}
                            pl.append((str(a.get("id") or x.get("id") or ""), a.get("displayName") or "?", x.get("winner")))
                        done = ((c.get("status") or {}).get("type") or {}).get("state") == "post"
                        games.append((parse_utc(c["date"]), pl, done, (c.get("round") or {}).get("displayName")))
        d += timedelta(days=1)
    games.sort(key=lambda g: g[0])
    if not games:
        lines.append("Сетки или расписания в источнике пока нет.")
        return lines

    # рейтинг: id игроков из топ-N
    ranks = {}
    for tour in ("atp", "wta"):
        rk = (http_json(f"{ESPN}/{tour}/rankings").get("rankings") or [{}])[0].get("ranks") or []
        for r in rk:
            if int(r.get("current") or 9999) <= top:
                ranks[str((r.get("athlete") or {}).get("id"))] = int(r["current"])
    if not ranks:
        raise RuntimeError("рейтинг в источнике не получен")

    # невылетевшие игроки из топ-N
    lost = {i for g in games for i, _, w in g[1] if g[2] and w is False}
    cand = {i: n for g in games for i, n, _ in g[1] if i in ranks and i not in lost}

    # место рождения
    def is_born(i):
        try:
            a = http_json(f"https://sports.core.api.espn.com/v2/sports/tennis/athletes/{i}")
            return any(b in json.dumps(a.get("birthPlace") or {}).lower() for b in born)
        except Exception as e:
            print(f"::warning::игрок {i}: {e}")
            return None
    with ThreadPoolExecutor(8) as ex:
        res = dict(zip(cand, ex.map(is_born, cand)))
    if cand and all(v is None for v in res.values()):
        raise RuntimeError("не удалось получить место рождения игроков")

    far = datetime.max.replace(tzinfo=timezone.utc)
    rows = []
    for i, ok in res.items():
        if not ok:
            continue
        nxt = next((g for g in games if not g[2] and any(x[0] == i for x in g[1])), None)
        head = f"{cand[i]} (#{ranks[i]}) — "
        if nxt:
            loc = nxt[0].astimezone(tz)
            opp = next((n for j, n, _ in nxt[1] if j != i), "соперник уточняется")
            rnd = f", {nxt[3]}" if nxt[3] else ""
            rows.append((nxt[0], head + f"{opp}, {day_label(loc)}, {loc:%H:%M}{rnd}"))
        else:
            rows.append((far, head + "ждёт следующего матча"))
    if not rows:
        lines.append(f"Невылетевших игроков из топ-{top}, родившихся в нужной стране, нет.")
        return lines
    rows.sort(key=lambda r: r[0])
    return lines + [""] + [r[1] for r in rows] + ["", f"время по {tz.key}"]


# ---------- источник: Steam (магазин, ключ не нужен) ----------

def steam_upcoming(card, tz):
    """Топ-N самых ожидаемых игр до конца года по списку «Популярные ожидаемые» Steam
    (его порядок строится по вишлистам, предзаказам и успеху прошлых игр студии)."""
    n = int(card.get("count", 5))
    today = datetime.now(tz).date()
    end = date(today.year, 12, 31)
    games, seen = [], set()
    for start in range(0, int(card.get("scan", 150)), 50):
        j = http_json("https://store.steampowered.com/search/results/?query&count=50"
                      f"&start={start}&filter=popularcomingsoon&category1=998&cc=us&l=english&infinite=1")
        page = j.get("results_html") or ""
        if not page:
            break
        for appid, row in re.findall(r'<a [^>]*data-ds-appid="([\d,]+)"[^>]*>(.*?)</a>', page, re.S):
            t = re.search(r'class="title">(.*?)</span>', row, re.S)
            r = re.search(r'search_released[^>]*>\s*(.*?)\s*</div>', row, re.S)
            if not t or not r or appid in seen:
                continue
            seen.add(appid)
            d = None
            for fmt in ("%b %d, %Y", "%d %b, %Y"):
                try:
                    d = datetime.strptime(r.group(1).strip(), fmt).date()
                    break
                except ValueError:
                    pass
            if d and today <= d <= end:
                games.append((unescape(t.group(1).strip()), d))
        if len(games) >= n:
            break
    if not games:
        return ["В списке «Популярные ожидаемые» нет игр с точной датой до конца года."]
    lines = [f"Топ-{n} ожидаемых PC-новинок до {end.day} {MONTHS[end.month - 1]}"]
    for i, (name, d) in enumerate(games[:n], 1):
        lines.append(f"{i}. {name} — {d.day} {MONTHS[d.month - 1]}")
    return lines


# ---------- источник: Scryfall (сеты Magic, ключ не нужен) ----------

def mtg_roadmap(card, tz):
    """Основные бумажные сеты Magic от сегодня до конца года; без даты — TBA."""
    today = datetime.now(tz).date()
    end = date(today.year, 12, 31)
    types = set(card.get("types", ["core", "expansion"]))
    j = http_json("https://api.scryfall.com/sets", {
        "User-Agent": "DashNews/1.0 (github.com/SolTheCaptain/DashNews)",
        "Accept": "application/json;q=0.9,*/*;q=0.8"})
    dated, tba = [], list(card.get("extra", []))
    for s in j.get("data") or []:
        if s.get("digital") or s.get("set_type") not in types:
            continue
        if not s.get("released_at"):
            tba.append(s["name"])
            continue
        d = date.fromisoformat(s["released_at"])
        if today <= d <= end:
            dated.append((d, s["name"]))
    dated.sort()
    if not dated and not tba:
        return [f"До конца {end.year} года новых сетов в источнике нет."]
    lines = [f"{d.day} {MONTHS[d.month - 1]} — {name}" for d, name in dated]
    lines += [f"TBA — {name}" for name in tba]
    return lines


# ---------- источник: Wizards (баны), имена карт сверяем со Scryfall ----------

MONTHS_EN = ["january", "february", "march", "april", "may", "june", "july",
             "august", "september", "october", "november", "december"]
BR_URL = "https://magic.wizards.com/en/news/announcements/banned-and-restricted-{m}-{d}-{y}"
BR_FORMATS = r"Competitive Brawl|Standard|Pioneer|Modern|Legacy|Vintage|Pauper|Alchemy|Historic|Timeless|Brawl"
BR_SKIP = {"Standard", "Pioneer", "Modern", "Legacy", "Vintage", "Pauper", "Alchemy", "Historic",
           "Timeless", "Brawl", "Commander", "Explorer", "Arena", "Eternal", "Weekend", "Magic",
           "Wizards", "Banned", "Restricted", "Unbanned", "Changes", "Announcement", "Competitive"}


def http_text(url):
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (dashboard-collector)"})
    with urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def br_find_latest(today):
    """Страницы анонсов называются banned-and-restricted-<месяц>-<день>-<год>: перебираем дни назад."""
    def probe(d):
        try:
            return d, http_text(BR_URL.format(m=MONTHS_EN[d.month - 1], d=d.day, y=d.year))
        except Exception:
            return d, None
    for start in range(0, 150, 14):
        days = [today - timedelta(days=i) for i in range(start, start + 14)]
        with ThreadPoolExecutor(7) as ex:
            hits = [(d, h) for d, h in ex.map(probe, days) if h]
        if hits:
            return max(hits, key=lambda x: x[0])
    return None, None


def br_text_lines(html):
    html = re.sub(r"(?is)<(script|style|noscript).*?</\1>", " ", html)
    html = re.sub(r"(?i)</?(p|div|li|ul|ol|h[1-6]|br|tr|section|article)[^>]*>", "\n", html)
    html = re.sub(r"<[^>]+>", " ", html)
    return [re.sub(r"\s+", " ", unescape(l)).strip() for l in html.split("\n") if l.strip()]


def mtg_bans(card, tz):
    """Дата последнего анонса банов, карты из него, дата следующего."""
    today = datetime.now(tz).date()
    man = card.get("manual")
    if man:  # ручной запасной вариант из config.json
        return [man["date"]] + list(man.get("cards", [])) + (["Следующая: " + man["next"]] if man.get("next") else [])
    d, html = br_find_latest(today)
    if not d:
        raise RuntimeError("страница анонса не найдена")
    names = http_json("https://api.scryfall.com/catalog/card-names",
                      {"User-Agent": "DashNews/1.0 (github.com/SolTheCaptain/DashNews)",
                       "Accept": "application/json;q=0.9,*/*;q=0.8"}).get("data") or []
    known = {n.split(" // ")[0] for n in names}
    sets = http_json("https://api.scryfall.com/sets", {"User-Agent": "DashNews/1.0 (github.com/SolTheCaptain/DashNews)",
                                                       "Accept": "application/json;q=0.9,*/*;q=0.8"})
    set_names = " | ".join(x["name"].lower() for x in sets.get("data") or [])
    found, seen = [], set()
    fmt = ""
    active = 0
    for line in br_text_lines(html):
        kw = re.search(r"(?i)\b(un)?ban|restrict", line)
        if len(line) >= 200:
            active = 0
            continue
        fm = re.search(BR_FORMATS, line)
        if fm:
            fmt = fm.group(0)
        if kw:
            active = 20
        elif active and len(line) < 90:
            active -= 1
        else:
            active = 0
            continue
        w = line.split()
        i = 0
        while i < len(w):
            hit = 0
            for n in range(min(9, len(w) - i), 0, -1):
                cand = " ".join(w[i:i + n]).strip(".;:()\"“”")
                if cand in known and cand not in BR_SKIP and (n > 1 or len(cand) >= 6) \
                        and cand.lower() not in set_names:  # названия сетов бывают и названиями карт
                    hit = n
                    if (cand, fmt) not in seen:
                        seen.add((cand, fmt))
                        found.append((fmt, cand))
                    break
            i += hit or 1
    lines = [f"{d.day} {MONTHS[d.month - 1]} {d.year}"]
    groups = {}
    for f, n in found:  # карты по форматам: «Формат:» и под ним карты
        groups.setdefault(f, []).append(n)
    for f, ns in groups.items():
        lines += ([f + ":"] if f else []) + ["  " + n if f else n for n in ns]
    if not found:
        lines.append("(список карт не распознан)")
    m = re.search(r"(?is)next.{0,80}?([A-Z][a-z]+) (\d{1,2}), (\d{4})", " ".join(br_text_lines(html)))
    if m and m.group(1).lower() in MONTHS_EN:
        nd = date(int(m.group(3)), MONTHS_EN.index(m.group(1).lower()) + 1, int(m.group(2)))
        lines += [f"Следующая: {nd.day} {MONTHS[nd.month - 1]} {nd.year}"]
    return lines


# ---------- источник: Open-Meteo (погода, ключ не нужен) ----------

WMO = {
    0: "ясно", 1: "в основном ясно", 2: "переменная облачность", 3: "пасмурно",
    45: "туман", 48: "изморозь", 51: "морось", 53: "морось", 55: "сильная морось",
    56: "ледяная морось", 57: "ледяная морось", 61: "небольшой дождь", 63: "дождь",
    65: "сильный дождь", 66: "ледяной дождь", 67: "ледяной дождь", 71: "небольшой снег",
    73: "снег", 75: "сильный снег", 77: "снежные зёрна", 80: "ливень", 81: "ливень",
    82: "сильный ливень", 85: "снегопад", 86: "сильный снегопад", 95: "гроза",
    96: "гроза с градом", 99: "гроза с градом",
}


def signed(x):
    n = round(x)
    return f"{'+' if n > 0 else ''}{n}"


def weather(card, tz):
    url = (f"https://api.open-meteo.com/v1/forecast?latitude={card['lat']}&longitude={card['lon']}"
           "&current=temperature_2m,apparent_temperature,weather_code,wind_speed_10m"
           "&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code"
           f"&timezone={quote(tz.key)}&forecast_days=3&wind_speed_unit=ms")
    w = http_json(url)
    c, d = w["current"], w["daily"]
    lines = [f"Сейчас: {signed(c['temperature_2m'])}°, ощущается {signed(c['apparent_temperature'])}°, "
             f"{WMO.get(c['weather_code'], '—')}, ветер {round(c['wind_speed_10m'])} м/с"]
    names = ["Сегодня", "Завтра", "Послезавтра"]
    for i in range(len(d["time"])):
        lines.append(f"{names[i]}: {signed(d['temperature_2m_min'][i])}…{signed(d['temperature_2m_max'][i])}°, "
                     f"{WMO.get(d['weather_code'][i], '—')}, {d['precipitation_probability_max'][i]}%")
    return lines


# ---------- источник: ЦБ РФ и CoinGecko (котировки, ключи не нужны) ----------

def num(v, nd=2):
    return f"{v:,.{nd}f}".replace(",", "\xa0").replace(".", ",")


def pct(p):
    return f"{p:+.1f}".replace(".", ",") + "%"


def quotes(card, tz):
    """Курсы ЦБ к рублю (с изменением к прошлому курсу) и BTC/RUR."""
    cbr = http_json("https://www.cbr-xml-daily.ru/daily_json.js")
    lines = []
    for code in card.get("fiat", ["USD", "EUR"]):
        v = cbr["Valute"][code]
        val, prev = v["Value"] / v["Nominal"], v["Previous"] / v["Nominal"]
        lines.append(f"{code}/RUR — {num(val)} ({pct((val / prev - 1) * 100)})")
    try:
        btc = http_json("https://api.coingecko.com/api/v3/simple/price"
                        "?ids=bitcoin&vs_currencies=rub&include_24hr_change=true")["bitcoin"]
        lines.append(f"BTC/RUR — {num(btc['rub'], 0)} ({pct(btc['rub_24h_change'])})")
    except Exception as e:
        print(f"::warning::quotes BTC: {e}")
        lines.append("BTC/RUR — нет данных")
    return lines


# ---------- источник: Google Таблица с задачами (доступ «все, у кого есть ссылка») ----------

def sheet_tasks(card, tz):
    """Задачи из таблицы (колонки: Дата, Время, Контакт, Описание) на ближайшие N дней, включая сегодня.
    Год в дате игнорируется: событие считается ежегодным (дни рождения), берём ближайшее наступление дня и месяца."""
    sid = os.environ.get("TASKS_SHEET_ID")
    if not sid:
        raise RuntimeError("нет секрета TASKS_SHEET_ID")
    url = f"https://docs.google.com/spreadsheets/d/{quote(sid)}/export?format=csv&gid={card.get('gid', 0)}"
    with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0 (dashboard-collector)"}), timeout=30) as r:
        text = r.read().decode("utf-8-sig")
    if text.lstrip().startswith("<"):
        raise RuntimeError("таблица недоступна: проверь доступ «все, у кого есть ссылка»")
    today = datetime.now(tz).date()
    days = int(card.get("days", 10))
    dated, undated = [], []
    for row in csv.DictReader(io.StringIO(text)):
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        desc, contact = row.get("описание", ""), row.get("контакт", "")
        raw_d, raw_t = row.get("дата", ""), row.get("время", "").replace(".", ":")
        if not (desc or raw_d):
            continue
        d = None
        m = re.match(r"^(\d{1,2})\.(\d{1,2})\.?(?:\d{2,4})?\.?$", raw_d)
        if m:
            dd, mm = int(m.group(1)), int(m.group(2))
            for y in (today.year, today.year + 1):
                try:
                    d = date(y, mm, dd)
                except ValueError:
                    d = date(y, mm, 28) if (mm, dd) == (2, 29) else None
                if d and d >= today:
                    break
        t = None
        for fmt in ("%H:%M", "%H"):
            try:
                t = datetime.strptime(raw_t, fmt).time()
                break
            except ValueError:
                pass
        text_ = (desc or "(без описания)") + (f" ({contact})" if contact else "")
        if not d:
            undated.append(text_)
        elif today <= d < today + timedelta(days=days):
            dated.append((d, t or datetime.min.time(), f"{day_label(d)}{f', {t:%H:%M}' if t else ''} — {text_}"))
    dated.sort(key=lambda x: (x[0], x[1]))
    lines = [x[2] for x in dated] + [f"без даты — {u}" for u in undated]
    return lines or [f"В ближайшие {days} дн. задач нет."]


# ---------- реестр типов блоков ----------

HANDLERS = {
    "team_next": team_next,
    "teams_next": teams_next,
    "fd_table": fd_table,
    "fd_next": fd_next,
    "weather": weather,
    "quotes": quotes,
    "sheet_tasks": sheet_tasks,
    "slam_russians": slam_russians,
    "steam_upcoming": steam_upcoming,
    "mtg_roadmap": mtg_roadmap,
    "mtg_bans": mtg_bans,
}


# ---------- запуск ----------

def load_previous():
    """Прошлые данные нужны, чтобы при сбое источника показать последнее удачное."""
    prev = {}
    try:
        url = os.environ.get("PREV_URL")
        if url:
            data = http_json(url + "?t=" + str(int(datetime.now().timestamp())))
        else:
            with open(os.path.join(ROOT, "data.json"), encoding="utf-8") as f:
                data = json.load(f)
        for c in data.get("cards", []):
            prev[c["id"]] = c
    except Exception:
        pass
    return prev


def run_section(sec, tz, prev):
    """Один блок: возвращает словарь для data.json. Ошибка не роняет остальные."""
    out = {"id": sec["id"], "title": sec["title"]}
    days = sec.get("refresh_days")
    if days and prev and prev.get("ok") and prev.get("at"):
        try:
            if datetime.now(timezone.utc) - datetime.fromisoformat(prev["at"]) < timedelta(days=days):
                out.update(ok=True, lines=prev["lines"], at=prev["at"])
                print(f"cached   {sec['id']}")
                return out
        except Exception:
            pass
    try:
        fn = HANDLERS.get(sec.get("type"))
        if not fn:
            raise RuntimeError(f"неизвестный тип блока: {sec.get('type')}")
        res = fn(sec, tz)
        if isinstance(res, dict):
            out.update(ok=True, lines=res["lines"], matches=res.get("matches"))
        else:
            out.update(ok=True, lines=res)
        out["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        print(f"ok       {sec['id']}")
    except Exception as e:
        print(f"::warning::{sec['id']}: {e}")
        out.update(ok=False, error=str(e), lines=(prev or {}).get("lines") or [])
        if (prev or {}).get("matches"):
            out["matches"] = prev["matches"]
    return out


def run_group(card, tz, prev_card, now_iso):
    """Карточка с подблоками и одной общей пометкой «обновлено»."""
    prev_secs = {s["id"]: s for s in (prev_card or {}).get("sections", [])}
    secs = [run_group(s, tz, prev_secs.get(s["id"]), now_iso) if s.get("type") == "group"
            else run_section(s, tz, prev_secs.get(s["id"]))
            for s in card["sections"] if s.get("enabled", True) is not False]
    any_ok = any(s["ok"] for s in secs)
    return {"id": card["id"], "title": card["title"], "ok": all(s["ok"] for s in secs),
            "fetched": now_iso if any_ok else (prev_card or {}).get("fetched"), "sections": secs}


def main():
    with open(os.path.join(ROOT, "config.json"), encoding="utf-8") as f:
        config = json.load(f)
    tz = ZoneInfo(config.get("timezone", "Europe/Moscow"))
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")
    prev = load_previous()
    cards = []
    for card in config["cards"]:
        if card.get("enabled", True) is False:
            continue
        if card.get("type") == "group":
            cards.append(run_group(card, tz, prev.get(card["id"]), now_iso))
            continue
        out = run_section(card, tz, prev.get(card["id"]))
        out["fetched"] = now_iso if out["ok"] else (prev.get(card["id"]) or {}).get("fetched")
        cards.append(out)
    with open(os.path.join(ROOT, "data.json"), "w", encoding="utf-8") as f:
        json.dump({"updated": now_iso, "timezone": tz.key, "cards": cards}, f, ensure_ascii=False, indent=1)
    print(f"готово, карточек: {len(cards)}")


if __name__ == "__main__":
    main()

# КОНЕЦ ФАЙЛА collect.py
