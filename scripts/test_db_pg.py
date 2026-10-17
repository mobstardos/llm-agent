#!/usr/bin/env python
"""Тесты PostgreSQL-хранилища: init_db, репликатор, зеркала.

Юнит-часть выполняется всегда (без БД).
Интеграционная часть поднимает встроенный PostgreSQL (pip-пакет pgserver);
если пакет недоступен — интеграция мягко пропускается.

Запуск:
    /path/to/venv/python scripts/test_db_pg.py
"""
from __future__ import annotations

import asyncio
import json
import shutil
import sys
import time
import uuid
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

WORK_DIR = Path("/home/z/my-project/scripts/.pg_test_data")
PG_DATA_DIR = Path("/home/z/my-project/scripts/.pgserver_data")

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
# 1. Юнит: разбор SQL
# ═══════════════════════════════════════════════════════════════════════
def unit_split_statements() -> None:
    print("[1] split_statements")
    from src.db.init_db import split_statements

    sql = """
    -- комментарий; с точкой с запятой
    CREATE TABLE a (x text);  /* блок /* вложенный */ ; не SQL */
    INSERT INTO a VALUES ('строка; с точкой'), ("идент;"), (''';''');
    DO $$
    BEGIN
        EXECUTE format('CREATE TABLE %s_2025 PARTITION OF a', 'm');
    END $$;
    CREATE FUNCTION f() RETURNS void AS $body$ BEGIN PERFORM 1; END $body$
    LANGUAGE plpgsql;
    """
    stmts = split_statements(sql)
    check("число statements", len(stmts) == 5, f"got {len(stmts)}: {stmts}")
    check("DO-блок цел", any("EXECUTE format" in s for s in stmts))
    check("$body$ функция цел",
          any("PERFORM 1" in s and "plpgsql" in s for s in stmts))
    check("строка с ; не разрезана",
          any("'строка; с точкой'" in s for s in stmts))

    # Реальные файлы проекта разбираются без мусора
    for fname in ("init.sql", "journal.sql", "ops.sql"):
        text = (BASE_DIR / "db" / fname).read_text(encoding="utf-8")
        parts = split_statements(text)
        check(f"{fname}: statements >= 10", len(parts) >= 10, str(len(parts)))
        check(f"{fname}: нет пустых", all(p.strip() for p in parts))
    # DO-блоки с партициями есть только в init.sql и journal.sql
    for fname in ("init.sql", "journal.sql"):
        text = (BASE_DIR / "db" / fname).read_text(encoding="utf-8")
        parts = split_statements(text)
        check(f"{fname}: нет 'липших' разрезов в DO",
              sum(1 for p in parts if "EXECUTE format" in p) >= 1)


# ═══════════════════════════════════════════════════════════════════════
# 2. Юнит: маппинги репликатора
# ═══════════════════════════════════════════════════════════════════════
def unit_replicator_helpers() -> None:
    print("[2] репликатор: хелперы")
    from src.db.replicator import (
        PgReplicator, _js, _to_uuid, classify_action,
    )

    check("classify delete", classify_action("filesystem", "delete_file") == "delete")
    check("classify patch", classify_action("filesystem", "apply_patch") == "patch")
    check("classify read", classify_action("filesystem", "read_file") == "read")
    check("classify query", classify_action("mysql", "execute") == "query")
    check("classify external", classify_action("http", "get") == "external")
    check("classify fallback write", classify_action("x", "create_file") == "write")

    check("_to_uuid ok", _to_uuid("6f9619ff-8b86-d011-b42d-00c04fc964ff"))
    check("_to_uuid None", _to_uuid("web-afddc401") is None)
    check("_to_uuid пусто", _to_uuid("") is None)

    check("_js dict", json.loads(_js({"a": 1})) == {"a": 1})
    check("_js None", _js(None) == "{}")
    check("_js цикличный объект", isinstance(_js(object()), str))

    rep = PgReplicator(
        project_root=str(WORK_DIR),
        journal_dir=WORK_DIR / "j", sessions_dir=WORK_DIR / "s",
        plans_dir=WORK_DIR / "p", dsn="postgresql://x:y@127.0.0.1:1/db",
    )
    check("каталоги по умолчанию от project_root",
          rep.journal_dir == WORK_DIR / "j" and rep.plans_dir == WORK_DIR / "p")


