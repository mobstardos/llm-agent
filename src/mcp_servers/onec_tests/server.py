"""MCP-сервер: автотесты 1С."""
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
logger = logging.getLogger("onec-tests-mcp")


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


app = Server("onec_tests")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="tests_info",
             description="Информация о доступных тестах (YAxUnit, Vanessa).",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="run_yaxunit",
             description="Запустить YAxUnit-тесты (долго, требует 1cv8c).",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "module_filter": {"type": "string"},
                     "report_path": {"type": "string"},
                 },
             }),
        Tool(name="run_vanessa",
             description="Запустить Vanessa feature-тесты (долго).",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "features_dir": {"type": "string"},
                     "report_path": {"type": "string"},
                     "filter": {"type": "string"},
                 },
                 "required": ["features_dir"],
             }),
        Tool(name="list_features",
             description="Список feature-файлов.",
             inputSchema={
                 "type": "object",
                 "properties": {"features_dir": {"type": "string"}},
                 "required": ["features_dir"],
             }),
        Tool(name="list_yaxunit_modules",
             description="Найти модули с YAxUnit-тестами.",
             inputSchema={
                 "type": "object",
                 "properties": {"search_dir": {"type": "string"}},
             }),
        Tool(name="parse_report",
             description="Разобрать сохранённый отчёт тестов.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "report_path": {"type": "string"},
                     "kind": {"type": "string", "default": "yaxunit"},
                 },
                 "required": ["report_path"],
             }),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        from src.onec_tests.yaxunit import YAxUnitRunner
        from src.onec_tests.vanessa import VanessaRunner

        if name == "tests_info":
            yax = YAxUnitRunner(ib_path=os.getenv("ONEC_IB_PATH", ""))
            vanessa_epf = os.getenv("ONEC_VANESSA_EPF", "")
            van = VanessaRunner(
                ib_path=os.getenv("ONEC_IB_PATH", ""),
                vanessa_epf=vanessa_epf,
            )
            return [TextContent(
                type="text",
                text=json.dumps({
                    "yaxunit": {
                        "available": yax.available(),
                        "binary": str(yax.binary) if yax.binary else None,
                        "ib_path": yax.ib_path or "(не задан)",
                    },
                    "vanessa": {
                        "available": van.available(),
                        "binary": str(van.binary) if van.binary else None,
                        "vanessa_epf": vanessa_epf or "(не задан)",
                        "ib_path": van.ib_path or "(не задан)",
                    },
                }, ensure_ascii=False, indent=2),
            )]

        if name == "run_yaxunit":
            yax = YAxUnitRunner(
                ib_path=os.getenv("ONEC_IB_PATH", ""),
                user=os.getenv("ONEC_DESIGNER_USER", ""),
                password=os.getenv("ONEC_DESIGNER_PASSWORD", ""),
            )
            result = await yax.run(
                module_filter=arguments.get("module_filter", ""),
                report_path=arguments.get("report_path"),
            )
            return [TextContent(
                type="text",
                text=json.dumps({
                    "success": result.success,
                    "total": result.total,
                    "passed": result.passed,
                    "failed": result.failed,
                    "skipped": result.skipped,
                    "duration_ms": round(result.duration_ms, 1),
                    "errors": result.errors,
                    "report_path": result.report_path,
                    "details": result.details[:50],
                }, ensure_ascii=False, indent=2),
            )]

        if name == "run_vanessa":
            van = VanessaRunner(
                ib_path=os.getenv("ONEC_IB_PATH", ""),
                vanessa_epf=os.getenv("ONEC_VANESSA_EPF", ""),
                user=os.getenv("ONEC_DESIGNER_USER", ""),
                password=os.getenv("ONEC_DESIGNER_PASSWORD", ""),
            )
            features_dir = _safe(arguments["features_dir"])
            result = await van.run_features(
                features_dir=features_dir,
                report_path=arguments.get("report_path"),
                filter_=arguments.get("filter", ""),
            )
            return [TextContent(
                type="text",
                text=json.dumps({
                    "success": result.success,
                    "scenarios_total": result.scenarios_total,
                    "scenarios_passed": result.scenarios_passed,
                    "scenarios_failed": result.scenarios_failed,
                    "steps_total": result.steps_total,
                    "steps_passed": result.steps_passed,
                    "steps_failed": result.steps_failed,
                    "duration_ms": round(result.duration_ms, 1),
                    "errors": result.errors,
                    "report_path": result.report_path,
                    "details": result.details[:50],
                }, ensure_ascii=False, indent=2),
            )]

        if name == "list_features":
            d = _safe(arguments["features_dir"])
            if not d.exists():
                return [TextContent(type="text", text="Не найдено")]
            features = list(d.rglob("*.feature"))
            return [TextContent(
                type="text",
                text=json.dumps({
                    "total": len(features),
                    "items": [
                        str(f.relative_to(d)) for f in features[:200]
                    ],
                }, ensure_ascii=False, indent=2),
            )]

        if name == "list_yaxunit_modules":
            raw = arguments.get("search_dir", "")
            d = _safe(raw) if raw else _get_root()
            modules: list[str] = []
            for bsl in d.rglob("*.bsl"):
                try:
                    text = bsl.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                if "ЮТест" in text or "YAxUnit" in text or "Тест_" in bsl.name:
                    modules.append(str(bsl.relative_to(d)))
                    if len(modules) >= 100:
                        break
            return [TextContent(
                type="text",
                text="\n".join(modules) or "Не найдено",
            )]

        if name == "parse_report":
            p = _safe(arguments["report_path"])
            kind = arguments.get("kind", "yaxunit")
            if kind == "yaxunit":
                from src.onec_tests.yaxunit import YAxUnitRunner
                result = YAxUnitRunner._parse_report(p)
            else:
                from src.onec_tests.vanessa import VanessaRunner
                result = VanessaRunner._parse_report(p)
            return [TextContent(
                type="text",
                text=json.dumps(result.__dict__, ensure_ascii=False, indent=2),
            )]

        return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]
    except Exception as e:
        logger.exception("1C Tests tool %s failed", name)
        return [TextContent(type="text", text=f"Ошибка: {e}")]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
