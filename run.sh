#!/usr/bin/env bash
# ═══════════════════════════════════════════════════════════════════════
# LLM Agent — Linux лаунчер
# Первый запуск создаёт .venv и ставит зависимости автоматически.
# ═══════════════════════════════════════════════════════════════════════
set -e
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
VENV=".venv"

if [ ! -d "$VENV" ]; then
    echo "┌─ Первая установка: создаю виртуальное окружение..."
    "$PYTHON" -m venv "$VENV"
    source "$VENV/bin/activate"
    pip install --upgrade pip
    pip install -r requirements.txt
else
    source "$VENV/bin/activate"
fi

exec python run.py "$@"