# ═══════════════════════════════════════════════════════════════════════
# 3. Юнит: чтение хвоста jsonl
# ═══════════════════════════════════════════════════════════════════════
def unit_session_tail() -> None:
    print("[3] чтение хвоста jsonl")
    from src.db.replicator import PgReplicator

    rep = PgReplicator(dsn="postgresql://x@127.0.0.1:1/db")
    f = WORK_DIR / "s" / "tail.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)

    # Полные строки + plan-записи
    f.write_text(
        '{"role": "user", "content": "привет", "agent": "", "ts": 100.0}\n'
        '{"type": "plan", "plan": {"intent": "x"}}\n'
        '{"role": "assistant", "content": "ответ", "agent": "file", "ts": 101.0}\n',
        encoding="utf-8",
    )
    off, msgs, plans, last_role = rep._read_session_tail(f, 0)
    check("2 сообщения", len(msgs) == 2, str(len(msgs)))
    check("1 plan-запись", plans == 1)
    check("last_role", last_role == "assistant")
    check("offset == размер файла", off == f.stat().st_size, f"{off} != {f.stat().st_size}")

    # Хвост без \n: валидный JSON — потребляется
    with open(f, "ab") as fh:
        fh.write(b'{"role": "user", "content": "no-newline", "ts": 102.0}')
    off2, msgs2, _, _ = rep._read_session_tail(f, off)
    check("хвост без \\n потреблён", len(msgs2) == 1 and off2 == f.stat().st_size)

    # Хвост без \n: НЕвалидный (частичная) — ждём дозапись
    with open(f, "ab") as fh:
        fh.write(b'{"role": "user", "content": "part')
    off3, msgs3, _, _ = rep._read_session_tail(f, off2)
    check("частичная строка не съедена", len(msgs3) == 0 and off3 == off2)

    # Дозапись строки → съедается целиком
    with open(f, "ab") as fh:
        fh.write(b'ial", "ts": 103.0}\n')
    off4, msgs4, _, _ = rep._read_session_tail(f, off2)
    check("дозапись доесть", len(msgs4) == 1 and off4 == f.stat().st_size)

    # Битая строка посреди файла — стоп на ней
    f2 = WORK_DIR / "s" / "broken.jsonl"
    f2.write_text(
        '{"role": "user", "content": "a", "ts": 1.0}\n'
        'NOT JSON AT ALL\n'
        '{"role": "user", "content": "b", "ts": 2.0}\n',
        encoding="utf-8",
    )
    offb, msgsb, _, _ = rep._read_session_tail(f2, 0)
    check("битая строка: только 1 сообщение до неё", len(msgsb) == 1)
    check("битая строка: offset на начале строки",
          offb == len('{"role": "user", "content": "a", "ts": 1.0}\n', 'utf-8')
          if False else offb == f2.read_text(encoding='utf-8').index('NOT JSON'))


