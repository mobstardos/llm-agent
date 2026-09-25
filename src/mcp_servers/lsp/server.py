"""MCP-сервер: LSP."""
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
logger = logging.getLogger("lsp-mcp")

_manager = None


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


def _get_manager():
    global _manager
    if _manager is None:
        from src.mcp_servers.lsp.manager import LSPManager
        _manager = LSPManager(_get_root())
    return _manager


app = Server("lsp")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="lsp_status",
             description="Какие LSP-серверы доступны и запущены.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="lsp_diagnostics",
             description="Ошибки и предупреждения в файле.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"}},
                 "required": ["path"]}),
        Tool(name="lsp_hover",
             description="Информация о символе под курсором.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"},
                 "line": {"type": "integer"},
                 "col": {"type": "integer"}},
                 "required": ["path", "line", "col"]}),
        Tool(name="lsp_definition",
             description="Перейти к определению.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"},
                 "line": {"type": "integer"},
                 "col": {"type": "integer"}},
                 "required": ["path", "line", "col"]}),
        Tool(name="lsp_references",
             description="Все ссылки на символ.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"},
                 "line": {"type": "integer"},
                 "col": {"type": "integer"}},
                 "required": ["path", "line", "col"]}),
        Tool(name="lsp_implementation",
             description="Реализации интерфейса/абстрактного метода.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"},
                 "line": {"type": "integer"},
                 "col": {"type": "integer"}},
                 "required": ["path", "line", "col"]}),
        Tool(name="lsp_completion",
             description="Автодополнение в позиции.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"},
                 "line": {"type": "integer"},
                 "col": {"type": "integer"}},
                 "required": ["path", "line", "col"]}),
        Tool(name="lsp_signature_help",
             description="Подсказка по аргументам функции.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"},
                 "line": {"type": "integer"},
                 "col": {"type": "integer"}},
                 "required": ["path", "line", "col"]}),
        Tool(name="lsp_document_symbols",
             description="Все символы в файле (структура).",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"}},
                 "required": ["path"]}),
        Tool(name="lsp_workspace_symbols",
             description="Поиск символов по всему проекту.",
             inputSchema={"type": "object", "properties": {
                 "query": {"type": "string"}},
                 "required": ["query"]}),
        Tool(name="lsp_rename",
             description="Переименовать символ через LSP (безопасно).",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"},
                 "line": {"type": "integer"},
                 "col": {"type": "integer"},
                 "new_name": {"type": "string"},
                 "dry_run": {"type": "boolean", "default": True}},
                 "required": ["path", "line", "col", "new_name"]}),
        Tool(name="lsp_code_action",
             description="Доступные быстрые фиксы в позиции.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"},
                 "line": {"type": "integer"},
                 "col": {"type": "integer"}},
                 "required": ["path", "line", "col"]}),
        Tool(name="lsp_format",
             description="Форматирование файла через LSP.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"}},
                 "required": ["path"]}),
    ]


