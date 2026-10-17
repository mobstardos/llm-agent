"""Аналитические запросы и кэширование."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from src.db.pool import DatabasePool

logger = logging.getLogger(__name__)


class Analytics:
    """Обёртка над materialized views.

    Все методы возвращают уже готовые данные из MV.
    Обновление — через refresh() (фоновый таск или API).
    """

    def __init__(self, pool: DatabasePool):
        self.pool = pool

    # ═══════════════════════════════════════════════════════
    # Overview
    # ═══════════════════════════════════════════════════════
    async def overview(self) -> dict:
        """Общая сводка по системе."""
        rows = await self.pool.execute("""
            SELECT
                (SELECT COUNT(*) FROM memory.sessions) AS total_sessions,
                (SELECT COUNT(*) FROM memory.messages) AS total_messages,
                (SELECT COUNT(*) FROM memory.events) AS total_events,
                (SELECT COUNT(*) FROM memory.events
                 WHERE enriched_at IS NOT NULL) AS enriched_events,
                (SELECT COUNT(*) FROM memory.events
                 WHERE importance >= 0.7) AS important_events,
                (SELECT COUNT(*) FROM vectors.chunks) AS total_chunks,
                (SELECT COUNT(DISTINCT file) FROM vectors.chunks) AS total_files,
                (SELECT COUNT(*) FROM graph.nodes) AS total_nodes,
                (SELECT COUNT(*) FROM graph.edges) AS total_edges,
                (SELECT COUNT(*) FROM metrics.loop_runs) AS total_runs,
                (SELECT AVG(importance) FROM memory.events
                 WHERE importance IS NOT NULL) AS avg_importance,
                (SELECT COUNT(DISTINCT agent_id) FROM metrics.loop_runs)
                    AS active_agents,
                (SELECT COUNT(*) FROM memory.events
                 WHERE created_at > now() - interval '24 hours')
                    AS events_24h,
                (SELECT COUNT(*) FROM memory.events
                 WHERE created_at > now() - interval '7 days')
                    AS events_7d
        """)
        r = rows[0] if rows else {}

        # Округляем
        if r.get("avg_importance") is not None:
            r["avg_importance"] = round(float(r["avg_importance"]), 3)

        # DB size
        try:
            size_rows = await self.pool.execute(
                "SELECT pg_database_size(current_database()) AS size"
            )
            r["db_size_mb"] = round(
                (size_rows[0]["size"] or 0) / 1024 / 1024, 1,
            )
        except Exception:
            r["db_size_mb"] = 0

        return r

    # ═══════════════════════════════════════════════════════
    # Daily activity
    # ═══════════════════════════════════════════════════════
    async def daily_activity(self, days: int = 30) -> list[dict]:
        rows = await self.pool.execute("""
            SELECT
                day, events_count, sessions_count, errors_count,
                decisions_count, fixes_count, tool_calls_count,
                avg_importance, important_count, enriched_count
            FROM metrics.mv_daily_activity
            WHERE day > now() - interval '%s days'
            ORDER BY day ASC
        """, (days,))
        return [
            {
                **dict(r),
                "day": r["day"].isoformat() if r.get("day") else None,
                "avg_importance": round(float(r["avg_importance"]), 3)
                    if r.get("avg_importance") is not None else 0.0,
            }
            for r in rows
        ]

    # ═══════════════════════════════════════════════════════
    # Top files
    # ═══════════════════════════════════════════════════════
    async def top_files(self, limit: int = 20) -> list[dict]:
        rows = await self.pool.execute("""
            SELECT * FROM metrics.mv_top_files LIMIT %s
        """, (limit,))
        return [
            {
                **dict(r),
                "avg_importance": round(float(r["avg_importance"]), 3)
                    if r.get("avg_importance") is not None else 0.0,
                "last_updated": r["last_updated"].isoformat()
                    if r.get("last_updated") else None,
                "first_seen": r["first_seen"].isoformat()
                    if r.get("first_seen") else None,
            }
            for r in rows
        ]

    # ═══════════════════════════════════════════════════════
    # Agent performance
    # ═══════════════════════════════════════════════════════
    async def agent_performance(self) -> list[dict]:
        rows = await self.pool.execute("""
            SELECT * FROM metrics.mv_agent_performance
        """)
        return [
            {
                **dict(r),
                "success_rate": float(r["success_rate"] or 0),
                "last_run": r["last_run"].isoformat()
                    if r.get("last_run") else None,
            }
            for r in rows
        ]

    # ═══════════════════════════════════════════════════════
    # Topics
    # ═══════════════════════════════════════════════════════
    async def topics(self, limit: int = 20) -> list[dict]:
        rows = await self.pool.execute("""
            SELECT * FROM metrics.mv_topics LIMIT %s
        """, (limit,))
        return [
            {
                **dict(r),
                "avg_importance": round(float(r["avg_importance"]), 3)
                    if r.get("avg_importance") is not None else 0.0,
                "last_seen": r["last_seen"].isoformat()
                    if r.get("last_seen") else None,
                "sample_tags": (r.get("sample_tags") or [])[:5],
            }
            for r in rows
        ]

    # ═══════════════════════════════════════════════════════
    # Tool usage
    # ═══════════════════════════════════════════════════════
    async def tool_usage(self, limit: int = 20) -> list[dict]:
        rows = await self.pool.execute("""
            SELECT * FROM metrics.mv_tool_usage LIMIT %s
        """, (limit,))
        return [
            {
                **dict(r),
                "success_rate": float(r["success_rate"] or 0),
                "last_call": r["last_call"].isoformat()
                    if r.get("last_call") else None,
            }
            for r in rows
        ]

    # ═══════════════════════════════════════════════════════
    # Enrichment stats
    # ═══════════════════════════════════════════════════════
    async def enrichment_stats(self, days: int = 30) -> list[dict]:
        rows = await self.pool.execute("""
            SELECT * FROM metrics.mv_enrichment_stats
            WHERE day > now() - interval '%s days'
            ORDER BY day ASC
        """, (days,))
        return [
            {
                **dict(r),
                "day": r["day"].isoformat() if r.get("day") else None,
                "enrichment_rate": float(r["enrichment_rate"] or 0),
                "avg_importance": round(float(r["avg_importance"]), 3)
                    if r.get("avg_importance") is not None else 0.0,
            }
            for r in rows
        ]

    # ═══════════════════════════════════════════════════════
    # Time-series для графиков
    # ═══════════════════════════════════════════════════════
    async def hourly_activity(self, hours: int = 48) -> list[dict]:
        """Почасовая активность за N часов."""
        rows = await self.pool.execute("""
            SELECT
                date_trunc('hour', created_at) AS hour,
                COUNT(*) AS events,
                COUNT(*) FILTER (WHERE type = 'error') AS errors,
                COUNT(*) FILTER (WHERE type = 'tool_call') AS tool_calls
            FROM memory.events
            WHERE created_at > now() - interval '%s hours'
            GROUP BY 1
            ORDER BY 1 ASC
        """, (hours,))
        return [
            {
                **dict(r),
                "hour": r["hour"].isoformat() if r.get("hour") else None,
            }
            for r in rows
        ]

    # ═══════════════════════════════════════════════════════
    # Refresh
    # ═══════════════════════════════════════════════════════
    async def refresh(self) -> dict:
        """Обновить все MV CONCURRENTLY."""
        try:
            async with self.pool.connection() as conn:
                await conn.set_autocommit(True)
                async with conn.cursor() as cur:
                    await cur.execute("SELECT metrics.refresh_all()")
            logger.info("Materialized views refreshed")
            return {"ok": True, "refreshed_at": datetime.now(timezone.utc).isoformat()}
        except Exception as e:
            logger.warning("MV refresh failed: %s", e)
            return {"ok": False, "error": str(e)}

    async def refresh_one(self, view_name: str) -> dict:
        """Обновить одну MV. view_name — без схемы (mv_daily_activity)."""
        # Whitelist для защиты
        allowed = {
            "mv_daily_activity",
            "mv_top_files",
            "mv_agent_performance",
            "mv_topics",
            "mv_tool_usage",
            "mv_enrichment_stats",
        }
        if view_name not in allowed:
            return {"ok": False, "error": "not allowed"}

        try:
            async with self.pool.connection() as conn:
                await conn.set_autocommit(True)
                async with conn.cursor() as cur:
                    await cur.execute(
                        f"REFRESH MATERIALIZED VIEW CONCURRENTLY metrics.{view_name}"
                    )
            return {"ok": True, "view": view_name}
        except Exception as e:
            logger.warning("MV refresh %s failed: %s", view_name, e)
            return {"ok": False, "error": str(e)}

    async def mv_ages(self) -> dict:
        """Когда каждая MV последний раз обновлялась."""
        rows = await self.pool.execute("""
            SELECT
                schemaname || '.' || matviewname AS name,
                pg_relation_size(schemaname || '.' || matviewname) AS size_bytes
            FROM pg_matviews
            WHERE schemaname = 'metrics'
        """)
        return {
            r["name"]: {
                "size_kb": round((r["size_bytes"] or 0) / 1024, 1),
            }
            for r in rows
        }
