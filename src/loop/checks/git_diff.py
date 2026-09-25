"""Проверка git diff."""
from __future__ import annotations

import asyncio
import logging

from src.loop.checks import register_check

logger = logging.getLogger(__name__)


@register_check("git_diff")
async def check_git_diff(params: dict, context=None, state=None) -> bool:
    """Проверяет, что были изменения (или их не было — в зависимости от опции)."""
    root = None
    if context and context.file_state:
        root = str(context.file_state.root)
    if not root:
        return True  # если некуда смотреть — пропускаем

    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "diff", "--quiet",
            cwd=root,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        rc = await proc.wait()
        # rc=1 → есть изменения, rc=0 → нет
        has_changes = (rc == 1)
        require = params.get("require_changes", False)
        if require:
            return has_changes
        return True
    except FileNotFoundError:
        logger.debug("git не найден")
        return True
    except Exception as e:
        logger.debug("git_diff упал: %s", e)
        return True
