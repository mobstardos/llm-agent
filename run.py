"""LLM Agent — точка входа с проверками и автозапуском qwenproxy."""
from __future__ import annotations

import argparse
import importlib
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

# Windows: при перенаправленном stdout (bat → файл, трубопровод) консольная
# кодировка может быть cp1251 — print("✓") тогда падает UnicodeEncodeError
# и весь запуск «случайно» умирает. Не падаем никогда: заменяем символ.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001 — не-TextIO поток, старый Python
        pass

from src.config import get_settings


class C:
    RESET = "\033[0m"; BOLD = "\033[1m"; DIM = "\033[2m"
    RED = "\033[31m"; GREEN = "\033[32m"; YELLOW = "\033[33m"
    CYAN = "\033[36m"; MAGENTA = "\033[35m"


def _color(text: str, code: str) -> str:
    return f"{code}{text}{C.RESET}" if sys.stdout.isatty() else text


def ok(msg): print(f"  {_color('✓', C.GREEN)} {msg}")
def fail(msg): print(f"  {_color('✗', C.RED)} {msg}")
def warn(msg): print(f"  {_color('!', C.YELLOW)} {msg}")
def info(msg): print(f"  {_color('·', C.DIM)} {msg}")
def section(title):
    line = "─" * max(0, 60 - len(title) - 4)
    print(f"\n{_color('┌─ ' + title + ' ' + line, C.BOLD + C.CYAN)}")


def ask(prompt: str, default: str = "y") -> str:
    """input() без сюрпризов: EOF/Ctrl+C/закрытый stdin → значение по умолчанию.

    Без этого запуск под планировщиком/сервисом или с перенаправленным
    stdin падал с EOFError — «работает через раз».
    """
    try:
        return input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        warn("ввод недоступен — беру ответ по умолчанию")
        return default


def is_port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.6)
        return s.connect_ex((host, port)) == 0


def which(name: str) -> str | None:
    if os.name == "nt":
        for ext in (".cmd", ".exe", ".bat", ""):
            p = shutil.which(name + ext)
            if p:
                return p
    return shutil.which(name)


def try_import(module: str) -> bool:
    importlib.invalidate_caches()
    try:
        importlib.import_module(module)
        return True
    except ImportError:
        return False


def run_capture(cmd, timeout=20):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, shell=(os.name == "nt"))
        return p.returncode, p.stdout or "", p.stderr or ""
    except Exception as e:
        return 1, "", str(e)


def check_python() -> bool:
    section("Python")
    v = sys.version_info
    if (v.major, v.minor) < (3, 10):
        fail(f"Python {v.major}.{v.minor} — требуется ≥ 3.10")
        return False
    ok(f"Python {v.major}.{v.minor}.{v.micro}")
    if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        ok(f"venv: {sys.prefix}")
    else:
        warn("venv не активирован")
    return True


def check_node() -> bool:
    section("Node.js")
    node = which("node")
    if not node:
        warn("Node.js не найден — qwenproxy не запустится")
        return False
    code, out, _ = run_capture([node, "--version"])
    if code == 0:
        ok(f"Node {out.strip()}")
    return True


def check_dependencies() -> bool:
    section("Python-зависимости")
    critical = [
        ("fastapi", "fastapi"),
        ("uvicorn", "uvicorn[standard]"),
        ("openai", "openai"),
        ("mcp", "mcp"),
        ("httpx", "httpx"),
        ("pydantic", "pydantic"),
        ("yaml", "PyYAML"),
        ("dotenv", "python-dotenv"),
    ]
    optional = [
        ("pymysql", "pymysql"),
        ("psycopg", "psycopg[binary]"),
        ("lancedb", "lancedb"),
        ("sentence_transformers", "sentence-transformers"),
        ("watchdog", "watchdog"),
    ]
    missing_critical: list[str] = []
    missing_optional: list[str] = []

    for module, pkg in critical:
        if try_import(module):
            ok(f"{module}")
        else:
            fail(f"{module}")
            missing_critical.append(pkg)

    for module, pkg in optional:
        if try_import(module):
            ok(f"{module}")
        else:
            warn(f"{module} (опционально)")
            missing_optional.append(pkg)

    if missing_critical:
        answer = ask(
            f"\n  Установить {len(missing_critical)} критичных пакетов? [Y/n]: ")
        if answer in ("", "y", "yes", "д", "да"):
            info("Устанавливаю...")
            code, out, err = run_capture(
                [sys.executable, "-m", "pip", "install", *missing_critical],
                timeout=600,
            )
            if code != 0:
                fail("Ошибка установки:")
                print(err or out)
                return False
            ok("Установлено")
            importlib.invalidate_caches()
            for module, _ in critical:
                if not try_import(module):
                    fail(f"{module} не подхватился — перезапустите run.py")
                    return False
        else:
            return False
    return True


