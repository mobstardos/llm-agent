"""Автоматические бэкапы PostgreSQL через pg_dump.

Функции:
- create_backup() — полный дамп БД с сжатием
- list_backups() — список бэкапов
- restore(backup_file) — восстановление
- cleanup_old() — ротация
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


class BackupManager:
    """Управление бэкапами через pg_dump / pg_restore."""

    def __init__(
        self,
        backup_dir: str | Path = "data/db_backups",
        host: str = "localhost",
        port: int = 5432,
        user: str = "llmagent",
        password: str = "secret",
        database: str = "llmagent",
        keep_last: int = 7,
    ):
        self.backup_dir = Path(backup_dir)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        self.host = host
        self.port = port
        self.user = user
        self.password = password
        self.database = database
        self.keep_last = keep_last

        self._lock = asyncio.Lock()

    # ═══════════════════════════════════════════════════════
    # Create
    # ═══════════════════════════════════════════════════════
    async def create_backup(
        self,
        tables: list[str] | None = None,
        schemas: list[str] | None = None,
    ) -> Path | None:
        """Создать дамп БД.

        Args:
            tables: если задано — только эти таблицы
            schemas: если задано — только эти схемы
        """
        async with self._lock:
            return await self._create_backup_internal(tables, schemas)

    async def _create_backup_internal(
        self,
        tables: list[str] | None,
        schemas: list[str] | None,
    ) -> Path | None:
        pg_dump = shutil.which("pg_dump")
        if not pg_dump:
            logger.warning("pg_dump не найден в PATH")
            return None

        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        output = self.backup_dir / f"llmagent_{ts}.dump"

        env = {**os.environ, "PGPASSWORD": self.password}

        cmd = [
            pg_dump,
            "-h", self.host,
            "-p", str(self.port),
            "-U", self.user,
            "-d", self.database,
            "--no-owner",
            "--no-privileges",
            "-F", "c",  # custom format (сжатый, поддерживает pg_restore)
            "-f", str(output),
        ]

        # Опции фильтрации
        if schemas:
            for s in schemas:
                cmd.extend(["-n", s])
        elif tables:
            for t in tables:
                cmd.extend(["-t", t])

        # Прогресс
        cmd.append("--verbose")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()

            if proc.returncode != 0:
                err_text = stderr.decode("utf-8", errors="replace")
                logger.error("pg_dump failed: %s", err_text[-500:])
                if output.exists():
                    try:
                        output.unlink()
                    except Exception:
                        pass
                return None

            size_mb = output.stat().st_size / 1024 / 1024
            logger.info("Backup created: %s (%.1f MB)", output.name, size_mb)
            self._cleanup_old()
            return output

        except Exception as e:
            logger.exception("Backup failed: %s", e)
            return None

    # ═══════════════════════════════════════════════════════
    # Restore
    # ═══════════════════════════════════════════════════════
    async def restore(
        self,
        backup_file: Path,
        clean: bool = True,
        schemas: list[str] | None = None,
    ) -> bool:
        """Восстановить БД из бэкапа.

        Args:
            backup_file: путь к .dump
            clean: предварительно удалить существующие объекты
            schemas: только эти схемы
        """
        if not backup_file.exists():
            logger.error("Backup not found: %s", backup_file)
            return False

        pg_restore = shutil.which("pg_restore")
        if not pg_restore:
            logger.warning("pg_restore не найден")
            return False

        env = {**os.environ, "PGPASSWORD": self.password}

        cmd = [
            pg_restore,
            "-h", self.host,
            "-p", str(self.port),
            "-U", self.user,
            "-d", self.database,
            "--no-owner",
            "--no-privileges",
            "--single-transaction",  # атомарно
            "--verbose",
        ]

        if clean:
            cmd.append("--clean")
            cmd.append("--if-exists")

        if schemas:
            for s in schemas:
                cmd.extend(["-n", s])

        cmd.append(str(backup_file))

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await proc.communicate()

            # pg_restore может вернуть ненулевой код из-за warning'ов
            # Даже при --if-exists могут быть предупреждения
            stderr_text = stderr.decode("utf-8", errors="replace")
            errors = [
                line for line in stderr_text.splitlines()
                if "error:" in line.lower()
            ]

            if errors:
                logger.error("pg_restore errors:\n%s", "\n".join(errors[:10]))
                return False

            logger.info("Restored from: %s", backup_file.name)
            return True

        except Exception as e:
            logger.exception("Restore failed: %s", e)
            return False

    # ═══════════════════════════════════════════════════════
    # List
    # ═══════════════════════════════════════════════════════
    def list_backups(self) -> list[dict]:
        """Список бэкапов с метаданными."""
        result = []
        for f in sorted(self.backup_dir.glob("llmagent_*.dump"), reverse=True):
            try:
                stat = f.stat()
                result.append({
                    "name": f.name,
                    "path": str(f),
                    "size_mb": round(stat.st_size / 1024 / 1024, 2),
                    "modified": datetime.fromtimestamp(
                        stat.st_mtime,
                    ).isoformat(),
                    "age_hours": round(
                        (datetime.now().timestamp() - stat.st_mtime) / 3600, 1,
                    ),
                })
            except Exception:
                continue
        return result

    def stats(self) -> dict:
        backups = self.list_backups()
        total_mb = sum(b["size_mb"] for b in backups)
        return {
            "count": len(backups),
            "keep_last": self.keep_last,
            "total_mb": round(total_mb, 2),
            "backup_dir": str(self.backup_dir),
            "latest": backups[0] if backups else None,
        }

    # ═══════════════════════════════════════════════════════
    # Delete / cleanup
    # ═══════════════════════════════════════════════════════
    def delete_backup(self, name: str) -> bool:
        """Удалить конкретный бэкап."""
        # Защита от path traversal
        if "/" in name or "\\" in name or ".." in name:
            return False
        f = self.backup_dir / name
        if not f.exists() or not f.name.startswith("llmagent_"):
            return False
        try:
            f.unlink()
            logger.info("Deleted backup: %s", name)
            return True
        except Exception as e:
            logger.warning("Delete backup: %s", e)
            return False

    def _cleanup_old(self) -> int:
        """Удалить старые бэкапы, оставив keep_last."""
        files = sorted(
            self.backup_dir.glob("llmagent_*.dump"),
            reverse=True,
        )
        removed = 0
        for old in files[self.keep_last:]:
            try:
                old.unlink()
                removed += 1
                logger.debug("Removed old backup: %s", old.name)
            except Exception:
                pass
        return removed

    def cleanup_old(self) -> int:
        """Публичный API для очистки."""
        return self._cleanup_old()

    # ═══════════════════════════════════════════════════════
    # Auto-backup
    # ═══════════════════════════════════════════════════════
    async def backup_loop(
        self,
        interval_hours: float = 24.0,
        initial_delay_seconds: float = 60.0,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """Фоновый цикл бэкапов."""
        if initial_delay_seconds > 0:
            await asyncio.sleep(initial_delay_seconds)

        while True:
            if stop_event and stop_event.is_set():
                return
            try:
                result = await self.create_backup()
                if result:
                    logger.info("Auto-backup OK: %s", result.name)
                else:
                    logger.warning("Auto-backup failed")
            except Exception as e:
                logger.exception("Auto-backup loop error: %s", e)

            try:
                if stop_event:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=interval_hours * 3600,
                    )
                    return
                await asyncio.sleep(interval_hours * 3600)
            except asyncio.TimeoutError:
                pass
