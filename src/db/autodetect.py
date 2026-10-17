"""PostgreSQL-автодетект при каждом запуске (Task 24-b).

Порядок работы ensure_pg():
  1. PG_ENABLED=false → «disabled», ничего не делаем.
  2. Драйвер psycopg не установлен → «no_driver» (система живёт на SQLite).
  3. Текущий DSN (DATABASE_URL или PG_APP_*) уже отвечает на SELECT 1
     → «already» — на каждом запуске проверяем, но не переписываем конфиг.
  4. Порты 5432/5433/PG_APP_PORT закрыты → «no_service» — службы нет,
     система штатно уходит в SQLite/LanceDB; окружение не трогаем.
  5. Пользователь явно задал DATABASE_URL, но он не отвечает → «manual»
     (чужой конфиг не переписываем, даём подсказку).
  6. Служба есть, текущий DSN не отвечает → перебор дефолтных паролей
     из словарика (postgres/postgres, admin, 123456, …) по парам
     (пользователь, база). Нашли → «ok» via=dictionary.
  7. Словарик не сработал и процесс интерактивен (TTY) → запрос пароля
     админа (getpass, до 3 попыток). Успех → «ok» via=prompt.
  8. При успехе (словарик или ввод) — bootstrap: собственная роль
     приложения llmagent со сгенерированным паролем + база llmagent
     (идемпотентно), затем применение схемы (src.db.init_db) от имени
     админа — так CREATE EXTENSION (pgvector/pg_trgm) поднимется.
  9. Результат: .env (PG_APP_*) + os.environ + отчёт
     data/db_autodetect.json (БЕЗ паролей) и GET /api/db/autodetect.

Пароли из отчётов и логов никогда не печатаются — только источник
(«словарь: postgres», «ввод админа»).
"""
from __future__ import annotations

import getpass
import json
import logging
import os
import secrets
import socket
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# ── Словарик дефолтных паролей (локальные инсталляции) ────────────────
DEFAULT_PASSWORDS = [
    "postgres", "admin", "password", "123456", "1234", "root",
    "postgresql", "postgres123", "passw0rd", "changeme", "secret",
    "docker", "test", "masterkey", "qwerty", "12345678", "admin123",
    "pg", "sa", "password1", "letmein", "welcome", "llmagent",
]
DEFAULT_USERS = ["postgres", "llmagent"]
# пары (пользователь, база) — сначала «родные» сочетания
DEFAULT_PAIRS = [
    ("postgres", "postgres"),
    ("llmagent", "llmagent"),
    ("postgres", "llmagent"),
    ("llmagent", "postgres"),
]
PROBE_PORTS = [5432, 5433, 5434]
APP_ROLE = "llmagent"
APP_DATABASE = "llmagent"
PROMPT_TRIES = 3

_REPORT_FILE = "db_autodetect.json"

_last_result: dict | None = None          # кэш на процесс (run.py + lifespan)


# ═══════════════════════════════════════════════════════════════════════
# Чистые утилиты (тестируются без реального PostgreSQL)
# ═══════════════════════════════════════════════════════════════════════
def mask_dsn(dsn: str) -> str:
    """postgresql://user:secret@host:port/db → postgresql://user:***@…"""
    import re
    return re.sub(r"(?<=://)([^:@/]+):[^@/]+", r"\1:***", str(dsn or ""))


