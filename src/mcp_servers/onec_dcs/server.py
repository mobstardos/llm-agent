"""MCP-сервер: СКД 1С."""
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
logger = logging.getLogger("onec-dcs-mcp")


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


def _config_dir(arguments: dict) -> Path:
    raw = arguments.get("config_dir", "")
    if raw:
        return _safe(raw)
    env = os.getenv("ONEC_CONFIG_DIR", "").strip()
    if env:
        return _safe(env)
    return _get_root()


app = Server("onec_dcs")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="read_dcs",
             description="Прочитать СКД по пути.",
             inputSchema={
                 "type": "object",
                 "properties": {"path": {"type": "string"}},
                 "required": ["path"],
             }),
        Tool(name="list_dcs",
             description="Все СКД в конфигурации.",
             inputSchema={
                 "type": "object",
                 "properties": {"config_dir": {"type": "string"}},
             }),
        Tool(name="find_dcs",
             description="Найти СКД по имени отчёта.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "config_dir": {"type": "string"},
                     "report_name": {"type": "string"},
                 },
                 "required": ["report_name"],
             }),
        Tool(name="create_dcs",
             description="Создать СКД с нуля.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "output_dir": {"type": "string"},
                     "name": {"type": "string"},
                     "query": {"type": "string"},
                     "fields": {
                         "type": "array",
                         "items": {"type": "string"},
                     },
                 },
                 "required": ["output_dir", "name"],
             }),
        Tool(name="add_calculated_field",
             description="Добавить вычисляемое поле в СКД.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "path": {"type": "string"},
                     "name": {"type": "string"},
                     "expression": {"type": "string"},
                 },
                 "required": ["path", "name", "expression"],
             }),
        Tool(name="add_parameter",
             description="Добавить параметр в СКД.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "path": {"type": "string"},
                     "name": {"type": "string"},
                     "title": {"type": "string"},
                 },
                 "required": ["path", "name"],
             }),
        Tool(name="analyze_dcs",
             description="Анализ СКД — что внутри.",
             inputSchema={
                 "type": "object",
                 "properties": {"path": {"type": "string"}},
                 "required": ["path"],
             }),
        Tool(name="validate_dcs",
             description="Проверка СКД на ошибки.",
             inputSchema={
                 "type": "object",
                 "properties": {"path": {"type": "string"}},
                 "required": ["path"],
             }),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        from src.onec_dcs.reader import DCSReader
        from src.onec_dcs.writer import DCSWriter

        if name == "read_dcs":
            p = _safe(arguments["path"])
            dcs = DCSReader(p).read()
            return [TextContent(
                type="text",
                text=json.dumps({
                    "uuid": dcs.uuid,
                    "name": dcs.name,
                    "data_sets": [
                        {"name": ds.name, "type": ds.type.value,
                         "query": ds.query[:500],
                         "fields_count": len(ds.fields)}
                        for ds in dcs.data_sets
                    ],
                    "calculated_fields": [
                        {"name": cf.name, "expression": cf.expression}
                        for cf in dcs.calculated_fields
                    ],
                    "parameters": [
                        {"name": p.name, "title": p.title, "type": p.type_}
                        for p in dcs.parameters
                    ],
                    "variants": [v.name for v in dcs.variants],
                    "errors": dcs.errors,
                }, ensure_ascii=False, indent=2),
            )]

        if name == "list_dcs":
            cfg = _config_dir(arguments)
            schemas = list(cfg.rglob("*КомпоновкиДанных.xml"))
            items = []
            for s in schemas[:200]:
                items.append(str(s.relative_to(cfg)))
            return [TextContent(
                type="text",
                text=json.dumps({
                    "total": len(schemas),
                    "items": items,
                }, ensure_ascii=False, indent=2),
            )]

        if name == "find_dcs":
            cfg = _config_dir(arguments)
            report = arguments["report_name"]
            pattern = f"*{report}*КомпоновкиДанных.xml"
            matches = list(cfg.rglob(pattern))
            if not matches:
                return [TextContent(
                    type="text", text=f"Не найдено: {report}",
                )]
            return [TextContent(
                type="text",
                text="\n".join(str(m.relative_to(cfg)) for m in matches),
            )]

        if name == "create_dcs":
            from src.onec_dcs.schema import DataSet, DataSetField
            out_dir = _safe(arguments["output_dir"])
            name_ = arguments["name"]
            query = arguments.get("query", "")

            ds = DataSet(name="НаборДанных1")
            if query:
                ds.query = query
            for f in arguments.get("fields", []):
                ds.fields.append(DataSetField(field=f))

            writer = DCSWriter(out_dir)
            path = writer.create(name_, data_sets=[ds])
            return [TextContent(
                type="text", text=f"Создана СКД: {path}",
            )]

        if name == "add_calculated_field":
            # Простая вставка в XML
            from lxml import etree
            p = _safe(arguments["path"])
            if not p.exists():
                return [TextContent(type="text", text="Файл не найден")]

            tree = etree.parse(str(p))
            root = tree.getroot()

            cf_container = None
            for elem in root:
                if etree.QName(elem).localname == "calculatedFields":
                    cf_container = elem
                    break
            if cf_container is None:
                cf_container = etree.SubElement(root, "calculatedFields")

            new_cf = etree.SubElement(cf_container, "calculatedField")
            etree.SubElement(new_cf, "dataPath").text = arguments["name"]
            etree.SubElement(new_cf, "expression").text = arguments["expression"]

            tree.write(
                str(p), encoding="UTF-8",
                xml_declaration=True, pretty_print=True,
            )
            return [TextContent(
                type="text",
                text=f"Добавлено вычисляемое поле: {arguments['name']}",
            )]

        if name == "add_parameter":
            from lxml import etree
            p = _safe(arguments["path"])
            if not p.exists():
                return [TextContent(type="text", text="Файл не найден")]

            tree = etree.parse(str(p))
            root = tree.getroot()

            p_container = None
            for elem in root:
                if etree.QName(elem).localname == "parameters":
                    p_container = elem
                    break
            if p_container is None:
                p_container = etree.SubElement(root, "parameters")

            new_p = etree.SubElement(p_container, "parameter")
            etree.SubElement(new_p, "name").text = arguments["name"]
            if arguments.get("title"):
                title = etree.SubElement(new_p, "title")
                title.set(
                    "{http://www.w3.org/2001/XMLSchema-instance}type",
                    "v8:LocalStringType",
                )
                item = etree.SubElement(title, "item")
                etree.SubElement(item, "lang").text = "ru"
                etree.SubElement(item, "content").text = arguments["title"]

            tree.write(
                str(p), encoding="UTF-8",
                xml_declaration=True, pretty_print=True,
            )
            return [TextContent(
                type="text",
                text=f"Добавлен параметр: {arguments['name']}",
            )]

        if name == "analyze_dcs":
            p = _safe(arguments["path"])
            dcs = DCSReader(p).read()
            return [TextContent(
                type="text",
                text=json.dumps({
                    "data_sets": len(dcs.data_sets),
                    "fields_total": sum(len(ds.fields) for ds in dcs.data_sets),
                    "calculated_fields": len(dcs.calculated_fields),
                    "total_fields": len(dcs.total_fields),
                    "parameters": len(dcs.parameters),
                    "variants": len(dcs.variants),
                    "templates": dcs.templates,
                    "query_preview": dcs.data_sets[0].query[:300]
                        if dcs.data_sets else "",
                }, ensure_ascii=False, indent=2),
            )]

        if name == "validate_dcs":
            p = _safe(arguments["path"])
            dcs = DCSReader(p).read()
            issues: list[str] = []
            if not dcs.data_sets:
                issues.append("Нет наборов данных")
            for ds in dcs.data_sets:
                if ds.type.value == "DataSetQuery" and not ds.query.strip():
                    issues.append(f"Набор '{ds.name}' без запроса")
            if dcs.errors:
                issues.extend(dcs.errors)
            if not issues:
                issues.append("Явных проблем не найдено")
            return [TextContent(
                type="text",
                text="\n".join(f"- {i}" for i in issues),
            )]

        return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]
    except Exception as e:
        logger.exception("1C DCS tool %s failed", name)
        return [TextContent(type="text", text=f"Ошибка: {e}")]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
