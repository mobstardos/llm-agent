#!/usr/bin/env python
"""E2E-проверка PostgreSQL-контура: pgserver + init_db + живой сервер.

Поднимает встроенный PG, применяет схему, стартует FastAPI-сервер llm-agent
(uvicorn, subprocess), ждёт готовности и проверяет:
  - GET /api/db/status (pg_ok, бэкенд, счётчики репликатора);
  - фоновую репликацию (после старта сервера журнал должен начать переть
    в journal.events_mirror — ws-сессия создаёт события журнала).
После проверки останавливает сервер и PG.
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BASE = Path("/home/z/my-project/download/llm-agent")
PG_DATA = Path("/home/z/my-project/scripts/.pgserver_e2e")
VENV_PY = "/home/z/.venv/bin/python"

PASSED = 0
FAILED: list[str] = []


def check(name, cond, detail=""):
    global PASSED
    print(("  ok    " if cond else "  FAIL  ") + name + (f" {detail}" if detail and not cond else ""))
    if cond:
        PASSED += 1
    else:
        FAILED.append(name)


def http_get(url, timeout=5):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8"))
    except Exception as e:
        return None, str(e)


def main() -> int:
    import pgserver

    print("[1] pgserver + init_db")
    server = pgserver.get_server(str(PG_DATA))
    uri = server.get_uri()
    sys.path.insert(0, str(BASE))
    os.environ["DATABASE_URL"] = uri
    from src.db.init_db import check_schema, init_schema
    _, ok = init_schema(uri)
    info = check_schema(uri)
    check("схема применена", ok and info["ok"])

    env = dict(os.environ)
    env["DATABASE_URL"] = uri
    env["PG_ENABLED"] = "true"
    env["PG_REPLICATE_INTERVAL"] = "2"       # быстрый цикл для теста
    env.setdefault("LLM_BASE_URL", "http://127.0.0.1:7936/v1")

    print("[2] старт FastAPI-сервера (subprocess)...")
    proc = subprocess.Popen(
        [VENV_PY, "-m", "uvicorn", "src.main:app", "--port", "8123"],
        cwd=str(BASE), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True,
    )
    log_lines: list[str] = []
    try:
        deadline = time.time() + 150
        ready = False
        while time.time() < deadline:
            if proc.poll() is not None:
                break
            code, body = http_get("http://127.0.0.1:8123/api/db/status", timeout=3)
            if code == 200:
                ready = True
                break
            time.sleep(2)
        check("сервер поднялся, /api/db/status = 200", ready)
        if not ready:
            return finish(proc, log_lines)

        code, st = http_get("http://127.0.0.1:8123/api/db/status")
        print("   status:", json.dumps(st, ensure_ascii=False)[:400])
        check("postgres.healthy = true", st.get("postgres", {}).get("healthy") is True)
        check("репликатор в status", isinstance(st.get("replicator"), dict))
        check("backend = postgresql",
              st.get("postgres", {}).get("backend") == "postgresql")

        print("[3] фоновая репликация журнала (ws-сессии стартуют при коннекте)")
        # просто ждём 2 цикла репликатора — системные события (system/startup)
        # пишутся журналом в SQLite и должны утекать в PG
        time.sleep(6)
        import psycopg
        with psycopg.connect(uri, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM journal.events_mirror")
                n_mirror = cur.fetchone()[0]
                cur.execute("SELECT count(*) FROM ops.sync_state WHERE key='journal'")
                n_cur = cur.fetchone()[0]
        print(f"   events_mirror: {n_mirror}, курсор: {n_cur}")
        check("зеркало журнала наполняется", n_mirror > 0, f"rows={n_mirror}")

        code, st2 = http_get("http://127.0.0.1:8123/api/db/status")
        rep = (st2 or {}).get("replicator") or {}
        check("счётчик journal_events > 0", rep.get("journal_events", 0) > 0, str(rep))
        check("ошибок репликатора нет", rep.get("errors", 0) == 0, str(rep))
        return finish(proc, log_lines)
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
        try:
            server.cleanup()
        except Exception:
            pass


def finish(proc, log_lines) -> int:
    global PASSED
    try:
        out = proc.communicate(timeout=15)[0] or ""
    except Exception:
        out = ""
    text = "\n".join(log_lines) + str(out)
    print(f"\n{'=' * 60}")
    print(f"E2E пройдено: {PASSED}, провалено: {len(FAILED)}")
    for f in FAILED:
        print("  FAIL:", f)
    if FAILED:
        print("\n--- хвост лога сервера ---")
        print(text[-2500:])
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
