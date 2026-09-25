"""Откат действий.

Три сценария:
1. rollback_single(action_id) — откатить одно действие
2. rollback_chain(action_id) — откатить действие + всё, что от него зависит
3. rollback_to(checkpoint_id) — откатить всё до чекпоинта

Все — с dry_run.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any

from src.db.pool import DatabasePool
from src.journal.action_graph import ActionGraph
from src.journal.storage import BlobStore

logger = logging.getLogger(__name__)


class RollbackManager:
    def __init__(
        self,
        pool: DatabasePool,
        blobs: BlobStore,
        graph: ActionGraph,
        project_root: str,
    ):
        self.pool = pool
        self.blobs = blobs
        self.graph = graph
        self.project_root = Path(project_root).resolve()

    # ═══════════════════════════════════════════════════════
    # Public API
    # ═══════════════════════════════════════════════════════
    async def plan_rollback(
        self,
        *,
        action_id: str | None = None,
        checkpoint_id: str | None = None,
        include_children: bool = False,
    ) -> dict:
        """Dry-run: что будет откачено."""
        if checkpoint_id:
            return await self._plan_rollback_to_checkpoint(checkpoint_id)

        if not action_id:
            return {"error": "action_id or checkpoint_id required"}

        # Одно действие
        action = await self._get_action(action_id)
        if not action:
            return {"error": "action not found"}

        actions = [action]

        # Дочерние (если запрошено)
        if include_children:
            children = await self.graph.transitive_children(action_id)
            for c in children:
                a = await self._get_action(c["id"])
                if a:
                    actions.append(a)

        # Классификация
        reversible = [a for a in actions if a.get("reversible")]
        irreversible = [a for a in actions if not a.get("reversible")]

        files_to_restore = set()
        for a in reversible:
            for f in a.get("affects_files") or []:
                files_to_restore.add(f)

        return {
            "dry_run": True,
            "actions_total": len(actions),
            "reversible": len(reversible),
            "irreversible": len(irreversible),
            "files_to_restore": sorted(files_to_restore),
            "actions": [
                {
                    "id": a["id"],
                    "agent": a["agent"],
                    "tool": a["tool"],
                    "category": a["category"],
                    "created_at": a["created_at"].isoformat()
                        if a.get("created_at") else None,
                    "reversible": a.get("reversible"),
                    "affects_files": a.get("affects_files") or [],
                }
                for a in actions
            ],
            "irreversible_details": [
                {
                    "id": a["id"],
                    "agent": a["agent"],
                    "tool": a["tool"],
                    "reason": "не поддерживает откат",
                }
                for a in irreversible
            ],
        }

    async def rollback(
        self,
        *,
        action_id: str | None = None,
        checkpoint_id: str | None = None,
        include_children: bool = False,
        dry_run: bool = False,
        reason: str = "manual",
    ) -> dict:
        """Выполнить откат."""
        plan = await self.plan_rollback(
            action_id=action_id,
            checkpoint_id=checkpoint_id,
            include_children=include_children,
        )

        if "error" in plan:
            return plan

        rollback_id = str(uuid.uuid4())
        started = time.time()

        # Записать начало
        if not dry_run:
            await self._start_rollback(rollback_id, action_id, checkpoint_id)

        log: list[dict] = []
        files_restored = 0
        actions_reverted = 0

        # Откатываем в обратном порядке (сначала — самые свежие)
        actions_sorted = sorted(
            plan["actions"],
            key=lambda x: x["created_at"] or "",
            reverse=True,
        )

        for a in actions_sorted:
            if not a["reversible"]:
                log.append({
                    "action_id": a["id"],
                    "status": "skipped",
                    "reason": "irreversible",
                })
                continue

            try:
                restored = await self._revert_action(a["id"], dry_run=dry_run)
                files_restored += restored
                actions_reverted += 1
                log.append({
                    "action_id": a["id"],
                    "status": "ok" if not dry_run else "dry_run",
                    "files_restored": restored,
                })
            except Exception as e:
                log.append({
                    "action_id": a["id"],
                    "status": "error",
                    "error": str(e),
                })

        duration_ms = (time.time() - started) * 1000

        if not dry_run:
            await self._end_rollback(
                rollback_id, "success", actions_reverted,
                files_restored, log,
            )

        return {
            "rollback_id": rollback_id,
            "dry_run": dry_run,
            "actions_reverted": actions_reverted,
            "files_restored": files_restored,
            "duration_ms": round(duration_ms, 1),
            "plan": plan,
            "log": log,
        }

    # ═══════════════════════════════════════════════════════
    # Checkpoint-based rollback
    # ═══════════════════════════════════════════════════════
    async def _plan_rollback_to_checkpoint(
        self, checkpoint_id: str,
    ) -> dict:
        cp_rows = await self.pool.execute(
            "SELECT * FROM journal.checkpoints WHERE id = %s",
            (checkpoint_id,),
        )
        if not cp_rows:
            return {"error": "checkpoint not found"}

        cp = cp_rows[0]
        files_snapshot = cp.get("files_snapshot") or {}

        # Actions после чекпоинта
        rows = await self.pool.execute("""
            SELECT * FROM journal.actions
            WHERE created_at > %s
              AND category IN ('write', 'patch', 'delete', 'move')
            ORDER BY created_at DESC
        """, (cp["created_at"],))

        return {
            "dry_run": True,
            "checkpoint_id": checkpoint_id,
            "checkpoint_at": cp["created_at"].isoformat()
                if cp.get("created_at") else None,
            "actions_total": len(rows),
            "reversible": sum(1 for r in rows if r["reversible"]),
            "irreversible": sum(1 for r in rows if not r["reversible"]),
            "files_to_restore": sorted(files_snapshot.keys()),
            "actions": [
                {
                    "id": r["id"],
                    "agent": r["agent"],
                    "tool": r["tool"],
                    "category": r["category"],
                    "created_at": r["created_at"].isoformat(),
                    "reversible": r["reversible"],
                    "affects_files": r["affects_files"] or [],
                }
                for r in rows
            ],
        }

    # ═══════════════════════════════════════════════════════
    # Revert одного действия
    # ═══════════════════════════════════════════════════════
    async def _revert_action(self, action_id: str, dry_run: bool) -> int:
        """Восстановить состояние до действия."""
        # Before snapshots
        snaps = await self.pool.execute("""
            SELECT * FROM journal.snapshots
            WHERE action_id = %s AND phase = 'before'
        """, (action_id,))

        restored = 0

        for s in snaps:
            file_path = s["file_path"]
            hash_ = s["hash"]

            if not hash_:
                # Файла не было до действия — значит нужно удалить
                if not dry_run:
                    await self._delete_path(file_path)
                restored += 1
                continue

            # Восстановить содержимое
            content = await self.blobs.get(hash_)
            if content is None:
                logger.warning("Blob %s not found for %s", hash_, file_path)
                continue

            if not dry_run:
                await self._write_path(file_path, content)
            restored += 1

        # Дополнительно — обработать inverse_op
        action = await self._get_action(action_id)
        inverse = action.get("inverse_op") if action else None
        if isinstance(inverse, str):
            try:
                inverse = json.loads(inverse)
            except Exception:
                inverse = None

        if inverse and not dry_run:
            op = inverse.get("op")
            if op == "reverse_move":
                src = inverse.get("src")
                dst = inverse.get("dst")
                if src and dst:
                    await self._move_path(src, dst)

        return restored

    async def _delete_path(self, rel: str) -> None:
        try:
            p = (self.project_root / rel).resolve()
            if self.project_root not in p.parents and p != self.project_root:
                return
            if p.exists():
                if p.is_file():
                    p.unlink()
                elif p.is_dir():
                    import shutil
                    shutil.rmtree(p)
        except Exception as e:
            logger.debug("Delete %s: %s", rel, e)

    async def _write_path(self, rel: str, content: bytes) -> None:
        try:
            p = (self.project_root / rel).resolve()
            if self.project_root not in p.parents and p != self.project_root:
                return
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content)
        except Exception as e:
            logger.debug("Write %s: %s", rel, e)

    async def _move_path(self, src_rel: str, dst_rel: str) -> None:
        try:
            src = (self.project_root / src_rel).resolve()
            dst = (self.project_root / dst_rel).resolve()
            if self.project_root not in src.parents and src != self.project_root:
                return
            if src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                import shutil
                shutil.move(str(src), str(dst))
        except Exception as e:
            logger.debug("Move %s→%s: %s", src_rel, dst_rel, e)

    # ═══════════════════════════════════════════════════════
    # Helpers
    # ═══════════════════════════════════════════════════════
    async def _get_action(self, action_id: str) -> dict | None:
        rows = await self.pool.execute(
            "SELECT * FROM journal.actions WHERE id = %s",
            (action_id,),
        )
        return rows[0] if rows else None

    async def _start_rollback(
        self, rollback_id: str, action_id: str | None,
        checkpoint_id: str | None,
    ) -> None:
        try:
            async with self.pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("""
                        INSERT INTO journal.rollbacks
                            (id, target_action, checkpoint_id, status)
                        VALUES (%s, %s, %s, 'running')
                    """, (rollback_id, action_id, checkpoint_id))
        except Exception as e:
            logger.debug("Start rollback: %s", e)

    async def _end_rollback(
        self, rollback_id: str, status: str,
        actions_reverted: int, files_restored: int,
        log: list[dict],
    ) -> None:
        try:
            async with self.pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("""
                        UPDATE journal.rollbacks
                        SET status = %s, actions_reverted = %s,
                            files_restored = %s, ended_at = now(),
                            log = %s::jsonb
                        WHERE id = %s
                    """, (status, actions_reverted, files_restored,
                          json.dumps(log, default=str), rollback_id))
        except Exception as e:
            logger.debug("End rollback: %s", e)

    async def list_rollbacks(self, limit: int = 20) -> list[dict]:
        rows = await self.pool.execute("""
            SELECT id, target_action, checkpoint_id, actions_reverted,
                   files_restored, status, dry_run, started_at, ended_at
            FROM journal.rollbacks
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

    # ═══════════════════════════════════════════════════════
    # Checkpoints
    # ═══════════════════════════════════════════════════════
    async def create_checkpoint(
        self,
        label: str | None = None,
        reason: str = "manual",
        files: list[str] | None = None,
    ) -> str:
        """Создать чекпоинт — снимок текущего состояния."""
        files = files or []
        snapshot: dict[str, str] = {}

        # Если files не указаны — снимем все .py/.md/.yaml/.json
        if not files:
            for p in self.project_root.rglob("*"):
                if not p.is_file():
                    continue
                if any(part in (".git", ".venv", "__pycache__",
                                "node_modules", "data")
                       for part in p.parts):
                    continue
                if p.suffix.lower() not in (
                    ".py", ".js", ".ts", ".md", ".yaml", ".yml",
                    ".json", ".toml", ".sql", ".bsl", ".xml",
                ):
                    continue
                if p.stat().st_size > 2 * 1024 * 1024:
                    continue
                try:
                    rel = str(p.relative_to(self.project_root)).replace("\\", "/")
                    content = p.read_bytes()
                    h = await self.blobs.put(content)
                    snapshot[rel] = h
                except Exception:
                    continue

        # Текущее последнее действие
        rows = await self.pool.execute("""
            SELECT id FROM journal.actions ORDER BY created_at DESC LIMIT 1
        """)
        last_action = rows[0]["id"] if rows else None

        cp_id = str(uuid.uuid4())
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("""
                    INSERT INTO journal.checkpoints
                        (id, label, action_id, files_snapshot, reason)
                    VALUES (%s, %s, %s, %s::jsonb, %s)
                """, (cp_id, label, last_action,
                      json.dumps(snapshot), reason))

        logger.info("Checkpoint created: %s (%d files)", cp_id, len(snapshot))
        return cp_id

    async def list_checkpoints(self, limit: int = 50) -> list[dict]:
        rows = await self.pool.execute("""
            SELECT id, label, action_id, reason, created_at,
                   jsonb_object_keys_count(files_snapshot) AS files_count
            FROM journal.checkpoints
            ORDER BY created_at DESC
            LIMIT %s
        """, (limit,))
        return [
            {
                **dict(r),
                "created_at": r["created_at"].isoformat()
                    if r.get("created_at") else None,
            }
            for r in rows
        ]

    async def delete_checkpoint(self, checkpoint_id: str) -> bool:
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "DELETE FROM journal.checkpoints WHERE id = %s",
                    (checkpoint_id,),
                )
                return cur.rowcount > 0
