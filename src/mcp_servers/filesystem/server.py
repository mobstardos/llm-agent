"""MCP-сервер: файловая система проекта с sandboxing.

Расширенная версия: базовые операции + move/rename/copy/symlink,
поиск (glob/content/mtime/size), архивы, diff, hash.
"""
from __future__ import annotations

import asyncio
import difflib
import fnmatch
import hashlib
import logging
import os
import shutil
import tarfile
import time
import zipfile
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("filesystem-mcp")

try:
    from src.runtime_config import get_project_root as _rt_get_root
except Exception:
    _rt_get_root = None

try:
    import pathspec
    HAS_PATHSPEC = True
except ImportError:
    HAS_PATHSPEC = False

IGNORE_DIRS = {
    ".git", ".venv", "venv", "__pycache__", "node_modules",
    ".idea", ".vscode", "dist", "build", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "target", ".next",
}

app = Server("filesystem")


# ═════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════
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


def _load_gitignore() -> "pathspec.PathSpec | None":
    if not HAS_PATHSPEC:
        return None
    root = _get_root()
    gi = root / ".gitignore"
    if not gi.exists():
        return None
    try:
        with open(gi, "r", encoding="utf-8") as f:
            return pathspec.PathSpec.from_lines("gitwildmatch", f)
    except Exception:
        return None


def _is_ignored(path: Path, root: Path, spec) -> bool:
    try:
        rel = path.relative_to(root)
    except ValueError:
        return False
    if any(part in IGNORE_DIRS for part in rel.parts):
        return True
    if spec is not None:
        try:
            if spec.match_file(str(rel).replace("\\", "/")):
                return True
        except Exception:
            pass
    return False


