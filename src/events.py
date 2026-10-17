"""Событийная шина (Этап 4, ARCHITECTURE-V2 §2.4).

Лёгкая asyncio-шина только на stdlib: подсистемы и фичи публикуют
события (plan.created, step.finished, tool.called, <feature>.event),
подписчики получают их без правок ядра. Журнал сегодня монкипатчит
MCPManager.call_tool — шина даёт штатный путь для новых подписчиков,
монкипатч остаётся как fallback-режим.

Правила гигиены (V2 §2.5):
- publish() никогда не бросает исключений и никогда не блокирует
  издателя: падение обработчика изолируется и логируется;
- подписка из любого места; обработчик — async-функция от dict
  {"kind", "payload", "ts"};
- кольцевой буфер последних событий — для диагностики (/api/features,
  будущая вкладка «События»), не растёт бесконечно.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

Handler = Callable[[dict], Awaitable[None]]

_subscribers: dict[str, list[Handler]] = {}
#: кольцевой буфер последних событий (диагностика)
_recent: deque = deque(maxlen=200)


def subscribe(kind: str, handler: Handler) -> None:
    """Подписывает обработчик на.kind или на все события ("*")."""
    kind = (kind or "*").strip() or "*"
    _subscribers.setdefault(kind, []).append(handler)


def unsubscribe(kind: str, handler: Handler) -> None:
    """Снимает обработчик; тихо ничего не делает, если его нет."""
    subs = _subscribers.get(kind)
    if not subs:
        return
    try:
        subs.remove(handler)
    except ValueError:
        pass
    if not subs:
        _subscribers.pop(kind, None)


def subscribers_of(kind: str) -> list[Handler]:
    """Точные подписчики kind + подписчики "*" (без дубликатов)."""
    out: list[Handler] = []
    seen: set[int] = set()
    for h in _subscribers.get(kind, []) + _subscribers.get("*", []):
        if id(h) not in seen:
            seen.add(id(h))
            out.append(h)
    return out


async def publish(kind: str, payload: dict | None = None) -> None:
    """Публикует событие всем подписчикам; НИКОГДА не бросает исключений.

    Обработчики вызываются последовательно (порядок подписки), падение
    одного не мешает остальным. Издатель не ждёт «чужих» ошибок.
    """
    event = {"kind": kind, "payload": dict(payload or {}), "ts": time.time()}
    _recent.append(event)
    handlers = subscribers_of(kind)
    if not handlers:
        return
    for handler in handlers:
        try:
            await handler(event)
        except Exception:
            logger.warning("events: подписчик упал на %s", kind, exc_info=True)


def publish_soon(kind: str, payload: dict | None = None) -> None:
    """fire-and-forget вариант publish() для синхронного кода.

    Вне работающего event loop просто пишет событие в буфер.
    """
    event = {"kind": kind, "payload": dict(payload or {}), "ts": time.time()}
    _recent.append(event)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return
    handlers = subscribers_of(kind)
    if not handlers:
        return
    task = loop.create_task(publish(kind, payload))

    def _swallow(t: asyncio.Task) -> None:  # изоляция: не пугаем loop-обработчик
        if t.done() and not t.cancelled():
            t.exception()

    task.add_done_callback(_swallow)


def recent(limit: int = 50) -> list[dict]:
    """Последние события (диагностика), от старых к новым."""
    if limit <= 0:
        return []
    return list(_recent)[-limit:]


def clear() -> None:
    """Сбрасывает подписчиков и буфер (для тестов)."""
    _subscribers.clear()
    _recent.clear()
