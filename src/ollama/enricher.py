"""Обогащение событий малой моделью.

Для каждого события малая модель определяет:
- importance (0.0–1.0) — важность для долговременной памяти
- topic — короткая тема (1–3 слова)
- tags — авто-теги (технология, действие, область)
- reason — короткое обоснование

Это делает память умнее: важное остаётся, шум сжимается.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass

from src.ollama.client import OllamaClient

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """Ты — анализатор событий системы. Оцениваешь события по важности и извлекаешь метаданные.

Отвечай ТОЛЬКО валидным JSON-объектом."""


ENRICH_PROMPT = """Проанализируй событие:

Тип: {type}
Агент: {agent}
Сводка: {summary}
Детали: {details}

Верни JSON:
{{
  "importance": число от 0.0 до 1.0 (важность для долговременной памяти),
  "topic": краткая тема (1-3 слова, существительное),
  "tags": [массив из 1-4 тегов: технология, действие, область],
  "reason": короткое обоснование (до 80 символов)
}}

Критерии важности:
- 0.9-1.0: ключевые решения, архитектурные изменения, критические ошибки
- 0.7-0.8: успешные сложные операции, важные выводы
- 0.4-0.6: обычные операции (чтение, поиск)
- 0.0-0.3: шум, дубли, промежуточные шаги

Примеры:
- Решение "использовать alembic" → importance=0.9, topic="миграции", tags=["alembic","решение"]
- Успешный tool call read_file → importance=0.2, topic="чтение", tags=["файлы"]
- Ошибка подключения к БД → importance=0.85, topic="ошибка БД", tags=["postgres","error"]"""


@dataclass
class EnrichedEvent:
    importance: float
    topic: str
    tags: list[str]
    reason: str


class EventEnricher:
    """Обогащает события через Ollama."""

    def __init__(self, client: OllamaClient):
        self.client = client

    async def enrich(self, event: dict) -> EnrichedEvent:
        """Обогатить одно событие."""
        summary = (event.get("summary") or "")[:500]
        details = event.get("details") or {}
        if isinstance(details, dict):
            details_str = json.dumps(details, ensure_ascii=False)[:300]
        elif isinstance(details, str):
            details_str = details[:300]
        else:
            details_str = str(details)[:300]

        prompt = ENRICH_PROMPT.format(
            type=event.get("type", ""),
            agent=event.get("agent", "") or "—",
            summary=summary or "—",
            details=details_str or "—",
        )

        result = await self.client.generate_json(
            prompt, system=SYSTEM_PROMPT, temperature=0.1,
        )

        if not result:
            logger.debug("Enrich fallback для события %s", event.get("id"))
            return self._fallback(event)

        try:
            importance = float(result.get("importance", 0.5))
            importance = max(0.0, min(1.0, importance))
            topic = str(result.get("topic", "событие"))[:50]
            tags = result.get("tags") or []
            if not isinstance(tags, list):
                tags = []
            tags = [str(t)[:30] for t in tags[:4]]
            reason = str(result.get("reason", ""))[:100]

            return EnrichedEvent(
                importance=importance,
                topic=topic,
                tags=tags,
                reason=reason,
            )
        except Exception as e:
            logger.warning("Enrich parse: %s", e)
            return self._fallback(event)

    def _fallback(self, event: dict) -> EnrichedEvent:
        """Эвристика без модели (когда Ollama недоступна)."""
        type_ = (event.get("type") or "").lower()
        summary = (event.get("summary") or "").lower()

        if type_ == "error" or "ошибк" in summary or "error" in summary:
            return EnrichedEvent(
                importance=0.85,
                topic="ошибка",
                tags=["error"],
                reason="fallback: detected error",
            )
        if type_ == "decision":
            return EnrichedEvent(
                importance=0.9,
                topic="решение",
                tags=["decision"],
                reason="fallback: decision event",
            )
        if type_ == "fix":
            return EnrichedEvent(
                importance=0.75,
                topic="исправление",
                tags=["fix"],
                reason="fallback: fix event",
            )
        if type_ == "tool_call":
            return EnrichedEvent(
                importance=0.3,
                topic="tool call",
                tags=["tool", type_ or "other"],
                reason="fallback: tool call",
            )
        return EnrichedEvent(
            importance=0.4,
            topic="операция",
            tags=[type_ or "other"],
            reason="fallback: default",
        )

    async def batch_enrich(
        self, events: list[dict], concurrency: int = 4,
    ) -> list[EnrichedEvent]:
        """Батчевое обогащение с параллелизмом.

        Ollama обрабатывает последовательно на GPU, но HTTP-запросы
        параллельны. Semaphore ограничивает нагрузку.
        """
        import asyncio

        sem = asyncio.Semaphore(concurrency)

        async def one(e: dict) -> EnrichedEvent:
            async with sem:
                try:
                    return await self.enrich(e)
                except Exception as ex:
                    logger.warning("Enrich error: %s", ex)
                    return self._fallback(e)

        return await asyncio.gather(*(one(e) for e in events))
