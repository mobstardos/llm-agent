"""MCP-сервер: документация."""
from __future__ import annotations

import json
import asyncio
import logging
import os
import re
import subprocess
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("documentation-mcp")

IGNORE_DIRS = {".git", ".venv", "venv", "__pycache__", "node_modules",
               ".idea", ".vscode", "dist", "build", "target"}


def _get_root() -> Path:
    try:
        from src.runtime_config import get_project_root
        val = get_project_root(default="")
        if val:
            return Path(val).resolve()
    except Exception:
        pass
    return Path(os.getenv("PROJECT_ROOT", os.getcwd())).resolve()


def _safe(path: str) -> Path:
    root = _get_root()
    p = (root / path).resolve()
    if root not in p.parents and p != root:
        raise ValueError(f"Путь вне корня: {path}")
    return p


app = Server("documentation")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="docstring_extract",
             description="Извлечь docstring'и из Python-модуля.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"}},
                 "required": ["path"]}),
        Tool(name="docstring_generate",
             description="Сгенерировать docstring-заготовку для функции/класса.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"},
                 "name": {"type": "string"}},
                 "required": ["path", "name"]}),
        Tool(name="docstring_coverage",
             description="Процент функций/классов с docstring'ами.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string", "default": "."}}}),
        Tool(name="api_docs_extract",
             description="Извлечь публичный API Python-модуля.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"},
                 "module": {"type": "string"}},
                 "required": ["path"]}),
        Tool(name="mermaid_render",
             description="Отрендерить Mermaid в SVG/PNG (если установлен mmdc).",
             inputSchema={"type": "object", "properties": {
                 "code": {"type": "string"},
                 "output": {"type": "string"}},
                 "required": ["code", "output"]}),
        Tool(name="mermaid_generate_from_graph",
             description="Mermaid-диаграмма из dependency_graph.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string", "default": "."},
                 "output": {"type": "string"}}}),
        Tool(name="openapi_from_fastapi",
             description="Извлечь OpenAPI из FastAPI app.",
             inputSchema={"type": "object", "properties": {
                 "url": {"type": "string", "default": "http://127.0.0.1:8000/openapi.json"},
                 "output": {"type": "string"}}}),
        Tool(name="changelog_from_git",
             description="Сгенерировать changelog из git log.",
             inputSchema={"type": "object", "properties": {
                 "since": {"type": "string", "default": "7 days ago"},
                 "output": {"type": "string"}}}),
        Tool(name="readme_outline",
             description="Извлечь структуру README.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string", "default": "README.md"}}}),
        Tool(name="markdown_toc",
             description="Сгенерировать оглавление в Markdown.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"}},
                 "required": ["path"]}),
        Tool(name="markdown_lint",
             description="Проверка Markdown на базовые проблемы.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"}},
                 "required": ["path"]}),
    ]


