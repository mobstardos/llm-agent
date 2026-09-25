"""Память агентов на pgvector — семантический индекс прошлых задач (Task 14).

Что это: долгосрочная память «что система уже делала». Наполняется из
зеркал PostgreSQL (chat-сообщения, планы Supervisor, события журнала с
ошибками) и ищется по смыслу.

Хранение: memory.tasks (db/init.sql):
  * embedding / embedding_half (vector(1024)/halfvec(1024)) — если в БД
    есть расширение pgvector;
  * tsv (GENERATED to_tsvector('russian')) — есть всегда, служит
    полнотекстовым фолбэком (как у фичи «История»).

Поиск: трёхуровневый, с честным признаком режима в ответе:
  vector → fts → ilike. Отсутствие pgvector или embedder не ломает
  систему — понижается качество ранжирования.

Заполнение: AgentMemoryIndexer — фоновый цикл (main.py блок 7.5),
периодически дотягивает новые строки из зеркал по source_key
(идемпотентно: UNIQUE(kind, source_key)). AGENT_MEMORY=0 — выключить.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Any

import psycopg

from src.memory.embedders import AGENT_MEMORY_DIM, MemoryEmbedder, make_embedder

logger = logging.getLogger(__name__)


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class AgentMemory:
    """Семантическая память агентов (нужен пул psycopg, схема memory.tasks)."""

    def __init__(self, pool: Any, embedder: MemoryEmbedder | None = None):
        self.pool = pool
        self.embedder = embedder or make_embedder()
        self._flags: dict[str, bool] | None = None
        self._embed_warned = False

    # ─── Флаги окружения (кэшируются) ──────────────────────────────
    async def _capabilities(self) -> dict[str, bool]:
        if self._flags is not None:
            return self._flags
        flags = {"vector": False, "tsv": False}
        try:
            async with self.pool.connection() as conn, conn.cursor() as cur:
                await cur.execute("""
                    SELECT count(*)::int AS c FROM information_schema.columns
                    WHERE table_schema='memory' AND table_name='tasks'
                      AND column_name='embedding_half'
                """)
                row = await cur.fetchone()
                flags["vector"] = bool(row and row.get("c"))
                await cur.execute("""
                    SELECT count(*)::int AS c FROM information_schema.columns
                    WHERE table_schema='memory' AND table_name='tasks'
                      AND column_name='tsv'
                """)
                row = await cur.fetchone()
                flags["tsv"] = bool(row and row.get("c"))
        except Exception as e:
            logger.debug("AgentMemory capabilities: %s", e)
        self._flags = flags
        return flags

    def invalidate_flags(self) -> None:
        self._flags = None

    async def _embed_one(self, text: str) -> list[float] | None:
        try:
            vec = await asyncio.to_thread(self.embedder.embed_one, text)
            if vec and len(vec) == AGENT_MEMORY_DIM:
                return vec
            if not self._embed_warned:
                self._embed_warned = True
                logger.warning(
                    "Embedder %s вернул dim=%s (ожидалось %s) — "
                    "запись сохранена без вектора",
                    self.embedder.name, len(vec) if vec else 0,
                    AGENT_MEMORY_DIM)
            return None
        except Exception as e:
            if not self._embed_warned:
                self._embed_warned = True
                logger.warning("Embedder недоступен (%s) — записи "
                               "сохраняются без вектора (поиск по FTS)",
                               e.__class__.__name__)
            return None

    # ─── Запись ────────────────────────────────────────────────────
    async def upsert(
        self,
        text: str,
        *,
        kind: str = "note",
        source_key: str = "",
        session_id: str = "",
        plan_id: str = "",
        meta: dict | None = None,
        truncate: int = 8000,
    ) -> dict:
        """Добавить/обновить запись. Возвращает {id, embedded}."""
        text = (text or "").strip()
        if not text:
            return {"id": "", "embedded": False, "skipped": True}
        if len(text) > truncate:
            text = text[:truncate]
        h = _hash_text(text)
        key = source_key or f"manual:{h[:24]}"
        caps = await self._capabilities()
        vec = (await self._embed_one(text)) if caps["vector"] else None
        half = [round(x, 6) for x in vec] if vec else None

        if vec is None:
            # Схема без pgvector (или embedder недоступен): колонок
            # embedding/embedding_half может не быть — вставляем без них.
            sql = """
                INSERT INTO memory.tasks
                    (kind, source_key, text, content_hash, session_id,
                     plan_id, meta)
                VALUES (%(kind)s, %(key)s, %(text)s, %(hash)s, %(sid)s,
                        %(pid)s, %(meta)s::jsonb)
                ON CONFLICT (kind, source_key) DO UPDATE SET
                    text = EXCLUDED.text,
                    content_hash = EXCLUDED.content_hash,
                    session_id = EXCLUDED.session_id,
                    plan_id = EXCLUDED.plan_id,
                    meta = EXCLUDED.meta,
                    updated_at = now()
                RETURNING id::text
            """
        else:
            sql = """
                INSERT INTO memory.tasks
                    (kind, source_key, text, content_hash, session_id,
                     plan_id, embedding, embedding_half, meta)
                VALUES (%(kind)s, %(key)s, %(text)s, %(hash)s, %(sid)s,
                        %(pid)s, %(emb)s, %(half)s, %(meta)s::jsonb)
                ON CONFLICT (kind, source_key) DO UPDATE SET
                    text = EXCLUDED.text,
                    content_hash = EXCLUDED.content_hash,
                    session_id = EXCLUDED.session_id,
                    plan_id = EXCLUDED.plan_id,
                    embedding = coalesce(EXCLUDED.embedding,
                                         memory.tasks.embedding),
                    embedding_half = coalesce(EXCLUDED.embedding_half,
                                              memory.tasks.embedding_half),
                    meta = EXCLUDED.meta,
                    updated_at = now()
                RETURNING id::text
            """
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(sql, {
                "kind": kind, "key": key, "text": text, "hash": h,
                "sid": session_id, "pid": plan_id, "emb": vec,
                "half": half, "meta": _js(meta or {}),
            })
            row = await cur.fetchone()
            return {
                "id": (row or {}).get("id", ""),
                "embedded": vec is not None,
                "skipped": False,
            }

    # ─── Поиск ─────────────────────────────────────────────────────
    async def search(
        self,
        query: str,
        *,
        top_k: int = 8,
        kind: str | None = None,
        session_id: str | None = None,
        min_score: float = 0.15,
    ) -> dict:
        """Семантический поиск. Возвращает {mode, items}.

        mode: vector (pgvector + embedder) | fts | ilike | none.
        """
        query = (query or "").strip()
        if not query:
            return {"mode": "none", "items": []}
        top_k = max(1, min(int(top_k), 50))
        caps = await self._capabilities()

        conds: list[str] = []
        params: dict[str, Any] = {"k": top_k}
        if kind:
            conds.append("kind = %(kind)s")
            params["kind"] = kind
        if session_id:
            conds.append("session_id = %(sid)s")
            params["sid"] = session_id

        # 1) Векторный путь
        if caps["vector"]:
            vec = await self._embed_one(query)
            if vec:
                params = {"e": [round(x, 6) for x in vec], **params}
                vconds = conds + ["embedding_half IS NOT NULL"]
                vwhere = "WHERE " + " AND ".join(vconds)
                try:
                    async with self.pool.connection() as conn, \
                            conn.cursor() as cur:
                        await cur.execute(f"""
                            SELECT id::text, kind, text, session_id, plan_id,
                                   meta, created_at,
                                   1 - (embedding_half <=> %(e)s::halfvec)
                                       AS score
                            FROM memory.tasks
                            {vwhere}
                            ORDER BY embedding_half <=> %(e)s::halfvec
                            LIMIT %(k)s
                        """, params)
                        rows = await cur.fetchall()
                except Exception as e:
                    logger.warning("vector search: %s", e)
                    rows = []
                items = [
                    _item(r, mode="vector") for r in rows
                    if float(r.get("score") or 0) >= min_score
                ]
                if items:
                    return {"mode": "vector", "items": items[:top_k]}

        # 2) Полнотекстовый фолбэк
        if caps["tsv"]:
            params2 = {"q": query, **params}
            fconds = conds + ["tsv @@ websearch_to_tsquery('russian', %(q)s)"]
            fwhere = "WHERE " + " AND ".join(fconds)
            async with self.pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(f"""
                    SELECT id::text, kind, text, session_id, plan_id,
                           meta, created_at,
                           ts_rank(tsv, websearch_to_tsquery('russian', %(q)s))
                               AS score
                    FROM memory.tasks
                    {fwhere}
                    ORDER BY score DESC, created_at DESC
                    LIMIT %(k)s
                """, params2)
                rows = await cur.fetchall()
            return {
                "mode": "fts",
                "items": [_item(r, mode="fts") for r in rows][:top_k],
            }

        # 3) ILIKE как последний рубеж
        like = f"%{query.replace(chr(92), chr(92) * 2)
                        .replace('%', chr(92) + '%')
                        .replace('_', chr(92) + '_')}%"
        params3 = {"like": like, **params}
        iconds = conds + ["text ILIKE %(like)s"]
        iwhere = "WHERE " + " AND ".join(iconds)
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(f"""
                SELECT id::text, kind, text, session_id, plan_id,
                       meta, created_at, 0.0 AS score
                FROM memory.tasks
                {iwhere}
                ORDER BY created_at DESC
                LIMIT %(k)s
            """, params3)
            rows = await cur.fetchall()
        return {
            "mode": "ilike",
            "items": [_item(r, mode="ilike") for r in rows][:top_k],
        }

    # ─── Чтение/статистика ─────────────────────────────────────────
    async def recent(self, limit: int = 20, kind: str | None = None) -> list[dict]:
        limit = max(1, min(int(limit), 200))
        async with self.pool.connection() as conn, conn.cursor() as cur:
            if kind:
                await cur.execute("""
                    SELECT id::text, kind, text, session_id, plan_id, meta,
                           created_at
                    FROM memory.tasks WHERE kind = %s
                    ORDER BY created_at DESC LIMIT %s
                """, (kind, limit))
            else:
                await cur.execute("""
                    SELECT id::text, kind, text, session_id, plan_id, meta,
                           created_at
                    FROM memory.tasks
                    ORDER BY created_at DESC LIMIT %s
                """, (limit,))
            rows = await cur.fetchall()
        return [
            {
                "id": r["id"], "kind": r["kind"], "text": r["text"],
                "session_id": r["session_id"], "plan_id": r["plan_id"],
                "meta": r.get("meta") or {},
                "created_at": str(r["created_at"]),
            }
            for r in rows
        ]

    async def stats(self) -> dict:
        caps = await self._capabilities()
        async with self.pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT count(*)::int AS c FROM memory.tasks")
            total = (await cur.fetchone())["c"]
            await cur.execute("""
                SELECT kind, count(*)::int AS c FROM memory.tasks
                GROUP BY kind ORDER BY c DESC
            """)
            by_kind = {r["kind"]: r["c"] for r in await cur.fetchall()}
            embedded = 0
            if caps["vector"]:
                await cur.execute(
                    "SELECT count(*)::int AS c FROM memory.tasks "
                    "WHERE embedding IS NOT NULL")
                embedded = (await cur.fetchone())["c"]
        return {
            "total": total,
            "by_kind": by_kind,
            "embedded": embedded,
            "pgvector": caps["vector"],
            "fts": caps["tsv"],
            "embedder": self.embedder.name,
            "dim": AGENT_MEMORY_DIM,
        }

    async def clear(self, kind: str | None = None) -> int:
        async with self.pool.connection() as conn, conn.cursor() as cur:
            if kind:
                await cur.execute(
                    "DELETE FROM memory.tasks WHERE kind = %s", (kind,))
            else:
                await cur.execute("DELETE FROM memory.tasks")
            return cur.rowcount

    # ─── Наполнение из зеркал ──────────────────────────────────────
    async def backfill_from_mirrors(
        self,
        limit: int = 200,
        *,
        chat: bool = True,
        plans: bool = True,
        events: bool = True,
    ) -> dict:
        """Дотянуть новые строки из зеркал (идемпотентно по source_key).

        chat  : ops.chat_messages      → kind='chat'  (msg:<sid>:<seq>)
        plans : ops.plans (query)      → kind='plan'  (plan:<id>)
        events: journal.events_mirror с ошибкой → kind='event'
                (event:<uid>, текст = tool + error)
        """
        limit = max(1, min(int(limit), 2000))
        result = {"chat": 0, "plan": 0, "event": 0, "skipped": 0}
        async with self.pool.connection() as conn, conn.cursor() as cur:
            if chat:
                await cur.execute("""
                    SELECT m.session_id, m.seq, m.role, m.content
                    FROM ops.chat_messages m
                    WHERE m.content <> ''
                      AND NOT EXISTS (
                        SELECT 1 FROM memory.tasks t
                        WHERE t.kind = 'chat'
                          AND t.source_key =
                              'msg:' || m.session_id || ':' || m.seq)
                    ORDER BY m.ts DESC NULLS LAST
                    LIMIT %s
                """, (limit,))
                rows = await cur.fetchall()
                for r in rows:
                    await self.upsert(
                        r["content"], kind="chat",
                        source_key=f"msg:{r['session_id']}:{r['seq']}",
                        session_id=r["session_id"],
                        meta={"role": r["role"]},
                    )
                    result["chat"] += 1
            if plans:
                await cur.execute("""
                    SELECT p.plan_id, p.session_id, p.query, p.intent
                    FROM ops.plans p
                    WHERE p.query <> ''
                      AND NOT EXISTS (
                        SELECT 1 FROM memory.tasks t
                        WHERE t.kind = 'plan'
                          AND t.source_key = 'plan:' || p.plan_id)
                    ORDER BY p.updated_at DESC
                    LIMIT %s
                """, (limit,))
                rows = await cur.fetchall()
                for r in rows:
                    await self.upsert(
                        r["query"], kind="plan",
                        source_key=f"plan:{r['plan_id']}",
                        session_id=r["session_id"], plan_id=r["plan_id"],
                        meta={"intent": r["intent"]},
                    )
                    result["plan"] += 1
            if events:
                # только события с ошибками — остальное шум для памяти
                await cur.execute("""
                    SELECT e.event_uid, e.session_id, e.tool_name,
                           e.server_name, e.error
                    FROM journal.events_mirror e
                    WHERE coalesce(e.error, '') <> ''
                      AND NOT EXISTS (
                        SELECT 1 FROM memory.tasks t
                        WHERE t.kind = 'event'
                          AND t.source_key = 'event:' || e.event_uid)
                    ORDER BY e.ts DESC
                    LIMIT %s
                """, (limit,))
                rows = await cur.fetchall()
                for r in rows:
                    tool = r["tool_name"] or r["server_name"] or "tool"
                    text = f"Ошибка {tool}: {r['error']}"
                    await self.upsert(
                        text, kind="event",
                        source_key=f"event:{r['event_uid']}",
                        session_id=r["session_id"],
                        meta={"tool": r["tool_name"],
                              "server": r["server_name"]},
                    )
                    result["event"] += 1
        return result


class AgentMemoryIndexer:
    """Фоновый индексатор: периодически дотягивает зеркала в память."""

    def __init__(
        self,
        memory: AgentMemory,
        interval: float = 120.0,
        batch: int = 100,
        cooldown: float = 60.0,
    ):
        self.memory = memory
        self.interval = max(30.0, float(interval))
        self.batch = batch
        self.cooldown = cooldown
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self.last_result: dict = {}
        self.last_ok_ts: float = 0.0
        self.cycles: int = 0

    async def run_once(self) -> dict:
        t0 = time.time()
        try:
            res = await self.memory.backfill_from_mirrors(limit=self.batch)
            done = sum(res.get(k, 0) for k in ("chat", "plan", "event"))
            self.last_result = {**res, "seconds": round(time.time() - t0, 2)}
            self.last_ok_ts = time.time()
            self.cycles += 1
            logger.info("AgentMemory: проиндексировано %s", res)
            return self.last_result
        except Exception as e:
            self.last_result = {"error": str(e)[:200]}
            logger.warning("AgentMemory indexer: %s", e)
            return self.last_result

    async def _loop(self) -> None:
        while not self._stop.is_set():
            await self.run_once()
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    def status(self) -> dict:
        return {
            "running": self._task is not None and not self._task.done(),
            "interval": self.interval,
            "batch": self.batch,
            "cycles": self.cycles,
            "last": self.last_result,
        }


def _item(r: dict, mode: str) -> dict:
    score = r.get("score")
    return {
        "id": r["id"],
        "kind": r["kind"],
        "text": r["text"],
        "session_id": r.get("session_id") or "",
        "plan_id": r.get("plan_id") or "",
        "meta": r.get("meta") or {},
        "created_at": str(r["created_at"]),
        "score": round(float(score or 0), 4),
        "mode": mode,
    }


def _js(obj: Any) -> str:
    import json
    return json.dumps(obj, ensure_ascii=False, default=str)
