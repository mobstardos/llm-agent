"""TypeScript парсер (расширение JavaScript)."""
from __future__ import annotations

import logging
from pathlib import Path

from src.memory.graph_parsers.base import ParsedEdge, ParsedNode

logger = logging.getLogger(__name__)


class TypeScriptParser:
    id = "typescript"
    extensions = [".ts", ".tsx"]

    def __init__(self):
        self._parser_ts = None
        self._parser_tsx = None

    def _load(self, tsx: bool = False):
        from tree_sitter import Language, Parser
        import tree_sitter_typescript as tsts
        if tsx:
            if self._parser_tsx is None:
                self._parser_tsx = Parser(
                    Language(tsts.language_tsx())
                )
            return self._parser_tsx
        if self._parser_ts is None:
            self._parser_ts = Parser(Language(tsts.language_typescript()))
        return self._parser_ts

    def parse(
        self, path: Path, source: str,
    ) -> tuple[list[ParsedNode], list[ParsedEdge]]:
        is_tsx = path.suffix.lower() == ".tsx"
        parser = self._load(is_tsx)
        rel = str(path).replace("\\", "/")
        nodes = [ParsedNode(id=f"file:{rel}", kind="file",
                            name=rel, file=rel)]
        edges: list[ParsedEdge] = []

        try:
            tree = parser.parse(source.encode("utf-8"))
        except Exception as e:
            logger.warning("Parse failed %s: %s", path, e)
            return nodes, edges

        self._walk(tree.root_node, source, rel, nodes, edges)
        return nodes, edges

    def _walk(self, node, source, rel, nodes, edges):
        t = node.type

        if t == "import_statement":
            src_node = node.child_by_field_name("source")
            if src_node:
                mod = source[src_node.start_byte:src_node.end_byte].strip("\"'")
                edges.append(ParsedEdge(
                    src=f"file:{rel}", dst=f"module:{mod}", kind="imports",
                ))

        elif t == "function_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                name = source[name_node.start_byte:name_node.end_byte]
                fid = f"func:{rel}::{name}"
                nodes.append(ParsedNode(
                    id=fid, kind="function", name=name, file=rel,
                    line=node.start_point[0] + 1,
                ))
                edges.append(ParsedEdge(
                    src=f"file:{rel}", dst=fid, kind="defines",
                ))

        elif t in ("class_declaration", "abstract_class_declaration"):
            name_node = node.child_by_field_name("name")
            if name_node:
                name = source[name_node.start_byte:name_node.end_byte]
                cid = f"class:{rel}::{name}"
                nodes.append(ParsedNode(
                    id=cid, kind="class", name=name, file=rel,
                    line=node.start_point[0] + 1,
                ))
                edges.append(ParsedEdge(
                    src=f"file:{rel}", dst=cid, kind="defines",
                ))

        elif t == "interface_declaration":
            name_node = node.child_by_field_name("name")
            if name_node:
                name = source[name_node.start_byte:name_node.end_byte]
                iid = f"interface:{rel}::{name}"
                nodes.append(ParsedNode(
                    id=iid, kind="interface", name=name, file=rel,
                    line=node.start_point[0] + 1,
                ))
                edges.append(ParsedEdge(
                    src=f"file:{rel}", dst=iid, kind="defines",
                ))

        elif t == "lexical_declaration":
            for child in node.children:
                if child.type == "variable_declarator":
                    name_node = child.child_by_field_name("name")
                    value_node = child.child_by_field_name("value")
                    if name_node and value_node and value_node.type in (
                        "arrow_function", "function_expression",
                        "function",
                    ):
                        name = source[name_node.start_byte:name_node.end_byte]
                        fid = f"func:{rel}::{name}"
                        nodes.append(ParsedNode(
                            id=fid, kind="function", name=name, file=rel,
                            line=node.start_point[0] + 1,
                        ))
                        edges.append(ParsedEdge(
                            src=f"file:{rel}", dst=fid, kind="defines",
                        ))

        for child in node.children:
            self._walk(child, source, rel, nodes, edges)
