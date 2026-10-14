#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Живые игроки: итоги, карточка игрока, рекорды, страны.

Игрок — это IP-адрес: ник может совпасть у разных людей (например, «UnnamedPlayer»
у всех, кто не сменил имя), а один человек может сменить ник. Поэтому всё считается
по адресу, а ники, с которыми заходили с этого адреса, перечисляются.

У Quake адрес видно только в ответе `status`, в логах его нет. Панель записывает
адреса с момента своего запуска; более ранние заходы (восстановленные по игровому
логу) адреса не имеют — они сгруппированы по нику (ключ «~ник») и помечены. Объединять их с адресами по нику нельзя: так
склеились бы разные люди с одинаковым ником.
"""


def ip_of(address):
    """Адрес без порта («1.2.3.4:27960» -> «1.2.3.4»); бот и пустое — ''."""
    address = (address or "").strip()
    if not address or address.lower().startswith("bot"):
        return ""
    if address.count(":") == 1:
        address = address.split(":")[0]
    return address


def key_of(s):
    """Кто это: IP-адрес, а без адреса — ник с тильдой."""
    return ip_of(s.get("address")) or "~" + s["name"]


def kd(kills, deaths):
    return round(kills / deaths, 2) if deaths else float(kills)


def _end(s):
    return s["left_at"] or s["last_seen"]


def summarize(sessions, geo):
    """Итоги по каждому игроку: ники (первый — последний по времени), заходы, время, У/С."""
    by = {}
    for s in sessions:
        k = key_of(s)
        ip = ip_of(s.get("address"))
        p = by.get(k)
        if p is None:
            p = by[k] = {"key": k, "ip": ip or None, "name": s["name"], "names": [], "visits": 0,
                         "seconds": 0, "kills": 0, "deaths": 0, "first": s["joined_at"],
                         "last": s["joined_at"], "online": False, "approx": False}
        if s["name"] not in p["names"]:
            p["names"].append(s["name"])
        p["visits"] += 1
        p["seconds"] += max(0, s["duration"])
        p["kills"] += s["kills"]
        p["deaths"] += s["deaths"]
        p["first"] = min(p["first"], s["joined_at"])
        p["last"] = max(p["last"], _end(s))
        p["online"] = p["online"] or s["online"]
        p["approx"] = p["approx"] or bool(s.get("approx"))
    out = list(by.values())
    for p in out:
        p["country"] = geo.country(p["ip"]) if p["ip"] else None
        p["kd"] = kd(p["kills"], p["deaths"])
    out.sort(key=lambda p: (-p["seconds"], -p["visits"]))
    return out


def countries(players):
    """Сколько игроков (адресов) и времени из каждой страны. Без адреса — не считаем."""
    by = {}
    for p in players:
        if not p["ip"]:
            continue
        c = p["country"] or {"code": "?", "name": "не определить"}
        row = by.setdefault(c["code"], {"code": c["code"], "title": c["name"], "name": c["name"],
                                        "names": c.get("names") or {}, "players": 0, "seconds": 0})
        row["players"] += 1
        row["seconds"] += p["seconds"]
    return sorted(by.values(), key=lambda r: (-r["players"], -r["seconds"]))


def records(sessions, players):
    """Рекорды живых игроков: [{title, name, key, value, at}]."""
    out = []
    if not sessions:
        return out

    def rec(title, s, value, at=None):
        out.append({"title": title, "name": s["name"], "key": key_of(s), "value": value,
                    "at": s["joined_at"] if at is None else at, "approx": bool(s.get("approx"))})

    best = max(sessions, key=lambda s: (s["kills"], -s["joined_at"]))
    if best["kills"]:
        rec("Больше всего убийств за заход", best, "%d" % best["kills"])
    longest = max(sessions, key=lambda s: s["duration"])
    rec("Самый долгий заход", longest, {"duration": longest["duration"]})
    good = [s for s in sessions if s["kills"] >= 10]
    if good:
        s = max(good, key=lambda s: kd(s["kills"], s["deaths"]))
        rec("Лучший У/С за заход (от 10 убийств)", s, "%g" % kd(s["kills"], s["deaths"]))
    if players:
        p = max(players, key=lambda p: (p["visits"], p["seconds"]))
        out.append({"title": "Чаще всех заходит", "name": p["name"], "key": p["key"],
                    "value": {"times": p["visits"]}, "at": None, "approx": False})
    # больше всего людей одновременно
    events = []
    for s in sessions:
        events.append((s["joined_at"], 1))
        events.append((_end(s), -1))
    events.sort(key=lambda e: (e[0], e[1]))
    cur = peak = 0
    peak_at = None
    for ts, d in events:
        cur += d
        if cur > peak:
            peak, peak_at = cur, ts
    if peak:
        out.append({"title": "Больше всего людей сразу", "name": None, "key": None,
                    "value": "%d" % peak, "at": peak_at, "approx": False})
    return out


def card(key, sessions, geo, weapons, players=(), modes=(), maps_log=()):
    """Карточка игрока по ключу (IP или «~ник»): всё про его заходы.
    weapons, modes, maps_log — оружие, режимы и карты его ников из статистики
    игрового лога (stats.weapons_by_names, stats.breakdown_by_names); players —
    итоги по всем игрокам (summarize), чтобы назвать место по времени в игре."""
    mine = [s for s in sessions if key_of(s) == key]
    if not mine:
        return None
    names = []
    for s in mine:
        if s["name"] not in names:
            names.append(s["name"])
    ip = ip_of(mine[0].get("address")) or None
    seconds = sum(max(0, s["duration"]) for s in mine)
    kills = sum(s["kills"] for s in mine)
    deaths = sum(s["deaths"] for s in mine)
    maps = {}
    for s in mine:
        for m in s["maps"]:
            maps[m] = maps.get(m, 0) + 1
    best = max(mine, key=lambda s: (s["kills"], -s["joined_at"]))
    longest = max(mine, key=lambda s: s["duration"])
    # те же ники, но под другим ключом — чтобы не перепутать людей
    others = sorted({key_of(s) for s in sessions if s["name"] in names and key_of(s) != key})
    # убийств в час — только по заходам, где убийства и смерти известны (нашлись
    # матчи в игровом логе), иначе пустые заходы занижали бы счёт
    fought = [s for s in mine if s["kills"] or s["deaths"]]
    fought_sec = sum(max(0, s["duration"]) for s in fought)
    kph = round(sum(s["kills"] for s in fought) * 3600 / fought_sec, 1) if fought_sec >= 60 else None
    rank = next(({"pos": i + 1, "of": len(players)} for i, p in enumerate(players) if p["key"] == key), None)
    # где играет лучше: У/С по картам, где набралось хотя бы 10 убийств и смертей
    map_kd = [dict(r, kd=kd(r["kills"] or 0, r["deaths"] or 0)) for r in maps_log
              if (r["kills"] or 0) + (r["deaths"] or 0) >= 10]
    map_kd.sort(key=lambda r: (-r["kd"], -r["kills"]))
    return {
        "key": key, "ip": ip, "name": names[0], "names": names,
        "country": geo.country(ip) if ip else None,
        "online": any(s["online"] for s in mine),
        "approx": any(s.get("approx") for s in mine),
        "visits": len(mine), "seconds": seconds, "avg": seconds // len(mine),
        "first": min(s["joined_at"] for s in mine),
        "last": max(_end(s) for s in mine),
        "kills": kills, "deaths": deaths, "kd": kd(kills, deaths),
        "best": {"kills": best["kills"], "deaths": best["deaths"], "at": best["joined_at"]},
        "longest": {"duration": longest["duration"], "at": longest["joined_at"]},
        "maps": sorted(({"map": m, "visits": n} for m, n in maps.items()), key=lambda r: -r["visits"])[:6],
        "joins": [s["joined_at"] for s in mine],
        "recent": [{"name": s["name"], "joined_at": s["joined_at"], "duration": s["duration"],
                    "kills": s["kills"], "deaths": s["deaths"], "online": s["online"], "maps": s["maps"],
                    "approx": bool(s.get("approx"))}
                   for s in mine[:8]],
        "weapons": weapons[:6],
        "same_names_from": others,
        # [начало, длительность] всех заходов: дни на сервере и дни недели браузер
        # считает сам — в своём часовом поясе
        "spans": [[s["joined_at"], max(0, s["duration"])] for s in mine],
        "kph": kph,
        "rank": rank,
        "modes": [dict(r, playtime=r["playtime"] or 0, kills=r["kills"] or 0, deaths=r["deaths"] or 0)
                  for r in modes],
        "map_kd": map_kd[:6],
    }
