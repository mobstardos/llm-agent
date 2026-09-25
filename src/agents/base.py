"""BaseAgent — исполняет agent-декларацию через LoopController.

Агент не содержит логики — только адаптирует AgentSchema → LoopSpec и
прокидывает ресурсы (LLM, MCP, memory) в LoopContext.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from src.core.schema import AgentSchema, LoopSpec, LoopType
from src.loop.context import LoopContext
from src.loop.controller import LoopController
from src.loop.spec import LoopSpecLoader
from src.loop.state import LoopResult

logger = logging.getLogger(__name__)


class BaseAgent:
    def __init__(
        self,
        schema: AgentSchema,
        controller: LoopController,
        loop_loader: LoopSpecLoader,
        loop_id: str = "agent.reasoning",
    ):
        self.schema = schema
        self.controller = controller
        self.loop_loader = loop_loader
        self.loop_id = loop_id

    # ═══════════════════════════════════════════════════════
    # Запуск
    # ═══════════════════════════════════════════════════════
    async def run(
        self,
        query: str,
        *,
        model: str | None = None,
        context_text: str = "",
        llm_client: Any = None,
        mcp_manager: Any = None,
        memory: Any = None,
        file_state: Any = None,
        available_tools: list[dict] | None = None,
        on_token: Any = None,
        on_reasoning: Any = None,   # Task 24-d: reasoning-дельты → UI
        on_step: Any = None,
        approval_handler: Any = None,
        faults: Any = None,
        trace_id: str | None = None,
        session_id: str | None = None,
        extra: dict | None = None,
    ) -> LoopResult:
        """Запускает агента под управлением LoopController."""

        spec = self.loop_loader.get(self.loop_id)
        if spec is None:
            logger.error("Loop '%s' не найден", self.loop_id)
            return LoopResult(
                loop_id=self.loop_id,
                exit_reason="loop_not_found",
                success=False,
                iterations=0,
                tokens_input=0,
                tokens_output=0,
                tool_calls_count=0,
                duration_ms=0,
                error=f"Loop '{self.loop_id}' не найден",
            )

        # Ограничения из декларации агента
        spec = self._apply_agent_limits(spec)

        # Доступные инструменты — только те, что объявлены в MCP агента
        tools = available_tools
        if tools is None and mcp_manager is not None:
            tools = mcp_manager.get_openai_tools(self.schema.mcp_servers)

        ctx = LoopContext(
            llm_client=llm_client,
            model=model or self._resolve_model(),
            mcp_manager=mcp_manager,
            available_tools=tools or [],
            memory=memory,
            file_state=file_state,
            on_token=on_token,
            on_reasoning=on_reasoning,
            on_step=on_step,
            approval_handler=approval_handler,
            faults=faults,
            trace_id=trace_id,
            session_id=session_id,
            agent_id=self.schema.id,
            prompt=query,
            system_prompt=self.schema.prompt_text or "",
            user_template=self.schema.user_template_text or "{query}",
            context_text=context_text,
            extra={
                "max_result_chars": self.schema.runtime.max_result_chars,
                "dangerous_tools": list(self.schema.dangerous_tools),
                "loop_spec": spec,
                **(extra or {}),
            },
        )

        return await self.controller.run(spec, ctx)

    # ═══════════════════════════════════════════════════════
    # Helpers
    # ═══════════════════════════════════════════════════════
    def _resolve_model(self) -> str | None:
        return None  # переопределяется через runtime в loop-контексте

    def _apply_agent_limits(self, spec: LoopSpec) -> LoopSpec:
        """Накладывает лимиты агента на loop-спеку."""
        # Копируем спеку (Pydantic v2: model_copy)
        if hasattr(spec, "model_copy"):
            new = spec.model_copy(deep=True)
        else:
            new = spec.copy(deep=True)

        new.budget.max_iterations = self.schema.runtime.max_steps
        new.budget.max_duration_seconds = self.schema.runtime.timeout_seconds
        return new

    # ═══════════════════════════════════════════════════════
    # Introspection
    # ═══════════════════════════════════════════════════════
    @property
    def id(self) -> str:
        return self.schema.id

    @property
    def title(self) -> str:
        return self.schema.title

    def info(self) -> dict:
        return {
            "id": self.schema.id,
            "title": self.schema.title,
            "description": self.schema.description,
            "loop_id": self.loop_id,
            "mcp_servers": list(self.schema.mcp_servers),
            "mode": self.schema.mode.value,
        }
