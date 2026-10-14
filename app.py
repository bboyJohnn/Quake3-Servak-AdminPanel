#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Веб-панель сервера Quake III (ioquake3).

    python3 app.py              — запустить панель
    python3 app.py --password   — задать пароль панели (в .env попадает только его хэш)
    python3 app.py --check      — проверить настройки: файлы, getstatus, rcon — и выйти

Все настройки — в .env рядом с app.py (образец — .env.example). Работает на той же машине, что и игровой сервер. По умолчанию слушает
127.0.0.1:8091 и пускает только адреса из PANEL_ALLOWED_PEERS, а дальше всё равно
спрашивает пароль. Снаружи — через SSH-канал или обратный прокси с HTTPS.

С сервером панель говорит так же, как браузер серверов и админ: по UDP (getstatus
и rcon), а его файлы (server.cfg, games.log, qconsole.log, pk3) читает с диска.
"""

import argparse
import asyncio
import base64
import getpass
import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import sys
import time
import zipfile

from aiohttp import web

import audit as audit_mod
import cfg as cfg_mod
import geo as geo_mod
import humans as humans_mod
import presence as presence_mod
import previews as previews_mod
import q3 as q3_mod
import q3assets as assets_mod
import stats as stats_mod
import sysinfo as sys_mod

BASE = os.path.dirname(os.path.abspath(__file__))
ENV_PATH = os.environ.get("PANEL_ENV") or os.path.join(BASE, ".env")
SESSION_TTL = 12 * 3600
MODE_ALIASES = cfg_mod.ALIASES

# --- настройки из .env -------------------------------------------------------
# Всё, что панели нужно знать о сервере и о себе, — в одном файле .env (образец —
# .env.example). Значение может ссылаться на ключ выше: ${Q3_HOME}/baseq3.
# Текст после « #» — пояснение.
ENV_REF = re.compile(r"\$\{(\w+)\}")
ENV_COMMENT = re.compile(r"(?:^|\s+)#.*$")


def load_env(path):
    env = {}
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.read().splitlines()
    except OSError:
        return env
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = ENV_COMMENT.sub("", value)      # «ключ=значение   # пояснение»
        env[key.strip()] = ENV_REF.sub(lambda m: env.get(m.group(1), os.environ.get(m.group(1), "")), value)
    return env


def parse_peers(text):
    """Кого пускать: адреса и сети через запятую — 127.0.0.1, ::1, 192.168.0.0/24."""
    nets = []
    for item in (text or "127.0.0.1,::1").split(","):
        if item.strip():
            try:
                nets.append(ipaddress.ip_network(item.strip(), strict=False))
            except ValueError:
                sys.exit("PANEL_ALLOWED_PEERS: «%s» is not an IP address or network." % item.strip())
    return nets


def peer_allowed(addr, nets):
    try:
        ip = ipaddress.ip_address(addr or "")
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return any(ip in net for net in nets)


# --- пароль и сессии -------------------------------------------------------

def check_password(password, stored):
    try:
        _, salt, want = stored.split("$")
        got = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt),
                             n=2 ** 14, r=8, p=1, dklen=32)
        return hmac.compare_digest(got, base64.b64decode(want))
    except (ValueError, TypeError):
        return False


def hash_password(password):
    """scrypt$соль$хэш (base64) — то, что проверяет check_password."""
    salt = os.urandom(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2 ** 14, r=8, p=1, dklen=32)
    return "scrypt$%s$%s" % (base64.b64encode(salt).decode(), base64.b64encode(digest).decode())


def set_password():
    """python3 app.py --password: спрашивает пароль дважды и пишет его хэш в .env.
    Ключ подписи кук (PANEL_SECRET) создаётся, если его ещё нет. Сам пароль нигде не хранится."""
    if not os.path.exists(ENV_PATH):
        sys.exit("No settings file %s: copy .env.example to .env first." % ENV_PATH)
    while True:
        first = getpass.getpass("New panel password: ")
        if len(first) < 8:
            print("At least 8 characters, please.")
        elif getpass.getpass("Repeat it: ") != first:
            print("The passwords differ, try again.")
        else:
            break
    with open(ENV_PATH, encoding="utf-8") as f:
        text = f.read()

    def put(text, key, value):
        line = "%s=%s" % (key, value)
        pattern = re.compile(r"^%s=.*$" % re.escape(key), re.M)
        if pattern.search(text):
            return pattern.sub(lambda _m: line, text, count=1)
        return text.rstrip("\n") + "\n" + line + "\n"

    text = put(text, "PANEL_PASSWORD_HASH", hash_password(first))
    if not load_env(ENV_PATH).get("PANEL_SECRET"):
        text = put(text, "PANEL_SECRET", secrets.token_hex(32))
    with open(ENV_PATH, "w", encoding="utf-8") as f:
        f.write(text)
    os.chmod(ENV_PATH, 0o600)
    print("Saved to %s. Restart the panel if it is running." % ENV_PATH)


class Sessions:
    """Подписанная кука: переживает перезапуск панели и не требует базы."""

    def __init__(self, secret):
        self.secret = secret.encode()

    def _sig(self, text):
        return hmac.new(self.secret, text.encode(), hashlib.sha256).hexdigest()

    def make(self):
        body = "%d.%s" % (int(time.time()) + SESSION_TTL, secrets.token_hex(8))
        return body + "." + self._sig(body)

    def valid(self, value):
        try:
            expiry, nonce, sig = (value or "").split(".")
        except ValueError:
            return False
        body = expiry + "." + nonce
        return hmac.compare_digest(sig, self._sig(body)) and int(expiry) > time.time()

    def csrf(self, value):
        return self._sig("csrf:" + value)[:32]


# --- ошибки и проверки ввода ----------------------------------------------

class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def need(cond, message):
    if not cond:
        raise ApiError(message)


def int_arg(value, lo, hi, what="число"):
    try:
        n = int(value)
    except (TypeError, ValueError):
        raise ApiError("%s: нужно целое число" % what)
    need(lo <= n <= hi, "%s: от %d до %d" % (what, lo, hi))
    return n


def name_arg(value, what="имя"):
    value = (value or "").strip()
    need(re.fullmatch(r"[A-Za-z0-9_]{1,32}", value), "%s: только латиница, цифры и _" % what)
    return value


def map_arg(value):
    """Имя карты. В отличие от прочих имён допускает дефис: pro-q3dm6 и т. п."""
    value = (value or "").strip()
    need(re.fullmatch(r"[A-Za-z0-9_-]{1,32}", value), "карта: только латиница, цифры, _ и -")
    return value.lower()


def text_arg(value, maxlen=150):
    """Текст для чата: без кавычек, точек с запятой и переводов строк — иначе
    он мог бы закрыть кавычку и превратиться в ещё одну команду."""
    value = re.sub(r'["\\;\r\n\x00-\x1f]', " ", str(value or "")).strip()
    need(value, "пустой текст")
    return value[:maxlen]


def ok(data=None, **extra):
    body = {"ok": True}
    if data is not None:
        body["data"] = data
    body.update(extra)
    return web.json_response(body)


# --- приложение ------------------------------------------------------------

def paths_from_env(env):
    """Пути к файлам сервера. Всё выводится из Q3_HOME (fs_homepath сервера, где
    лежит папка мода baseq3), но каждый путь можно задать в .env отдельно."""
    home = os.path.expanduser(env.get("Q3_HOME") or "~/.q3a")
    game = os.path.join(home, (env.get("Q3_GAME") or "baseq3"))
    paks = [p for p in env.get("PAK_DIRS", "").split(":") if p] or [game]
    if game not in paks:
        paks.append(game)  # pk3 из fs_homepath движок тоже читает — и позже прочих
    return {
        "home": home,
        "data": os.path.expanduser(env.get("PANEL_DATA") or os.path.join(BASE, "data")),
        "server_cfg": env.get("SERVER_CFG") or os.path.join(game, "server.cfg"),
        "games_log": env.get("GAMES_LOG") or os.path.join(game, "games.log"),
        "console_log": env.get("CONSOLE_LOG") or os.path.join(game, "qconsole.log"),
        "pak_dirs": [os.path.expanduser(p) for p in paks],
    }


class Panel:
    def __init__(self, env):
        self.env = env
        self.paths = paths_from_env(env)
        self.server_cfg = self.paths["server_cfg"]
        data = self.paths["data"]
        os.makedirs(data, mode=0o700, exist_ok=True)  # тут база с IP игроков
        db = os.path.join(data, "panel.db")

        self.q3 = q3_mod.Q3Server(env.get("Q3_HOST") or "127.0.0.1", int(env.get("Q3_PORT") or "27960"),
                                  self.server_cfg, env.get("RCON_PASSWORD", ""))
        self.cfg = cfg_mod.ServerConfig(self.server_cfg, os.path.join(data, "cfg-backups"))
        self.stats = stats_mod.Stats(db, self.paths["games_log"])
        self.audit = audit_mod.Audit(db)
        self.presence = presence_mod.Presence(db)
        self.mode_order_file = os.path.join(data, "mode-order.txt")
        self.geo = geo_mod.Geo(data, env.get("GEOIP_DB", ""))
        # снимки карт из pk3 — в static/maps, при каждом запуске (новые карты подхватятся)
        try:
            self.previews = previews_mod.extract(self.paths["pak_dirs"], os.path.join(BASE, "static", "maps"))
        except OSError as e:
            print("previews: %s" % e, file=sys.stderr, flush=True)
            self.previews = []
        # для каких режимов рассчитана каждая карта — подсказка при загрузке карты
        try:
            self.arenas = previews_mod.arenas(self.paths["pak_dirs"])
        except OSError as e:
            print("arenas: %s" % e, file=sys.stderr, flush=True)
            self.arenas = {}
        self.sys = sys_mod.System({
            "console": self.paths["console_log"],
            "games": self.paths["games_log"],
        }, self.paths["home"] if os.path.isdir(self.paths["home"]) else "/")
        # оформление «Как в Quake III»: фоны, звуки и музыка из pk3 игры
        self.assets = assets_mod.Assets(self.paths["pak_dirs"], os.path.join(BASE, "static", "q3"))
        self.sessions = Sessions(env["PANEL_SECRET"])
        self.password_hash = env["PANEL_PASSWORD_HASH"]
        self.peers = parse_peers(env.get("PANEL_ALLOWED_PEERS"))
        # как панель называет сервер: меню, экран входа, вкладка браузера; адрес для игроков
        self.site = {"title": env.get("PANEL_TITLE", ""), "address": env.get("PANEL_ADDRESS", "")}
        self.login_fails = []
        self.maps_cache = None

    # --- помощники ------------------------------------------------------

    async def blocking(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

    async def rcon(self, action, command, detail=""):
        """rcon с записью в журнал. Ошибку превращает в понятный ответ."""
        try:
            out = await self.q3.rcon(command)
        except RuntimeError as e:
            self.audit.log(action, detail or command, ok=False, output=str(e))
            raise ApiError("Сервер: %s" % e, 502)
        self.audit.log(action, detail or command, ok=True, output=out)
        return out

    def all_maps(self):
        """Карты из pk3-архивов: имена maps/*.bsp. Меняются редко — считаем один раз."""
        if self.maps_cache is None:
            names = set()
            for base in self.paths["pak_dirs"]:
                try:
                    files = sorted(os.listdir(base))
                except OSError:
                    continue
                for fn in files:
                    if fn.lower().endswith(".pk3"):
                        try:
                            with zipfile.ZipFile(os.path.join(base, fn)) as z:
                                for n in z.namelist():
                                    m = re.match(r"(?i)^maps/([A-Za-z0-9_-]+)\.bsp$", n)
                                    if m:
                                        names.add(m.group(1).lower())
                        except (zipfile.BadZipFile, OSError):
                            pass
            self.maps_cache = sorted(names)
        return self.maps_cache

    # --- вход -----------------------------------------------------------

    async def me(self, request):
        """Вошёл ли браузер, и как называется сервер — это нужно ещё на экране входа."""
        cookie = request.cookies.get("q3p", "")
        site = dict(self.site, **self.assets.site())
        if self.sessions.valid(cookie):
            return ok({"auth": True, "csrf": self.sessions.csrf(cookie), "site": site})
        return ok({"auth": False, "site": site})

    async def music(self, request):
        """Трек прямо из pk3, с поддержкой Range — плеер может перематывать."""
        hit = self.assets.track(request.match_info.get("name"))
        if not hit:
            raise web.HTTPNotFound()
        pak, offset, size = hit
        start, end = 0, size - 1
        m = re.match(r"bytes=(\d*)-(\d*)$", request.headers.get("Range", ""))
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            if start > end or start >= size:
                raise web.HTTPRequestRangeNotSatisfiable(headers={"Content-Range": "bytes */%d" % size})
        resp = web.StreamResponse(status=206 if m and (m.group(1) or m.group(2)) else 200, headers={
            "Content-Type": "audio/wav", "Accept-Ranges": "bytes", "Content-Length": str(end - start + 1),
            "Cache-Control": "max-age=86400"})
        if resp.status == 206:
            resp.headers["Content-Range"] = "bytes %d-%d/%d" % (start, end, size)
        await resp.prepare(request)
        with open(pak, "rb") as f:
            f.seek(offset + start)
            left = end - start + 1
            while left > 0:
                chunk = f.read(min(65536, left))
                if not chunk:
                    break
                left -= len(chunk)
                await resp.write(chunk)
        await resp.write_eof()
        return resp

    async def login(self, request):
        now = time.time()
        self.login_fails = [t for t in self.login_fails if now - t < 300]
        if len(self.login_fails) >= 5:
            raise ApiError("Слишком много неверных попыток. Подождите 5 минут.", 429)
        body = await request.json()
        if not check_password(str(body.get("password", "")), self.password_hash):
            self.login_fails.append(now)
            self.audit.log("вход", "неверный пароль", ok=False)
            await asyncio.sleep(1)
            raise ApiError("Неверный пароль", 403)
        self.login_fails.clear()
        cookie = self.sessions.make()
        self.audit.log("вход", "успешно")
        resp = ok({"auth": True, "csrf": self.sessions.csrf(cookie)})
        resp.set_cookie("q3p", cookie, max_age=SESSION_TTL, httponly=True, samesite="Strict", path="/")
        return resp

    async def logout(self, request):
        resp = ok()
        resp.del_cookie("q3p", path="/")
        return resp

    # --- обзор ----------------------------------------------------------

    async def overview(self, request):
        info, players = await self.q3.status()
        host = await self.blocking(self.sys.host_info)
        server = None
        if info is not None:
            gt = info.get("g_gametype", "0")
            server = {
                "hostname": q3_mod.strip_colors(info.get("sv_hostname", "")),
                "map": info.get("mapname", "?"),
                "gametype": gt,
                "gametype_title": q3_mod.GAMETYPES.get(gt, gt),
                "maxclients": info.get("sv_maxclients", "?"),
                "timelimit": info.get("timelimit", ""),
                "fraglimit": info.get("fraglimit", ""),
                "capturelimit": info.get("capturelimit", ""),
                "bot_minplayers": info.get("bot_minplayers", ""),
                "version": info.get("version", ""),
                "players": sorted(players, key=lambda p: -p["score"]),
                "humans": sum(1 for p in players if p["ping"] > 0),
                "bots": sum(1 for p in players if p["ping"] == 0),
                "address": self.site["address"] or "%s:%s" % (self.q3.host, self.q3.port),
            }
        return ok({
            "server": server,
            "host": host,
            "setup": await self.blocking(self.setup_problems),
            "stats": await self.blocking(self.stats.totals),
            "stats_state": await self.blocking(self.stats.state),
            "logs": self.sys.log_files(),
            "now": int(time.time()),
        })

    def setup_problems(self):
        """Что не так с настройкой — список подсказок для «Обзора» (пусто — всё хорошо)."""
        out = []
        p = self.paths
        if not os.path.isfile(p["server_cfg"]):
            out.append("Не найден server.cfg: %s. Проверьте Q3_HOME (или SERVER_CFG) в .env." % p["server_cfg"])
        elif not self.q3.rcon_password():
            out.append("Нет пароля rcon: добавьте в server.cfg «seta rconpassword \"…\"» или RCON_PASSWORD в .env. "
                       "Без него не работают кик, бан, смена карты и консоль.")
        if not os.path.isfile(p["games_log"]):
            out.append("Нет игрового лога %s — статистики не будет. Включите в server.cfg «seta g_log \"games.log\"» "
                       "или укажите путь в GAMES_LOG." % p["games_log"])
        if not os.path.isfile(p["console_log"]):
            out.append("Нет лога консоли %s. Он не обязателен, но с ним точнее даты матчей: «seta logfile 1» "
                       "в server.cfg (или путь в CONSOLE_LOG)." % p["console_log"])
        if not self.all_maps():
            out.append("Не найдено ни одной карты в pk3: %s. Укажите папки с pak0.pk3 и другими pk3 в PAK_DIRS."
                       % ", ".join(p["pak_dirs"]))
        return out

    # --- игроки ---------------------------------------------------------

    async def players(self, request):
        rows = []
        try:
            rows = await self.q3.player_table()
        except RuntimeError as e:
            raise ApiError("Сервер: %s" % e, 502)
        bans = []
        try:
            bans = await self.q3.bans()
        except RuntimeError:
            pass
        return ok({"players": rows, "bans": bans})

    async def player_action(self, request):
        body = await request.json()
        action = body.get("action")
        if action == "kick":
            num = int_arg(body.get("num"), 0, 63, "номер игрока")
            out = await self.rcon("кик", "clientkick %d" % num, "игрок #%d %s" % (num, body.get("name", "")))
        elif action == "ban":
            num = int_arg(body.get("num"), 0, 63, "номер игрока")
            out = await self.rcon("бан", "banaddr %d" % num, "игрок #%d %s" % (num, body.get("name", "")))
            # banaddr сам игрока не выкидывает (sv_ccmds.c) — выкидываем отдельно
            out += "\n" + await self.rcon("кик", "clientkick %d" % num, "после бана #%d" % num)
        elif action == "ban_address":
            addr = (body.get("address") or "").strip()
            need(re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}(/\d{1,2})?", addr), "нужен IPv4-адрес, можно с маской /24")
            out = await self.rcon("бан", "banaddr %s" % addr)
        elif action == "unban":
            num = int_arg(body.get("num"), 1, 1024, "номер бана")
            kind = "exceptdel" if body.get("kind") == "except" else "bandel"
            out = await self.rcon("снятие бана", "%s %d" % (kind, num))
        elif action == "tell":
            num = int_arg(body.get("num"), 0, 63, "номер игрока")
            out = await self.rcon("личное сообщение", 'tell %d "%s"' % (num, text_arg(body.get("text"))))
        elif action == "say":
            out = await self.rcon("сообщение всем", 'say "%s"' % text_arg(body.get("text")))
        elif action == "kickbots":
            out = await self.rcon("кик ботов", "kickbots")
        elif action == "addbot":
            name = name_arg(body.get("name"), "имя бота")
            skill = int_arg(body.get("skill", 3), 1, 5, "сложность")
            out = await self.rcon("добавлен бот", "addbot %s %d" % (name, skill))
        elif action == "bots":
            n = int_arg(body.get("count"), 0, 16, "число игроков с ботами")
            out = await self.rcon("боты", "bot_minplayers %d" % n)
        else:
            raise ApiError("неизвестное действие")
        return ok({"output": out})

    # --- карты ----------------------------------------------------------

    @staticmethod
    def current_mode(rotations, nextmap_raw, current_map, gametype):
        """Какой режим идёт сейчас и какая это по счёту карта.

        Узнаём по nextmap («vstr tdm7» — значит, идёт 6-я карта командного боя).
        «vstr X1» особый случай: идёт последняя карта той ротации, что ведёт в X, —
        при очереди режимов это другой режим, поэтому ищем его по карте и типу игры.
        """
        m = re.match(r"^\s*vstr\s+(f|tdm|tour|c)(\d+)\s*$", nextmap_raw or "")
        if m and m.group(1) in rotations:
            key, n = m.group(1), int(m.group(2))
            if n > 1:
                cur, pos = key, n - 1
            else:
                cands = [k for k, r in rotations.items() if r["next"] == key] or [key]
                cur = next((k for k in cands if rotations[k]["maps"][-1] == current_map), None) \
                    or next((k for k in cands if str(rotations[k]["gametype"]) == str(gametype)), None) \
                    or cands[0]
                pos = len(rotations[cur]["maps"])
            r = rotations[cur]
            on_track = 0 < pos <= len(r["maps"]) and r["maps"][pos - 1] == current_map
            return {"key": cur, "title": r["title"], "position": pos,
                    "total": len(r["maps"]), "on_track": on_track}
        for k, r in rotations.items():
            if str(r["gametype"]) == str(gametype):
                return {"key": k, "title": r["title"], "position": None,
                        "total": len(r["maps"]), "on_track": False}
        # ротаций в формате панели нет — режим просто по g_gametype
        return {"key": None, "title": q3_mod.GAMETYPES.get(str(gametype), str(gametype)),
                "position": None, "total": 0, "on_track": False}

    def _saved_mode_order(self):
        try:
            with open(self.mode_order_file, encoding="utf-8") as f:
                return [k for k in f.read().split() if k in cfg_mod.ROTATIONS]
        except OSError:
            return []

    async def maps(self, request):
        info, _ = await self.q3.status()
        rotations = await self.blocking(self.cfg.rotations)
        nextmap_raw = None
        try:
            nextmap_raw = await self.q3.cvar("nextmap")
        except RuntimeError:
            pass
        current_map = info.get("mapname") if info else None
        gametype = info.get("g_gametype") if info else None
        # nextmap хранит команду вида «vstr f9» — показываем имя карты из ротации
        nextmap = nextmap_raw
        next_from_start = None
        m = re.match(r"^\s*vstr\s+(f|tdm|tour|c)(\d+)\s*$", nextmap_raw or "")
        if m and m.group(1) in rotations:
            maps_ = rotations[m.group(1)]["maps"]
            i = int(m.group(2)) - 1
            if 0 <= i < len(maps_):
                nextmap = maps_[i]
        # «vstr start_ffa» ставит панель после карты, загруженной вручную: дальше
        # очередь режимов с самого начала
        m = re.match(r"^\s*vstr\s+(start_\w+)\s*$", nextmap_raw or "")
        if m and cfg_mod.ALIAS_KEYS.get(m.group(1)) in rotations:
            next_from_start = cfg_mod.ALIAS_KEYS[m.group(1)]
            nextmap = rotations[next_from_start]["maps"][0]
        return ok({
            "current": current_map,
            "gametype": gametype,
            "nextmap": nextmap,
            "next_from_start": next_from_start,
            "arenas": self.arenas,
            "mode": self.current_mode(rotations, nextmap_raw, current_map, gametype) if info else None,
            "modes": await self.blocking(self.cfg.mode_order, self._saved_mode_order()),
            "rotations": rotations,
            # четыре режима — для выбора при загрузке карты, даже без ротаций в server.cfg
            "mode_list": [{"key": k, "title": r["title"], "gametype": r["gametype"]}
                          for k, r in cfg_mod.ROTATIONS.items()],
            "default": await self.blocking(self.cfg.default_rotation),
            "available": await self.blocking(self.all_maps),
            "previews": self.previews,
        })

    async def save_modes(self, request):
        body = await request.json()
        order = body.get("order") or []
        enabled = set(body.get("enabled") or [])
        need(isinstance(order, list) and all(k in cfg_mod.ROTATIONS for k in order), "неизвестный режим")
        try:
            backup, changed = await self.blocking(self.cfg.set_mode_order, order, enabled)
        except ValueError as e:
            raise ApiError(str(e))
        with open(self.mode_order_file, "w", encoding="utf-8") as f:
            f.write(" ".join(order))
        titles = [cfg_mod.ROTATIONS[k]["title"] for k in order if k in enabled]
        self.audit.log("очередь режимов", " → ".join(titles) + (" → по кругу" if len(titles) > 1 else " (сам по себе)"),
                       output="копия: " + os.path.basename(backup))
        # на работающем сервере меняем только последние строки ротаций — карта не сменится
        applied = True
        for var, text in changed.items():
            try:
                await self.q3.rcon('set %s "%s"' % (var, text))
            except RuntimeError:
                applied = False
                break
        return ok({"backup": os.path.basename(backup), "applied_live": applied})

    async def map_action(self, request):
        body = await request.json()
        action = body.get("action")
        if action == "map":
            name = map_arg(body.get("map"))
            need(name in self.all_maps(), "такой карты нет в pk3-архивах сервера")
            rotations = await self.blocking(self.cfg.rotations)
            key = body.get("rotation")
            need(key in cfg_mod.ROTATIONS, "выберите режим")
            title = cfg_mod.ROTATIONS[key]["title"]
            # Режим — та же голова, с которой начинается его ротация в server.cfg
            # («set g_gametype 3; set g_teamAutoJoin 1; …»), а без ротации — g_gametype.
            # «set nextmap» обязательно ПОСЛЕ map: при смене карты движок сам сбрасывает
            # nextmap на «map_restart 0», и сервер крутил бы эту карту по кругу.
            head = rotations[key]["head"] if key in rotations else ""
            if not re.fullmatch(r"set \w+ [\w.-]+(\s*;\s*set \w+ [\w.-]+)*", head):
                head = "set g_gametype %d" % cfg_mod.ROTATIONS[key]["gametype"]
            modes = await self.blocking(self.cfg.mode_order, self._saved_mode_order())
            if modes["enabled"] and cfg_mod.ALIAS_KEYS.get(await self.blocking(self.cfg.default_rotation)):
                # есть очередь режимов — после этой карты она начинается сначала
                first = modes["enabled"][0]
                after = "set nextmap vstr %s" % MODE_ALIASES[first]
                detail = "%s, режим «%s», дальше — очередь режимов с начала (%s)" % (
                    name, title, rotations[first]["title"])
            else:
                # очереди нет — после этой карты сервер идёт туда же, куда шёл
                try:
                    prev = await self.q3.cvar("nextmap")
                except RuntimeError:
                    prev = ""
                # без «;» и кавычек: значение встаёт внутрь одной строки «set panel_load "…"»
                ok_prev = prev and re.fullmatch(r"[\w .:-]{1,200}", prev) and "map_restart" not in prev
                after = "set nextmap %s" % prev if ok_prev else ""
                detail = "%s, режим «%s», дальше — %s" % (name, title, prev if ok_prev else "эта же карта")
            script = "%s; map %s%s" % (head, name, "; " + after if after else "")
            # одной командой rcon несколько команд через «;» не выполнить — как и в
            # ротациях, кладём их в переменную и запускаем через vstr
            try:
                await self.q3.rcon('set panel_load "%s"' % script)
            except RuntimeError as e:
                self.audit.log("смена карты", detail, ok=False, output=str(e))
                raise ApiError("Сервер: %s" % e, 502)
            out = await self.rcon("смена карты", "vstr panel_load", detail)
        elif action == "mode":
            key = body.get("rotation")
            need(key in MODE_ALIASES, "нет такой ротации")
            out = await self.rcon("смена режима", "vstr %s" % MODE_ALIASES[key])
        elif action == "next":
            out = await self.rcon("следующая карта", "vstr nextmap")
        elif action == "restart":
            out = await self.rcon("перезапуск карты", "map_restart 0")
        else:
            raise ApiError("неизвестное действие")
        return ok({"output": out})

    async def save_rotation(self, request):
        body = await request.json()
        key = body.get("rotation")
        maps = body.get("maps") or []
        need(key in cfg_mod.ROTATIONS, "нет такой ротации")
        need(isinstance(maps, list) and 1 <= len(maps) <= 64, "в ротации от 1 до 64 карт")
        available = set(self.all_maps())
        maps = [map_arg(m) for m in maps]
        missing = [m for m in maps if m not in available]
        need(not missing, "нет в pk3-архивах: %s" % ", ".join(missing))
        try:
            backup = await self.blocking(self.cfg.set_rotation, key, maps)
        except (KeyError, ValueError) as e:
            raise ApiError(str(e))
        self.audit.log("ротация", "%s: %s" % (key, ", ".join(maps)),
                       output="копия: " + os.path.basename(backup))

        # на работающем сервере старые значения живут в памяти — обновляем их
        # по одной строке, без exec server.cfg (он в конце переключил бы карту).
        # Шлём ровно то, что записано в файле, — в том числе продолжение очереди режимов.
        applied = True
        for var, text in (await self.blocking(self.cfg.rotation_bodies, key)).items():
            try:
                await self.q3.rcon('set %s "%s"' % (var, text))
            except RuntimeError:
                applied = False
                break
        return ok({"backup": os.path.basename(backup), "applied_live": applied})

    # --- настройки ------------------------------------------------------

    async def settings(self, request):
        items = await self.blocking(self.cfg.settings)
        info, _ = await self.q3.status()
        live = {k.lower(): v for k, v in (info or {}).items()}
        for it in items:
            v = live.get(it["name"].lower())
            it["live"] = None if it["secret"] else v
        return ok({"settings": items, "backups": self._backups()})

    def _backups(self):
        d = self.cfg.backup_dir
        if not os.path.isdir(d):
            return []
        return sorted((f for f in os.listdir(d) if f.startswith("server.cfg.")), reverse=True)[:30]

    async def save_setting(self, request):
        body = await request.json()
        name = (body.get("name") or "").strip()
        need(re.fullmatch(r"[A-Za-z_.][\w.]{0,40}", name), "недопустимое имя настройки")
        value = str(body.get("value", ""))
        need('"' not in value and "\n" not in value and ";" not in value,
             "в значении нельзя использовать кавычки, ; и переводы строк")
        need(len(value) <= 200, "слишком длинное значение")
        try:
            old, new, backup = await self.blocking(self.cfg.set_value, name, value)
        except KeyError as e:
            raise ApiError(str(e), 404)
        secret = name.lower() in cfg_mod.SECRETS
        self.audit.log("настройка", "%s: %s → %s" % (
            name, "***" if secret else old, "***" if secret else new),
            output="копия: " + os.path.basename(backup))

        note = cfg_mod.RESTART_ONLY.get(name.lower(), "")
        applied = False
        if not note:
            item = next((s for s in self.cfg.settings(hide_secrets=False) if s["name"].lower() == name.lower()), None)
            cmd = item["cmd"] if item else "set"
            try:
                await self.q3.rcon('%s %s "%s"' % (cmd, name, value))
                applied = True
            except RuntimeError:
                pass
        return ok({"backup": os.path.basename(backup), "applied_live": applied, "note": note})

    # --- статистика, логи, консоль, журнал --------------------------------

    async def stats_view(self, request):
        s = self.stats
        human_maps, human_modes = await self.blocking(s.human_breakdown, 10)
        return ok({
            "human_maps": human_maps,
            "human_modes": human_modes,
            "totals": await self.blocking(s.totals),
            "state": await self.blocking(s.state),
            "humans": await self.blocking(s.top, False, 50),
            "bots": await self.blocking(s.top, True, 40),
            "weapons_humans": await self.blocking(s.weapons, False),
            "weapons_bots": await self.blocking(s.weapons, True),
            "maps": await self.blocking(s.maps, 40),
            "recent": await self.blocking(s.recent_matches, 40),
            # часовой пояс браузера: часы и дни считаются по часам пользователя, а не сервера
            "activity": await self.blocking(s.activity, self._tz(request)),
        })

    @staticmethod
    def _tz(request):
        """Смещение браузера в минутах, как у JS getTimezoneOffset (Москва = -180)."""
        try:
            return max(-840, min(840, int(request.query.get("tz", 0))))
        except ValueError:
            return 0

    def _all_sessions(self):
        """Все заходы живых игроков, от новых к старым.

        Точные — те, что панель видела сама (с её первого запуска). Раньше —
        восстановленные по игровому логу (approx), только до начала точного учёта,
        чтобы не задвоить. Убийства и смерти за точный заход — из статистики
        матчей того же ника, пересёкшихся с заходом по времени.
        """
        now = int(time.time())
        since = self.presence.since() or float("inf")
        rows = self.stats.human_match_rows()
        out = []
        for s in self.presence.sessions(None, 100000):
            end = now if s["online"] else (s["left_at"] or s["last_seen"])
            k = d = 0
            maps = set(s["maps"])
            for a, b, kk, dd, mp, exact in rows.get(s["name"], ()):
                # тот же ник, та же карта, пересечение по времени; у матча с датой «≈»
                # (достроенной между точными) — допуск шире
                slack = 90 if exact else 1800
                if mp in maps and a <= end + slack and b >= s["joined_at"] - slack:
                    k += kk
                    d += dd
            s.update(kills=k, deaths=d, duration=end - s["joined_at"], approx=False)
            out.append(s)
        for v in self.stats.human_visits(100000):
            if v["joined_at"] and v["joined_at"] < since:
                out.append({"name": v["name"], "joined_at": v["joined_at"], "left_at": v["left_at"],
                            "last_seen": v["left_at"] or v["joined_at"], "online": False,
                            "duration": v["playtime"], "maps": v["maps"], "best_score": v["best_score"],
                            "kills": v["kills"], "deaths": v["deaths"], "address": None, "approx": True})
        out.sort(key=lambda s: -s["joined_at"])
        for s in out:
            s["ip"] = humans_mod.ip_of(s.get("address")) or None
            s["key"] = humans_mod.key_of(s)
            s["country"] = self.geo.country(s["ip"]) if s["ip"] else None
        return out

    def _sessions_view(self):
        sessions = self._all_sessions()
        players = humans_mod.summarize(sessions, self.geo)
        with_ip = [s["joined_at"] for s in sessions if s["ip"]]
        return {
            "sessions": sessions[:2000],
            "players": players,
            "countries": humans_mod.countries(players),
            "records": humans_mod.records(sessions, players),
            "tracking_since": self.presence.since(),
            "ip_since": min(with_ip) if with_ip else None,
            "now": int(time.time()),
        }

    async def player_sessions(self, request):
        """Заходы живых игроков, итоги по игрокам, страны, рекорды. Календарь строит
        браузер — в своём часовом поясе, поэтому сюда отдаются сырые моменты времени."""
        return ok(await self.blocking(self._sessions_view))

    def _player_card(self, key):
        sessions = self._all_sessions()
        names = sorted({s["name"] for s in sessions if s["key"] == key})
        modes, maps_log = self.stats.breakdown_by_names(names)
        return humans_mod.card(key, sessions, self.geo, self.stats.weapons_by_names(names),
                               humans_mod.summarize(sessions, self.geo), modes, maps_log)

    async def player_card(self, request):
        """Карточка игрока: ключ — IP-адрес или «~ник» для заходов без адреса."""
        key = (request.query.get("key") or "").strip()
        need(key and len(key) <= 80, "не указан игрок")
        card = await self.blocking(self._player_card, key)
        need(card, "такого игрока нет")
        return ok(card)

    async def logs(self, request):
        key = request.query.get("log", "console")
        need(key in ("console", "games"), "нет такого лога")
        lines = int_arg(request.query.get("lines", 300), 10, 3000, "строк")
        grep = request.query.get("grep", "")[:100]
        rows = await self.blocking(self.sys.tail, key, lines, grep)
        return ok({"lines": rows, "files": self.sys.log_files()})

    async def console(self, request):
        body = await request.json()
        command = (body.get("command") or "").strip()
        need(command, "пустая команда")
        need(len(command) <= 500 and "\n" not in command and "\r" not in command,
             "команда должна быть одной строкой до 500 символов")
        out = await self.rcon("консоль", command)
        return ok({"output": out})

    async def commands(self, request):
        return ok([{"cmd": c, "help": h} for c, h in q3_mod.COMMANDS])

    async def audit_view(self, request):
        return ok(await self.blocking(self.audit.recent, 300))

    async def stats_refresh(self, request):
        added = await self.blocking(self.stats.update)
        return ok({"added": added})

    # --- фоновое ------------------------------------------------------

    async def stats_loop(self, app):
        # если логи обрезают с копией в .1 (logrotate copytruncate) — читаем и копию
        games_logs = [self.paths["games_log"] + ".1", self.paths["games_log"]]
        console_logs = [self.paths["console_log"] + ".1", self.paths["console_log"]]

        async def loop():
            # новая схема базы: один раз перечитать оба игровых лога целиком
            try:
                if await self.blocking(self.stats.needs_rebuild):
                    n = await self.blocking(self.stats.rebuild, games_logs)
                    print("stats: логи перечитаны заново, матчей %d" % n, flush=True)
            except Exception as e:
                print("stats rebuild: %s" % e, file=sys.stderr, flush=True)
            i = 0
            while True:
                try:
                    await self.blocking(self.stats.update)
                    if i % 5 == 0:  # даты матчей — раз в 5 минут: лог консоли + замеченные начала
                        observed = await self.blocking(self.presence.map_starts)
                        n = await self.blocking(self.stats.restore_times, console_logs, observed)
                        if n:
                            print("stats: восстановлено дат матчей %d" % n, flush=True)
                except Exception as e:  # статистика не должна ронять панель
                    print("stats: %s" % e, file=sys.stderr, flush=True)
                i += 1
                await asyncio.sleep(60)

        async def presence_loop():
            # кто из живых игроков на сервере — раз в 20 секунд, для дат заходов
            while True:
                try:
                    info, players = await self.q3.status()
                    # адрес видно только в rcon status — спрашиваем, лишь когда пришёл
                    # живой игрок без записанного адреса (раз на заход, не каждые 20 с)
                    addresses = {}
                    if players and await self.blocking(self.presence.needs_addresses, players):
                        try:
                            for r in await self.q3.player_table():
                                if not r["is_bot"]:
                                    addresses[r["name"]] = humans_mod.ip_of(r["address"]) or None
                        except RuntimeError:
                            pass
                    await self.blocking(self.presence.tick, players,
                                        info.get("mapname") if info else None, None, addresses)
                    if info:  # смена карты = начало матча, точная опора для дат матчей
                        await self.blocking(self.presence.observe_map, info.get("mapname"), players)
                except Exception as e:
                    print("presence: %s" % e, file=sys.stderr, flush=True)
                await asyncio.sleep(20)

        tasks = [asyncio.create_task(loop()), asyncio.create_task(presence_loop())]
        yield
        for t in tasks:
            t.cancel()


