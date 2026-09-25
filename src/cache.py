"""SQLite-кэш ответов LLM с chunks-реплеем и метриками."""
from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ResponseCache:
    def __init__(self, path: Path | str, ttl: int = 3600):
        self.path = Path(path)
        self.ttl = ttl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS cache (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
            """)
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_created ON cache(created_at)"
            )
            conn.execute("""
                CREATE TABLE IF NOT EXISTS metrics (
                    key TEXT PRIMARY KEY,
                    value REAL NOT NULL DEFAULT 0
                )
            """)
            for k in ("hits", "misses", "sets", "evictions"):
                conn.execute(
                    "INSERT OR IGNORE INTO metrics(key, value) VALUES (?, 0)",
                    (k,),
                )

    @staticmethod
    def make_key(
        model: str, messages: list[dict],
        tools: list[dict] | None, state_hash: str | None = None,
    ) -> str:
        payload = {
            "model": model,
            "messages": messages,
            "tools": tools or [],
        }
        if state_hash:
            payload["state_hash"] = state_hash
        raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def get(self, key: str) -> tuple[Any | None, bool]:
        try:
            with sqlite3.connect(self.path) as conn:
                row = conn.execute(
                    "SELECT value, created_at FROM cache WHERE key = ?",
                    (key,),
                ).fetchone()
        except Exception as e:
            logger.warning("Cache get failed: %s", e)
            self._incr("misses")
            return None, False

        if not row:
            self._incr("misses")
            return None, False

        value, created_at = row
        if time.time() - created_at > self.ttl:
            self._delete(key)
            self._incr("evictions")
            self._incr("misses")
            return None, False

        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            self._incr("misses")
            return None, False

        self._incr("hits")
        return parsed, True

    def set(self, key: str, value: Any) -> None:
        try:
            with sqlite3.connect(self.path) as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO cache (key, value, created_at) "
                    "VALUES (?, ?, ?)",
                    (key, json.dumps(value, ensure_ascii=False, default=str),
                     time.time()),
                )
            self._incr("sets")
        except Exception as e:
            logger.warning("Cache set failed: %s", e)

    def _delete(self, key: str) -> None:
        try:
            with sqlite3.connect(self.path) as conn:
                conn.execute("DELETE FROM cache WHERE key = ?", (key,))
        except Exception:
            pass

    def _incr(self, key: str) -> None:
        try:
            with sqlite3.connect(self.path) as conn:
                conn.execute(
                    "INSERT INTO metrics(key, value) VALUES (?, 1) "
                    "ON CONFLICT(key) DO UPDATE SET value = value + 1",
                    (key,),
                )
        except Exception:
            pass

    def clear_all(self) -> int:
        with sqlite3.connect(self.path) as conn:
            cur = conn.execute("DELETE FROM cache")
            return cur.rowcount or 0

    def reset_metrics(self) -> None:
        with sqlite3.connect(self.path) as conn:
            conn.execute("UPDATE metrics SET value = 0")

    def stats(self) -> dict:
        try:
            with sqlite3.connect(self.path) as conn:
                total = conn.execute(
                    "SELECT COUNT(*) FROM cache"
                ).fetchone()[0]
                cutoff = time.time() - self.ttl
                fresh = conn.execute(
                    "SELECT COUNT(*) FROM cache WHERE created_at >= ?",
                    (cutoff,),
                ).fetchone()[0]
                metrics = dict(conn.execute(
                    "SELECT key, value FROM metrics"
                ).fetchall())
        except Exception as e:
            return {"total": 0, "fresh": 0, "ttl_seconds": self.ttl, "error": str(e)}

        hits = int(metrics.get("hits", 0))
        misses = int(metrics.get("misses", 0))
        total_req = hits + misses
        return {
            "total": total,
            "fresh": fresh,
            "ttl_seconds": self.ttl,
            "hits": hits,
            "misses": misses,
            "sets": int(metrics.get("sets", 0)),
            "evictions": int(metrics.get("evictions", 0)),
            "hit_rate": round(hits / total_req, 4) if total_req else 0.0,
        }
