#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Task 32: run.py заранее диагностирует занятый порт (вместо сырого 10048).

Поднимает слушатель на свободном порте и запускает run.py --port <этот порт>:
ожидаем rc=1, понятное сообщение «Порт … занят» и подсказки — БЕЗ старта
uvicorn. Запуск: python scripts/test_run_port_busy.py (из корня проекта).
"""
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

passed = failed = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global passed, failed
    mark = "[✓]" if cond else "[✗]"
    print(f"  {mark} {name}" + (f" — {extra}" if extra and not cond else ""))
    passed += int(cond)
    failed += int(not cond)


blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
blocker.bind(("127.0.0.1", 0))          # свободный порт выбирает ОС
blocker.listen(1)
port = blocker.getsockname()[1]

try:
    try:
        p = subprocess.run(
            [sys.executable, "run.py", "--port", str(port),
             "--skip-checks", "--skip-setup"],
            cwd=str(ROOT), capture_output=True, text=True, timeout=120,
        )
        out = p.stdout + p.stderr
        rc = p.returncode
    except subprocess.TimeoutExpired as e:
        _so = e.stdout or b""
        _se = e.stderr or b""
        out = _so.decode("utf-8", "replace") if isinstance(_so, bytes) else _so
        out += _se.decode("utf-8", "replace") if isinstance(_se, bytes) else _se
        rc = "timeout"

    check("rc=1 на занятом порте (не сырой 10048)", rc == 1, f"rc={rc!r}")
    check("сообщение «Порт … занят»", "занят" in out)
    check("подсказка findstr/taskkill", "findstr" in out and "taskkill" in out)
    check("альтернатива --port", f"--port {port + 1}" in out)
    check("uvicorn НЕ стартовал", "Application startup complete" not in out)
finally:
    blocker.close()

print(f"\n═══ Итог: {passed}/{passed + failed} ═══")
sys.exit(1 if failed else 0)