def _extract_docstrings_py(source: str, rel: str) -> list[dict]:
    import ast
    result: list[dict] = []
    try:
        tree = ast.parse(source)
    except Exception:
        return result

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(node) or ""
            result.append({
                "name": node.name,
                "kind": "function" if not isinstance(node, ast.ClassDef) else "class",
                "file": rel,
                "line": node.lineno,
                "has_docstring": bool(doc),
                "docstring": doc[:500],
            })
    return result


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    root = _get_root()

    try:
        if name == "docstring_extract":
            p = _safe(arguments["path"])
            if not p.exists() or p.suffix != ".py":
                return [TextContent(type="text", text="Python-файл не найден")]
            text = p.read_text(encoding="utf-8", errors="replace")
            rel = str(p.relative_to(root)) if root in p.parents else str(p)
            docs = _extract_docstrings_py(text, rel)
            return [TextContent(
                type="text", text=json.dumps(docs, ensure_ascii=False, indent=2),
            )]

        if name == "docstring_generate":
            p = _safe(arguments["path"])
            target = arguments["name"]
            if not p.exists():
                return [TextContent(type="text", text="Файл не найден")]
            text = p.read_text(encoding="utf-8", errors="replace")
            # Простой парсинг сигнатуры
            m = re.search(
                rf"(def|class)\s+{re.escape(target)}\s*\(([^)]*)\)",
                text,
            )
            if not m:
                return [TextContent(type="text", text="Функция/класс не найдены")]
            kind, params = m.group(1), m.group(2)
            param_names = []
            for param in params.split(","):
                pn = param.strip().split("=")[0].split(":")[0].strip()
                if pn and pn not in ("self", "cls", "*", "**"):
                    param_names.append(pn)

            if kind == "def":
                lines = [f'"""{target} — TODO описание.', ""]
                if param_names:
                    lines.append("Args:")
                    for pn in param_names:
                        lines.append(f"    {pn}: описание")
                    lines.append("")
                lines.append("Returns:")
                lines.append("    описание")
                lines.append('"""')
            else:
                lines = [f'"""{target} — TODO описание класса."""']

            return [TextContent(type="text", text="\n".join(lines))]

        if name == "docstring_coverage":
            base = _safe(arguments.get("path", "."))
            total = with_doc = 0
            by_file: list[dict] = []
            for p in base.rglob("*.py"):
                if any(part in IGNORE_DIRS for part in p.parts):
                    continue
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                rel = str(p.relative_to(root)) if root in p.parents else str(p)
                docs = _extract_docstrings_py(text, rel)
                if not docs:
                    continue
                f_total = len(docs)
                f_with = sum(1 for d in docs if d["has_docstring"])
                total += f_total
                with_doc += f_with
                by_file.append({
                    "file": rel,
                    "total": f_total,
                    "with_docstring": f_with,
                    "percent": round(f_with / f_total * 100, 1),
                })

            overall = round(with_doc / total * 100, 1) if total else 0
            return [TextContent(
                type="text",
                text=json.dumps({
                    "overall_percent": overall,
                    "total": total,
                    "with_docstring": with_doc,
                    "files": sorted(by_file, key=lambda x: x["percent"])[:50],
                }, ensure_ascii=False, indent=2),
            )]

        if name == "api_docs_extract":
            p = _safe(arguments["path"])
            if not p.exists():
                return [TextContent(type="text", text="Файл не найден")]
            text = p.read_text(encoding="utf-8", errors="replace")
            rel = str(p.relative_to(root)) if root in p.parents else str(p)
            docs = _extract_docstrings_py(text, rel)
            public = [d for d in docs if not d["name"].startswith("_")]
            return [TextContent(
                type="text", text=json.dumps(public, ensure_ascii=False, indent=2),
            )]

        if name == "mermaid_render":
            code = arguments["code"]
            output = _safe(arguments["output"])

            # Проверяем mmdc
            mmdc = shutil.which("mmdc") if hasattr(__import__("shutil"), "which") else None
            if mmdc is None:
                # Сохраняем .mmd
                mmd_path = output.with_suffix(".mmd")
                mmd_path.write_text(code, encoding="utf-8")
                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "note": "mmdc не установлен. Сохранён .mmd файл.",
                        "file": str(mmd_path),
                        "install": "npm install -g @mermaid-js/mermaid-cli",
                    }, ensure_ascii=False, indent=2),
                )]

            input_path = output.with_suffix(".mmd")
            input_path.write_text(code, encoding="utf-8")
            try:
                result = subprocess.run(
                    [mmdc, "-i", str(input_path), "-o", str(output)],
                    capture_output=True, text=True, timeout=60,
                )
                return [TextContent(
                    type="text",
                    text=json.dumps({
                        "output": str(output),
                        "exit_code": result.returncode,
                        "stderr": result.stderr[:500],
                    }, ensure_ascii=False, indent=2),
                )]
            except Exception as e:
                return [TextContent(type="text", text=f"Render error: {e}")]

        if name == "mermaid_generate_from_graph":
            base = _safe(arguments.get("path", "."))
            output = _safe(arguments.get("output", "graph.md"))

            # Граф зависимостей по импортам Python
            edges: list[tuple[str, str]] = []
            for p in base.rglob("*.py"):
                if any(part in IGNORE_DIRS for part in p.parts):
                    continue
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                rel = str(p.relative_to(base)) if base in p.parents else str(p)
                mod_name = rel.replace("/", ".").replace(".py", "")
                for m in re.finditer(r"^from\s+([.\w]+)\s+import", text, re.MULTILINE):
                    edges.append((mod_name, m.group(1)))

            # Mermaid
            lines = ["graph TD"]
            seen = set()
            for src, dst in edges[:100]:
                if (src, dst) in seen:
                    continue
                seen.add((src, dst))
                lines.append(f'    {src.replace(".", "_")} --> {dst.replace(".", "_")}')

            result = "\n".join(lines[:200])
            out_path = _safe(output)
            out_path.write_text(
                f"```mermaid\n{result}\n```\n",
                encoding="utf-8",
            )
            return [TextContent(type="text", text=f"Mermaid-граф сохранён: {out_path}")]

        return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]
    except Exception as e:
        logger.exception("Documentation tool %s failed", name)
        return [TextContent(type="text", text=f"Ошибка: {e}")]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())