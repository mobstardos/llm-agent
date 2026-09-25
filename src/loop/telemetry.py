"""Телеметрия loop: пишет в SQLite и метрики."""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS loop_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    trace_id TEXT NOT NULL,
    loop_id TEXT NOT NULL,
    parent_loop_id TEXT,
    agent_id TEXT,
    started_at REAL NOT NULL,
    ended_at REAL,
    iterations INTEGER,
    exit_reason TEXT,
    success INTEGER,
    tokens_input INTEGER,
    tokens_output INTEGER,
    tool_calls_count INTEGER,
    duration_ms REAL,
    session_id TEXT,
    scenario_id TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_loop_trace ON loop_runs(trace_id);
CREATE INDEX IF NOT EXISTS idx_loop_loop_id ON loop_runs(loop_id);
CREATE INDEX IF NOT EXISTS idx_loop_scenario ON loop_runs(scenario_id);

CREATE TABLE IF NOT EXISTS loop_iterations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    loop_run_id INTEGER NOT NULL,
    i INTEGER NOT NULL,
    ts REAL NOT NULL,
    detail_json TEXT,
    FOREIGN KEY (loop_run_id) REFERENCES loop_runs(id)
);
CREATE INDEX IF NOT EXISTS idx_iter_run ON loop_iterations(loop_run_id);
"""


class LoopTelemetry:
    """Пишет трейсы loop в SQLite."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        # Метрики в памяти
        self._counters: dict[str, int] = {
            "loop_runs_total": 0,
            "loop_success_total": 0,
            "loop_failure_total": 0,
        }
        self._by_loop: dict[str, dict] = {}

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def start_run(
        self,
        trace_id: str,
        loop_id: str,
        parent_loop_id: str | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        scenario_id: str | None = None,
    ) -> int:
        try:
            with self._conn() as conn:
                cur = conn.execute(
                    "INSERT INTO loop_runs "
                    "(trace_id, loop_id, parent_loop_id, agent_id, started_at, "
                    "session_id, scenario_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        trace_id, loop_id, parent_loop_id, agent_id,
                        time.time(), session_id, scenario_id,
                    ),
                )
                return cur.lastrowid or 0
        except Exception as e:
            logger.warning("Telemetry start_run failed: %s", e)
            return 0

    def end_run(self, run_id: int, result) -> None:
        if not run_id:
            return
        try:
            with self._conn() as conn:
                conn.execute(
                    "UPDATE loop_runs SET ended_at = ?, iterations = ?, "
                    "exit_reason = ?, success = ?, tokens_input = ?, "
                    "tokens_output = ?, tool_calls_count = ?, "
                    "duration_ms = ?, error = ? WHERE id = ?",
                    (
                        time.time(),
                        result.iterations,
                        result.exit_reason,
                        int(result.success),
                        result.tokens_input,
                        result.tokens_output,
                        result.tool_calls_count,
                        result.duration_ms,
                        result.error,
                        run_id,
                    ),
                )
        except Exception as e:
            logger.warning("Telemetry end_run failed: %s", e)

        # Update in-memory
        self._counters["loop_runs_total"] += 1
        if result.success:
            self._counters["loop_success_total"] += 1
        else:
            self._counters["loop_failure_total"] += 1

        lid = result.loop_id
        b = self._by_loop.setdefault(lid, {
            "runs": 0, "success": 0, "failure": 0,
            "total_iterations": 0, "total_duration_ms": 0,
        })
        b["runs"] += 1
        if result.success:
            b["success"] += 1
        else:
            b["failure"] += 1
        b["total_iterations"] += result.iterations
        b["total_duration_ms"] += result.duration_ms

    def record_iteration(
        self, run_id: int, i: int, detail: dict | None = None,
    ) -> None:
        if not run_id:
            return
        try:
            with self._conn() as conn:
                conn.execute(
                    "INSERT INTO loop_iterations "
                    "(loop_run_id, i, ts, detail_json) VALUES (?, ?, ?, ?)",
                    (
                        run_id, i, time.time(),
                        json.dumps(detail, ensure_ascii=False, default=str)
                        if detail else None,
                    ),
                )
        except Exception as e:
            logger.debug("Telemetry record_iteration failed: %s", e)

    # ═══════════════════════════════════════════════════════
    # Metrics
    # ═══════════════════════════════════════════════════════
    def render(self) -> list[str]:
        """Prometheus-строки для loop-метрик."""
        lines: list[str] = []
        lines.append("# HELP llmagent_loop_runs_total")
        lines.append("# TYPE llmagent_loop_runs_total counter")
        lines.append(
            f"llmagent_loop_runs_total {self._counters['loop_runs_total']}"
        )
        lines.append("# HELP llmagent_loop_success_total")
        lines.append("# TYPE llmagent_loop_success_total counter")
        lines.append(
            f"llmagent_loop_success_total {self._counters['loop_success_total']}"
        )
        lines.append("# HELP llmagent_loop_failure_total")
        lines.append("# TYPE llmagent_loop_failure_total counter")
        lines.append(
            f"llmagent_loop_failure_total {self._counters['loop_failure_total']}"
        )

        lines.append("# HELP llmagent_loop_runs_by_id")
        lines.append("# TYPE llmagent_loop_runs_by_id counter")
        for lid, b in self._by_loop.items():
            lines.append(
                f'llmagent_loop_runs_by_id{{loop="{lid}"}} {b["runs"]}'
            )
        lines.append("# HELP llmagent_loop_avg_iterations")
        lines.append("# TYPE llmagent_loop_avg_iterations gauge")
        for lid, b in self._by_loop.items():
            if b["runs"] > 0:
                avg = b["total_iterations"] / b["runs"]
                lines.append(
                    f'llmagent_loop_avg_iterations{{loop="{lid}"}} {avg:.2f}'
                )
        lines.append("# HELP llmagent_loop_avg_duration_ms")
        lines.append("# TYPE llmagent_loop_avg_duration_ms gauge")
        for lid, b in self._by_loop.items():
            if b["runs"] > 0:
                avg = b["total_duration_ms"] / b["runs"]
                lines.append(
                    f'llmagent_loop_avg_duration_ms{{loop="{lid}"}} {avg:.1f}'
                )
        return lines

    def stats(self, window_hours: int = 24) -> dict:
        cutoff = time.time() - window_hours * 3600
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT loop_id, exit_reason, COUNT(*) as c, "
                "AVG(iterations) as avg_iter, AVG(duration_ms) as avg_dur "
                "FROM loop_runs WHERE started_at >= ? "
                "GROUP BY loop_id, exit_reason",
                (cutoff,),
            ).fetchall()
        return {
            "window_hours": window_hours,
            "rows": [dict(r) for r in rows],
            "totals": dict(self._counters),
        }
