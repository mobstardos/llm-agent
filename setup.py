"""LLM Agent — единый скрипт настройки и запуска.

Что делает:
  1. Проверяет Python, Node.js, PostgreSQL, Ollama, Docker
  2. Устанавливает Python-зависимости
  3. Запускает Docker-сервисы (AGE, Kafka) если нужно
  4. Запускает Ollama и загружает модель
  5. Создаёт/обновляет .env из .env.example
  6. Создаёт БД, применяет SQL-схемы
  7. Запускает миграции данных (если есть старые)
  8. Проверяет все сервисы
  9. Запускает uvicorn

Использование:
  python setup.py                     # Полный цикл
  python setup.py --skip-install      # Пропустить pip install
  python setup.py --skip-docker       # Не поднимать Docker
  python setup.py --skip-ollama       # Не трогать Ollama
  python setup.py --skip-migrations   # Не мигрировать данные
  python setup.py --skip-checks       # Не проверять сервисы
  python setup.py --reset-db          # Удалить и создать БД заново
  python setup.py --only-check        # Только проверить, не запускать
"""
from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

# ═══════════════════════════════════════════════════════════════════════
# Константы
# ═══════════════════════════════════════════════════════════════════════

BASE_DIR = Path(__file__).resolve().parent
ENV_FILE = BASE_DIR / ".env"
ENV_EXAMPLE = BASE_DIR / ".env.example"
REQUIREMENTS = BASE_DIR / "requirements.txt"
DB_DIR = BASE_DIR / "db"
SCRIPTS_DIR = BASE_DIR / "scripts"
DATA_DIR = BASE_DIR / "data"

IS_WINDOWS = os.name == "nt"


# ═══════════════════════════════════════════════════════════════════════
# Цветной вывод
# ═══════════════════════════════════════════════════════════════════════

class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"


def _color(code: str, text: str) -> str:
    return f"{code}{text}{C.RESET}" if sys.stdout.isatty() else text


def ok(msg: str) -> None:
    print(f"  {_color(C.GREEN, '✓')} {msg}")


def fail(msg: str) -> None:
    print(f"  {_color(C.RED, '✗')} {msg}")


def warn(msg: str) -> None:
    print(f"  {_color(C.YELLOW, '!')} {msg}")


def info(msg: str) -> None:
    print(f"  {_color(C.DIM, '·')} {msg}")


def section(title: str) -> None:
    line = "─" * max(0, 60 - len(title) - 4)
    print(f"\n{_color(C.BOLD + C.CYAN, '┌─ ' + title + ' ' + line)}")


def banner() -> None:
    print()
    print(_color(C.BOLD + C.MAGENTA, "╔══════════════════════════════════════════════════════════╗"))
    print(_color(C.BOLD + C.MAGENTA, "║           🤖  LLM Agent — setup & run                    ║"))
    print(_color(C.BOLD + C.MAGENTA, "╚══════════════════════════════════════════════════════════╝"))


# ═══════════════════════════════════════════════════════════════════════
# Утилиты
# ═══════════════════════════════════════════════════════════════════════

def which(name: str) -> str | None:
    if IS_WINDOWS:
        for ext in (".cmd", ".exe", ".bat", ""):
            p = shutil.which(name + ext)
            if p:
                return p
    return shutil.which(name)


def run_capture(cmd: list[str], timeout: int = 20,
                env: dict | None = None) -> tuple[int, str, str]:
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=timeout, shell=IS_WINDOWS,
            env={**os.environ, **(env or {})},
        )
        return p.returncode, p.stdout or "", p.stderr or ""
    except FileNotFoundError:
        return 127, "", f"not found: {cmd[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", "timeout"
    except Exception as e:
        return 1, "", str(e)


def is_port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(1.0)
        return s.connect_ex((host, port)) == 0


def pip_install(packages: list[str], quiet: bool = False) -> bool:
    if not packages:
        return True
    cmd = [sys.executable, "-m", "pip", "install"]
    if quiet:
        cmd.append("-q")
    cmd.extend(packages)
    code, out, err = run_capture(cmd, timeout=1200)
    if code != 0:
        fail(f"pip install failed: {(err or out)[-500:]}")
        return False
    return True


# ═══════════════════════════════════════════════════════════════════════
# Проверки
# ═══════════════════════════════════════════════════════════════════════

