"""Базовая модель ExtractionResult и интерфейс Extractor."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass
class ExtractionResult:
    source: str
    mime: str
    extractor_id: str
    extractor_version: str
    text: str = ""
    pages: int | None = None
    chars: int = 0
    tables: list[list[list[Any]]] = field(default_factory=list)
    structure: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)
    confidence: float = 1.0
    truncated: bool = False
    warnings: list[str] = field(default_factory=list)
    duration_ms: float = 0.0

    def __post_init__(self):
        if not self.chars:
            self.chars = len(self.text or "")

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "mime": self.mime,
            "extractor_id": self.extractor_id,
            "extractor_version": self.extractor_version,
            "text": self.text,
            "pages": self.pages,
            "chars": self.chars,
            "tables": self.tables,
            "structure": self.structure,
            "metadata": self.metadata,
            "extras": self.extras,
            "confidence": self.confidence,
            "truncated": self.truncated,
            "warnings": self.warnings,
            "duration_ms": self.duration_ms,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "ExtractionResult":
        return cls(**d)


class Extractor(Protocol):
    id: str
    version: str
    mime_types: list[str]
    priority: int

    def can_handle(self, path: Path, mime: str) -> bool: ...

    def extract(self, path: Path, **kwargs) -> ExtractionResult: ...
