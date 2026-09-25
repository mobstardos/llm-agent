"""MCP-сервер: файловая система проекта с sandboxing.

PROJECT_ROOT читается на каждом вызове из data/runtime.json (fallback: env).
Это позволяет менять папку проекта из веб-интерфейса без перезапуска.
"""
import difflib
import os
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

try:
    from src.runtime_config import get_project_root as _rt_get_root
except Exception:
    _rt_get_root = None

IGNORE_DIRS = {
    ".git", ".venv", "venv", "__pycache__", "node_modules",
    ".idea", ".vscode", "dist", "build", ".pytest_cache",
    ".mypy_cache", ".ruff_cache",
}

app = Server("filesystem")


def _get_root() -> Path:
    val = ""
    if _rt_get_root is not None:
        try:
            val = _rt_get_root(default="")
        except Exception:
            val = ""
    if not val:
        val = os.getenv("PROJECT_ROOT", "").strip()
    if not val:
        val = os.getcwd()
    return Path(val).resolve()


def _safe(path: str) -> Path:
    root = _get_root()
    p = (root / path).resolve()
    if root not in p.parents and p != root:
        raise ValueError(f"Путь вне корня проекта: {path}")
    return p


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="list_files",
             description="Список файлов в директории (рекурсивно, без мусора).",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string", "default": "."}}}),
        Tool(name="read_file", description="Прочитать содержимое файла.",
             inputSchema={"type": "object", "properties": {"path": {"type": "string"}},
                          "required": ["path"]}),
        Tool(name="search_in_files", description="Найти подстроку в файлах директории.",
             inputSchema={"type": "object", "properties": {
                 "query": {"type": "string"},
                 "path": {"type": "string", "default": "."}},
                 "required": ["query"]}),
        Tool(name="write_file", description="Создать или полностью перезаписать файл.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"}, "content": {"type": "string"}},
                 "required": ["path", "content"]}),
        Tool(name="apply_patch",
             description=("Применить unified diff к файлу. Формат: заголовки ---/+++, "
                          "затем блоки @@ -N,M +N,M @@ со строками ' ', '-', '+'."),
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"}, "patch": {"type": "string"}},
                 "required": ["path", "patch"]}),
        Tool(name="delete_file", description="Удалить файл.",
             inputSchema={"type": "object", "properties": {"path": {"type": "string"}},
                          "required": ["path"]}),
        Tool(name="current_root", description="Показать текущий PROJECT_ROOT.",
             inputSchema={"type": "object", "properties": {}}),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        if name == "current_root":
            return [TextContent(type="text", text=str(_get_root()))]

        if name == "list_files":
            base = _safe(arguments.get("path", "."))
            if not base.exists():
                return [TextContent(type="text", text=f"Не найдено: {base}")]
            root = _get_root()
            out: list[str] = []
            for p in base.rglob("*"):
                if any(part in IGNORE_DIRS for part in p.parts):
                    continue
                if p.is_file():
                    try:
                        out.append(str(p.relative_to(root)))
                    except ValueError:
                        out.append(str(p))
            return [TextContent(type="text", text="\n".join(sorted(out)) or "(пусто)")]

        if name == "read_file":
            p = _safe(arguments["path"])
            if not p.exists():
                return [TextContent(type="text", text=f"Файл не найден: {arguments['path']}")]
            return [TextContent(type="text",
                                text=p.read_text(encoding="utf-8", errors="replace"))]

        if name == "search_in_files":
            query = arguments["query"]
            base = _safe(arguments.get("path", "."))
            root = _get_root()
            hits: list[str] = []
            for p in base.rglob("*"):
                if any(part in IGNORE_DIRS for part in p.parts) or not p.is_file():
                    continue
                try:
                    text = p.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                for i, line in enumerate(text.splitlines(), 1):
                    if query in line:
                        try:
                            rel = p.relative_to(root)
                        except ValueError:
                            rel = p
                        hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                        if len(hits) >= 200:
                            break
            return [TextContent(type="text", text="\n".join(hits) or "Совпадений нет")]

        if name == "write_file":
            p = _safe(arguments["path"])
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(arguments["content"], encoding="utf-8")
            return [TextContent(type="text", text=f"Записан: {arguments['path']}")]

        if name == "apply_patch":
            p = _safe(arguments["path"])
            if not p.exists():
                return [TextContent(type="text", text=f"Файл не найден: {arguments['path']}")]
            original = p.read_text(encoding="utf-8").splitlines(keepends=True)
            patch_lines = arguments["patch"].splitlines(keepends=True)

            new_lines: list[str] = []
            i = 0
            while i < len(patch_lines) and not patch_lines[i].startswith("@@"):
                i += 1

            src_idx = 0
            while i < len(patch_lines):
                header = patch_lines[i]
                if not header.startswith("@@"):
                    i += 1
                    continue
                try:
                    rng = header.split("@@")[1].strip()
                    old_range = rng.split(" ")[0]
                    start = int(old_range[1:].split(",")[0])
                except Exception:
                    return [TextContent(type="text", text="Некорректный заголовок патча")]

                while src_idx < start - 1:
                    new_lines.append(original[src_idx])
                    src_idx += 1

                i += 1
                while i < len(patch_lines) and not patch_lines[i].startswith("@@"):
                    line = patch_lines[i]
                    if line.startswith(" "):
                        new_lines.append(original[src_idx])
                        src_idx += 1
                    elif line.startswith("-"):
                        src_idx += 1
                    elif line.startswith("+"):
                        new_lines.append(line[1:])
                    i += 1

            while src_idx < len(original):
                new_lines.append(original[src_idx])
                src_idx += 1

            p.write_text("".join(new_lines), encoding="utf-8")
            diff = "".join(difflib.unified_diff(
                original, new_lines, fromfile=str(p), tofile=str(p)
            ))
            return [TextContent(type="text",
                                text=f"Патч применён к {arguments['path']}\n{diff[:2000]}")]

        if name == "delete_file":
            p = _safe(arguments["path"])
            if p.exists():
                p.unlink()
                return [TextContent(type="text", text=f"Удалён: {arguments['path']}")]
            return [TextContent(type="text", text=f"Не найден: {arguments['path']}")]

        return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]
    except Exception as e:
        return [TextContent(type="text", text=f"Ошибка: {e}")]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
