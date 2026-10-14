#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Заходы живых игроков с датой и временем.

В games.log дат нет, а сам лог сервер пишет с задержкой, кусками по несколько
килобайт (g_logSync 0). Поэтому время захода берётся не из лога: панель раз в
20 секунд спрашивает сервер «кто на тебе» (getstatus — тот же запрос, что у
браузера серверов, в логи он не пишется) и отмечает, кто появился и кто ушёл.
Точность — до одного опроса. Работает с момента первого запуска панели.

Живой игрок отличается от бота пингом: у ботов он всегда 0.

IP-адрес берётся из ответа rcon `status` — только когда пришёл живой игрок без
записанного адреса, чтобы не сорить строками «Rcon from …» в логе сервера зря.
"""

import sqlite3
import time

SCHEMA = """
CREATE TABLE IF NOT EXISTS player_sessions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    joined_at  INTEGER NOT NULL,
    last_seen  INTEGER NOT NULL,
    left_at    INTEGER,
    maps       TEXT NOT NULL DEFAULT '',
    best_score INTEGER NOT NULL DEFAULT 0,
    best_ping  INTEGER
);
CREATE INDEX IF NOT EXISTS ps_joined ON player_sessions(joined_at DESC);
CREATE INDEX IF NOT EXISTS ps_open ON player_sessions(left_at);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
-- начала матчей, которые панель видела сама: точные опорные точки для дат
-- матчей. Нужны потому, что цепочка «запуск сервера + длительности» рвётся на
-- каждой обрезке игрового лога (теряется матч), а запуск может давно уйти из лога.
CREATE TABLE IF NOT EXISTS map_starts (
    id  INTEGER PRIMARY KEY AUTOINCREMENT,
    map TEXT NOT NULL,
    at  INTEGER NOT NULL
);
"""

# Сервер молчит дольше этого — считаем, что все ушли (завис, перезапуск).
LOST_AFTER = 180


class Presence:
    def __init__(self, db_path):
        self.db_path = db_path
        self._last_map = None    # что шло при прошлом опросе — чтобы заметить смену карты
        self._last_score = 0
        with self._db() as db:
            db.executescript(SCHEMA)
            # панель перезапускалась: незакрытые заходы закрываем последним
            # моментом, когда игрок точно был на сервере
            have = {r["name"] for r in db.execute("PRAGMA table_info(player_sessions)")}
            if "address" not in have:
                db.execute("ALTER TABLE player_sessions ADD COLUMN address TEXT")
            db.execute("UPDATE player_sessions SET left_at = last_seen WHERE left_at IS NULL")
            # с какого момента ведётся учёт — чтобы честно подписать пустой календарь
            db.execute("INSERT OR IGNORE INTO meta(key, value) VALUES ('presence_since', ?)",
                       (str(int(time.time())),))

    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    def tick(self, players, mapname, now=None, addresses=None):
        """Один опрос. players — список из getstatus или None, если сервер молчит.
        addresses — {ник: IP} из rcon status, если удалось спросить."""
        now = int(now or time.time())
        addresses = addresses or {}
        with self._db() as db:
            open_rows = {r["name"]: dict(r) for r in db.execute(
                "SELECT id, name, last_seen, maps, address FROM player_sessions WHERE left_at IS NULL")}

            if players is None:
                for r in open_rows.values():
                    if now - r["last_seen"] > LOST_AFTER:
                        db.execute("UPDATE player_sessions SET left_at=? WHERE id=?",
                                   (r["last_seen"], r["id"]))
                return

            humans = {}
            for p in players:
                if p.get("ping", 0) > 0 and p.get("name"):
                    humans[p["name"]] = p

            for name, p in humans.items():
                row = open_rows.get(name)
                if row is None:
                    db.execute(
                        "INSERT INTO player_sessions(name, joined_at, last_seen, maps, best_score, best_ping,"
                        " address) VALUES (?,?,?,?,?,?,?)",
                        (name, now, now, mapname or "", p.get("score", 0), p.get("ping"),
                         addresses.get(name)),
                    )
                    continue
                maps = row["maps"].split(",") if row["maps"] else []
                if mapname and mapname not in maps:
                    maps.append(mapname)
                db.execute(
                    "UPDATE player_sessions SET last_seen=?, maps=?,"
                    " best_score=MAX(best_score, ?), best_ping=MIN(COALESCE(best_ping, ?), ?),"
                    " address=COALESCE(address, ?) WHERE id=?",
                    (now, ",".join(maps), p.get("score", 0), p.get("ping"), p.get("ping"),
                     addresses.get(name), row["id"]),
                )

            for name, row in open_rows.items():
                if name not in humans:
                    db.execute("UPDATE player_sessions SET left_at=? WHERE id=?", (now, row["id"]))

    def sessions(self, since=None, limit=2000):
        """Заходы от новых к старым. left_at пуст — игрок ещё на сервере."""
        sql = ("SELECT id, name, joined_at, last_seen, left_at, maps, best_score, best_ping, address"
               " FROM player_sessions")
        args = []
        if since:
            sql += " WHERE joined_at >= ?"
            args.append(int(since))
        sql += " ORDER BY joined_at DESC LIMIT ?"
        args.append(limit)
        with self._db() as db:
            rows = [dict(r) for r in db.execute(sql, args)]
        for r in rows:
            r["online"] = r["left_at"] is None
            r["duration"] = (r["left_at"] or r["last_seen"]) - r["joined_at"]
            r["maps"] = [m for m in r["maps"].split(",") if m]
        return rows

    def observe_map(self, mapname, players, now=None):
        """Замечает начало нового матча: сменилась карта или та же карта началась
        заново (счёт всех обнулился). Время начала — середина интервала опроса."""
        now = int(now or time.time())
        if not mapname:
            return
        score = sum(max(0, p.get("score", 0)) for p in (players or []))
        started = (self._last_map is not None and mapname != self._last_map) or \
                  (mapname == self._last_map and self._last_score >= 8 and score <= 1)
        if started:
            with self._db() as db:
                db.execute("INSERT INTO map_starts(map, at) VALUES (?, ?)", (mapname, now - 10))
        self._last_map, self._last_score = mapname, score

    def map_starts(self):
        """[(карта, момент начала)] от старых к новым."""
        with self._db() as db:
            return [(r["map"], r["at"]) for r in db.execute("SELECT map, at FROM map_starts ORDER BY at")]

    def needs_addresses(self, players):
        """Есть ли на сервере живой игрок, чей адрес ещё не записан, — тогда стоит спросить status."""
        humans = {p["name"] for p in (players or []) if p.get("ping", 0) > 0 and p.get("name")}
        if not humans:
            return False
        with self._db() as db:
            known = {r["name"] for r in db.execute(
                "SELECT name FROM player_sessions WHERE left_at IS NULL AND address IS NOT NULL")}
        return bool(humans - known)

    def since(self):
        """С какого момента панель отмечает заходы."""
        with self._db() as db:
            row = db.execute("SELECT value FROM meta WHERE key='presence_since'").fetchone()
        return int(row["value"]) if row else None
