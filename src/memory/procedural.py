"""Procedural memory."""
from __future__ import annotations

import logging
from pathlib import Path

import yaml

from src.memory.base import Procedure, new_id, now_ts
from src.memory.config import ProceduralSettings

logger = logging.getLogger(__name__)


class ProceduralMemory:
    def __init__(self, cfg: ProceduralSettings, base_dir: Path):
        self.cfg = cfg
        self.path = base_dir / cfg.path
        self._items: list[Procedure] | None = None

    def _load(self) -> list[Procedure]:
        if self._items is not None:
            return self._items
        if not self.path.exists():
            self._items = []
            return self._items
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f) or {}
            self._items = [
                Procedure(**{**p, "tags": p.get("tags", [])})
                for p in data.get("procedures", [])
            ]
        except Exception as e:
            logger.warning("Ошибка чтения процедур: %s", e)
            self._items = []
        return self._items

    def _save(self) -> None:
        items = self._load()
        items.sort(key=lambda x: (-x.success_rate, -x.success_count))
        items = items[: self.cfg.max_procedures]
        self._items = items

        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "procedures": [
                {
                    "id": p.id, "trigger": p.trigger, "steps": p.steps,
                    "success_count": p.success_count,
                    "fail_count": p.fail_count,
                    "last_used": p.last_used, "tags": p.tags,
                }
                for p in items
            ]
        }
        with open(self.path, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)

    def add(
        self, trigger: str, steps: list[str],
        tags: list[str] | None = None,
    ) -> Procedure:
        items = self._load()
        for p in items:
            if p.trigger.lower() == trigger.lower():
                return p
        proc = Procedure(
            id=new_id("proc_"), trigger=trigger, steps=steps,
            tags=tags or [],
        )
        items.append(proc)
        self._save()
        return proc

    def record_success(self, proc_id: str) -> None:
        for p in self._load():
            if p.id == proc_id:
                p.success_count += 1
                p.last_used = now_ts()
                self._save()
                return

    def record_failure(self, proc_id: str) -> None:
        for p in self._load():
            if p.id == proc_id:
                p.fail_count += 1
                p.last_used = now_ts()
                self._save()
                return

    def find(self, problem: str, limit: int = 3) -> list[Procedure]:
        if not problem:
            return []
        words = {w.lower() for w in problem.split() if len(w) > 2}
        scored: list[tuple[float, Procedure]] = []
        for p in self._load():
            if p.success_count < self.cfg.min_success_count:
                continue
            if p.success_rate < self.cfg.min_success_rate:
                continue
            text = (p.trigger + " " + " ".join(p.tags)).lower()
            hits = sum(1 for w in words if w in text)
            if hits:
                scored.append((hits, p))
        scored.sort(key=lambda x: (-x[0], -x[1].success_count))
        return [p for _, p in scored[:limit]]

    def all(self) -> list[Procedure]:
        return list(self._load())

    def stats(self) -> dict:
        items = self._load()
        return {
            "total": len(items),
            "usable": sum(
                1 for p in items
                if p.success_count >= self.cfg.min_success_count
                and p.success_rate >= self.cfg.min_success_rate
            ),
        }
