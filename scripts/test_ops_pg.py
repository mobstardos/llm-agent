#!/usr/bin/env python
"""Тесты Task 14: память агентов (pgvector), мультиинстанс-аналитика,
эксплуатация (бэкапы/ретенция) + фичи memory/cluster/ops.

Юнит-часть — всегда. Интеграционная — поднимает встроенный PostgreSQL
(pgserver) и накатывает реальные db/init.sql + journal.sql + ops.sql.
pgvector в pgserver нет: векторные statements мягко пропускаются, путь
поиска — FTS/ILIKE (это тестируется явно как деградация).

Запуск: /path/to/python scripts/test_ops_pg.py
"""
from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

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


# ═══════════════════════════════════════════════════════════════════════
# 1. Юнит: embedders
# ═══════════════════════════════════════════════════════════════════════
def unit_embedders() -> None:
    print("[1] HashingEmbedder / make_embedder")
    import numpy as np
    from src.memory.embedders import (AGENT_MEMORY_DIM, HashingEmbedder,
                                      make_embedder)

    e = HashingEmbedder()
    v1 = e.embed_one("настройка бэкапов PostgreSQL")
    v2 = e.embed_one("настройка бэкапов PostgreSQL")
    v3 = e.embed_one("совершенно другой текст про кошек")
    check("dim = 1024", len(v1) == AGENT_MEMORY_DIM, str(len(v1)))
    check("детерминизм", v1 == v2)
    check("разные тексты ≠", v1 != v3)
    norm = float(np.linalg.norm(np.asarray(v1)))
    check("L2-нормализация", abs(norm - 1.0) < 1e-5, str(norm))
    check("не нулевой вектор", float(np.max(np.abs(v1))) > 0)
    vs = e.embed(["а", "б"])
    check("batch embed", len(vs) == 2 and len(vs[0]) == AGENT_MEMORY_DIM)
    # похожие тексты ближе, чем непохожие (косинус через скалярное)
    a = e.embed_one("ошибка подключения к базе данных")
    b = e.embed_one("подключение к базе данных оборвалось с ошибкой")
    c = e.embed_one("рецепт борща со свёклой")
    sim_ab = float(np.dot(a, b))
    sim_ac = float(np.dot(a, c))
    check("похожесть осмысленна", sim_ab > sim_ac,
          f"{sim_ab:.3f} vs {sim_ac:.3f}")

    eh = make_embedder("hash")
    check("make_embedder('hash')", eh.name.startswith("hash"))
    emb = make_embedder("auto")
    v = emb.embed_one("тест")
    check("make_embedder('auto') даёт dim",
          len(v) == 1024, str(len(v)))


# ═══════════════════════════════════════════════════════════════════════
# 2. Юнит: ретенция/локи/скан фич
# ═══════════════════════════════════════════════════════════════════════
def unit_misc() -> None:
    print("[2] TABLES ретенции, _lock_id, скан фич")
    from src.db.retention import TABLES
    names = {t.name for t in TABLES}
    check("5 таблиц ретенции", len(TABLES) == 5, str(names))
    check("PK задан у всех", all(t.pk_cols for t in TABLES))
    check("events_mirror с ts", any(
        t.name == "journal.events_mirror" and t.ts_col == "ts"
        for t in TABLES))

    from src.db.multi_instance import _lock_id
    check("_lock_id детерминирован", _lock_id("a") == _lock_id("a"))
    check("_lock_id разные", _lock_id("a") != _lock_id("b"))
    check("_lock_id int64 signed", -2**63 <= _lock_id("x") < 2**63)

    from src.core.features import FeatureLoader
    loader = FeatureLoader(BASE_DIR)
    ids = [m.id for m, _ in loader.scan()]
    for fid in ("memory", "cluster", "ops"):
        check(f"фича {fid} в скане реестра", fid in ids, str(ids))


