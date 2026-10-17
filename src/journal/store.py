"""Хранилище журнала: SQLite (индекс, поиск, связи) + JSONL (полный поток).

SQLite работает в WAL-режиме — чтение не блокирует запись, журнал доступен
одновременно основному процессу и MCP-серверу journal (отдельный процесс).

Полнотекстовый поиск — FTS5; если сборка Python без FTS5, автоматический
откат на LIKE-поиск (ничего не ломается).

JSONL пишется по дням: data/journal/events/events-YYYYMMDD.jsonl.
Если запись слишком велика для JSONL — тело уходит в payloads/, остаётся ссылка.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

from .config import JournalConfig
from .schema import Event  # noqa: F401 (переиспользуется импортёрами)
from .utils import append_jsonl, sha256_dict, truncate, write_text_safe

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT UNIQUE NOT NULL,
    seq INTEGER NOT NULL,
    ts REAL NOT NULL,
    iso_time TEXT NOT NULL,
    kind TEXT NOT NULL,
    action TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'ok',
    session_id TEXT DEFAULT '',
    task_id TEXT DEFAULT '',
    trace_id TEXT DEFAULT '',
    agent_id TEXT DEFAULT '',
    loop_id TEXT DEFAULT '',
    iteration INTEGER DEFAULT -1,
    server_name TEXT DEFAULT '',
    tool_name TEXT DEFAULT '',
    targets TEXT DEFAULT '[]',
    args_json TEXT DEFAULT '',
    args_digest TEXT DEFAULT '',
    before_hash TEXT DEFAULT '',
    after_hash TEXT DEFAULT '',
    shadow_dir TEXT DEFAULT '',
    reversible INTEGER DEFAULT 0,
    inverse TEXT DEFAULT '{}',
    parent_id TEXT DEFAULT '',
    depends_on TEXT DEFAULT '[]',
    duration_ms REAL DEFAULT 0,
    bytes_before INTEGER DEFAULT -1,
    bytes_after INTEGER DEFAULT -1,
    error TEXT DEFAULT '',
    meta TEXT DEFAULT '{}',
    payload_path TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);
CREATE INDEX IF NOT EXISTS ix_events_kind ON events(kind);
CREATE INDEX IF NOT EXISTS ix_events_session ON events(session_id);
CREATE INDEX IF NOT EXISTS ix_events_task ON events(task_id);
CREATE INDEX IF NOT EXISTS ix_events_trace ON events(trace_id);
CREATE INDEX IF NOT EXISTS ix_events_tool ON events(server_name, tool_name);
CREATE INDEX IF NOT EXISTS ix_events_parent ON events(parent_id);
CREATE INDEX IF NOT EXISTS ix_events_target ON events(targets);
"""

_FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS journal_fts USING fts5(
    event_id UNINDEXED, action, targets, error, meta_text, tokenize='unicode61'
);
"""


class JournalStore:
    def __init__(self, cfg: JournalConfig):
        self.cfg = cfg
        self.cfg.base_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = cfg.db_path
        self._lock = threading.RLock()
        self._fts_ok = True
        self._conn = self._connect()
        self._init_schema()
        self._last_seq = self._load_last_seq()

    # ─── Подключение ─────────────────────────────────────────
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False, timeout=30.0,
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(_SCHEMA)
            try:
                self._conn.executescript(_FTS_SCHEMA)
                self._fts_ok = True
            except sqlite3.OperationalError:
                self._fts_ok = False
            self._conn.commit()

    def _load_last_seq(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT seq FROM events ORDER BY seq DESC LIMIT 1"
            ).fetchone()
            return int(row["seq"]) if row else 0

    # ─── Запись ──────────────────────────────────────────────
    def insert(self, ev: Event) -> Event:
        """Записывает событие в SQLite + JSONL. Возвращает событие (с seq)."""
        now = time.time()
        with self._lock:
            self._last_seq += 1
            ev.seq = self._last_seq
        if not ev.iso_time:
            ev.iso_time = time.strftime(
                "%Y-%m-%dT%H:%M:%S", time.localtime(ev.ts or now))
        ev.args_digest = sha256_dict(ev.args) if ev.args else ""

        # JSONL: полный поток по дням
        self._write_jsonl(ev)

        # SQLite: индекс без тяжёлых полей
        row = ev.to_row()
        placeholders = ",".join("?" * len(row))
        cols = ",".join(row.keys())
        try:
            with self._lock:
                self._conn.execute(
                    f"INSERT OR REPLACE INTO events ({cols}) VALUES ({placeholders})",
                    list(row.values()),
                )
                if self._fts_ok:
                    self._conn.execute(
                        "INSERT INTO journal_fts (event_id, action, targets, error, meta_text) "
                        "VALUES (?,?,?,?,?)",
                        (
                            ev.event_id, ev.action,
                            " ".join(ev.targets)[:2000],
                            ev.error[:1000],
                            json.dumps(ev.meta, ensure_ascii=False, default=str)[:2000],
                        ),
                    )
                self._conn.commit()
        except sqlite3.Error as e:
            # SQLite упал — JSONL уже сохранил запись; журнал не теряет данные
            self._write_error_event("sqlite_insert_failed", str(e), ev)
        return ev

    def _write_jsonl(self, ev: Event) -> None:
        try:
            data = ev.to_json(include_args=True)
            # Ограничение размера результата
            if self.cfg.record_results and isinstance(ev.result, str):
                data["result"] = truncate(ev.result, self.cfg.max_result_chars)
            if not self.cfg.record_args:
                data["args"] = {}
            line_size = len(json.dumps(data, ensure_ascii=False, default=str))
            if line_size > self.cfg.max_result_chars * 4:
                # Слишком велико — тело в payloads/, в JSONL — ссылка
                pfile = self._spill_payload(ev)
                data["args"] = {}
                data["result"] = "(см. payload)"
                data["payload_path"] = str(pfile) if pfile else ""
            day = time.strftime("%Y%m%d", time.localtime(ev.ts))
            f = self.cfg.jsonl_dir / f"events-{day}.jsonl"
            append_jsonl(f, data, fsync=self.cfg.fsync)
        except Exception as e:
            self._write_error_event("jsonl_write_failed", str(e), ev)

    def _spill_payload(self, ev: Event) -> Path | None:
        try:
            p = self.cfg.payloads_dir / f"{ev.event_id}.json"
            write_text_safe(p, json.dumps(
                {"args": ev.args, "result": ev.result},
                ensure_ascii=False, default=str))
            return p
        except Exception:
            return None

    def _write_error_event(self, where: str, msg: str, ev: Event) -> None:
        try:
            day = time.strftime("%Y%m%d")
            f = self.cfg.jsonl_dir / f"errors-{day}.jsonl"
            append_jsonl(f, {
                "ts": time.time(), "where": where, "error": msg,
                "event_id": ev.event_id, "kind": ev.kind, "action": ev.action,
            })
        except Exception:
            pass

    # ─── Чтение ──────────────────────────────────────────────
    def get(self, event_id: str, with_payload: bool = True) -> Event | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
        if not row:
            return None
        payload = self.load_payload(row["payload_path"]) if with_payload else None
        return Event.from_row(dict(row), payload)

    @staticmethod
    def load_payload(payload_path: str) -> dict | None:
        if not payload_path:
            return None
        p = Path(payload_path)
        if not p.is_absolute():
            # относительные пути — от data/journal
            p = Path("data/journal") / payload_path
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            # путь хранится как абсолютный в большинстве случаев
            try:
                return json.loads(Path(payload_path).read_text(encoding="utf-8"))
            except Exception:
                return None

    def query(
        self,
        kind: str | None = None,
        session_id: str | None = None,
        task_id: str | None = None,
        trace_id: str | None = None,
        agent_id: str | None = None,
        server_name: str | None = None,
        tool_name: str | None = None,
        status: str | None = None,
        target_like: str | None = None,
        since: float | None = None,
        until: float | None = None,
        limit: int = 200,
        offset: int = 0,
        order_desc: bool = True,
        only_mutating: bool = False,
    ) -> list[Event]:
        where: list[str] = []
        params: list[Any] = []
        if kind:
            where.append("kind = ?"); params.append(kind)
        if session_id:
            where.append("session_id = ?"); params.append(session_id)
        if task_id:
            where.append("task_id = ?"); params.append(task_id)
        if trace_id:
            where.append("trace_id = ?"); params.append(trace_id)
        if agent_id:
            where.append("agent_id = ?"); params.append(agent_id)
        if server_name:
            where.append("server_name = ?"); params.append(server_name)
        if tool_name:
            where.append("tool_name = ?"); params.append(tool_name)
        if status:
            where.append("status = ?"); params.append(status)
        if target_like:
            where.append("targets LIKE ?"); params.append(f'%"{target_like}"%')
        if since is not None:
            where.append("ts >= ?"); params.append(since)
        if until is not None:
            where.append("ts <= ?"); params.append(until)
        if only_mutating:
            where.append("reversible > 0")
        sql = "SELECT * FROM events"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY seq " + ("DESC" if order_desc else "ASC")
        sql += " LIMIT ? OFFSET ?"
        params += [max(1, min(int(limit), 5000)), max(0, int(offset))]
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [Event.from_row(dict(r)) for r in rows]

    def search(self, text: str, limit: int = 50) -> list[Event]:
        """Полнотекстовый поиск (FTS5, при отсутствии — LIKE)."""
        text = (text or "").strip()
        if not text:
            return []
        if self._fts_ok:
            q = " ".join(
                f'"{t}*"' for t in text.replace('"', " ").split()
            )
            try:
                with self._lock:
                    rows = self._conn.execute(
                        "SELECT e.* FROM journal_fts f "
                        "JOIN events e ON e.event_id = f.event_id "
                        "WHERE journal_fts MATCH ? "
                        "ORDER BY rank LIMIT ?",
                        (q, max(1, min(limit, 500))),
                    ).fetchall()
                return [Event.from_row(dict(r)) for r in rows]
            except sqlite3.OperationalError:
                pass
        like = f"%{text}%"
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM events WHERE action LIKE ? OR targets LIKE ? "
                "OR error LIKE ? OR meta LIKE ? "
                "ORDER BY seq DESC LIMIT ?",
                (like, like, like, like, max(1, min(limit, 500))),
            ).fetchall()
        return [Event.from_row(dict(r)) for r in rows]

    def last_event_id(self, session_id: str = "", task_id: str = "") -> str:
        sql = "SELECT event_id FROM events"
        conds, params = [], []
        if session_id:
            conds.append("session_id = ?"); params.append(session_id)
        if task_id:
            conds.append("task_id = ?"); params.append(task_id)
        if conds:
            sql += " WHERE " + " AND ".join(conds)
        sql += " ORDER BY seq DESC LIMIT 1"
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        return row["event_id"] if row else ""

    # ─── Статистика ──────────────────────────────────────────
    def stats(self) -> dict:
        from .utils import dir_size, disk_usage, human_size
        with self._lock:
            total = self._conn.execute("SELECT COUNT(*) c FROM events").fetchone()["c"]
            by_kind = {
                r["kind"]: r["c"]
                for r in self._conn.execute(
                    "SELECT kind, COUNT(*) c FROM events GROUP BY kind")
            }
            mutating = self._conn.execute(
                "SELECT COUNT(*) c FROM events WHERE reversible > 0"
            ).fetchone()["c"]
            sessions = self._conn.execute(
                "SELECT COUNT(DISTINCT session_id) c FROM events WHERE session_id != ''"
            ).fetchone()["c"]
            tasks = self._conn.execute(
                "SELECT COUNT(DISTINCT task_id) c FROM events WHERE task_id != ''"
            ).fetchone()["c"]
            first = self._conn.execute(
                "SELECT iso_time FROM events ORDER BY seq ASC LIMIT 1"
            ).fetchone()
        journal_size = dir_size(self.cfg.base_dir)
        disk = disk_usage(self.cfg.base_dir)
        return {
            "total_events": total,
            "by_kind": by_kind,
            "mutating_events": mutating,
            "sessions": sessions,
            "tasks": tasks,
            "journal_size_bytes": journal_size,
            "journal_size_human": human_size(journal_size),
            "first_event": first["iso_time"] if first else None,
            "disk": disk,
            "min_free_gb": self.cfg.min_free_gb,
            "retention_needed": (
                disk["free_gb"] < self.cfg.min_free_gb
                if disk["free_gb"] else False
            ),
            "fts_enabled": self._fts_ok,
            "db_path": str(self.db_path),
        }

    # ─── Обслуживание ────────────────────────────────────────
    def vacuum(self) -> None:
        with self._lock:
            self._conn.execute("VACUUM")
            self._conn.commit()

    def close(self) -> None:
        try:
            with self._lock:
                self._conn.commit()
                self._conn.close()
        except Exception:
            pass
