# -*- coding: utf-8 -*-
"""Tavily Search MCP — native Python implementation.

Использует httpx (>=0.27.2, уже в requirements.txt).

Tools:
  - tavily_search: AI-tuned search для RAG
  - tavily_extract: извлечь контент URL
  - tavily_crawl: рекурсивный crawl сайта

Env:
  - TAVILY_API_KEY: ключ от https://tavily.com
"""
from __future__ import annotations

import logging
import os
from typing import Any

from mcp import Server
from mcp.server import NotificationOptions
from mcp.server.models import InitializationOptions

logger = logging.getLogger(__name__)


async def tavily_search(query: str, max_results: int = 5, search_depth: str = "basic",
                        include_answer: bool = True, **kwargs) -> dict:
    """AI-tuned search для RAG через Tavily API."""
    import httpx
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        return {"error": "TAVILY_API_KEY не установлен"}
    url = "https://api.tavily.com/search"
    headers = {"Content-Type": "application/json"}
    payload = {
        "api_key": api_key,
        "query": query,
        "max_results": max_results,
        "search_depth": search_depth,
        "include_answer": include_answer,
        **kwargs,
    }
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(url, headers=headers, json=payload)
        r.raise_for_status()
        data = r.json()
    return {
        "answer": data.get("answer"),
        "results": [
            {
                "title": res.get("title"),
                "url": res.get("url"),
                "content": res.get("content", "")[:500],
                "score": res.get("score"),
            }
            for res in data.get("results", [])
        ]
    }


async def tavily_extract(urls: list) -> dict:
    """Извлечь контент страниц по списку URL."""
    import httpx
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        return {"error": "TAVILY_API_KEY не установлен"}
    url = "https://api.tavily.com/extract"
    payload = {"api_key": api_key, "urls": urls}
    async with httpx.AsyncClient(timeout=20) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()
        data = r.json()
    return {
        "results": [
            {"url": r.get("url"), "raw_content": r.get("raw_content", "")[:1000],
             "images": r.get("images", [])}
            for r in data.get("results", [])
        ]
    }


async def tavily_crawl(url: str, max_depth: int = 2, max_results: int = 20, **kwargs) -> dict:
    """Рекурсивный crawl сайта."""
    import httpx
    api_key = os.getenv("TAVILY_API_KEY")
    if not api_key:
        return {"error": "TAVILY_API_KEY не установлен"}
    api_url = "https://api.tavily.com/crawl"
    payload = {
        "api_key": api_key,
        "url": url,
        "max_depth": max_depth,
        "max_results": max_results,
        **kwargs,
    }
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.post(api_url, json=payload)
        r.raise_for_status()
        data = r.json()
    return {
        "results": [
            {"url": r.get("url"), "raw_content": r.get("raw_content", "")[:500]}
            for r in data.get("results", [])
        ]
    }


app = Server("tavily-search-mcp")


@app.list_tools()
async def list_tools() -> list:
    from mcp.types import Tool
    return [
        Tool(name="tavily_search", description="AI-tuned web search для RAG",
             inputSchema={"type": "object", "properties": {"query": {"type": "string"}, "max_results": {"type": "integer", "default": 5}, "search_depth": {"type": "string", "default": "basic"}, "include_answer": {"type": "boolean", "default": True}}, "required": ["query"]}),
        Tool(name="tavily_extract", description="Извлечь контент страниц по списку URL",
             inputSchema={"type": "object", "properties": {"urls": {"type": "array"}}, "required": ["urls"]}),
        Tool(name="tavily_crawl", description="Рекурсивный crawl сайта",
             inputSchema={"type": "object", "properties": {"url": {"type": "string"}, "max_depth": {"type": "integer", "default": 2}, "max_results": {"type": "integer", "default": 20}}, "required": ["url"]}),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list:
    from mcp.types import TextContent
    import json
    try:
        if name == "tavily_search":
            result = await tavily_search(**arguments)
        elif name == "tavily_extract":
            result = await tavily_extract(**arguments)
        elif name == "tavily_crawl":
            result = await tavily_crawl(**arguments)
        else:
            result = {"error": f"Unknown tool: {name}"}
        return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, default=str))]
    except Exception as e:
        logger.exception("tavily tool %s failed", name)
        return [TextContent(type="text", text=json.dumps({"error": str(e)}, ensure_ascii=False))]


async def main():
    from mcp.server.stdio import stdio_server
    init_options = InitializationOptions(
        server_name="tavily-search-mcp", server_version="1.0.0",
        capabilities=NotificationOptions(),
    )
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, init_options)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