def check_python() -> bool:
    section("Python")
    v = sys.version_info
    if (v.major, v.minor) < (3, 10):
        fail(f"Python {v.major}.{v.minor} — нужно ≥ 3.10")
        return False
    ok(f"Python {v.major}.{v.minor}.{v.micro}")
    ok(f"Путь: {sys.executable}")
    if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        ok(f"venv: {sys.prefix}")
    else:
        warn("venv не активирован")
    return True


def check_node() -> None:
    section("Node.js (для qwenproxy)")
    node = which("node")
    if node:
        code, out, _ = run_capture([node, "--version"])
        ok(f"Node.js {out.strip()}")
    else:
        warn("Node.js не найден — qwenproxy не запустится")
    npm = which("npm")
    if npm:
        code, out, _ = run_capture([npm, "--version"])
        ok(f"npm {out.strip()}")


def check_postgres() -> bool:
    section("PostgreSQL")
    psql = which("psql")
    if not psql:
        fail("psql не найден в PATH")
        info("Windows: добавьте C:\\Program Files\\PostgreSQL\\16\\bin в PATH")
        return False

    code, out, _ = run_capture([psql, "--version"])
    if code == 0:
        ok(out.strip().splitlines()[0])

    host = os.getenv("PG_APP_HOST", "localhost")
    port = int(os.getenv("PG_APP_PORT", "5432"))
    if is_port_open(host, port):
        ok(f"PostgreSQL слушает {host}:{port}")
        return True
    else:
        fail(f"PostgreSQL недоступен на {host}:{port}")
        info("Запустите службу: services.msc → postgresql-x64-16 → Запустить")
        return False


def check_pg_dump() -> bool:
    section("pg_dump / pg_restore")
    pg_dump = which("pg_dump")
    pg_restore = which("pg_restore")
    if pg_dump and pg_restore:
        code, out, _ = run_capture([pg_dump, "--version"])
        ok(f"pg_dump: {out.strip()}")
        return True
    else:
        warn("pg_dump / pg_restore не найдены — бэкапы не будут работать")
        info("Добавьте C:\\Program Files\\PostgreSQL\\16\\bin в PATH")
        return False


def check_docker() -> bool:
    section("Docker (для AGE, Kafka)")
    docker = which("docker")
    if not docker:
        warn("docker не найден — AGE и Kafka не будут работать")
        return False

    code, out, _ = run_capture([docker, "--version"])
    if code == 0:
        ok(out.strip())

    code, _, err = run_capture([docker, "info"], timeout=10)
    if code == 0:
        ok("Docker daemon работает")
        return True
    else:
        warn("Docker daemon не запущен (Docker Desktop?)")
        return False


def check_ollama() -> bool:
    section("Ollama")
    ollama = which("ollama")
    if not ollama:
        fail("Ollama не найден")
        info("Скачайте: https://ollama.com/download/windows")
        return False

    ok(f"Ollama: {ollama}")

    # Проверка сервиса
    if is_port_open("127.0.0.1", 11434):
        ok("Ollama сервис на 127.0.0.1:11434")
    else:
        warn("Ollama не отвечает на 11434 (возможно, не запущен)")

    # Проверка модели
    model = os.getenv("OLLAMA_MODEL", "qwen2.5:1.5b-instruct")
    code, out, _ = run_capture([ollama, "list"], timeout=15)
    if code == 0 and model in out:
        ok(f"Модель установлена: {model}")
    else:
        warn(f"Модель не установлена: {model}")
        info(f"Загрузка: ollama pull {model}")
    return True


def check_qwenproxy() -> bool:
    section("qwenproxy (LLM backend)")
    qp = which("qpx") or which("qwenproxy")
    if not qp:
        fail("qpx (qwenproxy-cli) не найден в PATH")
        info("Установка: npm install -g qwenproxy-cli")
        info("Затем: npx playwright install chromium")
        info("Затем: запустите qpx → вкладка [5] Accounts → A "
             "(email и пароль от chat.qwen.ai)")
        return False

    ok(f"qwenproxy: {qp}")

    if is_port_open("127.0.0.1", 7936):
        ok("qwenproxy работает на 127.0.0.1:7936")
        return True
    else:
        warn("qwenproxy не запущен (порт 7936 не слушается)")
        info("Запустите: qpx start")
        return True


