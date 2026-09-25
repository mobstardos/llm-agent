"""Постоянные approval-политики."""
from __future__ import annotations

import fnmatch
import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

EXPORT_VERSION = 1


class PolicyStore:
    def __init__(self, path: str | Path, ttl_days: int = 30,
                 deleted_history_limit: int = 1000):
        self.path = Path(path)
        self.ttl_days = ttl_days
        self.deleted_history_limit = deleted_history_limit
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS approval_policies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tool TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    path_pattern TEXT,
                    decision TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_used_at REAL NOT NULL,
                    use_count INTEGER NOT NULL DEFAULT 0
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_tool ON approval_policies(tool)"
            )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS deleted_policies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tool TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    decision TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    last_used_at REAL NOT NULL,
                    deleted_at REAL NOT NULL,
                    use_count INTEGER NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL
                )
            """)

    def check(self, tool: str, args: dict | None) -> bool | None:
        path_arg = (args or {}).get("path")
        with sqlite3.connect(self.path) as conn:
            rows = conn.execute(
                "SELECT id, scope, path_pattern, decision FROM approval_policies "
                "WHERE tool = ? ORDER BY created_at DESC",
                (tool,),
            ).fetchall()

        for pid, scope, pattern, decision in rows:
            matched = False
            if scope == "tool":
                matched = True
            elif scope == "path" and pattern and path_arg:
                norm = str(path_arg).replace("\\", "/")
                if fnmatch.fnmatch(norm, pattern):
                    matched = True
            if matched:
                try:
                    with sqlite3.connect(self.path) as conn:
                        conn.execute(
                            "UPDATE approval_policies SET last_used_at = ?, "
                            "use_count = use_count + 1 WHERE id = ?",
                            (time.time(), pid),
                        )
                except Exception:
                    pass
                return decision == "allow"
        return None

    def add(self, tool: str, scope: str, decision: str,
            path_pattern: str | None = None) -> int:
        now = time.time()
        with sqlite3.connect(self.path) as conn:
            existing = conn.execute(
                "SELECT id FROM approval_policies WHERE tool = ? AND scope = ? "
                "AND COALESCE(path_pattern, '') = ? AND decision = ?",
                (tool, scope, path_pattern or "", decision),
            ).fetchone()
            if existing:
                return existing[0]
            cur = conn.execute(
                "INSERT INTO approval_policies "
                "(tool, scope, path_pattern, decision, created_at, last_used_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (tool, scope, path_pattern, decision, now, now),
            )
            return cur.lastrowid or 0

    def list_all(self) -> list[dict]:
        with sqlite3.connect(self.path) as conn:
            rows = conn.execute(
                "SELECT id, tool, scope, path_pattern, decision, "
                "created_at, last_used_at, use_count "
                "FROM approval_policies ORDER BY created_at DESC"
            ).fetchall()
        return [
            {
                "id": r[0], "tool": r[1], "scope": r[2],
                "path_pattern": r[3], "decision": r[4],
                "created_at": r[5], "last_used_at": r[6], "use_count": r[7],
            }
            for r in rows
        ]

    def delete(self, policy_id: int) -> bool:
        with sqlite3.connect(self.path) as conn:
            cur = conn.execute(
                "DELETE FROM approval_policies WHERE id = ?", (policy_id,),
            )
            return (cur.rowcount or 0) > 0

    def clear_all(self) -> int:
        with sqlite3.connect(self.path) as conn:
            cur = conn.execute("DELETE FROM approval_policies")
            return cur.rowcount or 0

    def cleanup_old(self) -> int:
        if self.ttl_days <= 0:
            return 0
        cutoff = time.time() - self.ttl_days * 86400
        with sqlite3.connect(self.path) as conn:
            cur = conn.execute(
                "DELETE FROM approval_policies WHERE last_used_at < ?",
                (cutoff,),
            )
            return cur.rowcount or 0

    def stats(self) -> dict:
        with sqlite3.connect(self.path) as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM approval_policies"
            ).fetchone()[0]
            by_decision = dict(conn.execute(
                "SELECT decision, COUNT(*) FROM approval_policies GROUP BY decision"
            ).fetchall())
        return {
            "total": total,
            "ttl_days": self.ttl_days,
            "by_decision": by_decision,
        }

    def backup_to_file(self, path: str | Path) -> Path:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "version": EXPORT_VERSION,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "count": len(self.list_all()),
            "policies": self.list_all(),
        }
        p.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return p

    def backup_timestamped(self, backup_dir: str | Path, keep: int = 7) -> Path:
        d = Path(backup_dir)
        d.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        p = d / f"policies-{ts}.json"
        self.backup_to_file(p)
        if keep > 0:
            files = sorted(d.glob("policies-*.json"))
            for old in files[:-keep]:
                try:
                    old.unlink()
                except OSError:
                    pass
        return p
