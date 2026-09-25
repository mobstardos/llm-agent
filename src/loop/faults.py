"""Fault injection для chaos-тестирования."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class FaultRule:
    kind: str           # llm_timeout | llm_error | mcp_crash | mcp_slow | ...
    at_step: int | None = None
    at_iteration: int | None = None
    duration_ms: int | None = None
    server: str | None = None
    tool: str | None = None
    count: int = 1
    used: int = 0

    def should_trigger(self, iteration: int, step: int) -> bool:
        if self.used >= self.count:
            return False
        if self.at_iteration is not None and iteration != self.at_iteration:
            return False
        if self.at_step is not None and step != self.at_step:
            return False
        return True


class FaultInjector:
    """Инжектор сбоев. По умолчанию неактивен."""

    def __init__(self, rules: list[FaultRule] | None = None):
        self.rules: list[FaultRule] = rules or []
        self.enabled = bool(rules)

    def check_llm(self, iteration: int, step: int) -> FaultRule | None:
        return self._check("llm_", iteration, step)

    def check_mcp(self, server: str, tool: str) -> FaultRule | None:
        for r in self.rules:
            if r.used >= r.count:
                continue
            if r.kind == "mcp_crash" and r.server == server:
                r.used += 1
                return r
            if r.kind == "mcp_slow" and (r.server == server or r.tool == tool):
                r.used += 1
                return r
        return None

    def _check(self, prefix: str, iteration: int, step: int) -> FaultRule | None:
        for r in self.rules:
            if not r.kind.startswith(prefix):
                continue
            if not r.should_trigger(iteration, step):
                continue
            r.used += 1
            return r
        return None

    async def apply_llm_fault(self, rule: FaultRule) -> None:
        if rule.kind == "llm_timeout":
            await asyncio.sleep((rule.duration_ms or 5000) / 1000)
            raise TimeoutError(f"LLM timeout (injected)")
        if rule.kind == "llm_error":
            raise RuntimeError(f"LLM error (injected)")
        if rule.kind == "llm_malformed":
            raise ValueError(f"LLM malformed response (injected)")

    async def apply_mcp_fault(self, rule: FaultRule) -> None:
        if rule.kind == "mcp_crash":
            raise RuntimeError(f"MCP server crashed (injected)")
        if rule.kind == "mcp_slow":
            await asyncio.sleep((rule.duration_ms or 2000) / 1000)
