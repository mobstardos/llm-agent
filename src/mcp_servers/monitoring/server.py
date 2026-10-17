"""MCP-сервер: логи, Prometheus, Jaeger."""
from __future__ import annotations

import json
import asyncio
import logging
import os
import re
import sys
from collections import Counter
from pathlib import Path

import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("monitoring-mcp")


def _get_root() -> Path:
    try:
        from src.runtime_config import get_project_root
        val = get_project_root(default="")
        if val:
            return Path(val).resolve()
    except Exception:
        pass
    return Path(os.getenv("PROJECT_ROOT", os.getcwd())).resolve()


def _safe(path: str) -> Path:
    root = _get_root()
    p = (root / path).resolve()
    if root not in p.parents and p != root:
        raise ValueError(f"Путь вне корня: {path}")
    return p


def _json_result(data) -> str:
    try:
        return json.dumps(data, ensure_ascii=False, indent=2, default=str)[:30000]
    except Exception:
        return str(data)[:30000]


app = Server("monitoring")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="log_tail",
             description="Последние N строк лог-файла.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"},
                 "lines": {"type": "integer", "default": 200}},
                 "required": ["path"]}),
        Tool(name="log_grep",
             description="Поиск по лог-файлу (regex).",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"},
                 "pattern": {"type": "string"},
                 "max_results": {"type": "integer", "default": 200},
                 "context_lines": {"type": "integer", "default": 0}},
                 "required": ["path", "pattern"]}),
        Tool(name="log_stats",
             description="Статистика лог-файла: уровни, топ-сообщения.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"},
                 "level_field": {"type": "string", "default": "level"}}}),
        Tool(name="log_parse_json",
             description="Распарсить JSON-логи (по строкам).",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"},
                 "limit": {"type": "integer", "default": 100}},
                 "required": ["path"]}),
        Tool(name="prometheus_query",
             description="Мгновенный PromQL-запрос.",
             inputSchema={"type": "object", "properties": {
                 "query": {"type": "string"}},
                 "required": ["query"]}),
        Tool(name="prometheus_query_range",
             description="PromQL range-запрос.",
             inputSchema={"type": "object", "properties": {
                 "query": {"type": "string"},
                 "start": {"type": "string", "default": "1h"},
                 "step": {"type": "string", "default": "60s"}},
                 "required": ["query"]}),
        Tool(name="prometheus_targets",
             description="Список targets Prometheus.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="jaeger_services",
             description="Список сервисов Jaeger.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="jaeger_trace",
             description="Трейс по ID из Jaeger.",
             inputSchema={"type": "object", "properties": {
                 "trace_id": {"type": "string"}},
                 "required": ["trace_id"]}),
        Tool(name="monitoring_info",
             description="Доступные системы мониторинга.",
             inputSchema={"type": "object", "properties": {}}),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "monitoring_info":
            return [TextContent(
                type="text",
                text=_json_result({
                    "prometheus": {
                        "configured": bool(os.getenv("PROMETHEUS_URL")),
                        "url": os.getenv("PROMETHEUS_URL", ""),
                    },
                    "jaeger": {
                        "configured": bool(os.getenv("JAEGER_URL")),
                        "url": os.getenv("JAEGER_URL", ""),
                    },
                }),
            )]

        # ═══════════════════════════════════════════════════════
        # Logs
        # ═══════════════════════════════════════════════════════
        if name == "log_tail":
            p = _safe(arguments["path"])
            if not p.exists():
                return [TextContent(type="text", text="Файл не найден")]
            n = arguments.get("lines", 200)
            # Эффективное чтение хвоста
            try:
                with open(p, "rb") as f:
                    f.seek(0, 2)
                    size = f.tell()
                    block = min(size, n * 500)
                    f.seek(size - block)
                    data = f.read().decode("utf-8", errors="replace")
                lines = data.splitlines()[-n:]
            except Exception as e:
                return [TextContent(type="text", text=f"Error: {e}")]
            return [TextContent(type="text", text="\n".join(lines))]

        if name == "log_grep":
            p = _safe(arguments["path"])
            if not p.exists():
                return [TextContent(type="text", text="Файл не найден")]
            try:
                regex = re.compile(arguments["pattern"])
            except re.error as e:
                return [TextContent(type="text", text=f"Regex: {e}")]
            max_results = arguments.get("max_results", 200)
            context = arguments.get("context_lines", 0)

            hits: list[str] = []
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                all_lines = f.readlines()

            for i, line in enumerate(all_lines):
                if regex.search(line):
                    start = max(0, i - context)
                    end = min(len(all_lines), i + context + 1)
                    for j in range(start, end):
                        marker = ">" if j == i else " "
                        hits.append(f"{marker} {j+1}: {all_lines[j].rstrip()}")
                    if len(hits) >= max_results:
                        break

            return [TextContent(
                type="text",
                text="\n".join(hits[:max_results]) or "Совпадений нет",
            )]

        if name == "log_stats":
            p = _safe(arguments["path"])
            if not p.exists():
                return [TextContent(type="text", text="Файл не найден")]
            level_field = arguments.get("level_field", "level")

            levels: Counter = Counter()
            messages: Counter = Counter()
            total = 0
            errors: list[str] = []
            is_json = False

            with open(p, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    total += 1
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        obj = json.loads(line)
                        is_json = True
                        lvl = str(obj.get(level_field, "")).upper()
                        if lvl:
                            levels[lvl] += 1
                        msg = str(obj.get("message") or obj.get("msg") or "")
                        if msg:
                            messages[msg[:100]] += 1
                        if lvl in ("ERROR", "CRITICAL", "FATAL"):
                            errors.append(line[:200])
                    except json.JSONDecodeError:
                        # Простой формат: [LEVEL] message
                        m = re.search(
                            r"\b(ERROR|WARN|WARNING|INFO|DEBUG|TRACE|CRITICAL|FATAL)\b",
                            line, re.IGNORECASE,
                        )
                        if m:
                            levels[m.group(1).upper()] += 1
                        if "error" in line.lower():
                            errors.append(line[:200])

            return [TextContent(
                type="text",
                text=_json_result({
                    "total_lines": total,
                    "is_json": is_json,
                    "by_level": dict(levels),
                    "top_messages": messages.most_common(10),
                    "recent_errors": errors[-20:],
                }),
            )]

        if name == "log_parse_json":
            p = _safe(arguments["path"])
            if not p.exists():
                return [TextContent(type="text", text="Файл не найден")]
            limit = arguments.get("limit", 100)
            parsed: list[dict] = []
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                for i, line in enumerate(f):
                    if i >= limit:
                        break
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        parsed.append(json.loads(line))
                    except json.JSONDecodeError:
                        parsed.append({"raw": line[:300]})
            return [TextContent(
                type="text", text=_json_result(parsed[:limit]),
            )]

        # ═══════════════════════════════════════════════════════
        # Prometheus
        # ═══════════════════════════════════════════════════════
        if name.startswith("prometheus_"):
            url = os.getenv("PROMETHEUS_URL", "").rstrip("/")
            if not url:
                return [TextContent(type="text", text="PROMETHEUS_URL не задан")]

            async with httpx.AsyncClient(timeout=30.0) as client:
                try:
                    if name == "prometheus_query":
                        r = await client.get(
                            f"{url}/api/v1/query",
                            params={"query": arguments["query"]},
                        )
                        return [TextContent(type="text", text=_json_result(r.json()))]

                    if name == "prometheus_query_range":
                        import time as _t
                        # Очень упрощённо
                        now = int(_t.time())
                        start = now - 3600  # 1h по умолчанию
                        r = await client.get(
                            f"{url}/api/v1/query_range",
                            params={
                                "query": arguments["query"],
                                "start": start,
                                "end": now,
                                "step": arguments.get("step", "60s"),
                            },
                        )
                        return [TextContent(type="text", text=_json_result(r.json()))]

                    if name == "prometheus_targets":
                        r = await client.get(f"{url}/api/v1/targets")
                        return [TextContent(type="text", text=_json_result(r.json()))]
                except Exception as e:
                    return [TextContent(type="text", text=f"Prometheus: {e}")]

        # ═══════════════════════════════════════════════════════
        # Jaeger
        # ═══════════════════════════════════════════════════════
        if name.startswith("jaeger_"):
            url = os.getenv("JAEGER_URL", "").rstrip("/")
            if not url:
                return [TextContent(type="text", text="JAEGER_URL не задан")]

            async with httpx.AsyncClient(timeout=30.0) as client:
                try:
                    if name == "jaeger_services":
                        r = await client.get(f"{url}/api/services")
                        return [TextContent(type="text", text=_json_result(r.json()))]

                    if name == "jaeger_trace":
                        r = await client.get(
                            f"{url}/api/traces/{arguments['trace_id']}",
                        )
                        return [TextContent(type="text", text=_json_result(r.json()))]
                except Exception as e:
                    return [TextContent(type="text", text=f"Jaeger: {e}")]

        return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]
    except Exception as e:
        logger.exception("monitoring tool %s failed", name)
        return [TextContent(type="text", text=f"Ошибка: {e}")]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
