@echo off
chcp 65001 >nul 2>&1
REM ═══════════════════════════════════════════════════════════════════════
REM LLM Agent — интерактивный установщик для Windows 10/11 x64
REM
REM Что делает:
REM   1. Проверяет Python 3.11-3.14 (обычная сборка); на free-threaded
REM      3.13t/3.14t ставит усечённый набор requirements-freethreaded.txt
REM   2. Создаёт .venv и ставит зависимости
REM   3. Спрашивает: порт, PostgreSQL (docker / внешний / пропустить),
REM      режим embedder для памяти агентов
REM   4. Записывает .env (существующий не перезаписывает)
REM   5. Накатывает схему БД (scripts/init_db.py), если выбран PG
REM   6. Прогоняет смоук-тест сервера (старт → health → остановка)
REM
REM Тихий режим: install.bat --auto  (все вопросы — по умолчанию)
REM ═══════════════════════════════════════════════════════════════════════
cd /d "%~dp0"
setlocal EnableDelayedExpansion
title LLM Agent — установка

set AUTO=0
if /i "%~1"=="--auto" set AUTO=1

echo.
echo  ┌─────────────────────────────────────────────────────────┐
echo  │        LLM Agent — интерактивная установка на ПК        │
echo  └─────────────────────────────────────────────────────────┘
echo.

REM ─── 1. Python ────────────────────────────────────────────────────────
set PY=
REM Сначала точные обычные версии (3.14 — у новых ПК, 3.13 — золотая середина), потом generic
for %%m in (3.14 3.13 3.12 3.11) do (
    if not defined PY (
        py -%%m -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1
        if not errorlevel 1 set PY=py -%%m
    )
)
if not defined PY (
    py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1
    if not errorlevel 1 set PY=py -3
)
if not defined PY (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" >nul 2>&1
    if not errorlevel 1 set PY=python
)
if not defined PY (
    echo  [✗] Python 3.11-3.14 (обычная сборка) не найден.
    echo      Установите обычный Python 3.13 с https://www.python.org/downloads/
    echo      и отметьте галочку "Add python.exe to PATH",
    echo      затем запустите install.bat снова.
    if "!AUTO!"=="1" exit /b 1
    pause
    exit /b 1
)
set REQFILE=requirements.txt
%PY% -c "import sysconfig,sys; sys.exit(0 if str(sysconfig.get_config_var('Py_GIL_DISABLED') or '0')=='1' else 1)" >nul 2>&1
if not errorlevel 1 set REQFILE=requirements-freethreaded.txt
if "!REQFILE!"=="requirements-freethreaded.txt" (
    echo.
    echo  [^!] FREE-THREADED сборка Python ^(3.13t/3.14t^) — беру усечённый набор
    echo      зависимостей. Отличия от полного: без lancedb, sentence-
    echo      transformers, tree-sitter, duckdb, aiokafka, faster-whisper.
    echo      psycopg — чистый, без бинарника: PG-фичи отдадут 503, пока
    echo      libpq не появится в PATH ^(например: поставить PostgreSQL
    echo      client и добавить его bin в PATH^). Векторное хранилище —
    echo      postgres/файлы, embedder — быстрый hashing; граф кода, SQL-
    echo      аналитика MCP, CDC и транскрипция речи — отключены.
    echo.
)
for /f %%v in ('%PY% -c "import sys; print(sys.version.split()[0])" 2^>nul') do set PYVER=%%v
echo  [✓] Python !PYVER! найден

REM ─── 2. Виртуальное окружение + зависимости ──────────────────────────
if not exist ".venv" (
    echo  [·] Создаю виртуальное окружение .venv ...
    %PY% -m venv .venv || goto :fail
)
call .venv\Scripts\activate.bat
if not exist ".venv\.installed" (
    echo  [·] Ставлю зависимости ^(несколько минут^) ...
    python -m pip install --upgrade pip >nul
    python -m pip install -r "!REQFILE!"
    if errorlevel 1 (
        if /i "!REQFILE!"=="requirements.txt" (
            echo  [^!] Полный набор не собрался — пробую усечённый
            echo      requirements-freethreaded.txt ^(Python 3.14+ без колёс^) ...
            python -m pip install -r "requirements-freethreaded.txt"
            if errorlevel 1 goto :fail
            set "REQFILE=requirements-freethreaded.txt"
        ) else (
            goto :fail
        )
    )
    echo ok> .venv\.installed
) else (
    echo  [✓] Зависимости уже установлены
)

REM ─── 3. Вопросы ───────────────────────────────────────────────────────
set PORT=8000
set PG_MODE=skip
set EMBEDDER=auto
if "!AUTO!"=="1" goto :write_env

echo.
echo  ─── Настройка ────────────────────────────────────────────────────
set /p PORT_IN="Порт веб-интерфейса [8000]: "
if not "!PORT_IN!"=="" set PORT=!PORT_IN!

