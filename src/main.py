#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LLM Agent — точка входа.

Sprint 1.D: финальный рефакторинг. Весь код вынесен:
  - src/state.py         — AppState + dependency injection
  - src/app.py           — FastAPI app + lifespan + 16 роутеров
  - src/routes/*.py      — 86 HTTP эндпоинтов (16 модулей)
  - src/ws/chat.py       — WebSocket /ws handler

В этом файле остаётся ТОЛЬКО запуск uvicorn.

Запуск:
  python main.py             # dev mode, reload=True, host=127.0.0.1:8000
  python main.py --prod     # prod mode (gunicorn -k uvicorn.workers.UvicornWorker)
  python main.py --host 0.0.0.0   # наружу — ТОЛЬКО с LLM_AGENT_API_KEY!
  uvicorn src.app:app --reload  # альтернатива
"""
from __future__ import annotations

import argparse
import os
import sys

from src.app import app  # noqa: F401 — импорт app, чтобы FastAPI подхватил роутеры


def main() -> int:
    parser = argparse.ArgumentParser(description="LLM Agent server")
    # v2: по умолчанию слушаем только localhost; наружу — осознанно через
    # --host 0.0.0.0 (и тогда ОБЯЗАТЕЛЬНО задайте LLM_AGENT_API_KEY).
    parser.add_argument(
        "--host", default=os.getenv("LLM_AGENT_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--prod", action="store_true",
                        help="Production mode (no reload, multi-worker)")
    args = parser.parse_args()

    try:
        import uvicorn
    except ImportError:
        print("❌ uvicorn не установлен. Запустите: pip install -r requirements.txt",
              file=sys.stderr)
        return 1

    if args.prod:
        # Production: gunicorn with uvicorn worker class (multi-process)
        try:
            import gunicorn  # noqa: F401
        except ImportError:
            print("⚠ gunicorn не установлен — fallback на uvicorn single-process",
                  file=sys.stderr)
            uvicorn.run(app, host=args.host, port=args.port, workers=1)
            return 0
        # gunicorn запускается командой:
        #   gunicorn -k uvicorn.workers.UvicornWorker -w 4 -b 127.0.0.1:8000 src.app:app
        # Этот код НЕ запускает gunicorn напрямую — он только печатает инструкцию
        print("Production mode — используйте gunicorn:")
        print(f"  gunicorn -k uvicorn.workers.UvicornWorker -w 4 "
              f"-b {args.host}:{args.port} src.app:app")
        return 0

    # Development
    uvicorn.run(app, host=args.host, port=args.port, reload=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
