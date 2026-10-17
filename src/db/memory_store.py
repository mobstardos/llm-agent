"""Episodic memory на PostgreSQL."""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from src.db.pool import DatabasePool

logger = logging.getLogger(__name__)


class MemoryStore:
    def __init__(self, pool: DatabasePool):
        self.pool = pool

    async def create_session(self, title: str | None = None) -> str:
        sid = str(uuid.uuid4())
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "INSERT INTO memory.sessions (id, title) VALUES (%s, %s)",
                    (sid, title),
                )
        return sid

    async def end_session(self, session_id: str) -> None:
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "UPDATE memory.sessions SET ended_at = now() WHERE id = %s",
                    (session_id,),
                )

    async def get_session(self, session_id: str) -> dict | None:
        return await self.pool.execute_one(
            "SELECT * FROM memory.sessions WHERE id = %s", (session_id,),
        )

    async def add_message(
        self, session_id: str, role: str, content: str,
        tokens: int = 0,
        tool_calls: list | None = None,
        tool_call_id: str | None = None,
    ) -> int:
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    INSERT INTO memory.messages
                        (session_id, role, content, tokens, tool_calls, tool_call_id)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (
                    session_id, role, content, tokens,
                    json.dumps(tool_calls) if tool_calls else None,
                    tool_call_id,
                ))
                row = await cur.fetchone()
                await cur.execute(
                    "UPDATE memory.sessions SET message_count = message_count + 1, "
                    "tokens_used = tokens_used + %s WHERE id = %s",
                    (tokens, session_id),
                )
                return row["id"] if row else 0

    async def get_recent_messages(
        self, session_id: str, limit: int = 50,
    ) -> list[dict]:
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    SELECT * FROM (
                        SELECT * FROM memory.messages
                        WHERE session_id = %s
                        ORDER BY created_at DESC
                        LIMIT %s
                    ) sub ORDER BY created_at ASC
                """, (session_id, limit))
                return await cur.fetchall()

    async def add_event(
        self, type_: str, summary: str,
        session_id: str | None = None,
        trace_id: str | None = None,
        agent: str | None = None,
        details: dict | None = None,
        success: bool = True,
        importance: float | None = None,
        topic: str | None = None,
        tags: list[str] | None = None,
    ) -> int:
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    INSERT INTO memory.events
                        (session_id, trace_id, type, agent, summary, details,
                         success, importance, topic, tags)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id
                """, (
                    session_id, trace_id, type_, agent, summary,
                    json.dumps(details) if details else None,
                    success, importance, topic, tags or [],
                ))
                row = await cur.fetchone()
                return row["id"] if row else 0

    async def get_events(
        self, session_id: str | None = None,
        event_type: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        conditions: list[str] = []
        params: list[Any] = []
        if session_id:
            conditions.append("session_id = %s")
            params.append(session_id)
        if event_type:
            conditions.append("type = %s")
            params.append(event_type)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(limit)
        return await self.pool.execute(
            f"SELECT * FROM memory.events {where} "
            f"ORDER BY created_at DESC LIMIT %s",
            tuple(params),
        )

    async def add_summary(
        self, scope: str, content: str,
        covers_from: float, covers_to: float,
        session_id: str | None = None,
        importance: float | None = None,
    ) -> str:
        sid = str(uuid.uuid4())
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    INSERT INTO memory.summaries
                        (id, session_id, scope, content,
                         covers_from, covers_to, importance)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (
                    sid, session_id, scope, content,
                    datetime.fromtimestamp(covers_from, tz=timezone.utc),
                    datetime.fromtimestamp(covers_to, tz=timezone.utc),
                    importance,
                ))
        return sid

    async def get_summaries(
        self, scope: str | None = None,
        session_id: str | None = None,
        limit: int = 50,
    ) -> list[dict]:
        conditions: list[str] = []
        params: list[Any] = []
        if scope:
            conditions.append("scope = %s")
            params.append(scope)
        if session_id:
            conditions.append("session_id = %s")
            params.append(session_id)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(limit)
        return await self.pool.execute(
            f"SELECT * FROM memory.summaries {where} "
            f"ORDER BY created_at DESC LIMIT %s",
            tuple(params),
        )

    async def rolling_summary_text(self, session_id: str) -> str:
        rows = await self.pool.execute("""
            SELECT content FROM memory.summaries
            WHERE scope IN ('daily', 'session')
            ORDER BY created_at DESC LIMIT 3
        """)
        return "\n\n---\n\n".join(r["content"] for r in rows)

    async def get_unenriched_events(self, limit: int = 100) -> list[dict]:
        return await self.pool.execute("""
            SELECT id, type, agent, summary, details, created_at
            FROM memory.events
            WHERE enriched_at IS NULL
            ORDER BY created_at ASC
            LIMIT %s
        """, (limit,))

    async def mark_enriched(
        self, event_id: int,
        importance: float, topic: str, tags: list[str],
    ) -> None:
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    UPDATE memory.events
                    SET importance = %s, topic = %s, tags = %s,
                        enriched_at = now()
                    WHERE id = %s
                """, (importance, topic, tags, event_id))
