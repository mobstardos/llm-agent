"""Фоновая репликатор локальных данных → PostgreSQL («чтоб ничего не
потерялось»).

Что реплицируется (источник правды остаётся локальным):
  1. Журнал операций  data/journal/journal.sqlite (таблица events) →
     journal.events_mirror (полное зеркало 1:1) + journal.actions
     (аналитическая проекция tool_call-событий, db/journal.sql).
  2. Диалоги чата     data/sessions/<id>.jsonl →
     ops.chat_sessions + ops.chat_messages.
  3. Планы Supervisor data/plans/<plan_id>.json → ops.plans.

Гарантии долговечности:
  - курсоры хранятся в PG (ops.sync_state) и продвигаются ТОЛЬКО после
    успешного коммита партии — сбой/простой БД не теряет записи:
    после восстановления репликация продолжается с места остановки;
  - вставки идемпотентны (PK / ON CONFLICT DO NOTHING) — двойная
    загрузка невозможна даже при ручном сбросе курсора;
  - репликатор НИКОГДА не бросает исключения наружу и не блокирует
    чат: все ошибки гасятся в счётчиках и логе;
  - ноль правок горячего пути чата (хвостит файлы и SQLite по курсорам).

Запуск (обычно из main.lifespan):
    rep = PgReplicator(project_root=...)   # DSN из settings/env
    await rep.start()
    ...
    await rep.stop()

Отдельный прогон цикла: await rep.cycle()
Состояние: rep.status() → dict (эндпоинт GET /api/db/status).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_MESSAGE_CHARS = 100_000      # усечение сообщения чата при репликации
MAX_BATCH = 500                  # событий журнала за один цикл
PG_DOWN_COOLDOWN = 60.0          # сек между попытками переподключения к PG


def _js(value: Any, fallback: str = "{}") -> str:
    """JSON-строка для ::jsonb (никогда не бросает)."""
    if value is None:
        return fallback if fallback.startswith(("{", "[")) else "null"
    if isinstance(value, str):
        return value if value.strip() else fallback
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        return fallback


def _to_uuid(s: str) -> str | None:
    """Строка → UUID, если похоже; иначе None (колонка UUID в PG)."""
    if not s:
        return None
    try:
        return str(uuid.UUID(str(s)))
    except Exception:
        return None


def classify_action(server: str, tool: str) -> str:
    """Категория для journal.actions (read|write|patch|delete|move|query|external|admin)."""
    name = f"{server}.{tool}".lower()
    if "delete" in name or name.endswith(".rm"):
        return "delete"
    if "patch" in name:
        return "patch"
    if "move" in name or "rename" in name:
        return "move"
    if server in ("mysql", "postgres", "onec", "onec_query", "db_extended",
                  "kubernetes", "1c"):
        return "query"
    if server in ("shell", "cicd", "build", "debug", "lsp"):
        return "admin"
    if server in ("http", "browser", "media", "image", "deepseek", "github"):
        return "external"
    if any(k in name for k in ("read", "open", "list", "search", "find",
                               "get", "info", "status", "health")):
        return "read"
    if any(k in name for k in ("write", "create", "edit", "mkdir", "copy",
                               "commit", "push")):
        return "write"
    return "external"


class PgReplicator:
    """Периодически переносит новые записи локальных хранилищ в PostgreSQL."""

    def __init__(
        self,
        *,
        dsn: str = "",
        project_root: str | Path = "",
        journal_dir: str | Path = "",
        sessions_dir: str | Path = "",
        plans_dir: str | Path = "",
        interval_seconds: float | None = None,
        batch: int = MAX_BATCH,
    ):
        root = Path(project_root) if project_root else Path.cwd()
        self.journal_dir = Path(journal_dir) if journal_dir else root / "data" / "journal"
        self.sessions_dir = Path(sessions_dir) if sessions_dir else root / "data" / "sessions"
        self.plans_dir = Path(plans_dir) if plans_dir else root / "data" / "plans"
        self.interval = float(
            interval_seconds
            if interval_seconds is not None
            else os.getenv("PG_REPLICATE_INTERVAL", "15")
        )
        self.batch = max(1, int(batch))
        self._dsn = dsn
        self._pool = None                  # DatabasePool (lazy)
        self._pg_down_until = 0.0          # cooldown повторных подключений
        self._pg_fail_streak = 0           # серия неудач подряд (для backoff)
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()

        # Счётчики для /api/db/status
        self.counters: dict[str, Any] = {
            "cycles": 0,
            "pg_ok": False,
            "journal_events": 0,        # перенесено событий журнала
            "journal_actions": 0,       # проекций в journal.actions
            "sessions": 0,              # затронуто сессий
            "messages": 0,              # перенесено сообщений
            "plans": 0,                 # обновлено планов
            "errors": 0,
            "last_error": "",
            "last_cycle_ts": 0.0,
            "last_cycle_seconds": 0.0,
        }

    # ═══════════════════════════════════════════════════════
    # Жизненный цикл
    # ═══════════════════════════════════════════════════════
    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="pg-replicator")
            logger.info(
                "PgReplicator: запущен (интервал %.0fs, журнал=%s, сессии=%s, планы=%s)",
                self.interval, self.journal_dir, self.sessions_dir, self.plans_dir,
            )

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        if self._pool is not None:
            try:
                await self._pool.stop()
            except Exception:
                pass
            self._pool = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.cycle()
            except Exception as e:                      # никогда не падаем
                self._err(e, "cycle")
            try:
                await asyncio.wait_for(self._stop.wait(),
                                       timeout=max(2.0, self.interval))
            except asyncio.TimeoutError:
                pass

    # ═══════════════════════════════════════════════════════
    # Пул / соединение
    # ═══════════════════════════════════════════════════════
    async def _get_pool(self):
        import asyncio as _aio
        now = time.monotonic()
        if now < self._pg_down_until:
            return None
        if self._pool is not None:
            return self._pool
        try:
            from src.db.pool import DatabasePool
            if not self._dsn:
                from src.config import get_settings
                self._dsn = get_settings().postgres.dsn()
            pool = DatabasePool(self._dsn, min_size=1, max_size=4)
            # жёсткий таймаут на подключение, чтобы не висеть 30с в цикле
            await _aio.wait_for(pool.start(), timeout=10.0)
            self._pool = pool
            self._pg_fail_streak = 0
            self.counters["pg_ok"] = True
            logger.info("PgReplicator: PostgreSQL доступен")
            return pool
        except Exception as e:
            self._pg_fail_streak += 1
            # серия неудач → будим сеть реже: 60с → 300с
            # (пул теперь fail-fast: TCP-пробник ~2с, без спама psycopg)
            cooldown = PG_DOWN_COOLDOWN * (
                5 if self._pg_fail_streak >= 3 else 1
            )
            self._pg_down_until = now + cooldown
            self.counters["pg_ok"] = False
            self._err(e, "подключение PostgreSQL")
            return None

    def _err(self, e: Exception, where: str) -> None:
        # ленивый импорт: psycopg не обязателен для репликатора
        from src.db.pool import _fmt_exc
        reason = _fmt_exc(e)
        self.counters["errors"] += 1
        self.counters["last_error"] = f"{where}: {reason}"
        logger.warning("PgReplicator: %s: %s", where, reason[:200])

    # ═══════════════════════════════════════════════════════
    # Один цикл репликации
    # ═══════════════════════════════════════════════════════
    async def cycle(self) -> dict:
        """Один проход по всем источникам. Возвращает снапшот счётчиков."""
        t0 = time.monotonic()
        pool = await self._get_pool()
        if pool is None:
            self.counters["last_cycle_ts"] = time.time()
            return dict(self.counters)
        try:
            await self._sync_journal(pool)
        except Exception as e:
            self._err(e, "journal")
        try:
            await self._sync_sessions(pool)
        except Exception as e:
            self._err(e, "sessions")
        try:
            await self._sync_plans(pool)
        except Exception as e:
            self._err(e, "plans")

        self.counters["cycles"] += 1
        self.counters["last_cycle_ts"] = time.time()
        self.counters["last_cycle_seconds"] = round(time.monotonic() - t0, 3)
        return dict(self.counters)

    # ═══════════════════════════════════════════════════════
    # Курсоры (ops.sync_state)
    # ═══════════════════════════════════════════════════════
    async def _cursor_get(self, conn, key: str, default: dict) -> dict:
        async with conn.cursor() as cur:
            await cur.execute(
                "SELECT value FROM ops.sync_state WHERE key = %s", (key,),
            )
            row = await cur.fetchone()
        if row and row.get("value"):
            try:
                return dict(row["value"])
            except Exception:
                return default
        return default

    async def _cursor_set(self, conn, key: str, value: dict) -> None:
        async with conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO ops.sync_state (key, value, updated_at) "
                "VALUES (%s, %s::jsonb, now()) "
                "ON CONFLICT (key) DO UPDATE SET "
                "value = EXCLUDED.value, updated_at = now()",
                (key, _js(value)),
            )

    # ═══════════════════════════════════════════════════════
    # 1. Журнал (SQLite events → journal.events_mirror + journal.actions)
    # ═══════════════════════════════════════════════════════
    _MIRROR_SQL = """
        INSERT INTO journal.events_mirror (
            event_uid, seq, ts, iso_time, kind, action, status,
            session_id, task_id, trace_id, agent_id, loop_id, iteration,
            server_name, tool_name, targets, args, args_digest,
            before_hash, after_hash, shadow_dir, reversible, inverse,
            parent_id, depends_on, duration_ms, bytes_before, bytes_after,
            error, meta, payload_path)
        VALUES (
            %s,%s, to_timestamp(%s), %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s,
            %s, %s, %s::jsonb, %s::jsonb, %s,
            %s, %s, %s, %s, %s::jsonb,
            %s, %s::jsonb, %s, %s, %s,
            %s, %s::jsonb, %s)
        ON CONFLICT (event_uid) DO NOTHING
    """

    _ACTION_SQL = """
        INSERT INTO journal.actions (
            trace_id, session_id, agent, tool, args, result_summary,
            result_hash, success, error, duration_ms, category,
            reversible, inverse_op, affects_files, event_uid)
        VALUES (
            %s, %s, %s, %s, %s::jsonb, %s,
            %s, %s, %s, %s, %s,
            %s, %s::jsonb, %s, %s)
        -- ON CONFLICT невозможен: таблица партиционирована (PK включает
        -- created_at). Дубли отбрасываются заранее — см. _insert_journal_rows.
    """

    async def _sync_journal(self, pool) -> None:
        db = self.journal_dir / "journal.sqlite"
        if not db.exists():
            return
        async with pool.connection() as conn:
            cur_state = await self._cursor_get(conn, "journal", {"last_id": 0})
            rows = await asyncio.to_thread(
                self._journal_read, db, int(cur_state.get("last_id", 0)),
            )
            if not rows:
                return
            await self._insert_journal_rows(conn, rows)

    async def _insert_journal_rows(self, conn, rows: list[dict]) -> None:
        """Вставляет партию событий + курсор в ОДНОЙ транзакции."""
        mirror_rows: list[tuple] = []
        action_rows: list[tuple] = []
        for r in rows:
            tool_full = (
                f"{r['server_name']}__{r['tool_name']}"
                if r["server_name"] and r["tool_name"] else
                (r["tool_name"] or r["action"] or r["kind"] or "")
            )
            mirror_rows.append((
                r["event_id"], r["seq"], r["ts"], r["iso_time"], r["kind"],
                r["action"], r["status"], r["session_id"], r["task_id"],
                r["trace_id"], r["agent_id"], r["loop_id"], r["iteration"],
                r["server_name"], r["tool_name"],
                _js(_loads(r["targets"], []), "[]"),
                _js(_loads(r["args_json"], None), "null") if r["args_json"] else None,
                r["args_digest"], r["before_hash"], r["after_hash"],
                r["shadow_dir"], r["reversible"], _js(_loads(r["inverse"], {})),
                r["parent_id"], _js(_loads(r["depends_on"], []), "[]"),
                r["duration_ms"], r["bytes_before"], r["bytes_after"],
                r["error"], _js(_loads(r["meta"], {})), r["payload_path"],
            ))
            # Аналитическая проекция: только реальные вызовы инструментов
            if r["kind"] == "tool_call":
                action_rows.append((
                    r["trace_id"], _to_uuid(r["session_id"]),
                    r["agent_id"] or r["server_name"], tool_full,
                    _js(_loads(r["args_json"], {})),
                    _js(_loads(r["meta"], {}))[:500],       # result_summary
                    r["after_hash"],                        # result_hash
                    r["status"] != "error",                 # success
                    r["error"], r["duration_ms"],
                    classify_action(r["server_name"], r["tool_name"]),
                    bool(r["reversible"]),                  # BOOLEAN в PG
                    _js(_loads(r["inverse"], {})),
                    _loads(r["targets"], []),
                    r["event_id"],
                ))
        inserted_actions = 0
        async with conn.transaction():
            async with conn.cursor() as cur:
                if mirror_rows:
                    await cur.executemany(self._MIRROR_SQL, mirror_rows)
                if action_rows:
                    # Партиционированная таблица без глобального UNIQUE:
                    # дубли отбрасываем запросом (идемпотентно и при ручном
                    # сбросе курсора)
                    uids = [r[14] for r in action_rows]
                    await cur.execute(
                        "SELECT event_uid FROM journal.actions "
                        "WHERE event_uid = ANY(%s)", (uids,),
                    )
                    existing = {
                        r["event_uid"] for r in (await cur.fetchall())
                        if r.get("event_uid")
                    }
                    fresh = [r for r in action_rows if r[14] not in existing]
                    if fresh:
                        await cur.executemany(self._ACTION_SQL, fresh)
                        inserted_actions = len(fresh)
                await self._cursor_set(
                    conn, "journal", {"last_id": rows[-1]["id"]},
                )
        self.counters["journal_events"] += len(mirror_rows)
        self.counters["journal_actions"] += inserted_actions
        logger.debug("PgReplicator: журнал +%d событий", len(mirror_rows))

    def _journal_read(self, db: Path, last_id: int = 0) -> list[dict]:
        """Читает новые события журнала (id > last_id). SQLite — read-only,
        выполняется в отдельном потоке (не блокирует event loop)."""
        uri = f"file:{db.as_posix()}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=10.0)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                "SELECT id, event_id, seq, ts, iso_time, kind, action, status, "
                "session_id, task_id, trace_id, agent_id, loop_id, iteration, "
                "server_name, tool_name, targets, args_json, args_digest, "
                "before_hash, after_hash, shadow_dir, reversible, inverse, "
                "parent_id, depends_on, duration_ms, bytes_before, bytes_after, "
                "error, meta, payload_path "
                "FROM events WHERE id > ? ORDER BY id LIMIT ?",
                (last_id, self.batch),
            ).fetchall()
            return [dict(r) for r in rows]
        finally:
            conn.close()

    # ═══════════════════════════════════════════════════════
    # 2. Диалоги чата (data/sessions/<id>.jsonl → ops.chat_*)
    # ═══════════════════════════════════════════════════════
    _MSG_SQL = """
        INSERT INTO ops.chat_messages (session_id, seq, role, agent, content, ts)
        VALUES (%s, %s, %s, %s, %s, to_timestamp(NULLIF(%s, 0)))
        ON CONFLICT (session_id, seq) DO NOTHING
    """

    _SESSION_SQL = """
        INSERT INTO ops.chat_sessions
            (session_id, first_seen, last_seen, message_count, last_role, meta)
        VALUES (%s, now(), now(), %s, %s, %s::jsonb)
        ON CONFLICT (session_id) DO UPDATE SET
            last_seen = now(),
            message_count = EXCLUDED.message_count,
            last_role = EXCLUDED.last_role,
            meta = EXCLUDED.meta
    """

    async def _sync_sessions(self, pool) -> None:
        if not self.sessions_dir.exists():
            return
        files = sorted(self.sessions_dir.glob("*.jsonl"))
        if not files:
            return
        async with pool.connection() as conn:
            for f in files:
                await self._sync_one_session(conn, f)

    async def _sync_one_session(self, conn, f: Path) -> None:
        key = f"session:{f.stem}"
        st = await self._cursor_get(conn, key, {"offset": 0, "seq": 0, "count": 0})
        try:
            size = f.stat().st_size
        except OSError:
            return
        offset = int(st.get("offset", 0))
        seq = int(st.get("seq", 0))
        count = int(st.get("count", 0))
        if size <= offset:
            return
        if size < offset:               # файл перезаписан/урезан — читаем заново
            offset, seq, count = 0, 0, 0

        new_offset, msgs, plan_records, last_role = await asyncio.to_thread(
            self._read_session_tail, f, offset,
        )
        if msgs:
            rows = []
            for m in msgs:
                seq += 1
                count += 1
                rows.append((
                    f.stem, seq, m["role"], m.get("agent", ""),
                    (m.get("content") or "")[:MAX_MESSAGE_CHARS],
                    float(m.get("ts") or 0),
                ))
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.executemany(self._MSG_SQL, rows)
                    await cur.execute(
                        self._SESSION_SQL,
                        (f.stem, count, last_role,
                         _js({"plan_records": plan_records})),
                    )
                    await self._cursor_set(
                        conn, key,
                        {"offset": new_offset, "seq": seq, "count": count},
                    )
            self.counters["messages"] += len(rows)
            self.counters["sessions"] += 1
        elif new_offset != offset:
            # в файле только план/шаги (без новых сообщений) — двигаем курсор
            async with conn.transaction():
                async with conn.cursor() as cur:
                    await cur.execute(
                        self._SESSION_SQL,
                        (f.stem, count, last_role,
                         _js({"plan_records": plan_records})),
                    )
                    await self._cursor_set(
                        conn, key,
                        {"offset": new_offset, "seq": seq, "count": count},
                    )
            self.counters["sessions"] += 1

    def _read_session_tail(self, f: Path, offset: int):
        """Читает хвост jsonl от offset. Возвращает (new_offset, msgs,
        plan_records, last_role).

        Гарантия офсета: new_offset никогда не превышает размер файла;
        последняя строка без '\\n' считается ЗАВЕРШЁННОЙ только если это
        валидный JSON (иначе ждём дозапись — offset не двигается)."""
        msgs: list[dict] = []
        plan_records = 0
        last_role = ""
        with open(f, "rb") as fh:
            fh.seek(offset)
            data = fh.read()
        pos = 0
        total = len(data)
        while pos < total:
            nl = data.find(b"\n", pos)
            if nl == -1:
                # хвост без '\\n': обработаем только если это валидный JSON
                tail = data[pos:].strip()
                if not tail:
                    break
                try:
                    rec = json.loads(tail.decode("utf-8"))
                except Exception:
                    break                      # ждём дозапись строки
                if isinstance(rec, dict):
                    if rec.get("role"):
                        msgs.append(rec)
                        last_role = str(rec.get("role", ""))
                    elif rec.get("type") in ("plan", "step"):
                        plan_records += 1
                pos = total                    # строго ≤ размера файла
                break
            line = data[pos:nl].strip()
            pos = nl + 1
            if not line:
                continue
            try:
                rec = json.loads(line.decode("utf-8"))
            except Exception:
                # битая строка — остановиться до её завершения
                pos -= (nl + 1 - (data.rfind(b"\n", 0, nl) + 1))
                break
            if isinstance(rec, dict):
                if rec.get("role"):
                    msgs.append(rec)
                    last_role = str(rec.get("role", ""))
                elif rec.get("type") in ("plan", "step"):
                    plan_records += 1
        return offset + pos, msgs, plan_records, last_role

    # ═══════════════════════════════════════════════════════
    # 3. Планы Supervisor (data/plans/<id>.json → ops.plans)
    # ═══════════════════════════════════════════════════════
    _PLAN_SQL = """
        INSERT INTO ops.plans (
            plan_id, session_id, query, intent, mode, status, needs_approval,
            success, replans, steps_total, steps_done, steps_failed,
            created_at, updated_at, payload)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                to_timestamp(NULLIF(%s, 0)), to_timestamp(NULLIF(%s, 0)),
                %s::jsonb)
        ON CONFLICT (plan_id) DO UPDATE SET
            status = EXCLUDED.status,
            needs_approval = EXCLUDED.needs_approval,
            success = EXCLUDED.success,
            replans = EXCLUDED.replans,
            steps_total = EXCLUDED.steps_total,
            steps_done = EXCLUDED.steps_done,
            steps_failed = EXCLUDED.steps_failed,
            updated_at = EXCLUDED.updated_at,
            payload = EXCLUDED.payload,
            ingested_at = now()
    """

    async def _sync_plans(self, pool) -> None:
        if not self.plans_dir.exists():
            return
        files = sorted(self.plans_dir.glob("*.json"))
        if not files:
            return
        async with pool.connection() as conn:
            for f in files:
                await self._sync_one_plan(conn, f)

    async def _sync_one_plan(self, conn, f: Path) -> None:
        key = f"plan:{f.stem}"
        try:
            stat = f.stat()
        except OSError:
            return
        sig = {"mtime": round(stat.st_mtime, 3), "size": stat.st_size}
        st = await self._cursor_get(conn, key, {})
        if st.get("mtime") == sig["mtime"] and st.get("size") == sig["size"]:
            return

        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            self._err(e, f"план {f.name}")
            return
        if not isinstance(data, dict):
            return
        steps = data.get("steps") or []
        done = sum(1 for s in steps if s.get("status") == "done")
        failed = sum(
            1 for s in steps
            if s.get("status") in ("error", "failed", "skipped")
        )
        success = data.get("success")
        success = None if success is None else bool(success)
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(self._PLAN_SQL, (
                    data.get("plan_id") or f.stem,
                    str(data.get("session_id") or ""),
                    str(data.get("query") or "")[:2000],
                    str(data.get("intent") or ""),
                    str(data.get("mode") or "sequential"),
                    str(data.get("status") or "running"),
                    bool(data.get("needs_approval")),
                    success,
                    int(data.get("replans") or 0),
                    len(steps), done, failed,
                    float(data.get("created_at") or 0),
                    float(data.get("updated_at") or 0),
                    _js(data),
                ))
                await self._cursor_set(conn, key, sig)
        self.counters["plans"] += 1

    # ═══════════════════════════════════════════════════════
    # Состояние
    # ═══════════════════════════════════════════════════════
    def status(self) -> dict:
        return dict(self.counters)


def _loads(raw: Any, default: Any):
    """JSON-строка SQLite → объект (или default)."""
    if raw is None or raw == "":
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return default