# ═══════════════════════════════════════════════════════════════════════
# 3. Интеграция: схема (sync, psycopg)
# ═══════════════════════════════════════════════════════════════════════
def integration_schema(uri: str) -> None:
    print("[3] схема: init.sql + journal.sql + ops.sql через init_schema")
    import psycopg
    # Дата-каталог pgserver персистентен между прогонами: сбрасываем
    # схемы состояния, чтобы интеграция не упиралась в дубликаты PK
    with psycopg.connect(uri, autocommit=True) as conn:
        for schema in ("journal", "ops", "memory", "vectors", "graph",
                       "cache", "policies", "audit", "metrics", "meta"):
            conn.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.execute("CREATE SCHEMA IF NOT EXISTS journal")
        conn.execute("CREATE SCHEMA IF NOT EXISTS ops")
    from src.db.init_db import init_schema
    files = [BASE_DIR / "db" / n for n in
             ("init.sql", "journal.sql", "ops.sql")]
    reports, ok = init_schema(uri, files=files)
    errs = [str(e) for r in reports for e in r.get("errors", [])]
    # допустимые ошибки — только отсутствие pgvector
    allowed = all(
        ("vector" in e) or ("halfvec" in e) or ("vectors.chunks" in e)
        for e in errs
    ) if errs else True
    check("схема применилась", ok or allowed, "; ".join(errs)[:300])
    reports2, _ = init_schema(uri, files=files)
    errs2 = [str(e) for r in reports2 for e in r.get("errors", [])]
    check("идемпотентна", all(
        ("vector" in e) or ("halfvec" in e) or ("vectors.chunks" in e)
        for e in errs2) if errs2 else True, "; ".join(errs2)[:300])

    with psycopg.connect(uri, autocommit=True) as conn:
        row = conn.execute(
            "SELECT count(*)::int FROM information_schema.tables "
            "WHERE table_name = 'tasks' AND table_schema = 'memory'"
        ).fetchone()
        check("memory.tasks создана", bool(row and row[0] == 1))
        row = conn.execute(
            "SELECT count(*)::int FROM information_schema.columns "
            "WHERE table_name = 'instances' AND table_schema = 'ops'"
        ).fetchone()
        check("ops.instances создана", bool(row and row[0] == 10))
        row = conn.execute(
            "SELECT count(*)::int FROM information_schema.columns "
            "WHERE table_name = 'tasks' AND table_schema = 'memory' "
            "AND column_name = 'tsv'"
        ).fetchone()
        check("memory.tasks.tsv есть (FTS)", bool(row and row[0] == 1))


