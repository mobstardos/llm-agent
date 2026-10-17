"""Загрузка Loop-спеков из loops/*.yaml."""
from __future__ import annotations

import logging
from pathlib import Path

import yaml
from pydantic import ValidationError

from src.core.schema import LoopSpec

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent


class LoopSpecLoader:
    def __init__(self, base_dir: Path | None = None):
        self.base_dir = base_dir or BASE_DIR
        self.loops_dir = self.base_dir / "loops"
        self._cache: dict[str, LoopSpec] = {}
        self.errors: dict[str, str] = {}

    def load_all(self) -> dict[str, LoopSpec]:
        self._cache = {}
        self.errors = {}
        if not self.loops_dir.exists():
            logger.debug("Директория loops/ не найдена: %s", self.loops_dir)
            return {}

        for f in sorted(self.loops_dir.glob("*.yaml")):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    data = yaml.safe_load(fh) or {}
                spec = LoopSpec(**data)
                self._cache[spec.id] = spec
                logger.info("Loop загружен: %s (%s)", spec.id, spec.type.value)
            except ValidationError as e:
                self.errors[f.name] = str(e)
                logger.error("Ошибка валидации %s: %s", f, e)
            except Exception as e:
                self.errors[f.name] = str(e)
                logger.error("Ошибка загрузки %s: %s", f, e)

        return self._cache

    def get(self, loop_id: str) -> LoopSpec | None:
        if not self._cache:
            self.load_all()
        return self._cache.get(loop_id)

    def list(self) -> list[LoopSpec]:
        if not self._cache:
            self.load_all()
        return list(self._cache.values())

    def reload(self) -> dict[str, LoopSpec]:
        return self.load_all()
