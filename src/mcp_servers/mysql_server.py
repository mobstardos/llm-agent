"""MCP-сервер: MySQL и SQLite."""
import os
import re
import sqlite3
from typing import Any

import pymysql
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

app = Server("mysql")

READONLY_PREFIXES = ("select", "with", "explain", "show", "describe", "desc", "pragma")
FORBIDDEN = re.compile(r"\b(drop\s+database|attach\s+database|load_extension)\b", re.IGNORECASE)


def _is_sqlite() -> bool:
    return bool(os.getenv("SQLITE_PATH"))


def _connect() -> Any:
    if _is_sqlite():
        conn = sqlite3.connect(os.environ["SQLITE_PATH"])
        conn.row_factory = sqlite3.Row
        return conn
    return pymysql.connect(
        host=os.getenv("MYSQL_HOST", "localhost"),
        port=int(os.getenv("MYSQL_PORT", "3306")),
        user=os.getenv("MYSQL_USER", ""),
        password=os.getenv("MYSQL_PASSWORD", ""),
        database=os.getenv("MYSQL_DATABASE", "") or None,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )


def _execute(sql: str, write: bool) -> str:
    if FORBIDDEN.search(sql):
        return "Отказано: запрещённая операция."
    stripped = sql.strip().lower()
    is_read = stripped.startswith(READONLY_PREFIXES)
    if not write and not is_read:
        return f"Отказано: запрос не является read-only. Используйте execute_write_query или execute_migration."
    if write and is_read:
        return "Отказано: read-запрос передан в write-инструмент."

    conn = _connect()
    try:
        if _is_sqlite():
            cur = conn.cursor()
            cur.execute(sql)
            if is_read:
                rows = [dict(r) for r in cur.fetchall()]
                return str(rows[:200]) if rows else "(нет строк)"
            conn.commit()
            return f"OK, затронуто строк: {cur.rowcount}"
        else:
            with conn.cursor() as cur:
                cur.execute(sql)
                if is_read:
                    rows = cur.fetchall()
                    return str(rows[:200]) if rows else "(нет строк)"
                conn.commit()
                return f"OK, затронуто строк: {cur.rowcount}"
    except Exception as e:
        conn.rollback()
        return f"Ошибка SQL: {e}"
    finally:
        conn.close()


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="list_tables", description="Список таблиц в текущей БД.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="describe_table", description="Схема таблицы.",
             inputSchema={"type": "object", "properties": {"table": {"type": "string"}},
                          "required": ["table"]}),
        Tool(name="run_query", description="Выполнить SELECT-запрос (только чтение).",
             inputSchema={"type": "object", "properties": {"sql": {"type": "string"}},
                          "required": ["sql"]}),
        Tool(name="execute_write_query",
             description="INSERT/UPDATE/DELETE. ОПАСНО: требует подтверждения пользователя.",
             inputSchema={"type": "object", "properties": {"sql": {"type": "string"}},
                          "required": ["sql"]}),
        Tool(name="execute_migration",
             description="DDL: CREATE/ALTER/DROP. ОПАСНО: требует подтверждения.",
             inputSchema={"type": "object", "properties": {"sql": {"type": "string"}},
                          "required": ["sql"]}),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "list_tables":
        if _is_sqlite():
            sql = "SELECT name FROM sqlite_master WHERE type='table'"
        else:
            sql = "SHOW TABLES"
        return [TextContent(type="text", text=_execute(sql, write=False))]

    if name == "describe_table":
        t = arguments["table"]
        if _is_sqlite():
            return [TextContent(type="text", text=_execute(f"PRAGMA table_info('{t}')", write=False))]
        return [TextContent(type="text", text=_execute(f"SHOW CREATE TABLE `{t}`", write=False))]

    if name == "run_query":
        return [TextContent(type="text", text=_execute(arguments["sql"], write=False))]
    if name == "execute_write_query":
        return [TextContent(type="text", text=_execute(arguments["sql"], write=True))]
    if name == "execute_migration":
        return [TextContent(type="text", text=_execute(arguments["sql"], write=True))]

    return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
