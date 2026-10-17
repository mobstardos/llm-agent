"""MCP-сервер памяти — инструменты для агентов."""
from __future__ import annotations

import asyncio
import json
import logging
import sys

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("memory-mcp")

_memory = None


def _get_memory():
    global _memory
    if _memory is None:
        from src.memory.facade import Memory
        _memory = Memory()
    return _memory


app = Server("memory")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="recall",
             description="Найти релевантные чанки из проекта.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "query": {"type": "string"},
                     "top_k": {"type": "integer", "default": 5},
                 },
                 "required": ["query"],
             }),
        Tool(name="remember",
             description="Сохранить заметку в память.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "text": {"type": "string"},
                     "category": {"type": "string", "default": "note"},
                 },
                 "required": ["text"],
             }),
        Tool(name="get_project_profile",
             description="Профиль проекта.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="find_procedure",
             description="Найти процедуру для проблемы.",
             inputSchema={
                 "type": "object",
                 "properties": {"problem": {"type": "string"}},
                 "required": ["problem"],
             }),
        Tool(name="save_procedure",
             description="Сохранить процедуру.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "trigger": {"type": "string"},
                     "steps": {"type": "array", "items": {"type": "string"}},
                     "tags": {"type": "array", "items": {"type": "string"}},
                 },
                 "required": ["trigger", "steps"],
             }),
        Tool(name="who_uses",
             description="Кто использует сущность.",
             inputSchema={
                 "type": "object",
                 "properties": {"name": {"type": "string"}},
                 "required": ["name"],
             }),
        Tool(name="depends_on",
             description="От чего зависит файл.",
             inputSchema={
                 "type": "object",
                 "properties": {"file": {"type": "string"}},
                 "required": ["file"],
             }),
        Tool(name="recent_events",
             description="Последние события.",
             inputSchema={
                 "type": "object",
                 "properties": {"limit": {"type": "integer", "default": 20}},
             }),
        Tool(name="memory_stats",
             description="Статистика памяти.",
             inputSchema={"type": "object", "properties": {}}),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        mem = _get_memory()

        if name == "recall":
            if not mem.retriever:
                return [TextContent(type="text", text="Vector недоступна")]
            chunks = mem.retriever.retrieve(
                arguments["query"], top_k=arguments.get("top_k", 5),
            )
            if not chunks:
                return [TextContent(type="text", text="Ничего не найдено")]
            return [TextContent(
                type="text",
                text=mem.retriever.render_context(chunks, max_chars=10000),
            )]

        if name == "remember":
            mem.user.add_note(arguments["text"])
            return [TextContent(type="text", text="Сохранено")]

        if name == "get_project_profile":
            prof = mem.profile.render_for_prompt()
            return [TextContent(
                type="text", text=prof or "(профиль не сформирован)",
            )]

        if name == "find_procedure":
            procs = mem.find_procedures(arguments["problem"])
            if not procs:
                return [TextContent(type="text", text="Не найдено")]
            out = []
            for p in procs:
                out.append(
                    f"## {p.trigger} (success: {p.success_count}, "
                    f"rate: {p.success_rate:.0%})"
                )
                for i, s in enumerate(p.steps, 1):
                    out.append(f"  {i}. {s}")
            return [TextContent(type="text", text="\n".join(out))]

        if name == "save_procedure":
            p = mem.procedural.add(
                arguments["trigger"], arguments["steps"],
                arguments.get("tags"),
            )
            return [TextContent(
                type="text", text=f"Процедура сохранена: {p.id}",
            )]

        if name == "who_uses":
            if not mem.graph:
                return [TextContent(type="text", text="Graph недоступен")]
            rows = mem.graph.who_uses(arguments["name"])
            return [TextContent(
                type="text",
                text=json.dumps(rows, ensure_ascii=False, indent=2),
            )]

        if name == "depends_on":
            if not mem.graph:
                return [TextContent(type="text", text="Graph недоступен")]
            rows = mem.graph.depends_on(arguments["file"])
            return [TextContent(
                type="text",
                text=json.dumps(rows, ensure_ascii=False, indent=2),
            )]

        if name == "recent_events":
            events = mem.episodic.recent_events(
                limit=arguments.get("limit", 20),
            )
            lines = [
                f"[{e.type}] {e.summary} (agent={e.agent}, "
                f"success={e.success})"
                for e in events
            ]
            return [TextContent(type="text", text="\n".join(lines) or "(пусто)")]

        if name == "memory_stats":
            stats = mem.stats()
            return [TextContent(
                type="text",
                text=json.dumps(stats, ensure_ascii=False, indent=2),
            )]

        return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]
    except Exception as e:
        logger.exception("Memory tool failed: %s", name)
        return [TextContent(type="text", text=f"Ошибка: {e}")]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