# ═══════════════════════════════════════════════════════════════════════
# 4. Интеграция: прямая работа с пулом (ОДИН event loop)
# ═══════════════════════════════════════════════════════════════════════
async def integration_pool(uri: str) -> None:
    import psycopg

    from src.db.pool import DatabasePool
    from src.db.agent_memory import AgentMemory, AgentMemoryIndexer
    pool = DatabasePool(uri, min_size=1, max_size=4)
    await pool.start()

    try:
        print("[4] AgentMemory: запись/дедуп/поиск (FTS-режим)")
        am = AgentMemory(pool)
        caps = await am._capabilities()
        check("pgvector отсутствует → vector=False", caps["vector"] is False)
        check("tsv есть → tsv=True", caps["tsv"] is True)

        r1 = await am.upsert(
            "Настроить автоматические бэкапы PostgreSQL по расписанию",
            kind="note", source_key="note:backup")
        check("upsert вернул id", bool(r1["id"]))
        check("без pgvector embedded=False", r1["embedded"] is False)
        r2 = await am.upsert(
            "Настроить автоматические бэкапы PostgreSQL по расписанию",
            kind="note", source_key="note:backup")
        check("дедуп: тот же id", r2["id"] == r1["id"])
        st = await am.stats()
        check("stats: total=1", st["total"] == 1, str(st))
        check("stats: pgvector=False, fts=True",
              st["pgvector"] is False and st["fts"] is True)
        check("stats: embedder указан", bool(st["embedder"]))

        res = await am.search("бэкапы")
        check("search FTS находит", res["mode"] == "fts"
              and len(res["items"]) == 1, str(res["mode"]))
        res = await am.search("бэкапов")          # морфология
        check("search морфология (бэкапов)", len(res["items"]) == 1)
        res = await am.search("бэкапы", kind="chat")
        check("search kind-фильтр пуст", len(res["items"]) == 0)
        res = await am.search("бэкапы", kind="note")
        check("search kind-фильтр находит", len(res["items"]) == 1)
        res = await am.search("zzzнеНайдется")
        check("search пусто без ошибки", not res["items"], str(res))
        res = await am.search("")
        check("пустой запрос → none", res["mode"] == "none")

        # ILIKE-фолбэк (имитируем отсутствие tsv)
        am._flags = {"vector": False, "tsv": False}
        res = await am.search("бэкапы")
        check("ILIKE-фолбэк", res["mode"] == "ilike"
              and len(res["items"]) == 1, str(res["mode"]))
        am._flags = None

        rec = await am.recent(limit=10)
        check("recent", len(rec) == 1 and rec[0]["kind"] == "note")
        n = await am.clear(kind="chat")
        check("clear(kind='chat') не трогает note", n == 0)
        n = await am.clear(kind="note")
        check("clear(note) удаляет", n == 1)

        print("[5] backfill из зеркал")
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO ops.chat_sessions (session_id, message_count,"
                " last_role) VALUES ('s-mem-1', 2, 'assistant')")
            await cur.execute(
                "INSERT INTO ops.chat_messages (session_id, seq, role,"
                " agent, content, ts) VALUES "
                "('s-mem-1', 0, 'user', '', "
                "'Найди все упоминания бюджета в отчётах', now()), "
                "('s-mem-1', 1, 'assistant', 'file', "
                "'Прочитал отчёт, бюджет раздела равен 100000', now())")
            await cur.execute(
                "INSERT INTO ops.plans (plan_id, session_id, query, intent,"
                " status, updated_at, payload) VALUES"
                " ('p-mem-1', 's-mem-1', 'проанализировать бюджет',"
                " 'analysis', 'done', now(), '{}'::jsonb)")
            await cur.execute(
                "INSERT INTO journal.events_mirror (event_uid, seq, ts,"
                " kind, status, session_id, server_name, tool_name, error)"
                " VALUES"
                " ('ev-mem-1', 1, now(), 'tool_call', 'error', 's-mem-1',"
                " 'postgres', 'pg.query', 'connection refused'),"
                " ('ev-mem-2', 2, now(), 'tool_call', 'ok', 's-mem-1',"
                " 'filesystem', 'file.read', '')")
        bf = await am.backfill_from_mirrors(limit=100)
        check("backfill: чат 2", bf["chat"] == 2, str(bf))
        check("backfill: план 1", bf["plan"] == 1, str(bf))
        check("backfill: события с ошибками 1", bf["event"] == 1, str(bf))
        bf2 = await am.backfill_from_mirrors(limit=100)
        check("backfill идемпотентен", bf2["chat"] == 0 and bf2["plan"] == 0
              and bf2["event"] == 0, str(bf2))
        res = await am.search("бюджет")
        check("поиск находит чат из зеркал", len(res["items"]) >= 1
              and res["mode"] == "fts", str(res["mode"]))
        res = await am.search("connection")
        check("поиск находит ошибку события", len(res["items"]) >= 1)

        print("[6] AgentMemoryIndexer")
        indexer = AgentMemoryIndexer(am, interval=30, batch=50)
        res = await indexer.run_once()
        check("run_once без ошибок", "error" not in res, str(res))
        st = indexer.status()
        check("status: cycles=1", st["cycles"] == 1, str(st))

        print("[7] InstanceRegistry + analytics")
        from src.db.multi_instance import (InstanceRegistry, analytics)
        reg1 = InstanceRegistry(pool, instance_id="inst-alpha")
        await reg1.register({"role": "agent", "version": "2.0.0"})
        reg2 = InstanceRegistry(pool, instance_id="inst-beta")
        await reg2.register({"role": "agent"})
        await reg1.report_metrics({"agents": 36, "uptime_s": 60})
        rows = await reg1.list_active()
        ids = {r["instance_id"] for r in rows}
        check("list_active: оба", {"inst-alpha", "inst-beta"} <= ids,
              str(ids))
        check("метрики сохранились",
              any((r.get("metrics") or {}).get("agents") == 36
                  for r in rows))
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "UPDATE ops.instances SET last_heartbeat = "
                "now() - interval '1 hour' WHERE instance_id = 'inst-beta'")
        rows = await reg1.list_active()
        check("beta больше не активен",
              "inst-beta" not in {r["instance_id"] for r in rows})
        stale = await reg1.list_stale()
        check("beta в stale", "inst-beta" in
              {r["instance_id"] for r in stale})
        removed = await reg1.cleanup_stale(ttl_seconds=1800)
        check("cleanup удаляет beta", removed == 1, str(removed))
        ana = await analytics(pool, hours=24)
        check("analytics: events_total >= 2",
              ana.get("events_total", 0) >= 2, str(ana.get("events_total")))
        check("analytics: sessions/plans",
              ana.get("sessions_total", 0) >= 1
              and ana.get("plans_total", 0) >= 1)
        check("analytics: hourly список",
              isinstance(ana.get("hourly"), list))
        check("analytics: instances active_count",
              ana["instances"]["active_count"] >= 1)

        print("[8] PGRetention")
        from src.db.retention import PGRetention
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "INSERT INTO journal.events_mirror (event_uid, seq, ts,"
                " kind, status, error) VALUES"
                " ('ev-old-1', 1, now() - interval '400 days',"
                " 'tool_call', 'error', 'old error'),"
                " ('ev-new-1', 2, now(), 'tool_call', 'ok', '')")
            await cur.execute(
                "INSERT INTO ops.chat_messages (session_id, seq, role,"
                " content, ts) VALUES"
                " ('s-old', 0, 'user', 'старое сообщение',"
                " now() - interval '400 days')")
            await cur.execute(
                "INSERT INTO memory.tasks (kind, source_key, text,"
                " created_at, updated_at) VALUES"
                " ('note', 'note:old', 'древняя запись',"
                " now() - interval '400 days', now() - interval '400 days')")
        ret = PGRetention(pool, default_days=0)
        st = await ret.status(days=365)
        tbl = {t["table"]: t for t in st["tables"]}
        check("status: events_mirror deletable>=1",
              tbl["journal.events_mirror"]["deletable"] >= 1, str(tbl))
        check("status: chat_messages deletable>=1",
              tbl["ops.chat_messages"]["deletable"] >= 1)
        check("status: memory.tasks deletable>=1",
              tbl["memory.tasks"]["deletable"] >= 1)
        run0 = await ret.run(days=0)
        check("days=0 → отказ без удаления",
              run0["ok"] is False and run0["deleted_total"] == 0)
        dry = await ret.run(days=365, dry_run=True)
        check("dry_run ничего не удаляет", dry["deleted_total"] == 0
              and dry["ok"] is True, str(dry["deleted_total"]))
        real = await ret.run(days=365, dry_run=False)
        check("удалены старые: суммарно >=3",
              real["deleted_total"] >= 3, str(real["tables"]))
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*)::int AS c FROM journal.events_mirror "
                "WHERE event_uid = 'ev-old-1'")
            check("старое событие удалено",
                  (await cur.fetchone())["c"] == 0)
            await cur.execute(
                "SELECT count(*)::int AS c FROM journal.events_mirror "
                "WHERE event_uid = 'ev-new-1'")
            check("новое событие живо", (await cur.fetchone())["c"] == 1)
            await cur.execute(
                "SELECT count(*)::int AS c FROM ops.chat_messages "
                "WHERE session_id = 's-old'")
            check("старое сообщение удалено",
                  (await cur.fetchone())["c"] == 0)
            await cur.execute(
                "SELECT count(*)::int AS c FROM ops.chat_messages "
                "WHERE session_id = 's-mem-1'")
            check("свежие сообщения живы",
                  (await cur.fetchone())["c"] == 2)
    finally:
        await pool.stop()


