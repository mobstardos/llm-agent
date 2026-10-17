"""MCP-сервер: Redis, MongoDB, MSSQL, ElasticSearch."""
from __future__ import annotations

import json
import asyncio
import logging
import os
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("db-extended-mcp")


def _json_result(data) -> str:
    try:
        return json.dumps(data, ensure_ascii=False, indent=2, default=str)[:50000]
    except Exception:
        return str(data)[:50000]


app = Server("db_extended")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="db_available",
             description="Какие БД настроены и доступны.",
             inputSchema={"type": "object", "properties": {}}),
        # Redis
        Tool(name="redis_get",
             description="GET по ключу (Redis).",
             inputSchema={"type": "object", "properties": {
                 "key": {"type": "string"},
                 "type": {"type": "string", "default": "string"}},
                 "required": ["key"]}),
        Tool(name="redis_set",
             description="SET значения по ключу.",
             inputSchema={"type": "object", "properties": {
                 "key": {"type": "string"},
                 "value": {"type": "string"},
                 "ttl": {"type": "integer"}},
                 "required": ["key", "value"]}),
        Tool(name="redis_delete",
             description="DEL по ключу.",
             inputSchema={"type": "object", "properties": {
                 "key": {"type": "string"}},
                 "required": ["key"]}),
        Tool(name="redis_keys",
             description="KEYS по паттерну.",
             inputSchema={"type": "object", "properties": {
                 "pattern": {"type": "string", "default": "*"},
                 "limit": {"type": "integer", "default": 100}}}),
        Tool(name="redis_info",
             description="INFO о Redis.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="redis_publish",
             description="PUBLISH в канал Redis.",
             inputSchema={"type": "object", "properties": {
                 "channel": {"type": "string"},
                 "message": {"type": "string"}},
                 "required": ["channel", "message"]}),
        # MongoDB
        Tool(name="mongo_collections",
             description="Список коллекций MongoDB.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="mongo_find",
             description="Найти документы в MongoDB.",
             inputSchema={"type": "object", "properties": {
                 "collection": {"type": "string"},
                 "filter": {"type": "object"},
                 "limit": {"type": "integer", "default": 20}},
                 "required": ["collection"]}),
        Tool(name="mongo_insert",
             description="Вставить документ MongoDB.",
             inputSchema={"type": "object", "properties": {
                 "collection": {"type": "string"},
                 "document": {"type": "object"}},
                 "required": ["collection", "document"]}),
        Tool(name="mongo_update",
             description="Обновить документы MongoDB.",
             inputSchema={"type": "object", "properties": {
                 "collection": {"type": "string"},
                 "filter": {"type": "object"},
                 "update": {"type": "object"},
                 "multi": {"type": "boolean", "default": False}},
                 "required": ["collection", "filter", "update"]}),
        Tool(name="mongo_delete",
             description="Удалить документы MongoDB.",
             inputSchema={"type": "object", "properties": {
                 "collection": {"type": "string"},
                 "filter": {"type": "object"},
                 "multi": {"type": "boolean", "default": False}},
                 "required": ["collection", "filter"]}),
        Tool(name="mongo_aggregate",
             description="Aggregate pipeline MongoDB.",
             inputSchema={"type": "object", "properties": {
                 "collection": {"type": "string"},
                 "pipeline": {"type": "array"}},
                 "required": ["collection", "pipeline"]}),
        # MSSQL
        Tool(name="mssql_tables",
             description="Список таблиц MSSQL.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="mssql_describe",
             description="Схема таблицы MSSQL.",
             inputSchema={"type": "object", "properties": {
                 "table": {"type": "string"}},
                 "required": ["table"]}),
        Tool(name="mssql_query",
             description="SQL-запрос к MSSQL (SELECT).",
             inputSchema={"type": "object", "properties": {
                 "sql": {"type": "string"},
                 "write": {"type": "boolean", "default": False}},
                 "required": ["sql"]}),
        # ElasticSearch
        Tool(name="es_indices",
             description="Список индексов ElasticSearch.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="es_search",
             description="Поиск в ES.",
             inputSchema={"type": "object", "properties": {
                 "index": {"type": "string"},
                 "query": {"type": "object"},
                 "size": {"type": "integer", "default": 20}},
                 "required": ["index"]}),
        Tool(name="es_mapping",
             description="Mapping индекса ES.",
             inputSchema={"type": "object", "properties": {
                 "index": {"type": "string"}},
                 "required": ["index"]}),
        Tool(name="es_count",
             description="Количество документов в индексе.",
             inputSchema={"type": "object", "properties": {
                 "index": {"type": "string"}},
                 "required": ["index"]}),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        # ═══════════════════════════════════════════════════════
        # Available
        # ═══════════════════════════════════════════════════════
        if name == "db_available":
            return [TextContent(
                type="text",
                text=_json_result({
                    "redis": {
                        "configured": bool(os.getenv("REDIS_URL")),
                        "url": os.getenv("REDIS_URL", "")[:40] + "..." if os.getenv("REDIS_URL") else "",
                    },
                    "mongodb": {
                        "configured": bool(os.getenv("MONGO_URL")),
                        "db": os.getenv("MONGO_DB", ""),
                    },
                    "mssql": {
                        "configured": bool(os.getenv("MSSQL_HOST")),
                        "host": os.getenv("MSSQL_HOST", ""),
                        "db": os.getenv("MSSQL_DB", ""),
                    },
                    "elasticsearch": {
                        "configured": bool(os.getenv("ES_URL")),
                        "url": os.getenv("ES_URL", ""),
                    },
                }),
            )]

        # ═══════════════════════════════════════════════════════
        # Redis
        # ═══════════════════════════════════════════════════════
        if name.startswith("redis_"):
            redis_url = os.getenv("REDIS_URL", "").strip()
            if not redis_url:
                return [TextContent(
                    type="text", text="REDIS_URL не задан в .env",
                )]
            try:
                import redis.asyncio as aioredis
            except ImportError:
                return [TextContent(
                    type="text", text="redis не установлен: pip install redis",
                )]

            client = aioredis.from_url(redis_url, decode_responses=True)
            try:
                if name == "redis_get":
                    key = arguments["key"]
                    t = arguments.get("type", "string")
                    if t == "string":
                        val = await client.get(key)
                    elif t == "hash":
                        val = await client.hgetall(key)
                    elif t == "list":
                        val = await client.lrange(key, 0, -1)
                    elif t == "set":
                        val = list(await client.smembers(key))
                    elif t == "zset":
                        val = await client.zrange(key, 0, -1, withscores=True)
                    else:
                        val = await client.get(key)
                    return [TextContent(
                        type="text",
                        text=_json_result({"key": key, "value": val}),
                    )]

                if name == "redis_set":
                    key = arguments["key"]
                    val = arguments["value"]
                    ttl = arguments.get("ttl")
                    if ttl:
                        await client.setex(key, ttl, val)
                    else:
                        await client.set(key, val)
                    return [TextContent(type="text", text=f"OK: {key}")]

                if name == "redis_delete":
                    n = await client.delete(arguments["key"])
                    return [TextContent(
                        type="text", text=f"Удалено ключей: {n}",
                    )]

                if name == "redis_keys":
                    pat = arguments.get("pattern", "*")
                    limit = arguments.get("limit", 100)
                    keys = []
                    async for k in client.scan_iter(match=pat, count=1000):
                        keys.append(k)
                        if len(keys) >= limit:
                            break
                    return [TextContent(
                        type="text",
                        text=_json_result({"count": len(keys), "keys": keys}),
                    )]

                if name == "redis_info":
                    info = await client.info()
                    subset = {
                        k: v for k, v in info.items()
                        if k in ("redis_version", "uptime_in_seconds",
                                 "used_memory_human", "connected_clients",
                                 "total_commands_processed", "role")
                    }
                    return [TextContent(type="text", text=_json_result(subset))]

                if name == "redis_publish":
                    n = await client.publish(
                        arguments["channel"], arguments["message"],
                    )
                    return [TextContent(
                        type="text",
                        text=f"Опубликовано (получателей: {n})",
                    )]
            finally:
                await client.aclose()

        # ═══════════════════════════════════════════════════════
        # MongoDB
        # ═══════════════════════════════════════════════════════
        if name.startswith("mongo_"):
            mongo_url = os.getenv("MONGO_URL", "").strip()
            if not mongo_url:
                return [TextContent(type="text", text="MONGO_URL не задан")]
            try:
                from pymongo import AsyncMongoClient
            except ImportError:
                try:
                    from motor.motor_asyncio import AsyncIOMotorClient as AsyncMongoClient  # type: ignore
                except ImportError:
                    return [TextContent(
                        type="text", text="pymongo/motor не установлен",
                    )]

            client = AsyncMongoClient(mongo_url)
            db = client[os.getenv("MONGO_DB", "test")]
            try:
                if name == "mongo_collections":
                    names = await db.list_collection_names()
                    return [TextContent(type="text", text=_json_result(names))]

                if name == "mongo_find":
                    col = db[arguments["collection"]]
                    cursor = col.find(arguments.get("filter") or {}).limit(
                        arguments.get("limit", 20),
                    )
                    docs = []
                    async for d in cursor:
                        d["_id"] = str(d.get("_id"))
                        docs.append(d)
                    return [TextContent(type="text", text=_json_result(docs))]

                if name == "mongo_insert":
                    col = db[arguments["collection"]]
                    result = await col.insert_one(arguments["document"])
                    return [TextContent(
                        type="text",
                        text=f"Inserted: {result.inserted_id}",
                    )]

                if name == "mongo_update":
                    col = db[arguments["collection"]]
                    result = await col.update_many(
                        arguments["filter"], arguments["update"],
                    ) if arguments.get("multi") else await col.update_one(
                        arguments["filter"], arguments["update"],
                    )
                    return [TextContent(
                        type="text",
                        text=f"Matched: {result.matched_count}, "
                             f"Modified: {result.modified_count}",
                    )]

                if name == "mongo_delete":
                    col = db[arguments["collection"]]
                    result = await col.delete_many(
                        arguments["filter"],
                    ) if arguments.get("multi") else await col.delete_one(
                        arguments["filter"],
                    )
                    return [TextContent(
                        type="text",
                        text=f"Deleted: {result.deleted_count}",
                    )]

                if name == "mongo_aggregate":
                    col = db[arguments["collection"]]
                    cursor = col.aggregate(arguments["pipeline"])
                    docs = []
                    async for d in cursor:
                        d["_id"] = str(d.get("_id"))
                        docs.append(d)
                    return [TextContent(type="text", text=_json_result(docs))]
            finally:
                client.close()

        # ═══════════════════════════════════════════════════════
        # MSSQL
        # ═══════════════════════════════════════════════════════
        if name.startswith("mssql_"):
            if not os.getenv("MSSQL_HOST"):
                return [TextContent(type="text", text="MSSQL_HOST не задан")]
            try:
                import pymssql
            except ImportError:
                return [TextContent(
                    type="text",
                    text="pymssql не установлен: pip install pymssql",
                )]

            try:
                conn = pymssql.connect(
                    server=os.getenv("MSSQL_HOST", ""),
                    port=os.getenv("MSSQL_PORT", "1433"),
                    user=os.getenv("MSSQL_USER", ""),
                    password=os.getenv("MSSQL_PASSWORD", ""),
                    database=os.getenv("MSSQL_DB", ""),
                )
            except Exception as e:
                return [TextContent(type="text", text=f"Connection: {e}")]

            try:
                if name == "mssql_tables":
                    sql = ("SELECT TABLE_SCHEMA, TABLE_NAME FROM "
                           "INFORMATION_SCHEMA.TABLES WHERE TABLE_TYPE='BASE TABLE' "
                           "ORDER BY TABLE_SCHEMA, TABLE_NAME")
                elif name == "mssql_describe":
                    t = arguments["table"]
                    sql = (f"SELECT COLUMN_NAME, DATA_TYPE, IS_NULLABLE, "
                           f"CHARACTER_MAXIMUM_LENGTH FROM INFORMATION_SCHEMA.COLUMNS "
                           f"WHERE TABLE_NAME = '{t}' ORDER BY ORDINAL_POSITION")
                else:
                    sql = arguments["sql"]

                # Простая защита
                write = arguments.get("write", False)
                low = sql.strip().lower()
                if not write and not low.startswith(("select", "with", "exec sp_help")):
                    return [TextContent(
                        type="text",
                        text="Только SELECT (write=true для изменений)",
                    )]

                cursor = conn.cursor(as_dict=True)
                cursor.execute(sql)
                rows = cursor.fetchall()
                return [TextContent(
                    type="text", text=_json_result(rows[:500]),
                )]
            except Exception as e:
                return [TextContent(type="text", text=f"SQL error: {e}")]
            finally:
                conn.close()

        # ═══════════════════════════════════════════════════════
        # ElasticSearch
        # ═══════════════════════════════════════════════════════
        if name.startswith("es_"):
            es_url = os.getenv("ES_URL", "").strip()
            if not es_url:
                return [TextContent(type="text", text="ES_URL не задан")]
            try:
                import httpx
            except ImportError:
                return [TextContent(type="text", text="httpx не установлен")]

            auth = None
            if os.getenv("ES_USER"):
                auth = (os.getenv("ES_USER", ""), os.getenv("ES_PASSWORD", ""))

            async with httpx.AsyncClient(timeout=30.0, auth=auth) as client:
                try:
                    if name == "es_indices":
                        r = await client.get(f"{es_url}/_cat/indices?format=json")
                        return [TextContent(type="text", text=_json_result(r.json()))]

                    if name == "es_search":
                        idx = arguments["index"]
                        body = {
                            "query": arguments.get("query") or {"match_all": {}},
                            "size": arguments.get("size", 20),
                        }
                        r = await client.post(
                            f"{es_url}/{idx}/_search", json=body,
                        )
                        return [TextContent(type="text", text=_json_result(r.json()))]

                    if name == "es_mapping":
                        r = await client.get(f"{es_url}/{arguments['index']}/_mapping")
                        return [TextContent(type="text", text=_json_result(r.json()))]

                    if name == "es_count":
                        r = await client.get(f"{es_url}/{arguments['index']}/_count")
                        return [TextContent(type="text", text=_json_result(r.json()))]
                except Exception as e:
                    return [TextContent(type="text", text=f"ES error: {e}")]

        return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]
    except Exception as e:
        logger.exception("db_extended tool %s failed", name)
        return [TextContent(type="text", text=f"Ошибка: {e}")]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
