#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Universal Sniffer v3.1 — тонкий CLI-обёртка над вендоренным сниффером.

Адаптация оригинального sniffer.py для запуска внутри llm-agent:
    python -m src.sniffer                          # конфиг src/sniffer/config.json
    python -m src.sniffer --config my.json         # свой конфиг
    python -m src.sniffer --ports 3003:3004,10010:10011

Импорты оставлены «плоскими» (sniffcore, parsers), как в оригинале: каталог
этого пакета добавляется в sys.path, поэтому:
  * ParserRegistry.load_builtin() резолвит встроенные парсеры через
    importlib.import_module("parsers.<имя>") — имя пакета совпадает с
    basename каталога (см. parsers/__init__.py);
  * sniffcore/webserver.py вычисляет WEBUI_DIR от собственного расположения
    (<пакет>/webui) — путь корректен без изменений;
  * app_base() возвращает каталог пакета (или USNIFF_ROOT, если задан) —
    рядом создаются capture/, plugins/ и config.json.

Служба Windows (--service ...) сохранена как в оригинале (только Windows).
Подробнее — README.md рядом с этим файлом и docs/SNIFFER_INTEGRATION.md.
"""

import argparse
import json
import os
import sys
import threading
import time
from datetime import datetime

# Оригинальный подход sniffer.py: каталог пакета — в sys.path, чтобы
# `sniffcore` и `parsers` импортировались как top-level пакеты.
# Это сохраняет совместимость с динамической загрузкой парсеров
# (parsers/__init__.py → importlib.import_module("parsers.<mod>")).
_PKG_DIR = os.path.dirname(os.path.abspath(__file__))
if _PKG_DIR not in sys.path:
    sys.path.insert(0, _PKG_DIR)

from sniffcore import __version__
from sniffcore.hub import Hub
from sniffcore.console import ConsolePrinter
from sniffcore.exports import ExportManager
from sniffcore.alerts import AlertEngine
from sniffcore.engine import Engine
from sniffcore.webserver import PanelState, start_web
from parsers import ParserRegistry

DEFAULT_CONFIG = {
    # v2: по умолчанию слушаем только localhost; наружу — осознанно
    # через config.json / --config (с АЗС-сетью это отдельный сценарий).
    "listen_host": "127.0.0.1",
    "ports": [
        {"listen": 9000, "target_host": "127.0.0.1", "target_port": 9001,
         "name": "RemoteServer TCP", "parser": "auto"},
        {"listen": 9010, "target_host": "127.0.0.1", "target_port": 9011,
         "name": "Thrift RPC", "parser": "auto"},
    ],
    "web": {"enabled": True, "host": "127.0.0.1", "port": 9500},
    "console": {"enabled": True, "colors": True, "verbosity": "summary"},
    "detection": {"mode": "auto", "fallback": "hexdump"},
    "buffers": {"recv_size": 65536, "connect_timeout": 10},
    "storage": {
        "dir": "capture",
        "jsonl": True, "jsonl_rotation_mb": 50, "jsonl_keep": 5,
        "sqlite": True, "sqlite_file": "capture/traffic.db",
        "sqlite_retention_rows": 500000,
        "pcap": True, "pcap_file": "capture/traffic.pcap",
        "pcap_rotation_mb": 200, "pcap_keep": 5,
        "csv_reports": True, "reports_dir": "capture/reports",
        "text_logs_per_client": False, "clients_dir": "capture/clients",
    },
    "alerts": {
        "cooldown_sec": 30,
        "webhook_url": "",
        "silence_sec": 0,
        "alerts_file": "capture/alerts.jsonl",
        "rules": [
            {"name": "Thrift EXCEPTION", "severity": "crit",
             "match": {"protocol": "THRIFT", "msg_type": "EXCEPTION"},
             "actions": ["console", "web", "sound"]},
            {"name": "Откат безналичной оплаты", "severity": "crit",
             "match": {"method_type": "CASHLESS_ROLLBACK"},
             "threshold": {"count": 2, "window_sec": 60},
             "actions": ["console", "web", "sound"]},
            {"name": "Гигантский пакет", "severity": "warn",
             "size_gt": 1000000, "actions": ["console", "web"]},
            {"name": "Ошибка БД в запросе", "severity": "warn",
             "match": {"cmd_type": "DB_REQUEST"}, "keyword": "error",
             "actions": ["console", "web"]},
        ],
    },
}

USAGE_HINT = [
    "Порты по умолчанию: 9000->9001, 9010->9011, веб-панель 9500.",
    "Свои порты — любой из способов:",
    "  1. config.json -> секция ports (listen/target_port для каждой пары) и web.port;",
    "  2. ключ --ports 9003:9004,10013:10014 (цель = порт+1, если без двоеточия);",
    "  3. ключ --web-port 8080 для порта панели.",
    "",
    "Сценарий с АЗС (как в оригинальном multi_sniff.py):",
    "  python -m src.sniffer --ports 3003:3004,10010:10011",
    "  1. Остановите RemoteServer.",
    "  2. Запустите RemoteServer на портах-преемниках (3004 и 10011).",
    "  3. Сниффер слушает старые порты (3003, 10010), перенаправляет трафик на новые",
    "     и показывает всё в консоли и на веб-панели.",
    "  4. АЗС продолжают подключаться к старым портам как обычно.",
    "",
    "Переменная окружения USNIFF_ROOT задаёт каталог для capture/ и plugins/",
    "(MCP-сервер sniffer ставит её в <проект>/data/sniffer автоматически).",
]


# ------------------------------------------------------------------ конфиг

def app_base():
    """Каталог приложения: папка пакета sniffer; в frozen-сборке (PyInstaller /
    лаунчер exe) — папка, где лежит исполняемый файл. Переменная окружения
    USNIFF_ROOT (задаётся лаунчером портативной сборки или MCP-сервером
    sniffer) имеет высший приоритет. Рядом с этой папкой создаются capture/,
    config.json и plugins/."""
    env = os.environ.get("USNIFF_ROOT")
    if env:
        return env
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return _PKG_DIR


def load_config(path):
    with open(path, "r", encoding="utf-8") as f:
        user = json.load(f)
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))  # глубокая копия базовых значений
    _merge(cfg, user)
    return cfg


def _merge(base, user):
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v


def make_config(path):
    if os.path.exists(path):
        print("Файл %s уже существует — не перезаписываю." % path)
        return False
    with open(path, "w", encoding="utf-8") as f:
        json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
    print("Шаблон конфига сохранён: %s" % path)
    print("Отредактируйте его и запустите: python -m src.sniffer --config %s" % path)
    return True


def validate_config(cfg):
    errors = []
    if not cfg.get("ports"):
        errors.append("не задан ни один порт (ports)")
    for p in cfg.get("ports", []):
        for key in ("listen", "target_host", "target_port"):
            if key not in p:
                errors.append("порт %s: нет поля '%s'" % (p, key))
    if not (1 <= int(cfg.get("web", {}).get("port", 9500)) <= 65535):
        errors.append("некорректный web.port")
    return errors


def anchor_paths(cfg):
    """Относительные пути файлов захвата привязываем к папке приложения
    (в frozen-сборке — к папке exe), а не к текущему каталогу запуска."""
    base = app_base()
    storage = cfg.get("storage", {})
    for key in ("dir", "sqlite_file", "pcap_file", "reports_dir", "clients_dir"):
        v = storage.get(key)
        if isinstance(v, str) and v and not os.path.isabs(v):
            storage[key] = os.path.normpath(os.path.join(base, v))
    v = cfg.get("alerts", {}).get("alerts_file")
    if isinstance(v, str) and v and not os.path.isabs(v):
        cfg["alerts"]["alerts_file"] = os.path.normpath(os.path.join(base, v))


def parse_ports_arg(s, target_host):
    """'3003:3004,10010:10011' или '3003,10010' (цель = порт+1)."""
    ports = []
    for part in s.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            listen, target = part.split(":", 1)
            listen, target = int(listen), int(target)
        else:
            listen = int(part)
            target = listen + 1
        ports.append({"listen": listen, "target_host": target_host,
                      "target_port": target, "name": "PORT_%d" % listen,
                      "parser": "auto"})
    return ports


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser(
        prog="python -m src.sniffer",
        description="Universal Sniffer — прозрачный TCP-прокси с разбором протоколов, "
                    "веб-панелью, экспортом JSONL/SQLite/PCAP/CSV и тревогами.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join(USAGE_HINT))
    ap.add_argument("--config", default="config.json", help="путь к конфигу (по умолчанию config.json)")
    ap.add_argument("--ports", help="список прослушиваемых портов: 3003:3004,10010:10011 "
                                    "(переопределяет config)")
    ap.add_argument("--target-host", default="127.0.0.1", help="хост-цель для --ports")
    ap.add_argument("--web-port", type=int, help="порт веб-панели (переопределяет config)")
    ap.add_argument("--no-web", action="store_true", help="выключить веб-панель")
    ap.add_argument("--verbosity", choices=["quiet", "summary", "full"],
                    help="детализация консоли")
    ap.add_argument("--no-color", action="store_true", help="без ANSI-цветов")
    ap.add_argument("--make-config", metavar="FILE", help="сгенерировать шаблон конфига и выйти")
    ap.add_argument("--service", metavar="ACTION",
                    choices=["install", "remove", "start", "stop", "restart", "status", "run"],
                    help="служба Windows: install/remove/start/stop/restart/status "
                         "(run — служебный режим, вызывается диспетчером служб)")
    ap.add_argument("--root", help=argparse.SUPPRESS)  # внутренний: корень установки для службы
    ap.add_argument("--check-config", action="store_true", help="проверить конфиг и выйти")
    ap.add_argument("--open-browser", action="store_true",
                    help="открыть веб-панель в браузере при старте")
    ap.add_argument("--version", action="version", version="Universal Sniffer %s" % __version__)
    args = ap.parse_args()

    if args.make_config:
        sys.exit(0 if make_config(args.make_config) else 1)

    if args.root:
        os.environ["USNIFF_ROOT"] = os.path.abspath(args.root)

    if args.service:
        if args.service == "run":
            sys.exit(_service_run_mode(args))
        sys.exit(handle_service(args))

    # загрузка конфига; в frozen-сборке ищем config.json ещё и рядом с exe
    cfg_path = resolve_config_path(args.config)
    if os.path.exists(cfg_path):
        try:
            cfg = load_config(cfg_path)
        except Exception as e:
            print("ОШИБКА: не удалось прочитать конфиг %s: %s" % (cfg_path, e))
            sys.exit(1)
    else:
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))
        if os.path.abspath(args.config) != os.path.abspath("config.json"):
            print("Конфиг %s не найден — работаю со встроенными значениями по умолчанию." % cfg_path)

    if args.ports:
        cfg["ports"] = parse_ports_arg(args.ports, args.target_host)
    if args.web_port:
        cfg["web"]["port"] = args.web_port
    if args.no_web:
        cfg["web"]["enabled"] = False
    if args.verbosity:
        cfg["console"]["verbosity"] = args.verbosity
    if args.no_color:
        cfg["console"]["colors"] = False

    anchor_paths(cfg)

    errors = validate_config(cfg)
    if errors:
        for e in errors:
            print("ОШИБКА КОНФИГА: %s" % e)
        print("Проверьте конфиг: python -m src.sniffer --check-config")
        sys.exit(1)

    if args.check_config:
        print("Конфиг корректен: %s" % (cfg_path if os.path.exists(cfg_path) else "<встроенный>"))
        print(json.dumps(cfg, ensure_ascii=False, indent=2)[:2000])
        sys.exit(0)

    run_app(args, cfg, cfg_path, stop_event=None)


# ------------------------------------------------------------------ приложение

def resolve_config_path(cfg_path):
    """Абсолютный путь к конфигу; в frozen-сборке ищем config.json рядом с exe."""
    if (getattr(sys, "frozen", False) and not os.path.isabs(cfg_path)
            and not os.path.exists(cfg_path)):
        alt = os.path.join(app_base(), cfg_path)
        if os.path.exists(alt):
            cfg_path = alt
    return os.path.abspath(cfg_path)


def _service_run_mode(args):
    """Служебный режим службы Windows (--service run, вызывается SCM):
    логи в capture/service.log, регистрация в диспетчере служб,
    мягкая остановка по событию."""
    from sniffcore import winservice

    cfg_path = resolve_config_path(args.config)
    if os.path.exists(cfg_path):
        try:
            cfg = load_config(cfg_path)
        except Exception as e:
            print("ОШИБКА: не удалось прочитать конфиг %s: %s" % (cfg_path, e))
            return 1
    else:
        cfg = json.loads(json.dumps(DEFAULT_CONFIG))

    cfg["console"]["colors"] = False
    log_dir = os.path.join(app_base(), "capture")
    try:
        os.makedirs(log_dir, exist_ok=True)
    except Exception:
        pass
    try:
        f = open(os.path.join(log_dir, "service.log"), "a",
                 buffering=1, encoding="utf-8", errors="replace")
        sys.stdout = f
        sys.stderr = f
        print("\n[%s] === Universal Sniffer: запуск службы ===" %
              datetime.now().isoformat(timespec="seconds"))
    except Exception:
        pass

    def svc_main(ev):
        run_app(args, cfg, cfg_path, stop_event=ev)

    try:
        winservice.run_service(svc_main)
    except (OSError, winservice.ServiceError) as e:
        print("ОШИБКА: %s" % e)
        return 1
    return 0


def handle_service(args):
    """Управление службой Windows. Возвращает код выхода."""
    from sniffcore import winservice
    if os.name != "nt":
        print("Управление службой Windows доступно только на Windows (здесь: %s)." % sys.platform)
        print("Автозапуск на Linux: systemd — см. README.md, раздел «Служба Windows».")
        return 1
    action = args.service
    if action == "run":
        return _service_run_mode(args)
    try:
        if action == "install":
            root = app_base()
            cfg_abs = resolve_config_path(args.config)
            if os.path.exists(cfg_abs):
                errs = validate_config(load_config(cfg_abs))
                if errs:
                    for e in errs:
                        print("ОШИБКА КОНФИГА: %s" % e)
                    return 1
            else:
                print("ВНИМАНИЕ: конфиг %s не найден — служба будет работать со встроенными "
                      "значениями по умолчанию (порты 9000->9001, 9010->9011, панель 9500)." % cfg_abs)
            binpath = winservice.install(root, cfg_abs)
            print("Служба установлена: автозапуск при загрузке Windows, перезапуск при сбоях.")
            print("  Имя службы : %s" % winservice.SERVICE_NAME)
            print("  Команда    : %s" % binpath)
            print("Запустить службу    : --service start")
            print("Сменить порты       : правьте config.json, затем --service restart")
            print("Логи службы         : capture\\service.log")
            return 0
        if action == "remove":
            winservice.remove()
            print("Служба удалена.")
            return 0
        if action == "start":
            winservice.start()
            print("Служба запущена. " + winservice.status_text())
            return 0
        if action == "stop":
            winservice.stop()
            print("Служба остановлена.")
            return 0
        if action == "restart":
            winservice.restart()
            print("Служба перезапущена. " + winservice.status_text())
            return 0
        if action == "status":
            print(winservice.status_text())
            return 0
        print("Неизвестное действие: %s" % action)
        return 1
    except winservice.ServiceError as e:
        print("ОШИБКА: %s" % e)
        print("Подсказка: установку/удаление/запуск службы выполняйте с правами администратора.")
        return 1


def run_app(args, cfg, cfg_path, stop_event=None):
    """Создаёт компоненты, запускает движок и веб-панель, работает до остановки
    (Ctrl+C или событие остановки службы), затем корректно завершается."""
    # --- компоненты ---
    console = ConsolePrinter(cfg["console"].get("verbosity", "summary"),
                             cfg["console"].get("colors", True))
    hub = Hub()

    def on_status(msg):
        console.info(msg)

    def on_error(msg):
        console.error(msg)

    exports = ExportManager(cfg, on_error=on_error, on_status=on_status)

    registry = ParserRegistry()
    parsers_dir = os.path.join(_PKG_DIR, "parsers")
    registry.load_builtin(parsers_dir)
    plugins_dir = os.path.join(app_base(), "plugins")
    user_plugins = registry.load_user(plugins_dir)
    console.info("Парсеры: %s%s" % (", ".join(registry.names()),
                                    " (+ плагины: %s)" % ", ".join(user_plugins) if user_plugins else ""))

    alerts = AlertEngine(cfg.get("alerts", {}), console, hub, on_status=on_status)

    # горячая перезагрузка конфига (правила тревог, консоль)
    cfg_mtime = [os.path.getmtime(cfg_path) if os.path.exists(cfg_path) else 0]

    def reload_cb():
        if not os.path.exists(cfg_path):
            return "конфиг-файл не найден"
        m = os.path.getmtime(cfg_path)
        if m == cfg_mtime[0]:
            return "конфиг не менялся"
        new_cfg = load_config(cfg_path)
        alerts.reload_config(new_cfg.get("alerts", {}))
        console.set_verbosity(new_cfg.get("console", {}).get("verbosity", "summary"))
        cfg_mtime[0] = m
        return "конфиг перечитан: правила тревог и консоль обновлены"

    def config_watcher():
        while True:
            time.sleep(10)
            try:
                if os.path.exists(cfg_path) and os.path.getmtime(cfg_path) != cfg_mtime[0]:
                    console.info("Обнаружено изменение %s — %s" % (cfg_path, reload_cb()))
            except Exception:
                pass

    engine = Engine(cfg, hub, console, exports, alerts, registry)

    # --- старт ---
    lines = [
        "Конфиг       : %s" % (cfg_path if os.path.exists(cfg_path) else "<встроенный>"),
        "Веб-панель   : %s" % ("http://%s:%s/" % (cfg["web"].get("host", "127.0.0.1"),
                                                   cfg["web"]["port"])
                              if cfg["web"].get("enabled", True) else "отключена"),
        "Экспорт      : %s" % ", ".join(
            filter(None, ["JSONL" if cfg["storage"].get("jsonl") else "",
                          "SQLite" if cfg["storage"].get("sqlite") else "",
                          "PCAP" if cfg["storage"].get("pcap") else "",
                          "CSV" if cfg["storage"].get("csv_reports") else ""])) or "нет",
        "Тревоги      : правил — %s" % len(cfg.get("alerts", {}).get("rules", [])),
    ]
    console.startup("Universal Sniffer v%s" % __version__, lines)
    console.info("Подсказка: сценарий с АЗС и все настройки — в README.md")

    engine.start()
    threading.Thread(target=config_watcher, daemon=True).start()

    web_server = None
    if cfg["web"].get("enabled", True):
        state = PanelState(hub, cfg, console, exports, registry, reload_cb)
        state.started_iso = datetime.now().isoformat(timespec="seconds")
        try:
            web_server = start_web(state, cfg["web"].get("host", "0.0.0.0"),
                                   int(cfg["web"].get("port", 8080)))
            console.info("ВЕБ-ПАНЕЛЬ ЗАПУЩЕНА:  http://127.0.0.1:%s/  "
                         "(откройте в браузере на машине сниффера)" % cfg["web"]["port"])
            if args.open_browser:
                import webbrowser
                url = "http://127.0.0.1:%s/" % cfg["web"]["port"]
                threading.Timer(1.5, lambda: webbrowser.open(url)).start()
        except Exception as e:
            console.error("Веб-панель не запустилась: %s" % e)

    # серия статистики раз в секунду + раздача stats по SSE
    def series_loop():
        last_broadcast = 0.0
        while True:
            time.sleep(1.0)
            hub.tick_series()
            if time.time() - last_broadcast >= 2.0:
                last_broadcast = time.time()
                hub.broadcast("stats", hub.stats_snapshot())

    threading.Thread(target=series_loop, daemon=True).start()

    console.banner("ОЖИДАНИЕ ПОДКЛЮЧЕНИЙ — %s" %
                   ("остановка по команде диспетчера служб" if stop_event is not None
                    else "Ctrl+C для остановки и итоговой статистики"))

    try:
        if stop_event is not None:
            while not stop_event.is_set():
                time.sleep(0.5)
        else:
            while True:
                time.sleep(0.5)
    except KeyboardInterrupt:
        console.info("Остановка...", )
    finally:
        engine.stop()
        if web_server:
            try:
                web_server.shutdown()
            except Exception:
                pass
        paths = exports.close(hub)
        console.final_stats(hub, paths)


if __name__ == "__main__":
    main()
