"""Граф зависимостей действий.

Три вида связей (объединяются в один DAG):
  1. Явные:      event.depends_on → другие события
  2. Цепочка:    event.parent_id → предыдущее событие задачи (автоматически)
  3. По целям:   события, трогавшие один файл: write(A, x.py) зависит от
                 предыдущего write/read(x.py) — «почему файл стал таким»

Возможности:
  * топологический порядок (для replay/rollback)
  * цепочка изменений конкретного файла (провенанс)
  * подграф задачи/сессии для UI
  * критический путь и «хвосты» (события без потомков)
"""
from __future__ import annotations

import json
from collections import defaultdict, deque
from typing import Any

from .schema import Event
from .store import JournalStore


class DependencyGraph:
    def __init__(self, store: JournalStore):
        self.store = store

    # ─── Построение подграфа ─────────────────────────────────
    def build(
        self,
        session_id: str = "",
        task_id: str = "",
        trace_id: str = "",
        since: float | None = None,
        until: float | None = None,
        limit: int = 2000,
    ) -> dict[str, Event]:
        events = self.store.query(
            session_id=session_id or None,
            task_id=task_id or None,
            trace_id=trace_id or None,
            since=since, until=until,
            limit=limit, order_desc=False,
        )
        return {e.event_id: e for e in events}

    # ─── Рёбра ───────────────────────────────────────────────
    @staticmethod
    def edges(events: dict[str, Event], by_target: bool = True) -> list[dict]:
        """Список рёбер {from, to, type}."""
        edges: list[dict] = []
        seen: set[tuple[str, str, str]] = set()

        def add(a: str, b: str, t: str) -> None:
            if a and b and a != b and (a, b, t) not in seen:
                seen.add((a, b, t))
                edges.append({"from": a, "to": b, "type": t})

        # 1. Явные + parent
        for ev in events.values():
            for dep in ev.depends_on or []:
                if dep in events:
                    add(dep, ev.event_id, "explicit")
            if ev.parent_id in events:
                add(ev.parent_id, ev.event_id, "sequence")

        # 2. По целям (файлы/таблицы): предыдущая правка → следующая правка
        if by_target:
            last_by_target: dict[str, str] = {}
            for ev in sorted(events.values(), key=lambda x: x.seq):
                for t in ev.targets:
                    prev = last_by_target.get(t)
                    if prev:
                        add(prev, ev.event_id, f"target:{t}")
                    if ev.reversible > 0 or ev.kind == "tool_call":
                        last_by_target[t] = ev.event_id
        return edges

    # ─── Топологический порядок ──────────────────────────────
    @staticmethod
    def topo_order(events: dict[str, Event],
                   edges: list[dict] | None = None) -> list[str]:
        """Кан-карр-топосорт; при цикле — довеска по seq (устойчивость)."""
        if edges is None:
            edges = DependencyGraph.edges(events)
        indeg: dict[str, int] = {eid: 0 for eid in events}
        adj: dict[str, list[str]] = defaultdict(list)
        for e in edges:
            f, t = e["from"], e["to"]
            if f in indeg and t in indeg:
                adj[f].append(t)
                indeg[t] += 1
        q = deque(sorted(
            [eid for eid, d in indeg.items() if d == 0],
            key=lambda eid: events[eid].seq,
        ))
        order: list[str] = []
        while q:
            eid = q.popleft()
            order.append(eid)
            for nxt in adj.get(eid, []):
                indeg[nxt] -= 1
                if indeg[nxt] == 0:
                    q.append(nxt)
        if len(order) < len(events):  # цикл (не должен случаться) — добираем по seq
            rest = sorted(
                [eid for eid in events if eid not in set(order)],
                key=lambda eid: events[eid].seq,
            )
            order.extend(rest)
        return order

    # ─── Провенанс файла ─────────────────────────────────────
    def provenance(self, target: str, limit: int = 100) -> dict:
        """Полная история изменений конкретного файла/таблицы + цепочка причин."""
        events = self.store.query(target_like=target, limit=limit, order_desc=False)
        chain = []
        for ev in events:
            chain.append({
                "event_id": ev.event_id,
                "seq": ev.seq,
                "time": ev.iso_time,
                "agent": ev.agent_id,
                "task_id": ev.task_id,
                "server": ev.server_name,
                "tool": ev.tool_name,
                "action": ev.action,
                "status": ev.status,
                "before_hash": ev.before_hash[:12],
                "after_hash": ev.after_hash[:12],
                "shadow_dir": ev.shadow_dir,
                "reversible": ev.reversible,
                "error": ev.error[:200] if ev.error else "",
            })
        return {"target": target, "count": len(chain), "history": chain}

    # ─── Экспорт графа для UI ────────────────────────────────
    def to_vis(self, session_id: str = "", task_id: str = "",
               limit: int = 300) -> dict:
        """Узлы+рёбра для визуализации (UI, mermaid)."""
        events = self.build(session_id=session_id, task_id=task_id, limit=limit)
        edges = self.edges(events)
        nodes = []
        for ev in sorted(events.values(), key=lambda x: x.seq):
            label = ev.action or ev.kind
            if ev.tool_name:
                label = f"{ev.server_name}.{ev.tool_name}"
            nodes.append({
                "id": ev.event_id,
                "seq": ev.seq,
                "label": label[:60],
                "kind": ev.kind,
                "status": ev.status,
                "agent": ev.agent_id,
                "time": ev.iso_time,
                "reversible": ev.reversible,
                "task_id": ev.task_id,
            })
        return {"nodes": nodes, "edges": edges,
                "topo_order": self.topo_order(events, edges)}

    # ─── Mermaid (для отчётов — AI читает текст) ─────────────
    @staticmethod
    def to_mermaid(events: dict[str, Event], edges: list[dict],
                   max_nodes: int = 40) -> str:
        ids = sorted(events.values(), key=lambda x: x.seq)[:max_nodes]
        if not ids:
            return "(пусто)"
        idmap = {ev.event_id: f"E{ev.seq}" for ev in ids}
        lines = ["graph TD"]
        for ev in ids:
            label = (f"{ev.tool_name or ev.action or ev.kind}"
                     f"\\n{ev.iso_time[-8:]} {ev.agent_id}").replace('"', "'")
            style = "" if ev.status == "ok" else f':::"{ev.status}"'
            lines.append(f'    {idmap[ev.event_id]}["{label}"]{style}')
        for e in edges:
            f, t = idmap.get(e["from"]), idmap.get(e["to"])
            if f and t:
                arrow = "-->" if e["type"] in ("sequence", "explicit") else "-.->"
                lines.append(f"    {f} {arrow} {t}")
        return "\n".join(lines)


def graph_summary(events: dict[str, Event], edges: list[dict]) -> dict:
    """Краткая статистика подграфа."""
    kinds: dict[str, int] = defaultdict(int)
    agents: dict[str, int] = defaultdict(int)
    for ev in events.values():
        kinds[ev.kind] += 1
        agents[ev.agent_id or "—"] += 1
    return {
        "nodes": len(events),
        "edges": len(edges),
        "by_kind": dict(kinds),
        "by_agent": dict(agents),
        "mutating": sum(1 for e in events.values() if e.reversible > 0),
        "failed": sum(1 for e in events.values()
                      if e.status in ("error", "conflict")),
    }


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)
