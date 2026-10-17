# -*- coding: utf-8 -*-
"""Exa Search MCP — native Python implementation.

Использует httpx (>=0.27.2, уже в requirements.txt).

Tools:
  - exa_search: AI-tuned web search
  - exa_find_similar: найти похожие страницы
  - exa_get_contents: извлечь контент страницы

Env:
  - EXA_API_KEY: ключ от https://exa.ai
"""
from __future__ import annotations

import logging
import os
from typing import Any

from mcp import Server
from mcp.server import NotificationOptions
from mcp.server.models import InitializationOptions

logger = logging.getLogger(__name__)


async def exa_search(query: str, num_results: int = 5, type: str = "auto", **kwargs) -> dict:
    """AI-tuned web search через Exa API."""
    import httpx
    api_key = os.getenv("EXA_API_KEY")
    if not api_key:
        return {"error": "EXA_API_KEY не установлен"}
    url = "https://api.exa.ai/search"
    headers = {"x-api-key": api_key, "Content-Type": "application/json"}
    payload = {
        "query": query,
        "num_results": num_results,
        "type": type,
        "contents": {"text": True},
        **kwargs,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()
    return {
        "results": [
            {
                "title": r.get("title"),
                "url": r.get("url"),
                "text": r.get("text", "")[:500],
                "score": r.get("score"),
            }
            for r in data.get("results", [])
        ]
    }


async def exa_find_similar(url_to_search: str, num_results: int = 5) -> dict:
    """Найти похожие страницы на заданный URL."""
    import httpx
    api_key = os.getenv("EXA_API_KEY")
    if not api_key:
        return {"error": "EXA_API_KEY не установлен"}
    url = "https://api.exa.ai/search"
    headers = {"x-api-key": api_key, "Content-Type": "application/json"}
    payload = {
        "url": url_to_search,
        "num_results": num_results,
        "type": "neural",
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()
    return {
        "results": [
            {"title": r.get("title"), "url": r.get("url"), "score": r.get("score")}
            for r in data.get("results", [])
        ]
    }


async def exa_get_contents(urls: list, text: bool = True) -> dict:
    """Извлечь контент страниц по списку URL."""
    import httpx
    api_key = os.getenv("EXA_API_KEY")
    if not api_key:
        return {"error": "EXA_API_KEY не установлен"}
    url = "https://api.exa.ai/contents"
    headers = {"x-api-key": api_key, "Content-Type": "application/json"}
    payload = {"ids": urls, "text": text}
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()
    return {
        "results": [
            {"url": r.get("url"), "title": r.get("title"), "text": r.get("text", "")[:500]}
            for r in data.get("results", [])
        ]
    }


app = Server("exa-search-mcp")


@app.list_tools()
async def list_tools() -> list:
    from mcp.types import Tool
    return [
        Tool(name="exa_search", description="AI-tuned web search через Exa",
             inputSchema={"type": "object", "properties": {"query": {"type": "string"}, "num_results": {"type": "integer", "default": 5}, "type": {"type": "string", "default": "auto"}}, "required": ["query"]}),
        Tool(name="exa_find_similar", description="Найти похожие страницы по URL",
             inputSchema={"type": "object", "properties": {"url_to_search": {"type": "string"}, "num_results": {"type": "integer", "default": 5}}, "required": ["url_to_search"]}),
        Tool(name="exa_get_contents", description="Извлечь контент страниц по списку URL",
             inputSchema={"type": "object", "properties": {"urls": {"type": "array"}, "text": {"type": "boolean", "default": True}}, "required": ["urls"]}),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list:
    from mcp.types import TextContent
    import json
    try:
        if name == "exa_search":
            result = await exa_search(**arguments)
        elif name == "exa_find_similar":
            result = await exa_find_similar(**arguments)
        elif name == "exa_get_contents":
            result = await exa_get_contents(**arguments)
        else:
            result = {"error": f"Unknown tool: {name}"}
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, default=str))]
    except Exception as e:
        logger.exception("exa tool %s failed", name)
        return [TextContent(type="text", text=json.dumps({"error": str(e)}, ensure_ascii=False))]


async def main():
    from mcp.server.stdio import stdio_server
    init_options = InitializationOptions(
        server_name="exa-search-mcp", server_version="1.0.0",
        capabilities=NotificationOptions(),
    )
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, init_options)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