def check_files() -> bool:
    section("Файлы проекта")
    all_ok = True

    if not REQUIREMENTS.exists():
        fail(f"Не найден {REQUIREMENTS}")
        all_ok = False
    else:
        ok(f"requirements.txt")

    if not ENV_EXAMPLE.exists():
        fail(f"Не найден {ENV_EXAMPLE}")
        all_ok = False
    else:
        ok(f".env.example")

    if not DB_DIR.exists():
        fail(f"Не найдена папка {DB_DIR}")
        all_ok = False
    else:
        sql_files = list(DB_DIR.glob("*.sql"))
        ok(f"db/: {len(sql_files)} SQL файлов")

    return all_ok


# ═══════════════════════════════════════════════════════════════════════
# Setup: .env
# ═══════════════════════════════════════════════════════════════════════

def setup_env() -> bool:
    section(".env")
    if ENV_FILE.exists():
        ok(".env существует")
        return True
    if not ENV_EXAMPLE.exists():
        fail(".env.example не найден")
        return False
    shutil.copy(ENV_EXAMPLE, ENV_FILE)
    ok(".env создан из .env.example")
    warn("Отредактируйте .env: PROJECT_ROOT, LLM_API_KEY, DEEPSEEK_*")
    return True


# ═══════════════════════════════════════════════════════════════════════
# Setup: Python dependencies
# ═══════════════════════════════════════════════════════════════════════

def setup_pip() -> bool:
    section("Python-зависимости")

    # Проверка критичных пакетов
    critical = [
        ("fastapi", "fastapi"),
        ("uvicorn", "uvicorn[standard]"),
        ("openai", "openai"),
        ("mcp", "mcp"),
        ("httpx", "httpx"),
        ("pydantic", "pydantic"),
        ("yaml", "PyYAML"),
        ("dotenv", "python-dotenv"),
        ("psycopg", "psycopg[binary,pool]"),
        ("pgvector", "pgvector"),
    ]
    optional = [
        ("redis", "redis"),
        ("aiokafka", "aiokafka"),
        ("lancedb", "lancedb"),
        ("sentence_transformers", "sentence-transformers"),
        ("tree_sitter", "tree-sitter"),
        ("duckdb", "duckdb"),
        ("pymysql", "pymysql"),
    ]

    missing_critical = []
    missing_optional = []

    for module, pkg in critical:
        try:
            __import__(module)
            ok(f"{module}")
        except ImportError:
            fail(f"{module}")
            missing_critical.append(pkg)

    for module, pkg in optional:
        try:
            __import__(module)
            ok(f"{module}")
        except ImportError:
            warn(f"{module} (опционально)")
            missing_optional.append(pkg)

    if missing_critical:
        info(f"Устанавливаю критичные: {', '.join(missing_critical)}")
        if not pip_install(missing_critical, quiet=True):
            return False

    if missing_optional:
        print()
        ans = input(
            f"  Установить {len(missing_optional)} опциональных пакетов? [Y/n]: "
        ).strip().lower()
        if ans in ("", "y", "yes", "д", "да"):
            pip_install(missing_optional, quiet=True)

    # Полный requirements.txt для гарантии
    if REQUIREMENTS.exists():
        info("pip install -r requirements.txt (гарантия)")
        code, out, err = run_capture(
            [sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS)],
            timeout=1800,
        )
        if code == 0:
            ok("Все зависимости установлены")
            return True
        else:
            warn("pip install вернул ошибку, но критичные пакеты могут быть на месте")
            info((err or out)[-500:])
    return True


# ═══════════════════════════════════════════════════════════════════════
# Setup: Docker (AGE, Kafka)
# ═══════════════════════════════════════════════════════════════════════

