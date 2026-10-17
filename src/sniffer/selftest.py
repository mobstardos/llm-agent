#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Самопроверка вендоренного сниффера (без внешних зависимостей и сети).

Что делает:
  1. Поднимает локальный эхо-сервер на 127.0.0.1:19001.
  2. Запускает движок сниффера (Engine, in-process) как прокси
     127.0.0.1:19000 -> 127.0.0.1:19001.
  3. Генерирует несколько TCP-соединений с полезной нагрузкой
     (HTTP-текст, JSON, бинарный мусор) и читает эхо.
  4. Проверяет, что Hub накопил распарсенные пакеты
     (hub.recent_packets() > 0 и stats totals > 0).

Запуск из корня проекта:
    python -m src.sniffer.selftest

Печатает PASS/FAIL; код выхода 0/1.
"""

import os
import shutil
import socket
import sys
import tempfile
import threading
import time

# Каталог пакета — в sys.path: те же «плоские» импорты, что и в __main__.py
# (sniffcore / parsers как top-level), чтобы движок и парсеры были ровно
# теми же модулями, что использует `python -m src.sniffer`.
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)

from sniffcore import __version__  # noqa: E402
from sniffcore.hub import Hub  # noqa: E402
from sniffcore.console import ConsolePrinter  # noqa: E402
from sniffcore.exports import ExportManager  # noqa: E402
from sniffcore.alerts import AlertEngine  # noqa: E402
from sniffcore.engine import Engine  # noqa: E402
from parsers import ParserRegistry  # noqa: E402

LISTEN_PORT = 19000
TARGET_PORT = 19001
HOST = "127.0.0.1"

# Порты по заданию — 19000/19001. Если заняты (например, демо-панелью),
# selftest не падает, а берёт свободные эфемерные порты.

def _pick_free_port(preferred):
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        probe.bind((HOST, preferred))
        return preferred
    except OSError:
        probe.close()
        tmp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tmp.bind((HOST, 0))
        free = tmp.getsockname()[1]
        tmp.close()
        return free
    finally:
        try:
            probe.close()
        except Exception:
            pass


def _echo_server(stop_event, started_event, errors, port_holder):
    """Простой TCP-эхо-сервер: принимает соединения, возвращает всё, что получил."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        srv.bind((HOST, port_holder["target"]))
        srv.listen(16)
    except Exception as e:
        errors.append("эхо-сервер: не удалось занять порт %d: %s"
                      % (port_holder["target"], e))
        started_event.set()
        return
    port_holder["bound"] = True
    srv.settimeout(0.3)
    started_event.set()
    while not stop_event.is_set():
        try:
            conn, _ = srv.accept()
        except socket.timeout:
            continue
        except OSError:
            break

        def _handle(c):
            try:
                c.settimeout(2.0)
                while True:
                    data = c.recv(65536)
                    if not data:
                        break
                    c.sendall(data)
            except OSError:
                pass
            finally:
                try:
                    c.close()
                except Exception:
                    pass

        threading.Thread(target=_handle, args=(conn,), daemon=True).start()
    srv.close()


def _exchange(payload):
    """Открывает соединение через прокси, шлёт payload, возвращает эхо."""
    with socket.create_connection((HOST, LISTEN_PORT), timeout=5) as s:
        s.settimeout(5.0)
        s.sendall(payload)
        received = b""
        while len(received) < len(payload):
            chunk = s.recv(65536)
            if not chunk:
                break
            received += chunk
        return received


