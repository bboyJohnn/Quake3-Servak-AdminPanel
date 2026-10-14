#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Разбор games.log в SQLite и выборки для страницы статистики.

Движок пишет в игровой лог только время от начала карты, без календарной даты.
Дату матча (started_at) панель восстанавливает:
  * по логу консоли сервера (qconsole.log, включается `logfile 1` или `2`): в его
    начале движок пишет время запуска («logfile opened on Tue Oct  6 19:20:07 2026»),
    а дальше по порядку идут все загрузки карт (InitGame). Время матча = запуск +
    сумма длительностей предыдущих матчей. Длительность — внутриигровое время
    последней строки матча; загрузка карты в неё уже входит;
  * по началам матчей, которые панель видела сама, опрашивая сервер (точно);
  * между точными датами — по длительностям матчей (помечается «≈»).

Файл читается по частям: в базе хранится позиция и inode. Позиция указывает на
начало незаконченного матча — он будет дочитан целиком в следующий раз. Если лог
обрезали (ротация) или заменили, новый файл читается с начала, а накопленная
статистика остаётся: история живёт в базе, а не в логе.
"""

import os
import re
import sqlite3
import time

TIME_RE = re.compile(r"^\s*(\d+):(\d\d)\s?(.*)$")
KILL_RE = re.compile(r"^Kill:\s+(\d+)\s+(\d+)\s+(\d+):\s+(.*?)\s+killed\s+(.*?)\s+by\s+(MOD_\w+)$")
UINFO_RE = re.compile(r"^ClientUserinfoChanged:\s+(\d+)\s+(.*)$")
NUM_RE = re.compile(r"^(ClientConnect|ClientBegin|ClientDisconnect):\s+(\d+)")
SCORE_RE = re.compile(r"^score:\s+(-?\d+)\s+ping:\s+(\d+)\s+client:\s+(\d+)\s*(.*)$")
COLOR_RE = re.compile(r"\^[0-9a-zA-Z]")

WORLD = 1022

WEAPONS = {
    "MOD_GAUNTLET": "Кулак", "MOD_MACHINEGUN": "Пулемёт", "MOD_SHOTGUN": "Дробовик",
    "MOD_GRENADE": "Гранатомёт", "MOD_GRENADE_SPLASH": "Гранатомёт (взрыв)",
    "MOD_ROCKET": "Ракетомёт", "MOD_ROCKET_SPLASH": "Ракетомёт (взрыв)",
    "MOD_PLASMA": "Плазма", "MOD_PLASMA_SPLASH": "Плазма (взрыв)",
    "MOD_RAILGUN": "Рельса", "MOD_LIGHTNING": "Молния",
    "MOD_BFG": "BFG", "MOD_BFG_SPLASH": "BFG (взрыв)",
    "MOD_TELEFRAG": "Телефраг", "MOD_CRUSH": "Раздавлен", "MOD_FALLING": "Падение",
    "MOD_WATER": "Утонул", "MOD_SLIME": "Слизь", "MOD_LAVA": "Лава",
    "MOD_TRIGGER_HURT": "Ловушка", "MOD_SUICIDE": "Самоубийство",
    "MOD_TARGET_LASER": "Лазер", "MOD_GRAPPLE": "Крюк", "MOD_UNKNOWN": "Неизвестно",
}
ENVIRONMENT = {"MOD_TRIGGER_HURT", "MOD_FALLING", "MOD_WATER", "MOD_SLIME",
               "MOD_LAVA", "MOD_CRUSH", "MOD_SUICIDE", "MOD_TARGET_LASER"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS matches (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    map TEXT, gametype INTEGER, duration INTEGER,
    exit_reason TEXT, humans INTEGER, bots INTEGER, kills INTEGER,
    seen_at INTEGER
);
CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE, is_bot INTEGER,
    matches INTEGER DEFAULT 0, kills INTEGER DEFAULT 0, deaths INTEGER DEFAULT 0,
    suicides INTEGER DEFAULT 0, score_sum INTEGER DEFAULT 0, playtime INTEGER DEFAULT 0,
    last_seen_at INTEGER
);
CREATE TABLE IF NOT EXISTS match_players (
    match_id INTEGER, player_id INTEGER, score INTEGER, kills INTEGER, deaths INTEGER,
    playtime INTEGER,
    PRIMARY KEY (match_id, player_id)
);
CREATE TABLE IF NOT EXISTS weapon_kills (
    player_id INTEGER, weapon TEXT, kills INTEGER,
    PRIMARY KEY (player_id, weapon)
);
CREATE INDEX IF NOT EXISTS mp_player ON match_players(player_id);
CREATE INDEX IF NOT EXISTS m_seen ON matches(seen_at);
"""
# Поля, добавленные позже: база уже существует, поэтому доливаются ALTER TABLE.
MIGRATIONS = [
    ("matches", "started_at", "INTEGER"),        # восстановленное время начала матча (UTC)
    ("match_players", "joined_tsec", "INTEGER"),  # на какой секунде матча игрок появился
    # откуда дата: log — запуск сервера + длительности (точно), obs — панель видела
    # начало сама (точно), fill — достроено между точными (≈, пересчитывается)
    ("matches", "started_src", "TEXT"),
]
FIXED = ("log", "obs")  # даты, от которых можно считать дальше
SCHEMA_VERSION = 2  # 2 — нужен полный перечитанный лог, чтобы заполнить joined_tsec

