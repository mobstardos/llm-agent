"""Аудит изменений настроек."""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT,
    before_json TEXT,
    after_json TEXT,
    details_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON settings_audit(ts DESC);
CREATE INDEX IF NOT EXISTS idx_audit_target ON settings_audit(target);
CREATE INDEX IF NOT EXISTS idx_audit_action ON settings_audit(action);
"""


class AuditStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

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

    def log(
        self,
        actor: str,
        action: str,
        target: str | None = None,
        before: Any = None,
        after: Any = None,
        details: dict | None = None,
    ) -> int:
        try:
            with self._conn() as conn:
                cur = conn.execute(
                    "INSERT INTO settings_audit "
                    "(ts, actor, action, target, before_json, after_json, "
                    "details_json) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        time.time(), actor, action, target,
                        json.dumps(before, ensure_ascii=False, default=str)
                        if before is not None else None,
                        json.dumps(after, ensure_ascii=False, default=str)
                        if after is not None else None,
                        json.dumps(details, ensure_ascii=False, default=str)
                        if details else None,
                    ),
                )
                return cur.lastrowid or 0
        except Exception as e:
            logger.warning("Audit log упал: %s", e)
            return 0

    def list(
        self,
        limit: int = 100,
        target: str | None = None,
        action: str | None = None,
        since: float | None = None,
    ) -> list[dict]:
        sql = "SELECT * FROM settings_audit WHERE 1=1"
        params: list = []
        if target:
            sql += " AND target = ?"
            params.append(target)
        if action:
            sql += " AND action = ?"
            params.append(action)
        if since:
            sql += " AND ts >= ?"
            params.append(since)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)

        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()

        return [
            {
                "id": r["id"],
                "ts": r["ts"],
                "actor": r["actor"],
                "action": r["action"],
                "target": r["target"],
                "before": json.loads(r["before_json"]) if r["before_json"] else None,
                "after": json.loads(r["after_json"]) if r["after_json"] else None,
                "details": json.loads(r["details_json"]) if r["details_json"] else None,
            }
            for r in rows
        ]

    def cleanup(self, retention_days: int = 365) -> int:
        cutoff = time.time() - retention_days * 86400
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM settings_audit WHERE ts < ?", (cutoff,),
            )
            return cur.rowcount or 0
