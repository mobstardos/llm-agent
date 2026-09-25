"""Kafka publisher с буферизацией.

Если Kafka недоступна — буфер в памяти. При восстановлении — flush.
Это защищает от потери событий при кратких сбоях Kafka.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)


class KafkaPublisher:
    """Асинхронный publisher с буфером и auto-reconnect."""

    def __init__(
        self,
        bootstrap_servers: str | None = None,
        client_id: str = "llmagent-cdc",
        default_topic: str = "llmagent.cdc.default",
        buffer_size: int = 10000,
        flush_interval_seconds: float = 30.0,
    ):
        self.bootstrap_servers = (
            bootstrap_servers
            or os.getenv("KAFKA_BOOTSTRAP_SERVERS", "localhost:9092")
        )
        self.client_id = client_id
        self.default_topic = default_topic
        self.buffer_size = buffer_size
        self.flush_interval = flush_interval_seconds

        self._producer = None
        self._buffer: list[tuple[str, dict]] = []
        self._available = False
        self._flush_task: asyncio.Task | None = None
        self._running = False

        # Метрики
        self.sent_total = 0
        self.dropped_total = 0

    # ═══════════════════════════════════════════════════════
    # Lifecycle
    # ═══════════════════════════════════════════════════════
    async def connect(self) -> bool:
        try:
            from aiokafka import AIOKafkaProducer
        except ImportError:
            logger.warning("aiokafka не установлен: pip install aiokafka")
            return False

        try:
            self._producer = AIOKafkaProducer(
                bootstrap_servers=self.bootstrap_servers,
                client_id=self.client_id,
                value_serializer=lambda v: json.dumps(
                    v, ensure_ascii=False, default=str,
                ).encode("utf-8"),
                key_serializer=lambda k: k.encode("utf-8") if k else None,
                acks="all",
                compression_type="gzip",
                linger_ms=10,
                request_timeout_ms=5000,
                max_batch_size=16384,
            )
            await self._producer.start()
            self._available = True
            self._running = True
            logger.info("Kafka connected: %s", self.bootstrap_servers)

            # Запустить flush loop
            if self._flush_task is None:
                self._flush_task = asyncio.create_task(self._flush_loop())

            # Первый flush буфера
            await self._flush_buffer()
            return True
        except Exception as e:
            logger.warning("Kafka connect failed: %s", e)
            self._available = False
            return False

    async def disconnect(self) -> None:
        self._running = False
        if self._flush_task:
            self._flush_task.cancel()
            try:
                await self._flush_task
            except (asyncio.CancelledError, Exception):
                pass
            self._flush_task = None

        if self._producer:
            try:
                await self._flush_buffer()
                await self._producer.stop()
            except Exception:
                pass
            self._producer = None
            self._available = False

    async def _flush_loop(self) -> None:
        """Периодически пытаемся слить буфер (если Kafka недоступна)."""
        while self._running:
            await asyncio.sleep(self.flush_interval)
            if not self._available:
                # Попробовать переподключиться
                if await self.connect():
                    logger.info("Kafka reconnected")
            else:
                await self._flush_buffer()

    # ═══════════════════════════════════════════════════════
    # Publish
    # ═══════════════════════════════════════════════════════
    async def publish(
        self,
        topic: str | None = None,
        key: str | None = None,
        value: dict | None = None,
    ) -> bool:
        if not value:
            return False

        topic = topic or self.default_topic

        if not self._available or self._producer is None:
            # Буферизация
            if len(self._buffer) < self.buffer_size:
                self._buffer.append((topic, {"key": key, "value": value}))
                return False
            else:
                self.dropped_total += 1
                logger.warning("Kafka buffer full, dropping message")
                return False

        try:
            await self._producer.send_and_wait(
                topic, value=value, key=key,
            )
            self.sent_total += 1
            return True
        except Exception as e:
            logger.warning("Kafka publish: %s", e)
            self._available = False
            if len(self._buffer) < self.buffer_size:
                self._buffer.append((topic, {"key": key, "value": value}))
            else:
                self.dropped_total += 1
            return False

    async def _flush_buffer(self) -> int:
        if not self._producer or not self._buffer:
            return 0

        sent = 0
        remaining: list[tuple[str, dict]] = []

        for topic, item in self._buffer:
            try:
                await self._producer.send_and_wait(
                    topic, value=item["value"], key=item.get("key"),
                )
                sent += 1
                self.sent_total += 1
            except Exception:
                remaining.append((topic, item))

        self._buffer = remaining
        if sent:
            logger.info(
                "Kafka flushed %d buffered messages (%d remaining)",
                sent, len(remaining),
            )
        return sent

    # ═══════════════════════════════════════════════════════
    # Stats
    # ═══════════════════════════════════════════════════════
    def stats(self) -> dict:
        return {
            "available": self._available,
            "bootstrap": self.bootstrap_servers,
            "buffer_size": len(self._buffer),
            "buffer_capacity": self.buffer_size,
            "sent_total": self.sent_total,
            "dropped_total": self.dropped_total,
        }

    async def health(self) -> bool:
        try:
            if self._producer is None:
                return False
            # Простая проверка через partitioner
            partitions = await self._producer.partitions_for(
                self.default_topic,
            )
            return partitions is not None
        except Exception:
            return False
