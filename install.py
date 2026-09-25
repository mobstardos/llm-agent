#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# ═══════════════════════════════════════════════════════════════════════
# LLM Agent — кроссплатформенный интерактивный установщик (Python)
#
# Замена install.bat/install.sh: не зависит от причуд cmd.exe (кодировки,
# CRLF, задержанное раскрытие «!») и не «вылетает» молча — при любой
# ошибке печатает причину и ждёт Enter.
#
#   python install.py            # интерактивно
#   python install.py --auto     # тихо, все вопросы по умолчанию
#
# Что делает:
#   1. Проверяет Python >= 3.11; на free-threaded сборках (3.13t/3.14t)
#      автоматически берёт requirements-freethreaded.txt (усечённый набор).
#      Детекция t-сборки устойчивая, работает и на Windows: суффикс
#      расширений (.cp314t-win_amd64.pyd), Py_GIL_DISABLED, sys.abiflags,
#      путь интерпретатора (sysconfig на Windows часто НЕ отдаёт
#      Py_GIL_DISABLED — из-за этого прежняя версия ошибалась).
#      Нет requirements-freethreaded.txt? Усечённый набор генерируется
#      из requirements.txt. Полный набор не сходится на 3.14+ (нет
#      колёс)? pip-установка автоматически повторяется на усечённом.
#   2. Создаёт .venv и ставит зависимости (живой вывод pip)
#   3. Спрашивает: порт, PostgreSQL (docker / внешний / пропустить),
#      режим embedder памяти агентов
#   4. Пишет .env (существующий не перезаписывает)
#   5. Накатывает схему БД (scripts/init_db.py), если выбран PG
#   6. Прогоняет смоук-тест сервера (старт → health → остановка)
# ═══════════════════════════════════════════════════════════════════════
from __future__ import annotations

import argparse
import importlib.machinery
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import time
import traceback
import urllib.request
from collections import deque
from pathlib import Path

BASE = Path(__file__).resolve().parent
IS_WIN = os.name == "nt"
VENV_DIR = BASE / ".venv"
VP = VENV_DIR / ("Scripts\\python.exe" if IS_WIN else "bin/python")

# ANSI-цвета: на Windows 10+ достаточно «разбудить» терминал пустым os.system
if IS_WIN:
    os.system("")
# При перенаправлении вывода в файл консоль может быть не UTF-8 — не падать
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(errors="replace")
    except Exception:
        pass

USE_COLOR = sys.stdout.isatty()
C_RESET = "\033[0m" if USE_COLOR else ""
C_G = "\033[92m" if USE_COLOR else ""
C_Y = "\033[93m" if USE_COLOR else ""
C_R = "\033[91m" if USE_COLOR else ""
C_D = "\033[2m" if USE_COLOR else ""


def ok(msg: str) -> None:
    print(f" {C_G}[✓]{C_RESET} {msg}")

def warn(msg: str) -> None:
    print(f" {C_Y}[!]{C_RESET} {msg}")

def err(msg: str) -> None:
    print(f" {C_R}[✗]{C_RESET} {msg}")

def step(msg: str) -> None:
    print(f" {C_D}[·]{C_RESET} {msg}")


def pause_if_needed(auto: bool) -> None:
    """Чтобы окно не закрылось молча (двойной клик по install.py)."""
    if auto or not IS_WIN or not sys.stdin.isatty():
        return
    try:
        input("\nНажмите Enter для выхода...")
    except (EOFError, KeyboardInterrupt):
        pass


def ask(prompt: str, default: str) -> str:
    try:
        val = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        val = ""
    return val or default


