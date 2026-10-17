#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_no_new_noqa.py — локальный pre-commit хук.

Запрещает добавлять НОВЫЕ строки с `# noqa` в src/ (кроме baseline).
Существующие # noqa занесены в .pre-commit/noqa-baseline.txt.

Создать baseline (первый раз):
    python scripts/gen_noqa_baseline.py > .pre-commit/noqa-baseline.txt
"""
from __future__ import annotations
import sys
from pathlib import Path

BASELINE_FILE = Path(".pre-commit/noqa-baseline.txt")
REPO_ROOT = Path(".")


def load_baseline() -> set[str]:
    if not BASELINE_FILE.exists():
        return set()
    return {line.strip() for line in BASELINE_FILE.read_text(encoding="utf-8").splitlines() if line.strip()}


def current_noqa_lines() -> set[str]:
    """Возвращает множество 'file:line' для всех строк с # noqa в src/."""
    result = set()
    for path in REPO_ROOT.rglob("*.py"):
        # Пропускаем .git, .venv, __pycache__, attic
        parts = path.parts
        if any(p in {".git", ".venv", "__pycache__", "attic"} for p in parts):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for ln, line in enumerate(content.splitlines(), 1):
            if "# noqa" in line.lower():
                result.add(f"{path}:{ln}")
    return result


def main() -> int:
    if not BASELINE_FILE.exists():
        print("⚠ .pre-commit/noqa-baseline.txt не найден.")
        print("  Создайте его: python scripts/gen_noqa_baseline.py > .pre-commit/noqa-baseline.txt")
        return 0  # не блокируем — первый запуск

    baseline = load_baseline()
    current = current_noqa_lines()
    new_noqa = current - baseline

    if new_noqa:
        print(f"❌ Найдены {len(new_noqa)} новых '# noqa' комментариев:")
        for entry in sorted(new_noqa):
            print(f"  {entry}")
        print()
        print("Если это осознанное подавление — добавьте в .pre-commit/noqa-baseline.txt:")
        for entry in sorted(new_noqa):
            print(f"  {entry}")
        print()
        print("В противном случае — исправьте underlying issue (lint warning).")
        return 1

    print(f"✓ Новых '# noqa' нет (baseline: {len(baseline)} записей)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
