"""Тесты Task 24-b — PostgreSQL-автодетект при каждом запуске.

Запуск:  python tests/test_pg_autodetect_24b.py
         python -m pytest tests/test_pg_autodetect_24b.py -q

Без реального PostgreSQL: соединения эмулируются подменой _connect /
verify_dsn / find_service; bootstrap — фейковым коннектом с памятью
pg_roles/pg_database. Дополнительно — статическая идемпотентность
db/*.sql (CREATE TABLE/VIEW только с IF NOT EXISTS или после DROP IF EXISTS),
которая гарантирует безопасность повторных прогонов scripts/init_db.py.
"""
from __future__ import annotations

import os
import re
import socket
import sys
import tempfile
import threading
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

PASS, FAIL = [], []


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    mark = "✓" if cond else "✗"
    print(f"  {mark} {name}" + (f" — {detail}" if detail and not cond else ""))


class _Monkey:
    def __init__(self):
        self._saved: dict[str, str | None] = {}

    def setenv(self, key: str, val: str) -> None:
        if key not in self._saved:
            self._saved[key] = os.environ.get(key)
        os.environ[key] = val

    def undo(self) -> None:
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self._saved.clear()


MONKEY = _Monkey()


# ═══════════════════════════════════════════════════════════════════════
# 1. Чистые функции
# ═══════════════════════════════════════════════════════════════════════
def test_pure() -> None:
    print("── pure utils ──")
    from src.db.autodetect import (classify_error, is_credential_error,
                                   is_local_host, mask_dsn, probe_port)

    check("mask_dsn прячет пароль",
          mask_dsn("postgresql://postgres:secret@localhost:5432/db")
          == "postgresql://postgres:***@localhost:5432/db",
          mask_dsn("postgresql://postgres:secret@localhost:5432/db"))
    check("mask_dsn без пароля не меняет",
          mask_dsn("postgresql://u@h:5432/d") == "postgresql://u@h:5432/d")
    check("is_local_host",
          is_local_host("localhost") and is_local_host("127.0.0.1")
          and not is_local_host("db.example.com"))

    check("classify: auth",
          classify_error('FATAL: password authentication failed for user '
                         '"postgres"') == "auth")
    check("classify: no_database",
          classify_error('FATAL: database "llmagent" does not exist')
          == "no_database")
    check("classify: no_role",
          classify_error('FATAL: role "llmagent" does not exist')
          == "no_role")
    check("classify: unreachable",
          classify_error("connection refused to server") == "unreachable")
    check("credential-ошибки: auth/нет роли — да, недоступен — нет",
          is_credential_error("password authentication failed")
          and is_credential_error('role "x" does not exist')
          and not is_credential_error("connection refused"))

    # probe_port на живом слушателе
    srv = socket.socket()
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]
    check("probe_port: слушатель найден", probe_port("127.0.0.1", port))
    check("probe_port: закрытый порт",
          not probe_port("127.0.0.1", 1, timeout=0.3))
    srv.close()
    # порт освобождён — найден не будет
    check("find_service: пусто на закрытых",
          None in (None,) and probe_port("127.0.0.1", port) is False)


