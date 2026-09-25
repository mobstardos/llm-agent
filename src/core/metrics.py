"""Prometheus-совместимый экспорт метрик."""
from __future__ import annotations

import logging
import time

from src.core.schema import AgentStatus

logger = logging.getLogger(__name__)


class PrometheusExporter:
    def __init__(self, registry, cache=None, memory=None, history=None,
                 loop_metrics=None):
        self.registry = registry
        self.cache = cache
        self.memory = memory
        self.history = history
        self.loop_metrics = loop_metrics
        self._start_time = time.time()

    def render(self) -> str:
        lines: list[str] = []

        # ─── Process ────────────────────────────────────
        lines.append("# HELP llmagent_uptime_seconds Uptime")
        lines.append("# TYPE llmagent_uptime_seconds gauge")
        lines.append(f"llmagent_uptime_seconds {time.time() - self._start_time:.1f}")

        # ─── Registry snapshot ──────────────────────────
        snap = self.registry.snapshot if self.registry else None
        if snap:
            lines.append("# HELP llmagent_snapshot_build_duration_ms Snapshot build")
            lines.append("# TYPE llmagent_snapshot_build_duration_ms gauge")
            lines.append(
                f"llmagent_snapshot_build_duration_ms "
                f"{snap.build_duration_ms:.1f}"
            )

            # Agents
            status_map = {
                AgentStatus.ACTIVE.value: 3,
                AgentStatus.DEGRADED.value: 2,
                AgentStatus.UNAVAILABLE.value: 1,
                AgentStatus.DISABLED.value: 0,
                AgentStatus.FAILED.value: -1,
            }
            lines.append("# HELP llmagent_agent_status Статус агента")
            lines.append("# TYPE llmagent_agent_status gauge")
            for aid, st in snap.agents.items():
                sv = status_map.get(st.status.value, 0)
                lines.append(f'llmagent_agent_status{{agent="{aid}"}} {sv}')

            lines.append("# HELP llmagent_agent_params Эффективные параметры")
            lines.append("# TYPE llmagent_agent_params gauge")
            for aid, st in snap.agents.items():
                for k, v in st.effective_params.items():
                    if isinstance(v, (int, float)) and not isinstance(v, bool):
                        lines.append(
                            f'llmagent_agent_params{{agent="{aid}",param="{k}"}} {v}'
                        )

            # MCP
            lines.append("# HELP llmagent_mcp_alive MCP запущен")
            lines.append("# TYPE llmagent_mcp_alive gauge")
            for mid, st in snap.mcp_servers.items():
                lines.append(
                    f'llmagent_mcp_alive{{mcp="{mid}"}} {1 if st.alive else 0}'
                )

            lines.append("# HELP llmagent_mcp_enabled MCP включён")
            lines.append("# TYPE llmagent_mcp_enabled gauge")
            for mid, st in snap.mcp_servers.items():
                lines.append(
                    f'llmagent_mcp_enabled{{mcp="{mid}"}} '
                    f'{1 if st.enabled else 0}'
                )

            lines.append("# HELP llmagent_mcp_restarts_total Рестарты MCP")
            lines.append("# TYPE llmagent_mcp_restarts_total counter")
            for mid, st in snap.mcp_servers.items():
                lines.append(
                    f'llmagent_mcp_restarts_total{{mcp="{mid}"}} {st.restarts}'
                )

            # Capabilities
            lines.append("# HELP llmagent_capability_has_provider")
            lines.append("# TYPE llmagent_capability_has_provider gauge")
            for cid, r in snap.resolved_capabilities.items():
                v = 1 if r.get("primary") else 0
                lines.append(
                    f'llmagent_capability_has_provider{{capability="{cid}"}} {v}'
                )

        # ─── Cache ──────────────────────────────────────
        if self.cache:
            try:
                s = self.cache.stats()
                lines.append("# HELP llmagent_cache_hits_total")
                lines.append("# TYPE llmagent_cache_hits_total counter")
                lines.append(f"llmagent_cache_hits_total {s.get('hits', 0)}")
                lines.append("# HELP llmagent_cache_misses_total")
                lines.append("# TYPE llmagent_cache_misses_total counter")
                lines.append(f"llmagent_cache_misses_total {s.get('misses', 0)}")
                lines.append("# HELP llmagent_cache_records")
                lines.append("# TYPE llmagent_cache_records gauge")
                lines.append(f"llmagent_cache_records {s.get('total', 0)}")
            except Exception:
                pass

        # ─── Memory ─────────────────────────────────────
        if self.memory and getattr(self.memory, "enabled", False):
            try:
                ms = self.memory.stats()
                if isinstance(ms.get("vector"), dict):
                    lines.append("# HELP llmagent_vector_chunks")
                    lines.append("# TYPE llmagent_vector_chunks gauge")
                    lines.append(
                        f"llmagent_vector_chunks {ms['vector'].get('chunks', 0)}"
                    )
                if isinstance(ms.get("graph"), dict):
                    lines.append("# HELP llmagent_graph_nodes")
                    lines.append("# TYPE llmagent_graph_nodes gauge")
                    lines.append(
                        f"llmagent_graph_nodes {ms['graph'].get('nodes', 0)}"
                    )
                    lines.append("# HELP llmagent_graph_edges")
                    lines.append("# TYPE llmagent_graph_edges gauge")
                    lines.append(
                        f"llmagent_graph_edges {ms['graph'].get('edges', 0)}"
                    )
            except Exception:
                pass

        # ─── History ────────────────────────────────────
        if self.history:
            try:
                snaps = self.history.list(limit=100)
                lines.append("# HELP llmagent_snapshots_total")
                lines.append("# TYPE llmagent_snapshots_total gauge")
                lines.append(f"llmagent_snapshots_total {len(snaps)}")
                if snaps:
                    age = time.time() - snaps[0].built_at
                    lines.append("# HELP llmagent_last_snapshot_age_seconds")
                    lines.append("# TYPE llmagent_last_snapshot_age_seconds gauge")
                    lines.append(f"llmagent_last_snapshot_age_seconds {age:.1f}")
            except Exception:
                pass

        # ─── Loop ───────────────────────────────────────
        if self.loop_metrics:
            try:
                for line in self.loop_metrics.render():
                    lines.append(line)
            except Exception:
                pass

        return "\n".join(lines) + "\n"
