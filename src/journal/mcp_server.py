"""MCP-сервер journal: доступ агентов к журналу через stdio.

Запускается как обычный MCP-сервер (см. mcp_servers/journal/server.yaml).
Работает с той же базой SQLite (WAL) — чтение не блокирует запись.

Инструменты:
  journal_query    — выборка событий с фильтрами
  journal_search   — полнотекстовый поиск
  journal_get      — полное событие по id
  journal_timeline — хронология текстом (LLM-совместимой)
  journal_stats    — статистика и статус ретенции
  journal_rollback — откат (dry_run по умолчанию!)
  journal_replay_plan — план воспроизведения действий
  journal_provenance  — история изменений файла
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

# Корень проекта в sys.path (запуск: python -m src.journal.mcp_server)
_BASE = Path(__file__).resolve().parent.parent.parent
if str(_BASE) not in sys.path:
    sys.path.insert(0, str(_BASE))

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

from src.journal.config import JournalConfig
from src.journal.dependencies import DependencyGraph
from src.journal.recorder import JournalRecorder
from src.journal.replay import ReplayEngine
from src.journal.rollback import RollbackEngine

app = Server("journal")

_recorder: JournalRecorder | None = None


def _get_recorder() -> JournalRecorder:
    global _recorder
    if _recorder is None:
        # База журнала живёт в ПАПКЕ СИСТЕМЫ llm-agent (рядом с src/),
        # а PROJECT_ROOT — это редактируемый проект пользователя.
        base = os.getenv("JOURNAL_BASE_DIR", "").strip() or str(_BASE)
        project_root = os.getenv("PROJECT_ROOT", "").strip() or base
        cfg = JournalConfig.from_env(base_dir=Path(base) / "data" / "journal")
        # MCP-сервер — наблюдатель; watcher/отчёты ведёт основной процесс
        cfg.watch_project = False
        cfg.reports_enabled = False
        _recorder = JournalRecorder(cfg, project_root=project_root)
    return _recorder


def _project_root() -> Path:
    root = os.getenv("PROJECT_ROOT", "").strip()
    return Path(root) if root else _BASE


def _json(obj) -> list[TextContent]:
    return [TextContent(text=json.dumps(
        obj, ensure_ascii=False, indent=2, default=str))]


# ─── Схемы инструментов ──────────────────────────────────────
TOOLS = [
    Tool(
        name="journal_query",
        description="Выборка событий журнала с фильтрами. "
                    "kind: session|task|tool_call|file_change|rollback|replay|system.",
        inputSchema={
            "type": "object",
            "properties": {
                "kind": {"type": "string"},
                "session_id": {"type": "string"},
                "task_id": {"type": "string"},
                "agent_id": {"type": "string"},
                "server": {"type": "string"},
                "tool": {"type": "string"},
                "target": {"type": "string",
                           "description": "путь/таблица (точное совпадение)"},
                "mutating": {"type": "boolean",
                             "description": "только изменяющие действия"},
                "limit": {"type": "integer", "default": 50},
                "offset": {"type": "integer", "default": 0},
            },
        },
    ),
    Tool(
        name="journal_search",
        description="Полнотекстовый поиск по журналу (действия, файлы, ошибки).",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "default": 30},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="journal_get",
        description="Полное событие по event_id (аргументы, результат, тени).",
        inputSchema={
            "type": "object",
            "properties": {"event_id": {"type": "string"}},
            "required": ["event_id"],
        },
    ),
    Tool(
        name="journal_timeline",
        description="Хронология действий текстом: по задаче, сессии или последние N.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "session_id": {"type": "string"},
                "limit": {"type": "integer", "default": 100},
            },
        },
    ),
    Tool(
        name="journal_stats",
        description="Статистика журнала: количество событий, размер, диск, ретенция.",
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="journal_provenance",
        description="Полная история изменений файла или таблицы (кто, когда, чем).",
        inputSchema={
            "type": "object",
            "properties": {"target": {"type": "string"}},
            "required": ["target"],
        },
    ),
    Tool(
        name="journal_rollback",
        description="Откат изменений к состоянию 'как было'. "
                    "mode=dry_run (по умолчанию) только показывает план; "
                    "mode=execute выполняет. Цель: event_id | task_id | session_id.",
        inputSchema={
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "task_id": {"type": "string"},
                "session_id": {"type": "string"},
                "mode": {"type": "string",
                         "enum": ["dry_run", "execute"], "default": "dry_run"},
                "force": {"type": "boolean", "default": False,
                          "description": "игнорировать конфликты хэшей"},
            },
        },
    ),
    Tool(
        name="journal_replay_plan",
        description="План воспроизведения действий агентов (по задаче/сессии).",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "session_id": {"type": "string"},
                "from_event": {"type": "string"},
                "to_event": {"type": "string"},
            },
        },
    ),
]


@app.list_tools()
async def list_tools() -> list[Tool]:
    return TOOLS


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    rec = _get_recorder()
    a = arguments or {}

    try:
        if name == "journal_query":
            evs = rec.store.query(
                kind=a.get("kind") or None,
                session_id=a.get("session_id") or None,
                task_id=a.get("task_id") or None,
                agent_id=a.get("agent_id") or None,
                server_name=a.get("server") or None,
                tool_name=a.get("tool") or None,
                target_like=a.get("target") or None,
                only_mutating=bool(a.get("mutating")),
                limit=int(a.get("limit", 50)),
                offset=int(a.get("offset", 0)),
                order_desc=True,
            )
            return _json({
                "count": len(evs),
                "events": [
                    {"event_id": e.event_id, "seq": e.seq,
                     "time": e.iso_time, "kind": e.kind,
                     "action": e.action, "tool": e.tool_name,
                     "server": e.server_name, "agent": e.agent_id,
                     "task_id": e.task_id, "targets": e.targets,
                     "status": e.status, "reversible": e.reversible,
                     "shadow_dir": e.shadow_dir,
                     "error": e.error[:200] if e.error else ""}
                    for e in evs
                ],
            })

        if name == "journal_search":
            evs = rec.store.search(
                a.get("query", ""), limit=int(a.get("limit", 30)))
            return _json({"count": len(evs), "events": [
                {"event_id": e.event_id, "time": e.iso_time,
                 "kind": e.kind, "action": e.action,
                 "tool": e.tool_name, "targets": e.targets,
                 "task_id": e.task_id, "status": e.status}
                for e in evs]})

        if name == "journal_get":
            e = rec.store.get(a.get("event_id", ""))
            if not e:
                return _json({"error": "не найдено"})
            return _json(e.to_json())

        if name == "journal_timeline":
            evs = rec.store.query(
                task_id=a.get("task_id") or None,
                session_id=a.get("session_id") or None,
                limit=int(a.get("limit", 100)), order_desc=False)
            lines = ["ЖУРНАЛ ДЕЙСТВИЙ", "=" * 50]
            for e in evs:
                mark = "ok" if e.status == "ok" else e.status.upper()
                what = (f"{e.server_name}.{e.tool_name}"
                        if e.tool_name else e.action)
                tg = f" -> {', '.join(e.targets)}" if e.targets else ""
                lines.append(f"{e.seq:>6} {e.iso_time} [{mark}] "
                             f"{what}{tg} ({e.agent_id or '-'})")
            return [TextContent(text="\n".join(lines))]

        if name == "journal_stats":
            s = rec.store.stats()
            from src.journal.utils import dir_size
            s["shadows_size"] = dir_size(rec.cfg.shadows_dir)
            return _json(s)

        if name == "journal_provenance":
            g = DependencyGraph(rec.store)
            return _json(g.provenance(a.get("target", ""),
                                      limit=int(a.get("limit", 100))))

        if name == "journal_rollback":
            engine = RollbackEngine(
                rec.store, rec.shadows, _project_root())
            mode = a.get("mode", "dry_run")
            force = bool(a.get("force", False))
            if a.get("event_id"):
                res = engine.rollback_event(a["event_id"], mode, force)
            elif a.get("task_id"):
                res = engine.rollback_task(a["task_id"], mode, force)
            elif a.get("session_id"):
                res = engine.rollback_session(a["session_id"], mode, force)
            else:
                res = {"ok": False,
                       "error": "укажите event_id, task_id или session_id"}
            return _json(res)

        if name == "journal_replay_plan":
            engine = ReplayEngine(rec.store)
            plan = engine.build_plan(
                task_id=a.get("task_id", ""),
                session_id=a.get("session_id", ""),
                from_event=a.get("from_event", ""),
                to_event=a.get("to_event", ""),
            )
            return _json(plan)

        return _json({"error": f"Неизвестный инструмент: {name}"})

    except Exception as e:
        return _json({"error": str(e)})


async def _run() -> None:
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(_run())
