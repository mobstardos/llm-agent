"""Фоновые задачи: периодические, ежедневные."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)


class BackgroundScheduler:
    def __init__(self):
        self._tasks: list[asyncio.Task] = []

    def start(self) -> None:
        pass

    def stop(self) -> None:
        for t in self._tasks:
            t.cancel()
        self._tasks.clear()

    def add_periodic(
        self,
        name: str,
        interval_seconds: float,
        fn: Callable[[], Awaitable[None]],
        initial_delay_seconds: float = 0,
    ) -> None:
        async def loop():
            try:
                if initial_delay_seconds > 0:
                    await asyncio.sleep(initial_delay_seconds)
                while True:
                    try:
                        await fn()
                    except asyncio.CancelledError:
                        return
                    except Exception as e:
                        logger.exception("[%s] ошибка: %s", name, e)
                    await asyncio.sleep(interval_seconds)
            except asyncio.CancelledError:
                return

        self._tasks.append(asyncio.create_task(loop(), name=name))
        logger.info(
            "Фоновая задача '%s' запущена (интервал %ss)",
            name, interval_seconds,
        )

    def add_daily(
        self,
        name: str,
        hour: int,
        fn: Callable[[], Awaitable[None]],
    ) -> None:
        async def loop():
            try:
                while True:
                    try:
                        now = datetime.now()
                        target = now.replace(
                            hour=hour, minute=0, second=0, microsecond=0,
                        )
                        if target <= now:
                            delay = (
                                target.timestamp() - now.timestamp() + 86400
                            )
                        else:
                            delay = target.timestamp() - now.timestamp()
                        await asyncio.sleep(delay)
                        await fn()
                    except asyncio.CancelledError:
                        return
                    except Exception as e:
                        logger.exception("[%s] ошибка: %s", name, e)
                        await asyncio.sleep(3600)
            except asyncio.CancelledError:
                return

        self._tasks.append(asyncio.create_task(loop(), name=name))
        logger.info("Ежедневная задача '%s' запущена на %02d:00", name, hour)
