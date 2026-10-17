# -*- coding: utf-8 -*-
"""
Консольный вывод сниффера: цветные строки пакетов, событий и тревог.
Работает и в Windows (включение ANSI через os.system('')), и в Linux.
Вывод никогда не должен ломать перехват: любые ошибки глушатся.
"""

import sys
import threading
import time
from datetime import datetime


def enable_ansi():
    """Включает ANSI-коды на Windows-консоли (Windows 10+), на Linux — no-op."""
    if sys.platform == "win32":
        try:
            # Активирует обработку VT-последовательностей в conhost
            import os
            os.system("")
        except Exception:
            pass
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


class C:
    RESET = "\033[0m"
    DIM = "\033[2m"
    BOLD = "\033[1m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"
    GRAY = "\033[90m"
    BRIGHT_RED = "\033[91m"
    BRIGHT_GREEN = "\033[92m"
    BRIGHT_YELLOW = "\033[93m"
    BRIGHT_CYAN = "\033[96m"


class ConsolePrinter:
    """Печатает события в консоль согласно уровню детализации."""

    LEVELS = ("quiet", "summary", "full")

    def __init__(self, verbosity="summary", colors=True):
        self.verbosity = verbosity if verbosity in self.LEVELS else "summary"
        self.colors = colors
        self.lock = threading.Lock()
        enable_ansi()

    def set_verbosity(self, v):
        if v in self.LEVELS:
            self.verbosity = v

    # ---- внутреннее ----

    def _w(self, text, color=None):
        try:
            if self.colors and color:
                text = color + text + C.RESET
            with self.lock:
                sys.stdout.write(text + "\n")
                sys.stdout.flush()
        except Exception:
            pass

    @staticmethod
    def _ts(ts=None):
        t = ts if ts else time.time()
        return datetime.fromtimestamp(t).strftime("%H:%M:%S.%f")[:-3]

    @staticmethod
    def _fmt_size(n):
        if n >= 1024 * 1024:
            return "%.1fMB" % (n / 1048576.0)
        if n >= 1024:
            return "%.1fKB" % (n / 1024.0)
        return "%dB" % n

    # ---- события ----

    def startup(self, title, lines):
        self._w("=" * 78, C.CYAN)
        self._w("  " + title, C.BOLD + C.CYAN)
        for ln in lines:
            self._w("  " + ln)
        self._w("=" * 78, C.CYAN)

    def banner(self, text, color=None):
        self._w("-" * 78, C.GRAY)
        self._w(text, color or C.CYAN)
        self._w("-" * 78, C.GRAY)

    def packet(self, a):
        """a — analysis dict после парсера. Одна строка (+детали в full)."""
        if self.verbosity == "quiet":
            return
        try:
            ts = self._ts(a.get("ts"))
            if a.get("direction") == "TX":
                arrow, color = "->", C.GREEN
            else:
                arrow, color = "<-", C.BLUE
            proto = a.get("protocol", "RAW")
            line = "[%s] %s %s:%s [%s] %s  %s" % (
                ts, arrow, a.get("client", "?"), a.get("client_port", "?"),
                a.get("port_name", "?"), proto,
                a.get("summary", "") or ("valid" if a.get("valid") else "invalid"),
            )
            self._w(line, color)
            if self.verbosity == "full":
                extra = []
                if a.get("tables"):
                    extra.append("TABLES: " + ", ".join(a["tables"]))
                if a.get("strings"):
                    extra.append("DATA: " + " | ".join(map(str, a["strings"][:5])))
                if a.get("fields"):
                    extra.append("FIELDS: " + ", ".join(
                        "%s=%s" % (f.get("name") or f.get("id"), f.get("value")) for f in a["fields"][:8]))
                for e in extra:
                    self._w("        " + e, C.GRAY)
        except Exception:
            pass

    def connect(self, a):
        self._w("[%s] ++ %s:%s -> %s:%s (%s)%s" % (
            self._ts(), a.get("client"), a.get("client_port"),
            a.get("target_host"), a.get("target_port"), a.get("port_name"),
            "" if a.get("ok") else "  [ОШИБКА ПОДКЛЮЧЕНИЯ К ЦЕЛИ: %s]" % a.get("error", ""),
        ), C.MAGENTA if a.get("ok") else C.BRIGHT_RED)

    def disconnect(self, a):
        self._w("[%s] -- %s:%s (%s)  tx=%s rx=%s  %s" % (
            self._ts(), a.get("client"), a.get("client_port"), a.get("port_name"),
            a.get("packets_tx", 0), a.get("packets_rx", 0),
            a.get("reason", ""),
        ), C.GRAY)

    def detected(self, a):
        self._w("[%s] ?? автодетект %s: %s (уверенность %s%%)" % (
            self._ts(), a.get("port_name"), a.get("protocol"), a.get("confidence"),
        ), C.YELLOW)

    def alert(self, al):
        self._w("[%s] !! ТРЕВОГА [%s] %s — %s" % (
            self._ts(), al.get("severity", "warn").upper(), al.get("rule"), al.get("detail", ""),
        ), C.BRIGHT_RED if al.get("severity") == "crit" else C.BRIGHT_YELLOW)

    def info(self, text, color=None):
        self._w("[%s] i %s" % (self._ts(), text), color or C.CYAN)

    def error(self, text):
        self._w("[%s] ! ОШИБКА: %s" % (self._ts(), text), C.BRIGHT_RED)

    def stats_line(self, snap):
        if self.verbosity == "quiet":
            return
        t = snap.get("totals", {})
        self._w("[%s] ## статистика: %s пак., %s, сессий: %s активных / %s всего" % (
            self._ts(), t.get("packets", 0), self._fmt_size(t.get("bytes", 0)),
            snap.get("sessions_active", 0), t.get("connections", 0)), C.BOLD)

    def final_stats(self, hub, exports_info):
        try:
            snap = hub.stats_snapshot()
            self.banner("ИТОГОВАЯ СТАТИСТИКА", C.CYAN)
            t = snap["totals"]
            elapsed = snap["uptime_sec"]
            self._w("  Время работы : %.0f с" % elapsed)
            self._w("  Пакетов      : %s (%s TX / %s RX)" % (t["packets"], t["tx_packets"], t["rx_packets"]))
            self._w("  Объём        : %s TX / %s RX" % (self._fmt_size(t["tx_bytes"]), self._fmt_size(t["rx_bytes"])))
            self._w("  Соединений   : %s" % t["connections"])
            if snap["per_proto"]:
                self._w("  По протоколам:")
                for k, v in sorted(snap["per_proto"].items(), key=lambda kv: -kv[1]["packets"]):
                    self._w("    %-18s %8s пак.  %s" % (k, v["packets"], self._fmt_size(v["bytes"])))
            top = snap.get("top_types") or []
            if top:
                self._w("  Топ команд/методов:")
                for it in top[:10]:
                    self._w("    %-28s %s" % (it["type"], it["count"]))
            if exports_info:
                self._w("  Файлы захвата:")
                for ln in exports_info:
                    self._w("    " + ln)
            self._w("=" * 78, C.CYAN)
        except Exception:
            pass