def check_python() -> bool:
    v = sys.version_info
    if (v.major, v.minor) < (3, 11):
        err(f"Python {v.major}.{v.minor}.{v.micro} — требуется >= 3.11.")
        print("     Установите с https://www.python.org/downloads/")
        print("     и отметьте галочку «Add python.exe to PATH».")
        return False
    ok(f"Python {v.major}.{v.minor}.{v.micro} найден")
    if v.minor >= 14:
        ok("Python 3.14+ поддержан")
        step(f"детали: EXT_SUFFIX={sysconfig.get_config_var('EXT_SUFFIX') or '?'}, "
             f"Py_GIL_DISABLED={sysconfig.get_config_var('Py_GIL_DISABLED')}")
    return True


def is_freethreaded() -> tuple[bool, str]:
    """Надёжная детекция free-threaded сборки.

    На Windows sysconfig.get_config_var("Py_GIL_DISABLED") часто возвращает
    None (у установочных сборок нет полного sysconfigdata), поэтому главный
    признак — суффикс бинарных расширений: t-сборка -> .cp314t-win_amd64.pyd
    (Windows) / .cpython-314t-...so (Linux); обычная -> .cp314-win_amd64.pyd.
    """
    ext = ""
    try:
        suffixes = importlib.machinery.EXTENSION_SUFFIXES
        ext = suffixes[0] if suffixes else ""
    except Exception:
        pass
    if not ext:
        ext = str(sysconfig.get_config_var("EXT_SUFFIX") or "")
    if re.search(r"\d+t-", ext):          # cp314t- / cpython-314t-
        return True, f"суффикс расширений {ext}"
    if str(sysconfig.get_config_var("Py_GIL_DISABLED") or "0") == "1":
        return True, "Py_GIL_DISABLED=1"
    abif = str(getattr(sys, "abiflags", "") or "")
    if "t" in abif:
        return True, f"sys.abiflags={abif!r}"
    base = str(getattr(sys, "base_prefix", "") or sys.executable or "").lower()
    if re.search(r"(?:python)?3\.?\d{1,2}t$", base):
        return True, f"путь интерпретатора ({base})"
    return False, ""


# Пакеты без t-колёс (win_amd64, PyPI, 2026-09) — см. requirements-freethreaded.txt
FT_EXCLUDE = {
    "lancedb", "sentence-transformers", "duckdb", "aiokafka",
    "faster-whisper", "pydoc-markdown", "memray", "onnxruntime",
    "tokenizers", "safetensors",
}
FT_EXCLUDE_PREFIX = "tree-sitter"   # + все грамматики tree-sitter-*


def _split_req(line: str) -> tuple[str, str, str]:
    """'psycopg[binary,pool]>=3.2.0' -> ('psycopg', 'binary,pool', '>=3.2.0')."""
    m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)(\[[^\]]*\])?(.*)$",
                 line.strip())
    if not m:
        return "", "", ""
    return m.group(1), (m.group(2) or "").strip("[]"), m.group(3).strip()


def ensure_freethreaded_file() -> str:
    """Имя freethreaded-набора: кураторский файл, а если его нет (проект
    скопирован не полностью) — автогенерация усечённого из requirements.txt."""
    curated = BASE / "requirements-freethreaded.txt"
    if curated.exists():
        return curated.name
    auto = BASE / "requirements-freethreaded.auto.txt"
    out = [
        "# ═════════════════════════════════════════════════════════════════",
        "# LLM Agent — freethreaded-набор, АВТОМАТИЧЕСКИ сгенерирован install.py",
        "# из requirements.txt (кураторский requirements-freethreaded.txt не найден).",
        "# ═════════════════════════════════════════════════════════════════",
    ]
    src = BASE / "requirements.txt"
    lines = (src.read_text(encoding="utf-8", errors="replace").splitlines()
             if src.exists() else [])
    for raw in lines:
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            out.append(raw)
            continue
        name, extras, spec = _split_req(stripped)
        low = name.lower()
        if low and (low in FT_EXCLUDE or low.startswith(FT_EXCLUDE_PREFIX)):
            out.append(f"# [freethreaded] пропущен, нет t-колёс: {stripped}")
        elif low == "uvicorn" and "standard" in extras.lower():
            out.append(f"# [freethreaded] было: {stripped}  (uvloop не ставится под t)")
            out.append(f"uvicorn{spec}")
            out.append("watchfiles>=1.0.0")
        elif low == "psycopg" and "binary" in extras.lower():
            out.append(f"psycopg{spec}  # без [binary]: psycopg-binary без t-сборок")
        else:
            out.append(raw)
    auto.write_text("\n".join(out) + "\n", encoding="utf-8")
    step(f"requirements-freethreaded.txt не найден — сгенерировал {auto.name}")
    return auto.name


