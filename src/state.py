# -*- coding: utf-8 -*-
"""Типизированное глобальное состояние приложения.

Рекомендация 1 (Sprint 1.A — подготовка к декомпозиции main.py).

Замена глобальному `state: dict` в src/main.py (строка 81):
  было: state["policies"] = policies
  стало: state.policies = policies

Все поля — Optional, потому что инициализация происходит в lifespan
поэтапно: cache → policies → llm → memory → registry → mcp → journal →
orchestrator → supervisor → background.

Dependency-injection функции get_*() используются в routes/*.py через
FastAPI Depends():
    @router.get("/api/policies")
    async def list_policies(policies: PolicyStore = Depends(get_policies)):
        ...
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional, TYPE_CHECKING

from fastapi import HTTPException

if TYPE_CHECKING:
    from src.cache import ResponseCache
    from src.core.features import FeatureLoader
    from src.core.file_watcher import FileWatcher
    from src.core.metrics import PrometheusExporter
    from src.core.registry import Registry
    from src.db.agent_memory import AgentMemoryIndexer
    from src.db.age_store import AgeStore
    from src.db.age_sync import AgeSync
    from src.db.backup import BackupManager
    from src.db.replicator import PgReplicator
    from src.file_state import FileState
    from src.journal import Journal
    from src.llm_client import LLMClient
    from src.loop.controller import LoopController
    from src.loop.spec import LoopSpecLoader
    from src.loop.telemetry import LoopTelemetry
    from src.mcp_manager import MCPManager
    from src.memory.facade import Memory
    from src.orchestrator import Orchestrator
    from src.policies import PolicyStore
    from src.agents.runtime import AgentRuntime
    from src.supervisor.plans import PlanRegistry
    from src.supervisor.supervisor import Supervisor
    from src.ollama.worker import EnrichmentWorker
    from src.background.micro_tasks import MicroTasksWorker


@dataclass
class AppState:
    """Глобальное состояние приложения.

    Инициализируется в src/app.py:lifespan() поэтапно. Все поля Optional
    потому что:
      - PG-зависимые подсистемы (memory_pool, agent_memory, age, cdc,
        backup) могут быть не инициализированы если PG недоступен.
      - Journal мягко инициализируется (если не установлен — no-op).
      supervisor — опциональная подсистема (SUPERVISOR_ENABLED=0 выключает).

    Доступ к полям: `state.policies is None` → 503 в route через
    Depends(get_policies).
    """
    # ── Core ──────────────────────────────────────────────────────
    cache: Optional["ResponseCache"] = None
    policies: Optional["PolicyStore"] = None
    llm: Optional["LLMClient"] = None
    memory: Optional["Memory"] = None
    registry: Optional["Registry"] = None
    mcp: Optional["MCPManager"] = None
    file_state: Optional["FileState"] = None

    # ── Loop + Agents ──────────────────────────────────────────────
    loop_loader: Optional["LoopSpecLoader"] = None
    telemetry: Optional["LoopTelemetry"] = None
    loop_controller: Optional["LoopController"] = None
    agent_runtime: Optional["AgentRuntime"] = None

    # ── Orchestration ─────────────────────────────────────────────
    orchestrator: Optional["Orchestrator"] = None
    plans: Optional["PlanRegistry"] = None
    supervisor: Optional["Supervisor"] = None

    # ── Journal ───────────────────────────────────────────────────
    journal: Optional["Journal"] = None

    # ── Background workers ────────────────────────────────────────
    enrichment_worker: Optional["EnrichmentWorker"] = None
    micro_worker: Optional["MicroTasksWorker"] = None
    file_watcher: Optional["FileWatcher"] = None

    # ── Features ──────────────────────────────────────────────────
    features_loader: Optional["FeatureLoader"] = None

    # ── PostgreSQL-dependent subsystems ────────────────────────────
    pg_replicator: Optional["PgReplicator"] = None
    agent_memory_indexer: Optional["AgentMemoryIndexer"] = None
    age_store: Optional["AgeStore"] = None
    age_sync: Optional["AgeSync"] = None
    backup_manager: Optional["BackupManager"] = None

    # ── Runtime state ─────────────────────────────────────────────
    ws_clients: set = field(default_factory=set)
    """WebSocket-клиенты, подписанные на события (events bus → WS bridge)."""

    local_chat_active_until: float = 0.0
    """Если time.time() < этого значения — локальная LLM активна, micro_worker паузится."""

    started_at: float = 0.0
    """Время старта сервера — для uptime в метриках."""


# Глобальный синглтон — импортируется из routes/ и app.py
state = AppState()


# ── Dependency injection helpers ───────────────────────────────────────
# Используются в routes/*.py через FastAPI Depends().

def get_policies() -> "PolicyStore":
    """Возвращает PolicyStore или 503 если не инициализирован."""
    if state.policies is None:
        raise HTTPException(503, "Policies not initialized (lifespan not finished)")
    return state.policies


def get_llm() -> "LLMClient":
    if state.llm is None:
        raise HTTPException(503, "LLM client not initialized")
    return state.llm


def get_memory() -> "Memory":
    if state.memory is None:
        raise HTTPException(503, "Memory not initialized")
    return state.memory


def get_registry() -> "Registry":
    if state.registry is None:
        raise HTTPException(503, "Registry not initialized")
    return state.registry


def get_mcp() -> "MCPManager":
    if state.mcp is None:
        raise HTTPException(503, "MCPManager not initialized")
    return state.mcp


def get_orchestrator() -> "Orchestrator":
    if state.orchestrator is None:
        raise HTTPException(503, "Orchestrator not initialized")
    return state.orchestrator


def get_plans() -> "PlanRegistry":
    if state.plans is None:
        raise HTTPException(503, "PlanRegistry not initialized")
    return state.plans


def get_journal() -> Optional["Journal"]:
    """Journal — мягкая подсистема: может быть None (no-op)."""
    return state.journal


def get_features_loader() -> Optional["FeatureLoader"]:
    return state.features_loader


def get_cache() -> Optional["ResponseCache"]:
    return state.cache


def get_supervisor() -> Optional["Supervisor"]:
    """Supervisor — опциональная подсистема (SUPERVISOR_ENABLED=0)."""
    return state.supervisor
