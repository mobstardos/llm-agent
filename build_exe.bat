@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>&1
title LLM Agent — сборка exe
REM ═══════════════════════════════════════════════════════════════════════
REM LLM Agent — сборка llm-agent.exe (запускать НА Windows 10/11 x64)
REM
REM Требования: Python 3.11+ в PATH, проект рядом.
REM Результат:  dist\llm-agent.exe  (одиночный файл-лаунчер)
REM
REM Использование exe:
REM   llm-agent.exe              — запуск системы (папки config/agents/web
REM                                должны лежать рядом с exe)
REM ═══════════════════════════════════════════════════════════════════════
setlocal EnableExtensions
cd /d "%~dp0"

REM Python из .venv, если он есть — иначе системный python
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

echo [1/3] Устанавливаю зависимости (Python: %PY%)...
%PY% -m pip install --upgrade pip
%PY% -m pip install -r requirements.txt "pyinstaller>=6.10.0" || goto :error

echo [2/3] Сборка (5-15 минут)...
%PY% -m PyInstaller llm_agent.spec --noconfirm --clean || goto :error

echo [3/3] Готово.
echo    Лаунчер:  dist\llm-agent.exe
echo    Папки config\, agents\, mcp_servers\, capabilities\, loops\, src\web\,
echo    .env.example и README.md уже скопированы в dist\ PyInstaller-ом.
echo.
echo    Запуск:  dist\llm-agent.exe --port 8000
pause
exit /b 0

:error
echo [✗] Сборка не удалась. Подробности выше.
pause
exit /b 1