def choose_requirements() -> str:
    ft, why = is_freethreaded()
    if not ft:
        if sys.version_info >= (3, 14):
            warn("Python 3.14+ обычной сборки: пробую полный requirements.txt.")
            print("     Если pip не найдёт колёса — сам повторю на усечённом наборе.")
        return "requirements.txt"
    reqfile = ensure_freethreaded_file()
    print()
    warn(f"FREE-THREADED сборка Python обнаружена ({why}) — беру усечённый набор.")
    print("     Отличия от полного: без lancedb, sentence-transformers,")
    print("     tree-sitter, duckdb, aiokafka, faster-whisper.")
    print("     psycopg — чистый, без бинарника: PG-фичи отдадут 503, пока")
    print("     libpq не появится в PATH (например: поставить PostgreSQL")
    print("     client и добавить его bin в PATH). Векторное хранилище —")
    print("     postgres/файлы, embedder — быстрый hashing; граф кода, SQL-")
    print("     аналитика MCP, CDC и транскрипция речи — отключены.")
    return reqfile


def make_venv() -> bool:
    if VENV_DIR.exists() and not VP.exists():
        err(".venv есть, но интерпретатор внутри не найден (битое окружение).")
        print("     Удалите папку .venv и запустите install.py снова.")
        return False
    if not VENV_DIR.exists():
        step("Создаю виртуальное окружение .venv ...")
        r = subprocess.run([sys.executable, "-m", "venv", str(VENV_DIR)])
        if r.returncode != 0 or not VP.exists():
            err("Не удалось создать venv.")
            if not IS_WIN:
                print("     На Debian/Ubuntu: sudo apt install python3-venv")
            return False
    return True


def _pip_install(reqfile: str) -> bool:
    """pip install -r <файл> с живым выводом; хвост лога — для диагностики."""
    env = os.environ.copy()
    env.setdefault("PYTHONUTF8", "1")
    env.setdefault("PYTHONIOENCODING", "utf-8")
    cmd = [str(VP), "-m", "pip", "install", "-r", str(BASE / reqfile)]
    tail: deque = deque(maxlen=80)
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", env=env, bufsize=1)
    except Exception as e:
        err(f"Не удалось запустить pip: {e}")
        return False
    for raw in (proc.stdout or []):
        line = raw.rstrip()
        if line:
            print("   " + line)
        tail.append(line)
    code = proc.wait()
    if code == 0:
        return True
    err(f"pip завершился с кодом {code}.")
    missing = []
    for m in re.finditer(r"No matching distribution found for ([^\s(]+)",
                         "\n".join(tail)):
        if m.group(1) not in missing:
            missing.append(m.group(1))
    if missing:
        print("     Нет колёс под эту сборку Python: " + ", ".join(missing))
    return False


