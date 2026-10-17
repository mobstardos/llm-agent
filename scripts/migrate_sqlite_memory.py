"""Миграция episodic memory из SQLite в PostgreSQL.

Запуск:
    python scripts/migrate_sqlite_memory.py

Опции:
    --dry-run    только посчитать
    --truncate   очистить целевые таблицы перед миграцией
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def _to_ts(value) -> datetime | None:
    """Конвертирует unix timestamp → datetime."""
    if value is None:
        return None
    try:
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    except Exception:
        return None


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--truncate", action="store_true")
    args = parser.parse_args()

    sqlite_path = BASE_DIR / "data" / "memory.sqlite"
    if not sqlite_path.exists():
        logger.error("SQLite не найден: %s", sqlite_path)
        return 1

    logger.info("SQLite: %s", sqlite_path)
    conn = sqlite3.connect(sqlite_path)
    conn.row_factory = sqlite3.Row

    # ─── Считаем, что переносить ─────────────────────────
    def count(table: str) -> int:
        try:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        except Exception:
            return 0

    counts = {
        "sessions": count("sessions"),
        "messages": count("messages"),
        "events": count("events"),
        "summaries": count("summaries"),
    }
    logger.info("SQLite содержит:")
    for k, v in counts.items():
        logger.info("  %-12s %d", k, v)

    if args.dry_run:
        logger.info("DRY-RUN: миграция не выполнена")
        conn.close()
        return 0

    from src.db.pool import get_pool

    pool = await get_pool()

    # ─── Truncate ────────────────────────────────────────
    if args.truncate:
        logger.info("Очищаю целевые таблицы...")
        async with pool.connection() as c:
            async with c.cursor() as cur:
                await cur.execute(
                    "TRUNCATE memory.summaries, memory.events, "
                    "memory.messages, memory.sessions CASCADE"
                )

    # ─── Sessions ────────────────────────────────────────
    logger.info("Миграция sessions...")
    sessions = conn.execute("SELECT * FROM sessions").fetchall()
    sessions_ok = 0
    for s in sessions:
        try:
            started = _to_ts(s["started_at"]) or datetime.now(timezone.utc)
            ended = _to_ts(s["ended_at"]) if s["ended_at"] else None

            async with pool.connection() as c:
                async with c.cursor() as cur:
                    await cur.execute("""
                        INSERT INTO memory.sessions
                            (id, title, summary, started_at, ended_at,
                             message_count, tokens_used)
                        VALUES (%s::uuid, %s, %s, %s, %s, %s, %s)
                        ON CONFLICT (id) DO NOTHING
                    """, (
                        s["id"], s["title"], s["summary"],
                        started, ended,
                        s["message_count"] or 0,
                        s["tokens_used"] or 0,
                    ))
            sessions_ok += 1
        except Exception as e:
            logger.debug("Session %s: %s", s["id"], e)
    logger.info("  Sessions: %d/%d", sessions_ok, len(sessions))

    # ─── Messages ────────────────────────────────────────
    logger.info("Миграция messages...")
    messages = conn.execute(
        "SELECT * FROM messages ORDER BY ts"
    ).fetchall()
    messages_ok = 0
    for i, m in enumerate(messages):
        try:
            ts = _to_ts(m["ts"]) or datetime.now(timezone.utc)
            async with pool.connection() as c:
                async with c.cursor() as cur:
                    await cur.execute("""
                        INSERT INTO memory.messages
                            (session_id, created_at, role, content, tokens,
                             tool_calls, tool_call_id)
                        VALUES (%s::uuid, %s, %s, %s, %s, %s::jsonb, %s)
                    """, (
                        m["session_id"], ts, m["role"], m["content"],
                        m["tokens"] or 0,
                        m["tool_calls_json"], m["tool_call_id"],
                    ))
            messages_ok += 1
        except Exception as e:
            logger.debug("Message %s: %s", m["id"], e)

        if (i + 1) % 1000 == 0:
            logger.info("  Messages: %d/%d", i + 1, len(messages))
    logger.info("  Messages: %d/%d", messages_ok, len(messages))

    # ─── Events ──────────────────────────────────────────
    logger.info("Миграция events...")
    events = conn.execute("SELECT * FROM events ORDER BY ts").fetchall()
    events_ok = 0
    for i, e in enumerate(events):
        try:
            ts = _to_ts(e["ts"]) or datetime.now(timezone.utc)
            async with pool.connection() as c:
                async with c.cursor() as cur:
                    await cur.execute("""
                        INSERT INTO memory.events
                            (session_id, trace_id, created_at, type, agent,
                             summary, details, success)
                        VALUES (%s::uuid, %s, %s, %s, %s, %s, %s::jsonb, %s)
                    """, (
                        e["session_id"], e["trace_id"], ts, e["type"],
                        e["agent"], e["summary"],
                        e["details_json"], bool(e["success"]),
                    ))
            events_ok += 1
        except Exception as ex:
            logger.debug("Event %s: %s", e["id"], ex)

        if (i + 1) % 1000 == 0:
            logger.info("  Events: %d/%d", i + 1, len(events))
    logger.info("  Events: %d/%d", events_ok, len(events))

    # ─── Summaries ───────────────────────────────────────
    logger.info("Миграция summaries...")
    summaries = conn.execute(
        "SELECT * FROM summaries ORDER BY created_at"
    ).fetchall()
    summaries_ok = 0
    for s in summaries:
        try:
            covers_from = _to_ts(s["covers_from"]) or datetime.now(timezone.utc)
            covers_to = _to_ts(s["covers_to"]) or datetime.now(timezone.utc)
            created = _to_ts(s["created_at"]) or datetime.now(timezone.utc)
            async with pool.connection() as c:
                async with c.cursor() as cur:
                    await cur.execute("""
                        INSERT INTO memory.summaries
                            (session_id, scope, content,
                             covers_from, covers_to, created_at)
                        VALUES (%s::uuid, %s, %s, %s, %s, %s)
                    """, (
                        s["session_id"], s["scope"], s["content"],
                        covers_from, covers_to, created,
                    ))
            summaries_ok += 1
        except Exception as e:
            logger.debug("Summary %s: %s", s["id"], e)
    logger.info("  Summaries: %d/%d", summaries_ok, len(summaries))

    conn.close()

    # ─── Итог ────────────────────────────────────────────
    logger.info("═" * 60)
    logger.info("✓ Миграция завершена")
    logger.info("  Sessions:  %d/%d", sessions_ok, len(sessions))
    logger.info("  Messages:  %d/%d", messages_ok, len(messages))
    logger.info("  Events:    %d/%d", events_ok, len(events))
    logger.info("  Summaries: %d/%d", summaries_ok, len(summaries))

    # Проверка
    rows = await pool.execute("""
        SELECT
            (SELECT COUNT(*) FROM memory.sessions) AS s,
            (SELECT COUNT(*) FROM memory.messages) AS m,
            (SELECT COUNT(*) FROM memory.events) AS e,
            (SELECT COUNT(*) FROM memory.summaries) AS sm
    """)
    if rows:
        r = rows[0]
        logger.info(
            "  PG check: sessions=%d messages=%d events=%d summaries=%d",
            r["s"], r["m"], r["e"], r["sm"],
        )
    logger.info("═" * 60)

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
