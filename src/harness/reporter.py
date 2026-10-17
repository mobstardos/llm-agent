"""Отчёты harness: text, JSON, JUnit XML."""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from xml.etree import ElementTree as ET

from src.harness.schema import ScenarioRun

logger = logging.getLogger(__name__)


class Reporter:
    def __init__(self, output_dir: Path | None = None):
        self.output_dir = output_dir
        if output_dir:
            output_dir.mkdir(parents=True, exist_ok=True)

    # ═══════════════════════════════════════════════════════
    # Text
    # ═══════════════════════════════════════════════════════
    def render_text(self, runs: list[ScenarioRun]) -> str:
        lines: list[str] = []
        lines.append("═" * 60)
        lines.append("  Harness Report")
        lines.append("═" * 60)
        lines.append("")

        passed = sum(1 for r in runs if r.passed)
        failed = len(runs) - passed

        for r in runs:
            symbol = "✓" if r.passed else "✗"
            lines.append(
                f"{symbol} {r.scenario_id:<40} "
                f"{r.iterations} steps  {r.duration_ms/1000:.1f}s"
            )
            if not r.passed:
                for a in r.assertions:
                    if not a.passed and a.severity == "hard":
                        lines.append(f"    FAIL: {a.message}")
                if r.error:
                    lines.append(f"    ERROR: {r.error}")

        lines.append("")
        lines.append("─" * 60)
        lines.append(f"Итого: {passed} passed, {failed} failed")
        lines.append("═" * 60)

        return "\n".join(lines)

    # ═══════════════════════════════════════════════════════
    # JSON
    # ═══════════════════════════════════════════════════════
    def render_json(self, runs: list[ScenarioRun]) -> str:
        return json.dumps(
            [r.model_dump() for r in runs],
            ensure_ascii=False, indent=2,
        )

    def save_json(
        self, runs: list[ScenarioRun],
        filename: str = "report.json",
    ) -> Path:
        if not self.output_dir:
            raise ValueError("output_dir не задан")
        path = self.output_dir / filename
        path.write_text(self.render_json(runs), encoding="utf-8")
        return path

    # ═══════════════════════════════════════════════════════
    # JUnit XML (для CI)
    # ═══════════════════════════════════════════════════════
    def render_junit(self, runs: list[ScenarioRun]) -> str:
        """JUnit XML формат для GitHub Actions / GitLab / Jenkins."""
        testsuites = ET.Element("testsuites")
        testsuites.set("name", "llm-agent-harness")

        # Группируем по scenario_id (каждый сценарий — отдельный suite)
        for r in runs:
            suite = ET.SubElement(testsuites, "testsuite")
            suite.set("name", r.scenario_id)
            suite.set("tests", "1")
            suite.set("failures", "0" if r.passed else "1")
            suite.set("errors", "1" if r.error else "0")
            suite.set("time", f"{r.duration_ms/1000:.3f}")

            testcase = ET.SubElement(suite, "testcase")
            testcase.set("name", r.scenario_id)
            testcase.set("classname", "harness")
            testcase.set("time", f"{r.duration_ms/1000:.3f}")

            if r.error:
                err = ET.SubElement(testcase, "error")
                err.set("message", r.error[:200])
                err.text = r.error
            elif not r.passed:
                failure = ET.SubElement(testcase, "failure")
                # Собираем все hard-фейлы
                failed_assertions = [
                    a for a in r.assertions
                    if not a.passed and a.severity == "hard"
                ]
                msg = "; ".join(
                    f"[{a.type}] {a.message}" for a in failed_assertions
                ) or f"exit_reason={r.exit_reason}"
                failure.set("message", msg[:500])
                failure.text = "\n".join(
                    f"FAIL: {a.type} — {a.message}"
                    for a in failed_assertions
                )

            # stdout/system-out
            sysout = ET.SubElement(testcase, "system-out")
            sysout.text = json.dumps({
                "iterations": r.iterations,
                "tokens_input": r.tokens_input,
                "tokens_output": r.tokens_output,
                "tool_calls": r.tool_calls_count,
                "exit_reason": r.exit_reason,
                "trace_id": r.trace_id,
            }, ensure_ascii=False, indent=2)

        # Формируем красивое дерево
        ET.indent(testsuites, space="  ")
        return ET.tostring(testsuites, encoding="unicode")

    def save_junit(
        self, runs: list[ScenarioRun],
        filename: str = "harness-report.xml",
    ) -> Path:
        if not self.output_dir:
            raise ValueError("output_dir не задан")
        path = self.output_dir / filename
        path.write_text(self.render_junit(runs), encoding="utf-8")
        return path
