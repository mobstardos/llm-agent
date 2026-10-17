"""Состояние loop и результат."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass
class LoopState:
    iterations: int = 0
    tokens_input: int = 0
    tokens_output: int = 0
    tool_calls_count: int = 0
    started_at: float = field(default_factory=time.time)
    last_error: str | None = None
    last_response: str | None = None
    history: list[dict] = field(default_factory=list)
    messages: list[dict] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_seconds(self) -> float:
        return time.time() - self.started_at

    @property
    def tokens_total(self) -> int:
        return self.tokens_input + self.tokens_output

    def apply(self, step_result: dict) -> None:
        """Применяет результат шага к состоянию."""
        if "tokens_input" in step_result:
            self.tokens_input += step_result["tokens_input"]
        if "tokens_output" in step_result:
            self.tokens_output += step_result["tokens_output"]
        if "tool_calls_count" in step_result:
            self.tool_calls_count += step_result["tool_calls_count"]
        if "response" in step_result:
            self.last_response = step_result["response"]
        if "error" in step_result and step_result["error"]:
            self.last_error = step_result["error"]
        if "messages" in step_result:
            self.messages = step_result["messages"]
        self.history.append({
            "i": self.iterations,
            "ts": time.time(),
            **step_result,
        })


@dataclass
class LoopResult:
    loop_id: str
    exit_reason: str
    success: bool
    iterations: int
    tokens_input: int
    tokens_output: int
    tool_calls_count: int
    duration_ms: float
    response: str | None = None
    error: str | None = None
    history: list[dict] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "loop_id": self.loop_id,
            "exit_reason": self.exit_reason,
            "success": self.success,
            "iterations": self.iterations,
            "tokens_input": self.tokens_input,
            "tokens_output": self.tokens_output,
            "tool_calls_count": self.tool_calls_count,
            "duration_ms": self.duration_ms,
            "response": self.response,
            "error": self.error,
            "history_size": len(self.history),
            "extra": self.extra,
        }