def setup_docker() -> bool:
    section("Docker-сервисы (AGE, Kafka)")

    docker = which("docker")
    if not docker:
        warn("Docker не найден — пропускаю")
        return False

    compose_file = BASE_DIR / "docker-compose.yml"
    if not compose_file.exists():
        warn("docker-compose.yml не найден — пропускаю")
        return False

    # Проверить, что AGE и Kafka уже подняты
    age_up = is_port_open("localhost", 5433)
    kafka_up = is_port_open("localhost", 9092)

    if age_up and kafka_up:
        ok("AGE и Kafka уже работают")
        return True

    print()
    ans = input(
        "  Запустить docker-compose (AGE + Kafka)? [Y/n]: "
    ).strip().lower()
    if ans not in ("", "y", "yes", "д", "да"):
        info("Пропускаю Docker")
        return False

    info("docker-compose up -d...")
    code, out, err = run_capture(
        [docker, "compose", "up", "-d"],
        timeout=300,
    )
    if code != 0:
        fail("docker-compose up failed")
        info((err or out)[-500:])
        return False

    # Ждём готовности AGE
    info("Ожидание AGE...")
    for _ in range(30):
        if is_port_open("localhost", 5433):
            ok("AGE готов")
            break
        time.sleep(2)
    else:
        warn("AGE не поднялся за 60 сек")

    # Ждём Kafka
    info("Ожидание Kafka...")
    for _ in range(30):
        if is_port_open("localhost", 9092):
            ok("Kafka готов")
            break
        time.sleep(2)
    else:
        warn("Kafka не поднялся за 60 сек")

    return True


# ═══════════════════════════════════════════════════════════════════════
# Setup: Ollama
# ═══════════════════════════════════════════════════════════════════════