# ═══════════════════════════════════════════════════════════════════════
# 2. try_candidates (фейковый _connect)
# ═══════════════════════════════════════════════════════════════════════
def test_try_candidates(monkey_src) -> None:
    print("── try_candidates (фейковый драйвер) ──")

    # случай A: пароль postgres на паре (postgres, postgres)
    valid = {("postgres", "postgres", "postgres"),
             ("llmagent", "llmagent", "llmagent")}

    def fake_connect(dsn, timeout=2.5):
        m = re.match(r"postgresql://([^:]+):([^@]+)@[^:]+:(\d+)/([^/]+)", dsn)
        user, pwd, _, db = m.groups()
        if (user, pwd, db) not in valid:
            raise RuntimeError(
                f'FATAL: password authentication failed for user "{user}"')
        class _C:
            def close(self): ...
        return _C()

    monkey_src.setattr("src.db.autodetect._connect", fake_connect)
    from src.db.autodetect import try_candidates
    tries: list[tuple[str, str, bool]] = []
    res = try_candidates("127.0.0.1", 5432, ["wrong1", "postgres", "123456"],
                         on_try=lambda u, d, ok: tries.append((u, d, ok)))
    check("найдена пара postgres/postgres",
          res and res["user"] == "postgres" and res["password"] == "postgres",
          str(res))
    check("DSN собран", res and res["dsn"].startswith(
        "postgresql://postgres:postgres@127.0.0.1:5432/postgres"))
    check("неудачные попытки зарегистрированы",
          any(not ok for _, _, ok in tries) and bool(tries)
          and tries[-1][2] is True)

    # случай B: учётка рабочая (postgres/postgres), но база отсутствует
    def fake_connect_nodb(dsn, timeout=2.5):
        m = re.match(r"postgresql://([^:]+):([^@]+)@[^:]+:(\d+)/([^/]+)", dsn)
        user, pwd, _, db = m.groups()
        if (user, pwd, db) == ("postgres", "postgres", "postgres"):
            # пароль приняли, базы нет
            raise RuntimeError('FATAL: database "postgres" does not exist')
        raise RuntimeError(
            f'FATAL: password authentication failed for user "{user}"')

    monkey_src.setattr("src.db.autodetect._connect", fake_connect_nodb)
    res2 = try_candidates("127.0.0.1", 5432, ["postgres"])
    check("no_database → учётка принята, база bootstrap",
          res2 and res2.get("needs_db_create") is True
          and res2["database"] == "llmagent", str(res2))

    # случай C: всё мимо
    def fake_fail(dsn, timeout=2.5):
        raise RuntimeError("password authentication failed for user")

    monkey_src.setattr("src.db.autodetect._connect", fake_fail)
    res3 = try_candidates("127.0.0.1", 5432, ["postgres", "123456"])
    check("словарь не сработал → None", res3 is None)

    # случай D: драйвера нет → None (не падаем)
    def fake_import_err(dsn, timeout=2.5):
        raise ImportError("No module named psycopg")

    monkey_src.setattr("src.db.autodetect._connect", fake_import_err)
    res4 = try_candidates("127.0.0.1", 5432, ["postgres"])
    check("ImportError → None", res4 is None)


# ═══════════════════════════════════════════════════════════════════════
# 3. bootstrap_app_role (фейковый коннект с памятью)
# ═══════════════════════════════════════════════════════════════════════
class FakeCursor:
    def __init__(self, state: dict):
        self.state = state
        self.last = None

    def execute(self, q, params=None):
        s = str(q)
        self.last = s
        if "pg_roles" in s:
            role = self.state.get("pending_role_query")
            self._rows = [(1,)] if role in self.state["roles"] else []
        elif "pg_database" in s:
            db = self.state.get("pending_db_query")
            self._rows = [(1,)] if db in self.state["dbs"] else []
        elif "CREATE ROLE" in s:
            self.state["roles"].add(self.state["pending_role_query"])
            self.state["log"].append("CREATE ROLE")
        elif "ALTER ROLE" in s:
            self.state["log"].append("ALTER ROLE")
        elif "CREATE DATABASE" in s:
            self.state["dbs"].add(self.state["pending_db_query"])
            self.state["log"].append("CREATE DATABASE")
        elif "ALTER DATABASE" in s:
            self.state["log"].append("ALTER DATABASE")
        else:
            self.state["log"].append(s[:40])

    def fetchone(self):
        rows = getattr(self, "_rows", [])
        return rows[0] if rows else None

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeConn:
    """Коннект с очень упрощённым парсером SQL (для проверки логики)."""

    def __init__(self, roles=None, dbs=None):
        self.state = {"roles": set(roles or []), "dbs": set(dbs or []),
                      "log": [], "pending_role_query": APP_ROLE,
                      "pending_db_query": APP_DB}
        self.autocommit = False
        self._cur = None

    def cursor(self):
        self._cur = FakeCursor(self.state)
        return self._cur

    def close(self):
        pass


APP_ROLE = "llmagent"
APP_DB = "llmagent"


