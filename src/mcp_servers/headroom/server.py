# -*- coding: utf-8 -*-
"""Headroom MCP — интеграция с headroom-ai (cross-agent memory).

Headroom — это CLI-утилита для сжатия контекста LLM и cross-agent memory.
PyPI: `pip install headroom-ai[all]` (Python 3.10+)

Tools:
  - headroom_compress: сжать контекст для экономии токенов
  - headroom_save_memory: сохранить факт в cross-agent memory
  - headroom_load_memory: загрузить факты из memory
  - headroom_learn: майнинг неудачных сессий → коррекции в CLAUDE.md
  - headroom_status: состояние памяти и подключений

Env:
  - HEADROOM_BIN: путь к headroom CLI (по умолчанию 'headroom' в PATH)
  - HEADROOM_PROJECT_ROOT: корень проекта для контекста

GitHub: https://github.com/headroom-ai/headroom (предположительно)
PyPI: https://pypi.org/project/headroom-ai/
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
from typing import Any

from mcp import Server
from mcp.server import NotificationOptions
from mcp.server.models import InitializationOptions

logger = logging.getLogger(__name__)


def _find_headroom_bin() -> str | None:
    """Найти headroom CLI в PATH или через HEADROOM_BIN env."""
    explicit = os.getenv("HEADROOM_BIN")
    if explicit and shutil.which(explicit):
        return explicit
    return shutil.which("headroom") or shutil.which("headroom-ai")


async def _run_headroom(args: list[str], input_text: str = None) -> dict:
    """Запуск headroom CLI как subprocess."""
    bin_path = _find_headroom_bin()
    if not bin_path:
        return {
            "error": "headroom не установлен. Установите: pip install 'headroom-ai[all]'",
            "hint": "PyPI: https://pypi.org/project/headroom-ai/",
        }
    try:
        proc = await asyncio.create_subprocess_exec(
            bin_path, *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.PIPE if input_text else None,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(
            input_text.encode() if input_text else None
        ), timeout=60)
        return {
            "exit_code": proc.returncode,
            "stdout": stdout.decode("utf-8", errors="replace")[:5000],
            "stderr": stderr.decode("utf-8", errors="replace")[:2000],
        }
    except asyncio.TimeoutError:
        return {"error": "headroom timed out after 60s"}
    except Exception as e:
        return {"error": str(e)}


async def headroom_compress(text: str, model: str = "auto") -> dict:
    """Сжать контекст для экономии токенов LLM."""
    return await _run_headroom(["compress", "--model", model, "--text", text])


async def headroom_save_memory(fact: str, namespace: str = "default") -> dict:
    """Сохранить факт в cross-agent memory.

    Claude Code сохраняет факт — Codex/Cursor/Cline смогут прочитать.
    """
    return await _run_headroom(["memory", "save", "--fact", fact,
                                  "--namespace", namespace])


async def headroom_load_memory(query: str = "", namespace: str = "default") -> dict:
    """Загрузить факты из cross-agent memory по запросу."""
    args = ["memory", "load", "--namespace", namespace]
    if query:
        args.extend(["--query", query])
    return await _run_headroom(args)


async def headroom_learn() -> dict:
    """Майнинг неудачных сессий → автоматические коррекции в CLAUDE.md.

    Headroom анализирует логи ошибок, выписывает pattern → правило → в CLAUDE.md
    """
    return await _run_headroom(["learn", "--auto-write"])


async def headroom_status() -> dict:
    """Состояние cross-agent memory: namespaces, факты, подключения."""
    return await _run_headroom(["status", "--json"])


app = Server("headroom-mcp")


@app.list_tools()
async def list_tools() -> list:
    from mcp.types import Tool
    return [
        Tool(
            name="headroom_compress",
            description="Сжать контекст для экономии токенов LLM",
            inputSchema={
                "type": "object",
                "properties": {
                    "text": {"type": "string", "description": "Длинный контекст для сжатия"},
                    "model": {"type": "string", "default": "auto",
                              "description": "Модель сжатия (auto/fast/balanced)"},
                },
                "required": ["text"],
            },
        ),
        Tool(
            name="headroom_save_memory",
            description="Сохранить факт в cross-agent memory (доступно другим агентам)",
            inputSchema={
                "type": "object",
                "properties": {
                    "fact": {"type": "string", "description": "Факт для сохранения"},
                    "namespace": {"type": "string", "default": "default",
                                  "description": "Namespace памяти"},
                },
                "required": ["fact"],
            },
        ),
        Tool(
            name="headroom_load_memory",
            description="Загрузить факты из cross-agent memory",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Поисковый запрос"},
                    "namespace": {"type": "string", "default": "default"},
                },
            },
        ),
        Tool(
            name="headroom_learn",
            description="Майнинг неудачных сессий → коррекции в CLAUDE.md",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="headroom_status",
            description="Состояние cross-agent memory",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list:
    from mcp.types import TextContent
    import json
    try:
        if name == "headroom_compress":
            result = await headroom_compress(**arguments)
        elif name == "headroom_save_memory":
            result = await headroom_save_memory(**arguments)
        elif name == "headroom_load_memory":
            result = await headroom_load_memory(**arguments)
        elif name == "headroom_learn":
            result = await headroom_learn()
        elif name == "headroom_status":
            result = await headroom_status()
        else:
            result = {"error": f"Unknown tool: {name}"}
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, default=str))]
    except Exception as e:
        logger.exception("headroom tool %s failed", name)
        return [TextContent(type="text", text=json.dumps({"error": str(e)}, ensure_ascii=False))]


async def main():
    from mcp.server.stdio import stdio_server
    init_options = InitializationOptions(
        server_name="headroom-mcp", server_version="1.0.0",
        capabilities=NotificationOptions(),
    )
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, init_options)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
