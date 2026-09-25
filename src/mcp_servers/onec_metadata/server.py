"""MCP-сервер: работа с XML-выгрузкой 1С."""
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
logger = logging.getLogger("onec-metadata-mcp")


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


app = Server("onec_metadata")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="list_objects",
             description="Список объектов метаданных.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "config_dir": {"type": "string"},
                     "kind": {"type": "string"},
                 },
             }),
        Tool(name="count_objects",
             description="Сколько объектов каждого вида.",
             inputSchema={
                 "type": "object",
                 "properties": {"config_dir": {"type": "string"}},
             }),
        Tool(name="read_object",
             description="Свойства объекта метаданных.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "config_dir": {"type": "string"},
                     "kind": {"type": "string"},
                     "name": {"type": "string"},
                 },
                 "required": ["kind", "name"],
             }),
        Tool(name="read_module",
             description="Прочитать модуль (.bsl).",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "config_dir": {"type": "string"},
                     "kind": {"type": "string"},
                     "name": {"type": "string"},
                     "module": {"type": "string",
                                "default": "ObjectModule"},
                 },
                 "required": ["kind", "name"],
             }),
        Tool(name="write_module",
             description="Записать модуль (.bsl).",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "config_dir": {"type": "string"},
                     "kind": {"type": "string"},
                     "name": {"type": "string"},
                     "module": {"type": "string",
                                "default": "ObjectModule"},
                     "content": {"type": "string"},
                 },
                 "required": ["kind", "name", "content"],
             }),
        Tool(name="create_object",
             description="Создать объект метаданных.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "config_dir": {"type": "string"},
                     "kind": {"type": "string"},
                     "name": {"type": "string"},
                     "synonym_ru": {"type": "string"},
                     "comment": {"type": "string"},
                 },
                 "required": ["kind", "name"],
             }),
        Tool(name="add_attribute",
             description="Добавить реквизит.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "config_dir": {"type": "string"},
                     "kind": {"type": "string"},
                     "name": {"type": "string"},
                     "attr_name": {"type": "string"},
                     "attr_type": {"type": "string", "default": "String"},
                     "length": {"type": "integer", "default": 100},
                     "required": {"type": "boolean", "default": False},
                     "indexed": {"type": "boolean", "default": False},
                 },
                 "required": ["kind", "name", "attr_name"],
             }),
        Tool(name="add_tabular_section",
             description="Добавить табличную часть.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "config_dir": {"type": "string"},
                     "kind": {"type": "string"},
                     "name": {"type": "string"},
                     "ts_name": {"type": "string"},
                 },
                 "required": ["kind", "name", "ts_name"],
             }),
        Tool(name="create_extension",
             description="Создать расширение конфигурации.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "config_dir": {"type": "string"},
                     "extension_name": {"type": "string"},
                     "purpose": {"type": "string", "default": "Customization"},
                 },
                 "required": ["extension_name"],
             }),
        Tool(name="adopt_object",
             description="Заимствовать объект в расширение.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "extension_dir": {"type": "string"},
                     "kind": {"type": "string"},
                     "object_name": {"type": "string"},
                 },
                 "required": ["extension_dir", "kind", "object_name"],
             }),
        Tool(name="diff_config",
             description="Снимок XML-конфигурации (для сравнения).",
             inputSchema={
                 "type": "object",
                 "properties": {"config_dir": {"type": "string"}},
             }),
        Tool(name="find_module",
             description="Найти модуль .bsl по имени.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "config_dir": {"type": "string"},
                     "pattern": {"type": "string"},
                 },
                 "required": ["pattern"],
             }),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        from src.onec_metadata.schema import ObjectKind
        from src.onec_metadata.xml_reader import MetadataReader
        from src.onec_metadata.xml_writer import MetadataWriter
        from src.onec_metadata.differ import snapshot_dir

        cfg_dir = _config_dir(arguments)
        reader = MetadataReader(cfg_dir)

        # ─── Чтение ───────────────────────────────
        if name == "list_objects":
            kind_str = arguments.get("kind")
            kind = None
            if kind_str:
                try:
                    kind = ObjectKind(kind_str)
                except ValueError:
                    return [TextContent(
                        type="text",
                        text=f"Неизвестный kind: {kind_str}",
                    )]
            objs = reader.list_objects(kind)
            return [TextContent(
                type="text",
                text=json.dumps(objs[:500], ensure_ascii=False, indent=2),
            )]

        if name == "count_objects":
            counts = reader.count_objects()
            return [TextContent(
                type="text",
                text=json.dumps(counts, ensure_ascii=False, indent=2),
            )]

        if name == "read_object":
            try:
                kind = ObjectKind(arguments["kind"])
            except ValueError:
                return [TextContent(
                    type="text", text=f"Неизвестный kind",
                )]
            obj = reader.read_object(kind, arguments["name"])
            if not obj:
                return [TextContent(type="text", text="Объект не найден")]
            return [TextContent(
                type="text",
                text=json.dumps({
                    "kind": obj.kind.value,
                    "name": obj.name,
                    "synonym_ru": obj.synonym_ru,
                    "comment": obj.comment,
                    "uuid": obj.uuid,
                    "attributes": [
                        {"name": a.name, "type": a.type_,
                         "length": a.length, "required": a.required,
                         "indexed": a.indexed}
                        for a in obj.attributes
                    ],
                    "tabular_sections": [
                        {"name": ts.name, "attributes_count": len(ts.attributes)}
                        for ts in obj.tabular_sections
                    ],
                    "forms": [f.name for f in obj.forms],
                    "has_object_module": obj.has_object_module,
                    "has_manager_module": obj.has_manager_module,
                }, ensure_ascii=False, indent=2),
            )]

        if name == "read_module":
            kind = ObjectKind(arguments["kind"])
            module_name = arguments.get("module", "ObjectModule")
            dir_name = kind.value + "s" if not kind.value.endswith("s") else kind.value
            # Проще через reader
            obj = reader.read_object(kind, arguments["name"])
            if not obj or not obj.xml_path:
                return [TextContent(type="text", text="Объект не найден")]

            obj_dir = Path(obj.xml_path).parent
            module_path = obj_dir / "Ext" / f"{module_name}.bsl"
            if not module_path.exists():
                return [TextContent(
                    type="text",
                    text=f"Модуль не найден: {module_path}",
                )]
            content = module_path.read_text(encoding="utf-8", errors="replace")
            return [TextContent(type="text", text=content[:50000])]

        if name == "write_module":
            kind = ObjectKind(arguments["kind"])
            module_name = arguments.get("module", "ObjectModule")
            obj = reader.read_object(kind, arguments["name"])
            if not obj or not obj.xml_path:
                return [TextContent(type="text", text="Объект не найден")]

            obj_dir = Path(obj.xml_path).parent
            module_path = obj_dir / "Ext" / f"{module_name}.bsl"
            module_path.parent.mkdir(parents=True, exist_ok=True)
            module_path.write_text(
                arguments["content"], encoding="utf-8",
            )
            return [TextContent(
                type="text", text=f"Записан: {module_path}",
            )]

        # ─── Создание ─────────────────────────────
        writer = MetadataWriter(cfg_dir)

        if name == "create_object":
            kind = ObjectKind(arguments["kind"])
            obj = writer.create_object(
                kind, arguments["name"],
                synonym_ru=arguments.get("synonym_ru", ""),
                comment=arguments.get("comment", ""),
            )
            return [TextContent(
                type="text",
                text=json.dumps({
                    "created": obj.name,
                    "kind": obj.kind.value,
                    "xml": obj.xml_path,
                }, ensure_ascii=False, indent=2),
            )]

        if name == "add_attribute":
            from src.onec_metadata.schema import Attribute
            kind = ObjectKind(arguments["kind"])
            attr = Attribute(
                name=arguments["attr_name"],
                type_=arguments.get("attr_type", "String"),
                length=arguments.get("length", 100),
                required=arguments.get("required", False),
                indexed=arguments.get("indexed", False),
            )
            ok = writer.add_attribute(kind, arguments["name"], attr)
            return [TextContent(
                type="text",
                text=f"{'Добавлен' if ok else 'Не добавлен'}: "
                     f"{arguments['attr_name']}",
            )]

        if name == "add_tabular_section":
            from src.onec_metadata.schema import TabularSection
            kind = ObjectKind(arguments["kind"])
            ts = TabularSection(name=arguments["ts_name"])
            ok = writer.add_tabular_section(kind, arguments["name"], ts)
            return [TextContent(
                type="text",
                text=f"{'Добавлена' if ok else 'Не добавлена'}: "
                     f"{arguments['ts_name']}",
            )]

        if name == "create_extension":
            ext_dir = writer.create_extension(
                arguments["extension_name"],
                purpose=arguments.get("purpose", "Customization"),
            )
            return [TextContent(
                type="text",
                text=f"Расширение: {ext_dir}",
            )]

        if name == "adopt_object":
            kind = ObjectKind(arguments["kind"])
            ext_dir = _safe(arguments["extension_dir"])
            target = writer.adopt_object(
                ext_dir, kind, arguments["object_name"],
            )
            return [TextContent(
                type="text",
                text=f"Заимствован: {target}",
            )]

        # ─── Утилиты ──────────────────────────────
        if name == "diff_config":
            snap = snapshot_dir(cfg_dir)
            return [TextContent(
                type="text",
                text=json.dumps({
                    "total_files": len(snap),
                    "sample": dict(list(snap.items())[:20]),
                }, ensure_ascii=False, indent=2),
            )]

        if name == "find_module":
            pattern = arguments["pattern"].lower()
            found: list[str] = []
            for bsl in cfg_dir.rglob("*.bsl"):
                if pattern in bsl.name.lower():
                    found.append(str(bsl.relative_to(cfg_dir)))
                    if len(found) >= 100:
                        break
            return [TextContent(
                type="text",
                text="\n".join(found) or "Не найдено",
            )]

        return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]
    except Exception as e:
        logger.exception("1C Metadata tool %s failed", name)
        return [TextContent(type="text", text=f"Ошибка: {e}")]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
