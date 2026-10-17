#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
# LLM Agent — сборка Linux-бинарника llm-agent (PyInstaller)
# Результат: dist/llm-agent  (одиночный файл-лаунчер)
# ═══════════════════════════════════════════════════════════════════════
set -e
cd "$(dirname "$0")"

echo "[1/3] Зависимости..."
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt "pyinstaller>=6.10.0"

echo "[2/3] Сборка..."
python3 -m PyInstaller llm_agent.spec --noconfirm --clean

echo "[3/3] Готово: dist/llm-agent"
echo "       Запуск: dist/llm-agent --port 8000"
