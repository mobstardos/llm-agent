"""MCP-сервер: работа с документами."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("document-mcp")

_pipeline = None
_cache = None


def _get_root() -> Path:
    try:
        from src.runtime_config import get_project_root
        val = get_project_root(default="")
        if val:
            return Path(val).resolve()
    except Exception:
        pass
    val = os.getenv("PROJECT_ROOT", "").strip()
    return Path(val or os.getcwd()).resolve()


def _safe(path: str) -> Path:
    root = _get_root()
    p = (root / path).resolve()
    if root not in p.parents and p != root:
        raise ValueError(f"Путь вне корня: {path}")
    return p


def _get_pipeline():
    global _pipeline, _cache
    if _pipeline is None:
        from src.extraction.cache import ExtractionCache
        from src.extraction.pipeline import ExtractionPipeline

        cache_path = Path(__file__).resolve().parent.parent.parent.parent / "data" / "extraction.sqlite"
        _cache = ExtractionCache(cache_path)
        _pipeline = ExtractionPipeline(cache=_cache)
    return _pipeline


app = Server("document")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="read_document",
            description="Извлечь текст из документа (PDF, DOCX, XLSX, изображение).",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "max_chars": {"type": "integer", "default": 50000},
                },
                "required": ["path"],
            },
        ),
        Tool(
            name="extract_tables",
            description="Извлечь только таблицы из документа.",
            inputSchema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        ),
        Tool(
            name="get_metadata",
            description="Метаданные файла без извлечения текста.",
            inputSchema={
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        ),
        Tool(
            name="search_in_document",
            description="Поиск подстроки в тексте документа.",
            inputSchema={
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "query": {"type": "string"},
                    "context_chars": {"type": "integer", "default": 100},
                },
                "required": ["path", "query"],
            },
        ),
        Tool(
            name="list_supported",
            description="Список поддерживаемых extractors.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="extraction_cache_stats",
            description="Статистика кэша extraction.",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="extraction_cache_clear",
            description="Очистить кэш extraction.",
            inputSchema={"type": "object", "properties": {}},
        ),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "list_supported":
            pipe = _get_pipeline()
            return [TextContent(
                type="text",
                text=json.dumps(pipe.available(), ensure_ascii=False, indent=2),
            )]

        if name == "extraction_cache_stats":
            pipe = _get_pipeline()
            return [TextContent(
                type="text",
                text=json.dumps(
                    _cache.stats() if _cache else {}, ensure_ascii=False, indent=2,
                ),
            )]

        if name == "extraction_cache_clear":
            pipe = _get_pipeline()
            if _cache:
                # очищаем через новый экземпляр
                import sqlite3
                with sqlite3.connect(_cache.path) as conn:
                    conn.execute("DELETE FROM extraction_cache")
            return [TextContent(type="text", text="Кэш очищен")]

        p = _safe(arguments["path"])
        if not p.exists():
            return [TextContent(type="text", text=f"Файл не найден: {p}")]

        pipe = _get_pipeline()

        if name == "read_document":
            max_chars = arguments.get("max_chars", 50000)
            result = pipe.extract(p, max_chars=max_chars)
            text = result.text
            if result.warnings:
                text += "\n\n--- warnings ---\n" + "\n".join(result.warnings)
            return [TextContent(type="text", text=text[:max_chars])]

        if name == "extract_tables":
            result = pipe.extract(p)
            if not result.tables:
                return [TextContent(type="text", text="Таблиц не найдено")]
            return [TextContent(
                type="text",
                text=json.dumps(result.tables[:20], ensure_ascii=False, indent=2)[:30000],
            )]

        if name == "get_metadata":
            result = pipe.extract(p, max_chars=1)
            meta = {
                "source": result.source,
                "mime": result.mime,
                "extractor": result.extractor_id,
                "pages": result.pages,
                "chars": result.chars,
                "metadata": result.metadata,
                "warnings": result.warnings,
                "confidence": result.confidence,
            }
            return [TextContent(
                type="text",
                text=json.dumps(meta, ensure_ascii=False, indent=2),
            )]

        if name == "search_in_document":
            query = arguments["query"]
            context_chars = arguments.get("context_chars", 100)
            result = pipe.extract(p)
            text = result.text
            hits = []
            idx = 0
            while True:
                i = text.find(query, idx)
                if i < 0:
                    break
                start = max(0, i - context_chars)
                end = min(len(text), i + len(query) + context_chars)
                hits.append(text[start:end].replace("\n", " "))
                idx = i + len(query)
                if len(hits) >= 20:
                    break
            if not hits:
                return [TextContent(type="text", text="Не найдено")]
            return [TextContent(
                type="text",
                text=f"Найдено {len(hits)} вхождений:\n\n" + "\n---\n".join(hits),
            )]

        return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]
    except Exception as e:
        logger.exception("Document tool %s failed", name)
        return [TextContent(type="text", text=f"Ошибка: {e}")]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
