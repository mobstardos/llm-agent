"""MCP-сервер: управляемые формы 1С."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("onec-forms-mcp")


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


app = Server("onec_forms")

HANDLER_RE = re.compile(
    r"^\s*(?:&\w+\s+)?Процедура\s+([A-Za-zА-Яа-я_][A-Za-zА-Яа-я0-9_]*)"
    r"\s*\(([^)]*)\)",
    re.MULTILINE | re.IGNORECASE,
)


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="read_form",
             description="Прочитать управляемую форму.",
             inputSchema={
                 "type": "object",
                 "properties": {"xml_path": {"type": "string"}},
                 "required": ["xml_path"],
             }),
        Tool(name="list_forms",
             description="Формы объекта метаданных.",
             inputSchema={
                 "type": "object",
                 "properties": {"obj_dir": {"type": "string"}},
                 "required": ["obj_dir"],
             }),
        Tool(name="get_form_bsl",
             description="Модуль формы.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "xml_path": {"type": "string"},
                     "bsl_path": {"type": "string"},
                 },
                 "required": ["xml_path"],
             }),
        Tool(name="write_form_bsl",
             description="Записать модуль формы.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "bsl_path": {"type": "string"},
                     "content": {"type": "string"},
                 },
                 "required": ["bsl_path", "content"],
             }),
        Tool(name="create_form",
             description="Создать форму с элементами и командами.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "obj_dir": {"type": "string"},
                     "name": {"type": "string"},
                     "title": {"type": "string"},
                     "elements": {
                         "type": "array",
                         "items": {
                             "type": "object",
                             "properties": {
                                 "name": {"type": "string"},
                                 "kind": {"type": "string"},
                                 "title": {"type": "string"},
                                 "data_path": {"type": "string"},
                             },
                         },
                     },
                     "commands": {
                         "type": "array",
                         "items": {
                             "type": "object",
                             "properties": {
                                 "name": {"type": "string"},
                                 "title": {"type": "string"},
                                 "action": {"type": "string"},
                             },
                         },
                     },
                 },
                 "required": ["obj_dir", "name"],
             }),
        Tool(name="analyze_form",
             description="Анализ формы — элементы, команды, реквизиты.",
             inputSchema={
                 "type": "object",
                 "properties": {"xml_path": {"type": "string"}},
                 "required": ["xml_path"],
             }),
        Tool(name="list_handlers",
             description="Список обработчиков в модуле формы.",
             inputSchema={
                 "type": "object",
                 "properties": {"bsl_path": {"type": "string"}},
                 "required": ["bsl_path"],
             }),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        from src.onec_forms.reader import FormReader, find_forms_for_object
        from src.onec_forms.schema import (
            FormAttribute, FormCommand, FormElement, ManagedForm,
        )
        from src.onec_forms.writer import add_form_to_object

        if name == "read_form":
            p = _safe(arguments["xml_path"])
            form = FormReader(p).read()
            return [TextContent(
                type="text",
                text=json.dumps({
                    "uuid": form.uuid,
                    "name": form.name,
                    "title": form.title,
                    "kind": form.kind,
                    "attributes": [
                        {"name": a.name, "type": a.type_,
                         "main": a.main_attribute}
                        for a in form.attributes
                    ],
                    "elements": [
                        {"name": e.name, "kind": e.kind,
                         "title": e.title, "data_path": e.data_path,
                         "parent": e.parent}
                        for e in form.elements
                    ],
                    "commands": [
                        {"name": c.name, "title": c.title,
                         "action": c.action}
                        for c in form.commands
                    ],
                    "errors": form.errors,
                }, ensure_ascii=False, indent=2),
            )]

        if name == "list_forms":
            obj_dir = _safe(arguments["obj_dir"])
            forms = find_forms_for_object(obj_dir)
            return [TextContent(
                type="text",
                text=json.dumps(forms, ensure_ascii=False, indent=2),
            )]

        if name == "get_form_bsl":
            xml_path = _safe(arguments["xml_path"])
            bsl_path = arguments.get("bsl_path")
            if not bsl_path:
                # Ищем модуль рядом
                bsl_path = xml_path.parent / "Ext" / "Form" / "Module.bsl"
            else:
                bsl_path = _safe(bsl_path)

            if not Path(bsl_path).exists():
                return [TextContent(
                    type="text", text=f"Модуль не найден: {bsl_path}",
                )]
            content = Path(bsl_path).read_text(
                encoding="utf-8", errors="replace",
            )
            return [TextContent(type="text", text=content[:50000])]

        if name == "write_form_bsl":
            bsl_path = _safe(arguments["bsl_path"])
            bsl_path.parent.mkdir(parents=True, exist_ok=True)
            bsl_path.write_text(arguments["content"], encoding="utf-8")
            return [TextContent(
                type="text", text=f"Записан: {bsl_path}",
            )]

        if name == "create_form":
            obj_dir = _safe(arguments["obj_dir"])
            form = ManagedForm(
                name=arguments["name"],
                title=arguments.get("title", ""),
            )
            for e in arguments.get("elements", []):
                form.elements.append(FormElement(
                    name=e["name"],
                    kind=e.get("kind", "Field"),
                    title=e.get("title", ""),
                    data_path=e.get("data_path", ""),
                ))
            for c in arguments.get("commands", []):
                form.commands.append(FormCommand(
                    name=c["name"],
                    title=c.get("title", ""),
                    action=c.get("action", ""),
                ))

            target_dir = add_form_to_object(obj_dir, form)
            return [TextContent(
                type="text", text=f"Форма создана: {target_dir}",
            )]

        if name == "analyze_form":
            p = _safe(arguments["xml_path"])
            form = FormReader(p).read()
            summary = {
                "attributes_count": len(form.attributes),
                "elements_count": len(form.elements),
                "commands_count": len(form.commands),
                "elements_by_kind": {},
            }
            for e in form.elements:
                summary["elements_by_kind"][e.kind] = \
                    summary["elements_by_kind"].get(e.kind, 0) + 1
            return [TextContent(
                type="text",
                text=json.dumps(summary, ensure_ascii=False, indent=2),
            )]

        if name == "list_handlers":
            bsl_path = _safe(arguments["bsl_path"])
            if not bsl_path.exists():
                return [TextContent(type="text", text="Файл не найден")]
            content = bsl_path.read_text(
                encoding="utf-8", errors="replace",
            )
            handlers: list[dict] = []
            for m in HANDLER_RE.finditer(content):
                handlers.append({
                    "name": m.group(1),
                    "params": m.group(2).strip(),
                })
            return [TextContent(
                type="text",
                text=json.dumps(handlers, ensure_ascii=False, indent=2),
            )]

        return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]
    except Exception as e:
        logger.exception("1C Forms tool %s failed", name)
        return [TextContent(type="text", text=f"Ошибка: {e}")]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
