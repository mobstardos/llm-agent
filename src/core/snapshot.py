"""Snapshot — единый снимок состояния системы.

Собирается из:
  - загруженных деклараций
  - runtime_config (user_overrides, health)
  - capabilities resolve
  - health-check агентов/MCP

Из snapshot растут:
  - динамический промпт оркестратора
  - список MCP-серверов для старта
  - UI-панель агентов
  - метрики Prometheus
"""
from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from src.core.capabilities import CapabilityResolver
from src.core.health import RequirementChecker
from src.core.schema import (
    AgentSchema,
    AgentStatus,
    CapabilitySchema,
    DangerLevel,
    MCPServerSchema,
)

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# Data classes
# ═══════════════════════════════════════════════════════════════════════


@dataclass
class AgentState:
    schema: AgentSchema
    status: AgentStatus
    reasons: list[str] = field(default_factory=list)
    degraded_reasons: list[str] = field(default_factory=list)
    effective_params: dict[str, Any] = field(default_factory=dict)


@dataclass
class MCPState:
    schema: MCPServerSchema
    enabled: bool
    alive: bool = False
    restarts: int = 0
    reasons: list[str] = field(default_factory=list)


@dataclass
class Snapshot:
    built_at: float = 0.0
    build_duration_ms: float = 0.0
    agents: dict[str, AgentState] = field(default_factory=dict)
    mcp_servers: dict[str, MCPState] = field(default_factory=dict)
    capabilities: dict[str, CapabilitySchema] = field(default_factory=dict)
    resolved_capabilities: dict[str, dict] = field(default_factory=dict)
    orchestrator_prompt: str = ""
    orchestrator_prompt_hash: str = ""

    # ─── Helpers ────────────────────────────────────────
    @property
    def active_agents(self) -> list[str]:
        return [
            aid for aid, st in self.agents.items()
            if st.status == AgentStatus.ACTIVE
        ]

    @property
    def degraded_agents(self) -> list[str]:
        return [
            aid for aid, st in self.agents.items()
            if st.status == AgentStatus.DEGRADED
        ]

    @property
    def unavailable_agents(self) -> list[str]:
        return [
            aid for aid, st in self.agents.items()
            if st.status in (AgentStatus.UNAVAILABLE, AgentStatus.FAILED)
        ]

    @property
    def disabled_agents(self) -> list[str]:
        return [
            aid for aid, st in self.agents.items()
            if st.status == AgentStatus.DISABLED
        ]

    @property
    def enabled_mcp_ids(self) -> list[str]:
        return [
            mid for mid, st in self.mcp_servers.items()
            if st.enabled
        ]

    @property
    def alive_mcp_ids(self) -> list[str]:
        return [
            mid for mid, st in self.mcp_servers.items()
            if st.alive
        ]


# ═══════════════════════════════════════════════════════════════════════
# Builder
# ═══════════════════════════════════════════════════════════════════════


