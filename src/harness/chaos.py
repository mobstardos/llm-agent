"""Chaos mode: генерация FaultRule из Scenario.chaos."""
from __future__ import annotations

import logging

from src.loop.faults import FaultRule, FaultInjector

logger = logging.getLogger(__name__)


def build_fault_injector(chaos_spec) -> FaultInjector:
    """chaos_spec — ChaosSpec из ScenarioSpec."""
    if chaos_spec is None:
        return FaultInjector([])

    rules: list[FaultRule] = []
    for item in chaos_spec.inject:
        rule = FaultRule(
            kind=item.get("type", ""),
            at_step=item.get("at_step"),
            at_iteration=item.get("at_iteration"),
            duration_ms=item.get("duration_ms"),
            server=item.get("server"),
            tool=item.get("tool"),
            count=item.get("count", 1),
        )
        rules.append(rule)

    logger.info("Chaos: %d правил инжекции", len(rules))
    return FaultInjector(rules)
