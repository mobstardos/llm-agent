# -*- coding: utf-8 -*-
"""Brave Search MCP — native Python implementation.

Использует httpx (>=0.27.2, уже в requirements.txt).

Tools:
  - brave_search: web search
  - brave_search_news: news search
  - brave_search_images: image search

Env:
  - BRAVE_API_KEY: API key от https://brave.com/search/api/
"""
from __future__ import annotations

import logging
import os
from typing import Any

from mcp import Server
from mcp.server import NotificationOptions
from mcp.server.models import InitializationOptions

logger = logging.getLogger(__name__)


async def brave_search(query: str, count: int = 10, country: str = "US", **kwargs) -> dict:
    """Web search через Brave Search API."""
    import httpx
    api_key = os.getenv("BRAVE_API_KEY")
    if not api_key:
        return {"error": "BRAVE_API_KEY не установлен"}
    url = "https://api.search.brave.com/res/v1/web/search"
    headers = {"X-Subscription-Token": api_key, "Accept": "application/json"}
    params = {"q": query, "count": count, "country": country, **kwargs}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, headers=headers, params=params)
        r.raise_for_status()
        data = r.json()
    return {
        "results": [
            {
                "title": r.get("title"),
                "url": r.get("url"),
                "description": r.get("description"),
            }
            for r in data.get("web", {}).get("results", [])
        ]
    }


async def brave_search_news(query: str, count: int = 10) -> dict:
    """News search через Brave News API."""
    import httpx
    api_key = os.getenv("BRAVE_API_KEY")
    if not api_key:
        return {"error": "BRAVE_API_KEY не установлен"}
    url = "https://api.search.brave.com/res/v1/news/search"
    headers = {"X-Subscription-Token": api_key, "Accept": "application/json"}
    params = {"q": query, "count": count}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, headers=headers, params=params)
        r.raise_for_status()
        data = r.json()
    return {
        "results": [
            {
                "title": r.get("name"),
                "url": r.get("url"),
                "description": r.get("description"),
                "date": r.get("date"),
            }
            for r in data.get("results", [])
        ]
    }


async def brave_search_images(query: str, count: int = 10) -> dict:
    """Image search через Brave Images API."""
    import httpx
    api_key = os.getenv("BRAVE_API_KEY")
    if not api_key:
        return {"error": "BRAVE_API_KEY не установлен"}
    url = "https://api.search.brave.com/res/v1/images/search"
    headers = {"X-Subscription-Token": api_key, "Accept": "application/json"}
    params = {"q": query, "count": count}
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.get(url, headers=headers, params=params)
        r.raise_for_status()
        data = r.json()
    return {
        "results": [
            {
                "title": r.get("title"),
                "url": r.get("url"),
                "thumbnail": r.get("thumbnail", {}).get("src"),
            }
            for r in data.get("results", [])
        ]
    }


app = Server("brave-search-mcp")


@app.list_tools()
async def list_tools() -> list:
    from mcp.types import Tool
    return [
        Tool(name="brave_search", description="Web search через Brave Search API",
             inputSchema={"type": "object", "properties": {"query": {"type": "string"}, "count": {"type": "integer", "default": 10}, "country": {"type": "string", "default": "US"}}, "required": ["query"]}),
        Tool(name="brave_search_news", description="News search через Brave",
             inputSchema={"type": "object", "properties": {"query": {"type": "string"}, "count": {"type": "integer", "default": 10}}, "required": ["query"]}),
        Tool(name="brave_search_images", description="Image search через Brave",
             inputSchema={"type": "object", "properties": {"query": {"type": "string"}, "count": {"type": "integer", "default": 10}}, "required": ["query"]}),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list:
    from mcp.types import TextContent
    import json
    try:
        if name == "brave_search":
            result = await brave_search(**arguments)
        elif name == "brave_search_news":
            result = await brave_search_news(**arguments)
        elif name == "brave_search_images":
            result = await brave_search_images(**arguments)
        else:
            result = {"error": f"Unknown tool: {name}"}
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, default=str))]
    except Exception as e:
        logger.exception("brave tool %s failed", name)
        return [TextContent(type="text", text=json.dumps({"error": str(e)}, ensure_ascii=False))]


async def main():
    from mcp.server.stdio import stdio_server
    init_options = InitializationOptions(
        server_name="brave-search-mcp", server_version="1.0.0",
        capabilities=NotificationOptions(),
    )
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, init_options)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