def test_bootstrap() -> None:
    print("── bootstrap_app_role ──")
    from src.db.autodetect import bootstrap_app_role

    # A: ничего нет — создать роль и базу
    conn = FakeConn()
    rep = bootstrap_app_role(conn, "genpass-1")
    check("роль создана", rep["role_created"] is True, str(rep))
    check("база создана", rep["db_created"] is True)
    log = " | ".join(conn.state["log"])
    check("CREATE ROLE в логе", "CREATE ROLE" in log)
    check("CREATE DATABASE в логе", "CREATE DATABASE" in log)

    # B: роль и база уже есть — только ALTER (идемпотентный повтор)
    conn2 = FakeConn(roles={"llmagent"}, dbs={"llmagent"})
    rep2 = bootstrap_app_role(conn2, "genpass-2")
    check("повтор: роль сброшена паролем (ALTER)",
          rep2["role_created"] is False and rep2["role_reset"] is True,
          str(rep2))
    check("повтор: база не создавалась", rep2["db_created"] is False)
    check("повтор: ALTER DATABASE OWNER выполнен",
          "ALTER DATABASE" in " | ".join(conn2.state["log"]))


# ═══════════════════════════════════════════════════════════════════════
# 4. ensure_pg — сценарии (фейки вместо сети/драйвера)
# ═══════════════════════════════════════════════════════════════════════
def _clear_settings_cache() -> None:
    """get_settings в src.config под @lru_cache — сценарии с разными env
    обязаны сбрасывать кэш, иначе настройки заморожены первым вызовом."""
    try:
        from src.config import get_settings
        get_settings.cache_clear()
    except Exception:
        pass


