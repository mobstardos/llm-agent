"""Реестр чекеров для verification loop."""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_CHECKS: dict[str, Any] = {}


def register_check(name: str):
    def deco(fn):
        _CHECKS[name] = fn
        return fn
    return deco


async def run_check(
    name: str, params: dict, context=None, state=None,
) -> bool:
    """Возвращает True если проверка прошла."""
    fn = _CHECKS.get(name)
    if fn is None:
        logger.warning("Неизвестный чекер: %s", name)
        return True   # неизвестный checker не валит проверку
    try:
        return await fn(params, context=context, state=state)
    except Exception as e:
        logger.exception("Checker %s упал: %s", name, e)
        return False


# Импортируем встроенные чекеры для регистрации
def _bootstrap():
    from src.loop.checks import git_diff, lint, tests, todos  # noqa: F401


_bootstrap()
