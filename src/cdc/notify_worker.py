"""CDC через LISTEN/NOTIFY → Kafka.

PostgreSQL сам шлёт NOTIFY при изменениях.
Worker ловит их и публикует в Kafka.
"""
from __future__ import annotations

import asyncio
import json
import logging

from src.cdc.kafka_publisher import KafkaPublisher
from src.db.pool import DatabasePool

logger = logging.getLogger(__name__)


class NotifyCDCWorker:
    """Слушает PostgreSQL каналы и публикует в Kafka."""

    def __init__(
        self,
        pool: DatabasePool,
        publisher: KafkaPublisher,
        channels: list[str] | None = None,
        topic_prefix: str = "llmagent.cdc",
    ):
        self.pool = pool
        self.publisher = publisher
        self.channels = channels or [
            "new_events",
            "session_created",
            "session_ended",
            "tool_called",
            "important_event",
            "approval_decision",
        ]
        self.topic_prefix = topic_prefix

        self._tasks: list[asyncio.Task] = []
        self._running = False

        # Метрики
        self.received_total = 0
        self.published_total = 0
        self.parse_errors = 0

    async def start(self) -> None:
        if self._running:
            return
        self._running = True

        # Подключение Kafka
        if not await self.publisher.connect():
            logger.warning("Kafka unavailable, CDC will buffer")

        # Старт по одному listener'у на канал
        for channel in self.channels:
            task = asyncio.create_task(self._listen_channel(channel))
            self._tasks.append(task)
            logger.info("CDC listening: %s", channel)

    async def stop(self) -> None:
        self._running = False
        for t in self._tasks:
            t.cancel()
        for t in self._tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass
        self._tasks.clear()
        await self.publisher.disconnect()
        logger.info("CDC worker stopped")

    async def _listen_channel(self, channel: str) -> None:
        """Слушает один канал, публикует в Kafka."""
        while self._running:
            try:
                async with self.pool.connection() as conn:
                    await conn.set_autocommit(True)
                    async with conn.cursor() as cur:
                        await cur.execute(f"LISTEN {channel}")

                    logger.debug("Subscribed: %s", channel)

                    async for notify in conn.notifies():
                        if not self._running:
                            return
                        await self._handle_notify(channel, notify.payload)
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("CDC channel %s died: %s", channel, e)
                await asyncio.sleep(5)

    async def _handle_notify(self, channel: str, payload: str) -> None:
        """Обрабатывает одно уведомление."""
        self.received_total += 1

        # Парсинг payload (обычно JSON)
        parsed: Any = payload
        try:
            parsed = json.loads(payload)
        except (json.JSONDecodeError, TypeError):
            # Не JSON — оставляем как строку
            self.parse_errors += 1

        # Публикация в Kafka
        topic = f"{self.topic_prefix}.{channel}"
        ok = await self.publisher.publish(
            topic=topic,
            key=channel,
            value={
                "channel": channel,
                "payload": parsed,
                "source": "postgres_notify",
            },
        )
        if ok:
            self.published_total += 1

    def stats(self) -> dict:
        return {
            "running": self._running,
            "channels": self.channels,
            "received_total": self.received_total,
            "published_total": self.published_total,
            "parse_errors": self.parse_errors,
            "publisher": self.publisher.stats(),
        }


# Импорт для type-hint
from typing import Any  # noqa: E402
