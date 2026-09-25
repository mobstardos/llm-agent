"""MCP-сервер: отладка и профилирование."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("debug-mcp")


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


async def _run(cmd, cwd=None, timeout=300, env=None):
    merged_env = {**os.environ, **(env or {})}
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=str(cwd or _get_root()),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=merged_env,
        )
        try:
            stdout, _ = await asyncio.wait_for(
                proc.communicate(), timeout=timeout,
            )
        except asyncio.TimeoutError:
            proc.kill()
            return -1, "timeout"
        return (proc.returncode or 0, stdout.decode("utf-8", errors="replace"))
    except FileNotFoundError:
        return -1, f"Command not found: {cmd[0]}"
    except Exception as e:
        return -1, str(e)


app = Server("debug")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="debug_tools",
             description="Доступные инструменты отладки.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="profile_cpu",
             description="CPU-профиль скрипта (cProfile).",
             inputSchema={"type": "object", "properties": {
                 "script": {"type": "string"},
                 "args": {"type": "array", "items": {"type": "string"}},
                 "top_n": {"type": "integer", "default": 20},
                 "timeout_seconds": {"type": "integer", "default": 300}},
                 "required": ["script"]}),
        Tool(name="profile_memory",
             description="Профиль памяти (tracemalloc).",
             inputSchema={"type": "object", "properties": {
                 "script": {"type": "string"},
                 "args": {"type": "array", "items": {"type": "string"}},
                 "top_n": {"type": "integer", "default": 20},
                 "timeout_seconds": {"type": "integer", "default": 300}},
                 "required": ["script"]}),
        Tool(name="trace_calls",
             description="Трассировка вызовов функций (sys.settrace).",
             inputSchema={"type": "object", "properties": {
                 "script": {"type": "string"},
                 "pattern": {"type": "string"},
                 "timeout_seconds": {"type": "integer", "default": 120}},
                 "required": ["script"]}),
        Tool(name="py_spy_record",
             description="Записать профиль py-spy (требует py-spy).",
             inputSchema={"type": "object", "properties": {
                 "script": {"type": "string"},
                 "output": {"type": "string"},
                 "duration": {"type": "integer", "default": 30}},
                 "required": ["script"]}),
        Tool(name="py_spy_top",
             description="py-spy top-режим (N секунд).",
             inputSchema={"type": "object", "properties": {
                 "pid": {"type": "integer"},
                 "duration": {"type": "integer", "default": 10}},
                 "required": ["pid"]}),
        Tool(name="py_spy_dump",
             description="Снять stack trace процесса.",
             inputSchema={"type": "object", "properties": {
                 "pid": {"type": "integer"}},
                 "required": ["pid"]}),
        Tool(name="run_with_pdb",
             description="Запустить скрипт с точкой останова (batch).",
             inputSchema={"type": "object", "properties": {
                 "script": {"type": "string"},
                 "commands": {"type": "array", "items": {"type": "string"}},
                 "timeout_seconds": {"type": "integer", "default": 60}},
                 "required": ["script"]}),
        Tool(name="benchmark_run",
             description="Запустить скрипт N раз, измерить время.",
             inputSchema={"type": "object", "properties": {
                 "script": {"type": "string"},
                 "runs": {"type": "integer", "default": 5},
                 "timeout_seconds": {"type": "integer", "default": 600}},
                 "required": ["script"]}),
        Tool(name="timing_wrap",
             description="Замерить время выполнения фрагмента кода.",
             inputSchema={"type": "object", "properties": {
                 "code": {"type": "string"},
                 "runs": {"type": "integer", "default": 1}},
                 "required": ["code"]}),
        Tool(name="parse_profile",
             description="Разобрать .prof файл cProfile.",
             inputSchema={"type": "object", "properties": {
                 "profile_path": {"type": "string"},
                 "top_n": {"type": "integer", "default": 20}},
                 "required": ["profile_path"]}),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        root = _get_root()

        if name == "debug_tools":
            return [TextContent(
                type="text",
                text=json.dumps({
                    "python": shutil.which("python") is not None,
                    "py_spy": shutil.which("py-spy") is not None,
                    "pdb": True,  # встроен в Python
                    "memray": shutil.which("memray") is not None,
                }, ensure_ascii=False, indent=2),
            )]

        # ─── CPU profile ────────────────────────────
        if name == "profile_cpu":
            script = _safe(arguments["script"])
            if not script.exists():
                return [TextContent(type="text", text="Скрипт не найден")]

            top_n = arguments.get("top_n", 20)
            extra_args = arguments.get("args", [])

            profile_file = Path(tempfile.gettempdir()) / f"cpu_{int(time.time())}.prof"

            code = (
                f"import cProfile, sys, pstats, io\n"
                f"sys.argv = [{repr(str(script))}] + {extra_args!r}\n"
                f"cProfile.run('exec(open({str(script)!r}).read())', "
                f"{str(profile_file)!r})\n"
                f"p = pstats.Stats({str(profile_file)!r})\n"
                f"p.sort_stats('cumulative').print_stats({top_n})\n"
            )

            rc, out = await _run(
                ["python", "-c", code],
                timeout=arguments.get("timeout_seconds", 300),
            )
            return [TextContent(
                type="text",
                text=json.dumps({
                    "exit_code": rc,
                    "profile_file": str(profile_file),
                    "report": out[-8000:],
                }, ensure_ascii=False, indent=2),
            )]

        # ─── Memory profile ─────────────────────────
        if name == "profile_memory":
            script = _safe(arguments["script"])
            top_n = arguments.get("top_n", 20)
            extra_args = arguments.get("args", [])

            code = (
                f"import tracemalloc, sys\n"
                f"sys.argv = [{repr(str(script))}] + {extra_args!r}\n"
                f"tracemalloc.start()\n"
                f"exec(open({str(script)!r}).read())\n"
                f"snapshot = tracemalloc.take_snapshot()\n"
                f"top = snapshot.statistics('lineno')\n"
                f"for i, stat in enumerate(top[:{top_n}]):\n"
                f"    print(f'{{i+1}}. {{stat}}')\n"
            )

            rc, out = await _run(
                ["python", "-c", code],
                timeout=arguments.get("timeout_seconds", 300),
            )
            return [TextContent(
                type="text",
                text=f"exit_code={rc}\n\n{out[-8000:]}",
            )]

        # ─── Trace calls ────────────────────────────
        if name == "trace_calls":
            script = _safe(arguments["script"])
            pattern = arguments.get("pattern", "")

            code = f"""
