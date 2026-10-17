"""HTTP API журнала: /api/journal/*.

Подключается одной строкой в src/main.py:
    app.include_router(build_router())

Все эндпоинты отдают JSON; агенты и внешние инструменты могут
использовать API напрямую. Для LLM предназначены /timeline (текст)
и /reports (markdown-файлы).
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse

from .dependencies import DependencyGraph, graph_summary
from .integration import get_recorder
from .replay import ReplayEngine
from .rollback import RollbackEngine
from .utils import human_size


def build_router() -> APIRouter:
    router = APIRouter(prefix="/api/journal", tags=["journal"])

    def _rec():
        rec = get_recorder()
        if rec is None:
            raise HTTPException(503, "Journal не инициализирован")
        return rec

    # ─── События ─────────────────────────────────────────────
    @router.get("/events")
    async def events(
        kind: str = "", session_id: str = "", task_id: str = "",
        trace_id: str = "", agent_id: str = "", server: str = "",
        tool: str = "", status: str = "", target: str = "",
        since: float = 0, until: float = 0,
        limit: int = 200, offset: int = 0,
        mutating: bool = False, order_desc: bool = True,
        with_args: bool = False,
    ):
        rec = _rec()
        evs = rec.store.query(
            kind=kind or None, session_id=session_id or None,
            task_id=task_id or None, trace_id=trace_id or None,
            agent_id=agent_id or None, server_name=server or None,
            tool_name=tool or None, status=status or None,
            target_like=target or None,
            since=since or None, until=until or None,
            limit=limit, offset=offset, order_desc=order_desc,
            only_mutating=mutating,
        )
        data = []
        for e in evs:
            d = e.to_json(include_args=with_args)
            if not with_args:
                d.pop("args", None)
                d.pop("result", None)
            data.append(d)
        return {"count": len(data), "events": data}

    @router.get("/event/{event_id}")
    async def event_detail(event_id: str, with_args: bool = True):
        rec = _rec()
        ev = rec.store.get(event_id, with_payload=with_args)
        if not ev:
            raise HTTPException(404, "Событие не найдено")
        d = ev.to_json(include_args=True)
        # diff для файловых событий
        if ev.shadow_dir:
            manifest = rec.shadows.read_manifest(ev.shadow_dir, "before")
            d["shadow_files"] = (manifest or {}).get("files", {})
        return d

    @router.get("/event/{event_id}/diff")
    async def event_diff(event_id: str):
        """Unified diff содержимого файла до/после (текстовые файлы)."""
        rec = _rec()
        ev = rec.store.get(event_id)
        if not ev:
            raise HTTPException(404, "Событие не найдено")
        if not ev.shadow_dir:
            raise HTTPException(404, "Событие без теней (не файловая операция)")
        from .utils import read_text_safe
        import difflib
        manifest = rec.shadows.read_manifest(ev.shadow_dir, "before") or {}
        diffs = []
        for norm, info in manifest.get("files", {}).items():
            before_p = rec.cfg.shadows_dir / ev.shadow_dir / "before" / norm
            after_p = rec.cfg.shadows_dir / ev.shadow_dir / "after" / norm
            before = read_text_safe(before_p) if before_p.is_file() else ""
            after = read_text_safe(after_p) if after_p.is_file() else ""
            existed_before = info.get("existed", False)
            existed_after = info.get("existed_after", False)
            if not existed_before and existed_after:
                header = f"новый файл: {norm}"
            elif existed_before and not existed_after:
                header = f"удалён: {norm}"
            else:
                header = f"изменён: {norm}"
            diff = "\n".join(difflib.unified_diff(
                (before or "").splitlines(),
                (after or "").splitlines(),
                fromfile=f"before/{norm}", tofile=f"after/{norm}",
                lineterm="", n=3,
            ))
            diffs.append({"target": norm, "state": header,
                          "diff": diff[:100_000]})
        return {"event_id": event_id, "diffs": diffs}

    # ─── Поиск и таймлайн ────────────────────────────────────
    @router.get("/search")
    async def search(q: str, limit: int = 50):
        rec = _rec()
        evs = rec.store.search(q, limit=limit)
        return {"count": len(evs), "events": [
            {"event_id": e.event_id, "seq": e.seq, "time": e.iso_time,
             "kind": e.kind, "action": e.action, "tool": e.tool_name,
             "agent": e.agent_id, "task_id": e.task_id,
             "targets": e.targets, "status": e.status}
            for e in evs
        ]}

    @router.get("/timeline", response_class=PlainTextResponse)
    async def timeline(
        session_id: str = "", task_id: str = "",
        limit: int = 300, format: str = "text",
    ):
        """Человеко- и AI-читаемая хронология (LLM-совместимый вид)."""
        rec = _rec()
        evs = rec.store.query(
            session_id=session_id or None, task_id=task_id or None,
            limit=limit, order_desc=False)
        lines = []
        scope = f"task {task_id}" if task_id else f"session {session_id}"
        lines.append(f"ЖУРНАЛ ДЕЙСТВИЙ ({scope}): {len(evs)} событий")
        lines.append("=" * 60)
        for e in evs:
            flag = "✓" if e.status == "ok" else f"[{e.status}]"
            what = f"{e.server_name}.{e.tool_name}" if e.tool_name else e.action
            targets = f" → {', '.join(e.targets)}" if e.targets else ""
            agent = f" ({e.agent_id})" if e.agent_id else ""
            err = f" !! {e.error[:120]}" if e.error else ""
            lines.append(
                f"{e.seq:>6} {e.iso_time} {flag} {what}{targets}{agent} "
                f"[{e.duration_ms:.0f}мс]{err}")
        return "\n".join(lines)

    # ─── Граф зависимостей ───────────────────────────────────
    @router.get("/graph")
    async def graph(
        session_id: str = "", task_id: str = "",
        limit: int = 300, format: str = "json",
    ):
        rec = _rec()
        g = DependencyGraph(rec.store)
        if format == "mermaid":
            events = g.build(session_id=session_id, task_id=task_id, limit=limit)
            edges = g.edges(events)
            return PlainTextResponse(g.to_mermaid(events, edges))
        vis = g.to_vis(session_id=session_id, task_id=task_id, limit=limit)
        vis["summary"] = graph_summary(
            {n["id"]: _ev_from_node(rec, n) for n in vis["nodes"]},
            vis["edges"])
        return vis

    @router.get("/provenance")
    async def provenance(target: str, limit: int = 100):
        """Полная история изменений конкретного файла/таблицы."""
        rec = _rec()
        g = DependencyGraph(rec.store)
        return g.provenance(target, limit=limit)

    # ─── Откат ───────────────────────────────────────────────
    @router.post("/rollback")
    async def rollback(payload: dict = Body(...)):
        rec = _rec()
        engine = RollbackEngine(rec.store, rec.shadows, rec.project_root)
        mode = payload.get("mode", "dry_run")
        if mode not in ("dry_run", "execute"):
            raise HTTPException(400, "mode: dry_run | execute")
        force = bool(payload.get("force", False))
        if payload.get("event_id"):
            return engine.rollback_event(payload["event_id"], mode, force)
        if payload.get("task_id"):
            return engine.rollback_task(payload["task_id"], mode, force)
        if payload.get("session_id"):
            return engine.rollback_session(payload["session_id"], mode, force)
        raise HTTPException(400, "Нужен event_id, task_id или session_id")

    @router.post("/rollback/verify")
    async def rollback_verify(payload: dict = Body(default={})):
        rec = _rec()
        engine = RollbackEngine(rec.store, rec.shadows, rec.project_root)
        return engine.verify_targets(
            task_id=payload.get("task_id", ""),
            session_id=payload.get("session_id", ""))

    # ─── Воспроизведение ─────────────────────────────────────
    @router.post("/replay/plan")
    async def replay_plan(payload: dict = Body(default={})):
        rec = _rec()
        engine = ReplayEngine(rec.store)
        return engine.build_plan(
            task_id=payload.get("task_id", ""),
            session_id=payload.get("session_id", ""),
            from_event=payload.get("from_event", ""),
            to_event=payload.get("to_event", ""),
            include_non_reversible=bool(
                payload.get("include_non_reversible", False)),
        )

    @router.post("/replay/execute")
    async def replay_execute(payload: dict = Body(...)):
        rec = _rec()
        if payload.get("mode") == "dry_run":
            engine = ReplayEngine(rec.store)
            plan = engine.build_plan(
                task_id=payload.get("task_id", ""),
                session_id=payload.get("session_id", ""))
            return {"mode": "dry_run", "plan": plan}
        mcp = _mcp_from_state()
        if mcp is None:
            raise HTTPException(
                503, "MCPManager недоступен — execute только в основном процессе")
        engine = ReplayEngine(rec.store)
        plan = payload.get("plan")
        if not plan:
            plan = engine.build_plan(
                task_id=payload.get("task_id", ""),
                session_id=payload.get("session_id", ""))
        result = await engine.execute(
            plan, mcp,
            approval_handler=_approval_from_state(),
            session_id=payload.get("session_id", ""),
            journal=rec,
        )
        return result

    @router.get("/replay/export")
    async def replay_export(task_id: str = "", session_id: str = ""):
        rec = _rec()
        engine = ReplayEngine(rec.store)
        plan = engine.build_plan(task_id=task_id, session_id=session_id)
        name = f"replay-{time.strftime('%Y%m%d-%H%M%S')}.json"
        path = rec.cfg.reports_dir / name
        engine.export_script(plan, path)
        return {"path": str(path), "steps": plan.get("steps_count", 0)}

    # ─── Статистика / ретенция ───────────────────────────────
    @router.get("/stats")
    async def stats():
        rec = _rec()
        s = rec.store.stats()
        s["shadows_dir"] = str(rec.cfg.shadows_dir)
        s["watcher"] = rec._watcher.status() if rec._watcher else {"running": False}
        return s

    @router.get("/retention")
    async def retention():
        rec = _rec()
        from .retention import RetentionManager
        m = RetentionManager(rec.cfg, store=rec.store)
        st = m.status()
        st["last_result"] = m.last_result
        return st

    @router.post("/retention/sweep")
    async def retention_sweep():
        rec = _rec()
        from .retention import RetentionManager
        m = RetentionManager(rec.cfg, store=rec.store)
        return m.sweep()

    # ─── Экспорт и отчёты ────────────────────────────────────
    @router.get("/export")
    async def export(
        session_id: str = "", task_id: str = "",
        format: str = "json", limit: int = 5000,
    ):
        rec = _rec()
        evs = rec.store.query(
            session_id=session_id or None, task_id=task_id or None,
            limit=limit, order_desc=False)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        scope = task_id[:10] or session_id[:10] or "all"
        if format == "md":
            from .reports import ReportGenerator
            gen = ReportGenerator(rec)
            if task_id:
                p = gen._generate_task(task_id)
            else:
                p = gen._generate_session(session_id) if session_id else None
            if not p:
                raise HTTPException(404, "Нет событий для отчёта")
            return FileResponse(p, filename=p.name)
        name = rec.cfg.reports_dir / f"export-{scope}-{stamp}.json"
        from .utils import write_text_safe
        import json as _json
        write_text_safe(name, _json.dumps(
            {"count": len(evs),
             "events": [e.to_json() for e in evs]},
            ensure_ascii=False, indent=2, default=str))
        return FileResponse(name, filename=name.name)

    @router.get("/reports")
    async def reports():
        rec = _rec()
        d = rec.cfg.reports_dir
        if not d.exists():
            return {"reports": []}
        items = sorted(
            [f for f in d.iterdir() if f.suffix in (".md", ".json")],
            key=lambda f: f.stat().st_mtime, reverse=True)
        return {"reports": [
            {"name": f.name, "size": human_size(f.stat().st_size),
             "modified": time.strftime(
                 "%Y-%m-%d %H:%M:%S", time.localtime(f.stat().st_mtime))}
            for f in items[:100]
        ]}

    @router.get("/reports/{name}")
    async def report_file(name: str):
        rec = _rec()
        if "/" in name or "\\" in name or ".." in name:
            raise HTTPException(400, "Некорректное имя")
        p = rec.cfg.reports_dir / name
        if not p.exists():
            raise HTTPException(404, "Отчёт не найден")
        return FileResponse(p)

    return router


# ─── Вспомогательные ─────────────────────────────────────────
def _ev_from_node(rec, node: dict):
    return rec.store.get(node["id"]) if rec else None


def _mcp_from_state():
    try:
        from src import main as main_mod
        return main_mod.state.get("mcp")
    except Exception:
        return None


def _approval_from_state():
    # approval идёт через WS-клиента; при replay из API — авто-отказ
    # для опасных инструментов (требуют UI-подтверждения)
    async def _auto(request: dict) -> bool:
        return False
    return _auto
