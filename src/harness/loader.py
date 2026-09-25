"""Загрузка сценариев harness из harness/scenarios/."""
from __future__ import annotations

import logging
from pathlib import Path

import yaml
from pydantic import ValidationError

from src.core.schema import ScenarioSpec

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent


class ScenarioLoader:
    def __init__(self, base_dir: Path | None = None):
        self.base_dir = base_dir or BASE_DIR
        self.scenarios_dir = self.base_dir / "harness" / "scenarios"
        self.errors: dict[str, str] = {}

    def load_all(self) -> dict[str, ScenarioSpec]:
        result: dict[str, ScenarioSpec] = {}
        self.errors = {}

        if not self.scenarios_dir.exists():
            logger.debug("Нет директории scenarios: %s", self.scenarios_dir)
            return result

        for f in sorted(self.scenarios_dir.rglob("*.yaml")):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    data = yaml.safe_load(fh) or {}
                spec = ScenarioSpec(**data)
                result[spec.id] = spec
                logger.debug("Scenario загружен: %s", spec.id)
            except ValidationError as e:
                self.errors[f.name] = str(e)
                logger.warning("Scenario %s: ошибка валидации", f)
            except Exception as e:
                self.errors[f.name] = str(e)
                logger.warning("Scenario %s: %s", f, e)

        return result

    def list_by_tag(self, tag: str) -> list[ScenarioSpec]:
        return [s for s in self.load_all().values() if tag in s.tags]

    def list_by_category(self, cat: str) -> list[ScenarioSpec]:
        return [s for s in self.load_all().values() if s.category == cat]
