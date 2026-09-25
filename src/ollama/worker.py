"""Фоновый воркер обогащения событий.

Логика:
1. Проверить Ollama health
2. Получить advisory lock (только один инстанс работает)
3. Подписаться на LISTEN new_events
4. При событии или раз в interval_seconds — батч unenriched
5. Обогатить через малую модель
6. Записать обратно в memory.events
"""
from __future__ import annotations

import asyncio
import logging
import time

from src.db.multi_instance import AdvisoryLock
from src.db.pool import DatabasePool
from src.ollama.client import OllamaClient
from src.ollama.enricher import EventEnricher

logger = logging.getLogger(__name__)


class EnrichmentWorker:
    """Фоновое обогащение событий через Ollama."""

    def __init__(
        self,
        pool: DatabasePool,
        ollama: OllamaClient,
        *,
        batch_size: int = 50,
        interval_seconds: float = 30.0,
        concurrency: int = 4,
        min_importance_to_index: float = 0.5,
    ):
        self.pool = pool
        self.ollama = ollama
        self.enricher = EventEnricher(ollama)
        self.batch_size = batch_size
        self.interval_seconds = interval_seconds
        self.concurrency = concurrency
        self.min_importance = min_importance_to_index

        self._lock = AdvisoryLock(pool, "enrichment_worker")
        self._running = False
        self._task: asyncio.Task | None = None
        self._wakeup = asyncio.Event()

        # Метрики
        self.processed_total = 0
        self.last_batch_size = 0
        self.last_batch_at = 0.0

    # ═══════════════════════════════════════════════════════
    # Lifecycle
    # ═══════════════════════════════════════════════════════
    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(
            "EnrichmentWorker started (batch=%d, interval=%.0fs, model=%s)",
            self.batch_size, self.interval_seconds, self.ollama.model,
        )

    async def stop(self) -> None:
        self._running = False
        self._wakeup.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        await self._lock.release()
        logger.info("EnrichmentWorker stopped")

    def wakeup(self) -> None:
        """Разбудить воркер (при получении NOTIFY)."""
        self._wakeup.set()

    # ═══════════════════════════════════════════════════════
    # Main loop
    # ═══════════════════════════════════════════════════════
    async def _run_loop(self) -> None:
        """Основной цикл воркера."""
        # 1. Ждём Ollama
        if not await self.ollama.health():
            logger.warning(
                "Ollama недоступна. Воркер ждёт. Установите: ollama pull %s",
                self.ollama.model,
            )
            while self._running and not await self.ollama.health():
                await asyncio.sleep(60)
            if not self._running:
                return
            logger.info("Ollama доступна")

        # 2. Advisory lock
        acquired = await self._lock.try_acquire()
        if not acquired:
            logger.info("Enrichment lock занят другим инстансом, ждём...")
            while self._running and not acquired:
                await asyncio.sleep(30)
                acquired = await self._lock.try_acquire()
            if not self._running:
                return
            logger.info("Advisory lock acquired")

        # 3. Основной цикл
        last_batch = 0.0

        while self._running:
            try:
                # Ждём сигнал или таймаут
                try:
                    await asyncio.wait_for(
                        self._wakeup.wait(),
                        timeout=self.interval_seconds,
                    )
                    self._wakeup.clear()
                    # Небольшая задержка для накопления батча
                    await asyncio.sleep(2.0)
                except asyncio.TimeoutError:
                    pass

                # Если недавно обрабатывали — пропускаем
                now = time.time()
                if now - last_batch < 3.0:
                    continue

                # Обрабатываем
                processed = await self._process_batch()
                if processed > 0:
                    last_batch = time.time()
                    self.processed_total += processed
                    self.last_batch_size = processed
                    self.last_batch_at = last_batch
                    logger.info(
                        "Enriched %d events (total: %d)",
                        processed, self.processed_total,
                    )

            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.exception("Enrichment loop error: %s", e)
                await asyncio.sleep(10)

    async def _process_batch(self) -> int:
        """Обработать один батч unenriched событий."""
        # Получить с FOR UPDATE SKIP LOCKED — безопасно для multi-instance
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    SELECT id, type, agent, summary, details, created_at
                    FROM memory.events
                    WHERE enriched_at IS NULL
                      AND created_at > now() - interval '7 days'
                    ORDER BY created_at ASC
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                """, (self.batch_size,))
                events = await cur.fetchall()

        if not events:
            return 0

        # Обогатить
        try:
            enriched = await self.enricher.batch_enrich(
                events, concurrency=self.concurrency,
            )
        except Exception as e:
            logger.exception("Batch enrich failed: %s", e)
            return 0

        # Записать обратно
        async with self.pool.transaction() as conn:
            async with conn.cursor() as cur:
                for event, e in zip(events, enriched):
                    await cur.execute("""
                        UPDATE memory.events
                        SET importance = %s, topic = %s, tags = %s,
                            enriched_at = now()
                        WHERE id = %s
                    """, (e.importance, e.topic, e.tags, event["id"]))

        return len(events)

    # ═══════════════════════════════════════════════════════
    # Stats
    # ═══════════════════════════════════════════════════════
    def stats(self) -> dict:
        return {
            "running": self._running,
            "model": self.ollama.model,
            "batch_size": self.batch_size,
            "interval_seconds": self.interval_seconds,
            "processed_total": self.processed_total,
            "last_batch_size": self.last_batch_size,
            "last_batch_at": self.last_batch_at,
            "lock_held": self._lock._held,
        }

    async def queue_size(self) -> int:
        """Сколько событий ждёт обработки."""
        rows = await self.pool.execute("""
            SELECT COUNT(*) AS c
            FROM memory.events
            WHERE enriched_at IS NULL
              AND created_at > now() - interval '7 days'
        """)
        return rows[0]["c"] if rows else 0
