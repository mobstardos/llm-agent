#!/usr/bin/env python3
"""LLM Agent — корневая точка входа.

    python main.py          — то же, что python run.py (проверки + сервер)
    python main.py --port 8000

Раньше здесь лежал случайный обрывок src/main.py (24 строки, падал с
NameError: app) — теперь это полноценный шим на run.py, чтобы запуск
«python main.py» из корня всегда работал.
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from run import main  # noqa: E402 — путь уже прописан выше

if __name__ == "__main__":
    sys.exit(main())
