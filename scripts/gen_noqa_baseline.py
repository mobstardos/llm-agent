#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_noqa_baseline.py — генерирует baseline для check_no_new_noqa.py.

Обходит src/ и выводит в stdout все строки вида 'file:line' с # noqa.
Результат нужно перенаправить в .pre-commit/noqa-baseline.txt:

    python scripts/gen_noqa_baseline.py > .pre-commit/noqa-baseline.txt
"""
from __future__ import annotations
from pathlib import Path

REPO_ROOT = Path(".")
EXCLUDE_DIRS = {".git", ".venv", "__pycache__", "attic", "build"}


def main() -> int:
    for path in REPO_ROOT.rglob("*.py"):
        parts = path.parts
        if any(p in EXCLUDE_DIRS for p in parts):
            continue
        # Только src/, scripts/, tests/ (исключаем install.py/setup.py/first_run.py — там есть noqa, но мы их не трогаем)
        if not (path.parts[0] in {"src", "scripts", "tests"} or
                path.name in {"install.py", "setup.py", "first_run.py", "run.py"}):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for ln, line in enumerate(content.splitlines(), 1):
            if "# noqa" in line.lower():
                print(f"{path}:{ln}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