# --- промежуточные слои ----------------------------------------------------

def build_app(panel):
    open_paths = {"/api/me", "/api/login"}

    @web.middleware
    async def guard(request, handler):
        # сначала адрес (PANEL_ALLOWED_PEERS), потом уже пароль. SSH-канал и обратный
        # прокси на этой же машине приходят с 127.0.0.1
        if not peer_allowed(request.remote, panel.peers):
            raise web.HTTPForbidden(text="forbidden")
        try:
            if request.path.startswith(("/api/", "/q3/")) and request.path not in open_paths:
                cookie = request.cookies.get("q3p", "")
                if not panel.sessions.valid(cookie):
                    raise ApiError("Нужно войти", 401)
                if request.method == "POST":
                    token = request.headers.get("X-CSRF", "")
                    if not hmac.compare_digest(token, panel.sessions.csrf(cookie)):
                        raise ApiError("Устаревшая страница — обновите её", 403)
            resp = await handler(request)
        except ApiError as e:
            resp = web.json_response({"ok": False, "error": str(e)}, status=e.status)
        except web.HTTPException:
            raise
        except Exception as e:
            print("ошибка %s: %r" % (request.path, e), file=sys.stderr, flush=True)
            resp = web.json_response({"ok": False, "error": "Внутренняя ошибка: %s" % e}, status=500)
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["Referrer-Policy"] = "no-referrer"
        resp.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'")
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        elif request.path.endswith((".js", ".css")):
            # после обновления панели браузер не должен показывать старый скрипт из кеша
            resp.headers["Cache-Control"] = "no-cache"
        elif request.path.startswith("/static/"):
            resp.headers["Cache-Control"] = "max-age=86400"  # картинки карт, флаги, шрифты, оформление
        return resp

    app = web.Application(middlewares=[guard], client_max_size=256 * 1024)
    r = app.router
    r.add_get("/api/me", panel.me)
    r.add_post("/api/login", panel.login)
    r.add_post("/api/logout", panel.logout)
    r.add_get("/api/overview", panel.overview)
    r.add_get("/api/players", panel.players)
    r.add_post("/api/players", panel.player_action)
    r.add_get("/api/maps", panel.maps)
    r.add_post("/api/maps", panel.map_action)
    r.add_post("/api/rotation", panel.save_rotation)
    r.add_post("/api/modes", panel.save_modes)
    r.add_get("/api/sessions", panel.player_sessions)
    r.add_get("/api/player", panel.player_card)
    r.add_get("/api/settings", panel.settings)
    r.add_post("/api/settings", panel.save_setting)
    r.add_get("/api/stats", panel.stats_view)
    r.add_post("/api/stats/refresh", panel.stats_refresh)
    r.add_get("/api/logs", panel.logs)
    r.add_post("/api/console", panel.console)
    r.add_get("/api/commands", panel.commands)
    r.add_get("/api/audit", panel.audit_view)

    static = os.path.join(BASE, "static")

    async def index(request):
        # скрипт и стили — с номером версии (время изменения файла): обновление панели
        # сразу видно, даже если браузер держит прошлые файлы в кеше
        with open(os.path.join(static, "index.html"), encoding="utf-8") as f:
            html_ = f.read()
        for name in ("boot.js", "app.js", "app.css"):
            try:
                v = int(os.path.getmtime(os.path.join(static, name)))
            except OSError:
                continue
            html_ = html_.replace("/static/%s\"" % name, "/static/%s?v=%d\"" % (name, v))
        # версия словарей — для boot.js, который подключает static/i18n/<язык>.js
        try:
            v = max(int(os.path.getmtime(os.path.join(static, "i18n", n))) for n in os.listdir(os.path.join(static, "i18n")))
        except (OSError, ValueError):
            v = 0
        html_ = html_.replace("<html lang=\"ru\">", "<html lang=\"ru\" data-v=\"%d\">" % v)
        return web.Response(text=html_, content_type="text/html", headers={"Cache-Control": "no-store"})
    r.add_get("/", index)
    r.add_get("/q3/music/{name}", panel.music)
    r.add_static("/static/", static)
    app.cleanup_ctx.append(panel.stats_loop)
    return app


