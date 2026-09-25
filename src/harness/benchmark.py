"""Benchmark mode: N прогонов, агрегация метрик."""
from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)


@dataclass
class BenchmarkResult:
    scenario_id: str
    model: str
    runs: int
    pass_rate: float
    p50_ms: float
    p95_ms: float
    tokens_avg: float
    iterations_avg: float
    errors: list[str] = field(default_factory=list)


async def run_benchmark(
    scenario_id: str,
    model: str,
    runs: int,
    run_once: Callable[[str, str], Awaitable[dict]],
) -> BenchmarkResult:
    """Запускает сценарий N раз, агрегирует результат."""
    durations: list[float] = []
    tokens: list[int] = []
    iterations: list[int] = []
    passed = 0
    errors: list[str] = []

    for i in range(runs):
        try:
            r = await run_once(scenario_id, model)
            durations.append(r.get("duration_ms", 0))
            tokens.append(
                r.get("tokens_input", 0) + r.get("tokens_output", 0),
            )
            iterations.append(r.get("iterations", 0))
            if r.get("passed"):
                passed += 1
            if r.get("error"):
                errors.append(r["error"])
        except Exception as e:
            errors.append(str(e))

    if not durations:
        return BenchmarkResult(
            scenario_id=scenario_id, model=model, runs=runs,
            pass_rate=0.0, p50_ms=0, p95_ms=0,
            tokens_avg=0, iterations_avg=0, errors=errors,
        )

    durations.sort()

    def percentile(data: list[float], p: float) -> float:
        if not data:
            return 0.0
        idx = int(len(data) * p)
        idx = min(idx, len(data) - 1)
        return data[idx]

    return BenchmarkResult(
        scenario_id=scenario_id,
        model=model,
        runs=runs,
        pass_rate=passed / runs,
        p50_ms=percentile(durations, 0.50),
        p95_ms=percentile(durations, 0.95),
        tokens_avg=statistics.mean(tokens) if tokens else 0,
        iterations_avg=statistics.mean(iterations) if iterations else 0,
        errors=errors,
    )
