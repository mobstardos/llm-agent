"""MCP-сервер: PostgreSQL с авто-детектом драйвера.

Порядок приоритета: psycopg2 → psycopg3 → pg8000.
"""
from __future__ import annotations

import asyncio
import os
import re

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

DRIVER = None
_psycopg2 = None
_psycopg3 = None
_pg8000 = None

try:
    import psycopg2
    import psycopg2.extras
    _psycopg2 = psycopg2
    DRIVER = "psycopg2"
except ImportError:
    try:
        import psycopg
        from psycopg.rows import dict_row
        _psycopg3 = psycopg
        DRIVER = "psycopg3"
    except ImportError:
        try:
            import pg8000.dbapi as pg8000
            _pg8000 = pg8000
            DRIVER = "pg8000"
        except ImportError:
            DRIVER = None

app = Server("postgres")

READONLY_PREFIXES = ("select", "with", "explain", "show")
FORBIDDEN = re.compile(
    r"\b(drop\s+database|pg_read_file|pg_ls_dir|copy\s+.*\s+from\s+program)\b",
    re.IGNORECASE,
)


def _connect():
    if DRIVER is None:
        raise RuntimeError(
            "Нет драйвера PostgreSQL. Установите: pip install 'psycopg[binary]' "
            "или pip install psycopg2-binary==2.9.12 или pip install pg8000"
        )
    host = os.getenv("PG_HOST", "localhost")
    port = int(os.getenv("PG_PORT", "5432"))
    user = os.getenv("PG_USER", "")
    password = os.getenv("PG_PASSWORD", "")
    database = os.getenv("PG_DATABASE", "")

    if DRIVER == "psycopg2":
        return _psycopg2.connect(
            host=host, port=port, user=user,
            password=password, dbname=database,
        )
    if DRIVER == "psycopg3":
        return _psycopg3.connect(
            host=host, port=port, user=user,
            password=password, dbname=database,
        )
    return _pg8000.connect(
        host=host, port=port, user=user,
        password=password, database=database,
    )


def _execute(sql: str, write: bool) -> str:
    if FORBIDDEN.search(sql):
        return "Отказано: запрещённая операция."
    stripped = sql.strip().lower()
    is_read = stripped.startswith(READONLY_PREFIXES)
    if not write and not is_read:
        return "Отказано: инструмент только для чтения."
    if write and is_read:
        return "Отказано: read-запрос передан в write-инструмент."
    if DRIVER is None:
        return "Ошибка: не установлен драйвер PostgreSQL."

    try:
        conn = _connect()
    except Exception as e:
        return f"Ошибка подключения: {e}"

    try:
        if DRIVER == "psycopg2":
            with conn.cursor(
                cursor_factory=_psycopg2.extras.RealDictCursor,
            ) as cur:
                cur.execute(sql)
                if is_read:
                    rows = cur.fetchall()
                    return _fmt([dict(r) for r in rows])
                conn.commit()
                return f"OK, затронуто строк: {cur.rowcount}"
        elif DRIVER == "psycopg3":
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(sql)
                if is_read:
                    rows = cur.fetchall()
                    return _fmt([dict(r) for r in rows])
                conn.commit()
                return f"OK, затронуто строк: {cur.rowcount}"
        else:
            with conn.cursor() as cur:
                cur.execute(sql)
                if is_read:
                    cols = [d[0] for d in (cur.description or [])]
                    rows = [dict(zip(cols, row)) for row in cur.fetchall()]
                    return _fmt(rows)
                conn.commit()
                return f"OK, затронуто строк: {cur.rowcount}"
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        return f"Ошибка SQL: {e}"
    finally:
        try:
            conn.close()
        except Exception:
            pass


def _fmt(rows: list[dict]) -> str:
    if not rows:
        return "(нет строк)"
    return str(rows[:200])


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="list_schemas", description="Список схем.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="list_tables", description="Список таблиц.",
             inputSchema={
                 "type": "object",
                 "properties": {"schema": {"type": "string", "default": "public"}},
             }),
        Tool(name="describe_table", description="Схема таблицы.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "schema": {"type": "string", "default": "public"},
                     "table": {"type": "string"},
                 },
                 "required": ["table"],
             }),
        Tool(name="run_query", description="SELECT (только чтение).",
             inputSchema={
                 "type": "object",
                 "properties": {"sql": {"type": "string"}},
                 "required": ["sql"],
             }),
        Tool(name="execute_write_query",
             description="INSERT/UPDATE/DELETE. Требует подтверждения.",
             inputSchema={
                 "type": "object",
                 "properties": {"sql": {"type": "string"}},
                 "required": ["sql"],
             }),
        Tool(name="execute_migration",
             description="DDL. Требует подтверждения.",
             inputSchema={
                 "type": "object",
                 "properties": {"sql": {"type": "string"}},
                 "required": ["sql"],
             }),
        Tool(name="driver_info",
             description="Активный драйвер.",
             inputSchema={"type": "object", "properties": {}}),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    if name == "driver_info":
        return [TextContent(
            type="text", text=f"Активный драйвер: {DRIVER or 'не установлен'}",
        )]

    if name == "list_schemas":
        return [TextContent(type="text", text=_execute(
            "SELECT schema_name FROM information_schema.schemata "
            "ORDER BY schema_name", False))]

    if name == "list_tables":
        schema = arguments.get("schema", "public")
        return [TextContent(type="text", text=_execute(
            f"SELECT table_name FROM information_schema.tables "
            f"WHERE table_schema = '{schema}' ORDER BY table_name", False))]

    if name == "describe_table":
        schema = arguments.get("schema", "public")
        table = arguments["table"]
        sql = (
            f"SELECT column_name, data_type, is_nullable, column_default "
            f"FROM information_schema.columns "
            f"WHERE table_schema='{schema}' AND table_name='{table}' "
            f"ORDER BY ordinal_position"
        )
        return [TextContent(type="text", text=_execute(sql, False))]

    if name == "run_query":
        return [TextContent(type="text", text=_execute(arguments["sql"], False))]
    if name == "execute_write_query":
        return [TextContent(type="text", text=_execute(arguments["sql"], True))]
    if name == "execute_migration":
        return [TextContent(type="text", text=_execute(arguments["sql"], True))]

    return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