echo.
echo  PostgreSQL — долговременные зеркала, память агентов, аналитика:
echo    [1] Docker-контейнер ^(нужен Docker Desktop^) — рекомендую
echo    [2] Внешний сервер — введу DSN вручную
echo    [3] Пропустить ^(работа только на локальных файлах^)
set /p PG_IN="Выбор [3]: "
if "!PG_IN!"=="1" set PG_MODE=docker
if "!PG_IN!"=="2" set PG_MODE=external
if "!PG_IN!"=="3" set PG_MODE=skip

set DSN=
if "!PG_MODE!"=="external" (
    set /p DSN="DSN ^(postgres://user:pass@host:5432/db^): "
)

echo.
echo  Embedder памяти агентов:
echo    [1] auto — bge-m3, если получится загрузить, иначе быстрый hashing
echo    [2] hash — детерминированный, мгновенный, без загрузки моделей
set /p EM_IN="Выбор [1]: "
if "!EM_IN!"=="2" set EMBEDDER=hash

:write_env
if exist ".env" (
    echo.
    echo  [✓] .env уже существует — не перезаписываю.
    goto :pg_apply
)
echo.
echo  [·] Пишу .env ...
(
    echo # LLM Agent — сгенерировано install.bat
    echo HOST=127.0.0.1
    echo PORT=!PORT!
    echo PROJECT_ROOT=!CD!
    echo.
    echo # PostgreSQL ^(зеркала журнала/сессий/планов, память агентов, аналитика^)
    if "!PG_MODE!"=="external" (
        echo DATABASE_URL=!DSN!
    ) else (
        echo PG_APP_HOST=localhost
        echo PG_APP_PORT=5432
        echo PG_APP_USER=llmagent
        echo PG_APP_PASSWORD=secret
        echo PG_APP_DATABASE=llmagent
    )
    echo.
    echo # Память агентов: auto ^| hash ^| model
    echo AGENT_MEMORY_EMBEDDER=!EMBEDDER!
    echo.
    echo # Эксплуатация: авто-ретенция выключена по умолчанию
    echo PG_RETENTION_DAYS=0
    echo BACKUP_KEEP_LAST=7
) > .env
echo  [✓] .env записан

REM ─── 4. Первичная настройка (каталоги, базы, маркер) ───────────────
echo.
echo  [·] Первичная настройка ^(каталоги, базы, маркер^) ...
python first_run.py --defaults >nul 2>&1 || echo  [^!] first_run завершился с предупреждениями — не критично

REM ─── 5. Схема PostgreSQL ──────────────────────────────────────────────
:pg_apply
if "!PG_MODE!"=="skip" goto :smoke
if "!PG_MODE!"=="docker" (
    echo.
    echo  [·] Поднимаю PostgreSQL в Docker ...
    docker compose up -d postgres || (
        echo  [^!] Docker недоступен — поднимите контейнер вручную:
        echo      docker compose up -d postgres
    )
)
echo.
echo  [·] Накатываю схему БД (scripts/init_db.py) ...
python scripts/init_db.py --check >nul 2>&1
python scripts/init_db.py || echo  [^!] Схему накатить не удалось — сервер всё равно стартует (SQLite-режим)

REM ─── 6. Смоук-тест ────────────────────────────────────────────────────
:smoke
echo.
echo  [·] Смоук-тест: стартую сервер на порту !PORT! ...
set SRV_PID=
for /f %%i in ('powershell -NoProfile -Command "(Start-Process -FilePath '!CD!\.venv\Scripts\python.exe' -ArgumentList 'run.py','--skip-checks','--skip-setup','--port','!PORT!' -WorkingDirectory '!CD!' -PassThru -WindowStyle Hidden).Id" 2^>nul') do set SRV_PID=%%i
if "!SRV_PID!"=="" (
    echo  [^!] Не удалось запустить фоновый процесс — запустите вручную: run.bat
    goto :done
)
set HEALTH=
REM Первый старт долгий: 38+ MCP-серверов — ждём до ~2 минут.
REM Проверка через python из venv — curl в старых Windows отсутствует.
for /l %%i in (1,1,12) do (
    if not defined HEALTH (
        timeout /t 10 /nobreak >nul
        for /f %%c in ('.venv\Scripts\python.exe -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:!PORT!/api/db/status', timeout=5).status)" 2^>nul') do if "%%c"=="200" set HEALTH=200
    )
)
taskkill /PID !SRV_PID! /F /T >nul 2>&1
if defined HEALTH (
    echo  [✓] Сервер отвечает: HTTP 200
) else (
    echo  [^!] Сервер не ответил за 2 минуты — это нормально для первого старта.
    echo      Запустите run.bat: продолжится запуск и пройдёт мастер настройки AI.
)

:done
echo.
echo  ─── Готово ───────────────────────────────────────────────────────
echo   Запуск:              run.bat  ^(или: .venv\Scripts\python run.py^)
echo   Веб-интерфейс:       http://127.0.0.1:!PORT!
echo   Настройки AI:        python first_run.py --reconfigure-ai
echo   Проверка БД:         python scripts/init_db.py --check
echo   Переменные окружения: deploy\env.example
echo.
if "!AUTO!"=="0" pause
exit /b 0

:fail
echo.
echo  [✗] Ошибка установки. Проверьте интернет и версию Python, затем
echo      запустите install.bat снова.
if "!AUTO!"=="0" pause
exit /b 1
