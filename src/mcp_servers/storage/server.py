"""MCP-сервер: cloud storage."""
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
logger = logging.getLogger("storage-mcp")

_backend = None


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


def _safe_local(path: str) -> Path:
    root = _get_root()
    p = (root / path).resolve()
    if root not in p.parents and p != root:
        raise ValueError(f"Путь вне корня: {path}")
    return p


def _get_backend():
    global _backend
    if _backend is None:
        from src.storage.factory import create_storage
        _backend = create_storage()
        logger.info("Storage backend: %s", _backend.id)
    return _backend


app = Server("storage")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="upload",
             description="Загрузить локальный файл в хранилище.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "local_path": {"type": "string"},
                     "key": {"type": "string"},
                     "content_type": {"type": "string"},
                 },
                 "required": ["local_path"],
             }),
        Tool(name="download",
             description="Скачать файл из хранилища.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "key": {"type": "string"},
                     "local_path": {"type": "string"},
                 },
                 "required": ["key", "local_path"],
             }),
        Tool(name="list_objects",
             description="Список объектов в хранилище.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "prefix": {"type": "string", "default": ""},
                     "limit": {"type": "integer", "default": 500},
                 },
             }),
        Tool(name="delete_object",
             description="Удалить объект (опасно!).",
             inputSchema={
                 "type": "object",
                 "properties": {"key": {"type": "string"}},
                 "required": ["key"],
             }),
        Tool(name="exists",
             description="Проверить наличие объекта.",
             inputSchema={
                 "type": "object",
                 "properties": {"key": {"type": "string"}},
                 "required": ["key"],
             }),
        Tool(name="presign",
             description="Получить временную ссылку.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "key": {"type": "string"},
                     "expires_sec": {"type": "integer", "default": 3600},
                 },
                 "required": ["key"],
             }),
        Tool(name="sync_up",
             description="Загрузить всю директорию в хранилище.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "local_dir": {"type": "string"},
                     "key_prefix": {"type": "string", "default": ""},
                     "pattern": {"type": "string", "default": "**/*"},
                 },
                 "required": ["local_dir"],
             }),
        Tool(name="sync_down",
             description="Скачать всё по префиксу в локальную директорию.",
             inputSchema={
                 "type": "object",
                 "properties": {
                     "key_prefix": {"type": "string"},
                     "local_dir": {"type": "string"},
                 },
                 "required": ["key_prefix", "local_dir"],
             }),
        Tool(name="storage_info",
             description="Информация о текущем хранилище.",
             inputSchema={"type": "object", "properties": {}}),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        backend = _get_backend()

        if name == "storage_info":
            info = {
                "id": backend.id,
                "kind": backend.kind,
                "available": backend.available(),
            }
            if backend.kind == "s3":
                info["bucket"] = backend.bucket
                info["endpoint"] = backend.endpoint_url
                try:
                    buckets = await backend.list_buckets()
                    info["buckets"] = buckets[:20]
                except Exception:
                    pass
            elif backend.kind == "local":
                info["base_dir"] = str(backend.base_dir)
            return [TextContent(
                type="text",
                text=json.dumps(info, ensure_ascii=False, indent=2),
            )]

        if name == "upload":
            local = _safe_local(arguments["local_path"])
            result = await backend.upload(
                local, arguments.get("key"),
                arguments.get("content_type"),
            )
            return [TextContent(
                type="text",
                text=json.dumps({
                    "success": result.success,
                    "key": result.key,
                    "size": result.size,
                    "url": result.url,
                    "error": result.error,
                }, ensure_ascii=False, indent=2),
            )]

        if name == "download":
            local = _safe_local(arguments["local_path"])
            ok = await backend.download(arguments["key"], local)
            return [TextContent(
                type="text",
                text=f"OK: {local}" if ok else "Ошибка загрузки",
            )]

        if name == "list_objects":
            objs = await backend.list(
                arguments.get("prefix", ""),
                arguments.get("limit", 500),
            )
            return [TextContent(
                type="text",
                text=json.dumps({
                    "count": len(objs),
                    "objects": [
                        {"key": o.key, "size": o.size,
                         "last_modified": (
                             o.last_modified.isoformat()
                             if o.last_modified else None
                         )}
                        for o in objs[:200]
                    ],
                }, ensure_ascii=False, indent=2),
            )]

        if name == "delete_object":
            ok = await backend.delete(arguments["key"])
            return [TextContent(
                type="text",
                text="Удалено" if ok else "Не найдено",
            )]

        if name == "exists":
            ok = await backend.exists(arguments["key"])
            return [TextContent(
                type="text", text="true" if ok else "false",
            )]

        if name == "presign":
            url = await backend.presign(
                arguments["key"],
                arguments.get("expires_sec", 3600),
            )
            return [TextContent(type="text", text=url or "Не удалось")]

        if name == "sync_up":
            local_dir = _safe_local(arguments["local_dir"])
            prefix = arguments.get("key_prefix", "")
            pattern = arguments.get("pattern", "**/*")

            files = [f for f in local_dir.glob(pattern) if f.is_file()]
            uploaded = 0
            errors: list[str] = []
            for f in files[:1000]:
                rel = str(f.relative_to(local_dir)).replace("\\", "/")
                key = f"{prefix}/{rel}".strip("/")
                r = await backend.upload(f, key)
                if r.success:
                    uploaded += 1
                else:
                    errors.append(f"{key}: {r.error}")

            return [TextContent(
                type="text",
                text=json.dumps({
                    "total": len(files),
                    "uploaded": uploaded,
                    "errors": errors[:20],
                }, ensure_ascii=False, indent=2),
            )]

        if name == "sync_down":
            prefix = arguments["key_prefix"]
            local_dir = _safe_local(arguments["local_dir"])
            local_dir.mkdir(parents=True, exist_ok=True)

            objs = await backend.list(prefix, limit=2000)
            downloaded = 0
            errors: list[str] = []
            for o in objs:
                rel = o.key[len(prefix):].lstrip("/")
                target = local_dir / rel
                ok = await backend.download(o.key, target)
                if ok:
                    downloaded += 1
                else:
                    errors.append(o.key)

            return [TextContent(
                type="text",
                text=json.dumps({
                    "total": len(objs),
                    "downloaded": downloaded,
                    "errors": errors[:20],
                }, ensure_ascii=False, indent=2),
            )]

        return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]
    except Exception as e:
        logger.exception("Storage tool %s failed", name)
        return [TextContent(type="text", text=f"Ошибка: {e}")]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
