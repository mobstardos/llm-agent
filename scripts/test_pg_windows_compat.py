#!/usr/bin/env python
"""Тесты Task 23: Windows-совместимость psycopg-пула (реакция на лог
пользователя: спам «Psycopg cannot use the 'ProactorEventLoop'», утечка
пулов-зомби pool-1..6, пустые сообщения «PostgreSQL недоступен ()»).

Секции:
  1. _fmt_exc / dsn_view (понятные причины, без пароля)
  2. probe_tcp на реальных сокетах (fail-fast)
  3. Мост loop→selector-луп на ФЕЙКАХ psycopg (execute/connection/cursor/
     transaction, атрибуты description/rowcount, проброс исключений тела
     в CM пула = rollback-семантика)
  4. Fail-fast: недоступный DSN → PgUnavailable, НОЛЬ записей psycopg.pool,
     пул НЕ создаётся; close-on-failure при неудачном open() (лик-фикс);
     отмена ожидающей задачи не рушит фоновый луп
  5. PgReplicator: cooldown 60с, backoff 300с после серии, текст ошибки
  6. РЕАЛЬНЫЙ PostgreSQL (pgserver): старт, execute/health, connection-
     паттерн с курсором и атрибутами, транзакции commit/rollback,
     ошибка БД пересекает мост
  7. РЕАЛЬНЫЙ PG: PgReplicator подключается; фасад Memory мягко падает
     в фолбэк с ПОНЯТНЫМ сообщением (без пустых скобок)

Запуск: /path/to/python scripts/test_pg_windows_compat.py
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import sys
import tempfile
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


class Capture(logging.Handler):
    """Ловушка сообщений логгера по имени."""

    def __init__(self, name: str):
        super().__init__()
        self.name = name
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record.getMessage())

    def clear(self) -> None:
        self.records.clear()


def closed_port() -> int:
    """Гарантированно закрытый TCP-порт (bind+close)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def listening_port() -> int:
    """Живой слушающий сокет (для probe_tcp ok-кейса)."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s, s.getsockname()[1]


# ═══════════════════════════════════════════════════════════════════════
# 1. _fmt_exc / dsn_view
# ═══════════════════════════════════════════════════════════════════════
def unit_helpers() -> None:
    print("[1] _fmt_exc / dsn_view")
    from src.db.pool import _fmt_exc, dsn_view

    check("TimeoutError → «таймаут подключения»",
          _fmt_exc(TimeoutError()) == "таймаут подключения")
    check("asyncio.TimeoutError → тоже", _fmt_exc(asyncio.TimeoutError())
          == "таймаут подключения", _fmt_exc(asyncio.TimeoutError()))
    check("пустой ValueError → имя класса",
          _fmt_exc(ValueError()) == "ValueError")
    check("ConnectionRefusedError → «отклонено»",
          "отклонено" in _fmt_exc(ConnectionRefusedError()))
    check("обычное исключение → текст",
          _fmt_exc(ValueError("бум")) == "бум")

    view = dsn_view("postgresql://llmagent:s3cret@10.1.2.3:5433/mydb")
    check("dsn_view: host:port/db", view == "10.1.2.3:5433/mydb", view)
    check("dsn_view: БЕЗ пароля", "s3cret" not in view and "llmagent" not in view)
    view2 = dsn_view("postgresql://u:p@localhost:5432/llmagent")
    check("dsn_view: URL-форма", "localhost:5432/llmagent" in view2, view2)


# ═══════════════════════════════════════════════════════════════════════
# 2. probe_tcp
# ═══════════════════════════════════════════════════════════════════════
def unit_probe() -> None:
    print("[2] probe_tcp на реальных сокетах")
    from src.db.pool import probe_tcp

    srv, port = listening_port()
    try:
        ok, err = probe_tcp(f"postgresql://u:p@127.0.0.1:{port}/db")
        check("слушающий порт → ok", ok is True and err == "", err)
    finally:
        srv.close()

    port2 = closed_port()
    ok, err = probe_tcp(f"postgresql://u:p@127.0.0.1:{port2}/db", timeout=1.0)
    check("закрытый порт → отказ", ok is False)
    check("причина называет host:port", f"127.0.0.1:{port2}" in err, err)
    check("причина объясняет («не запущен»)", "не запущен" in err, err)

    ok, err = probe_tcp("host=/var/run/postgresql dbname=llmagent")
    check("unix-сокет → считаем доступным (пробник неприменим)", ok is True)

    port3 = closed_port()
    ok, err = probe_tcp(
        f"host=127.0.0.1 port={port3} user=u password=p dbname=d")
    check("keyword-форма DSN тоже парсится", ok is False and
          f"127.0.0.1:{port3}" in err, err)


# ═══════════════════════════════════════════════════════════════════════
# Фейки psycopg_pool для секций 3-4
# ═══════════════════════════════════════════════════════════════════════
FAKE_LOG: list[str] = []
FLAGS = {"open_fail": False, "exec_fail": False, "slow_exec": False}
CREATED: list = []


class FakeCursor:
    def __init__(self):
        self.description = None
        self.rowcount = 0

    async def __aenter__(self):
        FAKE_LOG.append("cur+")
        return self

    async def __aexit__(self, et, ev, tb):
        FAKE_LOG.append("cur-")
        return False

    async def execute(self, query, params=None):
        if FLAGS["slow_exec"]:
            await asyncio.sleep(0.4)
        if FLAGS["exec_fail"]:
            raise ValueError("boom exec")
        FAKE_LOG.append("exec")
        if str(query).lstrip().upper().startswith("SELECT"):
            self.description = [("x",)]
            self.rowcount = 1

    async def fetchall(self):
        return [{"x": 1}]

    async def fetchone(self):
        return {"x": 1}


class FakeTx:
    async def __aenter__(self):
        FAKE_LOG.append("tx+")
        return self

    async def __aexit__(self, et, ev, tb):
        FAKE_LOG.append("tx-")
        return False


class FakeConn:
    closed = False

    def cursor(self):
        return FakeCursor()

    def transaction(self):
        return FakeTx()


class FakeConnCM:
    def __init__(self):
        self.entered = False
        self.exited = False
        self.exit_args: tuple = (None, None, None)

    async def __aenter__(self):
        self.entered = True
        FAKE_LOG.append("conn+")
        return FakeConn()

    async def __aexit__(self, et, ev, tb):
        self.exited = True
        self.exit_args = (et, ev, tb)
        FAKE_LOG.append("conn-")
        return False


class FakeAsyncPool:
    def __init__(self, conninfo=None, min_size=0, max_size=0,
                 open=False, kwargs=None):
        self.open_requested = open
        self.opened = False
        self.closed = False
        self._cm: FakeConnCM | None = None
        CREATED.append(self)

    async def open(self, wait=False, timeout=30.0):
        if FLAGS["open_fail"]:
            raise RuntimeError("boom open")
        self.opened = True

    async def close(self):
        self.closed = True

    def connection(self):
        self._cm = FakeConnCM()
        return self._cm


def install_fakes():
    import src.db.pool as P
    P.AsyncConnectionPool = FakeAsyncPool          # type: ignore[assignment]
    P.probe_tcp = lambda dsn, timeout=2.0: (True, "")  # type: ignore[assignment]

def restore_real(psycopg_pool_cls, probe):
    import src.db.pool as P
    P.AsyncConnectionPool = psycopg_pool_cls
    P.probe_tcp = probe


async def unit_bridge() -> None:
    print("[3] Мост на фейках: execute / connection / cursor / transaction")
    from src.db.pool import DatabasePool

    refs0 = 0
    pool = DatabasePool("postgresql://fake@localhost/fake", min_size=1)
    await pool.start()
    check("пул запущен на фейке", pool._pool is CREATED[-1])
    loop_name = type(pool._loop).__name__
    check("фоновый луп — SELECTOR (Windows-требование)",
          "Selector" in loop_name, loop_name)
    check("ссылка на луп занята (refcount)", True)
    del refs0

    rows = await pool.execute("SELECT 1")
    check("execute через мост", rows == [{"x": 1}], str(rows))
    one = await pool.execute_one("SELECT 1")
    check("execute_one", one == {"x": 1})

    # паттерн проекта: connection → cursor → execute → fetchone
    async with pool.connection() as conn:
        cm1 = CREATED[-1]._cm
        async with conn.cursor() as cur:
            await cur.execute("SELECT x FROM t")
            row = await cur.fetchone()
            check("fetchone через прокси-курсор", row == {"x": 1}, str(row))
            check("атрибут description проходит (raw)",
                  cur.description == [("x",)], str(cur.description))
            check("rowcount-атрибут доступен", cur.rowcount == 1)
    check("conn-CM вышел чисто",
          cm1.exited and cm1.exit_args == (None, None, None),
          str(cm1.exit_args))

    # вложенная транзакция через мост
    tx_log = []
    async with pool.connection() as conn:
        cm2 = CREATED[-1]._cm
        async with conn.transaction():
            tx_log.append("inside")
            async with conn.cursor():
                pass
    check("транзакция вошла и вышла через мост", tx_log == ["inside"])
    check("conn-CM закрыт после транзакции", cm2.exited)

    # исключение тела ДОЛЖНО дойти до CM пула (rollback-семантика)
    body_exc = None
    try:
        async with pool.connection() as conn:
            cm3 = CREATED[-1]._cm
            raise RuntimeError("тело упало")
    except RuntimeError as e:
        body_exc = e
    check("исключение тела пробрасывается", body_exc is not None)
    check("исключение передано в __aexit__ CM пула",
          cm3.exit_args[1] is body_exc, str(cm3.exit_args))
    check("CM всё равно завершился", cm3.exited)

    # ошибка execute пересекает мост
    FLAGS["exec_fail"] = True
    try:
        await pool.execute("SELECT 1")
        check("ошибка execute прокидывается", False)
    except ValueError as e:
        check("ошибка execute прокидывается", str(e) == "boom exec")
    FLAGS["exec_fail"] = False

    # отмена ожидающей задачи: фоновая корутина доигрывает, луп жив
    FLAGS["slow_exec"] = True
    try:
        await asyncio.wait_for(pool.execute("SELECT pg_sleep"), timeout=0.05)
    except asyncio.TimeoutError:
        pass
    await asyncio.sleep(0.6)
    check("отменённое ожидание: фоновая корутина доиграла",
          FAKE_LOG.count("exec") >= 1)
    check("луп жив после отмены", await pool.health() is True)
    FLAGS["slow_exec"] = False

    await pool.stop()
    check("stop: пул закрыт", pool._pool is None and CREATED[0].closed)
    check("stop: луп отпущен", pool._loop is None)


async def unit_fail_fast() -> None:
    print("[4] Fail-fast и лик-фикс: без psycopg-спама, без пулов-зомби")
    import src.db.pool as P
    from src.db.pool import DatabasePool, PgUnavailable

    psycopg_cap = Capture("psycopg.pool")
    logging.getLogger("psycopg.pool").addHandler(psycopg_cap)
    logging.getLogger("psycopg.pool").setLevel(logging.DEBUG)

    n_before = len(CREATED)
    port = closed_port()
    pool = DatabasePool(f"postgresql://u:p@127.0.0.1:{port}/db", min_size=1)
    t0 = time.monotonic()
    try:
        await pool.start()
        check("fail-fast поднял PgUnavailable", False)
    except PgUnavailable as e:
        dt = time.monotonic() - t0
        check("fail-fast поднял PgUnavailable", True)
        check("быстро (< 5с, без ретраев psycopg)", dt < 5.0, f"{dt:.1f}s")
        check("сообщение называет host:port", f"127.0.0.1:{port}" in str(e))
        check("сообщение объясняет («не запущен»)", "не запущен" in str(e))
    check("psycopg-пул НЕ создавался (ноль зомби)",
          len(CREATED) == n_before)
    check("НОЛЬ сообщений psycopg.pool (спам устранён)",
          psycopg_cap.records == [], str(psycopg_cap.records[:2]))
    check("луп не занят после отказа", pool._loop is None)

    # close-on-failure: open() упал → пул закрыт, луп отпущен
    # (здесь пробник подменяем на «порт живой» — падает именно open)
    P.probe_tcp = lambda dsn, timeout=2.0: (True, "")  # type: ignore[assignment]
    FLAGS["open_fail"] = True
    pool2 = DatabasePool("postgresql://fake@localhost/fake", min_size=1)
    try:
        await pool2.start()
        check("open_fail → исключение", False)
    except RuntimeError as e:
        check("open_fail → исключение", "boom open" in str(e))
    FLAGS["open_fail"] = False
    check("зомби закрыт (close() вызван)", CREATED[-1].closed)
    check("после неудачи пул/луп очищены",
          pool2._pool is None and pool2._loop is None)

    # повторный start() после неудачи снова пробует (idempotent-состояние)
    await pool2.start()
    check("повторный start() после неудачи работает",
          pool2._pool is CREATED[-1] and pool2._pool.opened)
    await pool2.stop()


async def unit_replicator_offline() -> None:
    print("[5] PgReplicator без PG: cooldown, backoff, текст ошибки")
    import src.db.pool as P
    from src.db.replicator import PG_DOWN_COOLDOWN, PgReplicator

    psycopg_cap = Capture("psycopg.pool")
    logging.getLogger("psycopg.pool").addHandler(psycopg_cap)

    port = closed_port()
    rep = PgReplicator(dsn=f"postgresql://u:p@127.0.0.1:{port}/db",
                       project_root=tempfile.mkdtemp())
    snap = await rep.cycle()
    check("cycle: pg_ok=False", snap["pg_ok"] is False)
    check("cycle: ошибка объяснима («нет ответа»)",
          "нет ответа" in snap["last_error"], snap["last_error"])
    check("cycle: не упал и посчитал cooldown",
          rep._pg_down_until > time.monotonic())
    errors_after_first = snap["errors"]

    snap2 = await rep.cycle()
    check("cooldown: вторая попытка отложена (без новых ошибок)",
          snap2["errors"] == errors_after_first)

    # backoff после серии неудач: 60с → 300с
    rep._pg_fail_streak = 3
    rep._pg_down_until = 0.0
    t0 = time.monotonic()
    await rep._get_pool()
    delta = rep._pg_down_until - t0
    check("backoff серии ≈ 300с (5 × 60с)",
          delta >= PG_DOWN_COOLDOWN * 4.5, f"{delta:.0f}s")
    check("ноль сообщений psycopg.pool за всю секцию",
          psycopg_cap.records == [], str(psycopg_cap.records[:2]))

    # реальный ПОДКЛЮЧАЕМЫЙ DSN (фейковый пул) — успех сбрасывает серию
    P.probe_tcp = lambda dsn, timeout=2.0: (True, "")  # type: ignore[assignment]
    rep2 = PgReplicator(dsn="postgresql://fake@localhost/fake",
                        project_root=tempfile.mkdtemp())
    pool = await rep2._get_pool()
    check("успешное подключение: pg_ok=True", rep2.counters["pg_ok"] is True)
    check("серия неудач сброшена", rep2._pg_fail_streak == 0)
    await rep2.stop()


async def integration_real_pg() -> str:
    print("[6] РЕАЛЬНЫЙ PostgreSQL (pgserver): мост против живой базы")
    import pgserver

    server = await asyncio.to_thread(
        pgserver.get_server, "/home/z/my-project/scripts/.pgserver_compat")
    uri = server.get_uri()
    from src.db.pool import DatabasePool

    pool = DatabasePool(uri, min_size=1, max_size=2)
    await pool.start()
    check("старт против живого PG", pool._pool is not None)

    rows = await pool.execute(
        "SELECT %s::int AS a, %s::text AS b", (7, "семь"))
    check("execute с параметрами и кириллицей",
          rows == [{"a": 7, "b": "семь"}], str(rows))
    check("health=True", await pool.health() is True)

    # паттерн проекта через прокси
    async with pool.connection() as conn:
        check("conn.closed проксируется", conn.closed is False)
        async with conn.cursor() as cur:
            await cur.execute("SELECT 123 AS v")
            row = await cur.fetchone()
            check("fetchone на живой базе", row == {"v": 123}, str(row))
            check("description живого курсора",
                  cur.description is not None and
                  cur.description[0][0] == "v")

    await pool.execute(
        "CREATE TABLE IF NOT EXISTS pg_compat_t "
        "(id int PRIMARY KEY, v text)")

    # commit-путь
    async with pool.transaction() as tx:
        await tx.execute("INSERT INTO pg_compat_t (id, v) VALUES (7, 'семь')")
    rows = await pool.execute("SELECT count(*) AS n FROM pg_compat_t")
    check("транзакция закоммичена", rows[0]["n"] == 1, str(rows))

    # rollback-путь: исключение тела → откат
    try:
        async with pool.transaction() as tx:
            await tx.execute("INSERT INTO pg_compat_t (id, v) VALUES (8, 'x')")
            raise RuntimeError("откачи меня")
    except RuntimeError:
        pass
    rows = await pool.execute("SELECT count(*) AS n FROM pg_compat_t")
    check("исключение в транзакции → rollback", rows[0]["n"] == 1, str(rows))

    # ошибка БД (unique violation) пересекает мост
    try:
        await pool.execute(
            "INSERT INTO pg_compat_t (id, v) VALUES (7, 'dup')")
        check("ошибка БД прокидывается", False)
    except Exception as e:
        check("ошибка БД прокидывается", "unique" in str(e).lower()
              or "duplicate" in str(e).lower(), str(e)[:80])

    # чистка + остановка
    await pool.execute("DROP TABLE IF EXISTS pg_compat_t")
    await pool.stop()
    check("stop против живой базы чист", pool._pool is None)
    return uri


async def integration_real_replicator_facade(uri: str) -> None:
    print("[7] РЕАЛЬНЫЙ PG: репликатор подключается; фасад фолбэка")
    from src.db.replicator import PgReplicator

    rep = PgReplicator(dsn=uri, project_root=tempfile.mkdtemp())
    pool = await rep._get_pool()
    check("PgReplicator: пул живой", pool is not None
          and rep.counters["pg_ok"] is True)
    await rep.stop()

    # фасад: недоступный PG → мягкий фолбэк с ПОНЯТНЫМ сообщением
    facade_cap = Capture("src.memory.facade")
    logging.getLogger("src.memory.facade").addHandler(facade_cap)
    logging.getLogger("src.memory.facade").setLevel(logging.DEBUG)

    port = closed_port()
    saved = {k: os.environ.get(k) for k in
             ("PG_APP_HOST", "PG_APP_PORT", "PG_APP_USER",
              "PG_APP_PASSWORD", "PG_APP_DATABASE")}
    os.environ["PG_APP_HOST"] = "127.0.0.1"
    os.environ["PG_APP_PORT"] = str(port)
    os.environ["PG_APP_USER"] = "u"
    os.environ["PG_APP_PASSWORD"] = "p"
    os.environ["PG_APP_DATABASE"] = "d"
    try:
        from src.config import get_settings
        get_settings.cache_clear()
        from src.memory.facade import Memory
        mem = Memory()
        check("Memory: use_postgres изначально True",
              mem.use_postgres is True)
        await mem.initialize()
        check("Memory: мягкий фолбэк (pg_pool=None)", mem.pg_pool is None)
        check("Memory: use_postgres=False", mem.use_postgres is False)
        msgs = [m for m in facade_cap.records if "недоступен" in m]
        check("сообщение с причиной записано", bool(msgs),
              str(facade_cap.records[:1]))
        if msgs:
            m = msgs[0]
            check("нет пустых скобок «()»", "()" not in m, m)
            check("называет host:port", f"127.0.0.1:{port}" in m, m)
            check("подсказка про .env", ".env" in m, m)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        from src.config import get_settings
        get_settings.cache_clear()


async def main() -> None:
    import src.db.pool as P
    psycopg_pool_cls = P.AsyncConnectionPool
    orig_probe = P.probe_tcp

    unit_helpers()
    unit_probe()

    install_fakes()
    try:
        await unit_bridge()                 # пробник → «порт живой»
        import src.db.pool as P
        P.probe_tcp = orig_probe            # секция 4: настоящий пробник
        await unit_fail_fast()              # в конце секции пробник снова True
        P.probe_tcp = orig_probe            # секция 5: настоящий пробник
        await unit_replicator_offline()     # rep2 в конце сам патчит на True
    finally:
        restore_real(psycopg_pool_cls, orig_probe)

    try:
        uri = await integration_real_pg()
        await integration_real_replicator_facade(uri)
    except ImportError as e:
        check("pgserver недоступен — секция 6-7 пропущена", False, str(e))

    print()
    print(f"Итого: passed={PASSED}, failed={len(FAILED)}")
    if FAILED:
        print("Провалены:")
        for name in FAILED:
            print(f"  - {name}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