# ═════════════════════════════════════════════════════════
# Tool definitions
# ═════════════════════════════════════════════════════════
@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        # ─── Базовые ────────────────────────────────
        Tool(name="list_files",
             description="Список файлов в директории (рекурсивно, без мусора).",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string", "default": "."},
                 "respect_gitignore": {"type": "boolean", "default": True}}}),
        Tool(name="list_tree",
             description="Дерево директорий с отступами.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string", "default": "."},
                 "max_depth": {"type": "integer", "default": 4}}}),
        Tool(name="read_file",
             description="Прочитать содержимое файла.",
             inputSchema={"type": "object", "properties": {"path": {"type": "string"}},
                          "required": ["path"]}),
        Tool(name="read_file_range",
             description="Прочитать диапазон строк файла.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"},
                 "start_line": {"type": "integer"},
                 "end_line": {"type": "integer"}},
                 "required": ["path", "start_line", "end_line"]}),
        Tool(name="search_in_files",
             description="Найти подстроку в файлах директории (простой поиск).",
             inputSchema={"type": "object", "properties": {
                 "query": {"type": "string"},
                 "path": {"type": "string", "default": "."}},
                 "required": ["query"]}),
        Tool(name="write_file",
             description="Создать или полностью перезаписать файл.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"}, "content": {"type": "string"}},
                 "required": ["path", "content"]}),
        Tool(name="apply_patch",
             description=("Применить unified diff к файлу."),
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"}, "patch": {"type": "string"}},
                 "required": ["path", "patch"]}),
        Tool(name="delete_file",
             description="Удалить файл.",
             inputSchema={"type": "object", "properties": {"path": {"type": "string"}},
                          "required": ["path"]}),
        Tool(name="current_root",
             description="Показать текущий PROJECT_ROOT.",
             inputSchema={"type": "object", "properties": {}}),

        # ─── Расширенные ────────────────────────────
        Tool(name="move_file",
             description="Переместить файл.",
             inputSchema={"type": "object", "properties": {
                 "src": {"type": "string"}, "dst": {"type": "string"}},
                 "required": ["src", "dst"]}),
        Tool(name="rename_file",
             description="Переименовать файл (в той же директории).",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"}, "new_name": {"type": "string"}},
                 "required": ["path", "new_name"]}),
        Tool(name="copy_file",
             description="Копировать файл.",
             inputSchema={"type": "object", "properties": {
                 "src": {"type": "string"}, "dst": {"type": "string"}},
                 "required": ["src", "dst"]}),
        Tool(name="move_dir",
             description="Переместить директорию.",
             inputSchema={"type": "object", "properties": {
                 "src": {"type": "string"}, "dst": {"type": "string"}},
                 "required": ["src", "dst"]}),
        Tool(name="create_dir",
             description="Создать директорию.",
             inputSchema={"type": "object", "properties": {"path": {"type": "string"}},
                          "required": ["path"]}),
        Tool(name="delete_dir",
             description="Удалить директорию.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"},
                 "recursive": {"type": "boolean", "default": False}},
                 "required": ["path"]}),
        Tool(name="symlink",
             description="Создать символическую ссылку.",
             inputSchema={"type": "object", "properties": {
                 "target": {"type": "string"}, "link_path": {"type": "string"}},
                 "required": ["target", "link_path"]}),
        Tool(name="read_symlink",
             description="Прочитать, куда указывает симлинк.",
             inputSchema={"type": "object", "properties": {"path": {"type": "string"}},
                          "required": ["path"]}),
        Tool(name="chmod",
             description="Изменить права доступа (Unix).",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"}, "mode": {"type": "string"}},
                 "required": ["path", "mode"]}),

        # ─── Поиск ──────────────────────────────────
        Tool(name="find_by_glob",
             description="Найти файлы по glob-паттерну (**/*.py).",
             inputSchema={"type": "object", "properties": {
                 "pattern": {"type": "string"},
                 "path": {"type": "string", "default": "."},
                 "limit": {"type": "integer", "default": 500}},
                 "required": ["pattern"]}),
        Tool(name="find_by_name",
             description="Найти файлы, чьё имя содержит подстроку.",
             inputSchema={"type": "object", "properties": {
                 "name": {"type": "string"},
                 "path": {"type": "string", "default": "."},
                 "limit": {"type": "integer", "default": 500}},
                 "required": ["name"]}),
        Tool(name="find_by_content",
             description="Найти файлы, содержащие regex (с контекстом строк).",
             inputSchema={"type": "object", "properties": {
                 "pattern": {"type": "string"},
                 "path": {"type": "string", "default": "."},
                 "case_sensitive": {"type": "boolean", "default": False},
                 "max_results": {"type": "integer", "default": 200}},
                 "required": ["pattern"]}),
        Tool(name="find_by_mtime",
             description="Найти файлы, изменённые за последние N дней.",
             inputSchema={"type": "object", "properties": {
                 "days": {"type": "integer", "default": 7},
                 "path": {"type": "string", "default": "."},
                 "limit": {"type": "integer", "default": 500}},
                 "required": ["days"]}),
        Tool(name="find_by_size",
             description="Найти файлы по размеру (в KB).",
             inputSchema={"type": "object", "properties": {
                 "min_kb": {"type": "integer", "default": 0},
                 "max_kb": {"type": "integer"},
                 "path": {"type": "string", "default": "."},
                 "limit": {"type": "integer", "default": 500}}}),
        Tool(name="grep",
             description="Поиск через regex по файлам (с номерами строк и контекстом).",
             inputSchema={"type": "object", "properties": {
                 "pattern": {"type": "string"},
                 "path": {"type": "string", "default": "."},
                 "file_glob": {"type": "string", "default": "*"},
                 "case_sensitive": {"type": "boolean", "default": False},
                 "context_lines": {"type": "integer", "default": 0},
                 "max_results": {"type": "integer", "default": 200}},
                 "required": ["pattern"]}),

        # ─── Информация ─────────────────────────────
        Tool(name="file_info",
             description="Информация о файле: размер, mtime, тип, hash.",
             inputSchema={"type": "object", "properties": {"path": {"type": "string"}},
                          "required": ["path"]}),
        Tool(name="disk_usage",
             description="Использование диска в директории.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string", "default": "."},
                 "top_n": {"type": "integer", "default": 10}}}),
        Tool(name="diff_files",
             description="Сравнить два файла (unified diff).",
             inputSchema={"type": "object", "properties": {
                 "path_a": {"type": "string"}, "path_b": {"type": "string"},
                 "context_lines": {"type": "integer", "default": 3}},
                 "required": ["path_a", "path_b"]}),
        Tool(name="hash_file",
             description="SHA256-хэш файла.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"},
                 "algorithm": {"type": "string", "default": "sha256"}},
                 "required": ["path"]}),
        Tool(name="count_lines",
             description="Количество строк (total/code/blank/comment) в файле или директории.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"}}}),
        Tool(name="dir_stats",
             description="Статистика директории: файлы по расширению, размер.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string", "default": "."}}}),

        # ─── Архивы ─────────────────────────────────
        Tool(name="list_archive",
             description="Список файлов внутри архива (zip/tar/7z).",
             inputSchema={"type": "object", "properties": {"path": {"type": "string"}},
                          "required": ["path"]}),
        Tool(name="create_archive",
             description="Создать архив из файлов/директорий.",
             inputSchema={"type": "object", "properties": {
                 "dest": {"type": "string"},
                 "sources": {"type": "array", "items": {"type": "string"}},
                 "format": {"type": "string", "default": "zip"}},
                 "required": ["dest", "sources"]}),
        Tool(name="extract_archive",
             description="Распаковать архив (безопасно).",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"},
                 "dest": {"type": "string"}},
                 "required": ["path", "dest"]}),
    ]


