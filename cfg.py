#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Чтение и правка server.cfg.

Файл правится построчно: панель меняет значение в существующей строке и не
трогает ни комментарии, ни порядок, ни оформление. Перед каждой записью
сохраняется копия в data/cfg-backups.

Ротации карт живут в том же файле переменными вида
    set f1 "set g_gametype 0; map q3dm17; set nextmap vstr f2"
и разбираются отдельно: список карт для показа и правки.

Очередь режимов держится на последней карте каждой ротации: её «set nextmap vstr X1»
указывает, с какой ротации продолжить. Указывает на себя — режим крутится сам по
себе; на другую — после последней карты сервер переходит к следующему режиму.
Режим, с которого сервер начинает после запуска, — последняя строка «vstr start_*».
"""

import os
import re
import shutil
import time

# Строка настройки: seta/set/sets ИМЯ ЗНАЧЕНИЕ [// комментарий]. Имя бывает в
# кавычках — так записаны теги для браузера серверов: sets ".Location" "Europe".
SETTING_RE = re.compile(
    r'^(?P<head>\s*)(?P<cmd>seta|set|sets)\s+(?P<q>"?)(?P<name>\.?[A-Za-z_][\w.]*)(?P=q)\s+'
    r'(?P<value>"[^"]*"|\S+)(?P<tail>.*)$'
)
ROT_RE = re.compile(r'^(?P<head>\s*)set\s+(?P<key>f|tdm|tour|c)(?P<idx>\d+)\s+"(?P<body>.*)"\s*$')
MAP_RE = re.compile(r'\bmap\s+([A-Za-z0-9_-]+)')
NEXT_RE = re.compile(r'(set\s+nextmap\s+vstr\s+)(f|tdm|tour|c)(\d+)')
START_RE = re.compile(r'^(?P<head>\s*vstr\s+)(?P<alias>start_\w+)(?P<tail>\s*)$')

ROTATIONS = {
    "f": {"title": "Все против всех", "gametype": 0},
    "tdm": {"title": "Командный бой", "gametype": 3},
    "tour": {"title": "Дуэли", "gametype": 1},
    "c": {"title": "Захват флага", "gametype": 4},
}
# Короткие имена для запуска режима целиком: set start_ffa "vstr f1" и т. д.
ALIASES = {"f": "start_ffa", "tdm": "start_tdm", "tour": "start_tourney", "c": "start_ctf"}
ALIAS_KEYS = {v: k for k, v in ALIASES.items()}

# Переменные, которые сервер принимает только при запуске: менять их в файле можно,
# но подействуют они после перезапуска. Панель показывает это пользователю.
RESTART_ONLY = {
    "dedicated": "задаётся только в командной строке запуска сервера",
    "vm_game": "задаётся только в командной строке запуска сервера",
    "com_hunkmegs": "применяется после перезапуска сервера",
    "net_port": "применяется после перезапуска сервера",
    "fs_game": "применяется после перезапуска сервера",
    "sv_maxclients": "применяется после смены карты",
    "sv_pure": "применяется после смены карты",
}

# Секретные значения, которые не уходят в браузер.
SECRETS = {"rconpassword", "sv_privatepassword"}


class ServerConfig:
    def __init__(self, path, backup_dir):
        self.path = path
        self.backup_dir = backup_dir

    # --- чтение -----------------------------------------------------------

    def read(self):
        """Текст файла; нет файла — пустая строка (что не так — покажет «Обзор»)."""
        try:
            with open(self.path, encoding="utf-8", errors="replace") as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def settings(self, hide_secrets=True):
        """Все настройки файла в порядке появления."""
        out = []
        section = ""
        for n, line in enumerate(self.read().split("\n")):
            head = re.match(r"^\s*//\s*(\d+\..*)$", line)
            if head:
                section = head.group(1).strip()
            m = SETTING_RE.match(line)
            if not m or line.lstrip().startswith("//"):
                continue
            name = m.group("name")
            if ROT_RE.match(line) or name.startswith(("f", "tdm", "tour", "c")) and re.match(
                r"^(f|tdm|tour|c)\d+$", name
            ):
                continue  # это ротация, она разбирается отдельно
            if re.match(r"^start_(ffa|tdm|tourney|ctf)$", name):
                continue
            value = m.group("value").strip('"')
            comment = m.group("tail").split("//", 1)[1].strip() if "//" in m.group("tail") else ""
            secret = name.lower() in SECRETS
            out.append({
                "line": n,
                "cmd": m.group("cmd"),
                "name": name,
                "value": "" if (secret and hide_secrets) else value,
                "secret": secret,
                "comment": comment,
                "section": section,
                "restart_note": RESTART_ONLY.get(name.lower(), ""),
            })
        return out

    def rotations(self):
        """Ротации карт: ключ -> {title, maps, head, next}.

        next — с какой ротации сервер продолжит после последней карты этой
        (равен самому ключу, если режим крутится сам по себе).
        """
        found = {}
        for line in self.read().split("\n"):
            m = ROT_RE.match(line)
            if not m:
                continue
            key, idx, body = m.group("key"), int(m.group("idx")), m.group("body")
            maps = MAP_RE.findall(body)
            if not maps:
                continue
            entry = found.setdefault(key, {"maps": [], "head": "", "targets": {}})
            entry["maps"].append((idx, maps[0]))
            nm = NEXT_RE.search(body)
            if nm:
                entry["targets"][idx] = nm.group(2)
            if idx == 1:
                # всё, что стоит до "map" в первой строке: режим, настройки команд
                entry["head"] = body.split("map ")[0].strip().rstrip(";").strip()
        result = {}
        for key, meta in ROTATIONS.items():
            if key in found:
                ordered = sorted(found[key]["maps"])
                last_idx = ordered[-1][0]
                result[key] = {
                    "title": meta["title"],
                    "gametype": meta["gametype"],
                    "maps": [m for _, m in ordered],
                    "head": found[key]["head"],
                    "next": found[key]["targets"].get(last_idx, key),
                }
        return result

    def rotation_bodies(self, key):
        """Точный текст строк ротации из файла: {"f1": "set g_gametype 0; map …", …}.
        Им панель обновляет работающий сервер — ровно тем, что записано в файле."""
        out = {}
        for line in self.read().split("\n"):
            m = ROT_RE.match(line)
            if m and m.group("key") == key:
                out["%s%s" % (key, m.group("idx"))] = m.group("body")
        return out

    def default_rotation(self):
        """Какая ротация запускается при старте (последняя строка vstr start_*)."""
        m = re.findall(r"^\s*vstr\s+(start_\w+)\s*$", self.read(), re.M)
        return m[-1] if m else ""

    def mode_order(self, saved_order=None):
        """Очередь режимов: {order, enabled, start}.

        Включённые режимы — те, по которым сервер проходит сам, начиная со
        стартового. Остальные идут следом в сохранённом панелью порядке.
        """
        rot = self.rotations()
        start = ALIAS_KEYS.get(self.default_rotation(), next(iter(rot), "f"))
        chain = []
        k = start
        while k in rot and k not in chain:
            chain.append(k)
            k = rot[k]["next"]
        rest = [k for k in (saved_order or []) if k in rot and k not in chain]
        rest += [k for k in ROTATIONS if k in rot and k not in chain and k not in rest]
        return {"order": chain + rest, "enabled": chain, "start": start}

    def set_mode_order(self, order, enabled):
        """Выстраивает очередь режимов. Возвращает (путь_к_копии, {ключ: новая строка}).

        Для каждого режима меняется одна строка — последняя в его ротации: её
        «set nextmap vstr X1» получает следующий включённый режим (или себя, если
        режим выключен). Плюс строка «vstr start_*» — первый включённый режим.
        """
        rot = self.rotations()
        need_keys = set(rot)
        if sorted(order) != sorted(need_keys):
            raise ValueError("в очереди должны быть все режимы ровно по разу: %s" % ", ".join(sorted(need_keys)))
        chain = [k for k in order if k in enabled]
        if not chain:
            raise ValueError("включите хотя бы один режим")
        target = {k: k for k in rot}
        for i, k in enumerate(chain):
            target[k] = chain[(i + 1) % len(chain)]
        last_idx = {k: len(v["maps"]) for k, v in rot.items()}

        lines = self.read().split("\n")
        changed = {}
        start_line = None
        for i, line in enumerate(lines):
            m = ROT_RE.match(line)
            if m and m.group("key") in rot and int(m.group("idx")) == last_idx[m.group("key")]:
                key = m.group("key")
                new = NEXT_RE.sub(lambda nm: nm.group(1) + target[key] + "1", line, count=1)
                if new != line:
                    lines[i] = new
                changed[key] = ROT_RE.match(new).group("body")
            if START_RE.match(line):
                start_line = i
        if start_line is None:
            raise ValueError("в server.cfg нет строки vstr start_* — не знаю, где стартовый режим")
        sm = START_RE.match(lines[start_line])
        lines[start_line] = sm.group("head") + ALIASES[chain[0]] + sm.group("tail")

        backup = self.backup()
        self._write("\n".join(lines))
        return backup, {"%s%d" % (k, last_idx[k]): body for k, body in changed.items()}

    # --- запись -----------------------------------------------------------

    def backup(self):
        os.makedirs(self.backup_dir, exist_ok=True)
        now = time.time()
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now)) + "-%03d" % (now % 1 * 1000)
        dst = os.path.join(self.backup_dir, "server.cfg." + stamp)
        shutil.copy2(self.path, dst)
        self._trim_backups()
        return dst

    def _trim_backups(self, keep=30):
        files = sorted(
            (os.path.join(self.backup_dir, f) for f in os.listdir(self.backup_dir)
             if f.startswith("server.cfg.")),
            reverse=True,
        )
        for old in files[keep:]:
            try:
                os.remove(old)
            except OSError:
                pass

    def _write(self, text):
        """Пишет через временный файл: обрыв на полпути не испортит конфиг."""
        tmp = self.path + ".tmp"
        try:
            mode = os.stat(self.path).st_mode & 0o777  # права прежние: файл читает сервер
        except OSError:
            mode = 0o600
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.chmod(tmp, mode)
        os.replace(tmp, self.path)

    def set_value(self, name, value):
        """Меняет одну настройку. Возвращает (старое, новое, путь_к_копии)."""
        if '"' in value or "\n" in value:
            raise ValueError('значение не должно содержать кавычек и переводов строк')
        lines = self.read().split("\n")
        for i, line in enumerate(lines):
            m = SETTING_RE.match(line)
            if not m or line.lstrip().startswith("//"):
                continue
            if m.group("name").lower() != name.lower():
                continue
            old = m.group("value").strip('"')
            # оформление не трогаем: кавычки ставим, только если они были или без них нельзя
            quoted = m.group("value").startswith('"') or not value or re.search(r"\s", value)
            shown = '"%s"' % value if quoted else value
            lines[i] = '%s%s %s%s%s %s%s' % (
                m.group("head"), m.group("cmd"), m.group("q"), m.group("name"), m.group("q"),
                shown, m.group("tail")
            )
            backup = self.backup()
            self._write("\n".join(lines))
            return old, value, backup
        raise KeyError("в server.cfg нет настройки %s" % name)

    def set_rotation(self, key, maps):
        """Перезаписывает ротацию новым списком карт."""
        if key not in ROTATIONS:
            raise KeyError("нет такой ротации: %s" % key)
        if not maps:
            raise ValueError("в ротации должна остаться хотя бы одна карта")
        for m in maps:
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", m):
                raise ValueError("недопустимое имя карты: %s" % m)

        current = self.rotations().get(key)
        head = current["head"] if current else "set g_gametype %d" % ROTATIONS[key]["gametype"]
        # последняя карта продолжает очередь режимов туда же, куда и раньше
        after = current["next"] if current else key

        lines = self.read().split("\n")
        first_idx = None
        kept = []
        for line in lines:
            m = ROT_RE.match(line)
            if m and m.group("key") == key:
                if first_idx is None:
                    first_idx = len(kept)
                continue
            kept.append(line)

        block = []
        width = max(len(key) + len(str(len(maps))), 4)
        for i, name in enumerate(maps, start=1):
            nxt = "%s1" % after if i == len(maps) else "%s%d" % (key, i + 1)
            body = ("%s; map %s; set nextmap vstr %s" % (head, name, nxt)) if i == 1 else \
                   ("map %s; set nextmap vstr %s" % (name, nxt))
            block.append('set %-*s "%s"' % (width, "%s%d" % (key, i), body))

        at = first_idx if first_idx is not None else len(kept)
        kept[at:at] = block
        backup = self.backup()
        self._write("\n".join(kept))
        return backup
