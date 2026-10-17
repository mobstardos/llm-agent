"""Хранилище последних валидных деклараций с автовосстановлением."""
from __future__ import annotations

import logging
import shutil
import time
from pathlib import Path

logger = logging.getLogger(__name__)

MAX_VERSIONS = 10
KINDS = ("agents", "mcp_servers", "capabilities")


class RollbackStore:
    def __init__(self, base_dir: Path, path: str | Path | None = None):
        self.base_dir = Path(base_dir)
        self.path = Path(path) if path else self.base_dir / "data" / "declarations_backups"
        self.path.mkdir(parents=True, exist_ok=True)

    # ═══════════════════════════════════════════════════════
    # Snapshot
    # ═══════════════════════════════════════════════════════
    def snapshot(self, kind: str, tag: str = "auto") -> Path | None:
        if kind not in KINDS:
            raise ValueError(f"kind должен быть из {KINDS}")
        src = self.base_dir / kind
        if not src.exists():
            return None

        ts = time.strftime("%Y-%m-%d_%H-%M-%S")
        dst = self.path / kind / f"{ts}_{tag}"
        dst.parent.mkdir(parents=True, exist_ok=True)

        try:
            shutil.copytree(src, dst, dirs_exist_ok=True)
            logger.debug("Rollback snapshot: %s → %s", src.name, dst.name)
            self._prune(kind)
            return dst
        except Exception as e:
            logger.warning("Не удалось создать snapshot %s: %s", kind, e)
            return None

    def snapshot_all(self, tag: str = "auto") -> dict[str, Path | None]:
        return {kind: self.snapshot(kind, tag) for kind in KINDS}

    # ═══════════════════════════════════════════════════════
    # Restore
    # ═══════════════════════════════════════════════════════
    def restore(self, kind: str, version_dir: str | Path | None = None) -> bool:
        if kind not in KINDS:
            return False
        versions = self.list(kind)
        if not versions:
            logger.warning("Нет версий для %s", kind)
            return False

        if version_dir is None:
            src = self.path / kind / versions[0]["name"]
        else:
            src = Path(version_dir)
        if not src.exists():
            return False

        dst = self.base_dir / kind
        try:
            # Backup текущего перед восстановлением
            self.snapshot(kind, tag="pre_restore")
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)
            logger.info("Восстановлено %s из %s", kind, src.name)
            return True
        except Exception as e:
            logger.exception("Восстановление %s упало: %s", kind, e)
            return False

    # ═══════════════════════════════════════════════════════
    # List
    # ═══════════════════════════════════════════════════════
    def list(self, kind: str) -> list[dict]:
        d = self.path / kind
        if not d.exists():
            return []
        versions = []
        for v in sorted(d.iterdir(), reverse=True):
            if not v.is_dir():
                continue
            try:
                mtime = v.stat().st_mtime
            except OSError:
                continue
            versions.append({
                "name": v.name,
                "path": str(v),
                "ts": mtime,
                "size": self._dir_size(v),
            })
        return versions

    @staticmethod
    def _dir_size(d: Path) -> int:
        total = 0
        for f in d.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    pass
        return total

    def _prune(self, kind: str) -> None:
        d = self.path / kind
        if not d.exists():
            return
        dirs = sorted(
            [x for x in d.iterdir() if x.is_dir()],
            key=lambda x: x.stat().st_mtime,
            reverse=True,
        )
        for old in dirs[MAX_VERSIONS:]:
            try:
                shutil.rmtree(old)
            except Exception:
                pass
