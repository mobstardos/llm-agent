"""Чтение loop-трейсов из telemetry для assertions."""
from __future__ import annotations

import logging
import sqlite3
from pathlib import Path

logger = logging.getLogger(__name__)


class TraceReader:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)

    def get_tool_calls(self, trace_id: str) -> list[tuple[str, dict]]:
        """Извлекает tool calls из trace."""
        if not self.db_path.exists():
            return []
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                rows = conn.execute(
                    "SELECT detail_json FROM loop_iterations "
                    "WHERE loop_run_id IN ("
                    "  SELECT id FROM loop_runs WHERE trace_id = ?"
                    ") ORDER BY i",
                    (trace_id,),
                ).fetchall()
        except Exception as e:
            logger.debug("Trace read failed: %s", e)
            return []

        import json
        calls: list[tuple[str, dict]] = []
        for r in rows:
            if not r["detail_json"]:
                continue
            try:
                d = json.loads(r["detail_json"])
                for tc in (d.get("tool_calls") or []):
                    name = tc.get("function", {}).get("name")
                    args = tc.get("function", {}).get("arguments", "{}")
                    if isinstance(args, str):
                        try:
                            args = json.loads(args)
                        except Exception:
                            args = {}
                    if name:
                        calls.append((name, args))
            except Exception:
                continue
        return calls
