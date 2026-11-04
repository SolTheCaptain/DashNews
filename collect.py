#!/usr/bin/env python3
"""Собирает данные для дашборда в data.json.

Читает config.json, для каждого блока вызывает обработчик по полю "type"
и пишет результат в data.json. Только стандартная библиотека Python.

Как добавить новый тип блока:
  1. написать функцию вида  def мой_блок(card, tz): ... return [строки]
  2. добавить её в словарь HANDLERS внизу файла
  3. использовать "type": "мой_блок" в config.json
"""
import base64
import csv
import hashlib
import hmac
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

_LAST_TSDB = [0.0]


def http_json(url, headers=None):
    """Бесплатный ключ TheSportsDB пускает ~30 запросов в минуту: держим паузу между ними и повторяем при 429."""
    import time
    from urllib.error import HTTPError
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (dashboard-collector)", **(headers or {})})
    for attempt in range(4):
        if "thesportsdb.com" in url:
            wait = 2.2 - (time.time() - _LAST_TSDB[0])
            if wait > 0:
                time.sleep(wait)
            _LAST_TSDB[0] = time.time()
        try:
            with urlopen(req, timeout=30) as r:
                text = r.read().decode("utf-8")
            return json.loads(text) if text.strip() else {}
        except HTTPError as e:
            if e.code != 429 or attempt == 3:
                raise
            time.sleep(10 * (attempt + 1))


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


_RANKS = {}
NOT_LEAGUE = re.compile(r"cup|champions|europa|conference|super|friendl|trophy|shield|copa|play-?off", re.I)
# чемпионаты, которые football-data.org отдаёт целиком (у TheSportsDB на бесплатном ключе в таблице только первая пятёрка)
FD_LEAGUES = {"english premier league": "PL", "spanish la liga": "PD", "italian serie a": "SA",
              "german bundesliga": "BL1", "french ligue 1": "FL1", "dutch eredivisie": "DED",
              "portuguese primeira liga": "PPL", "english league championship": "ELC"}


# чемпионаты, которых нет у football-data.org на бесплатном ключе: таблицу берём у ESPN
ESPN_LEAGUES = {"russian premier league": "rus.1", "russian football premier league": "rus.1"}


_ESPN = {}


def espn_json(slug):
    if slug not in _ESPN:
        _ESPN[slug] = http_json(f"https://site.api.espn.com/apis/v2/sports/soccer/{slug}/standings")
    return _ESPN[slug]


def espn_ranks(slug):
    j = espn_json(slug)
    out = {}
    for ch in j.get("children") or [j]:
        entries = (ch.get("standings") or {}).get("entries") or []
        for i, e in enumerate(entries, 1):
            st = {x.get("name"): x.get("value") for x in e.get("stats") or []}
            pos = int(st["rank"]) if st.get("rank") else i
            for nm in (e["team"].get("displayName"), e["team"].get("shortDisplayName"), e["team"].get("name")):
                if nm:
                    out[norm_team(nm)] = pos
    return out


def norm_team(s):
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower().replace("&", "and")
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = re.sub(r"\b(fc|cf|afc|sc|ac|as|ssc|rc|club|de|the|calcio|fk)\b", " ", s)
    s = re.sub(r"\b(moskva|moskow)\b", "moscow", s)
    s = re.sub(r"\b(dinamo|dynamo)\b", "dynamo", s)
    return re.sub(r"\s+", " ", s).strip()


