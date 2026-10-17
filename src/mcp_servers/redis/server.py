# -*- coding: utf-8 -*-
"""Redis MCP — native Python implementation.

Использует `redis` пакет (>=5.0.0, уже в requirements.txt).

Tools:
  - redis_get / redis_set / redis_delete: базовые операции с ключами
  - redis_hset: hash-операции
  - redis_publish: pub/sub
  - redis_search: RedisSearch (RediSearch module)
  - redis_keys: список ключей по паттерну

Env:
  - REDIS_URL: redis://[user:password@]host:port/db
"""
from __future__ import annotations

import logging
import os
from typing import Any

from mcp import Server
from mcp.server import NotificationOptions
from mcp.server.models import InitializationOptions

logger = logging.getLogger(__name__)

_redis_client = None


def get_client():
    """Ленивое создание Redis-клиента."""
    global _redis_client
    if _redis_client is not None:
        return _redis_client
    from redis import Redis
    url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    _redis_client = Redis.from_url(url, decode_responses=True)
    return _redis_client


async def redis_get(key: str) -> dict:
    """Получить значение по ключу."""
    r = get_client()
    value = r.get(key)
    return {"key": key, "value": value, "type": type(value).__name__}


async def redis_set(key: str, value: str, ex: int = None) -> dict:
    """Установить ключ (с опциональным TTL в секундах)."""
    r = get_client()
    r.set(key, value, ex=ex)
    return {"ok": True, "key": key, "ttl": ex}


async def redis_delete(key: str) -> dict:
    """Удалить ключ. DESTRUCTIVE!"""
    r = get_client()
    deleted = r.delete(key)
    return {"deleted": deleted, "key": key}


async def redis_hset(name: str, key: str, value: str) -> dict:
    """Установить поле в hash."""
    r = get_client()
    r.hset(name, key, value)
    return {"ok": True, "hash": name, "field": key}


async def redis_publish(channel: str, message: str) -> dict:
    """Опубликовать сообщение в pub/sub канал."""
    r = get_client()
    receivers = r.publish(channel, message)
    return {"published": True, "channel": channel, "receivers": receivers}


async def redis_search(index: str, query: str, limit: int = 10) -> dict:
    """FTS-поиск через RediSearch module."""
    r = get_client()
    try:
        from redis.commands.search import Search
        search = Search(r, index_name=index)
        result = search.search(query, query_params={"limit": limit})
        return {
            "total": result.total,
            "docs": [
                {"id": doc.id, "fields": doc.__dict__.get("__hash__", {})}
                for doc in result.docs
            ],
        }
    except Exception as e:
        return {"error": str(e), "hint": "RediSearch module may not be installed"}


async def redis_keys(pattern: str = "*", limit: int = 100) -> dict:
    """Список ключей по паттерну (KEYS с лимитом для safety)."""
    r = get_client()
    keys = r.scan_iter(match=pattern, count=limit)
    return {"keys": list(keys)[:limit], "pattern": pattern}


app = Server("redis-mcp")


@app.list_tools()
async def list_tools() -> list:
    from mcp.types import Tool
    return [
        Tool(name="redis_get", description="Получить значение по ключу",
             inputSchema={"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}),
        Tool(name="redis_set", description="Установить ключ",
             inputSchema={"type": "object", "properties": {"key": {"type": "string"}, "value": {"type": "string"}, "ex": {"type": "integer"}}, "required": ["key", "value"]}),
        Tool(name="redis_delete", description="Удалить ключ (destructive)",
             inputSchema={"type": "object", "properties": {"key": {"type": "string"}}, "required": ["key"]}),
        Tool(name="redis_hset", description="Установить hash field",
             inputSchema={"type": "object", "properties": {"name": {"type": "string"}, "key": {"type": "string"}, "value": {"type": "string"}}, "required": ["name", "key", "value"]}),
        Tool(name="redis_publish", description="Опубликовать в pub/sub",
             inputSchema={"type": "object", "properties": {"channel": {"type": "string"}, "message": {"type": "string"}}, "required": ["channel", "message"]}),
        Tool(name="redis_search", description="RediSearch FTS поиск",
             inputSchema={"type": "object", "properties": {"index": {"type": "string"}, "query": {"type": "string"}, "limit": {"type": "integer", "default": 10}}, "required": ["index", "query"]}),
        Tool(name="redis_keys", description="Список ключей по паттерну",
             inputSchema={"type": "object", "properties": {"pattern": {"type": "string", "default": "*"}, "limit": {"type": "integer", "default": 100}}}),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list:
    from mcp.types import TextContent
    import json
    try:
        if name == "redis_get":
            result = await redis_get(**arguments)
        elif name == "redis_set":
            result = await redis_set(**arguments)
        elif name == "redis_delete":
            result = await redis_delete(**arguments)
        elif name == "redis_hset":
            result = await redis_hset(**arguments)
        elif name == "redis_publish":
            result = await redis_publish(**arguments)
        elif name == "redis_search":
            result = await redis_search(**arguments)
        elif name == "redis_keys":
            result = await redis_keys(**arguments)
        else:
            result = {"error": f"Unknown tool: {name}"}
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, default=str))]
    except Exception as e:
        logger.exception("redis tool %s failed", name)
        return [TextContent(type="text", text=json.dumps({"error": str(e)}, ensure_ascii=False))]


async def main():
    from mcp.server.stdio import stdio_server
    init_options = InitializationOptions(
        server_name="redis-mcp", server_version="1.0.0",
        capabilities=NotificationOptions(),
    )
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, init_options)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
