"""Базовые типы для graph-парсеров."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass
class ParsedNode:
    id: str
    kind: str           # file | function | class | method | interface
    name: str
    file: str
    line: int = 0
    metadata: dict = field(default_factory=dict)


@dataclass
class ParsedEdge:
    src: str
    dst: str
    kind: str           # imports | defines | calls | exports


class LanguageParser(Protocol):
    id: str
    extensions: list[str]

    def parse(self, path: Path, source: str) -> tuple[
        list[ParsedNode], list[ParsedEdge],
    ]: ...