def check_pg_autodetect() -> None:
    """PostgreSQL-автодетект (Task 24-b): при каждом запуске.

    Служба есть, пароль не подходит → словарик дефолтов; не помогло —
    спросим пароль админа (мы в TTY). Успех → .env + os.environ,
    bootstrap роли llmagent и идемпотентная схема (src.db.init_db).
    """
    section("PostgreSQL (автодетект)")
    try:
        from src.db.autodetect import ensure_pg
    except Exception as e:  # noqa: BLE001 — модуль не должен валить старт
        warn(f"модуль автодетекта недоступен: {e}")
        return
    rep = ensure_pg(interactive=True)
    status = rep.get("status")
    if status == "already":
        ok(f"Подключение работает: {rep.get('dsn', '?')}")
    elif status == "ok":
        ok(f"PostgreSQL подключён ({rep.get('via', '?')}) — "
           f"{rep.get('hint', '')}")
        if rep.get("port_switched"):
            info(f"порт: {rep['port_switched']}")
        sch = rep.get("schema") or {}
        if sch.get("already_ok"):
            info("схема на месте (проверка init_db --check)")
        elif sch.get("applied"):
            info(f"схема применена (ошибок: {sch.get('errors', 0)})")
    elif status == "no_service":
        warn("PostgreSQL не найден — система работает на SQLite/LanceDB")
    elif status == "no_driver":
        warn("psycopg не установлен — " + rep.get("hint", ""))
    elif status == "manual":
        warn(rep.get("hint", "PostgreSQL требует ручной настройки"))
    else:
        info(f"статус: {status} — {rep.get('hint', '')}")


def check_qwenproxy() -> subprocess.Popen | None:
    section("QwenProxy")
    s = get_settings()

    if not s.qwenproxy.enabled:
        # Пользователь В ДИАЛОГЕ выключил qwenproxy (QWENPROXY_ENABLED=false)
        # — это не ошибка, и красный ✗ здесь только путает
        info("выключен (QWENPROXY_ENABLED=false) — необязательный путь; "
             "основной может работать через куки веб-чатов")
        return None

    if is_port_open(s.qwenproxy.host, s.qwenproxy.port):
        ok(f"Уже работает на {s.qwenproxy.host}:{s.qwenproxy.port}")
        return None

    qp = which("qpx") or which("qwenproxy")
    if not qp:
        fail("qpx (qwenproxy-cli) не найден")
        info("Установите: npm install -g qwenproxy-cli")
        info("Затем: запустите qpx → вкладка [5] Accounts → A "
             "(email и пароль от chat.qwen.ai)")
        return None

    if not s.qwenproxy.auto_start:
        return None

    print()
    answer = ask(
        f"  Запустить qwenproxy на {s.qwenproxy.host}:{s.qwenproxy.port}? [Y/n]: ")
    if answer not in ("", "y", "yes", "д", "да"):
        return None

    info(f"Запускаю {qp}")
    try:
        proc = subprocess.Popen(
            [qp, "start", "--port", str(s.qwenproxy.port)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1,
        )
    except Exception as e:
        fail(f"Не удалось запустить: {e}")
        return None

    for i in range(60):
        time.sleep(1)
        if is_port_open(s.qwenproxy.host, s.qwenproxy.port):
            ok(f"Готов за {i+1}с")
            return proc
    fail("Таймаут (60с)")
    return proc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-checks", action="store_true")
    parser.add_argument("--skip-setup", action="store_true",
                        help="не запускать мастер первого запуска")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()

    print(_color(
        "\n╔══════════════════════════════════════════════════════╗\n"
        "║            🤖  LLM Agent — запуск системы            ║\n"
        "╚══════════════════════════════════════════════════════╝",
        C.BOLD + C.MAGENTA,
    ))

    qp_proc: subprocess.Popen | None = None
    try:
        if not args.skip_checks:
            # Первый запуск → интерактивный мастер настройки (first_run.py)
            if not args.skip_setup:
                from first_run import ensure_configured
                if not ensure_configured():
                    return 1
            if not check_python(): return 1
            check_node()
            if not check_dependencies(): return 1
            check_pg_autodetect()
            qp_proc = check_qwenproxy()

        import uvicorn
        s = get_settings()
        port = args.port or s.web_port
        section("Запуск сервера")
        if is_port_open(s.web_host, port):
            # Диагностика ДО uvicorn: иначе пользователь видит сырой
            # [Errno 10048] и не понимает, что старый экземпляр держит
            # порт и НЕ видит свежий .env (куки и т.п.)
            fail(f"Порт {port} занят — обычно это ещё работающий "
                 f"старый экземпляр LLM Agent")
            info("Он НЕ подхватит свежий .env (например, куки из мастера) "
                 "— нужен перезапуск:")
            info(f"  1) остановите старый: Ctrl+C в его окне; либо "
                 f"netstat -ano | findstr :{port} → taskkill /PID <PID> /F")
            info("  2) запустите заново: python run.py")
            info(f"Альтернатива: другой порт — python run.py --port {port + 1}")
            return 1
        ok(f"http://{s.web_host}:{port}")
        uvicorn.run(
            "src.main:app", host=s.web_host, port=port,
            reload=False, log_level="info",
        )
    except KeyboardInterrupt:
        pass
    finally:
        if qp_proc is not None:
            info("Останавливаю qwenproxy...")
            try:
                qp_proc.terminate()
                qp_proc.wait(timeout=5)
            except Exception:
                try:
                    qp_proc.kill()
                except Exception:
                    pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
