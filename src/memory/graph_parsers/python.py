"""Python-парсер через tree-sitter."""
from __future__ import annotations

import logging
from pathlib import Path

from src.memory.graph_parsers.base import ParsedEdge, ParsedNode

logger = logging.getLogger(__name__)


class PythonParser:
    id = "python"
    extensions = [".py"]

    def __init__(self):
        self._lang = None
        self._parser = None

    def _load(self):
        if self._parser is not None:
            return
        try:
            from tree_sitter import Language, Parser
            import tree_sitter_python as tspython
            self._lang = Language(tspython.language())
            self._parser = Parser(self._lang)
        except ImportError as e:
            logger.warning("tree-sitter-python недоступен: %s", e)
            raise

    def parse(
        self, path: Path, source: str,
    ) -> tuple[list[ParsedNode], list[ParsedEdge]]:
        self._load()
        rel = str(path).replace("\\", "/")
        nodes: list[ParsedNode] = []
        edges: list[ParsedEdge] = []

        nodes.append(ParsedNode(
            id=f"file:{rel}", kind="file", name=rel, file=rel,
        ))

        try:
            tree = self._parser.parse(source.encode("utf-8"))
        except Exception as e:
            logger.warning("Parse failed %s: %s", path, e)
            return nodes, edges

        root = tree.root_node
        self._walk(root, source, rel, nodes, edges, parent=None)
        return nodes, edges

    def _walk(
        self, node, source: str, rel: str,
        nodes: list[ParsedNode], edges: list[ParsedEdge],
        parent: str | None,
    ):
        t = node.type

        if t == "import_statement":
            for child in node.children:
                if child.type in ("dotted_name", "identifier"):
                    mod = source[child.start_byte:child.end_byte]
                    edges.append(ParsedEdge(
                        src=f"file:{rel}",
                        dst=f"module:{mod}",
                        kind="imports",
                    ))
        elif t == "import_from_statement":
            mod_node = node.child_by_field_name("module_name")
            if mod_node:
                mod = source[mod_node.start_byte:mod_node.end_byte]
                edges.append(ParsedEdge(
                    src=f"file:{rel}",
                    dst=f"module:{mod}",
                    kind="imports",
                ))
        elif t in ("function_definition", "async_function_definition"):
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
                if parent:
                    edges.append(ParsedEdge(
                        src=parent, dst=fid, kind="defines",
                    ))
        elif t == "class_definition":
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

                # Методы внутри класса
                for child in node.children:
                    if child.type == "block":
                        for sub in child.children:
                            if sub.type in (
                                "function_definition",
                                "async_function_definition",
                            ):
                                self._walk(
                                    sub, source, rel, nodes, edges, parent=cid,
                                )
                return  # уже обошли

        for child in node.children:
            self._walk(child, source, rel, nodes, edges, parent=parent)