# Первая строка qconsole.log после запуска сервера: время по часам машины (asctime).
RUN_RE = re.compile(r"logfile opened on (\w{3} \w{3}\s+\d{1,2} \d\d:\d\d:\d\d \d{4})")
INIT_MAP_RE = re.compile(r"InitGame:.*?\\mapname\\([A-Za-z0-9_-]+)")


def clean(name):
    return COLOR_RE.sub("", name or "").strip()


class Stats:
    def __init__(self, db_path, log_path):
        self.db_path = db_path
        self.log_path = log_path
        self._q3_cache = {}
        with self._db() as db:
            db.executescript(SCHEMA)
            for table, column, kind in MIGRATIONS:
                have = {r["name"] for r in db.execute("PRAGMA table_info(%s)" % table)}
                if column not in have:
                    db.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, column, kind))
            # даты, проставленные до появления started_src, — из журнала запусков
            db.execute("UPDATE matches SET started_src='log' WHERE started_at IS NOT NULL AND started_src IS NULL")

    def needs_rebuild(self):
        with self._db() as db:
            return int(self._meta(db, "schema_version", 1)) < SCHEMA_VERSION

    def _db(self):
        db = sqlite3.connect(self.db_path, timeout=30)
        db.row_factory = sqlite3.Row
        return db

    # --- состояние разбора ------------------------------------------------

    def _meta(self, db, key, default=None):
        row = db.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def _set_meta(self, db, key, value):
        db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?,?)", (key, str(value)))

    def state(self):
        with self._db() as db:
            return {
                "offset": int(self._meta(db, "offset", 0)),
                "inode": self._meta(db, "inode", ""),
                "parsed_at": int(self._meta(db, "parsed_at", 0)),
                "matches": db.execute("SELECT COUNT(*) c FROM matches").fetchone()["c"],
                "log_size": os.path.getsize(self.log_path) if os.path.exists(self.log_path) else 0,
            }

    def reset(self, db):
        for t in ("matches", "players", "match_players", "weapon_kills"):
            db.execute("DELETE FROM " + t)
        self._set_meta(db, "offset", 0)

    # --- разбор -----------------------------------------------------------

    def update(self):
        """Дочитывает лог. Возвращает, сколько матчей добавлено.

        Самый первый разбор (база пуста) времени не проставляет: когда были те
        матчи, неизвестно. Всё, что разобрано потом, помечается текущим временем.
        """
        if not os.path.exists(self.log_path):
            return 0
        st = os.stat(self.log_path)
        inode = "%d:%d" % (st.st_dev, st.st_ino)

        with self._db() as db:
            offset = int(self._meta(db, "offset", 0))
            if self._meta(db, "inode", "") != inode or st.st_size < offset:
                # лог заменили или обрезали при ротации — новый файл читаем с начала,
                # накопленное в базе не трогаем
                offset = 0
                self._set_meta(db, "inode", inode)
            if st.st_size == offset:
                return 0

            first_import = db.execute("SELECT COUNT(*) c FROM matches").fetchone()["c"] == 0
            now = None if first_import else int(time.time())

            # latin-1: один байт — один символ, поэтому позиции в тексте = позиции в файле
            with open(self.log_path, "r", encoding="latin-1", errors="replace", newline="") as f:
                f.seek(offset)
                chunk = f.read(st.st_size - offset)
            cut = chunk.rfind("\n")
            if cut == -1:
                return 0
            added, resume = self._consume(db, chunk[:cut + 1], now)
            self._set_meta(db, "offset", offset + resume)
            self._set_meta(db, "parsed_at", int(time.time()))
        return added

    def _consume(self, db, text, now):
        """Разбирает целые строки. Возвращает (матчей добавлено, откуда читать дальше).

        «Откуда читать дальше» — начало незаконченного матча: он ещё идёт, поэтому
        не записывается сейчас, а будет прочитан целиком при следующем обновлении.
        """
        match = None
        match_pos = len(text)
        added = 0
        pos = 0
        for raw in text.split("\n"):
            line_pos = pos
            pos += len(raw) + 1
            m = TIME_RE.match(raw)
            if not m:
                continue
            tsec = int(m.group(1)) * 60 + int(m.group(2))
            body = m.group(3).strip()
            if body.startswith("---"):
                # разделитель «0:00 ------» стоит перед следующим матчем и к текущему
                # не относится: иначе длительность матча обнулилась бы
                continue

            if body.startswith("InitGame:"):
                if match:
                    self._flush(db, match, now)
                    added += 1
                match = self._new_match(body)
                match_pos = line_pos
                continue
            if match is None:
                continue
            match["last"] = tsec

            if body.startswith("ShutdownGame") or body.startswith("Exit:"):
                if body.startswith("Exit:"):
                    match["exit"] = body[5:].strip()
                continue

            um = UINFO_RE.match(body)
            if um:
                slot, info = int(um.group(1)), um.group(2)
                name = re.search(r"n\\([^\\]*)", info)
                p = match["slots"].setdefault(slot, self._new_slot())
                if name:
                    p["name"] = clean(name.group(1))
                p["is_bot"] = 1 if re.search(r"\\skill\\", info) else 0
                continue

            nm = NUM_RE.match(body)
            if nm:
                kind, slot = nm.group(1), int(nm.group(2))
                p = match["slots"].setdefault(slot, self._new_slot())
                if kind == "ClientConnect":
                    p["in"] = tsec
                    if p["first_in"] is None:
                        p["first_in"] = tsec
                elif kind == "ClientDisconnect":
                    if p["in"] is not None:
                        p["playtime"] += max(0, tsec - p["in"])
                        p["in"] = None
                    # клетка освободилась: следующий вход в неё — уже другой игрок.
                    # Без этого за долгий матч в одну запись склеивались разные люди
                    match["gone"].append(match["slots"].pop(slot))
                continue

            km = KILL_RE.match(body)
            if km:
                killer, victim, _, _, _, mod = (
                    int(km.group(1)), int(km.group(2)), km.group(3),
                    km.group(4), km.group(5), km.group(6),
                )
                vp = match["slots"].setdefault(victim, self._new_slot())
                vp["deaths"] += 1
                match["kills"] += 1
                if killer == WORLD or killer == victim:
                    vp["suicides"] += 1
                else:
                    kp = match["slots"].setdefault(killer, self._new_slot())
                    kp["kills"] += 1
                    kp["weapons"][mod] = kp["weapons"].get(mod, 0) + 1
                continue

            sm = SCORE_RE.match(body)
            if sm:
                slot = int(sm.group(3))
                p = match["slots"].setdefault(slot, self._new_slot())
                p["score"] = int(sm.group(1))
                if not p["name"]:
                    p["name"] = clean(sm.group(4))

        if match is None:
            return added, len(text)
        # последний матч ещё идёт: не записываем, дочитаем целиком в следующий раз
        self._set_meta(db, "pending_map", match["map"])
        return added, match_pos

    @staticmethod
    def _new_slot():
        return {"name": "", "is_bot": 1, "kills": 0, "deaths": 0, "suicides": 0,
                "score": 0, "playtime": 0, "in": 0, "first_in": None, "weapons": {}}

    @staticmethod
    def _new_match(body):
        # строка начинается с обратной косой черты: \sv_hostname\имя\mapname\q3dm17...
        info = body[len("InitGame:"):].strip().lstrip("\\").split("\\")
        kv = dict(zip(info[::2], info[1::2]))
        return {
            "map": kv.get("mapname", "?"),
            "gametype": int(kv.get("g_gametype", 0) or 0),
            "slots": {}, "gone": [], "kills": 0, "last": 0, "exit": "",
        }

    @staticmethod
    def _players_of(match):
        """Все, кто был в матче: и ушедшие, и оставшиеся. Если человек вышел и
        вернулся в тот же матч, его записи сливаются по имени."""
        merged = {}
        for p in match["gone"] + list(match["slots"].values()):
            if not p["name"]:
                continue
            if p["in"] is not None:
                p["playtime"] += max(0, match["last"] - p["in"])
                p["in"] = None
            m = merged.get(p["name"])
            if m is None:
                merged[p["name"]] = p
                continue
            for k in ("kills", "deaths", "suicides", "playtime"):
                m[k] += p[k]
            m["score"] = max(m["score"], p["score"])
            ins = [x for x in (m["first_in"], p["first_in"]) if x is not None]
            m["first_in"] = min(ins) if ins else None
            m["is_bot"] = p["is_bot"]
            for mod, n in p["weapons"].items():
                m["weapons"][mod] = m["weapons"].get(mod, 0) + n
        return merged

    def _flush(self, db, match, now):
        players = self._players_of(match)
        humans = sum(1 for p in players.values() if not p["is_bot"])
        cur = db.execute(
            "INSERT INTO matches(map, gametype, duration, exit_reason, humans, bots, kills, seen_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (match["map"], match["gametype"], match["last"], match["exit"],
             humans, len(players) - humans, match["kills"], now),
        )
        match_id = cur.lastrowid

        for p in players.values():
            db.execute(
                "INSERT OR IGNORE INTO players(name, is_bot) VALUES (?,?)", (p["name"], p["is_bot"])
            )
            row = db.execute("SELECT id FROM players WHERE name=?", (p["name"],)).fetchone()
            pid = row["id"]
            db.execute(
                "UPDATE players SET is_bot=?, matches=matches+1, kills=kills+?, deaths=deaths+?,"
                " suicides=suicides+?, score_sum=score_sum+?, playtime=playtime+?,"
                " last_seen_at=COALESCE(?, last_seen_at) WHERE id=?",
                (p["is_bot"], p["kills"], p["deaths"], p["suicides"], p["score"],
                 p["playtime"], now, pid),
            )
            db.execute(
                "INSERT OR REPLACE INTO match_players(match_id, player_id, score, kills, deaths,"
                " playtime, joined_tsec) VALUES (?,?,?,?,?,?,?)",
                (match_id, pid, p["score"], p["kills"], p["deaths"], p["playtime"],
                 p["first_in"] if p["first_in"] is not None else 0),
            )
            for mod, n in p["weapons"].items():
                db.execute(
                    "INSERT INTO weapon_kills(player_id, weapon, kills) VALUES (?,?,?)"
                    " ON CONFLICT(player_id, weapon) DO UPDATE SET kills = kills + ?",
                    (pid, mod, n, n),
                )

    # --- полный перечит и даты -------------------------------------------

    def rebuild(self, paths):
        """Перечитывает игровые логи с нуля: старый (games.log.1) и текущий подряд,
        как один поток. Нужен один раз после смены схемы. Позиция чтения
        ставится на текущий лог, дальше update() продолжает как обычно."""
        texts = []
        for path in paths:
            if os.path.exists(path):
                with open(path, "r", encoding="latin-1", errors="replace", newline="") as f:
                    texts.append((path, f.read()))
        if not texts:
            return 0
        whole = "".join(t for _, t in texts)
        cut = whole.rfind("\n")
        with self._db() as db:
            self.reset(db)
            added, resume = self._consume(db, whole[:cut + 1], None)
            # позиция — в последнем файле (текущий games.log)
            last_path, last_text = texts[-1]
            base = len(whole) - len(last_text)
            st = os.stat(last_path)
            self._set_meta(db, "inode", "%d:%d" % (st.st_dev, st.st_ino))
            self._set_meta(db, "offset", max(0, resume - base))
            self._set_meta(db, "parsed_at", int(time.time()))
            self._set_meta(db, "schema_version", SCHEMA_VERSION)
        return added

    def restore_times(self, console_logs, observed=()):
        """Проставляет даты матчам. Возвращает, сколько дат поставлено или исправлено.

        Три прохода:
          1. лог консоли: время запуска сервера + длительности матчей (log);
          2. начала матчей, которые панель видела сама, — observed [(карта, время)] (obs);
          3. матчи между точными датами достраиваются длительностями (fill, «≈»).
             Если следующая точная дата известна, расхождение раскладывается по
             промежутку: оно копится от матчей, потерянных при обрезке игрового лога.
        Даты log и obs не меняются; fill пересчитываются каждый раз.
        """
        changed = self._restore_from_console(console_logs)
        changed += self._restore_from_observed(observed)
        changed += self._fill_gaps()
        return changed

    @staticmethod
    def _align(rows, seq, name_of):
        """Сопоставление с конца по названию карты: {индекс в seq: строка матча}."""
        pairs = {}
        j = len(seq) - 1
        for r in reversed(rows):
            k = j
            while k >= 0 and name_of(seq[k]) != r["map"]:
                k -= 1
            if k < 0:
                break
            pairs[k] = r
            j = k - 1
        return pairs

    def _restore_from_console(self, console_logs):
        """Матчи базы и загрузки карт из лога консоли — с конца, свежие со свежими. Загрузка
        без матча в игровом логе (зависание, убитый процесс, обрезка лога) обрывает
        цепочку, пока не встретится запуск сервера или уже точная дата."""
        events = []
        for path in console_logs:
            if os.path.exists(path):
                events += self._console_events(path)
        maps_idx = [i for i, e in enumerate(events) if e[0] == "map"]
        with self._db() as db:
            rows = db.execute("SELECT id, map, duration, started_at, started_src FROM matches ORDER BY id").fetchall()
            aligned = self._align(rows, maps_idx, lambda i: events[i][1])
            pairs = {maps_idx[k]: r for k, r in aligned.items()}
            n = 0
            t = None
            for i, (kind, value) in enumerate(events):
                if kind == "run":
                    t = value + 3  # запуск движка до первой карты — пара секунд
                    continue
                r = pairs.get(i)
                if r is None:
                    t = None  # длительность неизвестна: дальше не угадываем
                    continue
                if r["started_src"] in FIXED:
                    t = r["started_at"]  # уже точное время надёжнее своей суммы
                elif t is not None:
                    db.execute("UPDATE matches SET started_at=?, started_src='log' WHERE id=?", (int(t), r["id"]))
                    n += 1
                if t is not None:
                    t += r["duration"] or 0
        return n

    def _restore_from_observed(self, observed):
        """Начала матчей, замеченные панелью, — точные даты для ещё не точных матчей.
        Самое свежее начало может быть у матча, который ещё идёт и в базу не попал:
        такое пропускаем по времени (матч не мог кончиться позже, чем сейчас)."""
        observed = list(observed or ())
        if not observed:
            return 0
        now = time.time()
        with self._db() as db:
            first_obs = observed[0][1]
            rows = db.execute(
                "SELECT id, map, duration, started_at, started_src FROM matches"
                " WHERE started_src IS NULL OR started_src = 'fill' OR started_at >= ? ORDER BY id",
                (first_obs - 7200,)).fetchall()
            pending = self._meta(db, "pending_map", "")
            if observed and observed[-1][0] == pending:
                observed = observed[:-1]  # это начало матча, который ещё идёт
            # С конца, с проверкой по времени: матч должен кончиться к началу
            # следующего сопоставленного, а соседние по номеру — идти встык. Иначе матч,
            # начало которого панель пропустила (её перезапускали), «украл» бы более
            # раннее начало той же карты, и все старые матчи съехали бы.
            pairs = {}
            j = len(observed) - 1
            t_next, next_id = now, None
            for r in reversed(rows):
                if j < 0 or (r["started_at"] is not None and r["started_at"] < observed[0][1] - 7200):
                    break  # дальше только матчи старше всех замеченных начал
                dur = r["duration"] or 0
                k = j
                while k >= 0:
                    if observed[k][0] == r["map"]:
                        end = observed[k][1] + dur
                        back_to_back = next_id == r["id"] + 1
                        if end <= t_next + 120 and (not back_to_back or end >= t_next - 300):
                            break
                    k -= 1
                if k < 0:
                    continue  # начала этого матча панель не видела
                pairs[k] = r
                j = k - 1
                t_next, next_id = observed[k][1], r["id"]
            n = 0
            for k, r in pairs.items():
                if r["started_src"] in FIXED:
                    continue
                db.execute("UPDATE matches SET started_at=?, started_src='obs' WHERE id=?",
                           (int(observed[k][1]), r["id"]))
                n += 1
        return n

    def _fill_gaps(self):
        """Матчи без точной даты — между точными: от предыдущей точной даты по
        длительностям, с поправкой под следующую точную, если она есть."""
        with self._db() as db:
            rows = db.execute("SELECT id, duration, started_at, started_src FROM matches ORDER BY id").fetchall()
            n = 0
            i = 0
            while i < len(rows):
                if rows[i]["started_src"] in FIXED:
                    i += 1
                    continue
                j = i
                while j < len(rows) and rows[j]["started_src"] not in FIXED:
                    j += 1
                gap = rows[i:j]
                prev = rows[i - 1] if i > 0 else None
                nxt = rows[j] if j < len(rows) else None
                i = j
                if prev is None:
                    continue  # до первой точной даты достраивать не от чего
                t0 = prev["started_at"] + (prev["duration"] or 0)
                est = sum(r["duration"] or 0 for r in gap)
                scale = 1.0
                if nxt is not None and est > 0:
                    real = nxt["started_at"] - t0
                    if real > 0:
                        scale = real / est
                t = t0
                for r in gap:
                    if r["started_at"] != int(t) or r["started_src"] != "fill":
                        db.execute("UPDATE matches SET started_at=?, started_src='fill' WHERE id=?", (int(t), r["id"]))
                        n += 1
                    t += (r["duration"] or 0) * scale
        return n

    def _console_events(self, path):
        """Запуски и загрузки карт из одного лога консоли. Разбор держим в памяти,
        пока файл не изменится: старая копия .1 меняется только при ротации."""
        st = os.stat(path)
        key = (st.st_ino, st.st_size, int(st.st_mtime))
        hit = self._q3_cache.get(path)
        if hit and hit[0] == key:
            return hit[1]
        events = []
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = RUN_RE.search(line)
                if m:
                    # asctime по местному времени машины — панель живёт на ней же
                    try:
                        events.append(("run", time.mktime(time.strptime(m.group(1), "%a %b %d %H:%M:%S %Y"))))
                    except ValueError:
                        pass
                    continue
                m = INIT_MAP_RE.search(line)
                if m:
                    events.append(("map", m.group(1)))
        self._q3_cache[path] = (key, events)
        return events

    # --- выборки ----------------------------------------------------------

    def totals(self):
        with self._db() as db:
            r = db.execute(
                "SELECT COUNT(*) matches, COALESCE(SUM(kills),0) kills,"
                " COALESCE(SUM(duration),0) seconds FROM matches"
            ).fetchone()
            humans = db.execute("SELECT COUNT(*) c FROM players WHERE is_bot=0").fetchone()["c"]
            bots = db.execute("SELECT COUNT(*) c FROM players WHERE is_bot=1").fetchone()["c"]
            hm = db.execute("SELECT COUNT(*) c FROM matches WHERE humans>0").fetchone()["c"]
        return {"matches": r["matches"], "kills": r["kills"], "seconds": r["seconds"],
                "humans": humans, "bots": bots, "matches_with_humans": hm}

    def top(self, is_bot, limit=25):
        with self._db() as db:
            rows = db.execute(
                "SELECT name, matches, kills, deaths, suicides, score_sum, playtime, last_seen_at"
                " FROM players WHERE is_bot=? ORDER BY kills DESC, matches DESC LIMIT ?",
                (1 if is_bot else 0, limit),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["kd"] = round(r["kills"] / r["deaths"], 2) if r["deaths"] else float(r["kills"])
            out.append(d)
        return out

    def weapons(self, is_bot=None):
        sql = ("SELECT w.weapon, SUM(w.kills) kills FROM weapon_kills w"
               " JOIN players p ON p.id=w.player_id")
        args = []
        if is_bot is not None:
            sql += " WHERE p.is_bot=?"
            args.append(1 if is_bot else 0)
        sql += " GROUP BY w.weapon ORDER BY kills DESC"
        with self._db() as db:
            rows = db.execute(sql, args).fetchall()
        return [{"weapon": r["weapon"], "title": WEAPONS.get(r["weapon"], r["weapon"]),
                 "kills": r["kills"], "environment": r["weapon"] in ENVIRONMENT} for r in rows]

    def maps(self, limit=40):
        with self._db() as db:
            rows = db.execute(
                "SELECT map, COUNT(*) matches, COALESCE(SUM(kills),0) kills,"
                " CAST(AVG(duration) AS INTEGER) avg_duration,"
                " SUM(CASE WHEN humans>0 THEN 1 ELSE 0 END) with_humans"
                " FROM matches GROUP BY map ORDER BY matches DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def human_visits(self, limit=300):
        """Заходы живых игроков по игровому логу, от новых к старым.

        Заход — подряд идущие матчи одного игрока: при смене карты игрок
        переподключается на 0:00, это продолжение, а не новый заход. Время входа =
        восстановленное начало матча + секунда, на которой игрок появился; выхода —
        вход в последнем матче + сколько он там пробыл.
        """
        with self._db() as db:
            rows = db.execute(
                "SELECT m.id mid, m.map, m.started_at, p.name, mp.score, mp.kills, mp.deaths,"
                " mp.playtime, COALESCE(mp.joined_tsec, 0) jt"
                " FROM match_players mp"
                " JOIN players p ON p.id=mp.player_id AND p.is_bot=0"
                " JOIN matches m ON m.id=mp.match_id"
                " ORDER BY p.name, m.id"
            ).fetchall()
        visits = []
        cur = None
        for r in rows:
            if not (cur and cur["name"] == r["name"] and r["mid"] == cur["last_mid"] + 1 and r["jt"] == 0):
                cur = {"name": r["name"], "first_mid": r["mid"], "maps": [], "best_score": 0,
                       "kills": 0, "deaths": 0, "playtime": 0,
                       "joined_at": r["started_at"] + r["jt"] if r["started_at"] else None}
                visits.append(cur)
            cur["last_mid"] = r["mid"]
            if r["map"] not in cur["maps"]:
                cur["maps"].append(r["map"])
            cur["best_score"] = max(cur["best_score"], r["score"] or 0)
            cur["kills"] += r["kills"] or 0
            cur["deaths"] += r["deaths"] or 0
            cur["playtime"] += r["playtime"] or 0
            cur["left_at"] = (r["started_at"] + r["jt"] + (r["playtime"] or 0)) if r["started_at"] else None
        visits.sort(key=lambda v: v["first_mid"], reverse=True)
        return visits[:limit]

    def recent_matches(self, limit=40):
        """Последние матчи — от новых к старым. started_src: log/obs — точно, fill — ≈."""
        with self._db() as db:
            rows = db.execute(
                "SELECT id, map, gametype, started_at, started_src, duration, humans, bots, kills, exit_reason"
                " FROM matches ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def human_match_rows(self):
        """Участие живых игроков в матчах с временем: для подсчёта убийств за заход.
        {ник: [(начало, конец, убийств, смертей, карта, точно ли время), ...]}"""
        out = {}
        with self._db() as db:
            for r in db.execute(
                "SELECT p.name, m.map, m.started_src, m.started_at + COALESCE(mp.joined_tsec, 0) s,"
                " mp.playtime, mp.kills, mp.deaths"
                " FROM match_players mp JOIN players p ON p.id = mp.player_id AND p.is_bot = 0"
                " JOIN matches m ON m.id = mp.match_id WHERE m.started_at IS NOT NULL"
            ):
                out.setdefault(r["name"], []).append((r["s"], r["s"] + (r["playtime"] or 0),
                                                      r["kills"] or 0, r["deaths"] or 0,
                                                      r["map"], r["started_src"] in FIXED))
        return out

    def weapons_by_names(self, names):
        """Любимое оружие ников — статистика оружия ведётся по нику."""
        if not names:
            return []
        marks = ",".join("?" * len(names))
        with self._db() as db:
            rows = db.execute(
                "SELECT w.weapon, SUM(w.kills) kills FROM weapon_kills w JOIN players p ON p.id = w.player_id"
                " WHERE p.name IN (%s) AND p.is_bot = 0 GROUP BY w.weapon ORDER BY kills DESC" % marks, names
            ).fetchall()
        return [{"weapon": r["weapon"], "title": WEAPONS.get(r["weapon"], r["weapon"]), "kills": r["kills"]}
                for r in rows if r["weapon"] not in ENVIRONMENT]

    def human_breakdown(self, limit=10):
        """Где и во что играют живые игроки (все вместе): время по картам и режимам.
        Возвращает (карты, режимы): [{map, matches, playtime, kills}], [{gametype, matches, playtime, kills}]."""
        base = (" FROM match_players mp JOIN players p ON p.id = mp.player_id AND p.is_bot = 0"
                " JOIN matches m ON m.id = mp.match_id")
        sums = "COUNT(DISTINCT m.id) matches, SUM(mp.playtime) playtime, SUM(mp.kills) kills"
        with self._db() as db:
            maps = [dict(r) for r in db.execute(
                "SELECT m.map, " + sums + base + " GROUP BY m.map ORDER BY playtime DESC LIMIT ?", (limit,))]
            modes = [dict(r) for r in db.execute(
                "SELECT m.gametype, " + sums + base + " GROUP BY m.gametype ORDER BY playtime DESC")]
        return maps, modes

    def breakdown_by_names(self, names):
        """Режимы и карты ников по игровому логу — для карточки игрока.
        Возвращает (режимы, карты): [{gametype, matches, playtime, kills, deaths}],
        [{map, matches, kills, deaths}]. Как и оружие, ведётся по нику."""
        if not names:
            return [], []
        marks = ",".join("?" * len(names))
        base = (" FROM match_players mp JOIN players p ON p.id = mp.player_id AND p.is_bot = 0"
                " JOIN matches m ON m.id = mp.match_id WHERE p.name IN (%s)" % marks)
        sums = "COUNT(*) matches, SUM(mp.playtime) playtime, SUM(mp.kills) kills, SUM(mp.deaths) deaths"
        with self._db() as db:
            modes = [dict(r) for r in db.execute(
                "SELECT m.gametype, " + sums + base + " GROUP BY m.gametype ORDER BY playtime DESC", names)]
            maps = [dict(r) for r in db.execute(
                "SELECT m.map, " + sums + base + " GROUP BY m.map", names)]
        return modes, maps

    def activity(self, tz_offset=0):
        """Матчи по часам суток и дням — по восстановленному времени начала.

        tz_offset — смещение браузера в минутах, как у JS getTimezoneOffset
        (Москва = -180): часы считаются по часам пользователя, а не сервера.
        """
        with self._db() as db:
            rows = db.execute(
                "SELECT COALESCE(started_at, seen_at) t, humans FROM matches"
                " WHERE COALESCE(started_at, seen_at) IS NOT NULL"
            ).fetchall()
        hours = [0] * 24
        human_hours = [0] * 24
        days = {}
        for r in rows:
            lt = time.gmtime(r["t"] - tz_offset * 60)
            hours[lt.tm_hour] += 1
            if r["humans"]:
                human_hours[lt.tm_hour] += 1
            key = time.strftime("%Y-%m-%d", lt)
            days[key] = days.get(key, 0) + 1
        return {"hours": hours, "human_hours": human_hours,
                "days": [{"day": k, "matches": v} for k, v in sorted(days.items())][-30:],
                "stamped": len(rows)}