def league_ranks(ev):
    """Места команд в таблице чемпионата: только для лиги, не для кубков и еврокубков. Нет данных — пусто."""
    lid, season, league = ev.get("idLeague"), ev.get("strSeason"), ev.get("strLeague") or ""
    if not lid or NOT_LEAGUE.search(league):
        return {}
    key = (lid, season)
    if key in _RANKS:
        return _RANKS[key]
    res = {"ids": {}, "names": {}}
    code = FD_LEAGUES.get(league.lower())
    try:
        if code and os.environ.get("FOOTBALL_DATA_KEY"):
            j = fd_get(f"/competitions/{code}/standings")
            st = next((x for x in j.get("standings") or [] if x.get("type") == "TOTAL"), None)
            for r in (st or {}).get("table") or []:
                for nm in (r["team"].get("name"), r["team"].get("shortName")):
                    if nm:
                        res["names"][norm_team(nm)] = r["position"]
        if not res["names"] and league.lower() in ESPN_LEAGUES:
            res["names"] = espn_ranks(ESPN_LEAGUES[league.lower()])
        if not res["names"] and season:
            rows = http_json(f"{TSDB}/lookuptable.php?l={lid}&s={season}").get("table") or []
            for r in rows:
                if r.get("intRank"):
                    res["ids"][str(r.get("idTeam"))] = int(r["intRank"])
                    res["names"][norm_team(r.get("strTeam"))] = int(r["intRank"])
    except Exception as e:
        print(f"::warning::таблица лиги {league}: {e}")
    _RANKS[key] = res
    return res


def rank_of(ranks, team_id, name):
    """Место команды: сначала по номеру TheSportsDB, потом по названию (в разных источниках оно пишется чуть по-разному)."""
    if not ranks:
        return None
    if team_id and str(team_id) in ranks["ids"]:
        return ranks["ids"][str(team_id)]
    n = norm_team(name)
    if not n:
        return None
    if n in ranks["names"]:
        return ranks["names"][n]
    for k2, pos in ranks["names"].items():
        if k2 and (k2 in n or n in k2) and min(len(k2), len(n)) >= 5:
            return pos
    return None


def league_table_rows(league):
    """Таблица чемпионата строками (место, команда, И, В, Н, П, разница, очки): football-data.org или ESPN."""
    code = FD_LEAGUES.get(league.lower())
    if code and os.environ.get("FOOTBALL_DATA_KEY"):
        sts = fd_get(f"/competitions/{code}/standings").get("standings") or []
        st = next((x for x in sts if x.get("type") == "TOTAL"), sts[0] if sts else None)
        return [(r["position"], r["team"].get("shortName") or r["team"]["name"], r["playedGames"], r["won"],
                 r["draw"], r["lost"], r["goalDifference"], r["points"]) for r in (st or {}).get("table") or []]
    slug = ESPN_LEAGUES.get(league.lower())
    if slug:
        j, rows = espn_json(slug), []
        for ch in j.get("children") or [j]:
            for i, e in enumerate((ch.get("standings") or {}).get("entries") or [], 1):
                st = {x.get("name"): x.get("value") for x in e.get("stats") or []}
                g = lambda k: int(st.get(k) or 0)
                rows.append((g("rank") or i, e["team"].get("shortDisplayName") or e["team"].get("displayName"),
                             g("gamesPlayed"), g("wins"), g("ties"), g("losses"), g("pointDifferential"), g("points")))
        return sorted(rows)
    return []