def install_deps(reqfile: str) -> bool:
    marker = VENV_DIR / ".installed"
    if marker.exists():
        ok("Зависимости уже установлены")
        return True
    step(f"Ставлю зависимости из {reqfile} (несколько минут, вывод pip ниже) ...")
    r = subprocess.run([str(VP), "-m", "pip", "install", "--upgrade", "pip"])
    if r.returncode != 0:
        warn("pip не обновился — продолжаю текущей версией.")
    if _pip_install(reqfile):
        marker.write_text(reqfile, encoding="utf-8")
        return True
    # Авто-спасение: на Python 3.14+ полный набор может не разрешиться
    # (нет колёс под конкретную сборку — как lancedb на free-threaded 3.14t).
    if reqfile == "requirements.txt" and sys.version_info >= (3, 14):
        alt = ensure_freethreaded_file()
        warn("pip не смог разрешить полный набор под эту сборку Python.")
        warn(f"Повторяю установку на усечённом freethreaded-наборе: {alt} ...")
        if _pip_install(alt):
            marker.write_text(alt, encoding="utf-8")
            return True
    err("Ошибка установки зависимостей (pip).")
    print("     Проверьте интернет и запустите install.py снова.")
    print("     Если ошибка повторится — пришлите вывод pip выше целиком.")
    return False


def ask_settings(auto: bool) -> tuple[str, str, str, str]:
    port, pg_mode, dsn, embedder = "8000", "skip", "", "auto"
    if auto:
        return port, pg_mode, dsn, embedder
    print()
    print(" ─── Настройка ────────────────────────────────────────────────")
    port = ask(" Порт веб-интерфейса [8000]: ", "8000")
    print()
    print(" PostgreSQL — долговременные зеркала, память агентов, аналитика:")
    print("   [1] Docker-контейнер (нужен Docker Desktop) — рекомендую")
    print("   [2] Внешний сервер — введу DSN вручную")
    print("   [3] Пропустить (работа только на локальных файлах)")
    choice = ask(" Выбор [3]: ", "3")
    pg_mode = {"1": "docker", "2": "external"}.get(choice, "skip")
    if pg_mode == "external":
        dsn = ask(" DSN (postgres://user:pass@host:5432/db): ", "")
        if not dsn:
            warn("DSN пуст — PostgreSQL пропускаю.")
            pg_mode = "skip"
    print()
    print(" Embedder памяти агентов:")
    print("   [1] auto — bge-m3, если получится загрузить, иначе быстрый hashing")
    print("   [2] hash — детерминированный, мгновенный, без загрузки моделей")
    if ask(" Выбор [1]: ", "1") == "2":
        embedder = "hash"
    return port, pg_mode, dsn, embedder


def write_env(port: str, pg_mode: str, dsn: str, embedder: str) -> None:
    env_path = BASE / ".env"
    if env_path.exists():
        ok(".env уже существует — не перезаписываю.")
        return
    step("Пишу .env ...")
    if pg_mode == "external" and dsn:
        pg_block = f"DATABASE_URL={dsn}"
    else:
        pg_block = "\n".join([
            "PG_APP_HOST=localhost",
            "PG_APP_PORT=5432",
            "PG_APP_USER=llmagent",
            "PG_APP_PASSWORD=secret",
            "PG_APP_DATABASE=llmagent",
        ])
    content = "\n".join([
        "# LLM Agent — сгенерировано install.py",
        "HOST=127.0.0.1",
        f"PORT={port}",
        f"PROJECT_ROOT={BASE}",
        "",
        "# PostgreSQL (зеркала журнала/сессий/планов, память агентов, аналитика)",
        pg_block,
        "",
        "# Память агентов: auto | hash | model",
        f"AGENT_MEMORY_EMBEDDER={embedder}",
        "",
        "# Эксплуатация: авто-ретенция выключена по умолчанию",
        "PG_RETENTION_DAYS=0",
        "BACKUP_KEEP_LAST=7",
        "",
    ])
    env_path.write_text(content, encoding="utf-8")
    ok(".env записан")


def run_vpy(script: str, *args: str, allow_fail: bool = False) -> bool:
    r = subprocess.run([str(VP), script, *args], cwd=str(BASE))
    if r.returncode != 0 and not allow_fail:
        return False
    return True


