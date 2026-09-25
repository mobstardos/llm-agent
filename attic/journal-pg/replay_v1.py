"""Воспроизведение действий.

Сценарии:
1. replay_from_checkpoint(checkpoint_id) — с чистого состояния
2. replay_range(from, to)               — конкретный диапазон
3. replay_session(session_id)           — вся сессия

Важно: read-tools воспроизводятся, write-tools — только если явно разрешено.
"""
from __future__ import annotations

import json
import logging
import time
import uuid

from src.db.pool import DatabasePool

logger = logging.getLogger(__name__)


class ReplayManager:
    def __init__(self, pool: DatabasePool, executor=None):
        """executor — async функция для вызова tool (может быть MCPManager)."""
        self.pool = pool
        self.executor = executor

    async def plan(
        self,
        *,
        session_id: str | None = None,
        from_action: str | None = None,
        to_action: str | None = None,
        checkpoint_id: str | None = None,
    ) -> dict:
        """Dry-run: что будет воспроизведено."""
        conditions = []
        params = []

        if session_id:
            conditions.append("session_id = %s")
            params.append(session_id)

        if checkpoint_id:
            cp = await self.pool.execute(
                "SELECT created_at FROM journal.checkpoints WHERE id = %s",
                (checkpoint_id,),
            )
            if not cp:
                return {"error": "checkpoint not found"}
            conditions.append("created_at > %s")
            params.append(cp[0]["created_at"])

        if from_action:
            fa = await self.pool.execute(
                "SELECT created_at FROM journal.actions WHERE id = %s",
                (from_action,),
            )
            if fa:
                conditions.append("created_at >= %s")
                params.append(fa[0]["created_at"])

        if to_action:
            ta = await self.pool.execute(
                "SELECT created_at FROM journal.actions WHERE id = %s",
                (to_action,),
            )
            if ta:
                conditions.append("created_at <= %s")
                params.append(ta[0]["created_at"])

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        sql = f"""
            SELECT id, agent, tool, args, category, success,
                   reversible, created_at
            FROM journal.actions
            {where}
            ORDER BY created_at ASC
        """

        rows = await self.pool.execute(sql, tuple(params))

        readonly = [r for r in rows if r["category"] in ("read", "query")]
        write = [r for r in rows if r["category"] not in ("read", "query")]

        return {
            "dry_run": True,
            "total": len(rows),
            "readonly": len(readonly),
            "write": len(write),
            "actions": [
                {
                    "id": r["id"],
                    "agent": r["agent"],
                    "tool": r["tool"],
                    "category": r["category"],
                    "success": r["success"],
                    "created_at": r["created_at"].isoformat(),
                }
                for r in rows
            ],
            "warnings": [
                "write-tools будут пропущены без allow_write=true",
            ] if write else [],
        }

    async def replay(
        self,
        *,
        session_id: str | None = None,
        from_action: str | None = None,
        to_action: str | None = None,
        checkpoint_id: str | None = None,
        allow_write: bool = False,
        dry_run: bool = False,
    ) -> dict:
        """Воспроизвести действия."""
        plan = await self.plan(
            session_id=session_id,
            from_action=from_action,
            to_action=to_action,
            checkpoint_id=checkpoint_id,
        )

        if "error" in plan:
            return plan

        if dry_run:
            return plan

        replay_id = str(uuid.uuid4())
        started = time.time()

        await self._start_replay(replay_id, from_action, to_action, checkpoint_id)

        log: list[dict] = []
        replayed = 0
        errors = 0

        for a in plan["actions"]:
            cat = a["category"]
            if cat not in ("read", "query") and not allow_write:
                log.append({
                    "action_id": a["id"],
                    "status": "skipped",
                    "reason": "write_not_allowed",
                })
                continue

            if self.executor is None:
                log.append({
                    "action_id": a["id"],
                    "status": "skipped",
                    "reason": "no_executor",
                })
                continue

            try:
                action_rows = await self.pool.execute(
                    "SELECT tool, args FROM journal.actions WHERE id = %s",
                    (a["id"],),
                )
                if not action_rows:
                    continue
                tool = action_rows[0]["tool"]
                args = action_rows[0]["args"]
                if isinstance(args, str):
                    args = json.loads(args)

                await self.executor(tool, args)
                replayed += 1
                log.append({
                    "action_id": a["id"],
                    "status": "ok",
                })
            except Exception as e:
                errors += 1
                log.append({
                    "action_id": a["id"],
                    "status": "error",
                    "error": str(e),
                })

        duration_ms = (time.time() - started) * 1000
        status = "success" if errors == 0 else "failed"

        await self._end_replay(replay_id, status, replayed, errors, log)

        return {
            "replay_id": replay_id,
            "status": status,
            "replayed": replayed,
            "errors": errors,
            "duration_ms": round(duration_ms, 1),
            "log": log,
        }

    async def _start_replay(
        self, replay_id: str, from_action: str | None,
        to_action: str | None, checkpoint_id: str | None,
    ) -> None:
        try:
            async with self.pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("""
                        INSERT INTO journal.replays
                            (id, from_action, to_action, checkpoint_id, status)
                        VALUES (%s, %s, %s, %s, 'running')
                    """, (replay_id, from_action, to_action, checkpoint_id))
        except Exception as e:
            logger.debug("Start replay: %s", e)

    async def _end_replay(
        self, replay_id: str, status: str,
        replayed: int, errors: int, log: list[dict],
    ) -> None:
        try:
            async with self.pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("""
                        UPDATE journal.replays
                        SET status = %s, actions_replayed = %s,
                            errors = %s, ended_at = now(),
                            log = %s::jsonb
                        WHERE id = %s
                    """, (status, replayed, errors,
                          json.dumps(log, default=str), replay_id))
        except Exception as e:
            logger.debug("End replay: %s", e)

    async def list_replays(self, limit: int = 20) -> list[dict]:
        rows = await self.pool.execute("""
            SELECT id, from_action, to_action, checkpoint_id,
                   status, actions_replayed, errors,
                   started_at, ended_at
            FROM journal.replays
            ORDER BY started_at DESC
            LIMIT %s
        """, (limit,))
        return [
            {
                **dict(r),
                "started_at": r["started_at"].isoformat()
                    if r.get("started_at") else None,
                "ended_at": r["ended_at"].isoformat()
                    if r.get("ended_at") else None,
            }
            for r in rows
        ]
