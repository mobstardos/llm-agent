# -*- coding: utf-8 -*-
"""
Реестр и загрузчик парсеров протоколов.

Плагин — обычный .py-файл, определяющий:
    NAME      = "my_protocol"          # уникальное имя (protocol в логах)
    LABEL     = "Мой протокол"         # человекочитаемое название
    PRIORITY  = 50                     # приоритет автодетекта (0..100, выше = раньше)

    def detect(chunk, ctx) -> int        # 0..100 — уверенность, что это ваш протокол
    def create_framer(ctx) -> Framer|None  # None => режим "кусками" (chunk-mode)
    def parse(frame, ctx) -> dict        # разбор кадра -> поля анализа

Загрузка: встроенные парсеры из parsers/*.py + пользовательские из папки
"plugins/" рядом со скриптом (если есть). Пользовательский плагин может
заменить встроенный, если совпадёт NAME.
"""

import importlib
import importlib.util
import os
import sys
import traceback


class FrameError(Exception):
    """Кадр не соответствует протоколу — движок уйдёт в прозрачный режим."""


# Используются в frozen-сборке (PyInstaller), где каталог parsers/ на диске
# отсутствует и список встроенных модулей нельзя получить через os.listdir.
BUILTIN_MODULES = ("hexdump", "json_http", "modbus", "remote_server", "thrift")


class BaseFramer:
    """Накопительный буфер с извлечением полных кадров.

    Наследники реализуют try_extract() -> bytes | None.
    При FrameError движок переключает канал в прозрачный режим
    (данные всегда ретранслируются, разбор — только наблюдение).
    """

    def __init__(self, max_frame=16 * 1024 * 1024, max_buffer=32 * 1024 * 1024):
        self.buf = b""
        self.max_frame = max_frame
        self.max_buffer = max_buffer

    def feed(self, data):
        if not data:
            return []
        self.buf += data
        if len(self.buf) > self.max_buffer:
            raise FrameError("буфер превышен (%d байт)" % len(self.buf))
        frames = []
        while True:
            frame = self.try_extract()
            if frame is None:
                break
            frames.append(frame)
            if len(frames) > 4096:
                raise FrameError("слишком много кадров в одном чанке")
        return frames

    def try_extract(self):
        raise NotImplementedError

    def reset(self):
        self.buf = b""


class ParserAPI:
    """Обёртка загруженного парсера."""

    def __init__(self, name, label, priority, detect, create_framer, parse, source):
        self.name = name
        self.label = label
        self.priority = priority
        self.detect = detect
        self.create_framer = create_framer
        self.parse = parse
        self.source = source

    def __repr__(self):
        return "<Parser %s prio=%d (%s)>" % (self.name, self.priority, self.source)


def _load_module(path):
    mod_name = "sniff_plugin_" + os.path.splitext(os.path.basename(path))[0]
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _wrap(mod, source):
    name = getattr(mod, "NAME", None)
    detect = getattr(mod, "detect", None)
    parse = getattr(mod, "parse", None)
    if not name or not detect or not parse:
        return None
    return ParserAPI(
        name=name,
        label=getattr(mod, "LABEL", name),
        priority=int(getattr(mod, "PRIORITY", 50)),
        detect=detect,
        create_framer=getattr(mod, "create_framer", None),
        parse=parse,
        source=source,
    )


class ParserRegistry:
    def __init__(self):
        self.parsers = {}

    def load_builtin(self, parsers_dir):
        """Встроенные парсеры грузим как модули пакета parsers (относительные
        импорты `from . import BaseFramer` работают корректно).
        В frozen-сборке (PyInstaller) каталога parsers на диске нет —
        берём фиксированный список модулей."""
        package = os.path.basename(os.path.normpath(parsers_dir)) or "parsers"
        try:
            names = [os.path.splitext(fn)[0] for fn in sorted(os.listdir(parsers_dir))
                     if fn.endswith(".py") and not fn.startswith("_")]
        except OSError:
            names = list(BUILTIN_MODULES)
        for mod_name in names:
            try:
                mod = importlib.import_module("%s.%s" % (package, mod_name))
                api = _wrap(mod, "builtin:%s" % mod_name)
                if api:
                    self.parsers[api.name] = api
            except Exception:
                traceback.print_exc()

    def load_user(self, plugins_dir):
        """Пользовательские плагины (могут переопределять встроенные по NAME)."""
        if not os.path.isdir(plugins_dir):
            return []
        loaded = []
        for fn in sorted(os.listdir(plugins_dir)):
            if not fn.endswith(".py") or fn.startswith("_"):
                continue
            path = os.path.join(plugins_dir, fn)
            try:
                mod = _load_module(path)
                api = _wrap(mod, "plugin:%s" % fn)
                if api:
                    self.parsers[api.name] = api
                    loaded.append(api.name)
            except Exception:
                traceback.print_exc()
        return loaded

    def get(self, name):
        return self.parsers.get(name)

    def all(self):
        return sorted(self.parsers.values(), key=lambda p: -p.priority)

    def names(self):
        return sorted(self.parsers.keys())
