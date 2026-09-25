"""Теневые копии файлов: содержимое ДО и ПОСЛЕ правки.

Раскладка на диске:
  data/journal/shadows/2026-09-15/{event_id}/
      before/{относительный путь файла}
      after/{относительный путь файла}
      manifest.json   — список сохранённых файлов с хэшами и размерами

Тени — единственный источник для отката, поэтому они НЕ зависят от
работоспособности SQLite/JSONL. Манифест дублирует метаданные.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path
from typing import Any

from .config import JournalConfig
from .utils import sha256_file, looks_binary, read_text_safe, write_text_safe


class ShadowStore:
    def __init__(self, cfg: JournalConfig):
        self.cfg = cfg
        self.root = cfg.shadows_dir

    # ─── Сохранение ДО правки ────────────────────────────────
    def save_before(
        self,
        event_id: str,
        targets: dict[str, Path],   # {нормализованный путь: абсолютный путь}
    ) -> dict[str, Any]:
        """Копирует файлы до изменения. Возвращает манифест до."""
        day = time.strftime("%Y-%m-%d")
        ev_dir = self.root / day / event_id
        before_dir = ev_dir / "before"
        manifest: dict[str, Any] = {"event_id": event_id, "day": day, "files": {}}

        for norm, abs_path in targets.items():
            try:
                if not abs_path.exists() or not abs_path.is_file():
                    # Файл не существовал (будет создан) — фиксируем отсутствие
                    manifest["files"][norm] = {
                        "existed": False, "sha256": "", "size": 0,
                    }
                    continue
                size = abs_path.stat().st_size
                if size > self.cfg.shadow_max_file_mb * 1024 * 1024:
                    manifest["files"][norm] = {
                        "existed": True, "sha256": sha256_file(abs_path),
                        "size": size, "skipped": "too_large",
                    }
                    continue
                dst = before_dir / norm
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(abs_path, dst)
                manifest["files"][norm] = {
                    "existed": True, "sha256": sha256_file(abs_path),
                    "size": size, "binary": looks_binary(abs_path),
                }
            except Exception as e:
                manifest["files"][norm] = {
                    "existed": abs_path.exists(), "error": str(e),
                }

        self._write_manifest(ev_dir, manifest, "before")
        return manifest

    # ─── Сохранение ПОСЛЕ правки ─────────────────────────────
    def save_after(
        self,
        event_id: str,
        targets: dict[str, Path],
        before_manifest: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Копирует файлы после изменения (и после отката тоже — для аудита)."""
        day = (before_manifest or {}).get("day") or time.strftime("%Y-%m-%d")
        ev_dir = self.root / day / event_id
        after_dir = ev_dir / "after"
        manifest: dict[str, Any] = {
            "event_id": event_id, "day": day, "files": {},
        }

        for norm, abs_path in targets.items():
            try:
                entry: dict[str, Any] = {}
                if before_manifest:
                    entry["before"] = before_manifest["files"].get(norm, {})
                if not abs_path.exists() or not abs_path.is_file():
                    entry.update({"existed_after": False, "sha256": "", "size": 0})
                else:
                    size = abs_path.stat().st_size
                    if size > self.cfg.shadow_max_file_mb * 1024 * 1024:
                        entry.update({
                            "existed_after": True,
                            "sha256": sha256_file(abs_path), "size": size,
                            "skipped": "too_large",
                        })
                    else:
                        dst = after_dir / norm
                        dst.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(abs_path, dst)
                        entry.update({
                            "existed_after": True,
                            "sha256": sha256_file(abs_path), "size": size,
                            "binary": looks_binary(abs_path),
                        })
                manifest["files"][norm] = entry
            except Exception as e:
                manifest["files"][norm] = {"error": str(e)}

        self._write_manifest(ev_dir, manifest, "after")
        return manifest

    # ─── Манифесты ───────────────────────────────────────────
    def _write_manifest(self, ev_dir: Path, manifest: dict, stage: str) -> None:
        if not manifest.get("files"):
            return
        ev_dir.mkdir(parents=True, exist_ok=True)
        write_text_safe(ev_dir / f"manifest.{stage}.json",
                        json.dumps(manifest, ensure_ascii=False, indent=2))

    def read_manifest(self, shadow_dir: str, stage: str = "before") -> dict | None:
        p = self.root / shadow_dir / f"manifest.{stage}.json"
        if not p.exists():
            return None
        try:
            return json.loads(read_text_safe(p))
        except Exception:
            return None

    # ─── Восстановление ──────────────────────────────────────
    def restore_before(self, shadow_dir: str, project_root: Path,
                       only: list[str] | None = None,
                       force: bool = False,
                       current_hashes: dict[str, str] | None = None,
                       ) -> dict[str, Any]:
        """Восстанавливает файлы из 'before'-теней.

        current_hashes — текущие sha256 файлов (для конфликт-детекции):
        если файл изменился после события, в safe-режиме он пропускается.
        Возвращает {восстановлено: [...], удалено: [...], пропущено: [...], ошибки: [...]}
        """
        manifest = self.read_manifest(shadow_dir, "before")
        result: dict[str, Any] = {
            "restored": [], "removed": [], "skipped": [], "errors": [],
        }
        if not manifest:
            result["errors"].append(f"Манифест не найден: {shadow_dir}/manifest.before.json")
            return result

        # Ожидаемые хэши ПОСЛЕ события — из after-манифеста (конфликт-детекция)
        after_manifest = self.read_manifest(shadow_dir, "after") or {}

        current_hashes = current_hashes or {}
        for norm, info in manifest.get("files", {}).items():
            if only and norm not in only:
                continue
            try:
                abs_path = (project_root / norm)
                cur_hash = current_hashes.get(norm) or (
                    sha256_file(abs_path) if abs_path.is_file() else ""
                )
                after_info = after_manifest.get("files", {}).get(norm, {})
                expected_after = after_info.get("sha256") or ""
                if not after_info.get("existed_after", False):
                    expected_after = ""  # файла не должно быть (создан событием)
                # Конфликт: файл менялся после события
                if (not force and expected_after
                        and cur_hash and cur_hash != expected_after):
                    result["skipped"].append({
                        "path": norm, "reason": "conflict",
                        "detail": "файл изменён после события; используйте force",
                    })
                    continue
                if info.get("existed"):
                    src = self.root / shadow_dir / "before" / norm
                    if not src.exists():
                        if info.get("skipped") == "too_large":
                            result["skipped"].append({
                                "path": norm, "reason": "shadow_not_stored",
                            })
                            continue
                        result["errors"].append(f"Тень отсутствует: {norm}")
                        continue
                    abs_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, abs_path)
                    result["restored"].append(norm)
                else:
                    # Файл создавался событием → удаляем
                    if abs_path.is_file():
                        abs_path.unlink()
                        result["removed"].append(norm)
            except Exception as e:
                result["errors"].append(f"{norm}: {e}")
        return result

    # ─── Служебное ───────────────────────────────────────────
    def event_shadow_dir(self, event_id: str, day: str | None = None) -> Path | None:
        """Ищет папку теней события (день неизвестен при восстановлении)."""
        if day:
            p = self.root / day / event_id
            return p if p.exists() else None
        if not self.root.exists():
            return None
        for d in sorted(self.root.iterdir(), reverse=True):
            if not d.is_dir():
                continue
            p = d / event_id
            if p.exists():
                return p
        return None

    def day_dirs(self) -> list[Path]:
        if not self.root.exists():
            return []
        return sorted([d for d in self.root.iterdir() if d.is_dir()])

    def day_size(self, day_dir: Path) -> int:
        from .utils import dir_size
        return dir_size(day_dir)
