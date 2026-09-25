"""MCP-сервер: тесты."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("testing-mcp")

_last_results: dict = {}


def _get_root() -> Path:
    try:
        from src.runtime_config import get_project_root
        val = get_project_root(default="")
        if val:
            return Path(val).resolve()
    except Exception:
        pass
    return Path(os.getenv("PROJECT_ROOT", os.getcwd())).resolve()


async def _run(cmd, cwd=None, timeout=600):
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(cwd or _get_root()),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            return -1, "", "timeout"
        return (proc.returncode or 0,
                stdout.decode("utf-8", errors="replace"),
                "")
    except FileNotFoundError:
        return -1, "", f"Command not found: {cmd[0]}"
    except Exception as e:
        return -1, "", str(e)


app = Server("testing")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="test_frameworks",
             description="Определить тестовый фреймворк проекта.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="test_discover",
             description="Найти все тесты.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string", "default": "."}}}),
        Tool(name="test_list",
             description="Список тестов (алиас discover).",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string", "default": "."}}}),
        Tool(name="test_run",
             description="Запустить все тесты.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"},
                 "filter": {"type": "string"},
                 "timeout_seconds": {"type": "integer", "default": 600}}}),
        Tool(name="test_run_single",
             description="Запустить один тест по id.",
             inputSchema={"type": "object", "properties": {
                 "test_id": {"type": "string"},
                 "timeout_seconds": {"type": "integer", "default": 120}},
                 "required": ["test_id"]}),
        Tool(name="test_run_file",
             description="Запустить тесты в файле.",
             inputSchema={"type": "object", "properties": {
                 "file": {"type": "string"},
                 "timeout_seconds": {"type": "integer", "default": 300}},
                 "required": ["file"]}),
        Tool(name="test_coverage",
             description="Покрытие кода тестами.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"},
                 "timeout_seconds": {"type": "integer", "default": 900}}}),
        Tool(name="test_generate",
             description="Сгенерировать тесты для файла (требует LLM).",
             inputSchema={"type": "object", "properties": {
                 "file": {"type": "string"},
                 "output": {"type": "string"}},
                 "required": ["file"]}),
        Tool(name="test_flaky_check",
             description="Проверить флаки-тест (N повторов).",
             inputSchema={"type": "object", "properties": {
                 "test_id": {"type": "string"},
                 "runs": {"type": "integer", "default": 5}},
                 "required": ["test_id"]}),
        Tool(name="test_last_results",
             description="Результаты последнего прогона.",
             inputSchema={"type": "object", "properties": {}}),
    ]


def _parse_pytest_output(output: str) -> dict:
    """Парсит вывод pytest."""
    result = {
        "passed": 0, "failed": 0, "errors": 0,
        "skipped": 0, "total": 0, "duration": 0.0,
        "failed_tests": [],
    }

    m = re.search(r"(\d+) passed", output)
    if m:
        result["passed"] = int(m.group(1))
    m = re.search(r"(\d+) failed", output)
    if m:
        result["failed"] = int(m.group(1))
    m = re.search(r"(\d+) error", output)
    if m:
        result["errors"] = int(m.group(1))
    m = re.search(r"(\d+) skipped", output)
    if m:
        result["skipped"] = int(m.group(1))
    m = re.search(r"in ([\d.]+)s", output)
    if m:
        result["duration"] = float(m.group(1))

    result["total"] = (result["passed"] + result["failed"]
                       + result["errors"] + result["skipped"])

    # FAILED test_path::test_name - message
    for m in re.finditer(r"FAILED\s+(\S+)(?:\s+-\s+(.+))?", output):
        result["failed_tests"].append({
            "id": m.group(1),
            "error": (m.group(2) or "").strip()[:200],
        })

    return result


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    global _last_results

    try:
        from src.mcp_servers.testing.frameworks import (
            detect_framework, framework_commands,
        )

        root = _get_root()

        if name == "test_frameworks":
            fw = detect_framework(root)
            return [TextContent(
                type="text",
                text=json.dumps({
                    "framework": fw or "(не определён)",
                    "commands": framework_commands(fw) if fw else {},
                }, ensure_ascii=False, indent=2),
            )]

        if name == "test_last_results":
            return [TextContent(
                type="text",
                text=json.dumps(_last_results, ensure_ascii=False, indent=2)
                     or "{}",
            )]

        fw = detect_framework(root)
        if not fw:
            return [TextContent(
                type="text",
                text="Тестовый фреймворк не определён",
            )]
        cmds = framework_commands(fw)

        # ─── Discover ───────────────────────────────
        if name in ("test_discover", "test_list"):
            cmd = cmds.get("discover", [])
            if not cmd:
                return [TextContent(type="text", text="Discover не поддержан")]
            target = arguments.get("path", ".")
            if target and target != ".":
                cmd = cmd + [target]
            rc, out, err = await _run(cmd, timeout=60)
            lines = [l for l in out.splitlines() if l.strip()][:200]
            return [TextContent(type="text", text="\n".join(lines) or "(пусто)")]

        # ─── Run all ────────────────────────────────
        if name == "test_run":
            cmd = list(cmds.get("run", []))
            target = arguments.get("path")
            filter_ = arguments.get("filter")
            if target:
                cmd.append(target)
            if filter_:
                if fw == "pytest":
                    cmd.extend(["-k", filter_])
                else:
                    cmd.append(filter_)

            t0 = time.perf_counter()
            rc, out, err = await _run(
                cmd, timeout=arguments.get("timeout_seconds", 600),
            )
            duration = time.perf_counter() - t0

            parsed = _parse_pytest_output(out) if fw == "pytest" else \
                     {"raw": out[:5000]}
            parsed["exit_code"] = rc
            parsed["framework"] = fw
            parsed["duration_total"] = round(duration, 2)
            parsed["stdout_tail"] = out[-3000:] if not parsed.get("passed") else ""

            _last_results = parsed
            return [TextContent(
                type="text",
                text=json.dumps(parsed, ensure_ascii=False, indent=2),
            )]

        if name == "test_run_single":
            test_id = arguments["test_id"]
            if fw == "pytest":
                cmd = ["python", "-m", "pytest", "-v", test_id]
            else:
                cmd = list(cmds.get("single", [])) + [test_id]
            rc, out, _ = await _run(
                cmd, timeout=arguments.get("timeout_seconds", 120),
            )
            return [TextContent(
                type="text",
                text=json.dumps({
                    "test_id": test_id,
                    "exit_code": rc,
                    "output": out[-5000:],
                }, ensure_ascii=False, indent=2),
            )]

        if name == "test_run_file":
            f = arguments["file"]
            if fw == "pytest":
                cmd = ["python", "-m", "pytest", "-v", f]
            else:
                cmd = list(cmds.get("run", [])) + [f]
            rc, out, _ = await _run(
                cmd, timeout=arguments.get("timeout_seconds", 300),
            )
            parsed = _parse_pytest_output(out) if fw == "pytest" else {"raw": out[:5000]}
            parsed["exit_code"] = rc
            return [TextContent(
                type="text",
                text=json.dumps(parsed, ensure_ascii=False, indent=2),
            )]

        # ─── Coverage ───────────────────────────────
        if name == "test_coverage":
            if fw != "pytest":
                return [TextContent(
                    type="text",
                    text="Coverage поддержан только для pytest",
                )]

            # Проверяем pytest-cov
            rc, out, _ = await _run(
                ["python", "-c", "import pytest_cov"],
                timeout=10,
            )
            if rc != 0:
                return [TextContent(
                    type="text",
                    text="pytest-cov не установлен: pip install pytest-cov",
                )]

            cmd = [
                "python", "-m", "pytest",
                "--cov", "--cov-report=term-missing",
                "-q",
            ]
            target = arguments.get("path")
            if target:
                cmd.append(target)

            t0 = time.perf_counter()
            rc, out, _ = await _run(
                cmd, timeout=arguments.get("timeout_seconds", 900),
            )
            duration = time.perf_counter() - t0

            # Парсим coverage
            coverage = 0.0
            m = re.search(r"TOTAL\s+\d+\s+\d+\s+(\d+)%", out)
            if m:
                coverage = int(m.group(1))

            return [TextContent(
                type="text",
                text=json.dumps({
                    "coverage_percent": coverage,
                    "duration_seconds": round(duration, 2),
                    "exit_code": rc,
                    "report": out[-5000:],
                }, ensure_ascii=False, indent=2),
            )]

        # ─── Generate ───────────────────────────────
        if name == "test_generate":
            f = arguments["file"]
            return [TextContent(
                type="text",
                text=(
                    f"Для генерации тестов используй агента file + deepseek. "
                    f"Прочитай {f}, сгенерируй тесты через deepseek, "
                    f"запиши в {arguments.get('output', 'tests/test_' + f)}"
                ),
            )]

        # ─── Flaky check ────────────────────────────
        if name == "test_flaky_check":
            test_id = arguments["test_id"]
            runs = arguments.get("runs", 5)
            results = []
            for i in range(runs):
                if fw == "pytest":
                    cmd = ["python", "-m", "pytest", "-q", test_id]
                else:
                    cmd = list(cmds.get("single", [])) + [test_id]
                rc, _, _ = await _run(cmd, timeout=120)
                results.append(rc == 0)

            passed = sum(results)
            return [TextContent(
                type="text",
                text=json.dumps({
                    "test_id": test_id,
                    "runs": runs,
                    "passed": passed,
                    "failed": runs - passed,
                    "flaky": 0 < passed < runs,
                    "results": results,
                }, ensure_ascii=False, indent=2),
            )]

        return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]
    except Exception as e:
        logger.exception("Testing tool %s failed", name)
        return [TextContent(type="text", text=f"Ошибка: {e}")]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
