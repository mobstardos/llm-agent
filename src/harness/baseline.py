"""Baseline: эталонные результаты + regression detection."""
from __future__ import annotations

import json
import logging
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class BaselineEntry:
    scenario_id: str
    recorded_at: float
    passed: bool
    iterations: int = 0
    tokens_input: int = 0
    tokens_output: int = 0
    duration_ms: float = 0.0
    tool_calls_count: int = 0
    response_hash: str = ""


@dataclass
class RegressionReport:
    scenario_id: str
    has_regression: bool = False
    regressions: list[dict] = field(default_factory=list)


class BaselineStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)

    def save(self, entry: BaselineEntry) -> None:
        f = self.path / f"{entry.scenario_id}.json"
        f.write_text(
            json.dumps(asdict(entry), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load(self, scenario_id: str) -> BaselineEntry | None:
        f = self.path / f"{scenario_id}.json"
        if not f.exists():
            return None
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            return BaselineEntry(**data)
        except Exception as e:
            logger.warning("Baseline не прочитать: %s", e)
            return None

    def list(self) -> list[BaselineEntry]:
        result: list[BaselineEntry] = []
        for f in sorted(self.path.glob("*.json")):
            try:
                result.append(BaselineEntry(**json.loads(
                    f.read_text(encoding="utf-8"),
                )))
            except Exception:
                continue
        return result

    def compare(
        self, scenario_id: str, current: dict,
    ) -> RegressionReport:
        """Сравнивает текущий прогон с baseline."""
        baseline = self.load(scenario_id)
        if baseline is None:
            return RegressionReport(
                scenario_id=scenario_id,
                has_regression=False,
                regressions=[{"type": "no_baseline"}],
            )

        report = RegressionReport(scenario_id=scenario_id)

        # Pass → fail
        if baseline.passed and not current.get("passed", True):
            report.regressions.append({
                "type": "pass_to_fail",
                "before": True,
                "after": False,
            })

        # Steps drift > 30%
        cur_steps = current.get("iterations", 0)
        if baseline.iterations > 0:
            ratio = cur_steps / baseline.iterations
            if ratio > 1.3:
                report.regressions.append({
                    "type": "steps_drift",
                    "before": baseline.iterations,
                    "after": cur_steps,
                    "ratio": round(ratio, 2),
                })

        # Tokens drift > 50%
        cur_tokens = current.get("tokens_input", 0) + current.get(
            "tokens_output", 0,
        )
        base_tokens = baseline.tokens_input + baseline.tokens_output
        if base_tokens > 0:
            ratio = cur_tokens / base_tokens
            if ratio > 1.5:
                report.regressions.append({
                    "type": "tokens_drift",
                    "before": base_tokens,
                    "after": cur_tokens,
                    "ratio": round(ratio, 2),
                })

        # Duration drift > 2x
        cur_dur = current.get("duration_ms", 0)
        if baseline.duration_ms > 0:
            ratio = cur_dur / baseline.duration_ms
            if ratio > 2.0:
                report.regressions.append({
                    "type": "duration_drift",
                    "before": baseline.duration_ms,
                    "after": cur_dur,
                    "ratio": round(ratio, 2),
                })

        report.has_regression = bool(report.regressions)
        return report
