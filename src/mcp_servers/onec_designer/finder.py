"""Поиск 1cv8.exe / 1cv8 на системе."""
from __future__ import annotations

import logging
import os
import shutil
from pathlib import Path

logger = logging.getLogger(__name__)


def find_1cv8() -> Path | None:
    """Ищет 1cv8: env → типовые пути → PATH."""
    # 1. Env
    env_path = os.getenv("ONEC_1CV8_PATH", "").strip()
    if env_path:
        p = Path(env_path)
        if p.exists():
            return p
        logger.warning("ONEC_1CV8_PATH не существует: %s", p)

    # 2. PATH
    binary = shutil.which("1cv8") or shutil.which("1cv8.exe")
    if binary:
        return Path(binary)

    # 3. Типовые пути Windows
    if os.name == "nt":
        candidates = []
        for base in (
            r"C:\Program Files\1cv8",
            r"C:\Program Files (x86)\1cv8",
        ):
            base_p = Path(base)
            if base_p.exists():
                # Сортируем версии по убыванию
                for ver_dir in sorted(
                    base_p.iterdir(), reverse=True,
                ):
                    exe = ver_dir / "bin" / "1cv8.exe"
                    if exe.exists():
                        candidates.append(exe)
        if candidates:
            return candidates[0]

    # 4. Linux
    for base in ("/opt/1cv8/x86_64", "/opt/1cv8/i386"):
        base_p = Path(base)
        if base_p.exists():
            for ver_dir in sorted(base_p.iterdir(), reverse=True):
                exe = ver_dir / "1cv8"
                if exe.exists():
                    return exe

    return None


def find_1cv8_enterprise() -> Path | None:
    """Ищет 1cv8c (тонкий клиент) — для запуска тестов."""
    binary = shutil.which("1cv8c") or shutil.which("1cv8c.exe")
    if binary:
        return Path(binary)

    base = find_1cv8()
    if base:
        # Рядом с 1cv8.exe лежит 1cv8c.exe
        for name in ("1cv8c.exe", "1cv8c"):
            candidate = base.parent / name
            if candidate.exists():
                return candidate
    return None