# --- проверка настроек и запуск ---------------------------------------------

def check_setup(env):
    """python3 app.py --check: что панель нашла и до чего достучалась. Код выхода 1 — есть ошибки."""
    bad = 0

    def line(good, what, detail="", warn=False):
        nonlocal bad
        bad += not good and not warn
        print("  %-4s %-24s %s" % ("ok" if good else ("warn" if warn else "FAIL"), what, detail))

    print("Settings: %s" % ENV_PATH)
    for key in ("PANEL_PASSWORD_HASH", "PANEL_SECRET"):
        line(bool(env.get(key)), key, "set" if env.get(key) else "empty — run: python3 app.py --password")
    p = paths_from_env(env)
    line(os.path.isdir(p["home"]), "Q3_HOME", p["home"])
    line(os.path.isfile(p["server_cfg"]), "server.cfg", p["server_cfg"])
    line(os.access(p["server_cfg"], os.W_OK), "server.cfg writable", "needed to save settings and rotations", warn=True)
    q3 = q3_mod.Q3Server(env.get("Q3_HOST") or "127.0.0.1", int(env.get("Q3_PORT") or "27960"),
                         p["server_cfg"], env.get("RCON_PASSWORD", ""))
    line(bool(q3.rcon_password()), "rcon password", "from RCON_PASSWORD" if env.get("RCON_PASSWORD")
         else "from server.cfg" if q3.rcon_password() else "none — seta rconpassword in server.cfg or RCON_PASSWORD")
    line(os.path.isfile(p["games_log"]), "Game log (g_log)", "%s — statistics come from it" % p["games_log"], warn=True)
    line(os.path.isfile(p["console_log"]), "Console log (logfile)", "%s — optional, dates of matches" % p["console_log"],
         warn=True)
    maps = []
    for d in p["pak_dirs"]:
        maps += [f for f in (os.listdir(d) if os.path.isdir(d) else []) if f.lower().endswith(".pk3")]
    line(bool(maps), "pk3 files", "%d in %s" % (len(maps), ", ".join(p["pak_dirs"])))
    art = assets_mod.Assets(p["pak_dirs"], os.path.join(BASE, "static", "q3")).site()
    line(bool(art["backgrounds"]), "Quake III look", "%d backgrounds, %d sounds, %d music tracks from pk3" % (
        len(art["backgrounds"]), len(art["sounds"]), len(art["music"])), warn=True)
    geo = geo_mod.Geo(p["data"], env.get("GEOIP_DB", ""))
    line(geo.country("8.8.8.8") is not None, "GeoIP database", "optional, countries of players' IPs", warn=True)
    try:
        os.makedirs(p["data"], mode=0o700, exist_ok=True)
        line(os.access(p["data"], os.W_OK), "Panel data folder", p["data"])
    except OSError as e:
        line(False, "Panel data folder", "%s: %s" % (p["data"], e))

    async def probe():
        info, players = await q3.status()
        line(info is not None, "getstatus %s:%d" % (q3.host, q3.port),
             "%s, map %s, %d players" % (q3_mod.strip_colors(info.get("sv_hostname", "")), info.get("mapname", "?"),
                                         len(players)) if info else "no answer — is the server running?")
        if info is not None and q3.rcon_password():
            try:
                out = await q3.rcon("status")
                good = "map:" in out
                line(good, "rcon", "works" if good else (out.splitlines() or ["no answer"])[0][:80])
            except RuntimeError as e:
                line(False, "rcon", str(e))
    asyncio.run(probe())
    host, port = env.get("PANEL_HOST") or "127.0.0.1", env.get("PANEL_PORT") or "8091"
    print("Panel will listen on %s:%s and accept %s" % (host, port, ", ".join(
        str(n) for n in parse_peers(env.get("PANEL_ALLOWED_PEERS")))))
    return bad


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--check", action="store_true", help="check the settings and exit")
    ap.add_argument("--password", action="store_true", help="set the panel password and exit")
    args = ap.parse_args()
    if args.password:
        return set_password()
    if not os.path.exists(ENV_PATH):
        sys.exit("No settings file %s: copy .env.example to .env and fill it in (see README)." % ENV_PATH)
    env = load_env(ENV_PATH)
    if args.check:
        sys.exit(1 if check_setup(env) else 0)
    if not env.get("PANEL_SECRET") or not env.get("PANEL_PASSWORD_HASH"):
        sys.exit("No panel password in %s — run: venv/bin/python app.py --password" % ENV_PATH)
    panel = Panel(env)
    host = env.get("PANEL_HOST") or "127.0.0.1"
    port = int(env.get("PANEL_PORT") or "8091")
    web.run_app(build_app(panel), host=host, port=port, access_log=None,
                print=lambda *_: print("Panel: http://%s:%d, accepting %s" % (
                    host, port, ", ".join(str(n) for n in panel.peers)), flush=True))


if __name__ == "__main__":
    main()
