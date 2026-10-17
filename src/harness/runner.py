"""HarnessRunner — запись и воспроизведение сценариев."""
from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
from typing import Any

from src.core.schema import ScenarioSpec
from src.harness.assertions import run_assertion
from src.harness.baseline import BaselineStore
from src.harness.chaos import build_fault_injector
from src.harness.fixtures import FixtureRunner
from src.harness.mocks import (
    LlmFixtureStore,
    MockLLMClient,
    MockMCPManager,
    RecordingLLMClient,
    RecordingMCPManager,
)
from src.harness.sandbox import Sandbox
from src.harness.schema import ScenarioRun
from src.harness.trace_reader import TraceReader
from src.loop.context import LoopContext
from src.loop.controller import LoopController
from src.loop.spec import LoopSpecLoader
from src.loop.telemetry import LoopTelemetry

logger = logging.getLogger(__name__)


class HarnessRunner:
    """Режимы:
      - replay: --mock-llm (по умолчанию в CI)
      - record: --record (записать реальные ответы)
      - real:   обычный прогон
    """

    def __init__(
        self,
        *,
        base_dir: Path | None = None,
        loops_dir: Path | None = None,
        telemetry: LoopTelemetry | None = None,
        runs_dir: Path | None = None,
        keep_runs: bool = True,
        fixtures_dir: Path | None = None,
    ):
        self.base_dir = base_dir or Path(__file__).resolve().parent.parent.parent
        self.loops_dir = loops_dir or self.base_dir / "loops"
        self.telemetry = telemetry
        self.runs_dir = runs_dir or self.base_dir / "data" / "harness_runs"
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.keep_runs = keep_runs

        self.fixtures_dir = fixtures_dir or self.base_dir / "harness" / "fixtures" / "llm"
        self.fixture_store = LlmFixtureStore(self.fixtures_dir)

        self.controller = LoopController(telemetry=telemetry)
        self.loop_loader = LoopSpecLoader(base_dir=self.base_dir)
        self.loop_loader.load_all()

    # ═══════════════════════════════════════════════════════
    # Public
    # ═══════════════════════════════════════════════════════
    async def run_scenario(
        self,
        spec: ScenarioSpec,
        *,
        llm_client: Any = None,
        mcp_manager: Any = None,
        mock_llm: bool = False,
        mock_mcp: bool = False,
        record: bool = False,
        strict_mock: bool = True,
        mock_responses: dict | None = None,
    ) -> ScenarioRun:
        scenario_id = spec.id
        t0 = time.perf_counter()

        sandbox = Sandbox(self.runs_dir)
        run_id = sandbox.run_id

        try:
            # 1. Fixtures
            fx = FixtureRunner(sandbox=sandbox.path)
            project_root = await fx.apply(spec.fixture)

            # 2. LLM — выбор режима
            recorder_llm = None
            if record:
                if llm_client is None:
                    return self._error_run(
                        scenario_id, "record mode требует llm_client",
                        t0, run_id,
                    )
                recorder_llm = RecordingLLMClient(llm_client)
                llm = recorder_llm
            elif mock_llm:
                fixtures = self.fixture_store.load(scenario_id)
                if not fixtures:
                    return self._error_run(
                        scenario_id,
                        f"нет fixtures для {scenario_id}; "
                        f"сначала запустите с --record",
                        t0, run_id,
                    )
                exchanges = fixtures.get("exchanges", [])
                llm = MockLLMClient(exchanges, strict=strict_mock)
            else:
                llm = llm_client

            # 3. MCP — выбор режима
            recorder_mcp = None
            if mock_mcp:
                mcp_responses = (mock_responses or {}).get("mcp", {})
                mcp = MockMCPManager(responses=mcp_responses)
            elif record and mcp_manager is not None:
                recorder_mcp = RecordingMCPManager(mcp_manager)
                mcp = recorder_mcp
            else:
                mcp = mcp_manager

            # 4. Loop spec
            loop_spec = self.loop_loader.get(spec.loop)
            if loop_spec is None:
                return self._error_run(
                    scenario_id, f"Loop '{spec.loop}' не найден",
                    t0, run_id,
                )

            # 5. Faults
            faults = build_fault_injector(spec.chaos)

            # 6. Tools
            tools: list[dict] = []
            if mcp is not None:
                srv = spec.options.get("mcp_servers", ["filesystem"])
                tools = mcp.get_openai_tools(srv)

            # 7. Context
            trace_id = uuid.uuid4().hex[:16]
            ctx = LoopContext(
                llm_client=llm,
                model=spec.options.get("model"),
                mcp_manager=mcp,
                available_tools=tools,
                prompt=spec.prompt,
                system_prompt=spec.options.get(
                    "system_prompt", "Ты — агент.",
                ),
                user_template=spec.options.get(
                    "user_template", "{query}",
                ),
                context_text="",
                faults=faults,
                trace_id=trace_id,
                session_id=run_id,
                agent_id=spec.options.get("agent_id", scenario_id),
                extra={
                    "scenario_id": scenario_id,
                    "max_result_chars": 30000,
                    "run_id": run_id,
                },
            )
            ctx.file_state = _DummyFileState(project_root)

            # 8. Run
            result = await self.controller.run(loop_spec, ctx)

            # 9. Если record — сохранить fixtures
            if record and recorder_llm is not None:
                model_name = spec.options.get("model", "unknown")
                self.fixture_store.save(
                    scenario_id, model_name, recorder_llm.exchanges,
                )

            # 10. Tool calls
            tool_calls: list[tuple[str, dict]] = []
            if isinstance(mcp, MockMCPManager):
                tool_calls = list(mcp.calls)
            elif isinstance(mcp, RecordingMCPManager):
                tool_calls = [(c["name"], c["arguments"]) for c in mcp.calls]
            elif self.telemetry:
                tr = TraceReader(self.telemetry.path)
                tool_calls = tr.get_tool_calls(trace_id)

            response_text = result.response or ""

            # 11. Assertions
            assertion_results = []
            for a in spec.assertions:
                r = await run_assertion(
                    a, project_root, result, tool_calls, response_text,
                )
                assertion_results.append(r)

            hard_failed = sum(
                1 for r in assertion_results
                if not r["passed"] and r["severity"] == "hard"
            )
            soft_failed = sum(
                1 for r in assertion_results
                if not r["passed"] and r["severity"] == "soft"
            )

            passed = (
                result.exit_reason == "success" and hard_failed == 0
            )

            # 12. Baseline
            baseline_store = BaselineStore(
                self.base_dir / "harness" / "baselines"
            )
            current_dict = {
                "passed": passed,
                "iterations": result.iterations,
                "tokens_input": result.tokens_input,
                "tokens_output": result.tokens_output,
                "duration_ms": result.duration_ms,
            }
            regression = baseline_store.compare(scenario_id, current_dict)

            duration_ms = (time.perf_counter() - t0) * 1000

            return ScenarioRun(
                scenario_id=scenario_id,
                passed=passed,
                hard_failures=hard_failed,
                soft_failures=soft_failed,
                duration_ms=duration_ms,
                iterations=result.iterations,
                tokens_input=result.tokens_input,
                tokens_output=result.tokens_output,
                tool_calls_count=result.tool_calls_count,
                exit_reason=result.exit_reason,
                assertions=[
                    _to_assertion_result(x) for x in assertion_results
                ],
                trace_id=trace_id,
                run_dir=str(sandbox.path),
            )

        except Exception as e:
            logger.exception("Scenario %s failed", scenario_id)
            return self._error_run(scenario_id, str(e), t0, run_id)

        finally:
            sandbox.cleanup(keep=self.keep_runs)

    # ═══════════════════════════════════════════════════════
    # Bulk
    # ═══════════════════════════════════════════════════════
    async def run_suite(
        self, specs: list[ScenarioSpec], **kwargs,
    ) -> list[ScenarioRun]:
        runs: list[ScenarioRun] = []
        for s in specs:
            logger.info("Running scenario: %s", s.id)
            r = await self.run_scenario(s, **kwargs)
            runs.append(r)
        return runs

    # ═══════════════════════════════════════════════════════
    # Helpers
    # ═══════════════════════════════════════════════════════
    def _error_run(
        self, scenario_id: str, error: str,
        t0: float, run_id: str,
    ) -> ScenarioRun:
        return ScenarioRun(
            scenario_id=scenario_id,
            passed=False,
            error=error,
            duration_ms=(time.perf_counter() - t0) * 1000,
            run_dir=run_id,
        )


def _to_assertion_result(x: dict):
    from src.harness.schema import AssertionResult
    return AssertionResult(
        type=x["type"], passed=x["passed"],
        severity=x["severity"], message=x["message"],
    )


class _DummyFileState:
    def __init__(self, root: Path):
        self.root = root

    def hash(self) -> str:
        return ""

    def invalidate(self) -> None:
        pass
