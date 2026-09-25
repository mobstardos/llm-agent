"""Контекст выполнения loop — разделяемые ресурсы."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

TokenCallback = Callable[[str], Awaitable[None]]
ApprovalHandler = Callable[[dict], Awaitable[bool]]
StepCallback = Callable[[str, int], Awaitable[None]]


@dataclass
class LoopContext:
    """Контекст, в котором исполняется loop."""

    # LLM
    llm_client: Any = None
    model: str | None = None

    # MCP
    mcp_manager: Any = None
    available_tools: list[dict] = field(default_factory=list)

    # Система
    memory: Any = None
    registry: Any = None
    file_state: Any = None

    # Callbacks (для UI)
    on_token: TokenCallback | None = None
    on_reasoning: TokenCallback | None = None  # Task 24-d: ход мыслей R1
    on_step: StepCallback | None = None
    approval_handler: ApprovalHandler | None = None

    # Fault injection (chaos mode)
    faults: Any = None  # FaultInjector

    # Telemetry
    trace_id: str | None = None
    session_id: str | None = None
    agent_id: str | None = None
    parent_loop_id: str | None = None

    # Данные
    prompt: str = ""
    system_prompt: str = ""
    user_template: str = "{query}"
    context_text: str = ""

    # Дополнительно
    extra: dict[str, Any] = field(default_factory=dict)

    def child(self, **overrides) -> "LoopContext":
        """Создаёт дочерний контекст с переопределениями."""
        data = {
            "llm_client": self.llm_client,
            "model": self.model,
            "mcp_manager": self.mcp_manager,
            "available_tools": list(self.available_tools),
            "memory": self.memory,
            "registry": self.registry,
            "file_state": self.file_state,
            "on_token": self.on_token,
            "on_reasoning": self.on_reasoning,
            "on_step": self.on_step,
            "approval_handler": self.approval_handler,
            "faults": self.faults,
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "agent_id": self.agent_id,
            "parent_loop_id": self.parent_loop_id,
            "prompt": self.prompt,
            "system_prompt": self.system_prompt,
            "user_template": self.user_template,
            "context_text": self.context_text,
            "extra": dict(self.extra),
        }
        data.update(overrides)
        return LoopContext(**data)