# ═══════════════════════════════════════════════════════════════════════
# 4. Интеграция: реальный PostgreSQL (pgserver)
# ═══════════════════════════════════════════════════════════════════════
async def integration(uri: str) -> None:
    print("[4] интеграция: init_db на реальном PG")
    from src.db.init_db import check_schema, init_schema

    reports, ok = init_schema(uri)
    executed = sum(r["executed"] for r in reports)
    errors = sum(len(r["errors"]) for r in reports)
    skipped = sum(len(r["skipped"]) for r in reports)
    check("init_schema: ok", ok, str(reports))
    check("init_schema: нет ошибок", errors == 0, str(errors))
    print(f"       выполнено={executed}, пропущено (расширения)={skipped}")
    # pgvector в pgserver нет — его statements должны быть пропущены мягко
    if skipped:
        print("       (ожидаемо: pgvector отсутствует в embedded PG)")

    info = check_schema(uri)
    check("check_schema: ok", info["ok"], str(info["schemas"]))
    check("схема ops создана", info["schemas"].get("ops") not in (None, 0))
    check("схема journal создана", info["schemas"].get("journal") not in (None, 0))
    check("повторный init идемпотентен", init_schema(uri)[1] is True)

    print("[5] интеграция: репликатор журнала")
    from src.journal.config import JournalConfig
    from src.journal.schema import Event, EventKind, EventStatus
    from src.journal.store import JournalStore
    from src.db.replicator import PgReplicator

    jdir = WORK_DIR / "journal"
    cfg = JournalConfig(base_dir=jdir)
    store = JournalStore(cfg)
    sids = ["6f9619ff-8b86-d011-b42d-00c04fc964ff", "websess-не-uuid"]
    for i in range(5):
        ev = Event(
            kind=EventKind.TOOL_CALL if i % 2 == 0 else EventKind.TASK,
            server_name="filesystem" if i % 2 == 0 else "",
            tool_name="write_file" if i % 2 == 0 else "",
            action="write_file" if i % 2 == 0 else "task",
            agent_id="file" if i % 2 == 0 else "",
            session_id=sids[i % 2],
            trace_id=f"trace-{i}",
            args={"path": f"f{i}.txt", "content": "x" * 100},
            targets=[f"f{i}.txt"],
            reversible=1 if i % 2 == 0 else 0,
            duration_ms=10.5 * i,
            status=EventStatus.OK if i != 3 else EventStatus.ERROR,
            error="oops" if i == 3 else "",
        )
        store.insert(ev)

    rep = PgReplicator(
        dsn=uri, journal_dir=jdir,
        sessions_dir=WORK_DIR / "sessions", plans_dir=WORK_DIR / "plans",
        interval_seconds=3600,
    )
    st = await rep.cycle()
    check("cycle: pg_ok", st["pg_ok"] is True)
    check("журнал: 5 событий перенесено", st["journal_events"] == 5, str(st))
    check("журнал: 3 проекции actions (tool_call)", st["journal_actions"] == 3)

    from src.db.pool import DatabasePool
    pool = DatabasePool(uri, min_size=1, max_size=2)
    await pool.start()
    rows = await pool.execute("SELECT count(*) c FROM journal.events_mirror")
    check("events_mirror: 5 строк", rows[0]["c"] == 5, str(rows))
    acts = await pool.execute(
        "SELECT count(*) c, count(*) FILTER (WHERE reversible) rev "
        "FROM journal.actions")
    check("actions: 3 строки", acts[0]["c"] == 3, str(acts))
    check("actions: reversible=True у write_file",
          acts[0]["rev"] == 3, str(acts))
    cur = await pool.execute("SELECT value FROM ops.sync_state WHERE key='journal'")
    check("курсор journal записан", cur and cur[0]["value"].get("last_id") == 5)

    # Идемпотентность: новый репликатор (курсор только в PG) — 0 новых
    rep2 = PgReplicator(
        dsn=uri, journal_dir=jdir,
        sessions_dir=WORK_DIR / "sessions", plans_dir=WORK_DIR / "plans",
        interval_seconds=3600,
    )
    st2 = await rep2.cycle()
    check("рестарт: курсор из PG, ничего не задублировано",
          st2["journal_events"] == 0 and st2["journal_actions"] == 0, str(st2))

    # Дозапись событий → догружаются только новые
    ev6 = Event(kind=EventKind.TOOL_CALL, server_name="git", tool_name="commit",
                session_id=sids[0], args={"m": "msg"}, targets=["repo"])
    store.insert(ev6)
    st3 = await rep2.cycle()
    check("догрузка: только новое событие", st3["journal_events"] == 1, str(st3))

    print("[6] интеграция: чат-сессии и планы")
    sdir = WORK_DIR / "sessions"
    pdir = WORK_DIR / "plans"
    sdir.mkdir(parents=True, exist_ok=True)
    pdir.mkdir(parents=True, exist_ok=True)
    sf = sdir / "sess01.jsonl"
    sf.write_text(
        '{"role": "user", "content": "вопрос", "agent": "", "ts": 1700000000.0}\n'
        '{"role": "assistant", "content": "ответ", "agent": "file", "ts": 1700000005.0}\n',
        encoding="utf-8",
    )
    pf = pdir / "plan-1.json"
    pf.write_text(json.dumps({
        "plan_id": "plan-1", "session_id": "sess01", "query": "сделай",
        "intent": "file", "mode": "sequential", "status": "running",
        "needs_approval": False, "success": None, "replans": 0,
        "steps": [
            {"id": "1", "agent": "file", "task": "прочитать", "status": "done"},
            {"id": "2", "agent": "git", "task": "закоммитить", "status": "pending"},
        ],
        "created_at": time.time(), "updated_at": time.time(),
    }, ensure_ascii=False), encoding="utf-8")

    st4 = await rep2.cycle()
    check("сообщения: 2 перенесено", st4["messages"] == 2, str(st4))
    msgs = await pool.execute(
        "SELECT count(*) c FROM ops.chat_messages WHERE session_id='sess01'")
    check("ops.chat_messages: 2", msgs[0]["c"] == 2, str(msgs))
    sess = await pool.execute(
        "SELECT message_count, last_role FROM ops.chat_sessions WHERE session_id='sess01'")
    check("ops.chat_sessions: count=2, last_role=assistant",
          sess and sess[0]["message_count"] == 2
          and sess[0]["last_role"] == "assistant", str(sess))
    plans_rows = await pool.execute("SELECT steps_done, steps_failed, status FROM ops.plans WHERE plan_id='plan-1'")
    check("ops.plans: шаги посчитаны",
          plans_rows and plans_rows[0]["steps_done"] == 1
          and plans_rows[0]["steps_failed"] == 0, str(plans_rows))

    # Апдейт плана и дозапись чата → только изменения
    time.sleep(0.02)  # mtime
    data = json.loads(pf.read_text(encoding="utf-8"))
    data["status"] = "done"
    data["success"] = True
    data["steps"][1]["status"] = "done"
    data["updated_at"] = time.time()
    pf.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    with open(sf, "a", encoding="utf-8") as fh:
        fh.write('{"role": "user", "content": "спасибо", "ts": 1700000010.0}\n')

    st5 = await rep2.cycle()
    check("дозапись чата: +1 сообщение", st5["messages"] - st4["messages"] == 1,
          str(st5))
    plans_rows = await pool.execute("SELECT status, success, steps_done FROM ops.plans WHERE plan_id='plan-1'")
    check("план обновлён (upsert)",
          plans_rows[0]["status"] == "done" and plans_rows[0]["success"] is True
          and plans_rows[0]["steps_done"] == 2, str(plans_rows))
    msgs = await pool.execute("SELECT count(*) c FROM ops.chat_messages WHERE session_id='sess01'")
    check("всего сообщений 3", msgs[0]["c"] == 3, str(msgs))

    # Симуляция простоя БД: недоступный порт — cycle мягко деградирует
    print("[7] интеграция: деградация при недоступном PG")
    rep_bad = PgReplicator(
        dsn="postgresql://llmagent:secret@127.0.0.1:59999/llmagent",
        journal_dir=jdir, sessions_dir=sdir, plans_dir=pdir,
        interval_seconds=3600,
    )
    t0 = time.monotonic()
    st6 = await rep_bad.cycle()
    dt = time.monotonic() - t0
    check("cycle не упал", isinstance(st6, dict))
    check("pg_ok=False", st6["pg_ok"] is False)
    check("ошибка зафиксирована", st6["errors"] >= 1, str(st6))
    check("быстрый отказ (<15с, cooldown работает)", dt < 15, f"{dt:.1f}s")
    # второй цикл сразу — cooldown не даёт повторных попыток
    st7 = await rep_bad.cycle()
    check("cooldown: повторных подключений нет",
          st7["errors"] == st6["errors"], f"{st6['errors']} vs {st7['errors']}")

    await pool.stop()


def main() -> int:
    t0 = time.time()
    # Чистое состояние: и рабочие каталоги, и данные встроенного PG
    shutil.rmtree(PG_DATA_DIR, ignore_errors=True)
    if WORK_DIR.exists():
        shutil.rmtree(WORK_DIR)
    WORK_DIR.mkdir(parents=True, exist_ok=True)

    try:
        unit_split_statements()
        unit_replicator_helpers()
        unit_session_tail()
    except Exception as e:
        import traceback
        traceback.print_exc()
        FAILED.append(f"юнит-блок упал: {e}")

    # Интеграция — только если есть реальный PG (pgserver)
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
            asyncio.run(integration(uri))
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
            shutil.rmtree(WORK_DIR, ignore_errors=True)
            shutil.rmtree(PG_DATA_DIR, ignore_errors=True)

    print(f"\n{'=' * 60}")
    print(f"Пройдено: {PASSED}, провалено: {len(FAILED)} "
          f"({time.time() - t0:.1f}s)")
    for name in FAILED:
        print(f"  FAIL: {name}")
    return 0 if not FAILED else 1


if __name__ == "__main__":
    raise SystemExit(main())
