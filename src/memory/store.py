"""SQLite для episodic memory."""
from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from src.memory.base import Event, Message, Session, Summary, new_id, now_ts

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    started_at REAL NOT NULL,
    ended_at REAL,
    title TEXT,
    summary TEXT,
    message_count INTEGER DEFAULT 0,
    tokens_used INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_sessions_started ON sessions(started_at DESC);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    ts REAL NOT NULL,
    role TEXT NOT NULL,
    content TEXT,
    tokens INTEGER DEFAULT 0,
    tool_calls_json TEXT,
    tool_call_id TEXT,
    FOREIGN KEY (session_id) REFERENCES sessions(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, ts);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    trace_id TEXT,
    ts REAL NOT NULL,
    type TEXT NOT NULL,
    agent TEXT,
    summary TEXT,
    details_json TEXT,
    success INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_type ON events(type, ts);

CREATE TABLE IF NOT EXISTS summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    scope TEXT NOT NULL,
    content TEXT NOT NULL,
    covers_from REAL NOT NULL,
    covers_to REAL NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_summaries_scope ON summaries(scope, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_summaries_session ON summaries(session_id);
"""


class EpisodicStore:
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

    # ─── Sessions ─────────────────────────────────────
    def create_session(self, title: str | None = None) -> Session:
        sid = new_id("sess_")
        ts = now_ts()
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO sessions (id, started_at, title) VALUES (?, ?, ?)",
                (sid, ts, title),
            )
        return Session(id=sid, started_at=ts, title=title)

    def end_session(self, session_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE sessions SET ended_at = ? WHERE id = ?",
                (now_ts(), session_id),
            )

    def get_session(self, session_id: str) -> Session | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,),
            ).fetchone()
        if not row:
            return None
        return Session(
            id=row["id"], started_at=row["started_at"],
            ended_at=row["ended_at"], title=row["title"],
            summary=row["summary"],
            message_count=row["message_count"],
            tokens_used=row["tokens_used"],
        )

    def set_session_summary(self, session_id: str, summary: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE sessions SET summary = ? WHERE id = ?",
                (summary, session_id),
            )

    def recent_sessions(self, limit: int = 20) -> list[Session]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM sessions ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            Session(
                id=r["id"], started_at=r["started_at"],
                ended_at=r["ended_at"], title=r["title"],
                summary=r["summary"],
                message_count=r["message_count"],
                tokens_used=r["tokens_used"],
            )
            for r in rows
        ]

    # ─── Messages ─────────────────────────────────────
    def add_message(self, msg: Message, max_chars: int = 50000) -> int:
        content = msg.content or ""
        if len(content) > max_chars:
            content = content[:max_chars] + "\n...[обрезано]"

        tool_calls_json = (
            json.dumps(msg.tool_calls, ensure_ascii=False, default=str)
            if msg.tool_calls else None
        )
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO messages (session_id, ts, role, content, tokens, "
                "tool_calls_json, tool_call_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    msg.session_id, msg.ts, msg.role, content, msg.tokens,
                    tool_calls_json, msg.tool_call_id,
                ),
            )
            conn.execute(
                "UPDATE sessions SET message_count = message_count + 1, "
                "tokens_used = tokens_used + ? WHERE id = ?",
                (msg.tokens, msg.session_id),
            )
            return cur.lastrowid or 0

    def get_messages(
        self, session_id: str, limit: int | None = None,
    ) -> list[Message]:
        with self._conn() as conn:
            if limit:
                rows = conn.execute(
                    "SELECT * FROM messages WHERE session_id = ? "
                    "ORDER BY ts DESC LIMIT ?",
                    (session_id, limit),
                ).fetchall()
                rows = list(reversed(rows))
            else:
                rows = conn.execute(
                    "SELECT * FROM messages WHERE session_id = ? ORDER BY ts",
                    (session_id,),
                ).fetchall()
        return [
            Message(
                id=r["id"], session_id=r["session_id"],
                role=r["role"], content=r["content"],
                ts=r["ts"], tokens=r["tokens"],
                tool_calls=(json.loads(r["tool_calls_json"])
                            if r["tool_calls_json"] else None),
                tool_call_id=r["tool_call_id"],
            )
            for r in rows
        ]

    def count_messages(self, session_id: str) -> int:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT COUNT(*) as c FROM messages WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        return row["c"] if row else 0

    # ─── Events ───────────────────────────────────────
    def add_event(self, event: Event) -> int:
        details_json = (
            json.dumps(event.details, ensure_ascii=False, default=str)
            if event.details else None
        )
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO events (session_id, trace_id, ts, type, agent, "
                "summary, details_json, success) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.session_id, event.trace_id, event.ts, event.type,
                    event.agent, event.summary, details_json,
                    int(event.success),
                ),
            )
            return cur.lastrowid or 0

    def get_events(
        self, session_id: str | None = None,
        event_type: str | None = None,
        limit: int = 100,
    ) -> list[Event]:
        sql = "SELECT * FROM events WHERE 1=1"
        params: list[Any] = []
        if session_id:
            sql += " AND session_id = ?"
            params.append(session_id)
        if event_type:
            sql += " AND type = ?"
            params.append(event_type)
        sql += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)

        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            Event(
                id=r["id"], session_id=r["session_id"],
                trace_id=r["trace_id"], ts=r["ts"],
                type=r["type"], agent=r["agent"],
                summary=r["summary"], success=bool(r["success"]),
                details=(json.loads(r["details_json"])
                         if r["details_json"] else None),
            )
            for r in rows
        ]

    def find_similar_events(
        self, query_terms: list[str], limit: int = 5,
    ) -> list[Event]:
        if not query_terms:
            return []
        like = "%" + "%".join(query_terms[:3]) + "%"
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM events WHERE summary LIKE ? "
                "ORDER BY ts DESC LIMIT ?",
                (like, limit),
            ).fetchall()
        return [
            Event(
                id=r["id"], session_id=r["session_id"],
                trace_id=r["trace_id"], ts=r["ts"],
                type=r["type"], agent=r["agent"],
                summary=r["summary"], success=bool(r["success"]),
                details=(json.loads(r["details_json"])
                         if r["details_json"] else None),
            )
            for r in rows
        ]

    # ─── Summaries ────────────────────────────────────
    def add_summary(self, s: Summary) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "INSERT INTO summaries (session_id, scope, content, "
                "covers_from, covers_to, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (s.session_id, s.scope, s.content,
                 s.covers_from, s.covers_to, s.created_at),
            )
            return cur.lastrowid or 0

    def get_summaries(
        self, scope: str | None = None,
        session_id: str | None = None,
        limit: int = 50,
    ) -> list[Summary]:
        sql = "SELECT * FROM summaries WHERE 1=1"
        params: list[Any] = []
        if scope:
            sql += " AND scope = ?"
            params.append(scope)
        if session_id:
            sql += " AND session_id = ?"
            params.append(session_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        with self._conn() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            Summary(
                id=r["id"], session_id=r["session_id"],
                scope=r["scope"], content=r["content"],
                covers_from=r["covers_from"], covers_to=r["covers_to"],
                created_at=r["created_at"],
            )
            for r in rows
        ]

    # ─── Retention ────────────────────────────────────
    def cleanup(self, retention) -> dict[str, int]:
        import time
        now = time.time()
        removed: dict[str, int] = {}
        with self._conn() as conn:
            cutoff = now - retention.raw_messages_days * 86400
            cur = conn.execute("DELETE FROM messages WHERE ts < ?", (cutoff,))
            removed["messages"] = cur.rowcount or 0

            cutoff = now - retention.events_days * 86400
            cur = conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
            removed["events"] = cur.rowcount or 0

            cutoff = now - retention.summaries_days * 86400
            cur = conn.execute(
                "DELETE FROM summaries WHERE created_at < ?", (cutoff,),
            )
            removed["summaries"] = cur.rowcount or 0
        return removed