def test_ensure_pg(tmp: Path, monkey_src) -> None:
    print("── ensure_pg: сценарии ──")
    from src.db import autodetect as ad

    root = tmp / "proj"
    (root / "data").mkdir(parents=True)
    (root / ".env.example").write_text(
        "PG_APP_HOST=localhost\nPG_APP_PORT=5432\nPG_APP_USER=llmagent\n"
        "PG_APP_PASSWORD=secret\nPG_APP_DATABASE=llmagent\n",
        encoding="utf-8")
    (root / ".env").write_text("PG_APP_PASSWORD=secret\n", encoding="utf-8")
    MONKEY.setenv("PROJECT_ROOT", str(root))
    MONKEY.setenv("PG_ENABLED", "true")
    # окружение хоста не должно протекать в сценарии
    MONKEY.setenv("DATABASE_URL", "")
    MONKEY.setenv("PG_APP_HOST", "localhost")
    MONKEY.setenv("PG_APP_PORT", "5432")

    # A: disabled
    MONKEY.setenv("PG_ENABLED", "false")
    _clear_settings_cache()
    ad.reset_cache()
    rep = ad.ensure_pg(interactive=False)
    check("PG_ENABLED=false → disabled", rep["status"] == "disabled")
    MONKEY.setenv("PG_ENABLED", "true")

    # B: no_driver (psycopg «удалён» из sys.modules)
    import importlib
    saved_psycopg = sys.modules.get("psycopg")
    sys.modules["psycopg"] = None  # import выдаст ImportError
    try:
        _clear_settings_cache()
        ad.reset_cache()
        rep = ad.ensure_pg(interactive=False)
        check("нет psycopg → no_driver", rep["status"] == "no_driver",
              str(rep))
    finally:
        if saved_psycopg is not None:
            sys.modules["psycopg"] = saved_psycopg
        else:
            sys.modules.pop("psycopg", None)

    # C: no_service
    monkey_src.setattr("src.db.autodetect.verify_dsn", lambda *a, **k: False)
    monkey_src.setattr("src.db.autodetect.find_service", lambda *a, **k: None)
    _clear_settings_cache()
    ad.reset_cache()
    rep = ad.ensure_pg(interactive=False)
    check("порт закрыт → no_service", rep["status"] == "no_service")

    # D: DATABASE_URL задан и не отвечает → manual (не переписываем)
    MONKEY.setenv("DATABASE_URL",
                  "postgresql://user:pass@localhost:5432/otherdb")
    monkey_src.setattr("src.db.autodetect.find_service",
                       lambda *a, **k: 5432)
    _clear_settings_cache()
    ad.reset_cache()
    rep = ad.ensure_pg(interactive=False)
    check("чужой DATABASE_URL не переписывается (manual)",
          rep["status"] == "manual")
    MONKEY.setenv("DATABASE_URL", "")
    os.environ["DATABASE_URL"] = ""

    # E: успех по словарю → ok, bootstrap, env записан
    def fake_verify(dsn, timeout=2.5):
        # текущий DSN не живой, но приложение после bootstrap — живое
        return "genpass" in dsn or "@localhost:5433/" in dsn

    monkey_src.setattr("src.db.autodetect.verify_dsn", fake_verify)
    monkey_src.setattr("src.db.autodetect.find_service",
                       lambda *a, **k: 5433)
    monkey_src.setattr("src.db.autodetect.try_candidates", lambda *a, **k: {
        "user": "postgres", "password": "postgres",
        "database": "postgres",
        "dsn": "postgresql://postgres:postgres@localhost:5433/postgres"})
    monkey_src.setattr("src.db.autodetect.bootstrap_app_role",
                       lambda conn, pwd, **k: {"role_created": True,
                                               "role_reset": False,
                                               "db_created": True})
    monkey_src.setattr("src.db.autodetect.ensure_schema",
                       lambda dsn, **k: {"already_ok": True})
    # админ-коннект (не через verify_dsn) — фейкаем и его
    class _FakeConn:
        def close(self):
            pass

    monkey_src.setattr("src.db.autodetect._connect",
                       lambda dsn, timeout=2.5: _FakeConn())
    _clear_settings_cache()
    ad.reset_cache()
    rep = ad.ensure_pg(interactive=False)
    check("словарь сработал → ok via=dictionary",
          rep["status"] == "ok" and rep.get("via") == "dictionary",
          str(rep))
    check("bootstrap записан в отчёт",
          (rep.get("bootstrap") or {}).get("role_created") is True)
    env_text = (root / ".env").read_text(encoding="utf-8")
    check(".env: роль приложения llmagent",
          "PG_APP_USER=llmagent" in env_text)
    check(".env: сгенерированный пароль (не админский)",
          "PG_APP_PASSWORD=" in env_text
          and "postgres" not in env_text.split("PG_APP_PASSWORD=")[1]
          .split("\n")[0])
    check(".env: база llmagent", "PG_APP_DATABASE=llmagent" in env_text)
    check(".env: порт подставлен с детекта", "PG_APP_PORT=5433" in env_text)
    check("os.environ обновлён",
          os.environ.get("PG_APP_USER") == "llmagent"
          and os.environ.get("PG_APP_PORT") == "5433")
    # get_settings должен увидеть НОВЫЕ значения (кэш сброшен ensure_pg)
    from src.config import get_settings
    fresh = get_settings().postgres
    check("кэш settings сброшен: свежий DSN",
          fresh.user == "llmagent"
          and fresh.password == os.environ.get("PG_APP_PASSWORD")
          and fresh.port == 5433)
    rep_file = root / "data" / "db_autodetect.json"
    check("отчёт сохранён", rep_file.exists())
    if rep_file.exists():
        txt = rep_file.read_text(encoding="utf-8")
        check("в отчёте нет паролей",
              "genpass" not in txt and "postgres:postgres" not in txt)

    # F: кэш на процесс — второй вызов не пересчитывает
    def boom(*a, **k):
        raise AssertionError("не должен пересчитываться")

    monkey_src.setattr("src.db.autodetect.find_service", boom)
    rep2 = ad.ensure_pg(interactive=False)
    check("кэш: второй вызов мгновенный",
          rep2.get("status") == "ok" and rep2 is ad.last_result())
    ad.reset_cache()

    # G: интерактивный запрос пароля админа (фейковый prompt)
    monkey_src.setattr("src.db.autodetect.verify_dsn",
                       lambda *a, **k: False)
    monkey_src.setattr("src.db.autodetect.find_service",
                       lambda *a, **k: 5432)
    monkey_src.setattr("src.db.autodetect.try_candidates",
                       lambda *a, **k: None)
    prompts = {"n": 0}

    def fake_prompt(user, host):
        prompts["n"] += 1
        return "admin-secret" if prompts["n"] == 1 else None

    monkey_src.setattr("src.db.autodetect.prompt_admin_password",
                       fake_prompt)

    def try_with_admin(host, port, passwords, **kw):
        if passwords == ["admin-secret"]:
            return {"user": "postgres", "password": "admin-secret",
                    "database": "postgres",
                    "dsn": "postgresql://postgres:admin-secret@"
                           "localhost:5432/postgres"}
        return None

    monkey_src.setattr("src.db.autodetect.try_candidates", try_with_admin)
    _clear_settings_cache()
    rep3 = ad.ensure_pg(interactive=True, write_env=False)
    check("введённый пароль админа принят → ok via=prompt",
          rep3.get("status") == "ok" and rep3.get("via") == "prompt",
          str(rep3))
    check("пароль админа нигде не светится в отчёте",
          "admin-secret" not in str(rep3))
    ad.reset_cache()
    MONKEY.undo()


