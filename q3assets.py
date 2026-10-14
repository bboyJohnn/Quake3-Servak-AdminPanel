#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Оформление «Как в Quake III»: фоны, звуки и музыка — из pk3 самой игры.

Файлов игры в репозитории нет: при каждом запуске панель достаёт из ваших pk3 четыре
текстуры для фона, эмблему для экрана входа и звуки меню (static/q3/, вместе это около
1 МБ), а музыку отдаёт прямо из pk3 — без копирования на диск, с перемоткой. Нет нужного
файла (например, демо-версия игры) — панель просто не предлагает этот фон или трек.
"""

import os
import re
import zipfile

from previews import _paks

# фоны: ключ -> путь в pk3 (pak0.pk3 оригинальной игры)
BACKGROUNDS = [
    ("gothic", "textures/gothic_floor/largerblock3b.jpg"),       # готический камень
    ("tech", "textures/gothic_ceiling/ceilingtech02_a.jpg"),     # металлические балки
    ("space", "env/space1_ft.jpg"),                              # космос с астероидами, как на q3dm17
    ("lava", "textures/liquids/lavahell.jpg"),                   # лава
]
EMBLEM = "textures/sfx/largerblock3b3_pent.jpg"                   # пентаграмма — эмблема экрана входа

# звуки интерфейса: ключ -> путь в pk3
SOUNDS = {
    "menu1": "sound/misc/menu1.wav",           # переход в раздел
    "menu2": "sound/misc/menu2.wav",           # выбор
    "menu3": "sound/misc/menu3.wav",           # закрыть окно
    "menu4": "sound/misc/menu4.wav",           # ошибка
    "fight": "sound/feedback/fight.wav",       # вход в панель
    "prepare": "sound/feedback/prepare.wav",
    "excellent": "sound/feedback/excellent.wav",
    "impressive": "sound/feedback/impressive.wav",
}

# музыка: ключ, вступление (или None), основная часть; играет вступление, потом основная
MUSIC = [
    ("fla1", "music/fla22k_01_intro.wav", "music/fla22k_01_loop.wav"),
    ("fla2", None, "music/fla22k_02.wav"),
    ("fla3", None, "music/fla22k_03.wav"),
    ("fla4", "music/fla22k_04_intro.wav", "music/fla22k_04_loop.wav"),
    ("fla5", None, "music/fla22k_05.wav"),
    ("fla6", None, "music/fla22k_06.wav"),
    ("sonic1", None, "music/sonic1.wav"),
    ("sonic2", None, "music/sonic2.wav"),
    ("sonic3", None, "music/sonic3.wav"),
    ("sonic4", None, "music/sonic4.wav"),
    ("sonic5", None, "music/sonic5.wav"),
    ("sonic6", None, "music/sonic6.wav"),
]

SAFE = re.compile(r"^[a-z0-9_]+\.wav$")


class Assets:
    def __init__(self, pak_dirs, out_dir):
        self.out_dir = out_dir
        self.files = {}          # путь в pk3 (строчными) -> (pk3, ZipInfo); поздний pak главнее
        for pak in _paks(pak_dirs):
            try:
                with zipfile.ZipFile(pak) as z:
                    for info in z.infolist():
                        self.files[info.filename.lower()] = (pak, info)
            except (zipfile.BadZipFile, OSError):
                continue
        self.backgrounds, self.sounds, self.music = [], [], []
        self.emblem = False
        self._extract()
        # музыка: только то, что есть в pk3; отдаётся по имени файла (fla22k_02.wav)
        self.tracks = {}
        for key, intro, main in MUSIC:
            if main in self.files and (intro is None or intro in self.files):
                parts = [p for p in (intro, main) if p]
                for p in parts:
                    self.tracks[os.path.basename(p)] = p
                self.music.append({"key": key, "files": [os.path.basename(p) for p in parts]})

    def _extract(self):
        os.makedirs(self.out_dir, exist_ok=True)
        want = [("bg-%s.jpg" % k, p) for k, p in BACKGROUNDS] + [("emblem.jpg", EMBLEM)]
        want += [("snd-%s.wav" % k, p) for k, p in SOUNDS.items()]
        for name, path in want:
            hit = self.files.get(path)
            if not hit:
                continue
            pak, info = hit
            dst = os.path.join(self.out_dir, name)
            try:
                if not (os.path.exists(dst) and os.path.getsize(dst) == info.file_size):
                    with zipfile.ZipFile(pak) as z, open(dst + ".tmp", "wb") as f:
                        f.write(z.read(info))
                    os.replace(dst + ".tmp", dst)
            except (zipfile.BadZipFile, OSError):
                continue
            if name.startswith("bg-"):
                self.backgrounds.append(name[3:-4])
            elif name.startswith("snd-"):
                self.sounds.append(name[4:-4])
            else:
                self.emblem = True

    def site(self):
        """Что из оформления есть — для браузера."""
        return {"backgrounds": self.backgrounds, "emblem": self.emblem,
                "sounds": self.sounds, "music": self.music}

    # --- музыка прямо из pk3 ----------------------------------------------

    def track(self, name):
        """(путь к pk3, смещение данных, размер) для файла музыки или None.
        Музыка в pk3 лежит без сжатия — её байты можно отдавать прямо из архива."""
        if not SAFE.match(name or "") or name not in self.tracks:
            return None
        pak, info = self.files[self.tracks[name]]
        if info.compress_type != zipfile.ZIP_STORED:
            return None
        try:
            with open(pak, "rb") as f:
                f.seek(info.header_offset)
                head = f.read(30)
                if head[:4] != b"PK\x03\x04":
                    return None
                name_len = int.from_bytes(head[26:28], "little")
                extra_len = int.from_bytes(head[28:30], "little")
        except OSError:
            return None
        return pak, info.header_offset + 30 + name_len + extra_len, info.file_size
