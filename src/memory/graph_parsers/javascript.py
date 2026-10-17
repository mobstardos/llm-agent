"""JavaScript / TypeScript парсер через tree-sitter."""
from __future__ import annotations

import logging
from pathlib import Path

from src.memory.graph_parsers.base import ParsedEdge, ParsedNode

logger = logging.getLogger(__name__)


class JavaScriptParser:
    id = "javascript"
    extensions = [".js", ".jsx", ".mjs", ".cjs"]

    def __init__(self):
        self._parser = None

    def _load(self):
        if self._parser is not None:
            return
        from tree_sitter import Language, Parser
        import tree_sitter_javascript as tsjs
        lang = Language(tsjs.language())
        self._parser = Parser(lang)

    def parse(
        self, path: Path, source: str,
    ) -> tuple[list[ParsedNode], list[ParsedEdge]]:
        self._load()
        rel = str(path).replace("\\", "/")
        nodes = [ParsedNode(id=f"file:{rel}", kind="file",
                            name=rel, file=rel)]
        edges: list[ParsedEdge] = []

        try:
            tree = self._parser.parse(source.encode("utf-8"))
        except Exception as e:
            logger.warning("Parse failed %s: %s", path, e)
            return nodes, edges

        self._walk(tree.root_node, source, rel, nodes, edges)
        return nodes, edges

    def _walk(self, node, source, rel, nodes, edges):
        t = node.type

        # import ... from "module"
        if t == "import_statement":
            src_node = node.child_by_field_name("source")
            if src_node:
                mod = source[src_node.start_byte:src_node.end_byte].strip("\"'")
                edges.append(ParsedEdge(
                    src=f"file:{rel}", dst=f"module:{mod}", kind="imports",
                ))

        # require("module")
        elif t == "call_expression":
            fn_node = node.child_by_field_name("function")
            if fn_node and source[fn_node.start_byte:fn_node.end_byte] == "require":
                args = node.child_by_field_name("arguments")
                if args and args.named_children:
                    arg = args.named_children[0]
                    mod = source[arg.start_byte:arg.end_byte].strip("\"'")
                    edges.append(ParsedEdge(
                        src=f"file:{rel}", dst=f"module:{mod}", kind="imports",
                    ))

        # function foo() {}
        elif t in ("function_declaration", "generator_function_declaration"):
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

        # const foo = () => {} / const foo = function() {}
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

        # class Foo {}
        elif t == "class_declaration":
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

                # Методы
                body = node.child_by_field_name("body")
                if body:
                    for child in body.children:
                        if child.type == "method_definition":
                            m_name = child.child_by_field_name("name")
                            if m_name:
                                mn = source[
                                    m_name.start_byte:m_name.end_byte
                                ]
                                mid = f"method:{rel}::{name}.{mn}"
                                nodes.append(ParsedNode(
                                    id=mid, kind="method",
                                    name=f"{name}.{mn}",
                                    file=rel,
                                    line=child.start_point[0] + 1,
                                ))
                                edges.append(ParsedEdge(
                                    src=cid, dst=mid, kind="defines",
                                ))

        for child in node.children:
            self._walk(child, source, rel, nodes, edges)
