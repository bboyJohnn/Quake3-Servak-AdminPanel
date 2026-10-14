#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Разговор с сервером Quake III по UDP.

Два запроса на все случаи жизни:
  * getstatus — публичные сведения и список игроков (этим же живёт браузер серверов);
  * rcon      — команды управления; пароль берётся из server.cfg (seta rconpassword),
                чтобы секрет лежал в одном месте. Если пароль задан не в server.cfg,
                а, например, в командной строке сервера, — RCON_PASSWORD в .env.
"""

import asyncio
import re
import socket
import time

COLOR_RE = re.compile(r"\^[0-9a-zA-Z]")
PLAYER_RE = re.compile(r'^(-?\d+)\s+(\d+)\s+"(.*)"$')
# Строка `rcon status`: номер, счёт, пинг, имя, адрес, rate. Имя может содержать пробелы,
# поэтому опираемся на хвост: адрес и rate.
# Вместо пинга бывает CNCT (ещё подключается, грузит карту) и ZMBI (только что вышел):
# такие строки тоже разбираются, иначе у игрока, который так и не вошёл в игру, не
# записался бы адрес.
STATUS_RE = re.compile(r"^\s*(\d+)\s+(-?\d+)\s+(\d+|CNCT|ZMBI)\s+(.*?)\s+(\S+)\s+(\d+)\s*$")

GAMETYPES = {
    "0": "Все против всех",
    "1": "Дуэль",
    "2": "Одиночная",
    "3": "Командный бой",
    "4": "Захват флага",
}


def strip_colors(text):
    """Убирает цветовые коды Quake (^1, ^7 и прочие)."""
    return COLOR_RE.sub("", text or "")


class Q3Server:
    """Сервер Quake III: адрес, пароль rcon и запросы к нему."""

    def __init__(self, host, port, server_cfg, password=""):
        self.host = host
        self.port = port
        self.server_cfg = server_cfg
        self.password = password

    # --- пароль -----------------------------------------------------------

    def rcon_password(self):
        """Пароль из .env (RCON_PASSWORD) или из server.cfg. В браузер он не уходит."""
        if self.password:
            return self.password
        try:
            with open(self.server_cfg, encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = re.match(r'^\s*seta?\s+rconpassword\s+"([^"]*)"', line, re.I)
                    if m:
                        return m.group(1)
        except OSError:
            pass
        return ""

    # --- низкий уровень ---------------------------------------------------

    def _query(self, payload, timeout=3.0, collect=0.6):
        """Шлёт запрос и собирает ответ: rcon отвечает несколькими пакетами."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(timeout)
        blob = b""
        try:
            s.sendto(b"\xff\xff\xff\xff" + payload, (self.host, self.port))
            blob = s.recvfrom(65535)[0]
            s.settimeout(collect)
            deadline = time.time() + collect
            while time.time() < deadline:
                try:
                    blob += s.recvfrom(65535)[0]
                except socket.timeout:
                    break
        finally:
            s.close()
        return blob

    async def _aquery(self, payload, timeout=3.0, collect=0.6):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._query, payload, timeout, collect)

    # --- высокий уровень --------------------------------------------------

    async def status(self):
        """(настройки, игроки) или (None, None), если сервер молчит."""
        try:
            raw = await self._aquery(b"getstatus\n", 3.0, 0.2)
        except (socket.timeout, OSError):
            return None, None

        lines = raw[4:].decode("latin-1", errors="replace").split("\n")
        if len(lines) < 2:
            return None, None

        parts = lines[1].split("\\")
        info = dict(zip(parts[1::2], parts[2::2]))

        players = []
        for line in lines[2:]:
            m = PLAYER_RE.match(line.strip())
            if m:
                players.append({
                    "score": int(m.group(1)),
                    "ping": int(m.group(2)),
                    "name": strip_colors(m.group(3)),
                    "raw_name": m.group(3),
                })
        return info, players

    async def alive(self):
        info, _ = await self.status()
        return info is not None

    async def rcon(self, command):
        """Выполняет команду. Возвращает текст ответа сервера."""
        password = self.rcon_password()
        if not password:
            raise RuntimeError("нет пароля rcon: его нет ни в server.cfg (seta rconpassword), ни в RCON_PASSWORD")
        payload = b"rcon " + password.encode() + b" " + command.encode("latin-1", "replace")
        try:
            raw = await self._aquery(payload, 4.0, 0.8)
        except (socket.timeout, OSError):
            raise RuntimeError("сервер не ответил")
        out = raw.replace(b"\xff\xff\xff\xffprint\n", b"")
        return strip_colors(out.decode("latin-1", errors="replace")).strip()

    async def cvar(self, name):
        """Текущее значение переменной. Ответ вида: "имя" is:"значение" default:"…"."""
        out = await self.rcon(name)
        m = re.search(r'is:\s*"([^"]*)"', out)
        return m.group(1).rstrip("^7") if m else None

    async def player_table(self):
        """Игроки с адресами — из `rcon status`. У ботов в адресе стоит "bot"."""
        out = await self.rcon("status")
        rows = []
        for line in out.split("\n")[2:]:
            m = STATUS_RE.match(line)
            if not m:
                continue
            num, score, ping, name, addr, rate = m.groups()
            addr = strip_colors(addr)
            state = {"CNCT": "connecting", "ZMBI": "leaving"}.get(ping, "active")
            ping = int(ping) if state == "active" else None
            rows.append({
                "num": int(num),
                "score": int(score),
                "ping": ping,
                "state": state,
                "name": strip_colors(name).strip(),
                "address": addr,
                "is_bot": addr.lower().startswith("bot") or ping == 0,
                "rate": int(rate),
            })
        return rows

    async def bans(self):
        """Баны и исключения (`listbans`, вывод «Ban #1: 1.2.3.4/32»).

        Номер бана нужен для bandel/exceptdel. Сам banaddr игрока не выкидывает —
        после бана панель отдельно делает clientkick (проверено по sv_ccmds.c).
        """
        out = await self.rcon("listbans")
        rows = []
        for line in out.split("\n"):
            m = re.match(r"^\s*(Ban|Except)\s+#(\d+):\s*(\S+)", line)
            if m:
                rows.append({"kind": "ban" if m.group(1) == "Ban" else "except",
                             "num": int(m.group(2)), "target": m.group(3)})
        return rows

