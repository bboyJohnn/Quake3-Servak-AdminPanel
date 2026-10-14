#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Страна по IP-адресу — из локальной базы в формате MaxMind (.mmdb).

Подходит GeoLite2-Country (MaxMind, бесплатно после регистрации) или DB-IP
«IP to Country Lite» (без регистрации, CC BY 4.0). Путь — GEOIP_DB в .env, иначе
панель ищет data/GeoLite2-Country.mmdb и data/dbip-country-lite.mmdb. Без базы и
без модуля maxminddb страны просто не показываются.

Адреса игроков никуда не отправляются: всё ищется в файле на этом сервере.
Раз в час панель проверяет, не обновили ли файл, и перечитывает его.
"""

import ipaddress
import os
import time

try:
    import maxminddb
except ImportError:
    maxminddb = None

LAN = {"code": "LAN", "name": "локальная сеть"}
LANGS = {"pt-BR": "pt", "zh-CN": "zh"}                  # коды баз MaxMind/DB-IP -> коды панели
PANEL_LANGS = {"ru", "en", "de", "fr", "es", "pt", "ja", "zh"}


class Geo:
    def __init__(self, data_dir, path=""):
        names = ("GeoLite2-Country.mmdb", "dbip-country-lite.mmdb")
        self.paths = ([path] if path else []) + [os.path.join(data_dir, n) for n in names]
        self.reader = None
        self.path = None
        self.mtime = 0
        self.checked = 0
        self.cache = {}

    def _reader(self):
        """Открытая база. Раз в час проверяет, не заменили ли файл."""
        if maxminddb is None:
            return None
        if self.reader and time.time() - self.checked < 3600:
            return self.reader
        self.checked = time.time()
        for path in self.paths:
            try:
                mtime = os.stat(path).st_mtime
            except OSError:
                continue
            if self.reader and path == self.path and mtime == self.mtime:
                return self.reader
            try:
                reader = maxminddb.open_database(path)
            except Exception:
                continue
            if self.reader:
                try:
                    self.reader.close()
                except Exception:
                    pass
            self.reader, self.path, self.mtime = reader, path, mtime
            self.cache.clear()
            return reader
        return self.reader

    def country(self, ip):
        """{"code": "RU", "name": "Россия"}; локальный адрес — LAN; неизвестно — None."""
        if not ip:
            return None
        if ip in self.cache:
            return self.cache[ip]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return None
        res = None
        if addr.is_private or addr.is_loopback:
            res = LAN
        else:
            reader = self._reader()
            if reader:
                try:
                    rec = reader.get(ip) or {}
                except Exception:
                    rec = {}
                c = rec.get("country") or rec.get("registered_country") or {}
                if c.get("iso_code"):
                    names = c.get("names") or {}
                    # названия на языках панели — браузер возьмёт своё (польского в базах нет — будет английское)
                    res = {"code": c["iso_code"], "name": names.get("ru") or names.get("en") or c["iso_code"],
                           "names": {LANGS.get(k, k): v for k, v in names.items() if LANGS.get(k, k) in PANEL_LANGS}}
        self.cache[ip] = res
        return res
