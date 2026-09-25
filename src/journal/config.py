"""Конфигурация журнала: окружение (.env) + config/settings.yaml.

Приоритет: переменные окружения JOURNAL_* → settings.yaml → значения по умолчанию.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1", "true", "yes", "on", "д", "да")


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


@dataclass
class JournalConfig:
    # ─── Расположение ────────────────────────────────────────
    # data_dir = <project_root>/data/journal
    base_dir: Path = Path("data/journal")

    # ─── Что записывать ──────────────────────────────────────
    enabled: bool = True
    record_results: bool = True          # сохранять результат tool call в JSONL
    max_result_chars: int = 200_000      # усечение результата в JSONL
    record_args: bool = True             # сохранять аргументы (с маскировкой)
    record_reads: bool = False           # записывать read-инструменты (много шума)
    fsync: bool = False                  # fsync после каждой записи JSONL (медленнее, надёжнее)

    # Секреты: значения ключей с такими именами маскируются
    redact_keys: tuple[str, ...] = (
        "password", "passwd", "secret", "api_key", "apikey", "token",
        "authorization", "cookie", "session", "dsn", "connection_string",
    )

    # ─── Тени файлов ─────────────────────────────────────────
    shadows_enabled: bool = True
    shadow_max_file_mb: float = 50.0     # не тенить файлы больше этого размера
    shadow_binary_exts: tuple[str, ...] = (
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf",
        ".zip", ".7z", ".rar", ".gz", ".tar", ".exe", ".dll", ".so",
        ".pyc", ".class", ".jar", ".mp3", ".mp4", ".avi", ".mov", ".sqlite",
        ".db", ".mdb", ".1cd",
    )

    # ─── Наблюдение за проектом (внешние изменения) ──────────
    watch_project: bool = False          # включать сканер внешних изменений
    watch_interval_seconds: float = 60.0
    watch_exclude_dirs: tuple[str, ...] = (
        ".git", ".venv", "venv", "__pycache__", "node_modules",
        "data", ".idea", ".vscode", "dist", "build",
    )

    # ─── Ретенция: «память копится, пока есть место» ─────────
    min_free_gb: float = 5.0             # жёсткий порог свободного места на диске
    max_share_percent: float = 0.0       # макс. % диска под журнал (0 = без лимита)
    compress_after_days: int = 3         # тени старше N дней сжимаются в zip
    sweep_interval_seconds: int = 1800   # период фоновой проверки ретенции

    # ─── Отчёты ──────────────────────────────────────────────
    reports_enabled: bool = True         # markdown-отчёты по сессиям/задачам

    # ─── Прочее ──────────────────────────────────────────────
    jsonl_retention_days: int = 0        # 0 = JSONL не удалять никогда (сжимается при ретенции)

    # ─── Инструменты, для которых тени обязательны ───────────
    # "server.tool" или "*" сервера. Определяет, какие аргументы — файлы.
    file_tools: tuple[str, ...] = (
        "filesystem.write_file",
        "filesystem.apply_patch",
        "filesystem.delete_file",
        "filesystem_ext.*",
        "git.*",
        "shell.*",
    )

    # Ключи аргументов, в которых ищутся пути к файлам
    path_arg_keys: tuple[str, ...] = (
        "path", "file_path", "filepath", "target", "dest", "dst",
        "source", "src", "from", "to", "old_path", "new_path",
    )

    # ─────────────────────────────────────────────────────────
    @classmethod
    def from_env(cls, base_dir: str | Path | None = None) -> "JournalConfig":
        cfg = cls()
        if base_dir is not None:
            cfg.base_dir = Path(base_dir)
        else:
            cfg.base_dir = Path(
                _env("JOURNAL_DIR", "data/journal")
            )
        cfg.enabled = _env_bool("JOURNAL_ENABLED", True)
        cfg.record_results = _env_bool("JOURNAL_RECORD_RESULTS", True)
        cfg.record_args = _env_bool("JOURNAL_RECORD_ARGS", True)
        cfg.record_reads = _env_bool("JOURNAL_RECORD_READS", False)
        cfg.fsync = _env_bool("JOURNAL_FSYNC", False)
        cfg.max_result_chars = _env_int("JOURNAL_MAX_RESULT_CHARS", 200_000)

        cfg.shadows_enabled = _env_bool("JOURNAL_SHADOWS", True)
        cfg.shadow_max_file_mb = _env_float("JOURNAL_SHADOW_MAX_MB", 50.0)

        cfg.watch_project = _env_bool("JOURNAL_WATCH_PROJECT", False)
        cfg.watch_interval_seconds = _env_float("JOURNAL_WATCH_INTERVAL", 60.0)

        cfg.min_free_gb = _env_float("JOURNAL_MIN_FREE_GB", 5.0)
        cfg.max_share_percent = _env_float("JOURNAL_MAX_SHARE_PERCENT", 0.0)
        cfg.compress_after_days = _env_int("JOURNAL_COMPRESS_AFTER_DAYS", 3)
        cfg.sweep_interval_seconds = _env_int("JOURNAL_SWEEP_INTERVAL", 1800)
        cfg.jsonl_retention_days = _env_int("JOURNAL_JSONL_RETENTION_DAYS", 0)

        cfg.reports_enabled = _env_bool("JOURNAL_REPORTS", True)
        return cfg

    def apply_yaml(self, section: dict) -> None:
        """Наложить секцию journal: из config/settings.yaml (env важнее)."""
        if not isinstance(section, dict):
            return
        m = {
            "enabled": ("JOURNAL_ENABLED", None),
        }
        # Простые поля (env имеет приоритет — не перетираем, если задан)
        direct = {
            "record_reads": "JOURNAL_RECORD_READS",
            "watch_project": "JOURNAL_WATCH_PROJECT",
            "fsync": "JOURNAL_FSYNC",
        }
        for key, env_name in direct.items():
            if key in section and env_name not in os.environ:
                setattr(self, key, bool(section[key]))
        if "min_free_gb" in section and "JOURNAL_MIN_FREE_GB" not in os.environ:
            self.min_free_gb = float(section["min_free_gb"])
        if "max_share_percent" in section and "JOURNAL_MAX_SHARE_PERCENT" not in os.environ:
            self.max_share_percent = float(section["max_share_percent"])
        if "compress_after_days" in section and "JOURNAL_COMPRESS_AFTER_DAYS" not in os.environ:
            self.compress_after_days = int(section["compress_after_days"])
        if "max_result_chars" in section and "JOURNAL_MAX_RESULT_CHARS" not in os.environ:
            self.max_result_chars = int(section["max_result_chars"])
        if isinstance(section.get("file_tools"), list):
            self.file_tools = tuple(section["file_tools"])
        if isinstance(section.get("redact_keys"), list):
            self.redact_keys = tuple(section["redact_keys"])
        del m

    # ─── Пути ────────────────────────────────────────────────
    @property
    def db_path(self) -> Path:
        return self.base_dir / "journal.sqlite"

    @property
    def jsonl_dir(self) -> Path:
        return self.base_dir / "events"

    @property
    def shadows_dir(self) -> Path:
        return self.base_dir / "shadows"

    @property
    def archives_dir(self) -> Path:
        return self.base_dir / "archives"

    @property
    def reports_dir(self) -> Path:
        return self.base_dir / "reports"

    @property
    def payloads_dir(self) -> Path:
        return self.base_dir / "payloads"
