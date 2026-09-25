"""Ретенция: «память копится, пока есть свободное место на диске».

Политика (в порядке эскалации):
  1. Свободно > min_free_gb → НИЧЕГО не удаляем. Всё копится.
  2. Свободно <= min_free_gb → сжимаем тени дней старше compress_after_days
     в zip-архивы (типичное сжатие текста 5–15×), удаляем несжатые оригиналы.
  3. Свободно всё ещё мало → удаляем самые старые ARCHIVES (не индекс!).
  4. Если задан max_share_percent — та же эскалация по доле диска.

НИКОГДА не удаляются:
  * journal.sqlite (индекс и связи всех событий)
  * events/*.jsonl (полный поток; старые файлы сжимаются в zip — по флагу)
  * reports/*.md (человеко- и AI-читаемые отчёты)

Итог: история действий сохраняется метаданными всегда; объёмное содержимое
(тени) сжимается, а в крайнем случае удаляется от старых к новым.
"""
from __future__ import annotations

import shutil
import threading
import time
import zipfile
from pathlib import Path

from .config import JournalConfig
from .utils import dir_size, disk_usage, human_size


class RetentionManager:
    def __init__(self, cfg: JournalConfig, store=None, log=None):
        self.cfg = cfg
        self.store = store            # JournalStore (для записи событий system)
        self.log = log
        self._lock = threading.Lock()
        self.last_result: dict = {}

    # ─── Проверка необходимости ──────────────────────────────
    def status(self) -> dict:
        disk = disk_usage(self.cfg.base_dir)
        used = dir_size(self.cfg.base_dir)
        total = disk["total_bytes"] or 1
        return {
            "free_gb": disk["free_gb"],
            "min_free_gb": self.cfg.min_free_gb,
            "need_compress": disk["free_gb"] < self.cfg.min_free_gb,
            "journal_size": human_size(used),
            "journal_share_percent": round(used / total * 100, 3),
            "max_share_percent": self.cfg.max_share_percent,
            "policy": {
                "accumulate": disk["free_gb"] >= self.cfg.min_free_gb,
                "compress_after_days": self.cfg.compress_after_days,
            },
        }

    def _needed(self) -> tuple[bool, str]:
        disk = disk_usage(self.cfg.base_dir)
        if disk["free_gb"] and disk["free_gb"] < self.cfg.min_free_gb:
            return True, f"free {disk['free_gb']}GB < {self.cfg.min_free_gb}GB"
        if (self.cfg.max_share_percent > 0):
            used = dir_size(self.cfg.base_dir)
            total = disk["total_bytes"] or 1
            if used / total * 100 > self.cfg.max_share_percent:
                return True, "journal share exceeded"
        return False, ""

    # ─── Основной проход ─────────────────────────────────────
    def sweep(self) -> dict:
        """Один проход ретенции. Вызывается планировщиком и вручную."""
        with self._lock:
            result: dict = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "compressed_days": [], "removed_archives": [],
                "compressed_jsonl": [], "freed_bytes": 0,
                "trigger": "", "errors": [],
            }
            need, why = self._needed()
            if not need:
                result["trigger"] = "not_needed"
                self.last_result = result
                return result
            result["trigger"] = why

            # Этап 1: сжатие старых дней теней
            freed = self._compress_old_days(result)
            result["freed_bytes"] += freed

            # Проверяем ещё раз
            need, _ = self._needed()

            # Этап 2: сжатие старых JSONL
            if need and self.cfg.jsonl_retention_days >= 0:
                freed = self._compress_old_jsonl(result)
                result["freed_bytes"] += freed
                need, _ = self._needed()

            # Этап 3: удаление старейших архивов (крайняя мера)
            if need:
                freed = self._drop_oldest_archives(result)
                result["freed_bytes"] += freed

            self._log_event(result)
            self.last_result = result
            return result

    # ─── Этап 1: тени → zip ──────────────────────────────────
    def _compress_old_days(self, result: dict) -> int:
        freed = 0
        cutoff = time.time() - self.cfg.compress_after_days * 86400
        shadows_root = self.cfg.shadows_dir
        if not shadows_root.exists():
            return 0
        for day_dir in sorted(shadows_root.iterdir()):
            if not day_dir.is_dir():
                continue
            try:
                mtime = day_dir.stat().st_mtime
            except OSError:
                continue
            if mtime > cutoff:
                continue
            try:
                size_before = dir_size(day_dir)
                if size_before == 0:
                    continue
                archive = self.cfg.archives_dir / f"shadows-{day_dir.name}.zip"
                if archive.exists():
                    # уже сжат — удаляем оригинал
                    shutil.rmtree(day_dir, ignore_errors=True)
                    continue
                self.cfg.archives_dir.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
                    for f in day_dir.rglob("*"):
                        if f.is_file():
                            zf.write(f, f.relative_to(shadows_root))
                shutil.rmtree(day_dir, ignore_errors=True)
                size_after = archive.stat().st_size
                freed += max(0, size_before - size_after)
                result["compressed_days"].append({
                    "day": day_dir.name,
                    "before": human_size(size_before),
                    "after": human_size(size_after),
                })
            except Exception as e:
                result["errors"].append(f"compress {day_dir.name}: {e}")
        return freed

    # ─── Этап 2: старые JSONL → zip ──────────────────────────
    def _compress_old_jsonl(self, result: dict) -> int:
        if self.cfg.jsonl_retention_days == 0 and not self._needed()[0]:
            return 0
        freed = 0
        cutoff = time.time() - max(self.cfg.jsonl_retention_days, 7) * 86400
        ev_dir = self.cfg.jsonl_dir
        if not ev_dir.exists():
            return 0
        for f in sorted(ev_dir.glob("events-*.jsonl")):
            try:
                if f.stat().st_mtime > cutoff:
                    continue
                archive = self.cfg.archives_dir / f"{f.stem}.zip"
                if archive.exists():
                    continue
                self.cfg.archives_dir.mkdir(parents=True, exist_ok=True)
                size_before = f.stat().st_size
                with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
                    zf.write(f, f.name)
                f.unlink()
                size_after = archive.stat().st_size
                freed += max(0, size_before - size_after)
                result["compressed_jsonl"].append(f.name)
            except Exception as e:
                result["errors"].append(f"jsonl {f.name}: {e}")
        return freed

    # ─── Этап 3: старые архивы → удаление ────────────────────
    def _drop_oldest_archives(self, result: dict) -> int:
        freed = 0
        archives = self.cfg.archives_dir
        if not archives.exists():
            return 0
        files = sorted(
            [f for f in archives.iterdir() if f.is_file()],
            key=lambda f: f.stat().st_mtime,
        )
        for f in files:
            need, _ = self._needed()
            if not need:
                break
            try:
                size = f.stat().st_size
                f.unlink()
                freed += size
                result["removed_archives"].append({
                    "name": f.name, "size": human_size(size),
                })
            except OSError as e:
                result["errors"].append(f"drop {f.name}: {e}")
        return freed

    # ─── Аудит в журнал ──────────────────────────────────────
    def _log_event(self, result: dict) -> None:
        if self.store is None:
            return
        try:
            from .schema import Event, EventKind
            self.store.insert(Event(
                kind=EventKind.SYSTEM, action="retention_sweep",
                meta={k: v for k, v in result.items() if k != "ts"},
            ))
        except Exception:
            pass


def start_background_sweep(cfg: JournalConfig, manager: RetentionManager) -> threading.Thread:
    """Фоновый поток периодической ретенции (daemon)."""
    stop = threading.Event()

    def _run() -> None:
        # первый проход через минуту после старта
        if not stop.wait(60):
            try:
                manager.sweep()
            except Exception:
                pass
        while not stop.wait(cfg.sweep_interval_seconds):
            try:
                manager.sweep()
            except Exception:
                continue
    t = threading.Thread(target=_run, name="journal-retention", daemon=True)
    t.start()
    return t
