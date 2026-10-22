#!/usr/bin/env python3
"""Собирает данные для дашборда в data.json.

Читает config.json, для каждого блока вызывает обработчик по полю "type"
и пишет результат в data.json. Только стандартная библиотека Python.

Как добавить новый тип блока:
  1. написать функцию вида  def мой_блок(card, tz): ... return [строки]
  2. добавить её в словарь HANDLERS внизу файла
  3. использовать "type": "мой_блок" в config.json
"""
import json
import os
from datetime import datetime, timedelta, timezone
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

def team_next(card, tz):
    name, country = card["team"], card.get("country")
    found = http_json(f"{TSDB}/searchteams.php?t={quote(name)}")
    teams = [t for t in (found.get("teams") or []) if t.get("strSport") == "Soccer"]
    team = next((t for t in teams if not country or t.get("strCountry") == country),
                teams[0] if teams else None)
    if not team:
        raise RuntimeError(f"команда не найдена: {name}")
    events = http_json(f"{TSDB}/eventsnext.php?id={team['idTeam']}").get("events") or []
    if not events:
        return ["Ближайших матчей в источнике нет."]

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
    if w:
        loc = w.astimezone(tz)
        lines.append(f"{day_label(loc)}, {loc:%H:%M} ({tz.key})")
    lines.append("Турнир: " + (ev.get("strLeague") or "—"))
    if ev.get("strVenue"):
        lines.append("Стадион: " + ev["strVenue"])
    return lines


def teams_next(card, tz):
    """Несколько команд подряд: ближайший матч каждой."""
    lines, fails = [], 0
    for t in card["teams"]:
        label = t.get("label") or t["name"]
        if lines:
            lines.append("")
        lines.append(label)
        try:
            lines += ["  " + x for x in team_next({"team": t["name"], "country": t.get("country")}, tz)]
        except Exception as e:
            fails += 1
            print(f"::warning::{label}: {e}")
            lines.append(f"  не удалось получить данные ({e})")
    if fails == len(card["teams"]):
        raise RuntimeError("не удалось получить данные ни по одной команде")
    return lines


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
    lines = ["(первая команда — хозяева, вторая — гости)"]
    cur = ""
    for m in ms:
        loc = parse_utc(m["utcDate"]).astimezone(tz)
        dl = day_label(loc)
        if dl != cur:
            cur = dl
            lines += ["", dl.upper()]
        h, a = m["homeTeam"], m["awayTeam"]
        lines.append(f"  {loc:%H:%M}  {h.get('shortName') or h['name']} — {a.get('shortName') or a['name']}")
    lines += ["", f"время по {tz.key}"]
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


# ---------- реестр типов блоков ----------

HANDLERS = {
    "team_next": team_next,
    "teams_next": teams_next,
    "fd_table": fd_table,
    "fd_next": fd_next,
    "weather": weather,
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
    try:
        fn = HANDLERS.get(sec.get("type"))
        if not fn:
            raise RuntimeError(f"неизвестный тип блока: {sec.get('type')}")
        out.update(ok=True, lines=fn(sec, tz))
        print(f"ok       {sec['id']}")
    except Exception as e:
        print(f"::warning::{sec['id']}: {e}")
        out.update(ok=False, error=str(e), lines=(prev or {}).get("lines") or [])
    return out


def run_group(card, tz, prev_card, now_iso):
    """Карточка с подблоками и одной общей пометкой «обновлено»."""
    prev_secs = {s["id"]: s for s in (prev_card or {}).get("sections", [])}
    secs = [run_section(s, tz, prev_secs.get(s["id"]))
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
