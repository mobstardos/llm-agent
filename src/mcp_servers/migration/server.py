"""MCP-сервер: миграции БД."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("migration-mcp")


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
            return -1, "timeout"
        return (proc.returncode or 0, stdout.decode("utf-8", errors="replace"))
    except FileNotFoundError:
        return -1, f"Command not found: {cmd[0]}"
    except Exception as e:
        return -1, str(e)


def _detect_framework(root: Path) -> str:
    """Определяет фреймворк миграций."""
    if (root / "alembic.ini").exists() or (root / "alembic").is_dir():
        return "alembic"
    # Django
    for manage_py in root.glob("*/manage.py"):
        return "django"
    if (root / "manage.py").exists():
        return "django"
    # Rails
    if (root / "db" / "migrate").exists():
        return "rails"
    # Prisma
    if (root / "prisma" / "schema.prisma").exists():
        return "prisma"
    # Flyway
    if (root / "sql").is_dir() and list(root.glob("sql/V*.sql")):
        return "flyway"
    return ""


app = Server("migration")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="migration_info",
             description="Определить фреймворк миграций.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="migration_list",
             description="Список миграций.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="migration_current",
             description="Текущая ревизия БД.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="migration_history",
             description="История миграций.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="migration_create",
             description="Создать новую миграцию.",
             inputSchema={"type": "object", "properties": {
                 "message": {"type": "string"},
                 "autogenerate": {"type": "boolean", "default": True}},
                 "required": ["message"]}),
        Tool(name="migration_up",
             description="Применить миграции (upgrade).",
             inputSchema={"type": "object", "properties": {
                 "target": {"type": "string", "default": "head"}}}),
        Tool(name="migration_down",
             description="Откатить миграции (downgrade).",
             inputSchema={"type": "object", "properties": {
                 "target": {"type": "string", "default": "-1"}}}),
        Tool(name="migration_stamp",
             description="Пометить БД ревизией без применения.",
             inputSchema={"type": "object", "properties": {
                 "revision": {"type": "string"}},
                 "required": ["revision"]}),
        Tool(name="schema_dump",
             description="Дамп схемы БД.",
             inputSchema={"type": "object", "properties": {
                 "output": {"type": "string", "default": "schema.sql"}}}),
        Tool(name="schema_diff",
             description="Diff двух SQL-дампов схемы.",
             inputSchema={"type": "object", "properties": {
                 "a": {"type": "string"},
                 "b": {"type": "string"}},
                 "required": ["a", "b"]}),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        root = _get_root()
        fw = _detect_framework(root)

        if name == "migration_info":
            return [TextContent(
                type="text",
                text=json.dumps({
                    "framework": fw or "(не определён)",
                    "has_alembic": (root / "alembic.ini").exists(),
                    "has_django": (root / "manage.py").exists()
                                  or any(root.glob("*/manage.py")),
                    "has_prisma": (root / "prisma" / "schema.prisma").exists(),
                }, ensure_ascii=False, indent=2),
            )]

        if not fw:
            return [TextContent(
                type="text",
                text="Фреймворк миграций не определён. "
                     "Поддерживаются: Alembic, Django, Rails, Prisma.",
            )]

        # ─── Alembic ───────────────────────────────
        if fw == "alembic":
            if not shutil.which("alembic") and not (root / "alembic.ini").exists():
                return [TextContent(type="text", text="alembic не найден")]

            cmd_base = ["alembic"]

            if name == "migration_list":
                rc, out = await _run(cmd_base + ["history", "--verbose"], timeout=60)
                return [TextContent(type="text", text=out[:10000])]

            if name == "migration_current":
                rc, out = await _run(cmd_base + ["current"], timeout=60)
                return [TextContent(type="text", text=out or "(нет данных)")]

            if name == "migration_history":
                rc, out = await _run(cmd_base + ["history"], timeout=60)
                return [TextContent(type="text", text=out[:10000])]

            if name == "migration_create":
                message = arguments["message"]
                cmd = cmd_base + ["revision", "-m", message]
                if arguments.get("autogenerate", True):
                    cmd.append("--autogenerate")
                rc, out = await _run(cmd, timeout=120)
                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "exit_code": rc, "output": out[-5000:],
                    }, ensure_ascii=False, indent=2),
                )]

            if name == "migration_up":
                target = arguments.get("target", "head")
                rc, out = await _run(
                    cmd_base + ["upgrade", target], timeout=600,
                )
                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "target": target, "exit_code": rc,
                        "output": out[-5000:],
                    }, ensure_ascii=False, indent=2),
                )]

            if name == "migration_down":
                target = arguments.get("target", "-1")
                rc, out = await _run(
                    cmd_base + ["downgrade", target], timeout=600,
                )
                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "target": target, "exit_code": rc,
                        "output": out[-5000:],
                    }, ensure_ascii=False, indent=2),
                )]

            if name == "migration_stamp":
                rev = arguments["revision"]
                rc, out = await _run(cmd_base + ["stamp", rev], timeout=120)
                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "revision": rev, "exit_code": rc,
                        "output": out[-3000:],
                    }, ensure_ascii=False, indent=2),
                )]

        # ─── Django ────────────────────────────────
        if fw == "django":
            manage = root / "manage.py"
            if not manage.exists():
                for p in root.glob("*/manage.py"):
                    manage = p
                    break

            cmd_base = ["python", str(manage)]

            if name in ("migration_list", "migration_history"):
                rc, out = await _run(
                    cmd_base + ["showmigrations", "--plan"], timeout=60,
                )
                return [TextContent(type="text", text=out[:10000])]

            if name == "migration_current":
                rc, out = await _run(
                    cmd_base + ["showmigrations"], timeout=60,
                )
                return [TextContent(type="text", text=out[:10000])]

            if name == "migration_create":
                app = arguments.get("app", ".")
                rc, out = await _run(
                    cmd_base + ["makemigrations", app], timeout=120,
                )
                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "exit_code": rc, "output": out[-5000:],
                    }, ensure_ascii=False, indent=2),
                )]

            if name == "migration_up":
                app = arguments.get("app", "")
                cmd = cmd_base + ["migrate"] + ([app] if app else [])
                rc, out = await _run(cmd, timeout=600)
                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "exit_code": rc, "output": out[-5000:],
                    }, ensure_ascii=False, indent=2),
                )]

            if name == "migration_down":
                app = arguments.get("app", "")
                target = arguments.get("target", "zero")
                cmd = cmd_base + ["migrate"] + ([app] if app else []) + [target]
                rc, out = await _run(cmd, timeout=600)
                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "target": target, "exit_code": rc,
                        "output": out[-5000:],
                    }, ensure_ascii=False, indent=2),
                )]

        # ─── Prisma ────────────────────────────────
        if fw == "prisma":
            if name == "migration_list":
                rc, out = await _run(
                    ["npx", "prisma", "migrate", "status"], timeout=120,
                )
                return [TextContent(type="text", text=out[:10000])]

            if name == "migration_current":
                rc, out = await _run(
                    ["npx", "prisma", "migrate", "status"], timeout=120,
                )
                return [TextContent(type="text", text=out[:10000])]

            if name == "migration_create":
                msg = arguments["message"]
                rc, out = await _run(
                    ["npx", "prisma", "migrate", "dev", "--name", msg],
                    timeout=600,
                )
                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "exit_code": rc, "output": out[-5000:],
                    }, ensure_ascii=False, indent=2),
                )]

            if name == "migration_up":
                rc, out = await _run(
                    ["npx", "prisma", "migrate", "deploy"], timeout=600,
                )
                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "exit_code": rc, "output": out[-5000:],
                    }, ensure_ascii=False, indent=2),
                )]

        # ─── Schema diff / dump ────────────────────
        if name == "schema_dump":
            # Пытаемся через SQLite
            sqlite_path = os.getenv("SQLITE_PATH", "").strip()
            if sqlite_path and Path(sqlite_path).exists():
                import sqlite3
                output = arguments.get("output", "schema.sql")
                out_path = root / output

                with sqlite3.connect(sqlite_path) as conn:
                    schema = conn.execute(
                        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"
                    ).fetchall()
                out_path.write_text(
                    "\n\n".join(row[0] for row in schema),
                    encoding="utf-8",
                )
                return [TextContent(
                    type="text", text=f"Дамп схемы: {out_path}",
                )]

            # Для PostgreSQL/MySQL — через pg_dump/mysqldump
            if shutil.which("pg_dump") and os.getenv("PG_HOST"):
                output = arguments.get("output", "schema.sql")
                out_path = root / output
                cmd = ["pg_dump", "-s", "-h", os.getenv("PG_HOST", "localhost")]
                if os.getenv("PG_USER"):
                    cmd.extend(["-U", os.getenv("PG_USER")])
                if os.getenv("PG_DATABASE"):
                    cmd.append(os.getenv("PG_DATABASE"))
                rc, out = await _run(cmd, timeout=120)
                if rc == 0:
                    out_path.write_text(out, encoding="utf-8")
                    return [TextContent(
                        type="text", text=f"Дамп схемы: {out_path}",
                    )]

            return [TextContent(
                type="text",
                text="Не удалось получить дамп (нет SQLITE_PATH или pg_dump)",
            )]

        if name == "schema_diff":
            a = root / arguments["a"]
            b = root / arguments["b"]
            if not a.exists() or not b.exists():
                return [TextContent(type="text", text="Файлы не найдены")]
            import difflib
            text_a = a.read_text(encoding="utf-8").splitlines(keepends=True)
            text_b = b.read_text(encoding="utf-8").splitlines(keepends=True)
            diff = "".join(difflib.unified_diff(
                text_a, text_b,
                fromfile=arguments["a"], tofile=arguments["b"],
            ))
            return [TextContent(type="text", text=diff[:30000] or "(идентичны)")]

        return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]
    except Exception as e:
        logger.exception("Migration tool %s failed", name)
        return [TextContent(type="text", text=f"Ошибка: {e}")]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
