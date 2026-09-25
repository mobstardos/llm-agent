"""PostgreSQL метрики в Prometheus-формате.

Собирает:
- Размер БД, размеры схем
- Соединения
- Cache hit ratio
- Row counts ключевых таблиц
- MV размеры
- Lock waits
- Long-running queries
"""
from __future__ import annotations

import logging

from src.db.pool import DatabasePool

logger = logging.getLogger(__name__)


class PGMetrics:
    """Prometheus exporter для PostgreSQL."""

    def __init__(self, pool: DatabasePool):
        self.pool = pool

    async def render(self) -> str:
        """Возвращает текст в формате Prometheus exposition."""
        lines: list[str] = []

        try:
            await self._render_size(lines)
            await self._render_connections(lines)
            await self._render_cache(lines)
            await self._render_row_counts(lines)
            await self._render_mv_sizes(lines)
            await self._render_table_sizes(lines)
            await self._render_locks(lines)
            await self._render_long_running(lines)
        except Exception as e:
            logger.debug("PG metrics error: %s", e)

        return "\n".join(lines) + "\n"

    # ═══════════════════════════════════════════════════════
    # Size
    # ═══════════════════════════════════════════════════════
    async def _render_size(self, lines: list[str]) -> None:
        rows = await self.pool.execute("""
            SELECT pg_database_size(current_database()) AS size
        """)
        size = rows[0]["size"] if rows else 0
        lines.append("# HELP llmagent_pg_database_size_bytes Размер БД")
        lines.append("# TYPE llmagent_pg_database_size_bytes gauge")
        lines.append(f"llmagent_pg_database_size_bytes {size}")

        # По схемам
        rows = await self.pool.execute("""
            SELECT
                schemaname,
                SUM(pg_total_relation_size(schemaname || '.' || tablename))
                    AS size
            FROM pg_tables
            WHERE schemaname IN ('memory', 'vectors', 'graph', 'cache',
                                 'policies', 'audit', 'metrics', 'meta')
            GROUP BY schemaname
        """)
        lines.append("# HELP llmagent_pg_schema_size_bytes Размер схемы")
        lines.append("# TYPE llmagent_pg_schema_size_bytes gauge")
        for r in rows:
            lines.append(
                f'llmagent_pg_schema_size_bytes{{schema="{r["schemaname"]}"}} '
                f'{r["size"] or 0}'
            )

    # ═══════════════════════════════════════════════════════
    # Connections
    # ═══════════════════════════════════════════════════════
    async def _render_connections(self, lines: list[str]) -> None:
        rows = await self.pool.execute("""
            SELECT
                count(*) FILTER (WHERE state = 'active') AS active,
                count(*) FILTER (WHERE state = 'idle') AS idle,
                count(*) FILTER (WHERE state = 'idle in transaction')
                    AS idle_in_tx,
                count(*) AS total
            FROM pg_stat_activity
            WHERE datname = current_database()
        """)
        r = rows[0] if rows else {}

        lines.append("# HELP llmagent_pg_connections Соединения")
        lines.append("# TYPE llmagent_pg_connections gauge")
        lines.append(
            f'llmagent_pg_connections{{state="active"}} {r.get("active", 0)}'
        )
        lines.append(
            f'llmagent_pg_connections{{state="idle"}} {r.get("idle", 0)}'
        )
        lines.append(
            f'llmagent_pg_connections{{state="idle_in_transaction"}} '
            f'{r.get("idle_in_tx", 0)}'
        )
        lines.append(
            f'llmagent_pg_connections{{state="total"}} {r.get("total", 0)}'
        )

    # ═══════════════════════════════════════════════════════
    # Cache hit ratio
    # ═══════════════════════════════════════════════════════
    async def _render_cache(self, lines: list[str]) -> None:
        rows = await self.pool.execute("""
            SELECT
                CASE WHEN blks_hit + blks_read > 0
                    THEN 100.0 * blks_hit / (blks_hit + blks_read)
                    ELSE 100.0
                END AS ratio,
                blks_hit, blks_read
            FROM pg_stat_database
            WHERE datname = current_database()
        """)
        r = rows[0] if rows else {}
        ratio = round(float(r.get("ratio", 0)), 2)

        lines.append("# HELP llmagent_pg_cache_hit_ratio Cache hit ratio %")
        lines.append("# TYPE llmagent_pg_cache_hit_ratio gauge")
        lines.append(f"llmagent_pg_cache_hit_ratio {ratio}")

        lines.append("# HELP llmagent_pg_blocks_read Всего блоков прочитано")
        lines.append("# TYPE llmagent_pg_blocks_read counter")
        lines.append(f"llmagent_pg_blocks_read {r.get('blks_read', 0)}")

        lines.append("# HELP llmagent_pg_blocks_hit Всего блоков из кэша")
        lines.append("# TYPE llmagent_pg_blocks_hit counter")
        lines.append(f"llmagent_pg_blocks_hit {r.get('blks_hit', 0)}")

    # ═══════════════════════════════════════════════════════
    # Row counts
    # ═══════════════════════════════════════════════════════
    async def _render_row_counts(self, lines: list[str]) -> None:
        lines.append("# HELP llmagent_pg_table_rows Количество строк")
        lines.append("# TYPE llmagent_pg_table_rows gauge")

        tables = [
            ("memory", "sessions"),
            ("memory", "messages"),
            ("memory", "events"),
            ("memory", "summaries"),
            ("vectors", "chunks"),
            ("graph", "nodes"),
            ("graph", "edges"),
            ("metrics", "loop_runs"),
            ("cache", "llm_responses"),
            ("policies", "approval"),
        ]

        for schema, table in tables:
            try:
                rows = await self.pool.execute(
                    f"SELECT COUNT(*) AS c FROM {schema}.{table}"
                )
                count = rows[0]["c"] if rows else 0
                lines.append(
                    f'llmagent_pg_table_rows{{schema="{schema}",'
                    f'table="{table}"}} {count}'
                )
            except Exception as e:
                logger.debug("Row count %s.%s: %s", schema, table, e)

    # ═══════════════════════════════════════════════════════
    # Materialized views
    # ═══════════════════════════════════════════════════════
    async def _render_mv_sizes(self, lines: list[str]) -> None:
        rows = await self.pool.execute("""
            SELECT
                matviewname,
                pg_relation_size(schemaname || '.' || matviewname) AS size
            FROM pg_matviews
            WHERE schemaname = 'metrics'
        """)
        lines.append("# HELP llmagent_pg_mv_size_bytes Размер MV")
        lines.append("# TYPE llmagent_pg_mv_size_bytes gauge")
        for r in rows:
            lines.append(
                f'llmagent_pg_mv_size_bytes{{view="{r["matviewname"]}"}} '
                f'{r["size"] or 0}'
            )

    # ═══════════════════════════════════════════════════════
    # Top tables by size
    # ═══════════════════════════════════════════════════════
    async def _render_table_sizes(self, lines: list[str]) -> None:
        rows = await self.pool.execute("""
            SELECT
                schemaname,
                tablename,
                pg_total_relation_size(schemaname || '.' || tablename) AS size
            FROM pg_tables
            WHERE schemaname IN ('memory', 'vectors', 'graph', 'cache',
                                 'policies', 'audit', 'metrics', 'meta')
            ORDER BY size DESC
            LIMIT 20
        """)
        lines.append("# HELP llmagent_pg_table_size_bytes Размер таблицы")
        lines.append("# TYPE llmagent_pg_table_size_bytes gauge")
        for r in rows:
            lines.append(
                f'llmagent_pg_table_size_bytes{{schema="{r["schemaname"]}",'
                f'table="{r["tablename"]}"}} {r["size"] or 0}'
            )

    # ═══════════════════════════════════════════════════════
    # Locks
    # ═══════════════════════════════════════════════════════
    async def _render_locks(self, lines: list[str]) -> None:
        try:
            rows = await self.pool.execute("""
                SELECT
                    mode,
                    granted,
                    COUNT(*) AS count
                FROM pg_locks l
                JOIN pg_database d ON d.oid = l.database
                WHERE d.datname = current_database()
                GROUP BY mode, granted
            """)
            lines.append("# HELP llmagent_pg_locks Количество блокировок")
            lines.append("# TYPE llmagent_pg_locks gauge")
            for r in rows:
                granted = "granted" if r["granted"] else "waiting"
                lines.append(
                    f'llmagent_pg_locks{{mode="{r["mode"]}",'
                    f'status="{granted}"}} {r["count"]}'
                )
        except Exception as e:
            logger.debug("Locks: %s", e)

    # ═══════════════════════════════════════════════════════
    # Long-running queries
    # ═══════════════════════════════════════════════════════
    async def _render_long_running(self, lines: list[str]) -> None:
        try:
            rows = await self.pool.execute("""
                SELECT COUNT(*) AS c
                FROM pg_stat_activity
                WHERE datname = current_database()
                  AND state = 'active'
                  AND now() - query_start > interval '5 seconds'
                  AND query NOT LIKE '%pg_stat_activity%'
            """)
            count = rows[0]["c"] if rows else 0
            lines.append(
                "# HELP llmagent_pg_long_running_queries Запросы > 5 сек"
            )
            lines.append("# TYPE llmagent_pg_long_running_queries gauge")
            lines.append(f"llmagent_pg_long_running_queries {count}")
        except Exception as e:
            logger.debug("Long running: %s", e)
