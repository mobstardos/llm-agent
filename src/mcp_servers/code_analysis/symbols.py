"""Извлечение символов через tree-sitter (универсально для языков)."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class Symbol:
    name: str
    kind: str           # function | class | method | variable | import | interface
    file: str
    line: int = 0
    col: int = 0
    end_line: int = 0
    signature: str = ""
    docstring: str = ""
    parent: str = ""    # для методов — имя класса


def _load_parser(ext: str):
    """Возвращает parser для расширения или None."""
    try:
        from tree_sitter import Language, Parser
    except ImportError:
        return None

    parsers = {
        ".py": ("tree_sitter_python", "language"),
        ".js": ("tree_sitter_javascript", "language"),
        ".jsx": ("tree_sitter_javascript", "language"),
        ".mjs": ("tree_sitter_javascript", "language"),
        ".cjs": ("tree_sitter_javascript", "language"),
        ".ts": ("tree_sitter_typescript", "language_typescript"),
        ".tsx": ("tree_sitter_typescript", "language_tsx"),
    }

    if ext not in parsers:
        return None

    module_name, func_name = parsers[ext]
    try:
        mod = __import__(module_name, fromlist=[func_name])
        lang_func = getattr(mod, func_name)
        lang = Language(lang_func())
        return Parser(lang)
    except Exception as e:
        logger.debug("Parser load fail %s: %s", ext, e)
        return None


# Символы по типам узлов tree-sitter
NODE_KINDS = {
    "python": {
        "function_definition": "function",
        "async_function_definition": "function",
        "class_definition": "class",
        "decorated_definition": None,  # обёртка
    },
    "javascript": {
        "function_declaration": "function",
        "generator_function_declaration": "function",
        "class_declaration": "class",
        "method_definition": "method",
        "lexical_declaration": "variable",
    },
    "typescript": {
        "function_declaration": "function",
        "class_declaration": "class",
        "abstract_class_declaration": "class",
        "interface_declaration": "interface",
        "method_definition": "method",
        "lexical_declaration": "variable",
        "type_alias_declaration": "type",
        "enum_declaration": "enum",
    },
}


def detect_language(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".py":
        return "python"
    if ext in (".js", ".jsx", ".mjs", ".cjs"):
        return "javascript"
    if ext in (".ts", ".tsx"):
        return "typescript"
    return ""


def extract_symbols(path: Path, source: str, rel_path: str = "") -> list[Symbol]:
    """Извлекает символы из файла."""
    lang = detect_language(path)
    if not lang:
        return []

    parser = _load_parser(path.suffix.lower())
    if parser is None:
        return []

    kinds_map = NODE_KINDS.get(lang, {})

    try:
        tree = parser.parse(source.encode("utf-8"))
    except Exception as e:
        logger.debug("Parse fail %s: %s", path, e)
        return []

    symbols: list[Symbol] = []
    rel = rel_path or str(path)

    def _walk(node, parent_class: str = ""):
        t = node.type

        # decorated_definition — обёртка, идём внутрь
        if t == "decorated_definition":
            for child in node.children:
                _walk(child, parent_class)
            return

        if t in kinds_map and kinds_map[t] is not None:
            kind = kinds_map[t]
            name_node = node.child_by_field_name("name")

            if name_node is None and t in ("lexical_declaration", "variable_declaration"):
                # const foo = ...
                for child in node.children:
                    if child.type == "variable_declarator":
                        nm = child.child_by_field_name("name")
                        if nm:
                            name = source[nm.start_byte:nm.end_byte]
                            sig = source[node.start_byte:min(node.end_byte, node.start_byte + 100)]
                            symbols.append(Symbol(
                                name=name, kind="variable",
                                file=rel, line=node.start_point[0] + 1,
                                col=node.start_point[1] + 1,
                                end_line=node.end_point[0] + 1,
                                signature=sig.split("\n")[0][:120],
                                parent=parent_class,
                            ))
                return

            if name_node:
                name = source[name_node.start_byte:name_node.end_byte]
                # Signature — первая строка
                first_line_end = source.find("\n", node.start_byte)
                if first_line_end == -1:
                    first_line_end = node.end_byte
                signature = source[node.start_byte:first_line_end][:200]

                # Docstring (только для Python)
                docstring = ""
                if lang == "python":
                    docstring = _extract_python_docstring(node, source)

                new_parent = name if kind == "class" else parent_class

                symbols.append(Symbol(
                    name=name, kind=kind,
                    file=rel, line=node.start_point[0] + 1,
                    col=node.start_point[1] + 1,
                    end_line=node.end_point[0] + 1,
                    signature=signature,
                    docstring=docstring,
                    parent=parent_class,
                ))

                # Для классов — рекурсивно ищем методы
                if kind == "class":
                    body = node.child_by_field_name("body")
                    if body:
                        for child in body.children:
                            _walk(child, new_parent)
                    return

        for child in node.children:
            _walk(child, parent_class)

    _walk(tree.root_node)
    return symbols


def _extract_python_docstring(node, source: str) -> str:
    """Извлекает docstring функции/класса."""
    body = node.child_by_field_name("body")
    if body is None:
        return ""
    for child in body.children:
        if child.type == "expression_statement":
            for c2 in child.children:
                if c2.type == "string":
                    text = source[c2.start_byte:c2.end_byte]
                    return _strip_string_quotes(text)[:500]
            break
        if child.type != "comment":
            break
    return ""


def _strip_string_quotes(s: str) -> str:
    s = s.strip()
    for prefix in ('"""', "'''", '"', "'", "r", "f", "b", "u"):
        if s.startswith(prefix):
            s = s[len(prefix):]
            break
    for suffix in ('"""', "'''", '"', "'"):
        if s.endswith(suffix):
            s = s[:-len(suffix)]
            break
    return s.strip()