# Команды сервера этой версии ioquake3 (7d711f8, code/server/sv_ccmds.c) плюс
# игровые и общие команды, которыми пользуется панель. Для справочника в консоли.
COMMANDS = [
    ("status", "список игроков с номерами, пингом и адресами"),
    ("serverinfo", "настройки сервера, видимые снаружи"),
    ("dumpuser <номер>", "всё, что клиент сообщил о себе (userinfo)"),
    ("map <карта>", "загрузить карту"),
    ("map_restart [секунды]", "перезапустить текущую карту"),
    ("vstr nextmap", "следующая карта ротации"),
    ("vstr start_ffa | start_tdm | start_tourney | start_ctf", "переключить режим и ротацию"),
    ("say <текст>", "сообщение всем от имени сервера"),
    ("tell <номер> <текст>", "личное сообщение игроку"),
    ("sayto <имя> <текст>", "личное сообщение по имени"),
    ("kick <имя>", "выкинуть по имени (kick allbots — всех ботов)"),
    ("clientkick <номер> / kicknum <номер>", "выкинуть по номеру"),
    ("kickbots", "выкинуть всех ботов"),
    ("kickall", "выкинуть всех"),
    ("banaddr <ip[/маска]> | <номер>", "забанить адрес или диапазон; игрока не выкидывает"),
    ("exceptaddr <ip[/маска]> | <номер>", "исключение из бана"),
    ("listbans", "список банов и исключений"),
    ("bandel <номер>", "снять бан"),
    ("exceptdel <номер>", "снять исключение"),
    ("flushbans", "удалить все баны"),
    ("rehashbans", "перечитать serverbans.dat"),
    ("addbot <имя> [сложность 1-5] [red|blue]", "добавить бота"),
    ("set <переменная> <значение>", "изменить настройку до перезапуска"),
    ("<переменная>", "показать текущее значение"),
    ("exec server.cfg", "перечитать конфиг (если в его конце стоит map или vstr — карта сменится)"),
    ("heartbeat", "немедленно отправить анонс мастер-серверам"),
    ("systeminfo", "системные настройки (pure, pk3)"),
]
