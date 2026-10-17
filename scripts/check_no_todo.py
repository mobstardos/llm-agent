#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""check_no_todo.py — локальный pre-commit хук.

Запрещает новые TODO/FIXME/HACK/XXX в продакшен-коде (src/).
Исключения:
  - src/mcp_servers/code_analysis/ — MCP сам ищет TODO (там это литералы)
  - src/mcp_servers/documentation/ — автогенерация TODO-описаний
  - src/onec_query/parser.py — парсер обрабатывает 'Справочник.XXX' (метаданные 1С)
"""
from __future__ import annotations
import re
import sys
from pathlib import Path

# Каталоги, где TODO/FIXME — литералы или метаданные, а не настоящий долг
ALLOWED_PATHS = {
    Path("src/mcp_servers/code_analysis"),
    Path("src/mcp_servers/documentation"),
    Path("src/onec_query"),
}

# Паттерн TODO/FIXME/HACK/XXX — с границами слов
TODO_PATTERN = re.compile(r"\b(TODO|FIXME|HACK|XXX)\b")

# XXX в onec_query — это 'Справочник.XXX' (метаданные), не TODO
METADATA_XXX_PATTERN = re.compile(r"[А-Яа-я]\.\w*XXX\w*")


def should_skip(path: Path) -> bool:
    for allowed in ALLOWED_PATHS:
        try:
            path.relative_to(allowed)
            return True
        except ValueError:
            continue
    return False


def main() -> int:
    if len(sys.argv) < 2:
        # Нет файлов для проверки — pre-commit передаёт staged-файлы
        return 0

    files = sys.argv[1:]
    issues = []

    for file_path_str in files:
        file_path = Path(file_path_str)

        # Проверяем только .py в src/
        if not file_path_str.startswith("src/") or not file_path_str.endswith(".py"):
            continue

        if should_skip(file_path):
            continue

        try:
            content = file_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue

        for ln, line in enumerate(content.splitlines(), 1):
            # Пропускаем метаданные 1С (Справочник.XXX, Документ.YYY)
            if METADATA_XXX_PATTERN.search(line):
                continue
            if TODO_PATTERN.search(line):
                issues.append((file_path_str, ln, line.strip()[:120]))

    if issues:
        print(f"❌ Найдено {len(issues)} TODO/FIXME/HACK в продакшен-коде:")
        for path, ln, line in issues:
            print(f"  {path}:{ln}: {line}")
        print()
        print("Стратегия: TODO — это техдолг. Если задача нужна сейчас —")
        print("откройте GitHub Issue и сошлитесь на него в комментарии:")
        print('  # See #123 — refactor after main.py decomposition')
        print()
        print("Исключения (см. scripts/check_no_todo.py):")
        print("  src/mcp_servers/code_analysis/  (MCP ищет TODO)")
        print("  src/mcp_servers/documentation/  (автодокументация)")
        print("  src/onec_query/                 (метаданные 1С)")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
