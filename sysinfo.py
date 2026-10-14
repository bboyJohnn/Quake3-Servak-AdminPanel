#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Машина и логи — то, что панель узнаёт не у самого Quake, а у системы."""

import os
import shutil
import time


class System:
    def __init__(self, logs, disk_path="/"):
        """logs: {"console": путь, "games": путь}; disk_path — чей диск показывать."""
        self.logs = logs
        self.disk_path = disk_path
        self._cache = {}

    def _cached(self, key, ttl, fn):
        hit = self._cache.get(key)
        if hit and time.time() - hit[0] < ttl:
            return hit[1]
        value = fn()
        self._cache[key] = (time.time(), value)
        return value

    # --- машина -----------------------------------------------------------

    def host_info(self):
        return self._cached("host", 5, self._host_info)

    def _host_info(self):
        """Нагрузка, память, аптайм — из /proc (Linux). На другой системе — что найдётся."""
        info = {"cpus": os.cpu_count()}
        try:
            info["load"] = open("/proc/loadavg").read().split()[:3]
            mem = {}
            for line in open("/proc/meminfo"):
                k, v = line.split(":", 1)
                mem[k] = int(v.split()[0]) * 1024
            info["mem_total"] = mem.get("MemTotal", 0)
            info["mem_available"] = mem.get("MemAvailable", 0)
            info["swap_total"] = mem.get("SwapTotal", 0)
            info["swap_free"] = mem.get("SwapFree", 0)
            info["uptime"] = float(open("/proc/uptime").read().split()[0])
        except (OSError, ValueError):
            pass
        try:
            du = shutil.disk_usage(self.disk_path)
            info["disk_path"] = self.disk_path
            info["disk_total"], info["disk_free"] = du.total, du.free
        except OSError:
            pass
        return info

    # --- логи -------------------------------------------------------------

    def log_files(self):
        out = {}
        for key, path in self.logs.items():
            try:
                st = os.stat(path)
                out[key] = {"path": path, "size": st.st_size, "mtime": int(st.st_mtime)}
            except OSError:
                out[key] = {"path": path, "size": 0, "mtime": 0, "missing": True}
        return out

    def tail(self, key, lines=300, grep=""):
        """Последние строки лога. С фильтром ищет в последних 4 МБ файла."""
        path = self.logs.get(key)
        if not path or not os.path.exists(path):
            return []
        size = os.path.getsize(path)
        window = 4 * 1024 * 1024 if grep else min(size, 256 * 1024 + lines * 300)
        with open(path, "rb") as f:
            f.seek(max(0, size - window))
            data = f.read().decode("utf-8", errors="replace")
        rows = data.split("\n")
        if size > window:
            rows = rows[1:]  # первая строка обрезана посередине
        if grep:
            needle = grep.lower()
            rows = [r for r in rows if needle in r.lower()]
        rows = [r for r in rows if r.strip()]
        return rows[-lines:]
