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
    days = min(int(card.get("days", 9)), 10)  # у источника ограничение на диапазон дат
    now = datetime.now(timezone.utc)
    frm, to = now.strftime("%Y-%m-%d"), (now + timedelta(days=days)).strftime("%Y-%m-%d")
    j = fd_get(f"/competitions/{card['comp']}/matches?dateFrom={frm}&dateTo={to}")
    ok = {"SCHEDULED", "TIMED", "IN_PLAY", "PAUSED"}
    ms = sorted((m for m in (j.get("matches") or []) if m.get("status") in ok),
                key=lambda m: m["utcDate"])
    if not ms:
        return [f"В ближайшие {days} дней матчей нет."]
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


# ---------- реестр типов блоков ----------

HANDLERS = {
    "team_next": team_next,
    "fd_table": fd_table,
    "fd_next": fd_next,
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
        cid = card["id"]
        out = {"id": cid, "title": card["title"]}
        try:
            fn = HANDLERS.get(card.get("type"))
            if not fn:
                raise RuntimeError(f"неизвестный тип блока: {card.get('type')}")
            out.update(ok=True, lines=fn(card, tz), fetched=now_iso)
            print(f"ok       {cid}")
        except Exception as e:  # ошибка одного блока не должна ронять остальные
            print(f"::warning::{cid}: {e}")
            p = prev.get(cid) or {}
            out.update(ok=False, error=str(e), lines=p.get("lines") or [], fetched=p.get("fetched"))
        cards.append(out)
    with open(os.path.join(ROOT, "data.json"), "w", encoding="utf-8") as f:
        json.dump({"updated": now_iso, "timezone": tz.key, "cards": cards}, f, ensure_ascii=False, indent=1)
    print(f"готово: {sum(c['ok'] for c in cards)} из {len(cards)} блоков обновлено")


if __name__ == "__main__":
    main()
