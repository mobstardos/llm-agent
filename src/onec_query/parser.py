"""Парсер языка запросов 1С.

Не полный AST — извлекает: SELECT-поля, источники (таблицы), WHERE, JOIN,
параметры, реквизиты.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from src.onec_query.lexer import Lexer, Token, TokenType

logger = logging.getLogger(__name__)


@dataclass
class QueryField:
    expr: str
    alias: str = ""
    table: str = ""


@dataclass
class QuerySource:
    table: str
    alias: str = ""
    kind: str = ""      # Справочник | Документ | Регистр
    join_type: str = ""  # LEFT | INNER | ...


@dataclass
class ParsedQuery:
    raw: str = ""
    fields: list[QueryField] = field(default_factory=list)
    sources: list[QuerySource] = field(default_factory=list)
    where_clause: str = ""
    group_by: str = ""
    order_by: str = ""
    having: str = ""
    parameters: list[str] = field(default_factory=list)
    virtual_tables: list[str] = field(default_factory=list)
    metadata_refs: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


TEMP_TABLE_RE = re.compile(r"^[А-ЯA-Z][А-ЯA-Z0-9_]*$")
VIRTUAL_TABLE_RE = re.compile(
    r"([A-Za-zА-Яа-я_][A-Za-zА-Яа-я0-9_]*)\.([A-Za-zА-Яа-я_][A-Za-zА-Яа-я0-9_.]*)\s*\(",
)


class QueryParser:
    def parse(self, source: str) -> ParsedQuery:
        q = ParsedQuery(raw=source)

        if not source.strip():
            q.errors.append("Пустой запрос")
            return q

        lexer = Lexer()
        try:
            tokens = lexer.tokenize(source)
        except Exception as e:
            q.errors.append(f"Лексическая ошибка: {e}")
            return q

        try:
            self._parse_structure(source, tokens, q)
        except Exception as e:
            q.errors.append(f"Ошибка парсинга: {e}")

        # Параметры
        q.parameters = sorted({
            t.value for t in tokens if t.type == TokenType.PARAM
        })

        # Виртуальные таблицы
        for m in VIRTUAL_TABLE_RE.finditer(source):
            table = m.group(1)
            virtual = m.group(2)
            q.virtual_tables.append(f"{table}.{virtual}")

        # Метаданные (Справочник.XXX, Документ.YYY)
        for m in re.finditer(
            r"\b(Справочник|Документ|РегистрСведений|РегистрНакопления|"
            r"ПланВидовХарактеристик|БизнесПроцесс|Задача)\.([A-Za-zА-Яа-я_][A-Za-zА-Яа-я0-9_]*)",
            source,
            re.IGNORECASE,
        ):
            q.metadata_refs.append(f"{m.group(1)}.{m.group(2)}")

        return q

    def _parse_structure(
        self, source: str, tokens: list[Token], q: ParsedQuery,
    ) -> None:
        """Извлекает основные секции запроса регулярками по тексту."""
        # Верхнеуровневый SELECT
        text = self._strip_comments(source)

        # SELECT-поля (до FROM)
        select_match = re.search(
            r"\b(?:ВЫБРАТЬ|SELECT)\b(.*?)\b(?:ИЗ|FROM)\b",
            text, re.IGNORECASE | re.DOTALL,
        )
        if select_match:
            fields_text = select_match.group(1)
            q.fields = self._parse_fields(fields_text)

        # FROM ... до WHERE/GROUP/ORDER/etc
        from_match = re.search(
            r"\b(?:ИЗ|FROM)\b(.*?)(?=\b(?:ГДЕ|WHERE|СГРУППИРОВАТЬ|GROUP|"
            r"УПОРЯДОЧИТЬ|ORDER|ИМЕЮЩИЕ|HAVING|ОБЪЕДИНИТЬ|UNION|"
            r"ИТОГИ|TOTALS|$))",
            text, re.IGNORECASE | re.DOTALL,
        )
        if from_match:
            q.sources = self._parse_sources(from_match.group(1))

        # WHERE
        where_match = re.search(
            r"\b(?:ГДЕ|WHERE)\b(.*?)(?=\b(?:СГРУППИРОВАТЬ|GROUP|"
            r"УПОРЯДОЧИТЬ|ORDER|ИМЕЮЩИЕ|HAVING|ИТОГИ|TOTALS|$))",
            text, re.IGNORECASE | re.DOTALL,
        )
        if where_match:
            q.where_clause = where_match.group(1).strip()

        # GROUP BY
        group_match = re.search(
            r"\b(?:СГРУППИРОВАТЬ\s+ПО|GROUP\s+BY)\b(.*?)"
            r"(?=\b(?:УПОРЯДОЧИТЬ|ORDER|ИМЕЮЩИЕ|HAVING|ИТОГИ|TOTALS|$))",
            text, re.IGNORECASE | re.DOTALL,
        )
        if group_match:
            q.group_by = group_match.group(1).strip()

        # ORDER BY
        order_match = re.search(
            r"\b(?:УПОРЯДОЧИТЬ\s+ПО|ORDER\s+BY)\b(.*?)"
            r"(?=\b(?:ИТОГИ|TOTALS|$))",
            text, re.IGNORECASE | re.DOTALL,
        )
        if order_match:
            q.order_by = order_match.group(1).strip()

        # HAVING
        having_match = re.search(
            r"\b(?:ИМЕЮЩИЕ|HAVING)\b(.*?)(?=\b(?:УПОРЯДОЧИТЬ|ORDER|ИТОГИ|TOTALS|$))",
            text, re.IGNORECASE | re.DOTALL,
        )
        if having_match:
            q.having = having_match.group(1).strip()

    def _parse_fields(self, text: str) -> list[QueryField]:
        # Простая разбивка по запятым на верхнем уровне
        parts = self._split_top_level(text, ",")
        fields: list[QueryField] = []
        for p in parts:
            p = p.strip()
            if not p:
                continue
            # AS alias
            as_match = re.match(
                r"(.+?)\s+(?:КАК|AS)\s+([A-Za-zА-Яа-я_][A-Za-zА-Яа-я0-9_]*)$",
                p, re.IGNORECASE,
            )
            if as_match:
                fields.append(QueryField(
                    expr=as_match.group(1).strip(),
                    alias=as_match.group(2).strip(),
                ))
            else:
                fields.append(QueryField(expr=p))
        return fields

    def _parse_sources(self, text: str) -> list[QuerySource]:
        # Разделяем по JOIN-конструкциям
        text = text.strip()
        sources: list[QuerySource] = []

        # Разбиваем по ключевым словам JOIN
        parts = re.split(
            r"\b(ЛЕВОЕ\s+СОЕДИНЕНИЕ|ПРАВОЕ\s+СОЕДИНЕНИЕ|ВНУТРЕННЕЕ\s+СОЕДИНЕНИЕ|"
            r"ПОЛНОЕ\s+СОЕДИНЕНИЕ|СОЕДИНЕНИЕ|"
            r"LEFT\s+JOIN|RIGHT\s+JOIN|INNER\s+JOIN|FULL\s+JOIN|JOIN)\b",
            text, flags=re.IGNORECASE,
        )

        if parts:
            sources.append(self._parse_single_source(parts[0], ""))

        i = 1
        while i < len(parts) - 1:
            join_type = parts[i]
            src_text = parts[i + 1]
            # Убираем ON-условие
            on_match = re.search(
                r"\b(?:ПО|ON)\b", src_text, re.IGNORECASE,
            )
            if on_match:
                src_text = src_text[:on_match.start()]
            sources.append(self._parse_single_source(src_text, join_type))
            i += 2

        return sources

    def _parse_single_source(
        self, text: str, join_type: str,
    ) -> QuerySource:
        text = text.strip().rstrip(",;")
        # Возможные форматы:
        #   Справочник.Номенклатура КАК Н
        #   Справочник.Номенклатура.Наименование КАК Н
        #   РегистрНакопления.Продажи.Обороты(&Дата, , , ) КАК П
        #   ИмяВременнойТаблицы КАК Т

        as_match = re.match(
            r"(.+?)\s+(?:КАК|AS)\s+([A-Za-zА-Яа-я_][A-Za-zА-Яа-я0-9_]*)$",
            text, re.IGNORECASE,
        )
        if as_match:
            table_full = as_match.group(1).strip()
            alias = as_match.group(2).strip()
        else:
            table_full = text
            alias = ""

        # Убираем виртуальную часть
        virtual_match = re.match(
            r"(.+?)\.([A-Za-zА-Яа-я_][A-Za-zА-Яа-я0-9_]*)\s*\(",
            table_full,
        )
        if virtual_match:
            table_full = virtual_match.group(1)

        # Kind.SubObject
        parts = table_full.split(".")
        kind = parts[0] if parts else ""
        table = parts[1] if len(parts) > 1 else table_full

        return QuerySource(
            table=table, alias=alias, kind=kind, join_type=join_type,
        )

    @staticmethod
    def _split_top_level(text: str, sep: str) -> list[str]:
        """Разбивает по sep, игнорируя вложенные скобки и строки."""
        parts: list[str] = []
        depth = 0
        current: list[str] = []
        in_string = False

        i = 0
        while i < len(text):
            ch = text[i]
            if ch == '"' and (i == 0 or text[i - 1] != "\\"):
                in_string = not in_string
            if not in_string:
                if ch == "(":
                    depth += 1
                elif ch == ")":
                    depth -= 1
                elif ch == sep and depth == 0:
                    parts.append("".join(current))
                    current = []
                    i += 1
                    continue
            current.append(ch)
            i += 1

        if current:
            parts.append("".join(current))
        return parts

    @staticmethod
    def _strip_comments(text: str) -> str:
        return re.sub(r"//[^\n]*", "", text)