def round_block(rc, tz, head_id):
    """Тур чемпионата целиком (кроме ещё не сыгранного матча главной команды, он показан в карточке),
    сыгранные матчи — со счётом; плюс предыдущий тур и таблица. Места команд в скобках."""
    lid = str(rc["league_id"])
    evs = http_json(f"{TSDB}/eventsnextleague.php?id={lid}").get("events") or []
    evs = [e for e in evs if e.get("intRound")]
    if not evs:
        return None
    rnd = int(evs[0]["intRound"])
    season, league = evs[0].get("strSeason"), evs[0].get("strLeague") or rc.get("league", "")
    byid = {e["idEvent"]: e for e in evs if int(e["intRound"]) == rnd}
    try:  # весь тур, включая уже сыгранные матчи (в «ближайших» их нет)
        for e in http_json(f"{TSDB}/eventsround.php?id={lid}&r={rnd}&s={season}").get("events") or []:
            byid.setdefault(e["idEvent"], e)
    except Exception as e:
        print(f"::warning::тур {rnd}: {e}")
    ranks = league_ranks({"idLeague": lid, "strSeason": season, "strLeague": league})

    def when(e):
        if e.get("strTimestamp"):
            return parse_utc(e["strTimestamp"])
        if e.get("dateEvent"):
            return parse_utc(e["dateEvent"] + "T" + (e.get("strTime") or "00:00:00"))
        return None

    def nm(e, side):
        n = e.get(f"str{side}Team") or "?"
        r = rank_of(ranks, e.get(f"id{side}Team"), n)
        return f"{n} ({r})" if r else n

    def played(e):
        return e.get("intHomeScore") not in (None, "") and e.get("intAwayScore") not in (None, "")

    def listing(events, skip_unplayed_head):
        items = []
        for e in events:
            w = when(e)
            if not w or (skip_unplayed_head and not played(e)
                         and head_id in (str(e.get("idHomeTeam")), str(e.get("idAwayTeam")))):
                continue
            items.append((w, e))
        items.sort(key=lambda x: x[0])
        lines, cur = [], ""
        for w, e in items:
            loc = w.astimezone(tz)
            dl = day_label(loc).upper()
            if dl != cur:
                cur = dl
                lines += ([""] if lines else []) + [dl]
            head = f"{e['intHomeScore']}:{e['intAwayScore']}" if played(e) else f"{loc:%H:%M}"
            lines.append(f"  {head:<5}  {nm(e, 'Home')} — {nm(e, 'Away')}")
        return lines

    lines = listing(byid.values(), True)
    if not lines:
        return None
    out = {"title": f"Тур {rnd}", "lines": lines}
    if rnd > 1:
        prev = {}
        for url in (f"{TSDB}/eventspastleague.php?id={lid}", f"{TSDB}/eventsround.php?id={lid}&r={rnd - 1}&s={season}"):
            try:
                for e in http_json(url).get("events") or []:
                    if str(e.get("intRound")) == str(rnd - 1) and played(e):
                        prev.setdefault(e["idEvent"], e)
            except Exception as ex:
                print(f"::warning::тур {rnd - 1}: {ex}")
        pl = listing(prev.values(), False)
        if pl:
            out["prev"] = {"title": f"Тур {rnd - 1}", "lines": pl}
    try:
        rows = league_table_rows(league)
        if rows:
            out["table"] = {"title": "Таблица", "lines": table_lines(rows, [])}
    except Exception as ex:
        print(f"::warning::таблица {league}: {ex}")
    return out


