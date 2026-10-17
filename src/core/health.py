"""Проверка требований агентов и MCP: пакеты, env, пути, TCP, capabilities."""
from __future__ import annotations

import importlib.util
import logging
import os
import shutil
import socket
import time
from pathlib import Path

import httpx

from src.core.schema import (
    CapabilityRequirement,
    EnvRequirement,
    ExternalRequirement,
    PackageRequirement,
    PathRequirement,
    ProviderHealth,
    RequirementLevel,
)

logger = logging.getLogger(__name__)


class RequirementChecker:
    """Проверяет требования деклараций."""

    def __init__(self, runtime_config):
        self.runtime = runtime_config
        self._pkg_cache: dict[str, bool] = {}

    # ═══════════════════════════════════════════════════════
    # Packages
    # ═══════════════════════════════════════════════════════
    def check_packages(
        self, reqs: list[PackageRequirement],
    ) -> tuple[list[str], list[str]]:
        missing_hard: list[str] = []
        missing_soft: list[str] = []
        for r in reqs:
            available = self._check_pkg(r.name)
            if not available:
                msg = r.message or f"Пакет '{r.name}' не установлен"
                if r.level == RequirementLevel.HARD:
                    missing_hard.append(msg)
                else:
                    missing_soft.append(msg)
        return missing_hard, missing_soft

    def _check_pkg(self, name: str) -> bool:
        if name in self._pkg_cache:
            return self._pkg_cache[name]
        try:
            spec = importlib.util.find_spec(name)
            result = spec is not None
        except (ImportError, ValueError):
            result = False
        self._pkg_cache[name] = result
        return result

    # ═══════════════════════════════════════════════════════
    # Env
    # ═══════════════════════════════════════════════════════
    def check_env(
        self, reqs: list[EnvRequirement],
    ) -> tuple[list[str], list[str]]:
        missing_hard: list[str] = []
        missing_soft: list[str] = []
        for r in reqs:
            if not os.getenv(r.name, "").strip():
                msg = r.message or f"Переменная '{r.name}' не задана"
                if r.level == RequirementLevel.HARD:
                    missing_hard.append(msg)
                else:
                    missing_soft.append(msg)
        return missing_hard, missing_soft

    # ═══════════════════════════════════════════════════════
    # Paths
    # ═══════════════════════════════════════════════════════
    def check_paths(
        self, reqs: list[PathRequirement],
    ) -> tuple[list[str], list[str]]:
        missing_hard: list[str] = []
        missing_soft: list[str] = []
        for r in reqs:
            raw = r.path
            if r.env:
                raw = os.getenv(r.env, "")
            if not raw:
                msg = r.message or f"Путь '{r.name}' не задан"
                self._add_missing(r.level, msg, missing_hard, missing_soft)
                continue

            p = Path(raw).expanduser()
            if not p.exists():
                msg = r.message or f"Путь не существует: {p}"
                self._add_missing(r.level, msg, missing_hard, missing_soft)
                continue

            if r.type == "dir" and not p.is_dir():
                msg = r.message or f"Это не папка: {p}"
                self._add_missing(r.level, msg, missing_hard, missing_soft)
                continue

            if r.type == "file" and not p.is_file():
                msg = r.message or f"Это не файл: {p}"
                self._add_missing(r.level, msg, missing_hard, missing_soft)
                continue

            if r.writable and not os.access(p, os.W_OK):
                msg = r.message or f"Нет прав на запись: {p}"
                self._add_missing(r.level, msg, missing_hard, missing_soft)
        return missing_hard, missing_soft

    @staticmethod
    def _add_missing(level, msg, hard, soft):
        if level == RequirementLevel.HARD:
            hard.append(msg)
        else:
            soft.append(msg)

    # ═══════════════════════════════════════════════════════
    # External (TCP/HTTP)
    # ═══════════════════════════════════════════════════════
    def check_external(
        self, reqs: list[ExternalRequirement],
    ) -> tuple[list[str], list[str], dict]:
        missing_hard: list[str] = []
        missing_soft: list[str] = []
        health: dict[str, dict] = {}

        for r in reqs:
            status, latency, error = self._check_one_external(r)
            key = self._health_key(r)
            health[key] = {
                "status": status, "latency_ms": latency, "error": error,
            }
            try:
                self.runtime.set_health(key, status, latency, error)
            except Exception as e:
                logger.debug("Не сохранить health %s: %s", key, e)

            if status != ProviderHealth.HEALTHY.value:
                msg = r.message or (
                    f"Сервис недоступен: {r.host}:{r.port} ({error or status})"
                )
                self._add_missing(r.level, msg, missing_hard, missing_soft)

        return missing_hard, missing_soft, health

    @staticmethod
    def _health_key(r: ExternalRequirement) -> str:
        if r.protocol == "tcp":
            return f"tcp:{r.host}:{r.port}"
        return f"{r.protocol}:{r.host}:{r.port}{r.path}"

    def _check_one_external(
        self, r: ExternalRequirement,
    ) -> tuple[str, float | None, str | None]:
        t0 = time.perf_counter()
        try:
            if r.protocol == "tcp":
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.settimeout(r.timeout_seconds)
                    rc = s.connect_ex((r.host, r.port))
                    if rc != 0:
                        return ProviderHealth.UNHEALTHY.value, None, f"connect_ex={rc}"
                    return (
                        ProviderHealth.HEALTHY.value,
                        (time.perf_counter() - t0) * 1000,
                        None,
                    )
            # http/https
            url = r.url if hasattr(r, "url") and r.url else \
                  f"{r.protocol}://{r.host}:{r.port}{r.path}"
            try:
                resp = httpx.get(url, timeout=r.timeout_seconds)
                latency = (time.perf_counter() - t0) * 1000
                if 200 <= resp.status_code < 400:
                    return ProviderHealth.HEALTHY.value, latency, None
                return ProviderHealth.DEGRADED.value, latency, f"HTTP {resp.status_code}"
            except httpx.RequestError as e:
                return ProviderHealth.UNHEALTHY.value, None, str(e)
        except Exception as e:
            return ProviderHealth.UNHEALTHY.value, None, str(e)

    # ═══════════════════════════════════════════════════════
    # Capabilities
    # ═══════════════════════════════════════════════════════
    def check_capabilities(
        self, reqs: list[CapabilityRequirement],
        resolved: dict[str, dict],
    ) -> tuple[list[str], list[str]]:
        missing_hard: list[str] = []
        missing_soft: list[str] = []
        for r in reqs:
            info = resolved.get(r.id)
            if not info or not info.get("primary"):
                msg = r.message or f"Capability '{r.id}' недоступна"
                self._add_missing(r.level, msg, missing_hard, missing_soft)
        return missing_hard, missing_soft

    # ═══════════════════════════════════════════════════════
    # Util
    # ═══════════════════════════════════════════════════════
    def invalidate_pkg_cache(self) -> None:
        self._pkg_cache.clear()
