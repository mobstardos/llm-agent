@echo off
REM ═══════════════════════════════════════════════════════════════════════
REM LLM Agent — Windows лаунчер
REM
REM Первый запуск создаёт .venv и ставит зависимости автоматически.
REM Ошибка больше НЕ закрывает окно молча: внизу будет пауза и причина.
REM
REM Починено (Task 26): chcp 65001 (кириллица не ломает разбор), детекция
REM Python через py-лаунчер с отсевом Microsoft Store-заглушки, прямой
REM вызов .venv\Scripts\python.exe вместо activate.bat, пауза при ошибке.
REM ═══════════════════════════════════════════════════════════════════════
setlocal EnableExtensions
chcp 65001 >nul 2>&1
title LLM Agent
cd /d "%~dp0"
set "PYTHONUTF8=1"

REM ── 1. Готовый venv? ─────────────────────────────────────────────────
if exist ".venv\Scripts\python.exe" goto :run

echo [*] Виртуальное окружение .venv не найдено — первый запуск.

REM ── 2. Ищем Python: py-лаунчер по версиям, потом python из PATH ──────
REM (заглушка Microsoft Store отвечает ненулевым кодом на "-c import sys"
REM  с реальной проверкой версии — отсекается этой же проверкой)
set "PY="
for %%m in (3.14 3.13 3.12 3.11 3.10) do (
    if not defined PY (
        py -%%m -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
        if not errorlevel 1 set "PY=py -%%m"
    )
)
if not defined PY (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)" >nul 2>&1
    if not errorlevel 1 set "PY=python"
)
if not defined PY (
    echo [!] Python 3.10+ не найден.
    echo     Установите обычный Python с https://www.python.org/downloads/
    echo     и отметьте галочку "Add python.exe to PATH", затем повторите.
    goto :fail
)

REM ── 3. Создаём venv и ставим зависимости ─────────────────────────────
if not exist ".venv\Scripts\python.exe" (
    echo [*] Создаю виртуальное окружение .venv ...
    %PY% -m venv .venv
    if errorlevel 1 goto :fail
)
if not exist ".venv\Scripts\python.exe" (
    echo [!] .venv создался, но python.exe внутри не найден — окружение битое.
    echo     Удалите папку .venv и запустите run.bat снова.
    goto :fail
)

if exist ".venv\.installed" goto :run
echo [*] Ставлю зависимости (несколько минут, нужен интернет) ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [!] pip не смог поставить зависимости. Подробности выше.
    echo     Python 3.14+ без нужных колёс? Попробуйте install.py — он
    echo     умеет автоматически переключаться на усечённый набор.
    goto :fail
)
echo ok> ".venv\.installed"

REM ── 4. Запуск ────────────────────────────────────────────────────────
:run
echo [*] Запуск сервера. Веб-интерфейс откроется на http://127.0.0.1:8000
echo     (порт можно поменять в .env: PORT=...). Останов: Ctrl+C.
echo.
".venv\Scripts\python.exe" run.py %*
set "RC=%errorlevel%"
if "%RC%"=="0" exit /b 0

echo.
echo [!] Сервер завершился с кодом %RC%.
echo     Частые причины:
echo       - порт 8000 занят (смените PORT в .env или закройте прошлый запуск);
echo       - сломанный .env (удалите его и запустите install.py);
echo       - зависимости не доустановились (удалите .venv\.installed).
echo.
pause
exit /b %RC%

:fail
echo.
echo [✗] Запуск не удался. Это окно не закроется, пока вы не нажмёте клавишу —
echo     сфотографируйте ошибку выше и посмотрите docs\FAQ.md
pause
exit /b 1