def main():
    print("Universal Sniffer selftest v%s" % __version__)
    tmp_dir = tempfile.mkdtemp(prefix="sniffer-selftest-")
    errors = []

    target_port = _pick_free_port(TARGET_PORT)
    listen_port = _pick_free_port(LISTEN_PORT)
    if (target_port, listen_port) != (TARGET_PORT, LISTEN_PORT):
        print("note: порты %d/%d заняты — использую %d/%d"
              % (LISTEN_PORT, TARGET_PORT, listen_port, target_port))
    port_holder = {"target": target_port, "bound": False}

    stop_event = threading.Event()
    started_event = threading.Event()
    echo_thread = threading.Thread(
        target=_echo_server, args=(stop_event, started_event, errors, port_holder),
        daemon=True, name="selftest-echo")
    echo_thread.start()
    started_event.wait(3.0)
    if errors or not port_holder["bound"]:
        print("FAIL: %s" % ("; ".join(errors) or "эхо-сервер не поднялся"))
        return 1

    cfg = {
        "listen_host": HOST,
        "ports": [{"listen": listen_port, "target_host": HOST,
                   "target_port": target_port, "name": "selftest",
                   "parser": "auto"}],
        "web": {"enabled": False, "host": HOST, "port": 9500},
        "console": {"enabled": True, "colors": False, "verbosity": "quiet"},
        "detection": {"mode": "auto", "fallback": "hexdump"},
        "buffers": {"recv_size": 65536, "connect_timeout": 10},
        "storage": {
            "dir": tmp_dir,
            "jsonl": False, "jsonl_rotation_mb": 50, "jsonl_keep": 1,
            "sqlite": False, "sqlite_file": os.path.join(tmp_dir, "traffic.db"),
            "sqlite_retention_rows": 1000,
            "pcap": False, "pcap_file": os.path.join(tmp_dir, "traffic.pcap"),
            "pcap_rotation_mb": 10, "pcap_keep": 1,
            "csv_reports": False, "reports_dir": os.path.join(tmp_dir, "reports"),
            "text_logs_per_client": False, "clients_dir": os.path.join(tmp_dir, "clients"),
        },
        "alerts": {
            "cooldown_sec": 0,
            "webhook_url": "",
            "silence_sec": 0,
            "alerts_file": os.path.join(tmp_dir, "alerts.jsonl"),
            "rules": [],
        },
    }

    console = ConsolePrinter("quiet", False)
    hub = Hub()
    exports = ExportManager(cfg, on_error=lambda m: errors.append(str(m)),
                            on_status=lambda m: None)
    registry = ParserRegistry()
    registry.load_builtin(os.path.join(_PKG_DIR, "parsers"))
    alerts = AlertEngine(cfg.get("alerts", {}), console, hub, on_status=lambda m: None)
    engine = Engine(cfg, hub, console, exports, alerts, registry)
    engine.start()
    time.sleep(0.5)  # даём слушателю подняться

    payloads = [
        ("HTTP", b"GET /api/packets?limit=10 HTTP/1.1\r\n"
                 b"Host: selftest.local\r\nUser-Agent: selftest\r\n"
                 b"Accept: application/json\r\n\r\n"),
        ("JSON", b'{"jsonrpc": "2.0", "id": 1, "method": "tools/list", '
                 b'"params": {}}'),
        ("TEXT", b"hello selftest packet\n"),
        ("BIN", bytes(range(0, 64))),
    ]

    echo_ok = 0
    try:
        for name, payload in payloads:
            try:
                echo = _exchange(payload)
                if echo == payload:
                    echo_ok += 1
                else:
                    errors.append("%s: эхо не совпало (%d/%d байт)"
                                  % (name, len(echo), len(payload)))
            except Exception as e:
                errors.append("%s: обмен не удался: %s" % (name, e))

        # даём насосам направления и SQLite-очередям (если бы были) обработать
        deadline = time.time() + 10
        while time.time() < deadline:
            if hub.stats_snapshot()["totals"]["packets"] >= len(payloads) * 2:
                break
            time.sleep(0.2)

        stats = hub.stats_snapshot()
        packets = hub.recent_packets(100)
        totals = stats["totals"]
        protos = {k: v["packets"] for k, v in stats["per_proto"].items()}

        print("proxy          : %s:%d -> %s:%d" % (HOST, listen_port, HOST, target_port))
        print("echo round-trip: %d/%d" % (echo_ok, len(payloads)))
        print("packets parsed : %d (totals %d)" % (len(packets), totals["packets"]))
        print("by protocol    : %s" % protos)
        print("parsers        : %s" % ", ".join(registry.names()))

        ok = (echo_ok == len(payloads)
              and len(packets) > 0
              and totals["packets"] >= len(payloads) * 2  # TX + RX
              and totals["connections"] >= len(payloads))
        if errors:
            ok = False

        if ok:
            print("PASS: сниффер перехватил и разобрал трафик, эхо-канал прозрачен")
            return 0
        print("FAIL: %s" % ("; ".join(errors) if errors else
                            "условия не выполнены (packets=%d, totals=%s)"
                            % (len(packets), totals)))
        return 1
    finally:
        engine.stop()
        stop_event.set()
        echo_thread.join(timeout=2.0)
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
