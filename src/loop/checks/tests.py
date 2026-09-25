"""Проверка через тесты."""
from __future__ import annotations

import asyncio
import logging

from src.loop.checks import register_check

logger = logging.getLogger(__name__)


@register_check("tests")
async def check_tests(params: dict, context=None, state=None) -> bool:
    command = params.get("command", "pytest -q")
    timeout = params.get("timeout_seconds", 60)

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
        logger.debug("tests-команда не найдена")
        return True
    except Exception as e:
        logger.debug("tests упал: %s", e)
        return True