import sys
pattern = {repr(pattern)}
def tracer(frame, event, arg):
    if event == 'call':
        name = frame.f_code.co_name
        if not pattern or pattern in name:
            print(f"CALL {{name}} {{frame.f_code.co_filename}}:{{frame.f_lineno}}")
    return tracer
sys.settrace(tracer)
exec(open({str(script)!r}).read())
sys.settrace(None)
"""
            rc, out = await _run(
                ["python", "-c", code],
                timeout=arguments.get("timeout_seconds", 120),
            )
            lines = out.splitlines()[:500]
            return [TextContent(type="text", text="\n".join(lines))]

        # ─── py-spy ─────────────────────────────────
        if name == "py_spy_record":
            if not shutil.which("py-spy"):
                return [TextContent(
                    type="text", text="py-spy не установлен: pip install py-spy",
                )]
            script = _safe(arguments["script"])
            output = arguments.get("output", "profile.txt")
            duration = arguments.get("duration", 30)

            out_path = _safe(output)
            cmd = [
                "py-spy", "record",
                "--format", "speedscope",
                "--duration", str(duration),
                "--output", str(out_path),
                "--", "python", str(script),
            ]
            rc, out = await _run(cmd, timeout=duration + 30)
            return [TextContent(
                type="text",
                text=json.dumps({
                    "exit_code": rc,
                    "profile_file": str(out_path),
                    "output": out[-2000:],
                }, ensure_ascii=False, indent=2),
            )]

        if name == "py_spy_top":
            if not shutil.which("py-spy"):
                return [TextContent(type="text", text="py-spy не установлен")]
            pid = arguments["pid"]
            duration = arguments.get("duration", 10)
            rc, out = await _run(
                ["py-spy", "top", "--duration", str(duration), "--pid", str(pid)],
                timeout=duration + 15,
            )
            return [TextContent(type="text", text=out[-8000:])]

        if name == "py_spy_dump":
            if not shutil.which("py-spy"):
                return [TextContent(type="text", text="py-spy не установлен")]
            pid = arguments["pid"]
            rc, out = await _run(
                ["py-spy", "dump", "--pid", str(pid)], timeout=30,
            )
            return [TextContent(type="text", text=out[-8000:])]

        # ─── pdb ────────────────────────────────────
        if name == "run_with_pdb":
            script = _safe(arguments["script"])
            commands = arguments.get("commands", ["continue"])
            cmd_str = "; ".join(commands)

            code = (
                f"import pdb, sys\n"
                f"sys.argv = [{repr(str(script))}]\n"
                f"pdb.run(compile(open({str(script)!r}).read(), "
                f"{str(script)!r}, 'exec'), commands={repr(cmd_str)})\n"
            )
            rc, out = await _run(
                ["python", "-c", code],
                timeout=arguments.get("timeout_seconds", 60),
            )
            return [TextContent(type="text", text=out[-8000:])]

        # ─── Benchmark ──────────────────────────────
        if name == "benchmark_run":
            script = _safe(arguments["script"])
            runs = arguments.get("runs", 5)

            times: list[float] = []
            for i in range(runs):
                t0 = time.perf_counter()
                rc, _ = await _run(
                    ["python", str(script)],
                    timeout=arguments.get("timeout_seconds", 600) // runs,
                )
                duration = time.perf_counter() - t0
                if rc == 0:
                    times.append(duration)

            if not times:
                return [TextContent(type="text", text="Все прогоны упали")]

            times.sort()
            import statistics
            return [TextContent(
                type="text",
                text=json.dumps({
                    "runs_completed": len(times),
                    "min_sec": round(times[0], 4),
                    "max_sec": round(times[-1], 4),
                    "median_sec": round(statistics.median(times), 4),
                    "mean_sec": round(statistics.mean(times), 4),
                    "all": [round(t, 4) for t in times],
                }, ensure_ascii=False, indent=2),
            )]

        # ─── Timing wrapper ─────────────────────────
        if name == "timing_wrap":
            code = arguments["code"]
            runs = arguments.get("runs", 1)

            wrapper = (
                f"import time\n"
                f"t0 = time.perf_counter()\n"
                f"for _ in range({runs}):\n"
                f"    exec({repr(code)})\n"
                f"duration = time.perf_counter() - t0\n"
                f"print(f'Total: {{duration:.4f}}s for {runs} runs')\n"
                f"print(f'Per run: {{duration / {runs}:.6f}}s')\n"
            )
            rc, out = await _run(["python", "-c", wrapper], timeout=120)
            return [TextContent(type="text", text=out)]

        # ─── Parse profile ──────────────────────────
        if name == "parse_profile":
            profile_path = _safe(arguments["profile_path"])
            if not profile_path.exists():
                return [TextContent(type="text", text="Файл не найден")]
            top_n = arguments.get("top_n", 20)

            code = (
                f"import pstats\n"
                f"p = pstats.Stats({repr(str(profile_path))})\n"
                f"p.sort_stats('cumulative').print_stats({top_n})\n"
            )
            rc, out = await _run(["python", "-c", code], timeout=60)
            return [TextContent(type="text", text=out[-8000:])]

        return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]
    except Exception as e:
        logger.exception("Debug tool %s failed", name)
        return [TextContent(type="text", text=f"Ошибка: {e}")]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
