# -*- coding: utf-8 -*-
"""Whitelist рабочих папок проекта (Фаза 3).

WORKSPACE_ROOTS в .env — список разрешённых корней (`;` или `,`-список
абсолютных путей). Пустой список = разрешён только текущий PROJECT_ROOT.

Внутри корня агенту доступны подпапки, в т.ч. создаваемые автоматически:
  .inbox/  — входящие вложения пользователя (upload из чата)
  outputs/ — результаты работы агентов
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

INBOX_DIR = ".inbox"
OUTPUTS_DIR = "outputs"


def _split_roots(raw: str) -> list[str]:
    """Разбивает строку WORKSPACE_ROOTS на отдельные пути."""
    parts: list[str] = []
    for chunk in raw.replace(",", ";").split(";"):
        s = chunk.strip().strip('"').strip("'")
        if s:
            parts.append(s)
    return parts


def workspace_roots(default_root: str = "") -> list[Path]:
    """Список разрешённых корней (нормализованные, существующие папки).

    Всегда включает default_root (текущий PROJECT_ROOT) — даже если
    WORKSPACE_ROOTS не задан. Несуществующие пути из env молча пропускаются
    (с warning), чтобы битый .env не блокировал работу.
    """
    raw = os.getenv("WORKSPACE_ROOTS", "").strip()
    seen: set[str] = set()
    out: list[Path] = []

    def _add(p: str | Path) -> None:
        try:
            resolved = Path(p).expanduser().resolve()
        except (OSError, RuntimeError):
            logger.warning("WORKSPACE_ROOTS: не разобрать путь %r", p)
            return
        key = str(resolved).lower()
        if key in seen:
            return
        seen.add(key)
        out.append(resolved)

    if default_root:
        _add(default_root)
    for s in _split_roots(raw):
        p = Path(s).expanduser()
        if p.is_dir():
            _add(p)
        else:
            logger.warning("WORKSPACE_ROOTS: пропущена несуществующая "
                           "папка %r", s)
    return out


def is_allowed(path: str | Path, default_root: str = "") -> bool:
    """Путь внутри любого из разрешённых корней?"""
    try:
        resolved = Path(path).expanduser().resolve()
    except (OSError, RuntimeError):
        return False
    for root in workspace_roots(default_root):
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def ensure_workspace_dirs(root: str | Path) -> list[str]:
    """Создаёт .inbox/ и outputs/ внутри корня (best-effort).

    Возвращает созданные пути; ошибки логируются, но не поднимают
    исключений (нет прав — не критично для чтения).
    """
    made: list[str] = []
    base = Path(root)
    for name in (INBOX_DIR, OUTPUTS_DIR):
        d = base / name
        try:
            d.mkdir(parents=True, exist_ok=True)
            made.append(str(d))
        except OSError as e:
            logger.warning("Не создать %s: %s", d, e)
    return made


def workspace_prompt_line(default_root: str = "") -> str:
    """Строка для system prompt: текущая рабочая папка и её структура."""
    raw = os.getenv("PROJECT_ROOT", "").strip() or default_root
    if not raw:
        return ""
    root = Path(raw).expanduser().resolve()
    return (f"Рабочая папка проекта: {root} "
            f"(входящие файлы — {INBOX_DIR}/, результаты — {OUTPUTS_DIR}/). "
            "Не работай с файлами вне этой папки.")
