"""Плагинные миграции деклараций.

Глобальные миграции в src/core/migrations/*.py.
Локальные в <declaration_dir>/migrations/*.py.
Формат имени: m_X_Y_to_A_B.py
Внутри — функция migrate(data: dict) -> dict.
"""
from __future__ import annotations

import importlib.util
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

logger = logging.getLogger(__name__)


@dataclass
class Migration:
    from_version: str
    to_version: str
    fn: Callable[[dict], dict]
    source: Path


class MigrationEngine:
    def __init__(self, global_dir: Path | None = None):
        if global_dir is None:
            global_dir = Path(__file__).resolve().parent / "migrations"
        self.global_dir = global_dir
        self.global_migrations: list[Migration] = []
        if global_dir.exists():
            self.global_migrations = self._load_dir(global_dir)

    # ═══════════════════════════════════════════════════════
    # Discovery
    # ═══════════════════════════════════════════════════════
    def _load_dir(self, d: Path) -> list[Migration]:
        result: list[Migration] = []
        for f in sorted(d.glob("m_*.py")):
            m = self._load_file(f)
            if m:
                result.append(m)
        return result

    def _load_file(self, path: Path) -> Migration | None:
        name = path.stem
        if not name.startswith("m_"):
            return None
        try:
            parts = name[2:].split("_to_")
            from_v = parts[0].replace("_", ".")
            to_v = parts[1].replace("_", ".")
        except Exception:
            logger.warning("Не разобрано имя миграции: %s", path)
            return None

        try:
            spec = importlib.util.spec_from_file_location(
                f"_mig_{path.stem}", str(path),
            )
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            fn = getattr(mod, "migrate", None)
            if not callable(fn):
                logger.warning("Нет функции migrate() в %s", path)
                return None
            return Migration(from_v, to_v, fn, path)
        except Exception as e:
            logger.exception("Ошибка загрузки миграции %s: %s", path, e)
            return None

    # ═══════════════════════════════════════════════════════
    # Apply
    # ═══════════════════════════════════════════════════════
    def migrate(
        self,
        data: dict,
        from_version: str,
        to_version: str,
        local_dir: Path | None = None,
    ) -> tuple[dict, list[str]]:
        if from_version == to_version:
            return data, []

        migrations = list(self.global_migrations)
        if local_dir:
            local_mig_dir = local_dir / "migrations"
            if local_mig_dir.exists():
                migrations.extend(self._load_dir(local_mig_dir))

        chain = self._build_chain(migrations, from_version, to_version)
        if not chain:
            logger.debug(
                "Нет цепочки миграций %s → %s", from_version, to_version,
            )
            return data, []

        applied: list[str] = []
        current = dict(data)
        for m in chain:
            try:
                current = m.fn(current)
                applied.append(f"{m.from_version}→{m.to_version}")
                logger.info(
                    "Миграция %s→%s применена (%s)",
                    m.from_version, m.to_version, m.source.name,
                )
            except Exception as e:
                logger.exception("Миграция %s упала: %s", m.source, e)
                break

        return current, applied

    @staticmethod
    def _build_chain(
        migrations: list[Migration], from_v: str, to_v: str,
    ) -> list[Migration]:
        def vt(v: str) -> tuple[int, ...]:
            try:
                return tuple(int(x) for x in v.split("."))
            except ValueError:
                return (0,)

        try:
            target = vt(to_v)
            start = vt(from_v)
        except Exception:
            return []

        if start >= target:
            return []

        by_from: dict[str, list[Migration]] = {}
        for m in migrations:
            by_from.setdefault(m.from_version, []).append(m)

        chain: list[Migration] = []
        current = from_v
        visited: set[str] = set()

        while current != to_v and current not in visited:
            visited.add(current)
            candidates = [
                m for m in by_from.get(current, [])
                if vt(m.to_version) <= target
            ]
            if not candidates:
                return []
            candidates.sort(key=lambda m: vt(m.to_version), reverse=True)
            chosen = candidates[0]
            chain.append(chosen)
            current = chosen.to_version

        return chain if current == to_v else []