def setup_ollama() -> bool:
    section("Ollama — загрузка модели")

    ollama = which("ollama")
    if not ollama:
        warn("Ollama не установлен — обогащение не будет работать")
        return False

    model = os.getenv("OLLAMA_MODEL", "qwen2.5:1.5b-instruct")
    code, out, _ = run_capture([ollama, "list"], timeout=15)

    if code == 0 and model in out:
        ok(f"Модель уже загружена: {model}")
    else:
        print()
        ans = input(f"  Загрузить модель {model}? [Y/n]: ").strip().lower()
        if ans not in ("", "y", "yes", "д", "да"):
            return False

        info(f"ollama pull {model} (может занять 1–5 минут)...")
        code, out, err = run_capture(
            [ollama, "pull", model], timeout=1800,
        )
        if code == 0:
            ok(f"Модель загружена: {model}")
        else:
            fail("Загрузка не удалась")
            info((err or out)[-500:])
            return False

    # Проверка сервиса
    if not is_port_open("127.0.0.1", 11434):
        info("Запускаю Ollama сервис...")
        try:
            subprocess.Popen(
                [ollama, "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            for _ in range(15):
                if is_port_open("127.0.0.1", 11434):
                    ok("Ollama сервис запущен")
                    break
                time.sleep(1)
        except Exception as e:
            warn(f"Не удалось запустить: {e}")

    return True


# ═══════════════════════════════════════════════════════════════════════
# Setup: qwenproxy
# ═══════════════════════════════════════════════════════════════════════

def setup_qwenproxy() -> bool:
    section("qwenproxy — LLM backend")

    qp = which("qpx") or which("qwenproxy")
    if not qp:
        warn("qpx (qwenproxy-cli) не установлен")
        info("npm install -g qwenproxy-cli")
        info("npx playwright install chromium")
        info("Аккаунт: запустите qpx → вкладка [5] Accounts → A "
             "(email и пароль от chat.qwen.ai)")
        return False

    if is_port_open("127.0.0.1", 7936):
        ok("qwenproxy уже работает")
        return True

    print()
    ans = input("  Запустить qwenproxy на порту 7936? [Y/n]: ").strip().lower()
    if ans not in ("", "y", "yes", "д", "да"):
        return False

    info("Запускаю qwenproxy...")
    try:
        subprocess.Popen(
            [qp, "start"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for i in range(30):
            if is_port_open("127.0.0.1", 7936):
                ok(f"qwenproxy готов за {i+1} сек")
                return True
            time.sleep(1)
        warn("qwenproxy не отвечает (проверьте вкладку [5] Accounts в qpx)")
        return False
    except Exception as e:
        warn(f"Не удалось запустить: {e}")
        return False


# ═══════════════════════════════════════════════════════════════════════
# Setup: PostgreSQL — создание БД и схем
# ═══════════════════════════════════════════════════════════════════════

def setup_postgres(reset: bool = False) -> bool:
    section("PostgreSQL — инициализация БД")

    psql = which("psql")
    if not psql:
        fail("psql не найден")
        return False

    host = os.getenv("PG_APP_HOST", "localhost")
    port = os.getenv("PG_APP_PORT", "5432")
    user = os.getenv("PG_APP_USER", "llmagent")
    password = os.getenv("PG_APP_PASSWORD", "secret")
    database = os.getenv("PG_APP_DATABASE", "llmagent")

    env = {"PGPASSWORD": password}

    # Проверка подключения
    code, _, err = run_capture(
        [psql, "-h", host, "-p", port, "-U", user, "-d", database, "-c", "SELECT 1"],
        timeout=15, env=env,
    )
    if code != 0:
        # БД не существует — попробуем создать через postgres
        warn(f"Не могу подключиться к {database}. Пробую создать...")

        # Нужен пароль суперпользователя
        pg_pass = os.getenv("PG_SUPERUSER_PASSWORD", "")
        if not pg_pass:
            print()
            pg_pass = input("  Пароль пользователя postgres: ").strip()

        env_su = {"PGPASSWORD": pg_pass}

        # Проверка подключения как postgres
        code_su, _, _ = run_capture(
            [psql, "-U", "postgres", "-c", "SELECT 1"],
            timeout=10, env=env_su,
        )
        if code_su != 0:
            fail("Не могу подключиться как postgres")
            return False

        # Создать пользователя и БД
        info(f"Создаю пользователя {user}...")
        run_capture([
            psql, "-U", "postgres", "-c",
            f"CREATE USER {user} WITH PASSWORD '{password}';",
        ], env=env_su)

        info(f"Создаю БД {database}...")
        run_capture([
            psql, "-U", "postgres", "-c",
            f"CREATE DATABASE {database};",
        ], env=env_su)

        run_capture([
            psql, "-U", "postgres", "-d", database, "-c",
            f"GRANT ALL PRIVILEGES ON DATABASE {database} TO {user};",
        ], env=env_su)

        run_capture([
            psql, "-U", "postgres", "-d", database, "-c",
            f"GRANT ALL ON SCHEMA public TO {user};",
        ], env=env_su)

        ok("Пользователь и БД созданы")

    # Reset (если нужно)
    if reset:
        print()
        ans = input(
            "  ⚠️  RESET: удалить все данные и пересоздать БД? [y/N]: "
        ).strip().lower()
        if ans in ("y", "yes", "д", "да"):
            info("Удаляю схемы...")
            sql = """
                DROP SCHEMA IF EXISTS memory CASCADE;
                DROP SCHEMA IF EXISTS vectors CASCADE;
                DROP SCHEMA IF EXISTS graph CASCADE;
                DROP SCHEMA IF EXISTS cache CASCADE;
                DROP SCHEMA IF EXISTS policies CASCADE;
                DROP SCHEMA IF EXISTS audit CASCADE;
                DROP SCHEMA IF EXISTS metrics CASCADE;
                DROP SCHEMA IF EXISTS meta CASCADE;
            """
            run_capture(
                [psql, "-h", host, "-p", port, "-U", user, "-d", database,
                 "-c", sql],
                env=env,
            )
            ok("Схемы удалены")

    # Применить SQL-файлы
    sql_files = [
        ("db/init.sql", "Базовая схема"),
        ("db/analytics.sql", "Analytics MV"),
        ("db/search_improvements.sql", "Snowball + синонимы"),
        ("db/cdc_notify.sql", "CDC триггеры"),
    ]

    for sql_path, desc in sql_files:
        f = BASE_DIR / sql_path
        if not f.exists():
            warn(f"{sql_path} не найден")
            continue
        info(f"Применяю {sql_path} ({desc})...")
        code, out, err = run_capture(
            [psql, "-h", host, "-p", port, "-U", user, "-d", database,
             "-f", str(f)],
            timeout=120, env=env,
        )
        if code == 0:
            ok(f"{desc} — применено")
        else:
            # Проверяем, критично ли
            if "already exists" in (err or "").lower():
                ok(f"{desc} — уже применено")
            else:
                fail(f"{desc} — ошибка")
                info((err or out)[-300:])

    return True


# ═══════════════════════════════════════════════════════════════════════
# Setup: AGE
# ═══════════════════════════════════════════════════════════════════════

def setup_age() -> bool:
    section("Apache AGE")

    if not is_port_open("localhost", 5433):
        warn("AGE недоступен на 5433 (Docker не запущен?)")
        return False

    psql = which("psql")
    if not psql:
        return False

    age_sql = BASE_DIR / "db" / "age_schema.sql"
    if not age_sql.exists():
        warn("age_schema.sql не найден")
        return False

    env = {"PGPASSWORD": os.getenv("PG_APP_PASSWORD", "secret")}

    info("Применяю age_schema.sql...")
    code, out, err = run_capture(
        [psql, "-h", "localhost", "-p", "5433",
         "-U", os.getenv("PG_APP_USER", "llmagent"),
         "-d", os.getenv("PG_APP_DATABASE", "llmagent"),
         "-f", str(age_sql)],
        timeout=60, env=env,
    )
    if code == 0:
        ok("AGE схема применена")
        return True
    else:
        # Может уже быть применено
        if "already exists" in (err or out).lower():
            ok("AGE схема уже применена")
            return True
        warn("AGE схема не применилась")
        info((err or out)[-300:])
        return False


# ═══════════════════════════════════════════════════════════════════════
# Setup: миграции
# ═══════════════════════════════════════════════════════════════════════

def setup_migrations() -> bool:
    section("Миграции данных (LanceDB → PG, SQLite → PG)")

    scripts = [
        ("scripts/migrate_lancedb_to_pg.py", "LanceDB → PostgreSQL"),
        ("scripts/migrate_sqlite_memory.py", "SQLite memory → PostgreSQL"),
        ("scripts/migrate_graph_to_pg.py", "SQLite graph → PostgreSQL"),
    ]

    available = []
    for path, desc in scripts:
        f = BASE_DIR / path
        if f.exists():
            available.append((f, desc))

    if not available:
        info("Миграционные скрипты не найдены (нечего мигрировать)")
        return True

    # Проверяем, есть ли данные для миграции
    has_old_data = (
        (BASE_DIR / "data" / "vector.lance").exists() or
        (BASE_DIR / "data" / "memory.sqlite").exists() or
        (BASE_DIR / "data" / "graph.sqlite").exists()
    )

    if not has_old_data:
        ok("Старых данных нет — миграция не нужна")
        return True

    print()
    ans = input(
        "  Найдены старые данные. Запустить миграцию? [y/N]: "
    ).strip().lower()
    if ans not in ("y", "yes", "д", "да"):
        info("Пропускаю миграции")
        return True

    for f, desc in available:
        info(f"Миграция: {desc}...")
        code, out, err = run_capture(
            [sys.executable, str(f)],
            timeout=3600,
        )
        if code == 0:
            ok(f"{desc} — успешно")
        else:
            warn(f"{desc} — ошибка")
            info((err or out)[-300:])

    return True


# ═══════════════════════════════════════════════════════════════════════
# Проверка системы
# ═══════════════════════════════════════════════════════════════════════

def check_system_health() -> dict:
    section("Финальная проверка системы")

    results = {}

    # PostgreSQL
    if is_port_open("localhost", 5432):
        ok("PostgreSQL:5432 ✓")
        results["postgres"] = True
    else:
        fail("PostgreSQL:5432 ✗")
        results["postgres"] = False

    # Ollama
    if is_port_open("127.0.0.1", 11434):
        ok("Ollama:11434 ✓")
        results["ollama"] = True
    else:
        warn("Ollama:11434 ✗ (обогащение не будет работать)")
        results["ollama"] = False

    # qwenproxy
    if is_port_open("127.0.0.1", 7936):
        ok("qwenproxy:7936 ✓")
        results["qwenproxy"] = True
    else:
        warn("qwenproxy:7936 ✗ (LLM-запросы не будут работать)")
        results["qwenproxy"] = False

    # AGE
    if is_port_open("localhost", 5433):
        ok("AGE:5433 ✓")
        results["age"] = True
    else:
        info("AGE:5433 — не запущен (опционально)")
        results["age"] = False

    # Kafka
    if is_port_open("localhost", 9092):
        ok("Kafka:9092 ✓")
        results["kafka"] = True
    else:
        info("Kafka:9092 — не запущен (опционально)")
        results["kafka"] = False

    # Проверка БД
    try:
        from src.db.pool import get_pool
        import asyncio

        async def _check():
            pool = await get_pool()
            return await pool.health()

        healthy = asyncio.run(_check())
        if healthy:
            ok("PostgreSQL pool: OK")
            results["pg_pool"] = True
        else:
            warn("PostgreSQL pool: не отвечает")
            results["pg_pool"] = False
    except Exception as e:
        warn(f"PostgreSQL pool: {e}")
        results["pg_pool"] = False

    return results


# ═══════════════════════════════════════════════════════════════════════
# Запуск
# ═══════════════════════════════════════════════════════════════════════

def run_project() -> int:
    section("Запуск LLM Agent")

    # Загрузка settings
    try:
        from src.config import get_settings
        s = get_settings()
    except Exception as e:
        fail(f"Не могу загрузить config: {e}")
        info("Проверьте .env и requirements.txt")
        return 1

    host = s.web_host
    port = s.web_port

    # Проверка порта
    if is_port_open(host, port):
        fail(f"Порт {port} занят")
        free = None
        for p in range(port + 1, port + 20):
            if not is_port_open(host, p):
                free = p
                break
        if free:
            print()
            ans = input(f"  Использовать порт {free}? [Y/n]: ").strip().lower()
            if ans in ("", "y", "yes", "д", "да"):
                port = free
            else:
                return 1
        else:
            return 1

    print()
    print(_color(C.BOLD + C.GREEN, f"  🚀 Запуск на http://{host}:{port}"))
    print(_color(C.DIM, "     Ctrl+C — остановка"))
    print()

    try:
        import uvicorn
        uvicorn.run(
            "src.main:app",
            host=host,
            port=port,
            reload=False,
            log_level="info",
        )
    except KeyboardInterrupt:
        info("Остановлено пользователем")
        return 0
    except Exception as e:
        fail(f"Ошибка запуска: {e}")
        return 1

    return 0


# ═══════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════

def main() -> int:
    parser = argparse.ArgumentParser(
        description="LLM Agent — setup & run",
    )
    parser.add_argument("--skip-install", action="store_true",
                        help="Пропустить pip install")
    parser.add_argument("--skip-docker", action="store_true",
                        help="Не поднимать Docker")
    parser.add_argument("--skip-ollama", action="store_true",
                        help="Не трогать Ollama")
    parser.add_argument("--skip-migrations", action="store_true",
                        help="Не мигрировать данные")
    parser.add_argument("--skip-checks", action="store_true",
                        help="Не проверять сервисы")
    parser.add_argument("--reset-db", action="store_true",
                        help="Удалить и создать БД заново")
    parser.add_argument("--only-check", action="store_true",
                        help="Только проверить, не запускать")

    args = parser.parse_args()

    banner()

    # 1. Проверки окружения
    if not check_python():
        return 1

    check_node()
    check_files()

    if not setup_env():
        return 1

    # Перезагрузка env
    from dotenv import load_dotenv
    load_dotenv(ENV_FILE, override=True)

    # 2. PostgreSQL
    pg_ok = check_postgres()

    # 3. pg_dump
    check_pg_dump()

    # 4. Docker
    docker_ok = check_docker()

    # 5. Ollama
    ollama_ok = check_ollama()

    # 6. qwenproxy
    qwen_ok = check_qwenproxy()

    # ─── Setup (если не only-check) ─────────────────────────
    if args.only_check:
        section("Итог проверок")
        ok("Проверки завершены (--only-check)")
        return 0

    # 7. Установка зависимостей
    if not args.skip_install:
        if not setup_pip():
            fail("Установка зависимостей не удалась")
            return 1

    # 8. Docker сервисы
    if not args.skip_docker and docker_ok:
        setup_docker()

    # 9. Ollama
    if not args.skip_ollama and ollama_ok:
        setup_ollama()

    # 10. qwenproxy
    if qwen_ok:
        setup_qwenproxy()

    # 11. PostgreSQL — инициализация
    if pg_ok:
        if not setup_postgres(reset=args.reset_db):
            fail("Инициализация БД не удалась")
            return 1

    # 12. AGE
    if not args.skip_docker:
        setup_age()

    # 13. Миграции
    if not args.skip_migrations:
        setup_migrations()

    # 14. Финальная проверка
    if not args.skip_checks:
        health = check_system_health()

        # Критично: PostgreSQL
        if not health.get("postgres"):
            fail("PostgreSQL обязателен. Проверьте настройки.")
            return 1

        # Предупреждения
        if not health.get("qwenproxy"):
            warn("qwenproxy не работает — LLM-запросы не пройдут")
            print()
            ans = input("  Продолжить всё равно? [y/N]: ").strip().lower()
            if ans not in ("y", "yes", "д", "да"):
                return 1

    # 15. Запуск
    return run_project()


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
        info("Прервано пользователем")
        sys.exit(130)
    except Exception as e:
        print()
        fail(f"Непредвиденная ошибка: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