def probe_port(host: str, port: int, timeout: float = 0.8) -> bool:
    """Есть ли TCP-слушатель (простейший детект службы)."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def find_service(host: str, ports: list[int] | None = None) -> int | None:
    """Первый открытый порт из списка (по умолчанию PROBE_PORTS)."""
    ports = ports or PROBE_PORTS
    for port in ports:
        if port and probe_port(host, port):
            return port
    return None


def is_local_host(host: str) -> bool:
    return str(host or "").lower() in ("localhost", "127.0.0.1", "::1")


def classify_error(msg: str) -> str:
    """Класс ошибки подключения — для решения «пробовать дальше или нет»."""
    m = str(msg).lower()
    if "password authentication failed" in m or "28p01" in m:
        return "auth"
    if "does not exist" in m and ("database" in m or "3d000" in m):
        return "no_database"
    if "role" in m and "does not exist" in m:
        return "no_role"
    if "connection refused" in m or "could not connect" in m \
            or "timed out" in m or "timeout" in m:
        return "unreachable"
    return "other"


def is_credential_error(msg: str) -> bool:
    """Ошибка аутентификации (роль/пароль) — перебор имеет смысл."""
    return classify_error(msg) in ("auth", "no_role")


# ═══════════════════════════════════════════════════════════════════════
# Подключения
# ═══════════════════════════════════════════════════════════════════════
def _connect(dsn: str, timeout: float = 2.5):
    """psycopg-коннект (v3); при отсутствии драйвера — ImportError."""
    import psycopg  # noqa: PLC0415 — драйвер опционален
    return psycopg.connect(dsn, connect_timeout=timeout)


def verify_dsn(dsn: str, timeout: float = 2.5) -> bool | None:
    """True/False — ответил SELECT 1; None — драйвера нет."""
    try:
        with _connect(dsn, timeout) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()
        return True
    except ImportError:
        return None
    except Exception:
        return False


def try_candidates(host: str, port: int, passwords: list[str],
                   users: list[str] | None = None,
                   pairs: list[tuple[str, str]] | None = None,
                   on_try=None) -> dict | None:
    """Перебор (пароль × пара user/db); первый рабочий — результат.

    on_try(user, db, ok: bool) — колбэк для логов/тестов.
    Возвращает {user, password, database, dsn} или None.
    """
    users = users or DEFAULT_USERS
    pairs = pairs or DEFAULT_PAIRS
    pairs = [(u, d) for (u, d) in pairs if u in users]
    for password in passwords:
        for user, database in pairs:
            dsn = f"postgresql://{user}:{password}@{host}:{port}/{database}"
            try:
                _connect(dsn, timeout=2.0).close()
                if on_try:
                    on_try(user, database, True)
                return {"user": user, "password": password,
                        "database": database, "dsn": dsn}
            except ImportError:
                return None
            except Exception as e:
                if on_try:
                    on_try(user, database, False)
                if not is_credential_error(e):
                    # база может отсутствовать, но учётка рабочая —
                    # запомним и продолжим (bootstrap создаст базу)
                    if classify_error(e) == "no_database":
                        return {"user": user, "password": password,
                                "database": APP_DATABASE, "dsn": dsn,
                                "needs_db_create": True}
                    logger.debug("pg-autodetect: %s:%s@%s/%s → %s",
                                 user, "***", host, port,
                                 classify_error(e))
    return None


def prompt_admin_password(user: str, host: str) -> str | None:
    """Интерактивный запрос пароля админа (только TTY)."""
    if not sys.stdin.isatty():
        return None
    try:
        print(f"\n  PostgreSQL на {host}: нужен пароль администратора "
              f"«{user}» (ввод скрыт, Enter — пропустить).")
        pwd = getpass.getpass(f"  Пароль {user}: ")
        return pwd.strip() or None
    except (EOFError, KeyboardInterrupt):
        print()
        return None


# ═══════════════════════════════════════════════════════════════════════
# Bootstrap: роль приложения + база (идемпотентно)
# ═══════════════════════════════════════════════════════════════════════
def role_exists(conn, role: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,))
        return cur.fetchone() is not None


def database_exists(conn, name: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,))
        return cur.fetchone() is not None


def bootstrap_app_role(admin_conn, password: str,
                       role: str = APP_ROLE,
                       database: str = APP_DATABASE) -> dict:
    """Создаёт роль и базу приложения, если их нет (CREATE внутри
    транзакции запрещён — работаем в autocommit). Возвращает отчёт:
    {role_created, role_reset, db_created}.

    Роль существует, но пароль не подошёл текущему приложению —
    это bootstrap-режим: пароль роли сбрасывается на сгенерированный
    (локальная dev-инсталляция, владелец — сам пользователь).
    """
    from psycopg import sql  # noqa: PLC0415

    report = {"role_created": False, "role_reset": False,
              "db_created": False}
    admin_conn.autocommit = True
    with admin_conn.cursor() as cur:
        if role_exists(admin_conn, role):
            cur.execute(
                sql.SQL("ALTER ROLE {} WITH LOGIN PASSWORD {}")
                .format(sql.Identifier(role), sql.Literal(password)))
            report["role_reset"] = True
        else:
            cur.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}")
                .format(sql.Identifier(role), sql.Literal(password)))
            report["role_created"] = True
        if not database_exists(admin_conn, database):
            cur.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}")
                .format(sql.Identifier(database), sql.Identifier(role)))
            report["db_created"] = True
        else:
            cur.execute(
                sql.SQL("ALTER DATABASE {} OWNER TO {}")
                .format(sql.Identifier(database), sql.Identifier(role)))
    return report


# ═══════════════════════════════════════════════════════════════════════
# Схема (идемпотентная, из src.db.init_db)
# ═══════════════════════════════════════════════════════════════════════
def ensure_schema(dsn: str, timeout_s: float = 120.0) -> dict:
    """check_schema → если неполна, init_schema (оба идемпотентны)."""
    from src.db.init_db import check_schema, init_schema  # noqa: PLC0415
    out: dict = {"already_ok": False, "applied": False, "errors": 0}
    try:
        info = check_schema(dsn)
        if info.get("ok"):
            out["already_ok"] = True
            return out
        reports, ok = init_schema(dsn)
        out["applied"] = True
        out["errors"] = sum(len(r.get("errors", [])) for r in reports)
        out["ok"] = bool(ok)
    except Exception as e:  # noqa: BLE001 — схема не должна валить старт
        out["error"] = str(e)[:200]
    return out


# ═══════════════════════════════════════════════════════════════════════
# Главная точка входа
# ═══════════════════════════════════════════════════════════════════════
def _save_report(report: dict) -> None:
    try:
        root = Path(os.getenv("PROJECT_ROOT") or
                    Path(__file__).resolve().parent.parent.parent)
        p = root / "data" / _REPORT_FILE
        p.parent.mkdir(parents=True, exist_ok=True)
        clean = {k: v for k, v in report.items()
                 if k not in ("password", "dsn", "admin_dsn", "app_dsn")}
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=1),
                       encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass


def ensure_pg(interactive: bool = True,
              write_env: bool = True,
              force: bool = False) -> dict:
    """Автодетект при каждом запуске. Результат кэшируется на процесс
    (force=True — перепроверить). Никогда не бросает исключений."""
    global _last_result
    if _last_result is not None and not force:
        return _last_result

    report: dict = {"ts": time.time(), "status": "unknown", "hint": ""}

    try:
        from src.config import get_settings  # noqa: PLC0415
        pg = get_settings().postgres
    except Exception as e:  # noqa: BLE001
        report.update(status="error", hint=f"settings: {e}")
        _last_result = report
        _save_report(report)
        return report

    if not pg.enabled:
        report.update(status="disabled", hint="PG_ENABLED=false")
        _last_result = report
        _save_report(report)
        return report

    host = pg.host
    port = pg.port

    # 0. драйвер
    try:
        import psycopg  # noqa: F401, PLC0415
    except ImportError:
        report.update(status="no_driver",
                      hint="pip install 'psycopg[binary]' "
                           "(или requirements-psycopg3.txt)")
        _last_result = report
        _save_report(report)
        return report

    # 1. текущий DSN уже живой?
    current_dsn = pg.dsn()
    if verify_dsn(current_dsn):
        report.update(status="already", dsn=mask_dsn(current_dsn),
                      hint="текущий DSN отвечает — конфиг не трогаю")
        _last_result = report
        _save_report(report)
        return report

    # 2. служба вообще есть?
    found_port = find_service(host)
    if found_port is None:
        report.update(status="no_service",
                      hint=f"на {host}:{PROBE_PORTS} PostgreSQL не слушает — "
                           "система работает на SQLite/LanceDB")
        _last_result = report
        _save_report(report)
        return report
    if found_port != port:
        report["port_switched"] = f"{port} → {found_port}"
        port = found_port

    # 3. явный DATABASE_URL не отвечает — чужой конфиг не переписываем
    if pg.database_url.strip():
        report.update(
            status="manual",
            hint="DATABASE_URL задан, но не отвечает — проверьте сервер "
                 "или поправьте .env (автоматически не меняю)")
        _last_result = report
        _save_report(report)
        return report

    # 4. перебор словарика
    admin = try_candidates(host, port, DEFAULT_PASSWORDS,
                           users=DEFAULT_USERS, pairs=DEFAULT_PAIRS)
    via = "dictionary"
    # 5. интерактивный запрос пароля админа
    if admin is None and interactive:
        for _ in range(PROMPT_TRIES):
            pwd = prompt_admin_password("postgres", host)
            if not pwd:
                break
            admin = try_candidates(host, port, [pwd],
                                   users=["postgres"], pairs=DEFAULT_PAIRS)
            if admin:
                via = "prompt"
                break

    if admin is None:
        report.update(
            status="manual",
            hint="PostgreSQL найден, но пароль не подошёл (словарь"
                 + (" + ввод" if interactive else "") + "). Задайте свой "
                 "пароль в .env: PG_APP_PASSWORD=… или DATABASE_URL=… "
                 "(пример: postgresql://postgres:ПАРОЛЬ@localhost:5432/"
                 "postgres) и перезапустите")
        _last_result = report
        _save_report(report)
        return report

    # 6. bootstrap: роль+база приложения со сгенерированным паролем
    app_password = secrets.token_urlsafe(18)
    bootstrap: dict = {}
    app_dsn = ""
    try:
        admin_conn = _connect(admin["dsn"], timeout=3)
        try:
            bootstrap = bootstrap_app_role(admin_conn, app_password)
        finally:
            admin_conn.close()
        app_dsn = (f"postgresql://{APP_ROLE}:{app_password}"
                   f"@{host}:{port}/{APP_DATABASE}")
        if not verify_dsn(app_dsn):
            app_dsn = ""
    except Exception as e:  # noqa: BLE001
        bootstrap["error"] = str(e)[:200]

    if not app_dsn:
        # фолбэк: приложение работает учеткой админа (локальная dev-машина)
        app_dsn = admin["dsn"]
        report["app_fallback_admin"] = True

    # 7. схема — от имени админа (расширения ставятся суперпользователем)
    schema = {"skipped": True}
    try:
        schema = ensure_schema(admin["dsn"])
    except Exception as e:  # noqa: BLE001
        schema = {"error": str(e)[:200]}

    # 8. .env + os.environ
    if write_env:
        use_app_role = app_dsn.startswith(f"postgresql://{APP_ROLE}")
        env_vals = {
            "PG_APP_HOST": str(host),
            "PG_APP_PORT": str(port),
            "PG_APP_USER": APP_ROLE if use_app_role else admin["user"],
            "PG_APP_PASSWORD": app_password if use_app_role
            else admin["password"],
            "PG_APP_DATABASE": APP_DATABASE if use_app_role
            else admin["database"],
        }
        try:
            from src.env_file import apply_to_environ, write_env_updates \
                as _weu  # noqa: PLC0415
            _weu(env_vals)
            apply_to_environ(env_vals)
            os.environ.pop("DATABASE_URL", None)   # теперь точнее компоненты
        except Exception as e:  # noqa: BLE001
            report["env_error"] = str(e)[:200]

        # get_settings() под @lru_cache: без сброса все, кто вызовет его
        # позже в этом процессе (Memory в lifespan и др.), получат
        # СТАРЫЕ PG_APP_* и не увидят найденные креды до перезапуска.
        try:
            from src.config import get_settings as _gs  # noqa: PLC0415
            _gs.cache_clear()
        except Exception:  # noqa: BLE001
            pass

    report.update(
        status="ok",
        via=via,
        admin_user=admin["user"],
        port=port,
        bootstrap=bootstrap,
        schema=schema,
        app_dsn=mask_dsn(app_dsn),
        hint=f"PostgreSQL подключён ({via}); роль {APP_ROLE}: "
             + ("создана" if bootstrap.get("role_created")
                else "пароль сброшен" if bootstrap.get("role_reset")
                else "уже была"),
    )
    _last_result = report
    _save_report(report)
    logger.info("PG-автодетект: %s (via=%s, схема: %s)", report["status"],
                via, "ok" if schema.get("already_ok") or schema.get("applied")
                else "частично")
    return report


def last_result() -> dict | None:
    """Кэш последнего результата ensure_pg (для /api/db/autodetect)."""
    return _last_result


def load_report() -> dict:
    """Отчёт с диска (для API до первого ensure_pg в этом процессе)."""
    try:
        root = Path(os.getenv("PROJECT_ROOT") or
                    Path(__file__).resolve().parent.parent.parent)
        return json.loads(
            (root / "data" / _REPORT_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def reset_cache() -> None:
    global _last_result
    _last_result = None
