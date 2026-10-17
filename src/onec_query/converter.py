"""Конвертер SQL → язык запросов 1С (базовая конвертация)."""
from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


SQL_TO_1C_KEYWORDS = {
    "SELECT": "ВЫБРАТЬ",
    "DISTINCT": "РАЗЛИЧНЫЕ",
    "FROM": "ИЗ",
    "WHERE": "ГДЕ",
    "GROUP BY": "СГРУППИРОВАТЬ ПО",
    "ORDER BY": "УПОРЯДОЧИТЬ ПО",
    "HAVING": "ИМЕЮЩИЕ",
    "JOIN": "СОЕДИНЕНИЕ",
    "LEFT JOIN": "ЛЕВОЕ СОЕДИНЕНИЕ",
    "RIGHT JOIN": "ПРАВОЕ СОЕДИНЕНИЕ",
    "INNER JOIN": "ВНУТРЕННЕЕ СОЕДИНЕНИЕ",
    "FULL JOIN": "ПОЛНОЕ СОЕДИНЕНИЕ",
    "ON": "ПО",
    "AS": "КАК",
    "AND": "И",
    "OR": "ИЛИ",
    "NOT": "НЕ",
    "IN": "В",
    "BETWEEN": "МЕЖДУ",
    "LIKE": "ПОДОБНО",
    "IS NULL": "ЕСТЬ NULL",
    "IS NOT NULL": "ЕСТЬ НЕ NULL",
    "UNION ALL": "ОБЪЕДИНИТЬ ВСЕ",
    "UNION": "ОБЪЕДИНИТЬ",
    "LIMIT": "ПЕРВЫЕ",
    "ASC": "ВОЗР",
    "DESC": "УБЫВ",
    "TRUE": "ИСТИНА",
    "FALSE": "ЛОЖЬ",
    "COUNT": "КОЛИЧЕСТВО",
    "SUM": "СУММА",
    "MIN": "МИНИМУМ",
    "MAX": "МАКСИМУМ",
    "AVG": "СРЕДНЕЕ",
    "NULL": "NULL",
}

REVERSE = {v: k for k, v in SQL_TO_1C_KEYWORDS.items()}


class SqlTo1CConverter:
    """Простая конвертация SQL → язык запросов 1С.

    Не покрывает всё. Умеет:
      - Ключевые слова
      - Простые таблицы (schema.table → table)
      - LIMIT N → ПЕРВЫЕ N
      - Подсказки по метаданным (unresolved)
    """

    def convert(self, sql: str) -> tuple[str, list[str]]:
        """Возвращает (результат, предупреждения)."""
        warnings: list[str] = []
        result = sql

        # LIMIT N → ПЕРВЫЕ N (переносим в начало)
        limit_match = re.search(
            r"\bLIMIT\s+(\d+)\s*;?\s*$", result, re.IGNORECASE,
        )
        if limit_match:
            n = limit_match.group(1)
            result = result[:limit_match.start()].strip().rstrip(";")
            result = re.sub(
                r"^(\s*)(SELECT|ВЫБРАТЬ)\s",
                rf"\1\2 ПЕРВЫЕ {n} ",
                result, count=1, flags=re.IGNORECASE,
            )
            warnings.append(f"LIMIT {n} → ПЕРВЫЕ {n}")

        # FROM schema.table → FROM table
        if re.search(r"\bFROM\s+[a-zA-Z_][\w]*\.[a-zA-Z_]", result, re.IGNORECASE):
            result = re.sub(
                r"\bFROM\s+([a-zA-Z_][\w]*)\.([a-zA-Z_][\w]*)",
                r"FROM \2",
                result, flags=re.IGNORECASE,
            )
            warnings.append(
                "Схема удалена. Проверьте, что таблица существует как "
                "объект метаданных 1С (Справочник.X, Документ.Y)"
            )

        # Ключевые слова
        for en, ru in sorted(
            SQL_TO_1C_KEYWORDS.items(),
            key=lambda x: -len(x[0]),
        ):
            # Заменяем целые слова, сохраняя регистр
            pattern = re.compile(rf"\b{re.escape(en)}\b", re.IGNORECASE)
            result = pattern.sub(ru, result)

        warnings.append(
            "SQL → 1С: конвертация базовая. Проверьте вручную "
            "ссылки на метаданные, функции и соединения."
        )
        return result, warnings


class OneCToSqlConverter:
    """Обратная конвертация: язык запросов 1С → SQL (базовая)."""

    def convert(self, query: str) -> tuple[str, list[str]]:
        warnings: list[str] = []
        result = query

        # ПЕРВЫЕ N → LIMIT N
        top_match = re.search(
            r"\b(ВЫБРАТЬ|SELECT)\s+ПЕРВЫЕ\s+(\d+)\s",
            result, re.IGNORECASE,
        )
        if top_match:
            n = top_match.group(2)
            result = (
                result[:top_match.start()] +
                top_match.group(1) + " " +
                result[top_match.end():]
            ).strip()
            result = result + f" LIMIT {n}"
            warnings.append(f"ПЕРВЫЕ {n} → LIMIT {n}")

        # Ключевые слова
        for ru, en in sorted(
            REVERSE.items(),
            key=lambda x: -len(x[0]),
        ):
            pattern = re.compile(rf"\b{re.escape(ru)}\b", re.IGNORECASE)
            result = pattern.sub(en, result)

        warnings.append(
            "1С → SQL: конвертация базовая. Ссылки на объекты метаданных "
            "нужно заменить на конкретные таблицы."
        )
        return result, warnings