def _pos(line: int, col: int) -> dict:
    """LSP использует 0-based строки и столбцы."""
    return {"line": max(0, line - 1), "character": max(0, col - 1)}


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        mgr = _get_manager()

        if name == "lsp_status":
            available = mgr.detect_available()
            return [TextContent(
                type="text",
                text=json.dumps({
                    "available_languages": available,
                    "active_clients": list(mgr._clients.keys()),
                }, ensure_ascii=False, indent=2),
            )]

        # Все остальные требуют path
        p = _safe(arguments["path"])
        if not p.exists():
            return [TextContent(type="text", text=f"Файл не найден: {p}")]

        lang = mgr.language_for(p)
        if not lang:
            return [TextContent(
                type="text",
                text=f"LSP не поддерживается для {p.suffix}",
            )]

        client = await mgr.get_client(lang)
        if client is None:
            return [TextContent(
                type="text",
                text=f"LSP-сервер для {lang} не найден. Установите:\n"
                     f"  pyright: npm install -g pyright\n"
                     f"  pylsp: pip install python-lsp-server\n"
                     f"  typescript: npm install -g typescript typescript-language-server",
            )]

        # Открываем файл в LSP
        uri = await client.open_document(p, lang)

        # ─── Diagnostics ─────────────────────────────
        if name == "lsp_diagnostics":
            # Diagnostics приходят как notification — нужно немного подождать
            await asyncio.sleep(1.5)
            # Заглушка: запрашиваем через pull (не все LSP поддерживают)
            result = await client._request("textDocument/diagnostic", {
                "textDocument": {"uri": uri},
            })
            if result:
                return [TextContent(
                    type="text",
                    text=json.dumps(result, ensure_ascii=False, indent=2),
                )]
            return [TextContent(
                type="text",
                text="Diagnostics не доступны через pull. "
                     "Сервер может отправлять их notification'ами.",
            )]

        # ─── Position-based ─────────────────────────
        if name in ("lsp_hover", "lsp_definition", "lsp_references",
                    "lsp_implementation", "lsp_completion",
                    "lsp_signature_help", "lsp_code_action", "lsp_rename"):
            line = arguments["line"]
            col = arguments["col"]
            pos = _pos(line, col)
            td = {"textDocument": {"uri": uri}}

            if name == "lsp_hover":
                result = await client._request("textDocument/hover",
                                               {**td, "position": pos})
            elif name == "lsp_definition":
                result = await client._request("textDocument/definition",
                                               {**td, "position": pos})
            elif name == "lsp_references":
                result = await client._request("textDocument/references",
                                               {**td, "position": pos,
                                                "context": {"includeDeclaration": True}})
            elif name == "lsp_implementation":
                result = await client._request("textDocument/implementation",
                                               {**td, "position": pos})
            elif name == "lsp_completion":
                result = await client._request("textDocument/completion",
                                               {**td, "position": pos})
            elif name == "lsp_signature_help":
                result = await client._request("textDocument/signatureHelp",
                                               {**td, "position": pos})
            elif name == "lsp_code_action":
                result = await client._request("textDocument/codeAction", {
                    **td, "range": {"start": pos, "end": pos},
                    "context": {"diagnostics": []},
                })
            elif name == "lsp_rename":
                new_name = arguments["new_name"]
                dry = arguments.get("dry_run", True)
                result = await client._request("textDocument/rename", {
                    **td, "newName": new_name,
                })
                if result is None:
                    return [TextContent(
                        type="text", text="Rename не удался",
                    )]
                # Применяем WorkspaceEdit
                changes = result.get("changes") or {}
                applied = 0
                for file_uri, edits in changes.items():
                    file_path = Path(file_uri.replace("file:///", "").replace("file://", ""))
                    try:
                        file_path = file_path.resolve()
                    except Exception:
                        continue
                    if not file_path.exists():
                        continue
                    text = file_path.read_text(encoding="utf-8")
                    lines = text.splitlines(keepends=True)
                    # Сортируем edits снизу вверх
                    sorted_edits = sorted(
                        edits,
                        key=lambda e: (e["range"]["start"]["line"],
                                       e["range"]["start"]["character"]),
                        reverse=True,
                    )
                    for e in sorted_edits:
                        sl = e["range"]["start"]["line"]
                        sc = e["range"]["start"]["character"]
                        el = e["range"]["end"]["line"]
                        ec = e["range"]["end"]["character"]
                        if sl == el:
                            prefix = lines[sl][:sc]
                            suffix = lines[sl][ec:]
                            lines[sl] = prefix + e["newText"] + suffix
                        else:
                            # Многострочный — пропускаем (редко)
                            continue
                    if not dry:
                        file_path.write_text("".join(lines), encoding="utf-8")
                        applied += 1
                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "dry_run": dry,
                        "files_changed": applied if not dry else 0,
                        "files_to_change": len(changes),
                    }, ensure_ascii=False, indent=2),
                )]

            return [TextContent(
                type="text",
                text=json.dumps(result, ensure_ascii=False, indent=2),
            )]

        # ─── Document / Workspace symbols ───────────
        if name == "lsp_document_symbols":
            result = await client._request("textDocument/documentSymbol", {
                "textDocument": {"uri": uri},
            })
            return [TextContent(
                type="text",
                text=json.dumps(result, ensure_ascii=False, indent=2),
            )]

        if name == "lsp_workspace_symbols":
            query = arguments["query"]
            result = await client._request("workspace/symbol", {"query": query})
            return [TextContent(
                type="text",
                text=json.dumps(result, ensure_ascii=False, indent=2),
            )]

        if name == "lsp_format":
            result = await client._request("textDocument/formatting", {
                "textDocument": {"uri": uri},
                "options": {"tabSize": 4, "insertSpaces": True},
            })
            if not result:
                return [TextContent(
                    type="text", text="Форматирование не поддерживается",
                )]

            text = p.read_text(encoding="utf-8")
            lines = text.splitlines(keepends=True)
            sorted_edits = sorted(
                result,
                key=lambda e: (e["range"]["start"]["line"],
                               e["range"]["start"]["character"]),
                reverse=True,
            )
            for e in sorted_edits:
                sl = e["range"]["start"]["line"]
                sc = e["range"]["start"]["character"]
                el = e["range"]["end"]["line"]
                ec = e["range"]["end"]["character"]
                if sl == el:
                    prefix = lines[sl][:sc]
                    suffix = lines[sl][ec:]
                    lines[sl] = prefix + e["newText"] + suffix
            p.write_text("".join(lines), encoding="utf-8")
            return [TextContent(
                type="text", text=f"Отформатирован ({len(result)} правок)",
            )]

        return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]
    except Exception as e:
        logger.exception("LSP tool %s failed", name)
        return [TextContent(type="text", text=f"Ошибка: {e}")]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