# ═══════════════════════════════════════════════════════════════════════
# 5. Идемпотентность db/*.sql (безопасность повторных init_db)
# ═══════════════════════════════════════════════════════════════════════
def test_sql_idempotent() -> None:
    print("── db/*.sql идемпотентны ──")
    files = ["init.sql", "journal.sql", "ops.sql", "analytics.sql",
             "search_improvements.sql", "cdc_notify.sql"]
    create_re = re.compile(
        r"CREATE\s+(?:UNLOGGED\s+)?(?:TABLE|MATERIALIZED\s+VIEW)\s+"
        r"(?:IF\s+NOT\s+EXISTS\s+)?([\w.]+)", re.I)
    drop_re = re.compile(
        r"DROP\s+(?:TABLE|MATERIALIZED\s+VIEW)\s+IF\s+EXISTS\s+([\w.]+)",
        re.I)
    for name in files:
        sql = (BASE / "db" / name).read_text(encoding="utf-8")
        drops = {m.group(1).lower() for m in drop_re.finditer(sql)}
        bad = []
        for m in create_re.finditer(sql):
            obj = m.group(1).lower()
            # необязательная группа IF NOT EXISTS — часть match, если была
            if "if not exists" in m.group(0).lower():
                continue
            # динамический SQL (format('%s_…')): «CREATE TABLE IF» обрывается,
            # но в исходнике сразу дальше стоит «NOT EXISTS %s…»
            if obj == "if" and "not exists" in \
                    sql[m.end():m.end() + 32].lower():
                continue
            if obj in drops:   # допустимый паттерн: DROP IF EXISTS + CREATE
                continue
            bad.append(m.group(0)[:60])
        check(f"{name}: каждый CREATE защищён", not bad, str(bad))

    # scripts/init_db.py — тонкая обёртка над идемпотентным init_schema
    cli = (BASE / "scripts" / "init_db.py").read_text(encoding="utf-8")
    check("scripts/init_db.py остаётся CLI-обёрткой src.db.init_db",
          "from src.db.init_db import main" in cli)


# ═══════════════════════════════════════════════════════════════════════
# 6. Хуки в run.py / main.py
# ═══════════════════════════════════════════════════════════════════════
def test_hooks() -> None:
    print("── хуки run.py / main.py ──")
    run_src = (BASE / "run.py").read_text(encoding="utf-8")
    check("run.py: вызов check_pg_autodetect() в main",
          "check_pg_autodetect()" in run_src)
    check("run.py: интерактивный ensure_pg",
          "ensure_pg(interactive=True)" in run_src)
    main_src = (BASE / "src" / "main.py").read_text(encoding="utf-8")
    check("main.py: lifespan вызывает ensure_pg(interactive=False)",
          "ensure_pg(interactive=False)" in main_src)
    check("main.py: эндпоинт /api/db/autodetect",
          '"/api/db/autodetect"' in main_src
          and "db_autodetect_refresh" in main_src)


# ═══════════════════════════════════════════════════════════════════════
class _SrcMonkey:
    """setattr на модуль с восстановлением."""

    def __init__(self):
        self._saved = {}

    def setattr(self, target: str, value):
        mod_name, _, attr = target.rpartition(".")
        import importlib
        mod = importlib.import_module(mod_name)
        key = (mod_name, attr)
        if key not in self._saved:
            self._saved[key] = getattr(mod, attr)
        setattr(mod, attr, value)

    def undo(self):
        import importlib
        for (mod_name, attr), val in self._saved.items():
            setattr(importlib.import_module(mod_name), attr, val)
        self._saved.clear()


def main() -> int:
    print("\n══ Тесты Task 24-b: PostgreSQL-автодетект ══\n")
    srcmonkey = _SrcMonkey()
    test_pure()
    test_try_candidates(srcmonkey)
    test_bootstrap()
    with tempfile.TemporaryDirectory() as td:
        test_ensure_pg(Path(td), srcmonkey)
    test_sql_idempotent()
    test_hooks()
    srcmonkey.undo()
    MONKEY.undo()
    print(f"\nИтого: {len(PASS)} OK, {len(FAIL)} FAIL")
    if FAIL:
        for name in FAIL:
            print(f"  ✗ {name}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