# ═════════════════════════════════════════════════════════
# Call tool
# ═════════════════════════════════════════════════════════
@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        root = _get_root()

        # ─── Утилиты ────────────────────────────────
        if name == "current_root":
            return [TextContent(type="text", text=str(root))]

        if name == "list_files":
            base = _safe(arguments.get("path", "."))
            if not base.exists():
                return [TextContent(type="text", text=f"Не найдено: {base}")]
            spec = _load_gitignore() if arguments.get("respect_gitignore", True) else None
            out: list[str] = []
            for p in base.rglob("*"):
                if spec is not None and _is_ignored(p, root, spec):
                    continue
                elif any(part in IGNORE_DIRS for part in p.parts):
                    continue
                if p.is_file():
                    try:
                        out.append(str(p.relative_to(root)))
                    except ValueError:
                        out.append(str(p))
            return [TextContent(type="text", text="\n".join(sorted(out)) or "(пусто)")]

        if name == "list_tree":
            base = _safe(arguments.get("path", "."))
            max_depth = arguments.get("max_depth", 4)
            if not base.exists():
                return [TextContent(type="text", text="Не найдено")]
            lines: list[str] = []
            def _walk(p: Path, prefix: str, depth: int):
                if depth > max_depth:
                    return
                try:
                    items = sorted(
                        [x for x in p.iterdir() if not _is_skipped(x)],
                        key=lambda x: (not x.is_dir(), x.name),
                    )
                except OSError:
                    return
                for i, item in enumerate(items):
                    is_last = (i == len(items) - 1)
                    connector = "└── " if is_last else "├── "
                    lines.append(f"{prefix}{connector}{item.name}")
                    if item.is_dir():
                        _walk(item, prefix + ("    " if is_last else "│   "), depth + 1)
            lines.append(base.name + "/")
            _walk(base, "", 1)
            return [TextContent(type="text", text="\n".join(lines[:500]))]

        if name == "read_file":
            p = _safe(arguments["path"])
            if not p.exists():
                return [TextContent(type="text", text=f"Файл не найден: {arguments['path']}")]
            return [TextContent(
                type="text",
                text=p.read_text(encoding="utf-8", errors="replace"),
            )]

        if name == "read_file_range":
            p = _safe(arguments["path"])
            if not p.exists():
                return [TextContent(type="text", text="Файл не найден")]
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
            start = max(0, arguments["start_line"] - 1)
            end = min(len(lines), arguments["end_line"])
            result = "\n".join(
                f"{i+1:5d}  {lines[i]}"
                for i in range(start, end)
            )
            return [TextContent(type="text", text=result or "(пусто)")]

        if name == "search_in_files":
            query = arguments["query"]
            base = _safe(arguments.get("path", "."))
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
                original, new_lines, fromfile=str(p), tofile=str(p),
            ))
            return [TextContent(type="text", text=f"Патч применён к {arguments['path']}\n{diff[:2000]}")]

        if name == "delete_file":
            p = _safe(arguments["path"])
            if p.exists():
                p.unlink()
                return [TextContent(type="text", text=f"Удалён: {arguments['path']}")]
            return [TextContent(type="text", text=f"Не найден: {arguments['path']}")]

        # ─── Расширенные ────────────────────────────
        if name == "move_file":
            src = _safe(arguments["src"])
            dst = _safe(arguments["dst"])
            if not src.exists():
                return [TextContent(type="text", text=f"Источник не найден: {src}")]
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            return [TextContent(type="text", text=f"Перемещён: {arguments['src']} → {arguments['dst']}")]

        if name == "rename_file":
            src = _safe(arguments["path"])
            new_name = arguments["new_name"]
            if not src.exists():
                return [TextContent(type="text", text="Файл не найден")]
            dst = src.parent / new_name
            src.rename(dst)
            return [TextContent(type="text", text=f"Переименован: {src.name} → {new_name}")]

        if name == "copy_file":
            src = _safe(arguments["src"])
            dst = _safe(arguments["dst"])
            if not src.exists():
                return [TextContent(type="text", text="Источник не найден")]
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dst))
            return [TextContent(type="text", text=f"Скопирован: {arguments['src']} → {arguments['dst']}")]

        if name == "move_dir":
            src = _safe(arguments["src"])
            dst = _safe(arguments["dst"])
            if not src.is_dir():
                return [TextContent(type="text", text="Директория не найдена")]
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dst))
            return [TextContent(type="text", text=f"Директория перемещена: {arguments['src']} → {arguments['dst']}")]

        if name == "create_dir":
            p = _safe(arguments["path"])
            p.mkdir(parents=True, exist_ok=True)
            return [TextContent(type="text", text=f"Создана: {arguments['path']}")]

        if name == "delete_dir":
            p = _safe(arguments["path"])
            if not p.exists():
                return [TextContent(type="text", text="Не найдено")]
            if arguments.get("recursive"):
                shutil.rmtree(str(p))
            else:
                p.rmdir()
            return [TextContent(type="text", text=f"Удалена: {arguments['path']}")]

        if name == "symlink":
            target = arguments["target"]
            link = _safe(arguments["link_path"])
            link.parent.mkdir(parents=True, exist_ok=True)
            if link.exists() or link.is_symlink():
                return [TextContent(type="text", text=f"Ссылка уже существует: {link}")]
            os.symlink(target, str(link))
            return [TextContent(type="text", text=f"Symlink создан: {link} → {target}")]

        if name == "read_symlink":
            p = _safe(arguments["path"])
            if not p.is_symlink():
                return [TextContent(type="text", text="Это не симлинк")]
            return [TextContent(type="text", text=str(p.readlink()))]

        if name == "chmod":
            p = _safe(arguments["path"])
            mode_str = arguments["mode"]
            mode = int(mode_str, 8)
            os.chmod(str(p), mode)
            return [TextContent(type="text", text=f"Права изменены: {arguments['path']} → {mode_str}")]

        # ─── Поиск ──────────────────────────────────
        if name == "find_by_glob":
            base = _safe(arguments.get("path", "."))
            pattern = arguments["pattern"]
            limit = arguments.get("limit", 500)
            matches = list(base.glob(pattern))[:limit]
            rel = []
            for m in matches:
                try:
                    rel.append(str(m.relative_to(root)))
                except ValueError:
                    rel.append(str(m))
            return [TextContent(type="text", text="\n".join(rel) or "Не найдено")]

        if name == "find_by_name":
            base = _safe(arguments.get("path", "."))
            name_sub = arguments["name"].lower()
            limit = arguments.get("limit", 500)
            matches: list[str] = []
            for p in base.rglob("*"):
                if any(part in IGNORE_DIRS for part in p.parts):
                    continue
                if name_sub in p.name.lower():
                    try:
                        matches.append(str(p.relative_to(root)))
                    except ValueError:
                        matches.append(str(p))
                    if len(matches) >= limit:
                        break
            return [TextContent(type="text", text="\n".join(matches) or "Не найдено")]

        if name == "find_by_content":
            base = _safe(arguments.get("path", "."))
            import re
            flags = 0 if arguments.get("case_sensitive") else re.IGNORECASE
            try:
                regex = re.compile(arguments["pattern"], flags)
            except re.error as e:
                return [TextContent(type="text", text=f"Ошибка regex: {e}")]
            max_results = arguments.get("max_results", 200)
            hits: list[str] = []
            for p in base.rglob("*"):
                if not p.is_file() or any(part in IGNORE_DIRS for part in p.parts):
                    continue
                try:
                    text = p.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                for i, line in enumerate(text.splitlines(), 1):
                    if regex.search(line):
                        try:
                            rel = p.relative_to(root)
                        except ValueError:
                            rel = p
                        hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                        if len(hits) >= max_results:
                            break
                if len(hits) >= max_results:
                    break
            return [TextContent(type="text", text="\n".join(hits) or "Совпадений нет")]

        if name == "find_by_mtime":
            base = _safe(arguments.get("path", "."))
            days = arguments["days"]
            limit = arguments.get("limit", 500)
            cutoff = time.time() - days * 86400
            matches: list[tuple[float, str]] = []
            for p in base.rglob("*"):
                if not p.is_file() or any(part in IGNORE_DIRS for part in p.parts):
                    continue
                try:
                    mtime = p.stat().st_mtime
                except OSError:
                    continue
                if mtime >= cutoff:
                    try:
                        rel = str(p.relative_to(root))
                    except ValueError:
                        rel = str(p)
                    matches.append((mtime, rel))
            matches.sort(reverse=True)
            out = [f"{m[1]}  ({time.strftime('%Y-%m-%d %H:%M', time.localtime(m[0]))})"
                   for m in matches[:limit]]
            return [TextContent(type="text", text="\n".join(out) or "Не найдено")]

        if name == "find_by_size":
            base = _safe(arguments.get("path", "."))
            min_kb = arguments.get("min_kb", 0)
            max_kb = arguments.get("max_kb")
            limit = arguments.get("limit", 500)
            matches: list[tuple[int, str]] = []
            for p in base.rglob("*"):
                if not p.is_file() or any(part in IGNORE_DIRS for part in p.parts):
                    continue
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                size_kb = size // 1024
                if size_kb < min_kb:
                    continue
                if max_kb is not None and size_kb > max_kb:
                    continue
                try:
                    rel = str(p.relative_to(root))
                except ValueError:
                    rel = str(p)
                matches.append((size, rel))
            matches.sort(reverse=True)
            out = [f"{m[1]}  ({m[0] // 1024} KB)" for m in matches[:limit]]
            return [TextContent(type="text", text="\n".join(out) or "Не найдено")]

        if name == "grep":
            base = _safe(arguments.get("path", "."))
            import re
            flags = 0 if arguments.get("case_sensitive") else re.IGNORECASE
            try:
                regex = re.compile(arguments["pattern"], flags)
            except re.error as e:
                return [TextContent(type="text", text=f"Ошибка regex: {e}")]
            file_glob = arguments.get("file_glob", "*")
            context = arguments.get("context_lines", 0)
            max_results = arguments.get("max_results", 200)
            hits: list[str] = []
            for p in base.rglob(file_glob):
                if not p.is_file() or any(part in IGNORE_DIRS for part in p.parts):
                    continue
                try:
                    text = p.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                lines = text.splitlines()
                try:
                    rel = p.relative_to(root)
                except ValueError:
                    rel = p
                for i, line in enumerate(lines):
                    if regex.search(line):
                        start = max(0, i - context)
                        end = min(len(lines), i + context + 1)
                        block = []
                        for j in range(start, end):
                            marker = ">" if j == i else " "
                            block.append(f"{marker} {rel}:{j+1}: {lines[j][:200]}")
                        hits.append("\n".join(block))
                        if len(hits) >= max_results:
                            break
                if len(hits) >= max_results:
                    break
            return [TextContent(type="text", text="\n---\n".join(hits) or "Совпадений нет")]

        # ─── Информация ─────────────────────────────
        if name == "file_info":
            p = _safe(arguments["path"])
            if not p.exists():
                return [TextContent(type="text", text="Не найдено")]
            st = p.stat()
            h = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else ""
            info = {
                "path": str(p.relative_to(root)) if root in p.parents else str(p),
                "type": "file" if p.is_file() else "dir" if p.is_dir() else "symlink" if p.is_symlink() else "other",
                "size": st.st_size,
                "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_mtime)),
                "ctime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st.st_ctime)),
                "sha256": h[:64],
            }
            import json
            return [TextContent(type="text", text=json.dumps(info, ensure_ascii=False, indent=2))]

        if name == "disk_usage":
            base = _safe(arguments.get("path", "."))
            top_n = arguments.get("top_n", 10)
            if not base.is_dir():
                return [TextContent(type="text", text="Не директория")]
            total = 0
            by_dir: dict[str, int] = {}
            for p in base.rglob("*"):
                if not p.is_file() or any(part in IGNORE_DIRS for part in p.parts):
                    continue
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                total += size
                try:
                    rel_dir = str(p.relative_to(base).parent).split(os.sep)[0]
                except ValueError:
                    rel_dir = "."
                by_dir[rel_dir] = by_dir.get(rel_dir, 0) + size
            top = sorted(by_dir.items(), key=lambda x: -x[1])[:top_n]
            lines = [f"Всего: {total // 1024 // 1024} MB ({total} bytes)", ""]
            for d, s in top:
                lines.append(f"  {d:40s}  {s // 1024:>10} KB")
            return [TextContent(type="text", text="\n".join(lines))]

        if name == "diff_files":
            a = _safe(arguments["path_a"])
            b = _safe(arguments["path_b"])
            if not a.exists() or not b.exists():
                return [TextContent(type="text", text="Один из файлов не найден")]
            text_a = a.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            text_b = b.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
            diff = "".join(difflib.unified_diff(
                text_a, text_b,
                fromfile=str(a), tofile=str(b),
                n=arguments.get("context_lines", 3),
            ))
            return [TextContent(type="text", text=diff or "(идентичны)")]

        if name == "hash_file":
            p = _safe(arguments["path"])
            if not p.exists():
                return [TextContent(type="text", text="Файл не найден")]
            algo = arguments.get("algorithm", "sha256")
            try:
                h = hashlib.new(algo)
            except ValueError:
                return [TextContent(type="text", text=f"Алгоритм не поддерживается: {algo}")]
            with open(p, "rb") as f:
                while chunk := f.read(65536):
                    h.update(chunk)
            return [TextContent(type="text", text=h.hexdigest())]

        if name == "count_lines":
            p = _safe(arguments.get("path", "."))
            targets = [p] if p.is_file() else [
                x for x in p.rglob("*")
                if x.is_file() and not any(part in IGNORE_DIRS for part in x.parts)
            ]
            total = code = blank = comment = 0
            for t in targets:
                try:
                    text = t.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                for line in text.splitlines():
                    total += 1
                    stripped = line.strip()
                    if not stripped:
                        blank += 1
                    elif stripped.startswith(("#", "//", "/*", "*", "--")):
                        comment += 1
                    else:
                        code += 1
            result = {
                "files": len(targets),
                "total": total,
                "code": code,
                "blank": blank,
                "comment": comment,
            }
            import json
            return [TextContent(type="text", text=json.dumps(result, indent=2))]

        if name == "dir_stats":
            base = _safe(arguments.get("path", "."))
            if not base.is_dir():
                return [TextContent(type="text", text="Не директория")]
            by_ext: dict[str, dict] = {}
            for p in base.rglob("*"):
                if not p.is_file() or any(part in IGNORE_DIRS for part in p.parts):
                    continue
                try:
                    size = p.stat().st_size
                except OSError:
                    continue
                ext = p.suffix.lower() or "(no ext)"
                if ext not in by_ext:
                    by_ext[ext] = {"count": 0, "size": 0}
                by_ext[ext]["count"] += 1
                by_ext[ext]["size"] += size
            lines = ["Extension   Files    Size"]
            for ext, data in sorted(by_ext.items(), key=lambda x: -x[1]["size"]):
                lines.append(f"{ext:12s} {data['count']:>5d}   {data['size'] // 1024:>8d} KB")
            return [TextContent(type="text", text="\n".join(lines))]

        # ─── Архивы ─────────────────────────────────
        if name == "list_archive":
            p = _safe(arguments["path"])
            if not p.exists():
                return [TextContent(type="text", text="Не найдено")]
            files: list[str] = []
            try:
                if zipfile.is_zipfile(p):
                    with zipfile.ZipFile(p) as z:
                        for info in z.infolist():
                            files.append(f"{info.filename}  ({info.file_size} bytes)")
                elif tarfile.is_tarfile(p):
                    with tarfile.open(p) as t:
                        for m in t.getmembers():
                            files.append(f"{m.name}  ({m.size} bytes)")
                else:
                    return [TextContent(type="text", text="Формат не поддерживается")]
            except Exception as e:
                return [TextContent(type="text", text=f"Ошибка: {e}")]
            return [TextContent(type="text", text="\n".join(files[:500]) or "(пусто)")]

        if name == "create_archive":
            dest = _safe(arguments["dest"])
            sources = [_safe(s) for s in arguments["sources"]]
            fmt = arguments.get("format", "zip").lower()
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                if fmt in ("zip",):
                    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
                        for src in sources:
                            if src.is_dir():
                                for f in src.rglob("*"):
                                    if f.is_file():
                                        z.write(f, f.relative_to(src.parent))
                            else:
                                z.write(src, src.name)
                elif fmt in ("tar", "tar.gz", "tgz", "tar.bz2"):
                    mode = "w:gz" if "gz" in fmt or fmt == "tgz" else "w:bz2" if "bz2" in fmt else "w"
                    with tarfile.open(dest, mode) as t:
                        for src in sources:
                            t.add(str(src), arcname=src.name)
                else:
                    return [TextContent(type="text", text=f"Формат не поддерживается: {fmt}")]
            except Exception as e:
                return [TextContent(type="text", text=f"Ошибка: {e}")]
            return [TextContent(type="text", text=f"Архив создан: {dest}")]

        if name == "extract_archive":
            p = _safe(arguments["path"])
            dest = _safe(arguments["dest"])
            if not p.exists():
                return [TextContent(type="text", text="Архив не найден")]
            dest.mkdir(parents=True, exist_ok=True)
            try:
                if zipfile.is_zipfile(p):
                    with zipfile.ZipFile(p) as z:
                        # Безопасная распаковка — защита от path traversal
                        for info in z.infolist():
                            name = info.filename
                            if name.startswith("/") or ".." in name.split("/"):
                                continue
                            z.extract(info, dest)
                elif tarfile.is_tarfile(p):
                    with tarfile.open(p) as t:
                        for m in t.getmembers():
                            if m.name.startswith("/") or ".." in m.name.split("/"):
                                continue
                            t.extract(m, dest)
                else:
                    return [TextContent(type="text", text="Формат не поддерживается")]
            except Exception as e:
                return [TextContent(type="text", text=f"Ошибка: {e}")]
            return [TextContent(type="text", text=f"Распакован: {dest}")]

        return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]
    except Exception as e:
        logger.exception("Tool failed: %s", name)
        return [TextContent(type="text", text=f"Ошибка: {e}")]


def _is_skipped(p: Path) -> bool:
    return any(part in IGNORE_DIRS for part in p.parts) or p.name.startswith(".")


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
