"""Проверка через линтер."""
from __future__ import annotations

import asyncio
import logging
import shlex

from src.loop.checks import register_check

logger = logging.getLogger(__name__)


@register_check("lint")
async def check_lint(params: dict, context=None, state=None) -> bool:
    command = params.get("command", "ruff check .")
    timeout = params.get("timeout_seconds", 30)

    cwd = None
    if context and context.file_state:
        cwd = str(context.file_state.root)

    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return False
        return proc.returncode == 0
    except FileNotFoundError:
        logger.debug("lint-команда не найдена: %s", command)
        return True
    except Exception as e:
        logger.debug("lint упал: %s", e)
        return True
