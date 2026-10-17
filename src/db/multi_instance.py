"""Multi-instance: advisory locks + LISTEN/NOTIFY."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import socket
from typing import Awaitable, Callable

from src.db.pool import DatabasePool

logger = logging.getLogger(__name__)


def _lock_id(name: str) -> int:
    h = hashlib.sha256(name.encode("utf-8")).digest()
    return int.from_bytes(h[:8], "big", signed=True)


class AdvisoryLock:
    def __init__(self, pool: DatabasePool, name: str):
        self.pool = pool
        self.name = name
        self.lock_id = _lock_id(name)
        self._held = False

    async def try_acquire(self) -> bool:
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT pg_try_advisory_lock(%s) AS ok", (self.lock_id,),
                )
                row = await cur.fetchone()
                self._held = bool(row and row.get("ok"))
                return self._held

    async def release(self) -> None:
        if not self._held:
            return
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT pg_advisory_unlock(%s)", (self.lock_id,),
                )
        self._held = False


class InstanceRegistry:
    """Реестр инстансов в ops.instances (Task 14).

    Каждый инстанс: register() при старте, heartbeat() каждые 30с,
    report_metrics() раз в минуту (JSONB-снапшот состояния).
    Аналитика по реестру и нагрузке — analytics() ниже; эндпоинты —
    фича features/cluster.
    """

    ACTIVE_TTL = 90          # сек: heartbeat свежее → инстанс активен
    STALE_TTL = 300          # сек: не отвечает дольше → «отсутствует»
    CLEANUP_TTL = 6 * 3600   # сек: запись удаляется из реестра

    def __init__(self, pool: DatabasePool, instance_id: str | None = None):
        self.pool = pool
        self.instance_id = instance_id or os.getenv(
            "INSTANCE_ID", f"{socket.gethostname()}-{os.getpid()}",
        )
        self.heartbeat_task: asyncio.Task | None = None
        self.registered: dict = {}

    async def register(self, info: dict | None = None) -> None:
        data = {
            "hostname": socket.gethostname(),
            "pid": os.getpid(),
            "role": "agent",
            "version": "",
        }
        data.update(info or {})
        self.registered = data
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("""
                INSERT INTO ops.instances
                    (instance_id, hostname, pid, role, version,
                     started_at, last_heartbeat, meta)
                VALUES (%s, %s, %s, %s, %s, now(), now(), %s::jsonb)
                ON CONFLICT (instance_id) DO UPDATE SET
                    hostname = EXCLUDED.hostname,
                    pid = EXCLUDED.pid,
                    role = EXCLUDED.role,
                    version = EXCLUDED.version,
                    last_heartbeat = now(),
                    meta = EXCLUDED.meta,
                    updated_at = now()
            """, (
                self.instance_id, data.get("hostname", ""),
                int(data.get("pid") or 0), data.get("role", "agent"),
                data.get("version", ""), _js(data.get("meta") or {}),
            ))

    async def heartbeat(self) -> None:
        while True:
            try:
                async with self.pool.connection() as conn, \
                        conn.cursor() as cur:
                    await cur.execute(
                        "UPDATE ops.instances SET last_heartbeat = now(), "
                        "updated_at = now() WHERE instance_id = %s",
                        (self.instance_id,),
                    )
            except Exception as e:
                logger.debug("Heartbeat failed: %s", e)
            await asyncio.sleep(30)

    async def report_metrics(self, metrics: dict) -> None:
        """Обновить JSONB-снапшот состояния (вызывается планировщиком)."""
        try:
            async with self.pool.connection() as conn, \
                    conn.cursor() as cur:
                await cur.execute(
                    "UPDATE ops.instances SET metrics = %s::jsonb, "
                    "last_heartbeat = now(), updated_at = now() "
                    "WHERE instance_id = %s",
                    (_js(metrics), self.instance_id),
                )
        except Exception as e:
            logger.debug("report_metrics: %s", e)

    async def start_heartbeat(self) -> None:
        if self.heartbeat_task is None:
            self.heartbeat_task = asyncio.create_task(self.heartbeat())

    async def stop_heartbeat(self) -> None:
        if self.heartbeat_task:
            self.heartbeat_task.cancel()
            self.heartbeat_task = None

    async def _rows(self, op: str, ttl_seconds: int) -> list[dict]:
        try:
            rows = await self.pool.execute(
                f"""
                SELECT instance_id, hostname, pid, role, version,
                       started_at, last_heartbeat, metrics, meta
                FROM ops.instances
                WHERE last_heartbeat {op} now() - (%s || ' seconds')::interval
                ORDER BY last_heartbeat DESC
                """,
                (ttl_seconds,),
            )
        except Exception as e:
            logger.debug("InstanceRegistry list: %s", e)
            return []
        return [_inst(r) for r in rows or []]

    async def list_active(self, ttl_seconds: int = ACTIVE_TTL) -> list[dict]:
        """Активные инстансы (heartbeat свежее ttl)."""
        return await self._rows(">", ttl_seconds)

    async def list_stale(self, ttl_seconds: int = STALE_TTL) -> list[dict]:
        """Инстансы без heartbeat дольше ttl (но ещё не вычищенные)."""
        return await self._rows("<", ttl_seconds)

    async def cleanup_stale(self, ttl_seconds: int = CLEANUP_TTL) -> int:
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "DELETE FROM ops.instances "
                "WHERE last_heartbeat < now() - (%s || ' seconds')::interval",
                (ttl_seconds,),
            )
            return cur.rowcount


async def analytics(pool: DatabasePool, hours: int = 24) -> dict:
    """Мультиинстанс-аналитика: реестр + нагрузка по зеркалам за окно.

    Используется фичей features/cluster (GET /api/cluster/analytics).
    Работает на зеркалах — горячий путь чата не затрагивается.
    """
    hours = max(1, min(int(hours), 24 * 30))
    reg = InstanceRegistry(pool, instance_id="__analytics__")
    active = await reg.list_active()
    stale = await reg.list_stale()
    stale = [s for s in stale
             if s["instance_id"] not in
             {a["instance_id"] for a in active}]

    out: dict = {
        "hours": hours,
        "instances": {"active": active, "stale": stale,
                      "active_count": len(active),
                      "stale_count": len(stale)},
    }
    try:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("""
                SELECT count(*)::int AS c FROM journal.events_mirror
                WHERE ts > now() - (%s || ' hours')::interval
            """, (hours,))
            out["events_total"] = (await cur.fetchone())["c"]
            await cur.execute("""
                SELECT count(*)::int AS c FROM journal.actions
                WHERE created_at > now() - (%s || ' hours')::interval
            """, (hours,))
            out["tool_calls_total"] = (await cur.fetchone())["c"]
            await cur.execute("""
                SELECT count(*)::int AS c FROM ops.chat_sessions
                WHERE last_seen > now() - (%s || ' hours')::interval
            """, (hours,))
            out["sessions_total"] = (await cur.fetchone())["c"]
            await cur.execute("""
                SELECT count(*)::int AS c FROM ops.plans
                WHERE updated_at > now() - (%s || ' hours')::interval
            """, (hours,))
            out["plans_total"] = (await cur.fetchone())["c"]
            # Почасовая нагрузка (для спарклайна в UI)
            await cur.execute("""
                SELECT to_char(date_trunc('hour', ts), 'YYYY-MM-DD HH24:00')
                           AS hour,
                       count(*)::int AS events,
                       count(*) FILTER (WHERE kind = 'tool_call')::int
                           AS tool_calls
                FROM journal.events_mirror
                WHERE ts > now() - (%s || ' hours')::interval
                GROUP BY 1 ORDER BY 1
            """, (hours,))
            out["hourly"] = [
                {"hour": r["hour"], "events": r["events"],
                 "tool_calls": r["tool_calls"]}
                for r in await cur.fetchall()
            ]
            # Топ инструментов и агентов
            await cur.execute("""
                SELECT tool_name, count(*)::int AS c
                FROM journal.actions
                WHERE created_at > now() - (%s || ' hours')::interval
                GROUP BY 1 ORDER BY c DESC LIMIT 10
            """, (hours,))
            out["top_tools"] = [
                {"tool": r["tool_name"], "count": r["c"]}
                for r in await cur.fetchall()
            ]
            await cur.execute("""
                SELECT agent, count(*)::int AS c
                FROM journal.actions
                WHERE created_at > now() - (%s || ' hours')::interval
                GROUP BY 1 ORDER BY c DESC LIMIT 10
            """, (hours,))
            out["top_agents"] = [
                {"agent": r["agent"], "count": r["c"]}
                for r in await cur.fetchall()
            ]
            # Инстансы в событиях (если instance_id записан в meta)
            await cur.execute("""
                SELECT meta->>'instance_id' AS instance_id,
                       count(*)::int AS c
                FROM journal.events_mirror
                WHERE ts > now() - (%s || ' hours')::interval
                  AND meta ? 'instance_id'
                GROUP BY 1 ORDER BY c DESC LIMIT 20
            """, (hours,))
            out["events_by_instance"] = [
                {"instance_id": r["instance_id"], "count": r["c"]}
                for r in await cur.fetchall()
            ]
    except Exception as e:
        logger.debug("analytics mirror stats: %s", e)
        out["mirror_error"] = str(e)[:200]
    return out


def _inst(r: dict) -> dict:
    return {
        "instance_id": r["instance_id"],
        "hostname": r.get("hostname") or "",
        "pid": r.get("pid") or 0,
        "role": r.get("role") or "agent",
        "version": r.get("version") or "",
        "started_at": str(r.get("started_at") or ""),
        "last_heartbeat": str(r.get("last_heartbeat") or ""),
        "metrics": r.get("metrics") or {},
        "meta": r.get("meta") or {},
    }


def _js(obj) -> str:
    return json.dumps(obj, ensure_ascii=False, default=str)


class Listener:
    def __init__(self, pool: DatabasePool):
        self.pool = pool
        self._tasks: dict[str, asyncio.Task] = {}

    async def listen(
        self, channel: str,
        handler: Callable[[str], Awaitable[None]],
    ) -> None:
        if channel in self._tasks:
            return

        async def _loop():
            while True:
                try:
                    async with self.pool.connection() as conn:
                        await conn.set_autocommit(True)
                        async with conn.cursor() as cur:
                            await cur.execute(f"LISTEN {channel}")
                        async for notify in conn.notifies():
                            try:
                                await handler(notify.payload)
                            except Exception as e:
                                logger.warning("Listener handler: %s", e)
                except asyncio.CancelledError:
                    return
                except Exception as e:
                    logger.warning("Listener %s died: %s", channel, e)
                    await asyncio.sleep(5)

        self._tasks[channel] = asyncio.create_task(_loop())
        logger.info("Listening on channel: %s", channel)

    async def stop(self) -> None:
        for ch, task in self._tasks.items():
            task.cancel()
        self._tasks.clear()
