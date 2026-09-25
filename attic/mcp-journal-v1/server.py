"""MCP-сервер: Journal — доступ агентов к истории действий."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

logging.basicConfig(level=logging.INFO, stream=sys.stderr)
logger = logging.getLogger("journal-mcp")

_pool = None
_blobs = None
_graph = None
_recorder = None
_rollback = None
_replay = None
_retention = None


def _get_root() -> str:
    try:
        from src.runtime_config import get_project_root
        v = get_project_root(default="")
        if v:
            return v
    except Exception:
        pass
    return os.getenv("PROJECT_ROOT", os.getcwd())


async def _init():
    global _pool, _blobs, _graph, _recorder, _rollback, _replay, _retention
    if _pool is not None:
        return

    from src.db.pool import get_pool
    from src.journal.storage import BlobStore
    from src.journal.recorder import JournalRecorder
    from src.journal.action_graph import ActionGraph
    from src.journal.rollback import RollbackManager
    from src.journal.replay import ReplayManager
    from src.journal.retention import RetentionManager

    _pool = await get_pool()
    _blobs = BlobStore(_pool)
    _graph = ActionGraph(_pool)
    _recorder = JournalRecorder(_pool, _blobs)
    _rollback = RollbackManager(
        _pool, _blobs, _graph, project_root=_get_root(),
    )
    _replay = ReplayManager(_pool, executor=None)
    _retention = RetentionManager(_pool, _blobs, disk_path=_get_root())


def _json_result(data, limit: int = 50000) -> str:
    try:
        return json.dumps(data, ensure_ascii=False, indent=2, default=str)[:limit]
    except Exception:
        return str(data)[:limit]


app = Server("journal")


@app.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(name="journal_search",
             description="Найти действия по фильтрам (агент, инструмент, файл, категория).",
             inputSchema={"type": "object", "properties": {
                 "agent": {"type": "string"},
                 "tool": {"type": "string"},
                 "file": {"type": "string"},
                 "category": {"type": "string"},
                 "session_id": {"type": "string"},
                 "days_back": {"type": "integer", "default": 7},
                 "limit": {"type": "integer", "default": 50}}}),
        Tool(name="journal_get",
             description="Полное действие по ID (включая snapshots).",
             inputSchema={"type": "object", "properties": {
                 "action_id": {"type": "string"}},
                 "required": ["action_id"]}),
        Tool(name="journal_recent",
             description="Последние N действий.",
             inputSchema={"type": "object", "properties": {
                 "limit": {"type": "integer", "default": 20}}}),
        Tool(name="journal_history",
             description="История изменений файла (все snapshots).",
             inputSchema={"type": "object", "properties": {
                 "file_path": {"type": "string"},
                 "limit": {"type": "integer", "default": 50}},
                 "required": ["file_path"]}),
        Tool(name="journal_diff",
             description="Diff между двумя snapshots (before/after действия).",
             inputSchema={"type": "object", "properties": {
                 "action_id": {"type": "string"},
                 "file_path": {"type": "string"}},
                 "required": ["action_id", "file_path"]}),
        Tool(name="journal_tree",
             description="Дерево зависимостей (что от чего зависит).",
             inputSchema={"type": "object", "properties": {
                 "action_id": {"type": "string"},
                 "direction": {"type": "string", "default": "children"},
                 "depth": {"type": "integer", "default": 3}},
                 "required": ["action_id"]}),
        Tool(name="journal_stats",
             description="Статистика журнала.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="journal_checkpoint_create",
             description="Создать точку отката.",
             inputSchema={"type": "object", "properties": {
                 "label": {"type": "string"},
                 "reason": {"type": "string", "default": "manual"}}}),
        Tool(name="journal_checkpoint_list",
             description="Список чекпоинтов.",
             inputSchema={"type": "object", "properties": {
                 "limit": {"type": "integer", "default": 50}}}),
        Tool(name="journal_checkpoint_delete",
             description="Удалить чекпоинт.",
             inputSchema={"type": "object", "properties": {
                 "checkpoint_id": {"type": "string"}},
                 "required": ["checkpoint_id"]}),
        Tool(name="journal_plan_rollback",
             description="Dry-run отката (что будет откачено).",
             inputSchema={"type": "object", "properties": {
                 "action_id": {"type": "string"},
                 "checkpoint_id": {"type": "string"},
                 "include_children": {"type": "boolean", "default": False}}}),
        Tool(name="journal_rollback",
             description="Откатить действие или до чекпоинта (опасно!).",
             inputSchema={"type": "object", "properties": {
                 "action_id": {"type": "string"},
                 "checkpoint_id": {"type": "string"},
                 "include_children": {"type": "boolean", "default": False},
                 "dry_run": {"type": "boolean", "default": False}}}),
        Tool(name="journal_replay_plan",
             description="Dry-run реплея.",
             inputSchema={"type": "object", "properties": {
                 "session_id": {"type": "string"},
                 "from_action": {"type": "string"},
                 "to_action": {"type": "string"},
                 "checkpoint_id": {"type": "string"}}}),
        Tool(name="journal_replay",
             description="Воспроизвести действия (опасно!).",
             inputSchema={"type": "object", "properties": {
                 "session_id": {"type": "string"},
                 "from_action": {"type": "string"},
                 "to_action": {"type": "string"},
                 "allow_write": {"type": "boolean", "default": False}}}),
        Tool(name="journal_retention_stats",
             description="Статистика диска и журнала.",
             inputSchema={"type": "object", "properties": {}}),
        Tool(name="journal_retention_enforce",
             description="Применить retention policy (purge).",
             inputSchema={"type": "object", "properties": {}}),
    ]


@app.call_tool()
async def call_tool(name: str, arguments: dict) -> list[TextContent]:
    try:
        await _init()

        # ─── Search ─────────────────────────────────────
        if name == "journal_search":
            conditions = []
            params: list = []

            if arguments.get("agent"):
                conditions.append("agent = %s")
                params.append(arguments["agent"])
            if arguments.get("tool"):
                conditions.append("tool LIKE %s")
                params.append(f"%{arguments['tool']}%")
            if arguments.get("category"):
                conditions.append("category = %s")
                params.append(arguments["category"])
            if arguments.get("session_id"):
                conditions.append("session_id = %s")
                params.append(arguments["session_id"])
            if arguments.get("file"):
                conditions.append("affects_files && %s")
                params.append([arguments["file"]])

            days = arguments.get("days_back", 7)
            conditions.append("created_at > now() - interval '%s days'")
            params.append(days)

            limit = arguments.get("limit", 50)
            params.append(limit)

            where = " AND ".join(conditions)
            rows = await _pool.execute(f"""
                SELECT id, agent, tool, category, success,
                       affects_files, duration_ms, created_at,
                       result_summary
                FROM journal.actions
                WHERE {where}
                ORDER BY created_at DESC
                LIMIT %s
            """, tuple(params))

            return [TextContent(type="text", text=_json_result([
                {**dict(r),
                 "created_at": r["created_at"].isoformat()
                    if r.get("created_at") else None}
                for r in rows
            ]))]

        if name == "journal_get":
            action_id = arguments["action_id"]
            rows = await _pool.execute(
                "SELECT * FROM journal.actions WHERE id = %s", (action_id,),
            )
            if not rows:
                return [TextContent(type="text", text="Не найдено")]
            a = dict(rows[0])

            snaps = await _pool.execute("""
                SELECT phase, file_path, hash, size_bytes
                FROM journal.snapshots
                WHERE action_id = %s
                ORDER BY phase, file_path
            """, (action_id,))

            deps_out = await _pool.execute("""
                SELECT dst AS id, kind FROM journal.dependencies WHERE src = %s
            """, (action_id,))
            deps_in = await _pool.execute("""
                SELECT src AS id, kind FROM journal.dependencies WHERE dst = %s
            """, (action_id,))

            return [TextContent(type="text", text=_json_result({
                **a,
                "created_at": a["created_at"].isoformat()
                    if a.get("created_at") else None,
                "snapshots": [dict(s) for s in snaps],
                "depends_on": [dict(d) for d in deps_out],
                "used_by": [dict(d) for d in deps_in],
            }, limit=80000))]

        if name == "journal_recent":
            limit = arguments.get("limit", 20)
            rows = await _pool.execute("""
                SELECT id, agent, tool, category, success,
                       affects_files, created_at
                FROM journal.actions
                ORDER BY created_at DESC
                LIMIT %s
            """, (limit,))
            return [TextContent(type="text", text=_json_result([
                {**dict(r),
                 "created_at": r["created_at"].isoformat()
                    if r.get("created_at") else None}
                for r in rows
            ]))]

        if name == "journal_history":
            file_path = arguments["file_path"]
            limit = arguments.get("limit", 50)
            rows = await _pool.execute("""
                SELECT s.phase, s.hash, s.size_bytes, s.created_at,
                       a.id AS action_id, a.agent, a.tool, a.category
                FROM journal.snapshots s
                JOIN journal.actions a ON a.id = s.action_id
                WHERE s.file_path = %s
                ORDER BY s.created_at DESC
                LIMIT %s
            """, (file_path, limit))
            return [TextContent(type="text", text=_json_result([
                {**dict(r),
                 "created_at": r["created_at"].isoformat()
                    if r.get("created_at") else None}
                for r in rows
            ]))]

        if name == "journal_diff":
            action_id = arguments["action_id"]
            file_path = arguments["file_path"]

            rows = await _pool.execute("""
                SELECT phase, hash FROM journal.snapshots
                WHERE action_id = %s AND file_path = %s
            """, (action_id, file_path))
            if not rows:
                return [TextContent(type="text", text="Snapshots не найдены")]

            before_hash = next(
                (r["hash"] for r in rows if r["phase"] == "before"), None,
            )
            after_hash = next(
                (r["hash"] for r in rows if r["phase"] == "after"), None,
            )

            before_text = await _blobs.get_text(before_hash) if before_hash else ""
            after_text = await _blobs.get_text(after_hash) if after_hash else ""

            import difflib
            diff = "".join(difflib.unified_diff(
                (before_text or "").splitlines(keepends=True),
                (after_text or "").splitlines(keepends=True),
                fromfile=f"before:{file_path}",
                tofile=f"after:{file_path}",
            ))
            return [TextContent(type="text", text=diff[:50000] or "(идентичны)")]

        if name == "journal_tree":
            action_id = arguments["action_id"]
            direction = arguments.get("direction", "children")
            depth = arguments.get("depth", 3)

            if direction == "children":
                rows = await _graph.transitive_children(action_id, depth)
            else:
                rows = await _graph.transitive_parents(action_id, depth)

            return [TextContent(type="text", text=_json_result([
                {**dict(r),
                 "created_at": r["created_at"].isoformat()
                    if r.get("created_at") else None}
                for r in rows
            ]))]

        if name == "journal_stats":
            blob_stats = await _blobs.stats()
            rows = await _pool.execute("""
                SELECT
                    COUNT(*) AS actions_total,
                    COUNT(*) FILTER (WHERE success) AS success,
                    COUNT(*) FILTER (WHERE NOT success) AS failed,
                    COUNT(*) FILTER (WHERE reversible) AS reversible,
                    COUNT(*) FILTER (WHERE category IN ('write','patch','delete','move'))
                        AS write_ops,
                    COUNT(DISTINCT agent) AS agents,
                    COUNT(DISTINCT session_id) AS sessions
                FROM journal.actions
            """)
            actions = rows[0] if rows else {}

            cp = await _pool.execute(
                "SELECT COUNT(*) AS c FROM journal.checkpoints"
            )
            rb = await _pool.execute(
                "SELECT COUNT(*) AS c FROM journal.rollbacks"
            )
            rp = await _pool.execute(
                "SELECT COUNT(*) AS c FROM journal.replays"
            )
            deps = await _pool.execute(
                "SELECT COUNT(*) AS c FROM journal.dependencies"
            )

            return [TextContent(type="text", text=_json_result({
                "actions": dict(actions),
                "blobs": blob_stats,
                "checkpoints": cp[0]["c"] if cp else 0,
                "rollbacks": rb[0]["c"] if rb else 0,
                "replays": rp[0]["c"] if rp else 0,
                "dependencies": deps[0]["c"] if deps else 0,
            }))]

        if name == "journal_checkpoint_create":
            cp_id = await _rollback.create_checkpoint(
                label=arguments.get("label"),
                reason=arguments.get("reason", "manual"),
            )
            return [TextContent(type="text", text=f"Checkpoint: {cp_id}")]

        if name == "journal_checkpoint_list":
            cps = await _rollback.list_checkpoints(
                limit=arguments.get("limit", 50),
            )
            return [TextContent(type="text", text=_json_result(cps))]

        if name == "journal_checkpoint_delete":
            ok = await _rollback.delete_checkpoint(arguments["checkpoint_id"])
            return [TextContent(
                type="text", text="Удалён" if ok else "Не найден",
            )]

        if name == "journal_plan_rollback":
            plan = await _rollback.plan_rollback(
                action_id=arguments.get("action_id"),
                checkpoint_id=arguments.get("checkpoint_id"),
                include_children=arguments.get("include_children", False),
            )
            return [TextContent(type="text", text=_json_result(plan))]

        if name == "journal_rollback":
            result = await _rollback.rollback(
                action_id=arguments.get("action_id"),
                checkpoint_id=arguments.get("checkpoint_id"),
                include_children=arguments.get("include_children", False),
                dry_run=arguments.get("dry_run", False),
            )
            return [TextContent(type="text", text=_json_result(result))]

        if name == "journal_replay_plan":
            plan = await _replay.plan(
                session_id=arguments.get("session_id"),
                from_action=arguments.get("from_action"),
                to_action=arguments.get("to_action"),
                checkpoint_id=arguments.get("checkpoint_id"),
            )
            return [TextContent(type="text", text=_json_result(plan))]

        if name == "journal_replay":
            result = await _replay.replay(
                session_id=arguments.get("session_id"),
                from_action=arguments.get("from_action"),
                to_action=arguments.get("to_action"),
                allow_write=arguments.get("allow_write", False),
            )
            return [TextContent(type="text", text=_json_result(result))]

        if name == "journal_retention_stats":
            stats = await _retention.collect_stats()
            return [TextContent(type="text", text=_json_result(stats))]

        if name == "journal_retention_enforce":
            result = await _retention.enforce()
            return [TextContent(type="text", text=_json_result(result))]

        return [TextContent(type="text", text=f"Неизвестный инструмент: {name}")]
    except Exception as e:
        logger.exception("Journal tool %s failed", name)
        return [TextContent(type="text", text=f"Ошибка: {e}")]


async def main():
    async with stdio_server() as (read, write):
        await app.run(read, write, app.create_initialization_options())


if __name__ == "__main__":
    asyncio.run(main())
