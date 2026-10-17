@echo off
REM LLM Agent - Windows launcher
REM
REM Error no longer silently closes the window: a pause and the reason will appear at the bottom.
REM Fixed (Task 26): chcp 65001, detection of Python via py-launcher with filtering
REM Microsoft Store placeholder, direct call to .venv\Scripts\python.exe.
REM ═══════════════════════════════════════════════════════════════════════
setlocal EnableExtensions
chcp 65001 >nul 2>&1
title LLM Agent
cd /d "%~dp0"
set "PYTHONUTF8=1"

REM ── 1. Готовый venv? ─────────────────────────────────────────────────
if exist ".venv\Scripts\python.exe" goto :run

echo [*] Virtual environment .venv not found - first launch.

REM ── 2. Ищем Python: py-лаунчер по версиям, потом python из PATH ──────
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
    echo [!] Python 3.10+ not found.
    echo     Install Python from https://www.python.org/downloads/
    echo     and check "Add python.exe to PATH", then retry.
    goto :fail
)

REM ── 3. Создаём venv и ставим зависимости ─────────────────────────────
if not exist ".venv\Scripts\python.exe" (
    echo [*] Creating virtual environment .venv ...
    %PY% -m venv .venv
    if errorlevel 1 goto :fail
)
if not exist ".venv\Scripts\python.exe" (
    echo [!] .venv created but python.exe inside not found - environment broken.
    echo     Delete .venv folder and run run.bat again.
    goto :fail
)

if exist ".venv\.installed" goto :run
echo [*] Installing dependencies (several minutes, internet required) ...
.".venv\Scripts\python.exe" -m pip install --upgrade pip
.".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
    echo [!] pip could not install dependencies. Details above.
    echo     Try installing dependencies manually or use --skip-checks.
    goto :fail
)
echo ok> ".venv\.installed"

REM ── 4. Запуск с SKIP_MCP=1 ─────────────────────────────────────────
:run
echo [*] Starting server. Web interface at http://127.0.0.1:8000
echo     (port can be changed in .env: PORT=...). Stop: Ctrl+C.
echo.
REM Set SKIP_MCP=1 by default to avoid MCP initialization errors
set "SKIP_MCP=1"
".venv\Scripts\python.exe" run.py %*
set "RC=%errorlevel%"
if "%RC%"=="0" exit /b 0

echo.
echo [%] Server exited with code %RC%.
echo     Common causes:
echo       - Port 8000 occupied (change PORT in .env or close previous launch);
echo       - Broken .env (delete it and run run.bat again);
echo       - Dependencies not installed (delete .venv\.installed).
echo.
pause
exit /b %RC%

:fail
echo.
echo [%] Launch failed. This window will not close until you press a key -
echo     take a screenshot of the error above and check docs\FAQ.md
pause
exit /b 1