def pg_setup(pg_mode: str) -> None:
    if pg_mode == "docker":
        step("Поднимаю PostgreSQL в Docker ...")
        r = subprocess.run(["docker", "compose", "up", "-d", "postgres"], cwd=str(BASE))
        if r.returncode != 0:
            warn("Docker недоступен — поднимите контейнер вручную:")
            print("     docker compose up -d postgres")
    step("Накатываю схему БД (scripts/init_db.py) ...")
    if not run_vpy("scripts/init_db.py", allow_fail=True):
        warn("Схему накатить не удалось — сервер всё равно стартует "
             "(SQLite-режим).")


def smoke_test(port: str) -> bool:
    step(f"Смоук-тест: стартую сервер на порту {port} (до 2 минут) ...")
    log_path = BASE / ".install_smoke.log"
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if IS_WIN else 0
    try:
        log = open(log_path, "wb")
        proc = subprocess.Popen(
            [str(VP), "run.py", "--skip-checks", "--skip-setup", "--port", port],
            cwd=str(BASE), stdout=log, stderr=subprocess.STDOUT,
            creationflags=flags,
        )
    except Exception as e:
        warn(f"Не удалось запустить фоновый процесс ({e}) — запустите run.bat/run.sh.")
        return False
    url = f"http://127.0.0.1:{port}/api/db/status"
    healthy = False
    for _ in range(24):  # ~120 c: первый старт долгий (38+ MCP-серверов)
        time.sleep(5)
        if proc.poll() is not None:
            break  # процесс умер раньше времени — смотри .install_smoke.log
        try:
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status == 200:
                    healthy = True
                    break
        except Exception:
            continue
    if IS_WIN:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/F", "/T"],
                       capture_output=True)
    else:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except Exception:
            proc.kill()
    log.close()
    return healthy


def banner() -> None:
    print()
    print(" ┌─────────────────────────────────────────────────────────┐")
    print(" │        LLM Agent — интерактивная установка на ПК        │")
    print(" └─────────────────────────────────────────────────────────┘")
    print()


def done_block(port: str) -> None:
    run_cmd = "run.bat" if IS_WIN else "bash run.sh"
    vpy = (".venv\\Scripts\\python.exe run.py" if IS_WIN
           else ".venv/bin/python run.py")
    print()
    print(" ─── Готово ───────────────────────────────────────────────────")
    print(f"  Запуск:               {run_cmd}  (или: {vpy})")
    print(f"  Веб-интерфейс:        http://127.0.0.1:{port}")
    print("  Настройки AI:         python first_run.py --reconfigure-ai")
    print("  Проверка БД:          python scripts/init_db.py --check")
    print("  Переменные окружения: deploy/env.example")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--auto", action="store_true",
                        help="тихий режим: все вопросы по умолчанию")
    auto = parser.parse_known_args()[0].auto

    banner()
    if not check_python():
        return 1
    reqfile = choose_requirements()

    if not make_venv():
        return 1
    if not install_deps(reqfile):
        return 1

    port, pg_mode, dsn, embedder = ask_settings(auto)
    write_env(port, pg_mode, dsn, embedder)

    step("Первичная настройка (каталоги, базы, маркер) ...")
    if not run_vpy("first_run.py", "--defaults", allow_fail=True):
        warn("first_run завершился с предупреждениями — не критично")

    if pg_mode != "skip":
        pg_setup(pg_mode)

    if smoke_test(port):
        ok("Сервер отвечает: HTTP 200")
    else:
        warn("Сервер не ответил за 2 минуты (лог: .install_smoke.log).")
        print("     Это нормально для первого старта — запустите "
              f"{'run.bat' if IS_WIN else 'run.sh'}:")
        print("     продолжится запуск и пройдёт мастер настройки AI.")

    done_block(port)
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except KeyboardInterrupt:
        print()
        err("Прервано пользователем.")
        code = 130
    except Exception:
        print()
        err("Непредвиденная ошибка установщика:")
        traceback.print_exc()
        print("     Пришлите этот вывод разработчику.")
        code = 1
    pause_if_needed("--auto" in sys.argv)
    sys.exit(code)