class SnapshotBuilder:
    """Собирает snapshot из деклараций + runtime + health."""

    def __init__(self, runtime, base_dir=None):
        self.runtime = runtime
        self.base_dir = base_dir
        self.checker = RequirementChecker(runtime)
        self.resolver = CapabilityResolver(runtime)

    async def build(
        self,
        agents: dict[str, AgentSchema],
        mcp_servers: dict[str, MCPServerSchema],
        capabilities: dict[str, CapabilitySchema],
    ) -> Snapshot:
        t0 = time.perf_counter()
        snap = Snapshot(built_at=time.time(), capabilities=dict(capabilities))

        # ─── 1. Capabilities first ──────────────────────
        snap.resolved_capabilities = await self.resolver.resolve_all(capabilities)

        # ─── 2. MCP — базовые состояния ─────────────────
        for mid, mschema in mcp_servers.items():
            enabled = not self.runtime.is_mcp_disabled(mid)
            snap.mcp_servers[mid] = MCPState(
                schema=mschema, enabled=enabled,
            )

        # ─── 3. Agents — базовые статусы ────────────────
        for aid, aschema in agents.items():
            snap.agents[aid] = self._resolve_agent(aschema, snap)

        # ─── 4. Применяем runtime overrides и зависимости
        for aid, state in snap.agents.items():
            self._apply_overrides(aid, state, snap)

        # ─── 5. Собираем промпт оркестратора ────────────
        snap.orchestrator_prompt = self._build_orchestrator_prompt(snap)
        snap.orchestrator_prompt_hash = hashlib.sha256(
            snap.orchestrator_prompt.encode("utf-8")
        ).hexdigest()[:16]

        snap.build_duration_ms = (time.perf_counter() - t0) * 1000

        # ─── 6. Сохраняем timestamp ─────────────────────
        try:
            self.runtime.set_last_snapshot(snap.built_at)
        except Exception as e:
            logger.debug("Не сохранить last_snapshot: %s", e)

        logger.info(
            "Snapshot: %d agents (%d active, %d degraded, %d unavailable, %d disabled), "
            "%d MCP, %.0f ms",
            len(snap.agents),
            len(snap.active_agents),
            len(snap.degraded_agents),
            len(snap.unavailable_agents),
            len(snap.disabled_agents),
            len(snap.mcp_servers),
            snap.build_duration_ms,
        )
        return snap

    # ═══════════════════════════════════════════════════════
    # Agent resolution
    # ═══════════════════════════════════════════════════════
    def _resolve_agent(
        self, a: AgentSchema, snap: Snapshot,
    ) -> AgentState:
        hard_missing: list[str] = []
        soft_missing: list[str] = []

        # Пакеты
        h, s = self.checker.check_packages(a.requires.python_packages)
        hard_missing += h; soft_missing += s

        # Env
        h, s = self.checker.check_env(a.requires.env_vars)
        hard_missing += h; soft_missing += s

        # Paths
        h, s = self.checker.check_paths(a.requires.paths)
        hard_missing += h; soft_missing += s

        # External (TCP/HTTP)
        h, s, _ = self.checker.check_external(a.requires.external)
        hard_missing += h; soft_missing += s

        # Capabilities
        h, s = self.checker.check_capabilities(
            a.requires.capabilities, snap.resolved_capabilities,
        )
        hard_missing += h; soft_missing += s

        # MCP присутствие
        for mid in a.mcp_servers:
            if mid not in snap.mcp_servers:
                hard_missing.append(f"MCP '{mid}' не объявлен в mcp_servers/")

        if hard_missing:
            status = AgentStatus.UNAVAILABLE
        elif soft_missing:
            status = AgentStatus.DEGRADED
        else:
            status = AgentStatus.ACTIVE

        params = self._compute_params(a)

        return AgentState(
            schema=a,
            status=status,
            reasons=hard_missing,
            degraded_reasons=soft_missing,
            effective_params=params,
        )

    def _compute_params(self, a: AgentSchema) -> dict[str, Any]:
        """Дефолты из декларации + user overrides."""
        params = {
            "priority": a.priority,
            "mode": a.mode.value,
            "dangerous_tools": list(a.dangerous_tools),
            "max_steps": a.runtime.max_steps,
            "max_result_chars": a.runtime.max_result_chars,
            "timeout_seconds": a.runtime.timeout_seconds,
            "keywords": list(a.routing_hints.keywords),
            "negative_keywords": list(a.routing_hints.negative_keywords),
            "description_for_router": a.routing_hints.description_for_router,
            "model": self.runtime.get_model_pref(a.id),
        }
        overrides = self.runtime.get_agent_params(a.id)
        params.update(overrides)
        return params

    def _apply_overrides(
        self, aid: str, state: AgentState, snap: Snapshot,
    ) -> None:
        # Явное отключение
        if self.runtime.is_agent_disabled(aid):
            state.status = AgentStatus.DISABLED
            state.reasons = ["Отключён пользователем"]
            return

        # Зависимости
        for dep in state.schema.depends_on.hard:
            dep_state = snap.agents.get(dep)
            if dep_state and dep_state.status in (
                AgentStatus.UNAVAILABLE,
                AgentStatus.FAILED,
                AgentStatus.DISABLED,
            ):
                state.status = AgentStatus.UNAVAILABLE
                state.reasons.append(f"Зависит от недоступного '{dep}'")

        for dep in state.schema.depends_on.soft:
            dep_state = snap.agents.get(dep)
            if dep_state and dep_state.status in (
                AgentStatus.UNAVAILABLE,
                AgentStatus.FAILED,
                AgentStatus.DISABLED,
            ):
                if state.status == AgentStatus.ACTIVE:
                    state.status = AgentStatus.DEGRADED
                state.degraded_reasons.append(
                    f"Зависит от недоступного '{dep}' (soft)"
                )

    # ═══════════════════════════════════════════════════════
    # Orchestrator prompt
    # ═══════════════════════════════════════════════════════
    def _build_orchestrator_prompt(self, snap: Snapshot) -> str:
        active: list[AgentState] = []
        degraded: list[AgentState] = []
        unavailable: list[AgentState] = []

        for st in snap.agents.values():
            if st.status == AgentStatus.ACTIVE:
                active.append(st)
            elif st.status == AgentStatus.DEGRADED:
                degraded.append(st)
            elif st.status in (AgentStatus.UNAVAILABLE, AgentStatus.FAILED):
                unavailable.append(st)

        active.sort(key=lambda s: -s.effective_params.get("priority", 0))

        lines: list[str] = []
        lines.append(
            "Ты — диспетчер задач мультиагентной системы.\n"
            "Проанализируй запрос и выбери одного или нескольких агентов."
        )

        if active:
            lines.append("\n## Доступные агенты\n")
            for st in active:
                a = st.schema
                desc = st.effective_params.get(
                    "description_for_router", a.description,
                )
                lines.append(f"- **{a.id}** ({a.title}): {desc}")
                kw = st.effective_params.get("keywords", [])
                if kw:
                    lines.append(f"  Ключевые слова: {', '.join(kw)}")
                nk = st.effective_params.get("negative_keywords", [])
                if nk:
                    lines.append(
                        f"  НЕ использовать, если упомянуто: {', '.join(nk)}"
                    )

        if degraded:
            lines.append("\n## Ограниченно доступные агенты\n")
            for st in degraded:
                reasons = "; ".join(st.degraded_reasons[:2])
                lines.append(f"- **{st.schema.id}**: {reasons}")

        if unavailable:
            lines.append(
                "\n## Недоступные агенты "
                "(НЕ упоминай их, если пользователь не спросит явно)\n"
            )
            for st in unavailable:
                reasons = "; ".join(st.reasons[:2])
                lines.append(f"- {st.schema.id}: {reasons}")

        lines.append("\n## Формат ответа\n")
        lines.append(
            "Верни ТОЛЬКО JSON без markdown-обёрток:\n"
            '{"agents": ["id1", "id2"], "reason": "краткое обоснование"}\n'
        )

        lines.append("\n## Правила\n")
        lines.append("1. Выбирай ТОЛЬКО из секции 'Доступные агенты'.")
        lines.append(
            "2. Учитывай 'Ограниченно доступные' — только если задача явно подходит."
        )
        lines.append(
            "3. Если задача требует недоступного агента — верни пустой список."
        )
        lines.append(
            "4. Для составных задач указывай агентов в порядке выполнения."
        )
        lines.append(
            "5. Если задача требует изменений в файлах — всегда добавляй file."
        )

        return "\n".join(lines)