def team_match(card, tz):
    """Ближайший матч команды: (строки текстом, словарь для карточки с эмблемами)."""
    name, country = card["team"], card.get("country")
    if card.get("team_id"):  # команда задана номером в TheSportsDB (хоккей и т. п.), поиск по имени не нужен
        team = {"idTeam": str(card["team_id"]), "strTeam": name}
    else:
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
    ranks = league_ranks(ev)
    match = {
        "own": {"name": ev.get(f"str{own_side}Team") or team.get("strTeam") or name,
                "badge": ev.get(f"str{own_side}TeamBadge") or badge_of(team),
                "rank": rank_of(ranks, ev.get(f"id{own_side}Team") or team["idTeam"],
                                ev.get(f"str{own_side}Team") or team.get("strTeam") or name)},
        "opp": {"name": ev.get(f"str{opp_side}Team") or "?",
                "badge": ev.get(f"str{opp_side}TeamBadge") or (team_badge(opp_id) if opp_id else None),
                "rank": rank_of(ranks, opp_id, ev.get(f"str{opp_side}Team"))},
        "home": own_home, "when": when_txt, "where": where or None,
    }
    if card.get("round"):
        try:
            rb = round_block(card["round"], tz, str(team["idTeam"]))
            if rb:
                match["round"] = rb
        except Exception as e:
            print(f"::warning::тур чемпионата {name}: {e}")
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
            tl, m = team_match({"team": t["name"], "country": t.get("country"), "team_id": t.get("id"), "round": t.get("round")}, tz)
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
    played = min(r[2] for r in data)  # сколько туров сыграно всеми командами, а не номер текущего тура
    top = max(r[2] for r in data)
    if top > played:
        lines.append(f"Идёт {top} тур")  # тур в разгаре: часть команд уже сыграла, часть ещё нет
    elif played:
        lines.append(f"После {played} тура")
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
    if first_only and ms:
        first_date = parse_utc(ms[0]["utcDate"]).astimezone(tz).date()
        ms = [m for m in ms if parse_utc(m["utcDate"]).astimezone(tz).date() == first_date]
    past = []  # матчи, начавшиеся вчера: показываем с итоговым счётом в правом столбце
    if card.get("yesterday_results"):
        try:
            yday = (datetime.now(tz) - timedelta(days=1)).date()
            frm, to = (now - timedelta(days=2)).strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d")
            for m in fd_get(f"/competitions/{card['comp']}/matches?dateFrom={frm}&dateTo={to}").get("matches") or []:
                ft = (m.get("score") or {}).get("fullTime") or {}
                if m.get("status") == "FINISHED" and ft.get("home") is not None \
                        and parse_utc(m["utcDate"]).astimezone(tz).date() == yday:
                    past.append(m)
        except Exception as e:
            print(f"::warning::результаты вчерашних матчей: {e}")
    if not ms and not past:
        return [f"В ближайшие {days} дней матчей нет."]
    lines, cur = [], ""
    for m in ms:
        loc = parse_utc(m["utcDate"]).astimezone(tz)
        dl = day_label(loc).upper()
        if dl != cur:
            cur = dl
            lines += ([""] if lines else []) + [dl]
        h, a = m["homeTeam"], m["awayTeam"]
        lines.append(f"  {loc:%H:%M}  {h.get('shortName') or h['name']} — {a.get('shortName') or a['name']}")
    if not lines:
        lines = ["Ближайших матчей в источнике нет."]
    if not past:
        return lines
    names = []  # результаты вчерашних матчей отдельным столбцом справа: «Хозяева — Гости  счёт»
    for m in sorted(past, key=lambda m: m["utcDate"]):
        ft = m["score"]["fullTime"]
        h, a = m["homeTeam"], m["awayTeam"]
        names.append((f"{h.get('shortName') or h['name']} — {a.get('shortName') or a['name']}", f"{ft['home']}:{ft['away']}"))
    width = max(len(t) for t, _ in names)
    res = ["РЕЗУЛЬТАТЫ", day_label(parse_utc(past[0]["utcDate"]).astimezone(tz)).upper()]
    res += [t.ljust(width) + " " + sc for t, sc in names]
    return {"lines": lines, "results": res}


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
    games, seen = [], set()  # игра: (название, дата, номер в Steam)
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
                games.append((unescape(t.group(1).strip()), d, appid.split(",")[0]))
        if len(games) >= n:
            break
    if not games:
        return ["В списке «Популярные ожидаемые» нет игр с точной датой до конца года."]
    lines = [f"Топ-{n} ожидаемых новинок до {end.day} {MONTHS[end.month - 1]}"]
    links = {}
    for i, (name, d, appid) in enumerate(games[:n], 1):
        links[str(len(lines))] = f"https://store.steampowered.com/app/{appid}/"  # номер строки → страница игры
        lines.append(f"{i}. {name} — {d.day} {MONTHS[d.month - 1]}")
    return {"lines": lines, "links": links}


# ---------- источник: Scryfall (сеты Magic, ключ не нужен) ----------

def mtg_roadmap(card, tz):
    """Основные бумажные сеты Magic от сегодня: до конца года или `count` ближайших; без даты — TBA."""
    today = datetime.now(tz).date()
    count = card.get("count")
    end = date(today.year + 1, 12, 31) if count else date(today.year, 12, 31)
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
    return lines[:count] if count else lines


# ---------- источник: Wizards (дата следующего анонса банов) ----------

MONTHS_EN = ["january", "february", "march", "april", "may", "june", "july",
             "august", "september", "october", "november", "december"]
