"""Проверка на запрещённые паттерны в файлах."""
from __future__ import annotations

import logging
from pathlib import Path

from src.loop.checks import register_check

logger = logging.getLogger(__name__)


@register_check("todos")
async def check_todos(params: dict, context=None, state=None) -> bool:
    """Ищет forbidden-паттерны в изменённых файлах."""
    forbid = params.get("forbid", ["TODO", "FIXME"])
    if not forbid:
        return True

    if not context or not context.file_state:
        return True
    root = Path(context.file_state.root)
    if not root.exists():
        return True

    # Ищем только в .py/.js/.ts/.md
    exts = {".py", ".js", ".ts", ".tsx", ".jsx", ".md"}
    ignore_dirs = {".git", ".venv", "venv", "__pycache__",
                   "node_modules", ".idea", ".vscode", "dist", "build", "data"}

    hits: list[str] = []
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix.lower() not in exts:
            continue
        if any(part in ignore_dirs for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for pattern in forbid:
            if pattern in text:
                hits.append(f"{path.relative_to(root)}: {pattern}")
                break

    if hits:
        logger.info("todos checker нашёл: %d файлов", len(hits))
        return False
    return True
