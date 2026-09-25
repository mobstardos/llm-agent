"""Registry — единая точка правды.

Держит:
  - декларации (agents, mcp_servers, capabilities)
  - runtime_config
  - snapshot
  - подсистемы: migrations, history, profiles, audit, rollback

Публичный API:
  - load_declarations() / reload_all()
  - build_snapshot()
  - set_agent_enabled() / set_agent_params() / ...
  - apply_profile() / apply_overrides()
  - rollback_snapshot() / rollback_restore()
  - snapshot_history() / snapshot_diff()
  - audit_list()
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from src.core.loader import DeclarationLoader
from src.core.migrations import MigrationEngine
from src.core.runtime_config import RuntimeConfig
from src.core.schema import (
    AgentSchema,
    AgentStatus,
    CapabilitySchema,
    MCPServerSchema,
)
from src.core.snapshot import Snapshot, SnapshotBuilder

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent

SnapshotListener = Callable[[Snapshot], Any]


class Registry:
    def __init__(self, base_dir: Path | None = None):
        self.base_dir = base_dir or BASE_DIR

        # ─── Ядро ───────────────────────────────────────
        self.runtime = RuntimeConfig(self.base_dir / "data" / "runtime.yaml")
        self.loader = DeclarationLoader(self.base_dir)
        self.builder = SnapshotBuilder(self.runtime, self.base_dir)
        self.migrations = MigrationEngine()

        # ─── Декларации ─────────────────────────────────
        self.agents: dict[str, AgentSchema] = {}
        self.mcp_servers: dict[str, MCPServerSchema] = {}
        self.capabilities: dict[str, CapabilitySchema] = {}

        # ─── Snapshot ───────────────────────────────────
        self.snapshot: Snapshot | None = None
        self._lock = asyncio.Lock()
        self._listeners: list[SnapshotListener] = []

        # ─── Ленивая инициализация подсистем ────────────
        self._history = None
        self._profiles = None
        self._audit = None
        self._rollback = None

    # ═══════════════════════════════════════════════════════
    # Lazy subsystems
    # ═══════════════════════════════════════════════════════
    @property
    def history(self):
        if self._history is None:
            from src.core.history import SnapshotHistory
            self._history = SnapshotHistory(
                self.base_dir / "data" / "snapshots"
            )
        return self._history

    @property
    def profiles(self):
        if self._profiles is None:
            from src.core.profiles import ProfileStore
            self._profiles = ProfileStore(
                self.base_dir / "data" / "profiles"
            )
        return self._profiles

    @property
    def audit(self):
        if self._audit is None:
            from src.core.audit import AuditStore
            self._audit = AuditStore(
                self.base_dir / "data" / "audit.db"
            )
        return self._audit

    @property
    def rollback(self):
        if self._rollback is None:
            from src.core.rollback import RollbackStore
            self._rollback = RollbackStore(self.base_dir)
        return self._rollback

    # ═══════════════════════════════════════════════════════
    # Loading
    # ═══════════════════════════════════════════════════════
    def load_declarations(self) -> None:
        """Загружает все декларации. Перед этим — rollback snapshot."""
        for kind in ("agents", "mcp_servers", "capabilities"):
            if (self.base_dir / kind).exists():
                try:
                    self.rollback.snapshot(kind, tag="pre_load")
                except Exception as e:
                    logger.debug("Rollback snapshot для %s упал: %s", kind, e)

        self.agents = self.loader.load_agents()
        self.mcp_servers = self.loader.load_mcp_servers()
        self.capabilities = self.loader.load_capabilities()

        logger.info(
            "Декларации загружены: %d агентов, %d MCP, %d capabilities",
            len(self.agents), len(self.mcp_servers), len(self.capabilities),
        )

        if self.loader.errors:
            logger.warning(
                "Ошибки загрузки: %s",
                "; ".join(f"{k}: {v}" for k, v in self.loader.errors.items()),
            )

    # ═══════════════════════════════════════════════════════
    # Snapshot
    # ═══════════════════════════════════════════════════════
    async def build_snapshot(self, reason: str = "manual") -> Snapshot:
        async with self._lock:
            snap = await self.builder.build(
                agents=self.agents,
                mcp_servers=self.mcp_servers,
                capabilities=self.capabilities,
            )
            self.snapshot = snap

            # Записываем в историю (если есть)
            try:
                self.history.record(snap, reason=reason)
            except Exception as e:
                logger.debug("Не записать snapshot в историю: %s", e)

            # Уведомляем слушателей
            for cb in self._listeners:
                try:
                    r = cb(snap)
                    if asyncio.iscoroutine(r):
                        await r
                except Exception as e:
                    logger.exception("Snapshot listener упал: %s", e)

            return snap

    def on_snapshot(self, cb: SnapshotListener) -> None:
        self._listeners.append(cb)

    # ═══════════════════════════════════════════════════════
    # Reload
    # ═══════════════════════════════════════════════════════
    async def reload_agent(self, agent_id: str) -> None:
        logger.info("Reload агента: %s", agent_id)
        self.agents = self.loader.load_agents()
        await self.build_snapshot(reason="reload_agent")

    async def reload_mcp(self, mcp_id: str) -> None:
        logger.info("Reload MCP: %s", mcp_id)
        self.mcp_servers = self.loader.load_mcp_servers()
        await self.build_snapshot(reason="reload_mcp")

    async def reload_all(self) -> None:
        logger.info("Полный reload деклараций")
        self.load_declarations()
        await self.build_snapshot(reason="reload_all")

    # ═══════════════════════════════════════════════════════
    # Управление через runtime
    # ═══════════════════════════════════════════════════════
    async def set_agent_enabled(
        self, agent_id: str, enabled: bool, actor: str = "ui",
    ) -> None:
        before = {
            "disabled": self.runtime.is_agent_disabled(agent_id),
            "enabled": self.runtime.is_agent_forced_enabled(agent_id),
        }
        self.runtime.set_agent_enabled(agent_id, enabled, written_by=actor)
        after = {"disabled": not enabled, "enabled": enabled}
        try:
            self.audit.log(actor, "agent_enable", agent_id, before, after)
        except Exception:
            pass
        await self.build_snapshot(reason="ui_change")

    async def update_agent_params(
        self, agent_id: str, params: dict, actor: str = "ui",
    ) -> None:
        before = self.runtime.get_agent_params(agent_id)
        self.runtime.set_agent_params(agent_id, params, written_by=actor)
        after = self.runtime.get_agent_params(agent_id)
        try:
            self.audit.log(actor, "agent_params", agent_id, before, after)
        except Exception:
            pass
        await self.build_snapshot(reason="ui_change")

    async def reset_agent(self, agent_id: str, actor: str = "ui") -> None:
        before = self.runtime.get_agent_params(agent_id)
        self.runtime.reset_agent_params(agent_id, written_by=actor)
        try:
            self.audit.log(actor, "agent_reset", agent_id, before, None)
        except Exception:
            pass
        await self.build_snapshot(reason="ui_change")

    async def set_mcp_enabled(
        self, mcp_id: str, enabled: bool, actor: str = "ui",
    ) -> None:
        before = self.runtime.is_mcp_disabled(mcp_id)
        self.runtime.set_mcp_enabled(mcp_id, enabled, written_by=actor)
        try:
            self.audit.log(
                actor, "mcp_enable", mcp_id,
                {"disabled": before}, {"disabled": not enabled},
            )
        except Exception:
            pass
        await self.build_snapshot(reason="ui_change")

    async def set_capability_provider(
        self, cap_id: str, provider_id: str | None, actor: str = "ui",
    ) -> None:
        before = self.runtime.get_forced_capability(cap_id)
        self.runtime.set_forced_capability(cap_id, provider_id, written_by=actor)
        try:
            self.audit.log(
                actor, "capability_provider", cap_id, before, provider_id,
            )
        except Exception:
            pass
        await self.build_snapshot(reason="ui_change")

    async def set_model_pref(
        self, model: str | None, agent_id: str | None = None,
        actor: str = "ui",
    ) -> None:
        before = self.runtime.get_model_pref(agent_id)
        self.runtime.set_model_pref(model, agent_id, written_by=actor)
        try:
            self.audit.log(
                actor, "model_pref", agent_id or "default", before, model,
            )
        except Exception:
            pass
        await self.build_snapshot(reason="ui_change")

    # ═══════════════════════════════════════════════════════
    # Profiles
    # ═══════════════════════════════════════════════════════
    async def apply_profile(self, profile_id: str, actor: str = "ui"):
        p = self.profiles.get(profile_id)
        if not p:
            raise ValueError(f"Профиль не найден: {profile_id}")
        before = self.runtime.get_overrides()
        self.runtime.replace_overrides(p.overrides, written_by=f"{actor}:profile:{profile_id}")
        try:
            self.audit.log(
                actor, "profile_apply", profile_id,
                before=before, after=p.overrides,
            )
        except Exception:
            pass
        await self.build_snapshot(reason="ui_change")
        return p

    async def apply_overrides(
        self, overrides: dict, actor: str = "ui",
    ) -> None:
        before = self.runtime.get_overrides()
        self.runtime.merge_overrides(overrides, written_by=f"{actor}:overrides")
        try:
            self.audit.log(actor, "overrides_apply", None, before, overrides)
        except Exception:
            pass
        await self.build_snapshot(reason="ui_change")

    # ═══════════════════════════════════════════════════════
    # Rollback
    # ═══════════════════════════════════════════════════════
    def rollback_snapshot(self, kind: str, tag: str = "auto") -> bool:
        return bool(self.rollback.snapshot(kind, tag=tag))

    def rollback_restore(self, kind: str, version_dir=None) -> bool:
        return self.rollback.restore(kind, version_dir)

    # ═══════════════════════════════════════════════════════
    # History
    # ═══════════════════════════════════════════════════════
    def snapshot_history(self, limit: int = 20) -> list:
        return self.history.list(limit=limit)

    def snapshot_diff(self, from_id: str, to_id: str):
        return self.history.diff(from_id, to_id)

    def latest_snapshot_record(self, slot: str | None = None):
        return self.history.latest(slot=slot)

    # ═══════════════════════════════════════════════════════
    # Audit
    # ═══════════════════════════════════════════════════════
    def audit_list(
        self, limit: int = 100,
        target: str | None = None,
        action: str | None = None,
    ) -> list:
        return self.audit.list(limit=limit, target=target, action=action)

    # ═══════════════════════════════════════════════════════
    # Helpers for UI
    # ═══════════════════════════════════════════════════════
    def agent_info(self, agent_id: str) -> dict:
        snap = self.snapshot
        if not snap or agent_id not in snap.agents:
            return {}
        st = snap.agents[agent_id]
        a = st.schema
        return {
            "id": a.id,
            "title": a.title,
            "description": a.description,
            "version": a.version,
            "schema_version": a.schema_version,
            "status": st.status.value,
            "reasons": list(st.reasons),
            "degraded_reasons": list(st.degraded_reasons),
            "mode": a.mode.value,
            "category": a.ui.category,
            "icon": a.ui.icon,
            "color": a.ui.color,
            "mcp_servers": list(a.mcp_servers),
            "depends_on": {
                "hard": list(a.depends_on.hard),
                "soft": list(a.depends_on.soft),
            },
            "provides": list(a.provides),
            "effective_params": dict(st.effective_params),
            "default_params": {
                "priority": a.priority,
                "max_steps": a.runtime.max_steps,
                "max_result_chars": a.runtime.max_result_chars,
                "timeout_seconds": a.runtime.timeout_seconds,
                "keywords": list(a.routing_hints.keywords),
                "negative_keywords": list(a.routing_hints.negative_keywords),
                "description_for_router": a.routing_hints.description_for_router,
                "model": self.runtime.get_model_pref(a.id),
            },
            "user_overrides": self.runtime.get_agent_params(agent_id),
            "dangerous_tools": list(a.dangerous_tools),
            "prompt_length": len(a.prompt_text or ""),
        }

    def mcp_info(self, mcp_id: str) -> dict:
        snap = self.snapshot
        if not snap or mcp_id not in snap.mcp_servers:
            return {}
        st = snap.mcp_servers[mcp_id]
        m = st.schema
        return {
            "id": m.id,
            "title": m.title or m.id,
            "description": m.description,
            "enabled": st.enabled,
            "alive": st.alive,
            "restarts": st.restarts,
            "command": m.command,
            "args": list(m.args),
            "tools": [
                {
                    "name": t.name,
                    "danger": t.danger.value,
                    "description": t.description,
                }
                for t in m.tools
            ],
            "user_overrides": self.runtime.get_mcp_params(mcp_id),
        }

    def capability_info(self, cap_id: str) -> dict:
        cap = self.capabilities.get(cap_id)
        if not cap:
            return {}
        resolved = (
            self.snapshot.resolved_capabilities.get(cap_id, {})
            if self.snapshot else {}
        )
        return {
            "id": cap.id,
            "title": cap.title,
            "description": cap.description,
            "strategy": cap.strategy,
            "tie_breaker": list(cap.tie_breaker),
            "resolved": resolved,
            "providers": [
                {
                    "id": p.id,
                    "display_name": p.display_name or p.id,
                    "type": p.type,
                    "priority": p.priority,
                    "cost_hint": p.cost_hint,
                    "supports": list(p.supports),
                }
                for p in cap.providers
            ],
        }

    def list_all_agents(self) -> list[dict]:
        if not self.snapshot:
            return []
        return [self.agent_info(aid) for aid in self.snapshot.agents]

    def list_all_mcp(self) -> list[dict]:
        if not self.snapshot:
            return []
        return [self.mcp_info(mid) for mid in self.snapshot.mcp_servers]

    def list_all_capabilities(self) -> list[dict]:
        return [self.capability_info(cid) for cid in self.capabilities]

    def snapshot_summary(self) -> dict:
        if not self.snapshot:
            return {"error": "not_initialized"}
        snap = self.snapshot
        return {
            "built_at": snap.built_at,
            "build_duration_ms": round(snap.build_duration_ms, 1),
            "agents": {
                "active": snap.active_agents,
                "degraded": snap.degraded_agents,
                "unavailable": snap.unavailable_agents,
                "disabled": snap.disabled_agents,
            },
            "mcp_enabled": snap.enabled_mcp_ids,
            "mcp_alive": snap.alive_mcp_ids,
            "prompt_length": len(snap.orchestrator_prompt),
            "prompt_hash": snap.orchestrator_prompt_hash,
        }
