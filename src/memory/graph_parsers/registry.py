"""Registry парсеров по расширению."""
from __future__ import annotations

import logging
from pathlib import Path

from src.memory.graph_parsers.base import LanguageParser

logger = logging.getLogger(__name__)


class ParserRegistry:
    def __init__(self):
        self._by_ext: dict[str, LanguageParser] = {}
        self._register_builtin()

    def _register_builtin(self) -> None:
        # Python
        try:
            from src.memory.graph_parsers.python import PythonParser
            self.register(PythonParser())
        except Exception as e:
            logger.warning("PythonParser: %s", e)

        # JavaScript
        try:
            from src.memory.graph_parsers.javascript import JavaScriptParser
            self.register(JavaScriptParser())
        except Exception as e:
            logger.warning("JavaScriptParser: %s", e)

        # TypeScript
        try:
            from src.memory.graph_parsers.typescript import TypeScriptParser
            self.register(TypeScriptParser())
        except Exception as e:
            logger.warning("TypeScriptParser: %s", e)

    def register(self, parser: LanguageParser) -> None:
        for ext in parser.extensions:
            self._by_ext[ext] = parser

    def get_for(self, path: Path) -> LanguageParser | None:
        return self._by_ext.get(path.suffix.lower())

    def supported_extensions(self) -> list[str]:
        return sorted(self._by_ext.keys())
