"""AgentRuntime — реестр готовых к запуску агентов.

Строится на основе snapshot: только active-агенты получают экземпляры.
"""
from __future__ import annotations

import logging
from typing import Any

from src.agents.base import BaseAgent
from src.core.schema import AgentSchema, AgentStatus
from src.core.snapshot import Snapshot
from src.loop.controller import LoopController
from src.loop.spec import LoopSpecLoader

logger = logging.getLogger(__name__)


class AgentRuntime:
    def __init__(
        self,
        controller: LoopController,
        loop_loader: LoopSpecLoader,
    ):
        self.controller = controller
        self.loop_loader = loop_loader
        self.agents: dict[str, BaseAgent] = {}

    # ═══════════════════════════════════════════════════════
    # Rebuild из snapshot
    # ═══════════════════════════════════════════════════════
    def rebuild(self, snapshot: Snapshot) -> None:
        """Пересоздаёт реестр агентов. Только active и degraded попадают."""
        new_agents: dict[str, BaseAgent] = {}

        for aid, state in snapshot.agents.items():
            if state.status not in (AgentStatus.ACTIVE, AgentStatus.DEGRADED):
                continue
            schema = state.schema
            agent = BaseAgent(
                schema=schema,
                controller=self.controller,
                loop_loader=self.loop_loader,
                loop_id=self._loop_for(schema),
            )
            new_agents[aid] = agent

        self.agents = new_agents
        logger.info(
            "AgentRuntime: %d агентов доступны (%s)",
            len(self.agents), ", ".join(sorted(self.agents.keys())),
        )

    def _loop_for(self, schema: AgentSchema) -> str:
        """Какой loop использовать для агента. Задаётся в extra или дефолт."""
        # Агент может явно указать loop в description_fields; по умолчанию reasoning
        return "agent.reasoning"

    # ═══════════════════════════════════════════════════════
    # Access
    # ═══════════════════════════════════════════════════════
    def get(self, agent_id: str) -> BaseAgent | None:
        return self.agents.get(agent_id)

    def list_ids(self) -> list[str]:
        return sorted(self.agents.keys())

    def __contains__(self, agent_id: str) -> bool:
        return agent_id in self.agents

    def __len__(self) -> int:
        return len(self.agents)