BR_URL = "https://magic.wizards.com/en/news/announcements/banned-and-restricted-{m}-{d}-{y}"


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
    """Дата следующего анонса банов: в последнем анонсе Wizards есть фраза «next ... <месяц> <день>, <год>»."""
    man = card.get("manual")
    if man:  # ручной запасной вариант из config.json
        return ["Следующие: " + man["next"]] if man.get("next") else ["Дата следующего анонса пока неизвестна."]
    d, html = br_find_latest(datetime.now(tz).date())
    if not d:
        raise RuntimeError("страница анонса не найдена")
    m = re.search(r"(?is)next.{0,80}?([A-Z][a-z]+) (\d{1,2}), (\d{4})", " ".join(br_text_lines(html)))
    if m and m.group(1).lower() in MONTHS_EN:
        nd = date(int(m.group(3)), MONTHS_EN.index(m.group(1).lower()) + 1, int(m.group(2)))
        return [f"Следующие: {nd.day} {MONTHS[nd.month - 1]} {nd.year}"]
    return ["Дата следующего анонса пока неизвестна."]


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
           f"&timezone={quote(tz.key)}&forecast_days={int(card.get('days', 10))}&wind_speed_unit=ms")
    w = http_json(url)
    c, d = w["current"], w["daily"]
    lines = [f"Сейчас: {signed(c['temperature_2m'])}°, ощущается {signed(c['apparent_temperature'])}°, "
             f"{WMO.get(c['weather_code'], '—')}, ветер {round(c['wind_speed_10m'])} м/с"]
    names = ["Сегодня", "Завтра", "Послезавтра"]
    for i in range(min(3, len(d["time"]))):
        lines.append(f"{names[i]}: {signed(d['temperature_2m_min'][i])}…{signed(d['temperature_2m_max'][i])}°, "
                     f"{WMO.get(d['weather_code'][i], '—')}, {d['precipitation_probability_max'][i]}%")
    pts = [{"d": d["time"][i], "hi": d["temperature_2m_max"][i], "lo": d["temperature_2m_min"][i]}
           for i in range(len(d["time"]))]
    return {"lines": lines, "charts": {"weather": {"kind": "range", "title": f"Прогноз · {len(pts)} дн.", "points": pts}}}


# ---------- источник: ЦБ РФ и CoinGecko (котировки, ключи не нужны) ----------

def num(v, nd=2):
    return f"{v:,.{nd}f}".replace(",", "\xa0").replace(".", ",")


CBR_IDS = {"USD": "R01235", "EUR": "R01239"}


def cbr_history(code, days, tz):
    """Официальный курс ЦБ за последние дни: [{d: 'ГГГГ-ММ-ДД', v: число}], только рабочие дни."""
    import xml.etree.ElementTree as ET
    end = datetime.now(tz).date() + timedelta(days=1)
    start = end - timedelta(days=days + 1)
    url = ("https://www.cbr.ru/scripts/XML_dynamic.asp?date_req1=" + start.strftime("%d/%m/%Y") +
           "&date_req2=" + end.strftime("%d/%m/%Y") + "&VAL_NM_RQ=" + CBR_IDS[code])
    with urlopen(Request(url, headers={"User-Agent": "Mozilla/5.0 (dashboard-collector)"}), timeout=30) as r:
        root = ET.fromstring(r.read())
    out = []
    for rec in root.iter("Record"):
        d = datetime.strptime(rec.get("Date"), "%d.%m.%Y").date()
        v = float(rec.find("Value").text.replace(",", ".")) / int(rec.find("Nominal").text)
        if d <= datetime.now(tz).date():  # завтрашний курс ЦБ публикует заранее, на графике он ни к чему
            out.append({"d": d.isoformat(), "v": round(v, 4)})
    return sorted(out, key=lambda x: x["d"])


def btc_history(days, tz):
    """BTC/USD по дням (последняя цена за сутки) из CoinGecko."""
    j = http_json(f"https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days={days + 1}")
    by_day = {}
    for ts, price in j.get("prices") or []:
        d = datetime.fromtimestamp(ts / 1000, tz).date()
        by_day[d] = price
    return [{"d": d.isoformat(), "v": round(v)} for d, v in sorted(by_day.items())][-(days + 1):]


YAHOO = {"USD": "RUB=X", "EUR": "EURRUB=X", "BTC": "BTC-USD"}


