"""MCP-сервер: UniversalSniffer — перехват и разбор TCP-трафика.

Связь со сниффером — HTTP API веб-панели (по умолчанию http://127.0.0.1:9500,
переменная окружения SNIFFER_PANEL_URL; значение вида "${VAR}" из
settings.yaml трактуется как «не задано»).

Жизненный цикл: sniffer_start запускает subprocess
`python -m src.sniffer --config <cfg>` с cwd = корень проекта
(переменная PROJECT_ROOT или автоопределение по расположению этого файла),
pid пишется в data/sniffer.pid; sniffer_stop / sniffer_restart управляют им.
Чтение данных — только HTTP API: /api/status, /api/stats, /api/series,
/api/sessions, /api/packets, /api/packet/<id>, /api/config, /api/alerts,
/api/export/csv, POST /api/reload.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("sniffer-mcp")

HTTP_TIMEOUT = 5.0          # таймаут всех HTTP-вызовов к панели сниффера
MAX_PACKETS_FETCH = 2000    # верхний лимит выборки для фильтрации/анализа

# ═══════════════════════════════════════════════════════════════════════
# Вспомогательное
# ═══════════════════════════════════════════════════════════════════════


def _json_result(data) -> str:
    try:
        return json.dumps(data, ensure_ascii=False, indent=2, default=str)[:30000]
    except Exception:
        return str(data)[:30000]


def _env(name: str, default: str = "") -> str:
    """Значение переменной окружения; "${VAR}" из settings.yaml = не задано."""
    v = os.environ.get(name, "")
    if not v or v.startswith("${"):
        return default
    return v


def _project_root() -> Path:
    """Корень проекта llm-agent (для cwd сабпроцесса и путей data/)."""
    env = _env("PROJECT_ROOT")
    if env:
        return Path(env)
    here = Path(__file__).resolve()
    for parent in here.parents:  # .../src/mcp_servers/sniffer/server.py → корень
        if (parent / "src" / "mcp_servers").is_dir() and (parent / "agents").is_dir():
            return parent
    return here.parents[3] if len(here.parents) > 3 else here.parent


def _panel_url() -> str:
    override = _env("SNIFFER_PANEL_URL")
    if override:
        return override.rstrip("/")
    web_port = _env("SNIFFER_WEB_PORT", "9500")
    return "http://127.0.0.1:%s" % web_port


def _data_dir() -> Path:
    return _project_root() / "data"


def _capture_dirs():
    """Кандидаты каталога capture/ (зависит от USNIFF_ROOT процесса сниффера)."""
    candidates = []
    usniff = _env("USNIFF_ROOT")
    if usniff:
        candidates.append(Path(usniff) / "capture")
    root = _project_root()
    candidates += [root / "data" / "sniffer" / "capture",
                   root / "src" / "sniffer" / "capture",
                   root / "capture"]
    return candidates


def _capture_dir():
    for d in _capture_dirs():
        if d.is_dir():
            return d
    return None


# ═══════════════════════════════════════════════════════════════════════
# HTTP-клиент панели сниффера (stdlib urllib, синхронный)
# ═══════════════════════════════════════════════════════════════════════


def _http_request(path: str, method: str = "GET", timeout: float = HTTP_TIMEOUT):
    """Синхронный HTTP-запрос к панели. Возвращает (status, parsed_json|str)."""
    url = _panel_url() + path
    req = urllib.request.Request(url, method=method)
    req.add_header("Accept", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        ctype = resp.headers.get("Content-Type", "")
        status = resp.status
    if "application/json" in ctype:
        try:
            return status, json.loads(body)
        except Exception:
            return status, body
    return status, body


def _api(path: str, method: str = "GET"):
    """Запрос к API панели; при ошибке возвращает {"error": ...}."""
    try:
        status, data = _http_request(path, method=method)
        if status >= 400:
            if isinstance(data, dict):
                return {"error": "HTTP %s: %s" % (status, data.get("error", ""))}
            return {"error": "HTTP %s" % status}
        return data if isinstance(data, dict) else {"data": data}
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        return {"error": "панель сниффера недоступна (%s): %s"
                         % (_panel_url(), reason),
                "hint": "запустите: sniffer_start (требует подтверждения) "
                        "или python -m src.sniffer --ports 3003:3004"}
    except Exception as e:
        return {"error": str(e)}


async def _api_async(path: str, method: str = "GET") -> dict:
    return await asyncio.to_thread(_api, path, method)


def _http_ok() -> bool:
    try:
        status, _ = _http_request("/api/status", timeout=2.0)
        return status < 400
    except Exception:
        return False


# ═══════════════════════════════════════════════════════════════════════
# Локальный процесс сниффера (subprocess)
# ═══════════════════════════════════════════════════════════════════════

_proc = None            # subprocess.Popen | None
_started_at = None      # float | None
_log_fh = None


def _pid_file() -> Path:
    return _data_dir() / "sniffer.pid"


def _read_pid():
    try:
        return int(_pid_file().read_text(encoding="utf-8").strip())
    except Exception:
        return None


def _pid_alive(pid) -> bool:
    if not pid or pid == os.getpid():
        return False
    try:
        if os.name == "nt":
            out = subprocess.run(
                ["tasklist", "/FI", "PID eq %d" % pid],
                capture_output=True, text=True, timeout=5).stdout or ""
            return str(pid) in out
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except Exception:
        return False


def _proc_alive() -> bool:
    return _proc is not None and _proc.poll() is None


def _child_env() -> dict:
    env = dict(os.environ)
    env["PROJECT_ROOT"] = str(_project_root())
    # capture/, plugins/, config.json — рядом с USNIFF_ROOT, чтобы не засорять
    # каталог пакета src/sniffer и репозиторий.
    env["USNIFF_ROOT"] = _env("USNIFF_ROOT") or str(_data_dir() / "sniffer")
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _default_config_path() -> Path:
    root = _project_root()
    for candidate in (root / "data" / "sniffer" / "config.json",
                      root / "src" / "sniffer" / "config.json"):
        if candidate.is_file():
            return candidate
    return root / "src" / "sniffer" / "config.json"


def _spawn(cfg, ports, web_port) -> dict:
    global _proc, _started_at, _log_fh

    if _proc_alive():
        return {"ok": False, "error": "сниффер уже запущен (pid %s)" % _proc.pid}

    # Если процесс остался от прошлого запуска MCP (pid-файл) — не плодим второй.
    stale_pid = _read_pid()
    if stale_pid and _pid_alive(stale_pid) and _http_ok():
        return {"ok": False,
                "error": "похоже, сниффер уже работает (pid %s из %s), панель отвечает"
                         % (stale_pid, _pid_file())}

    cmd = [sys.executable, "-m", "src.sniffer",
           "--config", cfg or str(_default_config_path())]
    if ports:
        cmd += ["--ports", ports]
    if web_port:
        cmd += ["--web-port", str(int(web_port))]
    cmd += ["--no-color", "--verbosity", "quiet"]

    data_dir = _data_dir()
    (data_dir / "sniffer").mkdir(parents=True, exist_ok=True)
    try:
        _log_fh = open(data_dir / "sniffer-console.log", "a",
                       encoding="utf-8", errors="replace")
    except Exception:
        _log_fh = None

    try:
        _proc = subprocess.Popen(
            cmd, cwd=str(_project_root()), env=_child_env(),
            stdout=_log_fh or subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
        )
    except Exception as e:
        return {"ok": False, "error": "не удалось запустить сниффер: %s" % e}

    _started_at = time.time()
    try:
        _pid_file().write_text(str(_proc.pid), encoding="utf-8")
    except Exception:
        pass

    # ждём готовности панели (до ~10 c)
    ready = False
    for _ in range(20):
        if _proc.poll() is not None:
            break
        if _http_ok():
            ready = True
            break
        time.sleep(0.5)

    result = {
        "ok": True,
        "pid": _proc.pid,
        "cmd": cmd,
        "panel": _panel_url(),
        "ready": ready,
        "state": "running" if ready else
                 "starting (панель пока не отвечает; повторите sniffer_status)",
        "log": str(data_dir / "sniffer-console.log"),
    }
    if _proc.poll() is not None:
        result["ok"] = False
        result["error"] = ("процесс завершился сразу после старта (код %s); "
                           "смотрите %s" % (_proc.returncode,
                                            data_dir / "sniffer-console.log"))
    return result


def _cleanup_handles():
    global _proc, _started_at, _log_fh
    _proc = None
    _started_at = None
    try:
        if _pid_file().exists():
            _pid_file().unlink()
    except Exception:
        pass
    try:
        if _log_fh and not _log_fh.closed:
            _log_fh.close()
    except Exception:
        pass
    _log_fh = None


def _stop_proc() -> dict:
    global _proc, _started_at
    if _proc_alive():
        pid = _proc.pid
        managed = True
    else:
        pid = _read_pid()
        managed = False

    if not pid or (not managed and not _pid_alive(pid)):
        _cleanup_handles()
        return {"ok": True, "stopped": False,
                "message": "сниффер не был запущен (процесс не найден)"}

    try:
        if managed:
            _proc.terminate()
            try:
                _proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                _proc.kill()
                _proc.wait(timeout=3)
        elif _pid_alive(pid):
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                               capture_output=True, timeout=10)
            else:
                os.kill(pid, signal.SIGTERM)
                deadline = time.time() + 5
                while _pid_alive(pid) and time.time() < deadline:
                    time.sleep(0.2)
                if _pid_alive(pid):
                    os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except Exception as e:
        return {"ok": False, "error": "не удалось остановить pid %s: %s" % (pid, e)}
    finally:
        _cleanup_handles()

    return {"ok": True, "stopped": True, "pid": pid,
            "message": "сниффер остановлен (pid %s)" % pid}


def _local_state() -> dict:
    pid = _proc.pid if _proc else _read_pid()
    alive = _proc_alive() or (_proc is None and _pid_alive(pid))
    state = {
        "process_alive": bool(alive),
        "pid": pid,
        "managed_by_mcp": _proc is not None,
        "panel_url": _panel_url(),
        "pid_file": str(_pid_file()),
    }
    if alive and _started_at:
        state["uptime_sec"] = round(time.time() - _started_at, 1)
    return state


# ═══════════════════════════════════════════════════════════════════════
# Фильтрация и анализ (на стороне MCP: API панели фильтрует ограниченно)
# ═══════════════════════════════════════════════════════════════════════

_ERROR_KEYWORDS = ("error", "exception", "ошибка", "fail", "denied", "timeout")


def _to_ts(value) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _packet_matches(p: dict, search: str, protocol: str,
                    direction: str, min_size: int, max_size: int) -> bool:
    if protocol and (p.get("protocol") or "").upper() != protocol.upper():
        return False
    if direction and (p.get("direction") or "").upper() != direction.upper():
        return False
    size = p.get("size") or 0
    if min_size and size < min_size:
        return False
    if max_size and size > max_size:
        return False
    if search:
        hay = " ".join(filter(None, [
            str(p.get("summary") or ""),
            json.dumps(p.get("info") or {}, ensure_ascii=False),
            str(p.get("client") or ""), str(p.get("port_name") or ""),
        ])).lower()
        if search.lower() not in hay:
            return False
    return True


def _build_analyze(minutes: int) -> dict:
    """Агрегированная сводка для LLM: протоколы, клиенты, ошибки, тревоги,
    аномалии — из /api/packets?limit=2000 + /api/alerts."""
    packets_data = _api("/api/packets?limit=%d" % MAX_PACKETS_FETCH)
    alerts_data = _api("/api/alerts")
    if packets_data.get("error"):
        return packets_data

    packets = packets_data.get("packets") or []
    alerts = (alerts_data.get("alerts") or []) if isinstance(alerts_data, dict) else []
    cutoff = time.time() - max(1, int(minutes)) * 60

    recent = [p for p in packets if _to_ts(p.get("ts")) >= cutoff] or packets

    by_protocol = {}
    by_direction = {"TX": 0, "RX": 0}
    by_client = {}
    errors = []
    giant = []
    total_bytes = 0
    for p in recent:
        proto = p.get("protocol") or "RAW"
        by_protocol[proto] = by_protocol.get(proto, 0) + 1
        d = p.get("direction") or "?"
        if d in by_direction:
            by_direction[d] += 1
        cl = p.get("client") or "?"
        by_client[cl] = by_client.get(cl, 0) + 1
        total_bytes += p.get("size") or 0
        summary = str(p.get("summary") or "")
        info_text = json.dumps(p.get("info") or {}, ensure_ascii=False).lower()
        if any(k in summary.lower() or k in info_text for k in _ERROR_KEYWORDS):
            errors.append({"id": p.get("id"), "protocol": proto,
                           "summary": summary[:160]})
        if (p.get("size") or 0) > 1000000:
            giant.append({"id": p.get("id"), "size": p.get("size"),
                          "summary": summary[:160]})

    crit_alerts = [a for a in alerts
                   if str(a.get("severity", "")).lower() in ("crit", "critical")][-10:]

    top_clients = sorted(by_client.items(), key=lambda kv: -kv[1])[:10]
    findings = []
    if errors:
        findings.append("ошибок/исключений в payload: %d (показаны последние)" % len(errors))
    if giant:
        findings.append("гигантские пакеты (>1 МБ): %d" % len(giant))
    if crit_alerts:
        findings.append("критических тревог: %d" % len(crit_alerts))
    if not recent:
        findings.append("пакетов в окне нет (сниффер слушает порты без трафика? "
                        "проверьте sniffer_status и порты)")

    return {
        "window_minutes": minutes,
        "panel": _panel_url(),
        "packets_analyzed": len(recent),
        "bytes_total": total_bytes,
        "by_protocol": by_protocol,
        "by_direction": by_direction,
        "top_clients": [{"client": c, "packets": n} for c, n in top_clients],
        "error_samples": errors[-10:],
        "giant_packets": giant[-10:],
        "recent_crit_alerts": crit_alerts,
        "findings": findings,
    }


_TEXT_EXTS = {".jsonl", ".log", ".csv", ".txt"}
_META_EXTS = {".db", ".pcap", ".sqlite"}
_ALLOWED_EXTS = _TEXT_EXTS | _META_EXTS


# ═══════════════════════════════════════════════════════════════════════
# MCP-сервер
# ═══════════════════════════════════════════════════════════════════════

app = Server("sniffer")


@app.list_tools()
async def list_tools() -> list:
    return [
        Tool(name="sniffer_status",
             description="Статус сниффера: процесс, каналы (порты listen→target), "
                         "парсеры, uptime. Безопасно, вызывайте первым.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="sniffer_start",
             description="Запустить сниффер как subprocess (python -m src.sniffer). "
                         "ТРЕБУЕТ ПОДТВЕРЖДЕНИЯ пользователя.",
             inputSchema={"type": "object", "properties": {
                 "config": {"type": "string",
                            "description": "путь к config.json (по умолчанию src/sniffer/config.json)"},
                 "ports": {"type": "string",
                           "description": "пары портов: 3003:3004,10010:10011 (цель = порт+1 без двоеточия)"},
                 "web_port": {"type": "integer",
                              "description": "порт веб-панели (по умолчанию 9500)"}}}),
        Tool(name="sniffer_stop",
             description="Остановить запущенный сниффер (terminate, 5 c, затем kill). "
                         "ТРЕБУЕТ ПОДТВЕРЖДЕНИЯ пользователя.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="sniffer_restart",
             description="Перезапустить сниффер (stop + start с последним конфигом). "
                         "ТРЕБУЕТ ПОДТВЕРЖДЕНИЯ пользователя.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="sniffer_stats",
             description="Статистика: пакеты, байты, по протоколам/портам, top команд, клиенты.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="sniffer_series",
             description="Временной ряд пакетов/байт в секунду для графика.",
             inputSchema={"type": "object", "properties": {
                 "minutes": {"type": "integer", "default": 5,
                             "description": "глубина окна в минутах (точки = секунды)"}}}),
        Tool(name="sniffer_sessions",
             description="Активные сессии (соединения) сниффера.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="sniffer_packets",
             description="Последние пакеты с фильтрацией (поиск/протокол/направление/"
                         "размер) — фильтр применяется на стороне MCP.",
             inputSchema={"type": "object", "properties": {
                 "limit": {"type": "integer", "default": 50, "maximum": 2000},
                 "search": {"type": "string", "description": "подстрока в summary/info/клиенте"},
                 "protocol": {"type": "string", "description": "HTTP, JSON, THRIFT, MODBUS, RAW…"},
                 "direction": {"type": "string", "enum": ["TX", "RX"]},
                 "min_size": {"type": "integer"},
                 "max_size": {"type": "integer"}}}),
        Tool(name="sniffer_packet_detail",
             description="Детали пакета по id: полный разбор + hexdump.",
             inputSchema={"type": "object", "properties": {
                 "packet_id": {"type": "integer"}}, "required": ["packet_id"]}),
        Tool(name="sniffer_alerts",
             description="Сработавшие тревоги (правила: Thrift EXCEPTION, откаты, "
                         "гигантские пакеты, ошибки БД…).",
             inputSchema={"type": "object", "properties": {
                 "limit": {"type": "integer", "default": 50}}}),
        Tool(name="sniffer_config_get",
             description="Текущий конфиг сниффера + список загруженных парсеров. "
                         "Внешнее действие (обращение к живому процессу).",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="sniffer_reload",
             description="Горячая перезагрузка конфига (правила тревог, консоль) без рестарта. "
                         "POST /api/reload.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="sniffer_export_csv",
             description="Сгенерировать CSV-отчёты сейчас (файлы в capture/reports).",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="sniffer_list_capture",
             description="Список файлов захвата в capture/ (jsonl/db/pcap/отчёты) с размерами.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="sniffer_read_capture",
             description="Прочитать ТЕКСТОВЫЙ файл из capture/ (jsonl/log/csv/txt) — хвост N строк. "
                         "Бинарные (.db/.pcap) — только метаданные. Только имя файла без пути.",
             inputSchema={"type": "object", "properties": {
                 "name": {"type": "string", "description": "имя файла, например traffic.jsonl"},
                 "tail": {"type": "integer", "default": 200,
                          "description": "сколько последних строк вернуть"}},
                 "required": ["name"]}),
        Tool(name="sniffer_analyze",
             description="Агрегированная сводка: протоколы, клиенты, ошибки/исключения, "
                         "критические тревоги, аномалии (гигантские пакеты).",
             inputSchema={"type": "object", "properties": {
                 "minutes": {"type": "integer", "default": 10}}}),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list:
    try:
        if name == "sniffer_status":
            local = _local_state()
            http = await _api_async("/api/status")
            result = {"local": local, "panel": http}
            if http.get("error"):
                result["state"] = ("running/unreachable — процесс есть, панель не отвечает"
                                   if local.get("process_alive")
                                   else "stopped — сниффер не запущен")
            else:
                result["state"] = "running"
            return [TextContent(type="text", text=_json_result(result))]

        if name == "sniffer_start":
            result = await asyncio.to_thread(
                _spawn, arguments.get("config"),
                arguments.get("ports"), arguments.get("web_port"))
            return [TextContent(type="text", text=_json_result(result))]

        if name == "sniffer_stop":
            result = await asyncio.to_thread(_stop_proc)
            return [TextContent(type="text", text=_json_result(result))]

        if name == "sniffer_restart":
            stop_res = await asyncio.to_thread(_stop_proc)
            await asyncio.sleep(1.0)
            start_res = await asyncio.to_thread(_spawn, None, None, None)
            return [TextContent(type="text",
                                text=_json_result({"stop": stop_res, "start": start_res}))]

        if name == "sniffer_stats":
            return [TextContent(type="text",
                                text=_json_result(await _api_async("/api/stats")))]

        if name == "sniffer_series":
            minutes = max(1, min(int(arguments.get("minutes", 5) or 5), 15))
            limit = max(10, min(minutes * 60, 900))
            return [TextContent(type="text",
                                text=_json_result(await _api_async("/api/series?limit=%d" % limit)))]

        if name == "sniffer_sessions":
            return [TextContent(type="text",
                                text=_json_result(await _api_async("/api/sessions")))]

        if name == "sniffer_packets":
            limit = max(1, min(int(arguments.get("limit", 50) or 50), MAX_PACKETS_FETCH))
            data = await _api_async(
                "/api/packets?limit=%d" % min(limit * 4, MAX_PACKETS_FETCH))
            if data.get("error"):
                return [TextContent(type="text", text=_json_result(data))]
            packets = data.get("packets") or []
            filtered = [
                p for p in packets
                if _packet_matches(
                    p,
                    search=str(arguments.get("search") or ""),
                    protocol=str(arguments.get("protocol") or ""),
                    direction=str(arguments.get("direction") or ""),
                    min_size=int(arguments.get("min_size") or 0),
                    max_size=int(arguments.get("max_size") or 0),
                )
            ][:limit]
            return [TextContent(type="text", text=_json_result({
                "count": len(filtered),
                "fetched": len(packets),
                "filters": {k: arguments[k] for k in
                            ("search", "protocol", "direction", "min_size", "max_size")
                            if arguments.get(k)},
                "packets": filtered,
            }))]

        if name == "sniffer_packet_detail":
            pid = int(arguments["packet_id"])
            return [TextContent(type="text",
                                text=_json_result(await _api_async("/api/packet/%d" % pid)))]

        if name == "sniffer_alerts":
            limit = max(1, min(int(arguments.get("limit", 50) or 50), 300))
            data = await _api_async("/api/alerts")
            if data.get("error"):
                return [TextContent(type="text", text=_json_result(data))]
            alerts = data.get("alerts") or []
            return [TextContent(type="text", text=_json_result({
                "count": len(alerts[:limit]), "alerts": alerts[:limit]}))]

        if name == "sniffer_config_get":
            return [TextContent(type="text",
                                text=_json_result(await _api_async("/api/config")))]

        if name == "sniffer_reload":
            return [TextContent(type="text",
                                text=_json_result(await _api_async("/api/reload", method="POST")))]

        if name == "sniffer_export_csv":
            return [TextContent(type="text",
                                text=_json_result(await _api_async("/api/export/csv")))]

        if name == "sniffer_list_capture":
            cap = _capture_dir()
            if not cap:
                return [TextContent(type="text", text=_json_result({
                    "error": "каталог capture/ не найден",
                    "hint": "сниффер создаёт его при первом запуске (sniffer_start); "
                            "ищу в data/sniffer/capture, src/sniffer/capture, capture"}))]
            files = []
            for f in sorted(cap.iterdir()):
                if f.is_file():
                    try:
                        st = f.stat()
                        files.append({
                            "name": f.name, "size": st.st_size,
                            "mtime": time.strftime("%Y-%m-%d %H:%M:%S",
                                                   time.localtime(st.st_mtime))})
                    except OSError:
                        continue
            reports = cap / "reports"
            if reports.is_dir():
                for f in sorted(reports.iterdir()):
                    if f.is_file():
                        try:
                            st = f.stat()
                            files.append({
                                "name": "reports/" + f.name, "size": st.st_size,
                                "mtime": time.strftime("%Y-%m-%d %H:%M:%S",
                                                       time.localtime(st.st_mtime))})
                        except OSError:
                            continue
            return [TextContent(type="text", text=_json_result({
                "dir": str(cap), "count": len(files), "files": files}))]

        if name == "sniffer_read_capture":
            raw_name = str(arguments.get("name") or "")
            tail_n = max(1, min(int(arguments.get("tail", 200) or 200), 5000))
            # ── защита от path traversal: только имя файла, без путей ──
            if (not raw_name or raw_name != os.path.basename(raw_name)
                    or "/" in raw_name or "\\" in raw_name or ".." in raw_name):
                return [TextContent(type="text", text=_json_result({
                    "error": "недопустимое имя: передайте только имя файла "
                             "без слэшей и «..»"}))]
            ext = os.path.splitext(raw_name)[1].lower()
            if ext not in _ALLOWED_EXTS:
                return [TextContent(type="text", text=_json_result({
                    "error": "расширение %r не входит в белый список %s"
                             % (ext or "<нет>", sorted(_ALLOWED_EXTS))}))]
            cap = _capture_dir()
            path = (cap / raw_name) if cap else None
            if not path or not path.is_file():
                return [TextContent(type="text", text=_json_result({
                    "error": "файл не найден в capture/ (%s)"
                             % (cap or "каталог не найден")}))]
            size = path.stat().st_size
            meta = {"name": raw_name, "path": str(path), "size": size}
            if ext in _META_EXTS:
                meta.update({
                    "binary": True,
                    "content": "(бинарный файл — только метаданные; анализируйте "
                               "через sniffer_packets/PCAP-инструменты)",
                    "size_human": ("%.1f МБ" % (size / 1048576)) if size > 1048576
                                  else ("%.1f КБ" % (size / 1024)),
                })
                return [TextContent(type="text", text=_json_result(meta))]
            try:
                with open(path, "r", encoding="utf-8", errors="replace") as fh:
                    lines = fh.readlines()
            except Exception as e:
                return [TextContent(type="text",
                                    text=_json_result({"error": "не удалось прочитать: %s" % e}))]
            meta.update({"binary": False, "total_lines": len(lines),
                         "returned_lines": min(tail_n, len(lines)),
                         "tail": "".join(lines[-tail_n:])[:20000]})
            return [TextContent(type="text", text=_json_result(meta))]

        if name == "sniffer_analyze":
            minutes = max(1, min(int(arguments.get("minutes", 10) or 10), 60))
            result = await asyncio.to_thread(_build_analyze, minutes)
            return [TextContent(type="text", text=_json_result(result))]

        return [TextContent(type="text", text="Неизвестный инструмент: %s" % name)]
    except Exception as e:
        logger.exception("sniffer tool %s failed", name)
        return [TextContent(type="text", text="Ошибка: %s" % e)]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
