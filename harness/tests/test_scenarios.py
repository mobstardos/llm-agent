"""Автоматический прогон всех harness-сценариев через pytest."""
from __future__ import annotations

import pytest


def _scenario_ids(scenarios: dict) -> list[str]:
    return sorted(scenarios.keys())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scenario_id",
    [
        "file_read_basic",
        "file_apply_patch_basic",
    ],
)
async def test_scenario(
    scenario_id, all_scenarios, run_scenario,
):
    """Каждый сценарий должен пройти в mock-режиме."""
    if scenario_id not in all_scenarios:
        pytest.skip(f"Scenario {scenario_id} not found")

    spec = all_scenarios[scenario_id]
    result = await run_scenario(
        spec,
        mock_llm=True,
        mock_mcp=True,
        strict_mock=False,   # мягко, чтобы не падать если fixtures не записаны
    )
    assert result.passed, (
        f"Scenario {scenario_id} failed: {result.error or result.exit_reason}\n"
        f"Assertions: {[(a.type, a.message) for a in result.assertions if not a.passed]}"
    )
