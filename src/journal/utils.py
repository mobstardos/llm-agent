"""Утилиты журнала: хэши, диск, маскировка секретов, безопасный ввод-вывод.

Все операции с файлами ЯВНО используют encoding="utf-8" (локаль Windows
у пользователя cp1251 — наивное чтение текста ломает кириллицу).
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import threading
from pathlib import Path
from typing import Any

# Прогрессивные блокировки записи JSONL (потокобезопасность внутри процесса)
_jsonl_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


def get_lock(path: Path) -> threading.Lock:
    key = str(path).lower()
    with _locks_guard:
        if key not in _jsonl_locks:
            _jsonl_locks[key] = threading.Lock()
        return _jsonl_locks[key]


# ─── Хэши ─────────────────────────────────────────────────────
def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8", errors="replace"))


def sha256_file(path: Path, bufsize: int = 1 << 20) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(bufsize)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return ""


def sha256_dict(d: dict) -> str:
    return sha256_text(json.dumps(d, ensure_ascii=False, sort_keys=True, default=str))


# ─── Диск ─────────────────────────────────────────────────────
def disk_usage(path: Path) -> dict:
    """Свободно/всего на диске, где лежит path (Windows-совместимо, stdlib)."""
    try:
        usage = shutil.disk_usage(str(path))
        return {
            "free_bytes": usage.free,
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_gb": round(usage.free / (1024 ** 3), 3),
            "total_gb": round(usage.total / (1024 ** 3), 3),
        }
    except Exception:
        return {"free_bytes": 0, "total_bytes": 0, "used_bytes": 0,
                "free_gb": 0.0, "total_gb": 0.0}


def dir_size(path: Path) -> int:
    total = 0
    try:
        for p in path.rglob("*"):
            try:
                if p.is_file():
                    total += p.stat().st_size
            except OSError:
                continue
    except Exception:
        pass
    return total


def human_size(n: int | float) -> str:
    n = float(n)
    for unit in ("Б", "КБ", "МБ", "ГБ", "ТБ"):
        if abs(n) < 1024.0:
            return f"{n:.1f} {unit}"
        n /= 1024.0
    return f"{n:.1f} ПБ"


# ─── Маскировка секретов ──────────────────────────────────────
def redact(obj: Any, secret_keys: tuple[str, ...]) -> Any:
    """Рекурсивно заменяет значения секретных ключей на '***'."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if isinstance(k, str) and k.lower() in secret_keys:
                out[k] = "***"
            else:
                out[k] = redact(v, secret_keys)
        return out
    if isinstance(obj, (list, tuple)):
        return [redact(x, secret_keys) for x in obj]
    return obj


# ─── Безопасные файловые операции ─────────────────────────────
def read_text_safe(path: Path, max_bytes: int | None = None) -> str:
    """Читает текст, угадывая кодировку: utf-8 → utf-8-sig → cp1251 → замена."""
    try:
        size = path.stat().st_size
    except OSError:
        return ""
    limit = size if max_bytes is None else min(size, max_bytes)
    raw = b""
    try:
        with open(path, "rb") as f:
            raw = f.read(limit)
    except OSError:
        return ""
    for enc in ("utf-8", "utf-8-sig", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def write_text_safe(path: Path, text: str, fsync: bool = False) -> bool:
    """Атомарная запись текста в utf-8: tmp → replace."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(text)
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception:
        return False


def append_jsonl(path: Path, obj: dict, fsync: bool = False) -> bool:
    """Потоковая запись JSONL с блокировкой (multi-thread, single-process)."""
    lock = get_lock(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(obj, ensure_ascii=False, default=str)
        with lock:
            with open(path, "a", encoding="utf-8", newline="") as f:
                f.write(line + "\n")
                if fsync:
                    f.flush()
                    os.fsync(f.fileno())
        return True
    except Exception:
        return False


def is_binary_ext(path: Path, binary_exts: tuple[str, ...]) -> bool:
    return path.suffix.lower() in binary_exts


def looks_binary(path: Path, probe: int = 8192) -> bool:
    """Проверка первых байт на бинарность (NUL или доля непечатаемых)."""
    try:
        with open(path, "rb") as f:
            head = f.read(probe)
    except OSError:
        return True
    if not head:
        return False
    if b"\x00" in head:
        return True
    printable = sum(b for b in head if b == 9 or b == 10 or b == 13 or 32 <= b < 127)
    return (printable / len(head)) < 0.7


def truncate(text: str, limit: int) -> str:
    if text is None or limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + f"\n...[усечено, всего {len(text)} символов]"


def env_summary() -> dict:
    """Краткая сводка окружения для системных событий."""
    return {
        "platform": os.name,
        "python": os.sys.version.split()[0] if hasattr(os.sys, "version") else "",
        "pid": os.getpid(),
    }
