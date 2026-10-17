"""Capability Resolver: детерминированный выбор провайдера.

Правила сортировки (по порядку, каждый следующий — только при равенстве):
1. priority (из YAML, меньше = выше)
2. forced_capabilities (из runtime.yaml — побеждает всё)
3. health (healthy > degraded > unknown > unhealthy)
4. latency (меньше = лучше)
5. locality (local > ollama > openai_compatible > remote)
6. cost (free > cheap > paid)
7. alphabetical (для детерминизма)
"""
from __future__ import annotations

import logging
import shutil
import socket
import time
from typing import Any

import httpx

from src.core.schema import (
    CapabilitySchema,
    ProviderHealth,
    ProviderSchema,
)

logger = logging.getLogger(__name__)


class CapabilityResolver:
    """Разрешает конфликты между провайдерами capabilities."""

    def __init__(self, runtime_config):
        self.runtime = runtime_config

    # ═══════════════════════════════════════════════════════
    # Public API
    # ═══════════════════════════════════════════════════════
    async def resolve_all(
        self, capabilities: dict[str, CapabilitySchema],
    ) -> dict[str, dict]:
        """Возвращает {cap_id: {primary, fallback, strategy, reason, providers}}."""
        result: dict[str, dict] = {}
        for cap_id, cap in capabilities.items():
            result[cap_id] = await self.resolve_one(cap)
        return result

    async def resolve_one(self, cap: CapabilitySchema) -> dict:
        # Собираем провайдеров с health-статусами
        checked: list[tuple[ProviderSchema, str, float | None]] = []
        for p in cap.providers:
            status, latency = await self._check_provider(p)
            checked.append((p, status, latency))

        # Принудительный выбор из runtime
        forced = self.runtime.get_forced_capability(cap.id)
        if forced:
            forced_match = next(
                (c for c in checked if c[0].id == forced), None,
            )
            if forced_match:
                rest = [c for c in checked if c[0].id != forced]
                ordered = [forced_match] + self._sort_providers(rest, cap)
                return self._build(cap, ordered, reason="forced_by_user")
            logger.warning(
                "Forced provider '%s' для capability '%s' не найден",
                forced, cap.id,
            )

        # Автоматическая сортировка
        ordered = self._sort_providers(checked, cap)
        if not ordered:
            return {
                "primary": None,
                "fallback": [],
                "strategy": cap.strategy,
                "reason": "no_healthy_providers",
                "providers": [],
            }

        return self._build(cap, ordered, reason="auto_resolve")

    # ═══════════════════════════════════════════════════════
    # Sorting
    # ═══════════════════════════════════════════════════════
    def _sort_providers(
        self,
        items: list[tuple[ProviderSchema, str, float | None]],
        cap: CapabilitySchema,
    ) -> list[tuple[ProviderSchema, str, float | None]]:
        # Отбрасываем unhealthy
        healthy = [
            i for i in items
            if i[1] != ProviderHealth.UNHEALTHY.value
        ]

        def key(item):
            p, status, latency = item
            keys: list[Any] = [p.priority]
            for rule in cap.tie_breaker:
                if rule == "health":
                    keys.append(self._health_rank(status))
                elif rule == "latency":
                    keys.append(latency if latency is not None else 999999.0)
                elif rule == "locality":
                    keys.append(self._locality_rank(p.type))
                elif rule == "cost":
                    keys.append(self._cost_rank(p.cost_hint))
                elif rule == "alphabetical":
                    keys.append(p.id)
                elif rule == "priority":
                    pass  # уже добавлено
            return tuple(keys)

        healthy.sort(key=key)
        return healthy

    @staticmethod
    def _health_rank(status: str) -> int:
        return {
            ProviderHealth.HEALTHY.value: 0,
            ProviderHealth.DEGRADED.value: 1,
            ProviderHealth.UNKNOWN.value: 2,
            ProviderHealth.UNHEALTHY.value: 3,
        }.get(status, 4)

    @staticmethod
    def _locality_rank(type_: str) -> int:
        return {
            "local": 0, "ollama": 1,
            "openai_compatible": 2, "remote": 3,
        }.get(type_, 4)

    @staticmethod
    def _cost_rank(hint: str) -> int:
        return {"free": 0, "cheap": 1, "paid": 2}.get(hint, 3)

    # ═══════════════════════════════════════════════════════
    # Build
    # ═══════════════════════════════════════════════════════
    def _build(
        self,
        cap: CapabilitySchema,
        ordered: list[tuple[ProviderSchema, str, float | None]],
        reason: str,
    ) -> dict:
        primary = ordered[0]
        fallback = ordered[1:]
        return {
            "primary": primary[0].id,
            "fallback": [p.id for p, _, _ in fallback],
            "strategy": cap.strategy,
            "reason": reason,
            "providers": [
                {
                    "id": p.id,
                    "status": s,
                    "latency_ms": round(l, 1) if l is not None else None,
                }
                for p, s, l in ordered
            ],
        }

    # ═══════════════════════════════════════════════════════
    # Provider health
    # ═══════════════════════════════════════════════════════
    async def _check_provider(
        self, p: ProviderSchema,
    ) -> tuple[str, float | None]:
        if not p.health:
            return ProviderHealth.UNKNOWN.value, None

        t0 = time.perf_counter()
        try:
            if p.health.method == "http_get" and p.health.url:
                resp = httpx.get(p.health.url, timeout=p.health.timeout)
                latency = (time.perf_counter() - t0) * 1000
                if 200 <= resp.status_code < 400:
                    return ProviderHealth.HEALTHY.value, latency
                return ProviderHealth.DEGRADED.value, latency

            if p.health.method == "which" and p.health.binary:
                found = shutil.which(p.health.binary) is not None
                return (
                    ProviderHealth.HEALTHY.value if found
                    else ProviderHealth.UNHEALTHY.value
                ), None

            if p.health.method == "tcp" and p.health.host and p.health.port:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(p.health.timeout)
                    rc = s.connect_ex((p.health.host, p.health.port))
                    latency = (time.perf_counter() - t0) * 1000
                    if rc == 0:
                        return ProviderHealth.HEALTHY.value, latency
                    return ProviderHealth.UNHEALTHY.value, None

        except Exception as e:
            logger.debug("Health check для %s упал: %s", p.id, e)
            return ProviderHealth.UNHEALTHY.value, None

        return ProviderHealth.UNKNOWN.value, None
