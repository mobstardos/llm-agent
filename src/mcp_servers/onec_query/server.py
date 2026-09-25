"""MCP-сервер: язык запросов 1С."""
from __future__ import annotations

import asyncio
import json
import logging
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("onec-query-mcp")

app = Server("onec_query")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="parse_query",
             description="Разобрать язык запросов 1С.",
             inputSchema={
                 "type": "object",
                 "properties": {"query": {"type": "string"}},
                 "required": ["query"],
             }),
        Tool(name="validate_query",
             description="Проверить синтаксис запроса.",
             inputSchema={
                 "type": "object",
                 "properties": {"query": {"type": "string"}},
                 "required": ["query"],
             }),
        Tool(name="extract_metadata_refs",
             description="Извлечь ссылки на объекты метаданных.",
             inputSchema={
                 "type": "object",
                 "properties": {"query": {"type": "string"}},
                 "required": ["query"],
             }),
        Tool(name="extract_parameters",
             description="Извлечь параметры (&Параметр).",
             inputSchema={
                 "type": "object",
                 "properties": {"query": {"type": "string"}},
                 "required": ["query"],
             }),
        Tool(name="sql_to_1c",
             description="Конвертировать SQL → язык запросов 1С.",
             inputSchema={
                 "type": "object",
                 "properties": {"sql": {"type": "string"}},
                 "required": ["sql"],
             }),
        Tool(name="onec_to_sql",
             description="Конвертировать язык запросов 1С → SQL.",
             inputSchema={
                 "type": "object",
                 "properties": {"query": {"type": "string"}},
                 "required": ["query"],
             }),
        Tool(name="analyze_query",
             description="Полный анализ: структура + метаданные + параметры.",
             inputSchema={
                 "type": "object",
                 "properties": {"query": {"type": "string"}},
                 "required": ["query"],
             }),
        Tool(name="suggest_optimizations",
             description="Подсказки по оптимизации запроса.",
             inputSchema={
                 "type": "object",
                 "properties": {"query": {"type": "string"}},
                 "required": ["query"],
             }),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        from src.onec_query.parser import QueryParser
        from src.onec_query.validator import QueryValidator
        from src.onec_query.converter import (
            OneCToSqlConverter, SqlTo1CConverter,
        )

        query = arguments.get("query", "")

        if name == "parse_query":
            parser = QueryParser()
            q = parser.parse(query)
            return [TextContent(
                type="text",
                text=json.dumps({
                    "fields": [
                        {"expr": f.expr, "alias": f.alias}
                        for f in q.fields
                    ],
                    "sources": [
                        {"table": s.table, "alias": s.alias,
                         "kind": s.kind, "join": s.join_type}
                        for s in q.sources
                    ],
                    "where": q.where_clause[:500],
                    "group_by": q.group_by[:500],
                    "order_by": q.order_by[:500],
                    "having": q.having[:500],
                    "parameters": q.parameters,
                    "virtual_tables": q.virtual_tables,
                    "metadata_refs": q.metadata_refs,
                    "errors": q.errors,
                    "warnings": q.warnings,
                }, ensure_ascii=False, indent=2),
            )]

        if name == "validate_query":
            validator = QueryValidator()
            errors = validator.validate(query)
            has_errors = any(e.level == "error" for e in errors)
            return [TextContent(
                type="text",
                text=json.dumps({
                    "valid": not has_errors,
                    "errors": [
                        e.message for e in errors if e.level == "error"
                    ],
                    "warnings": [
                        e.message for e in errors if e.level == "warning"
                    ],
                }, ensure_ascii=False, indent=2),
            )]

        if name == "extract_metadata_refs":
            parser = QueryParser()
            q = parser.parse(query)
            return [TextContent(
                type="text",
                text="\n".join(q.metadata_refs) or "Нет ссылок",
            )]

        if name == "extract_parameters":
            parser = QueryParser()
            q = parser.parse(query)
            return [TextContent(
                type="text",
                text="\n".join(q.parameters) or "Нет параметров",
            )]

        if name == "sql_to_1c":
            conv = SqlTo1CConverter()
            result, warnings = conv.convert(arguments.get("sql", ""))
            return [TextContent(
                type="text",
                text=result + "\n\n--- warnings ---\n" + "\n".join(warnings),
            )]

        if name == "onec_to_sql":
            conv = OneCToSqlConverter()
            result, warnings = conv.convert(query)
            return [TextContent(
                type="text",
                text=result + "\n\n--- warnings ---\n" + "\n".join(warnings),
            )]

        if name == "analyze_query":
            parser = QueryParser()
            validator = QueryValidator()
            q = parser.parse(query)
            errs = validator.validate(query)
            return [TextContent(
                type="text",
                text=json.dumps({
                    "structure": {
                        "fields_count": len(q.fields),
                        "sources_count": len(q.sources),
                        "has_where": bool(q.where_clause),
                        "has_group": bool(q.group_by),
                        "has_order": bool(q.order_by),
                    },
                    "metadata_refs": q.metadata_refs,
                    "parameters": q.parameters,
                    "virtual_tables": q.virtual_tables,
                    "validation": {
                        "errors": [e.message for e in errs
                                   if e.level == "error"],
                        "warnings": [e.message for e in errs
                                     if e.level == "warning"],
                    },
                }, ensure_ascii=False, indent=2),
            )]

        if name == "suggest_optimizations":
            parser = QueryParser()
            q = parser.parse(query)
            tips: list[str] = []

            if q.sources and not q.where_clause:
                tips.append(
                    "Нет условий ГДЕ — возможно, читается вся таблица. "
                    "Добавьте фильтр."
                )

            if any("*" in f.expr for f in q.fields):
                tips.append(
                    "Используется *, что может тянуть все поля. "
                    "Перечислите только нужные."
                )

            if len(q.virtual_tables) > 2:
                tips.append(
                    f"Много виртуальных таблиц ({len(q.virtual_tables)}). "
                    "Возможно, стоит использовать временные таблицы."
                )

            if not q.group_by and any(
                any(agg in f.expr.upper()
                    for agg in ("КОЛИЧЕСТВО", "СУММА", "COUNT", "SUM"))
                for f in q.fields
            ):
                tips.append("Агрегат без группировки.")

            if len(q.sources) > 5:
                tips.append(
                    f"Много источников ({len(q.sources)}). "
                    "Проверьте, можно ли разделить запрос."
                )

            if not tips:
                tips.append("Явных проблем не видно.")

            return [TextContent(
                type="text",
                text="\n".join(f"- {t}" for t in tips),
            )]

        return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]
    except Exception as e:
        logger.exception("1C Query tool %s failed", name)
        return [TextContent(type="text", text=f"Ошибка: {e}")]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
