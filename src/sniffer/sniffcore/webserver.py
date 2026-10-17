# -*- coding: utf-8 -*-
"""
Встроенный веб-сервер панели мониторинга.

GET  /                    — веб-панель (webui/index.html + css/js)
GET  /api/stream          — SSE-поток событий (packet/stats/session/alert/...)
GET  /api/stats           — сводка статистики
GET  /api/sessions        — активные сессии
GET  /api/packets         — последние пакеты (кольцевой буфер)
GET  /api/packet/<id>     — детали пакета (полный JSON + hex-дамп)
GET  /api/config          — текущий конфиг
POST /api/reload          — перечитать конфиг (правила тревог, консоль, экспорт)
GET  /api/export/csv      — сгенерировать CSV-отчёты сейчас
GET  /download/<имя>      — файлы захвата (jsonl/db/pcap/reports) из capture/
"""

import json
import mimetypes
import os
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

WEBUI_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "webui")


class PanelState:
    """Мост между движком и HTTP-хендлером (без циклических импортов)."""

    def __init__(self, hub, cfg, console, exports, registry, reload_cb):
        self.hub = hub
        self.cfg = cfg
        self.console = console
        self.exports = exports
        self.registry = registry
        self.reload_cb = reload_cb
        self.started_iso = ""


def make_handler(state):
    capture_dir = os.path.abspath(state.cfg.get("storage", {}).get("dir", "capture"))

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "UniversalSniffer/3.0"

        # ---------- служебное ----------

        def log_message(self, fmt, *args):
            pass  # тихий режим: access-лог не нужен

        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _file(self, path, code=200):
            try:
                if not os.path.isfile(path):
                    self._json({"error": "not found"}, 404)
                    return
                ctype = mimetypes.guess_type(path)[0] or "application/octet-stream"
                size = os.path.getsize(path)
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(size))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                with open(path, "rb") as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception:
                pass

        # ---------- v2: токен панели (SNIFFER_WEB_TOKEN) ----------
        # Если env SNIFFER_WEB_TOKEN задан — все /api/* требуют ?token=...
        # или заголовок X-Panel-Token. Сам UI (/, style.css, app.js) отдаётся
        # без токена — данных в нём нет.
        _PANEL_TOKEN = os.getenv("SNIFFER_WEB_TOKEN", "").strip()

        def _token_ok(self, parsed) -> bool:
            if not self._PANEL_TOKEN:
                return True
            qs = urllib.parse.parse_qs(parsed.query)
            supplied = (self.headers.get("X-Panel-Token", "").strip()
                        or (qs.get("token", [""])[0].strip()))
            return supplied == self._PANEL_TOKEN

        # ---------- GET ----------

        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            route = parsed.path
            if route.startswith("/api/") and not self._token_ok(parsed):
                self._json({"error": "unauthorized: задайте ?token= или "
                                      "заголовок X-Panel-Token "
                                      "(SNIFFER_WEB_TOKEN)"}, 401)
                return
            try:
                if route in ("/", "/index.html"):
                    self._file(os.path.join(WEBUI_DIR, "index.html"))
                elif route == "/style.css":
                    self._file(os.path.join(WEBUI_DIR, "style.css"))
                elif route == "/app.js":
                    self._file(os.path.join(WEBUI_DIR, "app.js"))
                elif route == "/api/stream":
                    self._sse()
                elif route == "/api/stats":
                    self._json(self._stats())
                elif route == "/api/series":
                    self._json({"series": self.hub.series_snapshot(300)})
                elif route == "/api/sessions":
                    self._json({"sessions": self.hub.list_sessions()})
                elif route == "/api/packets":
                    qs = urllib.parse.parse_qs(parsed.query)
                    limit = int(qs.get("limit", ["300"])[0])
                    self._json({"packets": self.hub.recent_packets(limit)})
                elif route.startswith("/api/packet/"):
                    pid = int(route.rsplit("/", 1)[1])
                    entry, raw = self.hub.get_packet(pid)
                    if not entry:
                        self._json({"error": "not found"}, 404)
                        return
                    detail = dict(entry)
                    detail["hexdump"] = _hexdump(raw) if raw else "(нет сохранённых данных)"
                    self._json({"packet": detail})
                elif route == "/api/config":
                    cfg = dict(self.cfg)
                    self._json({"config": cfg, "parsers": self.registry.names()})
                elif route == "/api/alerts":
                    self._json({"alerts": self.hub.list_alerts(200)})
                elif route == "/api/export/csv":
                    paths = self.exports.generate_reports(self.hub)
                    self._json({"ok": bool(paths), "files": paths})
                elif route == "/api/status":
                    self._json({
                        "web": True,
                        "started_iso": state.started_iso,
                        "ports": [{"listen": p.get("listen"), "name": p.get("name"),
                                   "target": "%s:%s" % (p.get("target_host"), p.get("target_port"))}
                                  for p in self.cfg.get("ports", [])],
                        "parsers": self.registry.names(),
                    })
                elif route.startswith("/download/"):
                    name = urllib.parse.unquote(route[len("/download/"):])
                    self._serve_download(name)
                else:
                    self._json({"error": "unknown route"}, 404)
            except (BrokenPipeError, ConnectionResetError):
                pass
            except Exception as e:
                try:
                    self._json({"error": str(e)}, 500)
                except Exception:
                    pass

        # ---------- POST ----------

        def do_POST(self):
            parsed = urllib.parse.urlparse(self.path)
            route = parsed.path
            if route.startswith("/api/") and not self._token_ok(parsed):
                self._json({"error": "unauthorized: задайте ?token= или "
                                      "заголовок X-Panel-Token "
                                      "(SNIFFER_WEB_TOKEN)"}, 401)
                return
            if route == "/api/reload":
                try:
                    length = int(self.headers.get("Content-Length", 0) or 0)
                    if length:
                        self.rfile.read(length)
                    msg = state.reload_cb() if state.reload_cb else "перезагрузка недоступна"
                    self._json({"ok": True, "message": msg})
                except Exception as e:
                    self._json({"ok": False, "error": str(e)}, 500)
            else:
                self._json({"error": "unknown route"}, 404)

        # ---------- вспомогательные ----------

        @property
        def hub(self):
            return state.hub

        @property
        def cfg(self):
            return state.cfg

        @property
        def registry(self):
            return state.registry

        @property
        def exports(self):
            return state.exports

        def _stats(self):
            snap = self.hub.stats_snapshot()
            return snap

        def _serve_download(self, name):
            if "/" in name or "\\" in name or ".." in name:
                self._json({"error": "bad path"}, 400)
                return
            path = os.path.join(capture_dir, name)
            if not os.path.isfile(path):
                self._json({"error": "not found"}, 404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(os.path.getsize(path)))
            self.send_header("Content-Disposition",
                             'attachment; filename="%s"' % name.replace('"', ""))
            self.end_headers()
            with open(path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError):
                        return

        def _sse(self):
            sub = self.hub.subscribe()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                hello = {"type": "hello", "stats": self._stats()}
                self.wfile.write(b"event: status\ndata: " +
                                 json.dumps(hello, ensure_ascii=False).encode("utf-8") +
                                 b"\n\n")
                self.wfile.flush()
                idle = 0.0
                while True:
                    item = self.hub.drain(sub, timeout=0.5)
                    if item is None:
                        idle += 0.5
                        if idle >= 15.0:
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                            idle = 0.0
                        continue
                    idle = 0.0
                    event, data = item
                    payload = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
                    self.wfile.write(b"event: " + event.encode("ascii") + b"\ndata: " +
                                     payload + b"\n\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                self.hub.unsubscribe(sub)

    return Handler


def start_web(state, host, port):
    """Поднимает ThreadingHTTPServer в отдельном потоке. Возвращает сервер."""
    handler = make_handler(state)
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, daemon=True, name="web-panel")
    t.start()
    return httpd


def _hexdump(data, width=16):
    lines = []
    for off in range(0, len(data), width):
        chunk = data[off:off + width]
        hexpart = " ".join("%02X" % b for b in chunk)
        asciipart = "".join(chr(b) if 32 <= b <= 126 else "." for b in chunk)
        lines.append("%04X  %-47s  |%s|" % (off, hexpart, asciipart))
    if len(data) >= 65536:
        lines.append("... (сохранено первые 64КБ)")
    return "\n".join(lines)
