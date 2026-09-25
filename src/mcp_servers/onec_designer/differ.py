"""Сравнение конфигурации до/после загрузки (по XML/Bsl)."""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class ConfigDiff:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    modified: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.added) + len(self.removed) + len(self.modified)

    def to_dict(self) -> dict:
        return {
            "added": self.added[:100],
            "removed": self.removed[:100],
            "modified": self.modified[:100],
            "total": self.total,
        }


class ConfigDiffer:
    """Снимок конфигурации в файлах + сравнение."""

    IGNORE_DIRS = {".git", ".snapshots", ".vscode", "temp"}

    def __init__(self, config_dir: str | Path):
        self.config_dir = Path(config_dir)

    def snapshot(self) -> dict[str, str]:
        """Возвращает {rel_path: sha256}."""
        result: dict[str, str] = {}
        if not self.config_dir.exists():
            return result

        for path in self.config_dir.rglob("*"):
            if not path.is_file():
                continue
            if any(p in self.IGNORE_DIRS for p in path.parts):
                continue
            try:
                h = hashlib.sha256(path.read_bytes()).hexdigest()
                rel = str(path.relative_to(self.config_dir)).replace("\\", "/")
                result[rel] = h
            except Exception:
                continue
        return result

    def compare(
        self, before: dict[str, str], after: dict[str, str],
    ) -> ConfigDiff:
        diff = ConfigDiff()
        before_keys = set(before.keys())
        after_keys = set(after.keys())

        diff.added = sorted(after_keys - before_keys)[:500]
        diff.removed = sorted(before_keys - after_keys)[:500]

        for k in sorted(before_keys & after_keys):
            if before[k] != after[k]:
                diff.modified.append(k)
                if len(diff.modified) >= 500:
                    break

        return diff

    def save_snapshot(self, snapshots_dir: Path, name: str) -> Path:
        """Сохраняет snapshot в JSON."""
        import json
        import time
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        path = snapshots_dir / f"{name}_{int(time.time())}.json"
        path.write_text(
            json.dumps(self.snapshot(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return path