# ═══════════════════════════════════════════════════════════════════════
# 5. Интеграция: фичи через FeatureLoader + TestClient
# ═══════════════════════════════════════════════════════════════════════
def integration_features(uri: str) -> None:
    print("[9] монтаж фич + эндпоинты")
    from src.core.features import FeatureLoader
    loader = FeatureLoader(BASE_DIR)
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    asyncio.run(loader.mount(app, {}))
    for fid in ("memory", "cluster", "ops"):
        entry = loader.registry.get(fid) or {}
        check(f"{fid}: api смонтирован", entry.get("api_mounted") is True,
              str(entry.get("error", "")))
        check(f"{fid}: js_url", entry.get("js_url")
              == f"/features/{fid}/ui.js", str(entry.get("js_url")))
    client = TestClient(app)

    mem_mod = _load_feature_module("memory")
    clu_mod = _load_feature_module("cluster")
    ops_mod = _load_feature_module("ops")

    r = client.get("/api/memory/stats")
    d = r.json()
    check("memory/stats 200", r.status_code == 200)
    check("memory/stats поля", d.get("total", 0) >= 4
          and d.get("pgvector") is False and d.get("fts") is True, str(d))
    r = client.post("/api/memory/search",
                    json={"query": "бюджет", "top_k": 5})
    d = r.json()
    check("memory/search 200 fts", r.status_code == 200
          and d.get("mode") == "fts" and len(d.get("items", [])) >= 1)
    r = client.post("/api/memory/search",
                    json={"query": "бюджет", "kind": "chat"})
    check("memory/search kind", len(r.json().get("items", [])) >= 1)
    r = client.get("/api/memory/recent", params={"limit": 5})
    check("memory/recent", r.status_code == 200
          and len(r.json().get("items", [])) >= 1)
    r = client.post("/api/memory/index/run", json={"limit": 50})
    d = r.json()
    check("memory/index/run 200 идемпотентно", r.status_code == 200
          and d.get("chat") == 0 and d.get("plan") == 0
          and d.get("event") == 0, str(d))
    r = client.request("DELETE", "/api/memory")
    check("clear без confirm → 400", r.status_code == 400)
    r = client.request("DELETE", "/api/memory?confirm=true&kind=event")
    d = r.json()
    check("clear(kind=event)", r.status_code == 200
          and d.get("deleted") == 1, str(d))

    r = client.get("/api/cluster/instances")
    d = r.json()
    check("cluster/instances 200", r.status_code == 200
          and d.get("active_count", 0) >= 1)
    r = client.get("/api/cluster/analytics", params={"hours": 24})
    d = r.json()
    check("cluster/analytics 200", r.status_code == 200
          and "instances" in d and d.get("events_total", 0) >= 1)
    r = client.post("/api/cluster/cleanup", params={"ttl": 21600})
    check("cluster/cleanup 200", r.status_code == 200
          and r.json().get("ok") is True)

    r = client.get("/api/ops-admin/overview")
    d = r.json()
    check("ops/overview 200", r.status_code == 200
          and d.get("pg", {}).get("available") is True
          and "backups" in d)
    r = client.get("/api/ops-admin/backups")
    check("ops/backups 200", r.status_code == 200
          and isinstance(r.json().get("items"), list))
    r = client.post("/api/ops-admin/backups")
    check("backup без confirm → 400", r.status_code == 400)
    r = client.get("/api/ops-admin/retention", params={"days": 365})
    d = r.json()
    check("ops/retention 200", r.status_code == 200
          and len(d.get("tables", [])) == 5, str(d)[:120])
    r = client.post("/api/ops-admin/retention/run",
                    json={"days": 365, "dry_run": False})
    check("retention run без confirm → 400", r.status_code == 400)
    r = client.post("/api/ops-admin/retention/run",
                    json={"days": 365, "dry_run": True})
    d = r.json()
    check("retention dry-run 200", r.status_code == 200
          and d.get("ok") is True and d.get("deleted_total") == 0)

    print("[10] деградация: PG недоступен (503)")
    for mod, path, method, kwargs in (
            (mem_mod, "/api/memory/stats", "GET", {}),
            (mem_mod, "/api/memory/search", "POST",
             {"json": {"query": "x"}}),
            (clu_mod, "/api/cluster/instances", "GET", {}),
            (clu_mod, "/api/cluster/analytics", "GET", {}),
            (ops_mod, "/api/ops-admin/retention", "GET",
             {"params": {"days": 365}}),
            (ops_mod, "/api/ops-admin/backups", "POST",
             {"params": {"confirm": "true"}}),
    ):
        orig = mod._dsn
        mod._dsn = lambda: "postgresql://llmagent:secret@127.0.0.1:59999/nd"
        mod._POOL = None
        app2 = FastAPI()
        app2.include_router(mod.create_router())
        c2 = TestClient(app2)
        r = c2.request(method, path, **kwargs)
        check(f"503: {path} [{method}]", r.status_code == 503,
              str(r.status_code))
        mod._dsn = orig
        mod._POOL = None

    # overview — особый контракт: сводка (с локальными бэкапами) отвечает
    # 200 даже без PG, но честно сообщает pg.available=false
    ops_mod._dsn = lambda: "postgresql://llmagent:secret@127.0.0.1:59999/nd"
    ops_mod._POOL = None
    app3 = FastAPI()
    app3.include_router(ops_mod.create_router())
    c3 = TestClient(app3)
    r = c3.get("/api/ops-admin/overview")
    d = r.json()
    check("overview без PG: 200 + available=false",
          r.status_code == 200
          and d.get("pg", {}).get("available") is False, str(d)[:120])
    ops_mod._POOL = None

    print("[11] CLI-экспорт не задет")
    p = subprocess.run(
        [sys.executable, str(BASE_DIR / "scripts" / "export_report.py"),
         "--events", "--hours", "24"],
        capture_output=True, text=True, timeout=120,
        env=dict(os.environ))
    check("export_report --events работает", p.returncode == 0,
          (p.stderr or p.stdout)[-160:])