def intraday(code, tz, step=15):
    """Рыночный курс за последние сутки торгов с шагом step минут: [{t: ISO-время, v: число}].
    USD и EUR — Yahoo Finance, BTC — CoinGecko (запасной вариант — Yahoo)."""
    raw = []
    if code == "BTC":
        try:
            j = http_json("https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days=1")
            raw = [(datetime.fromtimestamp(ts / 1000, timezone.utc), p) for ts, p in j.get("prices") or []]
        except Exception as e:
            print(f"::warning::quotes day BTC (CoinGecko): {e}")
    if not raw:
        j = http_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(YAHOO[code])}?range=2d&interval={step}m")
        r = ((j.get("chart") or {}).get("result") or [{}])[0]
        closes = (((r.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
        raw = [(datetime.fromtimestamp(ts, timezone.utc), v) for ts, v in zip(r.get("timestamp") or [], closes) if v]
    if not raw:
        return []
    last = max(t for t, _ in raw)  # в выходные валюта не торгуется: берём последние сутки торгов, а не сутки от «сейчас»
    buckets = {}  # последняя цена в каждом интервале step минут
    for t, v in raw:
        if last - t <= timedelta(hours=24):
            buckets[int(t.timestamp()) // (step * 60)] = (t, v)
    nd = 0 if code == "BTC" else 4
    return [{"t": t.isoformat(timespec="minutes"), "v": round(v, nd)} for _, (t, v) in sorted(buckets.items())]


def market_daily(code, days, tz):
    """Рыночный курс по дням (закрытие дня) из Yahoo Finance: [{d, v}] за последние days+1 дней."""
    j = http_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{quote(YAHOO[code])}?range=1mo&interval=1d")
    r = ((j.get("chart") or {}).get("result") or [{}])[0]
    closes = (((r.get("indicators") or {}).get("quote") or [{}])[0]).get("close") or []
    by_day = {}
    for ts, v in zip(r.get("timestamp") or [], closes):
        if v:
            by_day[datetime.fromtimestamp(ts, tz).date()] = v
    return [{"d": d.isoformat(), "v": round(v, 4)} for d, v in sorted(by_day.items())][-(days + 1):]


def quotes(card, tz):
    """Рыночные курсы USD и EUR к рублю и BTC к доллару — как в Google (Yahoo Finance, BTC — CoinGecko).
    Графики по каждой паре: неделя (закрытие дня) и сутки (каждые 15 минут).
    Если рыночный источник не ответил: курс ЦБ (BTC — CoinGecko)."""
    ch = card.get("chart") or {}
    days, step = int(ch.get("days", 7)), int(ch.get("step", 15))
    fiat = card.get("fiat", ["USD", "EUR"])
    charts = {}  # сбой одного графика остальное не ломает
    for code in ch.get("codes", fiat + ["BTC"]):
        pair, cur = f"{code}/{'USD' if code == 'BTC' else 'RUR'}", "$" if code == "BTC" else "₽"
        got = {}
        for rng, fn, title in (
                ("week", lambda: btc_history(days, tz) if code == "BTC" else market_daily(code, days, tz), f"{days} дн."),
                ("day", lambda: intraday(code, tz, step), "сутки")):
            try:
                pts = fn()
            except Exception as e:
                print(f"::warning::quotes {rng} {code}: {e}")
                pts = []
            if rng == "week" and len(pts) < 2 and code != "BTC":
                try:  # запасной вариант недели — курс ЦБ
                    pts = cbr_history(code, days, tz)
                except Exception as e:
                    print(f"::warning::quotes week {code} (ЦБ): {e}")
            if len(pts) >= 2:
                got[rng] = {"title": f"{pair} · {title}", "points": pts, "cur": cur}
        if got:
            charts[code] = got

    def now_rate(code):  # текущий курс = последняя точка суточного графика
        p = (charts.get(code, {}).get("day") or {}).get("points") or []
        return p[-1]["v"] if p else None

    lines, cbr = [], None
    for code in fiat:
        v = now_rate(code)
        if v is None:
            try:
                cbr = cbr or http_json("https://www.cbr-xml-daily.ru/daily_json.js")
                x = cbr["Valute"][code]
                v = x["Value"] / x["Nominal"]
            except Exception as e:
                print(f"::warning::quotes {code}: {e}")
        lines.append(f"{code}/RUR — {num(v) if v is not None else 'нет данных'}")
    v = now_rate("BTC")
    if v is None:
        try:
            v = http_json("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd")["bitcoin"]["usd"]
        except Exception as e:
            print(f"::warning::quotes BTC: {e}")
    lines.append(f"BTC/USD — {num(v, 0) if v is not None else 'нет данных'}")
    return {"lines": lines, "charts": charts} if charts else lines


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


# ---------- шифрование ----------

# data.json шифруется паролем из секрета DASH_PASSWORD; страница расшифровывает его в браузере.
# Ключ: PBKDF2-SHA256 (пароль, постоянная соль, KDF_ITER) → 64 байта: 32 на шифр, 32 на подпись.
# Шифр: поток HMAC-SHA256(ключ, nonce + номер блока) XOR данные; подпись: HMAC-SHA256(nonce + шифротекст).
KDF_SALT, KDF_ITER = b"DashNews", 200000


def keys(password):
    k = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), KDF_SALT, KDF_ITER, 64)
    return k[:32], k[32:]


def stream(key, nonce, n):
    out = bytearray()
    for i in range((n + 31) // 32):
        out += hmac.new(key, nonce + i.to_bytes(4, "big"), hashlib.sha256).digest()
    return bytes(out[:n])


def encrypt(obj, password):
    ek, mk = keys(password)
    pt = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    nonce = os.urandom(16)
    ct = bytes(a ^ b for a, b in zip(pt, stream(ek, nonce, len(pt))))
    tag = hmac.new(mk, nonce + ct, hashlib.sha256).digest()
    b64 = lambda b: base64.b64encode(b).decode()
    return {"enc": 1, "iter": KDF_ITER, "nonce": b64(nonce), "ct": b64(ct), "tag": b64(tag)}


def decrypt(box, password):
    ek, mk = keys(password)
    nonce, ct, tag = (base64.b64decode(box[k]) for k in ("nonce", "ct", "tag"))
    if not hmac.compare_digest(hmac.new(mk, nonce + ct, hashlib.sha256).digest(), tag):
        raise ValueError("неверный пароль")
    return json.loads(bytes(a ^ b for a, b in zip(ct, stream(ek, nonce, len(ct)))))


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
        if data.get("enc"):
            data = decrypt(data, os.environ.get("DASH_PASSWORD", ""))
        for c in data.get("cards", []):
            prev[c["id"]] = c
    except Exception:
        pass  # прошлых данных нет или пароль сменился: соберём с нуля
    return prev


def run_section(sec, tz, prev):
    """Один блок: возвращает словарь для data.json. Ошибка не роняет остальные."""
    out = {"id": sec["id"], "title": sec["title"]}
    days = sec.get("refresh_days")
    if days and prev and prev.get("ok") and prev.get("at") and prev.get("rev") == sec.get("rev"):
        try:
            if datetime.now(timezone.utc) - datetime.fromisoformat(prev["at"]) < timedelta(days=days):
                out.update(ok=True, lines=prev["lines"], at=prev["at"], rev=sec.get("rev"))
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
            out.update(ok=True, lines=res["lines"], matches=res.get("matches"), charts=res.get("charts"),
                       links=res.get("links"), results=res.get("results"))
        else:
            out.update(ok=True, lines=res)
        out["at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
        out["rev"] = sec.get("rev")
        print(f"ok       {sec['id']}")
    except Exception as e:
        print(f"::warning::{sec['id']}: {e}")
        out.update(ok=False, error=str(e), lines=(prev or {}).get("lines") or [])
        if (prev or {}).get("matches"):
            out["matches"] = prev["matches"]
        if (prev or {}).get("charts"):
            out["charts"] = prev["charts"]
        if (prev or {}).get("links"):
            out["links"] = prev["links"]
        if (prev or {}).get("results"):
            out["results"] = prev["results"]
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
    data = {"updated": now_iso, "timezone": tz.key, "cards": cards}
    pw = os.environ.get("DASH_PASSWORD")
    if pw:
        data = encrypt(data, pw)
    else:
        print("::warning::нет секрета DASH_PASSWORD: data.json публикуется без шифрования")
    with open(os.path.join(ROOT, "data.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(f"готово, карточек: {len(cards)}")


if __name__ == "__main__":
    main()

# КОНЕЦ ФАЙЛА collect.py
