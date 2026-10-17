"""Валидация языка запросов 1С."""
from __future__ import annotations

import logging
import re

from src.onec_query.parser import ParsedQuery, QueryParser

logger = logging.getLogger(__name__)


class ValidationError:
    def __init__(self, level: str, message: str):
        self.level = level      # error | warning
        self.message = message

    def __repr__(self):
        return f"[{self.level}] {self.message}"


class QueryValidator:
    KNOWN_KINDS = {
        "Справочник", "Документ", "РегистрСведений", "РегистрНакопления",
        "РегистрБухгалтерии", "РегистрРасчета", "ПланВидовХарактеристик",
        "ПланСчетов", "ПланВидовРасчета", "БизнесПроцесс", "Задача",
        "Константа", "Перечисление",
        # English
        "Catalog", "Document", "InformationRegister", "AccumulationRegister",
    }

    def __init__(self):
        self.parser = QueryParser()

    def validate(self, source: str) -> list[ValidationError]:
        errors: list[ValidationError] = []

        q = self.parser.parse(source)

        for e in q.errors:
            errors.append(ValidationError("error", e))

        # Баланс скобок
        if source.count("(") != source.count(")"):
            errors.append(ValidationError(
                "error",
                f"Несбалансированные скобки: "
                f"{source.count('(')} открыто, {source.count(')')} закрыто",
            ))

        # Баланс кавычек
        # Простая проверка: чётное число " вне // строк
        text_no_comment = re.sub(r"//[^\n]*", "", source)
        quote_count = text_no_comment.count('"')
        if quote_count % 2 != 0:
            errors.append(ValidationError(
                "error", f"Непарные кавычки: {quote_count}",
            ))

        # Должен быть SELECT
        if not re.search(r"\b(ВЫБРАТЬ|SELECT)\b", source, re.IGNORECASE):
            errors.append(ValidationError(
                "error", "Запрос должен содержать ВЫБРАТЬ/SELECT",
            ))

        # Должен быть FROM
        if not re.search(r"\b(ИЗ|FROM)\b", source, re.IGNORECASE):
            errors.append(ValidationError(
                "error", "Запрос должен содержать ИЗ/FROM",
            ))

        # Метаданные
        for ref in q.metadata_refs:
            kind = ref.split(".")[0]
            if kind and kind not in self.KNOWN_KINDS:
                errors.append(ValidationError(
                    "warning",
                    f"Неизвестный вид метаданных: {kind}",
                ))

        # Поля без алиасов в агрегате + группировка
        has_group = bool(q.group_by)
        has_aggregate = any(
            re.search(r"\b(КОЛИЧЕСТВО|СУММА|МИНИМУМ|МАКСИМУМ|СРЕДНЕЕ|"
                      r"COUNT|SUM|MIN|MAX|AVG)\s*\(",
                      f.expr, re.IGNORECASE)
            for f in q.fields
        )
        if has_aggregate and not has_group:
            errors.append(ValidationError(
                "warning",
                "Агрегатные функции без СГРУППИРОВАТЬ ПО",
            ))
        if has_group and not has_aggregate:
            errors.append(ValidationError(
                "warning",
                "СГРУППИРОВАТЬ ПО без агрегатных функций",
            ))

        # Виртуальные таблицы — параметры
        for vt in q.virtual_tables:
            if "Обороты" in vt or "Turnovers" in vt:
                if "&" not in source and "Период" not in source.lower():
                    errors.append(ValidationError(
                        "warning",
                        f"Виртуальная таблица {vt} без параметра периода",
                    ))

        return errors


def is_valid(source: str) -> bool:
    v = QueryValidator()
    errors = v.validate(source)
    return not any(e.level == "error" for e in errors)