def _load_feature_module(fid: str):
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        f"feat_it_{fid}_{time.time_ns()}",
        BASE_DIR / "features" / fid / "api.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ═══════════════════════════════════════════════════════════════════════
def main() -> int:
    t0 = time.time()
    unit_embedders()
    unit_misc()

    uri = ""
    try:
        import pgserver  # noqa
        print("[PG] поднимаю встроенный PostgreSQL (pgserver)...")
        PG_DATA_DIR = Path("/home/z/my-project/scripts/.pgserver_ops")
        PG_DATA_DIR.parent.mkdir(parents=True, exist_ok=True)
        if PG_DATA_DIR.exists():
            os.chmod(PG_DATA_DIR, 0o700)
        server = pgserver.get_server(str(PG_DATA_DIR))
        uri = server.get_uri()
        print(f"[PG] uri: {uri}")
    except Exception as e:
        print(f"[PG] pgserver недоступен ({e}) — интеграция пропущена")

    if uri:
        os.environ["DATABASE_URL"] = uri
        os.environ["PG_ENABLED"] = "true"
        integration_schema(uri)
        asyncio.run(integration_pool(uri))
        integration_features(uri)
    else:
        print("  (интеграция: SKIP)")

    dt = time.time() - t0
    print(f"\n{'=' * 60}")
    print(f"Итог: {PASSED} ok, {len(FAILED)} fail за {dt:.1f}s")
    if FAILED:
        print("Провалены:")
        for name in FAILED:
            print(f"  - {name}")
        return 1
    print("ВСЕ ПРОВЕРКИ ЗЕЛЁНЫЕ")
    return 0


if __name__ == "__main__":
    sys.exit(main())
