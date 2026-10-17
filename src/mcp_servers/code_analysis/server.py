"""MCP-сервер: анализ кода и рефакторинг."""
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
logger = logging.getLogger("code-analysis-mcp")

IGNORE_DIRS = {
    ".git", ".venv", "venv", "__pycache__", "node_modules",
    ".idea", ".vscode", "dist", "build", "target", ".next",
}

CODE_EXTS = {".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"}


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


def _iter_code_files(root: Path):
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in IGNORE_DIRS for part in p.parts):
            continue
        if p.suffix.lower() in CODE_EXTS:
            yield p


app = Server("code_analysis")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        # Символы
        Tool(name="find_symbol",
             description="Найти определение символа по имени.",
             inputSchema={"type": "object", "properties": {
                 "name": {"type": "string"},
                 "kind": {"type": "string"},
                 "path": {"type": "string", "default": "."}},
                 "required": ["name"]}),
        Tool(name="find_references",
             description="Все места, где используется символ (текстовый поиск).",
             inputSchema={"type": "object", "properties": {
                 "name": {"type": "string"},
                 "path": {"type": "string", "default": "."},
                 "max_results": {"type": "integer", "default": 200}},
                 "required": ["name"]}),
        Tool(name="list_symbols_in_file",
             description="Список символов в файле.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"}},
                 "required": ["path"]}),
        Tool(name="search_symbols",
             description="Поиск символов по подстроке имени.",
             inputSchema={"type": "object", "properties": {
                 "query": {"type": "string"},
                 "kind": {"type": "string"},
                 "limit": {"type": "integer", "default": 100}},
                 "required": ["query"]}),
        Tool(name="get_symbol_info",
             description="Подробная информация о символе.",
             inputSchema={"type": "object", "properties": {
                 "name": {"type": "string"},
                 "file": {"type": "string"}},
                 "required": ["name"]}),
        Tool(name="call_hierarchy",
             description="Кто вызывает и кого вызывает функция.",
             inputSchema={"type": "object", "properties": {
                 "name": {"type": "string"},
                 "direction": {"type": "string", "default": "both"}},
                 "required": ["name"]}),
        Tool(name="class_hierarchy",
             description="Иерархия классов по имени.",
             inputSchema={"type": "object", "properties": {
                 "name": {"type": "string"}},
                 "required": ["name"]}),

        # Рефакторинг
        Tool(name="rename_symbol",
             description="Переименовать символ во всех файлах.",
             inputSchema={"type": "object", "properties": {
                 "old_name": {"type": "string"},
                 "new_name": {"type": "string"},
                 "path": {"type": "string", "default": "."},
                 "dry_run": {"type": "boolean", "default": True}},
                 "required": ["old_name", "new_name"]}),
        Tool(name="extract_function",
             description="Выделить диапазон строк в функцию.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"},
                 "start_line": {"type": "integer"},
                 "end_line": {"type": "integer"},
                 "new_name": {"type": "string"}},
                 "required": ["path", "start_line", "end_line", "new_name"]}),
        Tool(name="extract_variable",
             description="Выделить выражение в переменную.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"},
                 "line": {"type": "integer"},
                 "expression": {"type": "string"},
                 "var_name": {"type": "string"}},
                 "required": ["path", "line", "expression", "var_name"]}),
        Tool(name="inline_variable",
             description="Инлайн переменной (заменить использования значением).",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"},
                 "var_name": {"type": "string"},
                 "dry_run": {"type": "boolean", "default": True}},
                 "required": ["path", "var_name"]}),
        Tool(name="organize_imports",
             description="Упорядочить импорты (сортировка, группировка).",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"}},
                 "required": ["path"]}),
        Tool(name="remove_unused_imports",
             description="Удалить неиспользуемые импорты.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"}},
                 "required": ["path"]}),
        Tool(name="add_docstring",
             description="Добавить заглушку docstring в функцию.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"},
                 "function_name": {"type": "string"},
                 "text": {"type": "string"}},
                 "required": ["path", "function_name"]}),

        # Анализ
        Tool(name="find_unused",
             description="Найти неиспользуемые символы.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string", "default": "."},
                 "kind": {"type": "string", "default": "function"}}}),
        Tool(name="find_dead_code",
             description="Найти мёртвый код (функции/классы без вызовов).",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string", "default": "."},
                 "limit": {"type": "integer", "default": 100}}}),
        Tool(name="complexity_report",
             description="Сложность функций (cyclomatic).",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string", "default": "."},
                 "threshold": {"type": "integer", "default": 10}}}),
        Tool(name="dependency_graph",
             description="Граф зависимостей между файлами (по импортам).",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string", "default": "."}}}),
        Tool(name="find_duplicates",
             description="Найти дублирующийся код (похожие функции).",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string", "default": "."},
                 "min_lines": {"type": "integer", "default": 10}}}),
        Tool(name="find_todos",
             description="Найти TODO/FIXME/HACK в коде.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string", "default": "."},
                 "tags": {"type": "array", "items": {"type": "string"}}}}),

        # Навигация
        Tool(name="locate_import",
             description="Где определён модуль/пакет.",
             inputSchema={"type": "object", "properties": {
                 "module": {"type": "string"}},
                 "required": ["module"]}),
        Tool(name="resolve_import",
             description="Разобрать import path в файл проекта.",
             inputSchema={"type": "object", "properties": {
                 "import_path": {"type": "string"},
                 "from_file": {"type": "string"}},
                 "required": ["import_path"]}),
        Tool(name="file_outline",
             description="Структура файла — классы, функции, методы.",
             inputSchema={"type": "object", "properties": {
                 "path": {"type": "string"}},
                 "required": ["path"]}),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        from src.mcp_servers.code_analysis.symbols import extract_symbols, Symbol

        root = _get_root()

        # ─── Символы ────────────────────────────────
        if name == "find_symbol":
            target = arguments["name"]
            kind_filter = arguments.get("kind")
            base = _safe(arguments.get("path", "."))
            found: list[dict] = []
            for p in _iter_code_files(base):
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                rel = str(p.relative_to(root)).replace("\\", "/")
                syms = extract_symbols(p, text, rel)
                for s in syms:
                    if s.name == target:
                        if kind_filter and s.kind != kind_filter:
                            continue
                        found.append({
                            "name": s.name, "kind": s.kind,
                            "file": s.file, "line": s.line,
                            "signature": s.signature,
                        })
                        if len(found) >= 50:
                            break
                if len(found) >= 50:
                    break
            return [TextContent(
                type="text",
                text=json.dumps(found, ensure_ascii=False, indent=2),
            )]

        if name == "find_references":
            target = arguments["name"]
            base = _safe(arguments.get("path", "."))
            max_results = arguments.get("max_results", 200)
            hits: list[str] = []
            # Простой regex: слово boundaries
            regex = re.compile(rf"\b{re.escape(target)}\b")
            for p in _iter_code_files(base):
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                rel = str(p.relative_to(root)).replace("\\", "/")
                for i, line in enumerate(text.splitlines(), 1):
                    if regex.search(line):
                        hits.append(f"{rel}:{i}: {line.strip()[:200]}")
                        if len(hits) >= max_results:
                            break
                if len(hits) >= max_results:
                    break
            return [TextContent(
                type="text",
                text="\n".join(hits) or "Совпадений нет",
            )]

        if name == "list_symbols_in_file":
            p = _safe(arguments["path"])
            if not p.exists():
                return [TextContent(type="text", text="Файл не найден")]
            text = p.read_text(encoding="utf-8", errors="replace")
            rel = str(p.relative_to(root)).replace("\\", "/")
            syms = extract_symbols(p, text, rel)
            return [TextContent(
                type="text",
                text=json.dumps([
                    {
                        "name": s.name, "kind": s.kind,
                        "line": s.line, "end_line": s.end_line,
                        "parent": s.parent, "signature": s.signature[:120],
                        "docstring": s.docstring[:100],
                    }
                    for s in syms
                ], ensure_ascii=False, indent=2),
            )]

        if name == "search_symbols":
            query = arguments["query"].lower()
            kind_filter = arguments.get("kind")
            limit = arguments.get("limit", 100)
            found: list[dict] = []
            for p in _iter_code_files(root):
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                rel = str(p.relative_to(root)).replace("\\", "/")
                syms = extract_symbols(p, text, rel)
                for s in syms:
                    if kind_filter and s.kind != kind_filter:
                        continue
                    if query in s.name.lower():
                        found.append({
                            "name": s.name, "kind": s.kind,
                            "file": s.file, "line": s.line,
                        })
                        if len(found) >= limit:
                            break
                if len(found) >= limit:
                    break
            return [TextContent(
                type="text",
                text=json.dumps(found, ensure_ascii=False, indent=2),
            )]

        if name == "get_symbol_info":
            target = arguments["name"]
            file_filter = arguments.get("file")
            for p in _iter_code_files(root):
                rel = str(p.relative_to(root)).replace("\\", "/")
                if file_filter and file_filter not in rel:
                    continue
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                syms = extract_symbols(p, text, rel)
                for s in syms:
                    if s.name == target:
                        return [TextContent(
                            type="text",
                            text=json.dumps({
                                "name": s.name, "kind": s.kind,
                                "file": s.file, "line": s.line,
                                "end_line": s.end_line,
                                "parent": s.parent,
                                "signature": s.signature,
                                "docstring": s.docstring,
                            }, ensure_ascii=False, indent=2),
                        )]
            return [TextContent(type="text", text="Не найден")]

        if name == "call_hierarchy":
            # Через греп find_references
            target = arguments["name"]
            direction = arguments.get("direction", "both")
            regex = re.compile(rf"\b{re.escape(target)}\s*\(")

            callers: list[str] = []
            callees: set[str] = set()
            found_def_file = None

            for p in _iter_code_files(root):
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                rel = str(p.relative_to(root)).replace("\\", "/")
                for i, line in enumerate(text.splitlines(), 1):
                    if regex.search(line) and f"def {target}" not in line \
                            and f"function {target}" not in line:
                        callers.append(f"{rel}:{i}: {line.strip()[:120]}")

                syms = extract_symbols(p, text, rel)
                for s in syms:
                    if s.name == target and not found_def_file:
                        found_def_file = (p, s)
            if found_def_file and direction in ("down", "both"):
                p, s = found_def_file
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                    lines = text.splitlines()
                    body = "\n".join(lines[s.line - 1:s.end_line])
                    # Все вызовы в теле
                    for m in re.finditer(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\s*\(", body):
                        fn = m.group(1)
                        if fn not in ("if", "for", "while", "return",
                                      "print", "len", "str", "int"):
                            callees.add(fn)
                except Exception:
                    pass

            return [TextContent(
                type="text",
                text=json.dumps({
                    "symbol": target,
                    "callers": callers[:50],
                    "callees": sorted(callees)[:50],
                }, ensure_ascii=False, indent=2),
            )]

        if name == "class_hierarchy":
            target = arguments["name"]
            regex_base = re.compile(rf"class\s+{re.escape(target)}\s*\(([^)]*)\)")
            regex_child = re.compile(rf"class\s+\w+\s*\([^)]*\b{re.escape(target)}\b[^)]*\)")
            bases: list[str] = []
            children: list[str] = []
            for p in _iter_code_files(root):
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                rel = str(p.relative_to(root)).replace("\\", "/")
                for m in regex_base.finditer(text):
                    bases.extend(b.strip() for b in m.group(1).split(",") if b.strip())
                for m in regex_child.finditer(text):
                    child = re.match(r"class\s+(\w+)", m.group(0))
                    if child and child.group(1) != target:
                        children.append(f"{child.group(1)} @ {rel}")
            return [TextContent(
                type="text",
                text=json.dumps({
                    "class": target,
                    "bases": bases,
                    "children": children,
                }, ensure_ascii=False, indent=2),
            )]

        # ─── Рефакторинг ────────────────────────────
        if name == "rename_symbol":
            old = arguments["old_name"]
            new = arguments["new_name"]
            base = _safe(arguments.get("path", "."))
            dry = arguments.get("dry_run", True)
            regex = re.compile(rf"\b{re.escape(old)}\b")

            changes: list[dict] = []
            total = 0
            for p in _iter_code_files(base):
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                count = len(regex.findall(text))
                if count == 0:
                    continue
                rel = str(p.relative_to(root)).replace("\\", "/")
                changes.append({"file": rel, "count": count})
                total += count
                if not dry:
                    new_text = regex.sub(new, text)
                    p.write_text(new_text, encoding="utf-8")

            return [TextContent(
                type="text",
                text=json.dumps({
                    "dry_run": dry,
                    "old_name": old,
                    "new_name": new,
                    "total_replacements": total,
                    "files": changes,
                }, ensure_ascii=False, indent=2),
            )]

        if name == "extract_function":
            # Простая реализация: выделяет строки и оборачивает в def
            p = _safe(arguments["path"])
            if not p.exists():
                return [TextContent(type="text", text="Файл не найден")]
            start = arguments["start_line"]
            end = arguments["end_line"]
            new_name = arguments["new_name"]

            lines = p.read_text(encoding="utf-8").splitlines()
            if start < 1 or end > len(lines) or start > end:
                return [TextContent(type="text", text="Некорректные номера строк")]

            extracted = lines[start - 1:end]
            indent = len(extracted[0]) - len(extracted[0].lstrip())
            indent_str = " " * indent

            # Определяем вызываемые имена для параметров
            body_text = "\n".join(extracted)
            identifiers = set(re.findall(r"\b([a-zA-Z_][a-zA-Z0-9_]*)\b", body_text))
            keywords = {"if", "else", "for", "while", "return", "print",
                        "len", "str", "int", "float", "list", "dict",
                        "set", "tuple", "bool", "range", "in", "not",
                        "and", "or", "is", "None", "True", "False"}
            candidates = sorted(identifiers - keywords)

            # Упрощённо: используем все идентификаторы как параметры
            params = ", ".join(candidates[:5])

            func_lines = [f"{indent_str}def {new_name}({params}):"]
            for line in extracted:
                func_lines.append("    " + line)

            new_lines = (
                lines[:start - 1]
                + [f"{indent_str}# TODO: extract function {new_name} with args ({params})"]
                + func_lines
                + lines[end:]
            )
            p.write_text("\n".join(new_lines), encoding="utf-8")

            return [TextContent(
                type="text",
                text=f"Выделено в '{new_name}'. Проверьте аргументы вручную.",
            )]

        if name == "extract_variable":
            p = _safe(arguments["path"])
            if not p.exists():
                return [TextContent(type="text", text="Файл не найден")]
            line_num = arguments["line"]
            expr = arguments["expression"]
            var_name = arguments["var_name"]

            lines = p.read_text(encoding="utf-8").splitlines()
            if line_num < 1 or line_num > len(lines):
                return [TextContent(type="text", text="Некорректная строка")]

            line = lines[line_num - 1]
            if expr not in line:
                return [TextContent(type="text", text="Выражение не найдено в строке")]

            indent = " " * (len(line) - len(line.lstrip()))
            new_line = line.replace(expr, var_name)
            declaration = f"{indent}{var_name} = {expr}"

            lines[line_num - 1] = new_line
            lines.insert(line_num - 1, declaration)
            p.write_text("\n".join(lines), encoding="utf-8")

            return [TextContent(
                type="text", text=f"Введена переменная {var_name}",
            )]

        if name == "inline_variable":
            p = _safe(arguments["path"])
            if not p.exists():
                return [TextContent(type="text", text="Файл не найден")]
            var_name = arguments["var_name"]
            dry = arguments.get("dry_run", True)

            text = p.read_text(encoding="utf-8")
            # Ищем присваивание
            m = re.search(
                rf"^\s*{re.escape(var_name)}\s*=\s*(.+?)\s*$",
                text, re.MULTILINE,
            )
            if not m:
                return [TextContent(type="text", text="Присваивание не найдено")]
            value = m.group(1)

            uses = len(re.findall(rf"\b{re.escape(var_name)}\b", text)) - 1
            if not dry:
                # Удаляем строку с присваиванием
                text = re.sub(
                    rf"^\s*{re.escape(var_name)}\s*=\s*.+?\s*\n",
                    "",
                    text, count=1, flags=re.MULTILINE,
                )
                # Заменяем использования
                text = re.sub(
                    rf"\b{re.escape(var_name)}\b",
                    f"({value})",
                    text,
                )
                p.write_text(text, encoding="utf-8")

            return [TextContent(
                type="text",
                text=json.dumps({
                    "dry_run": dry,
                    "variable": var_name,
                    "value": value[:100],
                    "usages_replaced": uses,
                }, ensure_ascii=False, indent=2),
            )]

        if name == "organize_imports":
            p = _safe(arguments["path"])
            if not p.exists():
                return [TextContent(type="text", text="Файл не найден")]

            text = p.read_text(encoding="utf-8")
            lines = text.splitlines()

            # Собираем блок импортов сверху
            import_lines: list[str] = []
            import_end = 0
            for i, line in enumerate(lines[:100]):
                stripped = line.strip()
                if stripped.startswith(("import ", "from ")):
                    import_lines.append(stripped)
                    import_end = i + 1
                elif stripped.startswith("#") or not stripped:
                    if import_end > 0:
                        import_end = i + 1
                    continue
                else:
                    break

            if not import_lines:
                return [TextContent(type="text", text="Импорты не найдены")]

            # Сортируем: сначала import, потом from
            stdlib: list[str] = []
            third_party: list[str] = []
            local: list[str] = []

            stdlib_modules = {
                "os", "sys", "re", "json", "time", "pathlib",
                "typing", "dataclasses", "asyncio", "logging",
                "collections", "itertools", "functools", "math",
            }
            for imp in import_lines:
                if imp.startswith("import "):
                    mod = imp[7:].split(".")[0].strip()
                else:
                    mod = imp[5:].split(".")[0].strip()
                if mod in stdlib_modules:
                    stdlib.append(imp)
                elif mod.startswith("src.") or mod.startswith("."):
                    local.append(imp)
                else:
                    third_party.append(imp)

            organized = (
                sorted(set(stdlib))
                + [""]
                + sorted(set(third_party))
                + [""]
                + sorted(set(local))
            )
            # Убираем пустые подряд
            cleaned: list[str] = []
            for line in organized:
                if not line and (not cleaned or not cleaned[-1]):
                    continue
                cleaned.append(line)

            new_lines = cleaned + [""] + lines[import_end:]
            p.write_text("\n".join(new_lines), encoding="utf-8")

            return [TextContent(
                type="text",
                text=f"Импорты упорядочены ({len(import_lines)} → {len(cleaned)})",
            )]

        if name == "remove_unused_imports":
            p = _safe(arguments["path"])
            if not p.exists():
                return [TextContent(type="text", text="Файл не найден")]

            text = p.read_text(encoding="utf-8")
            lines = text.splitlines()

            unused: list[int] = []
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped.startswith("import "):
                    mod = stripped[7:].split(" as ")[-1].split(".")[0].strip()
                elif stripped.startswith("from ") and " import " in stripped:
                    names = stripped.split(" import ", 1)[1]
                    for name in names.split(","):
                        name = name.strip().split(" as ")[-1].strip()
                        if not name or name == "*":
                            continue
                        # Проверяем, встречается ли ещё где-то
                        pattern = re.compile(rf"\b{re.escape(name)}\b")
                        count = len(pattern.findall(text))
                        if count <= 1:
                            unused.append(i)
                else:
                    continue
                if stripped.startswith("import "):
                    if not re.search(rf"\b{re.escape(mod)}\b", text[text.find(stripped) + len(stripped):]):
                        unused.append(i)

            if not unused:
                return [TextContent(type="text", text="Неиспользуемых импортов нет")]

            new_lines = [l for i, l in enumerate(lines) if i not in set(unused)]
            p.write_text("\n".join(new_lines), encoding="utf-8")

            return [TextContent(
                type="text", text=f"Удалено импортов: {len(unused)}",
            )]

        if name == "add_docstring":
            p = _safe(arguments["path"])
            if not p.exists():
                return [TextContent(type="text", text="Файл не найден")]
            fn_name = arguments["function_name"]
            text_content = arguments.get("text", f'"""{fn_name} — TODO."""')

            source = p.read_text(encoding="utf-8")
            # Ищем def fn_name(...):
            pattern = re.compile(
                rf"^(\s*)def\s+{re.escape(fn_name)}\s*\([^)]*\)[^:]*:\s*$",
                re.MULTILINE,
            )
            m = pattern.search(source)
            if not m:
                return [TextContent(type="text", text="Функция не найдена")]

            indent = m.group(1) + "    "
            insert_pos = m.end()
            new_source = (
                source[:insert_pos]
                + "\n"
                + indent
                + text_content
                + source[insert_pos:]
            )
            p.write_text(new_source, encoding="utf-8")
            return [TextContent(type="text", text=f"Docstring добавлен в {fn_name}")]

        # ─── Анализ ─────────────────────────────────
        if name == "find_unused":
            base = _safe(arguments.get("path", "."))
            kind_filter = arguments.get("kind", "function")

            # Собираем все символы
            all_symbols: list[dict] = []
            for p in _iter_code_files(base):
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                rel = str(p.relative_to(root)).replace("\\", "/")
                syms = extract_symbols(p, text, rel)
                for s in syms:
                    if s.kind == kind_filter:
                        all_symbols.append({"name": s.name, "file": rel, "line": s.line})

            # Проверяем использования
            unused: list[dict] = []
            all_text = ""
            for p in _iter_code_files(base):
                try:
                    all_text += p.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    pass

            for sym in all_symbols:
                name = sym["name"]
                if name.startswith("_"):
                    continue
                # Ищем использования вне def
                uses = 0
                for m in re.finditer(rf"\b{re.escape(name)}\b", all_text):
                    before = all_text[max(0, m.start() - 20):m.start()]
                    if not re.search(rf"def\s+$", before):
                        uses += 1
                if uses <= 1:
                    unused.append(sym)

            return [TextContent(
                type="text",
                text=json.dumps({
                    "total_symbols": len(all_symbols),
                    "unused_count": len(unused),
                    "unused": unused[:100],
                }, ensure_ascii=False, indent=2),
            )]

        if name == "find_dead_code":
            base = _safe(arguments.get("path", "."))
            limit = arguments.get("limit", 100)

            # Простой анализ: функции без вызовов в проекте
            functions: dict[str, dict] = {}
            for p in _iter_code_files(base):
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                rel = str(p.relative_to(root)).replace("\\", "/")
                syms = extract_symbols(p, text, rel)
                for s in syms:
                    if s.kind == "function" and not s.name.startswith("_"):
                        functions[s.name] = {"file": rel, "line": s.line}

            all_text = ""
            for p in _iter_code_files(base):
                try:
                    all_text += p.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    pass

            dead: list[dict] = []
            for fn_name, info in functions.items():
                # Ищем вызовы (без def)
                pattern = re.compile(rf"\b{re.escape(fn_name)}\s*\(")
                calls = 0
                for m in pattern.finditer(all_text):
                    before = all_text[max(0, m.start() - 10):m.start()]
                    if not re.search(r"def\s+$", before):
                        calls += 1
                if calls == 0:
                    dead.append({**info, "name": fn_name})
                    if len(dead) >= limit:
                        break

            return [TextContent(
                type="text",
                text=json.dumps({
                    "total_functions": len(functions),
                    "dead_code_candidates": dead,
                }, ensure_ascii=False, indent=2),
            )]

        if name == "complexity_report":
            base = _safe(arguments.get("path", "."))
            threshold = arguments.get("threshold", 10)
            results: list[dict] = []
            for p in _iter_code_files(base):
                if p.suffix.lower() != ".py":
                    continue
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                rel = str(p.relative_to(root)).replace("\\", "/")
                # Простая метрика: количество if/for/while/and/or
                syms = extract_symbols(p, text, rel)
                for s in syms:
                    if s.kind not in ("function", "method"):
                        continue
                    lines = text.splitlines()[s.line - 1:s.end_line]
                    body = "\n".join(lines)
                    complexity = 1
                    for kw in ("if ", "elif ", "for ", "while ", " and ", " or ", "except "):
                        complexity += body.count(kw)
                    if complexity >= threshold:
                        results.append({
                            "name": s.name, "file": rel, "line": s.line,
                            "complexity": complexity,
                        })
            results.sort(key=lambda x: -x["complexity"])
            return [TextContent(
                type="text",
                text=json.dumps({
                    "threshold": threshold,
                    "results": results[:100],
                }, ensure_ascii=False, indent=2),
            )]

        if name == "dependency_graph":
            base = _safe(arguments.get("path", "."))
            graph: dict[str, list[str]] = {}
            for p in _iter_code_files(base):
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                rel = str(p.relative_to(root)).replace("\\", "/")
                imports: list[str] = []
                for m in re.finditer(r"^\s*(?:import\s+(\S+)|from\s+(\S+)\s+import)", text, re.MULTILINE):
                    mod = m.group(1) or m.group(2)
                    if mod:
                        imports.append(mod.split(".")[0])
                graph[rel] = sorted(set(imports))
            return [TextContent(
                type="text",
                text=json.dumps(graph, ensure_ascii=False, indent=2)[:30000],
            )]

        if name == "find_duplicates":
            base = _safe(arguments.get("path", "."))
            min_lines = arguments.get("min_lines", 10)

            # Простой hash-based детектор дубликатов блоков
            from collections import defaultdict
            blocks: dict[str, list[tuple[str, int]]] = defaultdict(list)

            for p in _iter_code_files(base):
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                rel = str(p.relative_to(root)).replace("\\", "/")
                lines = text.splitlines()
                for i in range(len(lines) - min_lines):
                    block = "\n".join(lines[i:i + min_lines])
                    # Нормализуем отступы
                    normalized = "\n".join(l.strip() for l in block.splitlines())
                    h = str(hash(normalized))
                    blocks[h].append((rel, i + 1))

            dups = [
                {"hash": h, "occurrences": occ[:5]}
                for h, occ in blocks.items() if len(occ) > 1
            ]
            return [TextContent(
                type="text",
                text=json.dumps({
                    "duplicates_found": len(dups),
                    "samples": dups[:50],
                }, ensure_ascii=False, indent=2),
            )]

        if name == "find_todos":
            base = _safe(arguments.get("path", "."))
            tags = arguments.get("tags") or ["TODO", "FIXME", "HACK", "XXX", "NOTE"]
            pattern = re.compile(rf"({'|'.join(tags)})\b[:\s]+(.*)", re.IGNORECASE)

            hits: list[dict] = []
            for p in _iter_code_files(base):
                try:
                    text = p.read_text(encoding="utf-8", errors="replace")
                except Exception:
                    continue
                rel = str(p.relative_to(root)).replace("\\", "/")
                for i, line in enumerate(text.splitlines(), 1):
                    m = pattern.search(line)
                    if m:
                        hits.append({
                            "tag": m.group(1).upper(),
                            "text": m.group(2).strip()[:200],
                            "file": rel, "line": i,
                        })

            by_tag: dict[str, int] = {}
            for h in hits:
                by_tag[h["tag"]] = by_tag.get(h["tag"], 0) + 1

            return [TextContent(
                type="text",
                text=json.dumps({
                    "total": len(hits),
                    "by_tag": by_tag,
                    "items": hits[:200],
                }, ensure_ascii=False, indent=2),
            )]

        # ─── Навигация ──────────────────────────────
        if name == "locate_import":
            mod = arguments["module"]
            # Ищем пакет в venv
            try:
                import importlib.util
                spec = importlib.util.find_spec(mod)
                if spec and spec.origin:
                    return [TextContent(type="text", text=spec.origin)]
            except Exception:
                pass
            return [TextContent(type="text", text="Не найден")]

        if name == "resolve_import":
            # Простое разрешение: если начинается с . — относительно файла
            imp = arguments["import_path"]
            from_file = arguments.get("from_file", "")
            if not imp.startswith("."):
                return [TextContent(type="text", text=f"Абсолютный импорт: {imp}")]
            if not from_file:
                return [TextContent(type="text", text="Нужен from_file для относительного импорта")]
            p = _safe(from_file)
            base = p.parent
            levels = len(imp) - len(imp.lstrip("."))
            mod_path = imp[levels:]
            for _ in range(levels - 1):
                base = base.parent
            target = base / mod_path.replace(".", "/")
            candidates = [target.with_suffix(".py"), target / "__init__.py"]
            for c in candidates:
                if c.exists():
                    try:
                        return [TextContent(
                            type="text",
                            text=str(c.relative_to(root)),
                        )]
                    except ValueError:
                        return [TextContent(type="text", text=str(c))]
            return [TextContent(type="text", text="Не разрешено")]

        if name == "file_outline":
            p = _safe(arguments["path"])
            if not p.exists():
                return [TextContent(type="text", text="Файл не найден")]
            text = p.read_text(encoding="utf-8", errors="replace")
            rel = str(p.relative_to(root)).replace("\\", "/")
            syms = extract_symbols(p, text, rel)

            lines = [f"{rel}:"]
            for s in sorted(syms, key=lambda x: x.line):
                indent = "    " if s.parent else "  "
                parent_info = f" [{s.parent}]" if s.parent else ""
                lines.append(f"{indent}{s.kind:8s} {s.name}{parent_info}  (line {s.line})")
            return [TextContent(type="text", text="\n".join(lines))]

        return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]
    except Exception as e:
        logger.exception("Code analysis tool %s failed", name)
        return [TextContent(type="text", text=f"Ошибка: {e}")]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
