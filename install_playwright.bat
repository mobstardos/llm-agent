@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>&1
title LLM Agent — установка Playwright
REM ═══════════════════════════════════════════════════════════════════════
REM LLM Agent — установка Playwright + Chromium для Browser MCP
REM Запускать из корня проекта на Windows: install_playwright.bat
REM ═══════════════════════════════════════════════════════════════════════
setlocal
cd /d "%~dp0"

REM Python из .venv, если он есть — иначе системный python
set "PY=python"
if exist ".venv\Scripts\python.exe" set "PY=.venv\Scripts\python.exe"

%PY% -m pip install playwright || goto :error
%PY% -m playwright install chromium || goto :error

echo.
echo [OK] Playwright и Chromium установлены. Browser MCP готов.
pause
exit /b 0

:error
echo [✗] Ошибка установки Playwright. Проверь, что Python в PATH.
pause
exit /b 1
