#!/usr/bin/env python
"""Route-отчёт «какой агент был нужен на самом деле» (Этап 5, V2 §3.10).

Читает data/routing/decisions.jsonl (заполняется автоматически из чата:
Intent Layer, LLM-роутер, Supervisor), строит агрегат и подсказки
тюнинга routing_hints в agents/*/agent.yaml.

Использование:
    python scripts/route_report.py                 # печать в консоль
    python scripts/route_report.py --days 7
    python scripts/route_report.py --out docs/route-report.md
"""
from __future__ import annotations

import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from src.route_analytics import main   # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
