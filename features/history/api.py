"""API фичи «История» — чтение долговременных зеркал PostgreSQL.

Контракт: create_router() -> fastapi.APIRouter. FeatureLoader вызывает
его и делает app.include_router() — без правок src/main.py.

Источники данных (наполняет PgReplicator, схема db/ops.sql):
  * ops.chat_sessions / ops.chat_messages — зеркало диалогов чата;
  * ops.plans                              — зеркало планов Supervisor;
  * journal.events_mirror                  — полный поток событий журнала.

Фича ТОЛЬКО читает: горячий путь чата и репликатор не затрагиваются.
DSN — тот же, что у репликатора: settings.postgres.dsn() (DATABASE_URL
/ PG_APP_*). PG недоступен → 503 с человеческим объяснением.
Полнотекстовый поиск использует FTS-колонки tsv (ops.sql); если их нет
(старая схема) — мягкий откат на ILIKE.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)

try:
    import psycopg
except Exception:                       # фича монтируется, но требует PG
    psycopg = None

try:
    from src.db.pool import DatabasePool
except Exception:
    DatabasePool = None

# Ленивый пул чтения (изоляция от репликатора и Memory)
_POOL: Any = None
_POOL_LOCK: asyncio.Lock | None = None
_FTS: bool | None = None                # кэш наличия FTS-колонок


def _dsn() -> str:
    from src.config import get_settings
    return get_settings().postgres.dsn()


async def _db() -> Any:
    """Пул соединений или None, если PG недоступен."""
    global _POOL, _POOL_LOCK
    if psycopg is None or DatabasePool is None:
        return None
    if _POOL is not None:
        return _POOL
    if _POOL_LOCK is None:
        _POOL_LOCK = asyncio.Lock()
    async with _POOL_LOCK:
        if _POOL is None:
            # Быстрый pre-check: при лежащем PG не висим 30 секунд
            try:
                conn = await psycopg.AsyncConnection.connect(
                    _dsn(), connect_timeout=3)
                await conn.close()
            except Exception as e:
                logger.info("history: PG недоступен: %s", e)
                return None
            pool = DatabasePool(_dsn(), min_size=1, max_size=4)
            await pool.start()
            _POOL = pool
    return _POOL


def _clamp(value: int | None, default: int, mx: int = 500) -> int:
    try:
        v = int(value) if value is not None else default
    except (TypeError, ValueError):
        return default
    return max(1, min(v, mx))


def _like(q: str) -> str:
    """Шаблон для ILIKE с экранированием %, _ и \\."""
    esc = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


async def _has_fts(pool: Any) -> bool:
    """Есть ли FTS-колонки tsv (кэшируется на жизнь процесса)."""
    global _FTS
    if _FTS is not None:
        return _FTS
    try:
        rows = await pool.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = 'ops' AND table_name = 'chat_messages' "
            "AND column_name = 'tsv' LIMIT 1")
        _FTS = bool(rows)
    except Exception:
        _FTS = False
    return _FTS


def _reset_cache() -> None:             # для тестов
    global _POOL, _FTS
    _POOL = None
    _FTS = None


def _fmt_ts(value: Any) -> str:
    if value is None:
        return ""
    try:
        return value.isoformat(sep=" ", timespec="seconds")
    except Exception:
        return str(value)


# ═══════════════════════════════════════════════════════════════════
# Роутер
# ═══════════════════════════════════════════════════════════════════
def create_router():
    from fastapi import APIRouter, HTTPException, Query
    from fastapi.responses import Response

    router = APIRouter(prefix="/api/ops", tags=["feature:history"])

    def _down(detail: str = "PostgreSQL недоступен") -> HTTPException:
        return HTTPException(
            503,
            f"{detail}. Зеркала наполняет PgReplicator — проверьте "
            f"GET /api/db/status и db/ops.sql (scripts/init_db.py).",
        )

    # ── Статус и счётчики зеркал ────────────────────────────────────
    @router.get("/status")
    async def ops_status():
        pool = await _db()
        if pool is None:
            return {
                "pg": {"available": False},
                "counts": {},
                "fts": {"chat": False, "events": False},
                "hint": "PostgreSQL недоступен — зеркала не читаются",
            }
        try:
            counts: dict = {}
            for name, table in (
                ("sessions", "ops.chat_sessions"),
                ("messages", "ops.chat_messages"),
                ("plans", "ops.plans"),
                ("events", "journal.events_mirror"),
            ):
                try:
                    row = await pool.execute_one(
                        f"SELECT count(*)::int AS n FROM {table}")
                    counts[name] = (row or {}).get("n", 0)
                except Exception:
                    counts[name] = None          # таблицы нет (старая схема)
            fts_chat = await _has_fts(pool)
            fts_events = False
            try:
                r = await pool.execute_one(
                    "SELECT 1 FROM information_schema.columns "
                    "WHERE table_schema='journal' "
                    "AND table_name='events_mirror' "
                    "AND column_name='tsv' LIMIT 1")
                fts_events = bool(r)
            except Exception:
                pass
            from src.config import get_settings
            dsn = _dsn()
            return {
                "pg": {"available": True, "dsn": dsn.split("@")[-1]},
                "counts": counts,
                "fts": {"chat": fts_chat, "events": fts_events},
                "settings_enabled": bool(get_settings().use_postgres),
            }
        except Exception as e:
            raise _down(f"PostgreSQL недоступен ({e.__class__.__name__})")

    # ── Список сессий чата ──────────────────────────────────────────
    @router.get("/sessions")
    async def sessions_list(
        limit: int | None = Query(None, ge=1),
        offset: int = 0,
        q: str = "",
    ):
        limit = _clamp(limit, 50)
        pool = await _db()
        if pool is None:
            raise _down()
        try:
            params: list = []
            where = ""
            if q.strip():
                where = (" WHERE session_id IN (SELECT session_id FROM "
                         "ops.chat_messages WHERE content ILIKE %s)")
                params.append(_like(q.strip()))
            params.extend([limit, int(max(0, offset))])
            rows = await pool.execute(
                "SELECT s.session_id, s.first_seen, s.last_seen, "
                "       s.message_count, s.last_role, "
                "       last_msg.content AS preview "
                "FROM ops.chat_sessions s "
                "LEFT JOIN LATERAL ( "
                "    SELECT content FROM ops.chat_messages m "
                "    WHERE m.session_id = s.session_id "
                "    ORDER BY m.seq DESC LIMIT 1) last_msg ON true"
                + where +
                " ORDER BY s.last_seen DESC LIMIT %s OFFSET %s",
                tuple(params),
            )
            for r in rows:
                r["first_seen"] = _fmt_ts(r.get("first_seen"))
                r["last_seen"] = _fmt_ts(r.get("last_seen"))
                preview = r.get("preview") or ""
                r["preview"] = preview[:160]
            return {"items": rows, "limit": limit, "offset": offset}
        except Exception as e:
            raise _down(f"PostgreSQL недоступен ({e.__class__.__name__})")

    # ── Сообщения сессии ────────────────────────────────────────────
    @router.get("/sessions/{session_id}/messages")
    async def session_messages(session_id: str,
                               limit: int | None = Query(None, ge=1),
                               offset: int = 0):
        limit = _clamp(limit, 200)
        pool = await _db()
        if pool is None:
            raise _down()
        try:
            rows = await pool.execute(
                "SELECT session_id, seq, role, agent, content, ts, "
                "       ingested_at "
                "FROM ops.chat_messages WHERE session_id = %s "
                "ORDER BY seq ASC LIMIT %s OFFSET %s",
                (session_id, limit, int(max(0, offset))),
            )
            for r in rows:
                r["ts"] = _fmt_ts(r.get("ts"))
            meta = await pool.execute_one(
                "SELECT first_seen, last_seen, message_count "
                "FROM ops.chat_sessions WHERE session_id = %s",
                (session_id,),
            )
            if meta:
                meta["first_seen"] = _fmt_ts(meta.get("first_seen"))
                meta["last_seen"] = _fmt_ts(meta.get("last_seen"))
            return {"session": meta or {}, "items": rows,
                    "limit": limit, "offset": offset}
        except Exception as e:
            raise _down(f"PostgreSQL недоступен ({e.__class__.__name__})")

    # ── Экспорт сессии в Markdown ───────────────────────────────────
    @router.get("/sessions/{session_id}/export")
    async def session_export(session_id: str):
        pool = await _db()
        if pool is None:
            raise _down()
        try:
            rows = await pool.execute(
                "SELECT seq, role, agent, content, ts "
                "FROM ops.chat_messages WHERE session_id = %s "
                "ORDER BY seq ASC", (session_id,))
            if not rows:
                raise HTTPException(404, "Сессия не найдена в зеркале")
            lines = [f"# Сессия {session_id}",
                     "",
                     f"Сообщений: {len(rows)}", ""]
            for r in rows:
                who = r.get("role") or "?"
                agent = r.get("agent") or ""
                when = _fmt_ts(r.get("ts")) or "—"
                head = f"## {when} — {who}"
                if agent:
                    head += f" ({agent})"
                lines += [head, "", r.get("content") or "", ""]
            md = "\n".join(lines)
            fname = f"session-{session_id[:24]}.md"
            return Response(
                content=md,
                media_type="text/markdown; charset=utf-8",
                headers={"Content-Disposition":
                         f'attachment; filename="{fname}"'},
            )
        except HTTPException:
            raise
        except Exception as e:
            raise _down(f"PostgreSQL недоступен ({e.__class__.__name__})")

    # ── Планы Supervisor ────────────────────────────────────────────
    @router.get("/plans")
    async def plans_list(session_id: str = "", status: str = "",
                         limit: int | None = Query(None, ge=1),
                         offset: int = 0):
        limit = _clamp(limit, 50)
        pool = await _db()
        if pool is None:
            raise _down()
        try:
            conds, params = [], []
            if session_id.strip():
                conds.append("session_id = %s")
                params.append(session_id.strip())
            if status.strip():
                conds.append("status = %s")
                params.append(status.strip())
            where = (" WHERE " + " AND ".join(conds)) if conds else ""
            params.extend([limit, int(max(0, offset))])
            rows = await pool.execute(
                "SELECT plan_id, session_id, query, intent, mode, status, "
                "       needs_approval, success, replans, steps_total, "
                "       steps_done, steps_failed, created_at, updated_at "
                "FROM ops.plans" + where +
                " ORDER BY updated_at DESC NULLS LAST LIMIT %s OFFSET %s",
                tuple(params))
            for r in rows:
                r["created_at"] = _fmt_ts(r.get("created_at"))
                r["updated_at"] = _fmt_ts(r.get("updated_at"))
                r["query"] = (r.get("query") or "")[:200]
            return {"items": rows, "limit": limit, "offset": offset}
        except Exception as e:
            raise _down(f"PostgreSQL недоступен ({e.__class__.__name__})")

    @router.get("/plans/{plan_id}")
    async def plan_detail(plan_id: str):
        pool = await _db()
        if pool is None:
            raise _down()
        try:
            row = await pool.execute_one(
                "SELECT * FROM ops.plans WHERE plan_id = %s", (plan_id,))
            if not row:
                raise HTTPException(404, "План не найден в зеркале")
            row["created_at"] = _fmt_ts(row.get("created_at"))
            row["updated_at"] = _fmt_ts(row.get("updated_at"))
            row["ingested_at"] = _fmt_ts(row.get("ingested_at"))
            return row
        except HTTPException:
            raise
        except Exception as e:
            raise _down(f"PostgreSQL недоступен ({e.__class__.__name__})")

    # ── События журнала (events_mirror) ─────────────────────────────
    @router.get("/events")
    async def events_list(kind: str = "", tool: str = "",
                          session_id: str = "", trace_id: str = "",
                          limit: int | None = Query(None, ge=1),
                          offset: int = 0):
        limit = _clamp(limit, 100)
        pool = await _db()
        if pool is None:
            raise _down()
        try:
            conds, params = [], []
            if kind.strip():
                conds.append("kind = %s")
                params.append(kind.strip())
            if tool.strip():
                conds.append("tool_name = %s")
                params.append(tool.strip())
            if session_id.strip():
                conds.append("session_id = %s")
                params.append(session_id.strip())
            if trace_id.strip():
                conds.append("trace_id = %s")
                params.append(trace_id.strip())
            where = (" WHERE " + " AND ".join(conds)) if conds else ""
            params.extend([limit, int(max(0, offset))])
            rows = await pool.execute(
                "SELECT event_uid, seq, ts, kind, action, status, "
                "       session_id, task_id, trace_id, agent_id, "
                "       server_name, tool_name, duration_ms, error "
                "FROM journal.events_mirror" + where +
                " ORDER BY ts DESC LIMIT %s OFFSET %s",
                tuple(params))
            for r in rows:
                r["ts"] = _fmt_ts(r.get("ts"))
            return {"items": rows, "limit": limit, "offset": offset}
        except Exception as e:
            raise _down(f"PostgreSQL недоступен ({e.__class__.__name__})")

    # ── Полнотекстовый поиск по зеркалам ────────────────────────────
    @router.get("/search")
    async def search(q: str, limit: int | None = Query(None, ge=1)):
        q = (q or "").strip()
        if not q:
            raise HTTPException(422, "Пустой поисковый запрос")
        limit = _clamp(limit, 30)
        pool = await _db()
        if pool is None:
            raise _down()
        try:
            fts = await _has_fts(pool)
            out: dict = {"query": q, "mode": "fts" if fts else "ilike",
                         "chat": [], "events": []}

            if fts:
                chat_rows = await pool.execute(
                    "SELECT session_id, seq, role, content, ts, "
                    "       ts_rank(tsv, websearch_to_tsquery('russian', "
                    " %s)) AS rank "
                    "FROM ops.chat_messages "
                    "WHERE tsv @@ websearch_to_tsquery('russian', %s) "
                    "ORDER BY rank DESC, ts DESC LIMIT %s",
                    (q, q, limit))
            else:
                chat_rows = await pool.execute(
                    "SELECT session_id, seq, role, content, ts "
                    "FROM ops.chat_messages WHERE content ILIKE %s "
                    "ORDER BY ts DESC LIMIT %s", (_like(q), limit))
            for r in chat_rows:
                r["ts"] = _fmt_ts(r.get("ts"))
                r.pop("rank", None)
                r["snippet"] = (r.get("content") or "")[:220]
            out["chat"] = chat_rows

            try:
                if fts:
                    ev_rows = await pool.execute(
                        "SELECT event_uid, ts, kind, status, session_id, "
                        "       trace_id, server_name, tool_name, error, "
                        "       ts_rank(tsv, websearch_to_tsquery("
                        "'russian', %s)) AS rank "
                        "FROM journal.events_mirror "
                        "WHERE tsv @@ websearch_to_tsquery('russian', %s) "
                        "ORDER BY rank DESC, ts DESC LIMIT %s",
                        (q, q, limit))
                else:
                    ev_rows = await pool.execute(
                        "SELECT event_uid, ts, kind, status, session_id, "
                        "       trace_id, server_name, tool_name, error "
                        "FROM journal.events_mirror "
                        "WHERE error ILIKE %s OR tool_name ILIKE %s "
                        "   OR server_name ILIKE %s OR kind ILIKE %s "
                        "ORDER BY ts DESC LIMIT %s",
                        (_like(q), _like(q), _like(q), _like(q), limit))
                for r in ev_rows:
                    r["ts"] = _fmt_ts(r.get("ts"))
                    r.pop("rank", None)
                out["events"] = ev_rows
            except Exception:
                # events_mirror может отсутствовать в старой схеме — это
                # не ошибка поиска по чату
                pass
            return out
        except HTTPException:
            raise
        except Exception as e:
            raise _down(f"PostgreSQL недоступен ({e.__class__.__name__})")

    return router
