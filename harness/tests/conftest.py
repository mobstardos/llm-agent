"""Pytest fixtures для harness."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))


@pytest.fixture(scope="session")
def base_dir() -> Path:
    return BASE_DIR


@pytest.fixture(scope="session")
def scenario_loader(base_dir):
    from src.harness.loader import ScenarioLoader
    return ScenarioLoader(base_dir)


@pytest.fixture(scope="session")
def all_scenarios(scenario_loader):
    return scenario_loader.load_all()


@pytest.fixture
def run_scenario(base_dir):
    """Возвращает async-функцию для запуска сценария."""
    from src.harness.runner import HarnessRunner
    from src.loop.telemetry import LoopTelemetry

    telemetry = LoopTelemetry(base_dir / "data" / "loop_telemetry.sqlite")
    runner = HarnessRunner(
        base_dir=base_dir,
        telemetry=telemetry,
        runs_dir=base_dir / "data" / "harness_runs",
        keep_runs=False,
    )

    async def _run(spec, **kwargs):
        return await runner.run_scenario(spec, **kwargs)

    return _run
