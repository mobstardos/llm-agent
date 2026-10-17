"""MCP-сервер: 1C Designer."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("onec-designer-mcp")

_runner = None


def _get_root() -> Path:
    try:
        from src.runtime_config import get_project_root
        val = get_project_root(default="")
        if val:
            return Path(val).resolve()
    except Exception:
        pass
    val = os.getenv("PROJECT_ROOT", "").strip()
    return Path(val or os.getcwd()).resolve()


def _safe(path: str) -> Path:
    root = _get_root()
    p = (root / path).resolve()
    if root not in p.parents and p != root:
        raise ValueError(f"Путь вне корня: {path}")
    return p


def _get_runner():
    global _runner
    if _runner is None:
        from src.mcp_servers.onec_designer.runner import DesignerRunner
        _runner = DesignerRunner(
            log_dir=Path(__file__).resolve().parent.parent.parent.parent
                    / "data" / "onec_logs",
        )
    return _runner


def _result_to_json(r) -> str:
    return json.dumps({
        "success": r.success,
        "exit_code": r.exit_code,
        "duration_ms": round(r.duration_ms, 1),
        "errors": r.errors[:50],
        "warnings": r.warnings[:50],
        "command": r.command[:500],
        "stdout": r.stdout[:5000] if r.stdout else "",
    }, ensure_ascii=False, indent=2)


app = Server("onec_designer")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="designer_info",
             description="Информация о 1cv8.exe и ИБ.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="find_binary",
             description="Найти 1cv8.exe на системе.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="load_config",
             description="Загрузить конфигурацию из файлов (опасно!).",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "config_dir": {"type": "string"},
                     "update_db": {"type": "boolean", "default": True},
                     "ib_path": {"type": "string"},
                 },
                 "required": ["config_dir"],
             }),
        Tool(name="unload_config",
             description="Выгрузить конфигурацию в файлы.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "config_dir": {"type": "string"},
                     "format": {"type": "string", "default": "Hierarchical"},
                     "ib_path": {"type": "string"},
                 },
                 "required": ["config_dir"],
             }),
        Tool(name="update_db",
             description="Обновить конфигурацию БД (опасно!).",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "ib_path": {"type": "string"},
                     "dynamic": {"type": "boolean", "default": False},
                 },
             }),
        Tool(name="create_ib",
             description="Создать новую ИБ.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "ib_path": {"type": "string"},
                     "dbms": {"type": "string", "default": "File"},
                 },
                 "required": ["ib_path"],
             }),
        Tool(name="dump_ib",
             description="Выгрузить ИБ в .dt.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "dt_path": {"type": "string"},
                     "ib_path": {"type": "string"},
                 },
                 "required": ["dt_path"],
             }),
        Tool(name="restore_ib",
             description="Восстановить ИБ из .dt (опасно!).",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "dt_path": {"type": "string"},
                     "ib_path": {"type": "string"},
                 },
                 "required": ["dt_path"],
             }),
        Tool(name="check_config",
             description="Проверка модулей (синтаксис).",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "ib_path": {"type": "string"},
                     "thin_client": {"type": "boolean", "default": True},
                 },
             }),
        Tool(name="build_cf",
             description="Собрать .cf.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "output_cf": {"type": "string"},
                     "ib_path": {"type": "string"},
                 },
                 "required": ["output_cf"],
             }),
        Tool(name="build_cfe",
             description="Собрать .cfe (расширение).",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "output_cfe": {"type": "string"},
                     "extension_name": {"type": "string"},
                     "ib_path": {"type": "string"},
                 },
                 "required": ["output_cfe"],
             }),
        Tool(name="build_epf",
             description="Собрать .epf (обработку).",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "epf_src": {"type": "string"},
                     "output_epf": {"type": "string"},
                     "ib_path": {"type": "string"},
                 },
                 "required": ["epf_src", "output_epf"],
             }),
        Tool(name="parse_log",
             description="Разобрать лог 1С.",
             inputSchema={
                 "type": "object",
                 "properties": {"log_path": {"type": "string"}},
                 "required": ["log_path"],
             }),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        r = _get_runner()

        if name == "designer_info":
            return [TextContent(
                type="text",
                text=json.dumps({
                    "binary": str(r.binary) if r.binary else None,
                    "available": r.available(),
                    "ib_path": r.ib_path,
                    "user": r.user or "(не задан)",
                }, ensure_ascii=False, indent=2),
            )]

        if name == "find_binary":
            from src.mcp_servers.onec_designer.finder import (
                find_1cv8, find_1cv8_enterprise,
            )
            return [TextContent(
                type="text",
                text=json.dumps({
                    "designer": str(find_1cv8() or ""),
                    "enterprise": str(find_1cv8_enterprise() or ""),
                }, ensure_ascii=False, indent=2),
            )]

        if not r.available():
            return [TextContent(
                type="text",
                text="1cv8.exe не найден. Установите 1С или задайте "
                     "ONEC_1CV8_PATH в .env.",
            )]

        if name == "load_config":
            cfg_dir = _safe(arguments["config_dir"])
            ib = arguments.get("ib_path")
            result = await r.load_config_from_files(
                cfg_dir, update_db=arguments.get("update_db", True),
                ib_path=ib,
            )
            return [TextContent(type="text", text=_result_to_json(result))]

        if name == "unload_config":
            cfg_dir = _safe(arguments["config_dir"])
            result = await r.dump_config_to_files(
                cfg_dir, format_=arguments.get("format", "Hierarchical"),
                ib_path=arguments.get("ib_path"),
            )
            return [TextContent(type="text", text=_result_to_json(result))]

        if name == "update_db":
            result = await r.update_db(
                ib_path=arguments.get("ib_path"),
                dynamic=arguments.get("dynamic", False),
            )
            return [TextContent(type="text", text=_result_to_json(result))]

        if name == "create_ib":
            result = await r.create_ib(
                arguments["ib_path"],
                dbms=arguments.get("dbms", "File"),
            )
            return [TextContent(type="text", text=_result_to_json(result))]

        if name == "dump_ib":
            dt = _safe(arguments["dt_path"])
            result = await r.dump_ib(dt, ib_path=arguments.get("ib_path"))
            return [TextContent(type="text", text=_result_to_json(result))]

        if name == "restore_ib":
            dt = _safe(arguments["dt_path"])
            result = await r.restore_ib(dt, ib_path=arguments.get("ib_path"))
            return [TextContent(type="text", text=_result_to_json(result))]

        if name == "check_config":
            result = await r.check_config(
                ib_path=arguments.get("ib_path"),
                thin_client=arguments.get("thin_client", True),
            )
            return [TextContent(type="text", text=_result_to_json(result))]

        if name == "build_cf":
            out = _safe(arguments["output_cf"])
            result = await r.build_cf(
                config_dir=out.parent, output_cf=out,
                ib_path=arguments.get("ib_path"),
            )
            return [TextContent(type="text", text=_result_to_json(result))]

        if name == "build_cfe":
            out = _safe(arguments["output_cfe"])
            ext_name = arguments.get("extension_name", "MyExtension")
            # используем более прямую команду
            extra_args = ["/DumpCfg", str(out), "-Extension", ext_name]
            result = await r.run(
                *extra_args, ib_path=arguments.get("ib_path"),
                log_name="build_cfe",
            )
            return [TextContent(type="text", text=_result_to_json(result))]

        if name == "build_epf":
            src = _safe(arguments["epf_src"])
            out = _safe(arguments["output_epf"])
            result = await r.build_epf(
                epf_src=src, output_epf=out,
                ib_path=arguments.get("ib_path"),
            )
            return [TextContent(type="text", text=_result_to_json(result))]

        if name == "parse_log":
            from src.mcp_servers.onec_designer.log_parser import (
                parse_1c_log, summarize_messages,
            )
            log_path = _safe(arguments["log_path"])
            messages = parse_1c_log(log_path)
            return [TextContent(
                type="text",
                text=json.dumps(
                    summarize_messages(messages),
                    ensure_ascii=False, indent=2,
                ),
            )]

        return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]
    except Exception as e:
        logger.exception("1C Designer tool %s failed", name)
        return [TextContent(type="text", text=f"Ошибка: {e}")]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
