#!/usr/bin/env python
"""Тесты фичи «История» (features/history): API зеркал /api/ops/*,
FTS-поиск, экспорт, деградация без PG, монтаж через FeatureLoader.

Интеграция поднимает встроенный PostgreSQL (pgserver) и накатывает
реальную схему db/ops.sql через init_db.init_schema.
Без pgserver выполняется только юнит-часть.

Запуск:
    /path/to/venv/python scripts/test_history_feature.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

PG_DATA_DIR = Path("/home/z/my-project/scripts/.pgserver_history")

PASSED = 0
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ok    {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL  {name} {detail}")


# ═══════════════════════════════════════════════════════════════════
# 1. Юнит (без БД)
# ═══════════════════════════════════════════════════════════════════
def unit_checks() -> None:
    print("[1] юнит: манифест и хелперы")
    import yaml
    from src.core.features import FeatureManifest

    raw = yaml.safe_load(
        (BASE_DIR / "features" / "history" / "feature.yaml")
        .read_text(encoding="utf-8"))
    mf = FeatureManifest.model_validate(raw)
    check("манифест валиден", mf.id == "history" and mf.enabled)
    check("api_router задан", mf.api_router == "api.py:create_router")
    check("ui_tab задан", bool(mf.ui_tab and mf.ui_tab.file == "ui.js"))

    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "hist_api_unit", BASE_DIR / "features" / "history" / "api.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    check("like-экранирование",
          mod._like("50%_x\\") == r"%50\%\_x\\%",
          mod._like("50%_x\\"))
    check("clamp: None → default", mod._clamp(None, 50) == 50)
    check("clamp: верхняя граница", mod._clamp(999999, 50) == 500)
    check("clamp: нижняя граница", mod._clamp(0, 50) == 1)

    check("ui.js существует", (BASE_DIR / "features" / "history" /
                               "ui.js").exists())
    check("api.py существует", (BASE_DIR / "features" / "history" /
                                "api.py").exists())


# ═══════════════════════════════════════════════════════════════════
# 2. Интеграция (живой PG)
# ═══════════════════════════════════════════════════════════════════
def integration(uri: str) -> None:
    os.environ["DATABASE_URL"] = uri
    os.environ["PG_ENABLED"] = "true"

    import psycopg

    print("[2] схема: db/ops.sql через init_schema")
    with psycopg.connect(uri, autocommit=True) as conn:
        conn.execute("CREATE SCHEMA IF NOT EXISTS journal")
    from src.db.init_db import init_schema
    ops = BASE_DIR / "db" / "ops.sql"
    reports, ok = init_schema(uri, files=[ops])
    errs = [str(e) for r in reports for e in r.get("errors", [])]
    # единственные допустимые ошибки — отсутствующая journal.actions
    # (реальную партиционированную таблицу создаёт db/journal.sql)
    allowed = all("journal.actions" in e for e in errs)
    check("ops.sql применился", ok or allowed, "; ".join(errs)[:200])
    reports2, _ = init_schema(uri, files=[ops])
    errs2 = [str(e) for r in reports2 for e in r.get("errors", [])]
    check("ops.sql идемпотентен",
          all("journal.actions" in e for e in errs2), "; ".join(errs2)[:200])

    print("[3] FTS-колонки")
    with psycopg.connect(uri, autocommit=True) as conn:
        row = conn.execute(
            "SELECT count(*)::int FROM information_schema.columns "
            "WHERE table_schema='ops' AND table_name='chat_messages' "
            "AND column_name='tsv'").fetchone()
        check("ops.chat_messages.tsv", bool(row and row[0] == 1))
        row = conn.execute(
            "SELECT count(*)::int FROM information_schema.columns "
            "WHERE table_schema='journal' AND table_name='events_mirror' "
            "AND column_name='tsv'").fetchone()
        check("journal.events_mirror.tsv", bool(row and row[0] == 1))

    print("[4] фикстуры (сессия, план, события)")
    with psycopg.connect(uri, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO ops.chat_sessions (session_id, message_count, "
            "last_role) VALUES ('s-test-1', 3, 'assistant')")
        conn.execute(
            "INSERT INTO ops.chat_messages (session_id, seq, role, agent, "
            "content, ts) VALUES "
            "('s-test-1', 0, 'user', '', "
            "'Привет, покажи файл отчёта по проекту', now()), "
            "('s-test-1', 1, 'assistant', 'file', "
            "'Прочитал data/report.txt, вот выжимка', now()), "
            "('s-test-1', 2, 'user', '', "
            "'Спасибо, найди все упоминания бюджета', now())")
        conn.execute(
            "INSERT INTO ops.plans (plan_id, session_id, query, intent, "
            "mode, status, success, steps_total, steps_done, updated_at, "
            "payload) VALUES ('p-test-1', 's-test-1', 'найди бюджет', "
            "'analysis', 'dag', 'done', true, 2, 2, now(), "
            "'{\"steps\":[{\"agent\":\"file\",\"task\":\"прочитать\","
            "\"status\":\"done\"}]}'::jsonb)")
        conn.execute(
            "INSERT INTO journal.events_mirror (event_uid, seq, ts, kind, "
            "status, session_id, trace_id, server_name, tool_name, error) "
            "VALUES "
            "('ev-1', 1, now(), 'task_start', 'ok', 's-test-1', 'tr-1', "
            "'', '', ''), "
            "('ev-2', 2, now(), 'tool_call', 'ok', 's-test-1', 'tr-1', "
            "'filesystem', 'file.read', ''), "
            "('ev-3', 3, now(), 'tool_call', 'error', 's-test-1', 'tr-1', "
            "'postgres', 'pg.query', 'connection refused')")

    print("[5] монтаж через FeatureLoader")
    from src.core.features import FeatureLoader
    loader = FeatureLoader(BASE_DIR)
    found = [m for m, _ in loader.scan() if m.id == "history"]
    check("history в скане реестра", bool(found))
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    asyncio.run(loader.mount(app, {}))
    entry = loader.registry.get("history") or {}
    check("api смонтирован", entry.get("api_mounted") is True)
    check("js_url корректен",
          entry.get("js_url") == "/features/history/ui.js")
    in_client = [i["id"] for i in loader.list_for_client()]
    check("history в /api/features", "history" in in_client)
    client = TestClient(app)

    print("[6] эндпоинты /api/ops/*")
    r = client.get("/api/ops/status")
    d = r.json()
    check("status 200", r.status_code == 200)
    check("pg available", d.get("pg", {}).get("available") is True)
    c = d.get("counts", {})
    check("счётчики", (c.get("sessions") == 1 and c.get("messages") == 3
                       and c.get("plans") == 1 and c.get("events") == 3),
          str(c))
    check("fts обнаружен", d.get("fts", {}).get("chat") is True
          and d.get("fts", {}).get("events") is True, str(d.get("fts")))

    r = client.get("/api/ops/sessions")
    d = r.json()
    check("sessions 200, 1 запись", r.status_code == 200
          and len(d.get("items", [])) == 1)
    check("preview последнего сообщения",
          "бюджета" in (d["items"][0].get("preview") or ""),
          str(d.get("items")))
    r = client.get("/api/ops/sessions", params={"q": "бюджет"})
    check("sessions?q= фильтр", len(r.json().get("items", [])) == 1)

    r = client.get("/api/ops/sessions/s-test-1/messages")
    d = r.json()
    check("messages 200, 3 шт", r.status_code == 200
          and len(d.get("items", [])) == 3)
    check("порядок seq", d["items"][0]["seq"] == 0
          and d["items"][0]["role"] == "user")

    r = client.get("/api/ops/sessions/s-test-1/export")
    check("export 200", r.status_code == 200)
    check("export markdown содержит текст",
          "Прочитал data/report.txt" in r.text)
    check("export attachment",
          "attachment" in r.headers.get("content-disposition", ""))
    r = client.get("/api/ops/sessions/missing/export")
    check("export 404 для пустой сессии", r.status_code == 404)

    r = client.get("/api/ops/plans")
    check("plans 200", r.status_code == 200
          and len(r.json().get("items", [])) == 1)
    r = client.get("/api/ops/plans/p-test-1")
    d = r.json()
    check("план: payload со steps", r.status_code == 200
          and d.get("payload", {}).get("steps"))
    r = client.get("/api/ops/plans/missing")
    check("план 404", r.status_code == 404)

    r = client.get("/api/ops/events", params={"kind": "task_start"})
    check("events kind-фильтр", len(r.json().get("items", [])) == 1)
    r = client.get("/api/ops/events", params={"tool": "file.read"})
    check("events tool-фильтр", len(r.json().get("items", [])) == 1)
    r = client.get("/api/ops/events", params={"limit": 999999})
    check("events limit clamped", r.status_code == 200)

    print("[7] поиск (FTS + ILIKE-фолбэк)")
    r = client.get("/api/ops/search", params={"q": "бюджет"})
    d = r.json()
    check("search 200, mode fts", r.status_code == 200
          and d.get("mode") == "fts", str(d.get("mode")))
    check("search: чат найден", len(d.get("chat", [])) >= 1)
    r = client.get("/api/ops/search", params={"q": "connection"})
    d = r.json()
    check("search: событие найдено", len(d.get("events", [])) >= 1)
    r = client.get("/api/ops/search", params={"q": "zzzzнеСуществует"})
    d = r.json()
    check("search: пусто без ошибки", r.status_code == 200
          and not d.get("chat") and not d.get("events"))
    r = client.get("/api/ops/search")
    check("search без q → 422", r.status_code == 422)

    # ILIKE-фолбэк: имитируем отсутствие FTS-колонок
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "hist_api_it", BASE_DIR / "features" / "history" / "api.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    check("feature-модуль подгружен", hasattr(mod, "create_router"))
    mod._FTS = False                       # форсируем фолбэк
    app2 = FastAPI()
    app2.include_router(mod.create_router())
    client2 = TestClient(app2)
    r = client2.get("/api/ops/search", params={"q": "бюджета"})
    d = r.json()
    check("ilike-фолбэк работает", r.status_code == 200
          and d.get("mode") == "ilike" and len(d.get("chat", [])) >= 1,
          str(d.get("mode")))

    print("[8] деградация: PG недоступен")
    orig_dsn = mod._dsn
    mod._dsn = lambda: "postgresql://llmagent:secret@127.0.0.1:59999/none"
    mod._POOL = None
    mod._FTS = None
    r = client2.get("/api/ops/status")
    d = r.json()
    check("status: available=false, 200", r.status_code == 200
          and d.get("pg", {}).get("available") is False)
    r = client2.get("/api/ops/sessions")
    check("sessions → 503 при лежащем PG", r.status_code == 503)
    r = client2.get("/api/ops/search", params={"q": "test"})
    check("search → 503", r.status_code == 503)
    mod._dsn = orig_dsn
    mod._POOL = None
    mod._FTS = None

    print("[9] CLI-экспорт (scripts/export_report.py)")
    env = dict(os.environ)
    for args, must in (
        (["--session", "s-test-1"], "Прочитал data/report.txt"),
        (["--sessions-list"], "s-test-1"),
        (["--plan", "p-test-1"], "прочитать"),
        (["--events", "--hours", "24"], "tool_call"),
        (["--events", "--hours", "24", "--format", "csv"],
         "session_id,trace_id"),
    ):
        p = subprocess.run(
            [sys.executable, str(BASE_DIR / "scripts" / "export_report.py")]
            + args, capture_output=True, text=True, env=env, timeout=120)
        check("export_report " + " ".join(args[:2]),
              p.returncode == 0 and must in p.stdout,
              (p.stderr or p.stdout)[-160:])


# ═══════════════════════════════════════════════════════════════════
def main() -> int:
    t0 = time.time()
    unit_checks()

    server = None
    uri = ""
    try:
        import pgserver  # noqa
        print("[PG] поднимаю встроенный PostgreSQL (pgserver)...")
        server = pgserver.get_server(str(PG_DATA_DIR))
        uri = server.get_uri()
        print(f"[PG] готов: {uri.split('@')[-1]}")
    except Exception as e:
        print(f"[PG] pgserver недоступен ({e}) — интеграция пропущена")

    if uri:
        try:
            integration(uri)
        except Exception as e:
            import traceback
            traceback.print_exc()
            FAILED.append(f"интеграция упала: {e}")
        finally:
            try:
                if server is not None:
                    server.cleanup()
            except Exception:
                pass
            shutil.rmtree(PG_DATA_DIR, ignore_errors=True)

    print(f"\n{'=' * 60}")
    print(f"Пройдено: {PASSED}, провалено: {len(FAILED)} "
          f"({time.time() - t0:.1f}s)")
    for name in FAILED:
        print(f"  FAIL: {name}")
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
