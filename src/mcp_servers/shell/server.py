"""MCP-сервер: shell с allowlist."""
from __future__ import annotations

import asyncio
import os
import shlex
import shutil
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

app = Server("shell")

DEFAULT_ALLOWLIST = {
    "pytest", "python", "python3", "pip", "ruff", "black",
    "npm", "npx", "node", "git", "make", "ls", "cat", "grep", "find",
    "echo", "which", "env", "pwd",
}


def _root() -> Path:
    try:
        from src.runtime_config import get_project_root
        val = get_project_root(default="")
        if val:
            return Path(val).resolve()
    except Exception:
        pass
    val = os.getenv("PROJECT_ROOT", "").strip()
    return Path(val or os.getcwd()).resolve()


def _is_allowed(command: str) -> tuple[bool, str]:
    try:
        parts = shlex.split(command)
    except ValueError as e:
        return False, f"Ошибка разбора команды: {e}"
    if not parts:
        return False, "Пустая команда"
    binary = parts[0]
    # Убираем .exe/.cmd на Windows
    stem = Path(binary).stem
    if stem not in DEFAULT_ALLOWLIST:
        return False, f"Команда '{stem}' не в allowlist"
    return True, ""


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="run",
             description="Выполнить allowlist-команду.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "command": {"type": "string"},
                     "timeout_seconds": {"type": "integer", "default": 60},
                 },
                 "required": ["command"],
             }),
        Tool(name="which",
             description="Найти бинарь.",
             inputSchema={
                 "type": "object",
                 "properties": {"name": {"type": "string"}},
                 "required": ["name"],
             }),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "which":
        return [TextContent(
            type="text",
            text=shutil.which(arguments["name"]) or "(не найден)",
        )]

    if name == "run":
        command = arguments["command"]
        timeout = arguments.get("timeout_seconds", 60)

        allowed, err = _is_allowed(command)
        if not allowed:
            return [TextContent(type="text", text=f"Отказано: {err}")]

        try:
            proc = await asyncio.create_subprocess_shell(
                command,
                cwd=str(_root()),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            try:
                stdout, _ = await asyncio.wait_for(
                    proc.communicate(), timeout=timeout,
                )
            except asyncio.TimeoutError:
                proc.kill()
                return [TextContent(type="text", text="(timeout)")]
            text = stdout.decode("utf-8", errors="replace")
            return [TextContent(
                type="text",
                text=f"exit={proc.returncode}\n{text[:10000]}",
            )]
        except Exception as e:
            return [TextContent(type="text", text=f"Ошибка: {e}")]

    return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
