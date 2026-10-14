#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Картинки карт для панели — снимки уровней из самой игры.

В pk3-архивах Quake III лежат `levelshots/<карта>.jpg` — те же картинки, что
показывает меню игры. Панель при запуске достаёт их в static/maps/ (более поздний
pak перекрывает ранний, как в движке) и дальше отдаёт как обычные файлы. Сами
картинки в репозиторий не входят: они из игры и берутся из ваших pk3.

Размер у оригинальных карт id — 128×128, у карт «pro» из патча — 512×512.
TGA браузеры не показывают — такие снимки пропускаются.
"""

import glob
import os
import re
import zipfile


def _paks(dirs):
    """pk3 по порядку: папки — как заданы (сначала fs_basepath, потом fs_homepath),
    внутри папки — по имени, как их читает движок; поздний перекрывает ранний."""
    if isinstance(dirs, str):
        dirs = [dirs]
    out = []
    for d in dirs:
        out += sorted(glob.glob(os.path.join(d, "*.pk3")))
    return out


def extract(dirs, out_dir):
    """Достаёт снимки карт. Возвращает отсортированный список карт с картинкой."""
    os.makedirs(out_dir, exist_ok=True)
    found = {}
    for pak in _paks(dirs):
        try:
            with zipfile.ZipFile(pak) as z:
                for info in z.infolist():
                    m = re.match(r"(?i)^levelshots/([A-Za-z0-9_-]+)\.jpe?g$", info.filename)
                    if m:
                        found[m.group(1).lower()] = (pak, info)
        except (zipfile.BadZipFile, OSError):
            continue
    for name, (pak, info) in found.items():
        dst = os.path.join(out_dir, name + ".jpg")
        try:
            if os.path.getsize(dst) == info.file_size:
                continue  # уже на месте
        except OSError:
            pass
        with zipfile.ZipFile(pak) as z, open(dst + ".tmp", "wb") as f:
            f.write(z.read(info))
        os.replace(dst + ".tmp", dst)
    return sorted(found)


# Слова из поля type в .arena -> ключи режимов панели (cfg.ROTATIONS).
# «single» — одиночная кампания, а это и есть «все против всех» с ботами: у q3dm0
# других слов нет, без этого она считалась бы картой без описания.
ARENA_TYPES = {"ffa": "f", "single": "f", "tourney": "tour", "team": "tdm", "ctf": "c"}


def arenas(dirs):
    """Описания карт из scripts/arenas.txt и scripts/*.arena — те же, по которым
    меню игры решает, в каких режимах показывать карту.
    Возвращает {карта: {"name": длинное имя, "modes": [ключи режимов]}}.

    Это рекомендация авторов, а не запрет: в «Все против всех» и «Дуэлях» играется
    любая карта, а вот без флагов (не CTF-карта) захват флага не получится.
    """
    out = {}
    for pak in _paks(dirs):
        try:
            z = zipfile.ZipFile(pak)
        except (zipfile.BadZipFile, OSError):
            continue
        with z:
            for n in z.namelist():
                if not re.match(r"(?i)^scripts/(arenas\.txt|[^/]+\.arena)$", n):
                    continue
                text = z.read(n).decode("latin-1")
                for block in re.findall(r"\{(.*?)\}", text, re.S):
                    kv = {k.lower(): v.strip() for k, v in re.findall(r'(?m)^\s*(\w+)\s+"?([^"\n]*)"?', block)}
                    if not kv.get("map"):
                        continue
                    modes = []
                    for t in kv.get("type", "").lower().split():
                        if t in ARENA_TYPES and ARENA_TYPES[t] not in modes:
                            modes.append(ARENA_TYPES[t])
                    out[kv["map"].lower()] = {"name": kv.get("longname", ""), "modes": modes}
    return out
