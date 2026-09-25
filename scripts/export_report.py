#!/usr/bin/env python
"""Экспорт отчётов из долговременных зеркал PostgreSQL (и локальных файлов).

Примеры:
  python scripts/export_report.py --sessions-list
  python scripts/export_report.py --session <session_id> --out report.md
  python scripts/export_report.py --plan <plan_id>
  python scripts/export_report.py --events --hours 24 --format csv --out events.csv

Источник: по умолчанию PostgreSQL-зеркала (ops.*, journal.events_mirror);
если PG недоступен — локальные файлы (data/sessions/*.jsonl,
data/plans/*.json). Журнал событий из файлов не экспортируется —
используйте CLI журнала: python -m src.journal.cli.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).resolve().parents[1]


# ═══════════════════════════════════════════════════════════════════
# Источники
# ═══════════════════════════════════════════════════════════════════
def _dsn() -> str:
    sys.path.insert(0, str(BASE))
    from src.config import get_settings
    return get_settings().postgres.dsn()


def _pg():
    """psycopg-соединение или None (PG недоступен)."""
    try:
        import psycopg
    except ImportError:
        return None
    try:
        conn = psycopg.connect(_dsn(), connect_timeout=3)
        return conn
    except Exception:
        return None


def _fmt_ts(v) -> str:
    if v is None:
        return ""
    try:
        return v.isoformat(sep=" ", timespec="seconds")
    except Exception:
        return str(v)


def pg_sessions(conn) -> list[dict]:
    from psycopg.rows import dict_row
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT s.session_id, s.first_seen, s.last_seen, "
            "       s.message_count "
            "FROM ops.chat_sessions s ORDER BY s.last_seen DESC LIMIT 500")
        return cur.fetchall()


def pg_session_messages(conn, sid: str) -> list[dict]:
    from psycopg.rows import dict_row
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT seq, role, agent, content, ts FROM ops.chat_messages "
            "WHERE session_id = %s ORDER BY seq ASC", (sid,))
        return cur.fetchall()


def pg_plan(conn, plan_id: str) -> dict | None:
    from psycopg.rows import dict_row
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT * FROM ops.plans WHERE plan_id = %s", (plan_id,))
        return cur.fetchone()


def pg_events(conn, hours: float) -> list[dict]:
    from psycopg.rows import dict_row
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT ts, kind, action, status, session_id, trace_id, "
            "       server_name, tool_name, duration_ms, error "
            "FROM journal.events_mirror WHERE ts >= %s "
            "ORDER BY ts DESC LIMIT 10000", (since,))
        return cur.fetchall()


# ── Локальные файлы (фолбэк, когда PG выключен) ─────────────────────
def file_sessions() -> list[dict]:
    out = []
    for p in sorted((BASE / "data" / "sessions").glob("*.jsonl"),
                    key=lambda x: x.stat().st_mtime, reverse=True):
        n = 0
        try:
            with p.open(encoding="utf-8", errors="replace") as f:
                for line in f:
                    if line.strip():
                        n += 1
        except OSError:
            continue
        out.append({"session_id": p.stem, "message_count": n,
                    "last_seen": _fmt_ts(
                        datetime.fromtimestamp(p.stat().st_mtime,
                                               tz=timezone.utc))})
    return out[:500]


def file_session_messages(sid: str) -> list[dict]:
    p = BASE / "data" / "sessions" / f"{sid}.jsonl"
    if not p.exists():
        return []
    out = []
    for i, line in enumerate(p.read_text(encoding="utf-8",
                                         errors="replace").splitlines()):
        if not line.strip():
            continue
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            continue
        out.append({"seq": i, "role": m.get("role", "?"),
                    "agent": m.get("agent", ""),
                    "content": m.get("content", ""), "ts": None})
    return out


def file_plan(plan_id: str) -> dict | None:
    p = BASE / "data" / "plans" / f"{plan_id}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def file_events(hours: float) -> list[dict]:
    """Из SQLite-журнала (тот же источник, что и у репликатора)."""
    db = BASE / "data" / "journal" / "journal.sqlite"
    if not db.exists():
        return []
    since = time.time() - hours * 3600
    out = []
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5.0)
    try:
        cur = conn.execute(
            "SELECT ts, iso_time, kind, status, session_id, trace_id, "
            "       server_name, tool_name, duration_ms, error "
            "FROM events WHERE ts >= ? ORDER BY id DESC LIMIT 10000",
            (since,))
        for row in cur:
            out.append({
                "ts": row[0], "kind": row[2], "status": row[3],
                "session_id": row[4] or "", "trace_id": row[5] or "",
                "server_name": row[6] or "", "tool_name": row[7] or "",
                "duration_ms": row[8] or 0, "error": row[9] or "",
                "iso_time": row[1] or "",
            })
    finally:
        conn.close()
    return out


# ═══════════════════════════════════════════════════════════════════
# Рендеры
# ═══════════════════════════════════════════════════════════════════
def render_session_md(sid: str, msgs: list[dict], source: str) -> str:
    lines = [f"# Сессия {sid}", "",
             f"Сообщений: {len(msgs)} · источник: {source}", ""]
    for m in msgs:
        head = f"## {_fmt_ts(m.get('ts')) or '—'} — {m.get('role', '?')}"
        if m.get("agent"):
            head += f" ({m['agent']})"
        lines += [head, "", m.get("content") or "", ""]
    return "\n".join(lines)


def render_plan_md(plan: dict, source: str) -> str:
    if "steps" not in plan and "payload" in plan:
        plan = plan["payload"] or {}
    lines = [f"# План {plan.get('plan_id', '?')}", "",
             f"Запрос: {plan.get('query', '')}",
             f"Intent: {plan.get('intent', '')} · режим: "
             f"{plan.get('mode', 'sequential')} · статус: "
             f"{plan.get('status', '')}",
             f"Источник: {source}", ""]
    for i, st in enumerate(plan.get("steps", []) or [], 1):
        lines.append(
            f"{i}. **{st.get('agent', '?')}** — {st.get('task', '')}"
            f" [{st.get('status', '?')}]")
    return "\n".join(lines) + "\n"


def render_events_csv(events: list[dict]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ts", "kind", "status", "session_id", "trace_id",
                "server", "tool", "duration_ms", "error"])
    for e in events:
        ts = e.get("iso_time") or _fmt_ts(e.get("ts"))
        w.writerow([ts, e.get("kind", ""), e.get("status", ""),
                    e.get("session_id", ""), e.get("trace_id", ""),
                    e.get("server_name", ""), e.get("tool_name", ""),
                    e.get("duration_ms", 0), (e.get("error") or "")])
    return buf.getvalue()


def render_events_md(events: list[dict], hours: float, source: str) -> str:
    lines = [f"# События журнала за {hours} ч", "",
             f"Всего: {len(events)} · источник: {source}", ""]
    for e in events[:500]:
        ts = e.get("iso_time") or _fmt_ts(e.get("ts"))
        mark = " ⚠" if e.get("status") not in ("ok", "") else ""
        lines.append(
            f"- `{ts}` {e.get('kind', '')} "
            f"{e.get('server_name', '')}/{e.get('tool_name', '')} "
            f"[{e.get('status', '')}]{mark}"
            + (f" — {e['error'][:120]}" if e.get("error") else ""))
    return "\n".join(lines) + "\n"


# ═══════════════════════════════════════════════════════════════════
# main
# ═══════════════════════════════════════════════════════════════════
def main() -> int:
    ap = argparse.ArgumentParser(
        description="Экспорт отчётов из зеркал PostgreSQL / локальных файлов")
    ap.add_argument("--sessions-list", action="store_true",
                    help="список сессий")
    ap.add_argument("--session", metavar="ID", help="экспорт сессии")
    ap.add_argument("--plan", metavar="ID", help="экспорт плана")
    ap.add_argument("--events", action="store_true",
                    help="экспорт событий журнала")
    ap.add_argument("--hours", type=float, default=24.0,
                    help="окно для --events (часов, по умолчанию 24)")
    ap.add_argument("--format", choices=["md", "csv"], default="md")
    ap.add_argument("--out", metavar="FILE", help="файл (иначе stdout)")
    args = ap.parse_args()

    if not (args.sessions_list or args.session or args.plan or args.events):
        ap.print_help()
        return 2

    conn = _pg()
    pg = conn is not None
    if not pg:
        print("# PostgreSQL недоступен — экспорт из локальных файлов\n",
              file=sys.stderr)

    def emit(text: str) -> None:
        if args.out:
            Path(args.out).write_text(text, encoding="utf-8")
            print(f"OK: {args.out} ({len(text)} символов)")
        else:
            sys.stdout.write(text)

    if args.sessions_list:
        rows = pg_sessions(conn) if pg else file_sessions()
        if args.format == "csv":
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["session_id", "message_count", "last_seen"])
            for r in rows:
                w.writerow([r.get("session_id"),
                            r.get("message_count", 0),
                            _fmt_ts(r.get("last_seen"))])
            emit(buf.getvalue())
        else:
            lines = [f"# Сессии ({len(rows)})", ""]
            for r in rows:
                lines.append(
                    f"- `{r.get('session_id')}` — "
                    f"{r.get('message_count', 0)} сообщ. · "
                    f"{_fmt_ts(r.get('last_seen'))}")
            emit("\n".join(lines) + "\n")
        return 0

    if args.session:
        msgs = (pg_session_messages(conn, args.session) if pg
                else file_session_messages(args.session))
        if not msgs:
            print(f"Сессия {args.session} не найдена "
                  f"({'PG' if pg else 'файлы'})", file=sys.stderr)
            return 1
        emit(render_session_md(args.session, msgs,
                               "PG-зеркало" if pg else "локальные файлы"))
        return 0

    if args.plan:
        plan = pg_plan(conn, args.plan) if pg else file_plan(args.plan)
        if not plan:
            print(f"План {args.plan} не найден "
                  f"({'PG' if pg else 'файлы'})", file=sys.stderr)
            return 1
        emit(render_plan_md(dict(plan), "PG-зеркало" if pg else "файлы"))
        return 0

    if args.events:
        events = pg_events(conn, args.hours) if pg else file_events(args.hours)
        if args.format == "csv":
            emit(render_events_csv(events))
        else:
            emit(render_events_md(events, args.hours,
                                  "PG-зеркало" if pg else "SQLite-журнал"))
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
