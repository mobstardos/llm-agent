"""Лексер языка запросов 1С."""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import Enum

logger = logging.getLogger(__name__)


class TokenType(str, Enum):
    KEYWORD = "keyword"
    IDENTIFIER = "identifier"
    STRING = "string"
    NUMBER = "number"
    PARAM = "param"        # &Параметр
    OPERATOR = "operator"
    PUNCT = "punct"
    COMMENT = "comment"
    UNKNOWN = "unknown"


@dataclass
class Token:
    type: TokenType
    value: str
    pos: int
    line: int = 1
    col: int = 1


KEYWORDS_RU = {
    "ВЫБРАТЬ", "ВЫБРАТЬ РАЗРЕШЕННЫЕ", "ВЫБРАТЬ РАЗЛИЧНЫЕ",
    "ИЗ", "ГДЕ", "ИМЕЮЩИЕ", "СГРУППИРОВАТЬ", "ПО", "УПОРЯДОЧИТЬ",
    "ВОЗР", "УБЫВ", "ИМЕЮЩИЕ", "СОЕДИНЕНИЕ", "ЛЕВОЕ", "ПРАВОЕ",
    "ВНУТРЕННЕЕ", "ПОЛНОЕ", "КАК", "ЕСТЬ", "НЕ", "И", "ИЛИ",
    "В", "МЕЖДУ", "ПОДОБНО", "ССЫЛКА", "ИСТИНА", "ЛОЖЬ",
    "ПЕРВЫЕ", "ОБЪЕДИНИТЬ", "ВСЕ", "ВЫРАЗИТЬ", "ТИП",
    "ВОЗРАСТАЮЩ", "УБЫВАЮЩ", "ДЛЯ", "ИЗМЕНЕНИЯ", "ИТОГИ",
    "АВТОУПОРЯДОЧИВАНИЕ", "ИЕРАРХИЯ", "ПЕРИОДАМИ", "ТОЛЬКО",
}

KEYWORDS_EN = {
    "SELECT", "DISTINCT", "FROM", "WHERE", "HAVING", "GROUP", "BY",
    "ORDER", "ASC", "DESC", "JOIN", "LEFT", "RIGHT", "INNER", "OUTER",
    "FULL", "AS", "IS", "NOT", "AND", "OR", "IN", "BETWEEN", "LIKE",
    "REF", "TRUE", "FALSE", "TOP", "UNION", "ALL", "CAST", "TYPE",
    "TOTALS", "AUTOORDER", "HIERARCHY", "ONLY",
}

ALL_KEYWORDS = KEYWORDS_RU | KEYWORDS_EN

TOKEN_PATTERNS = [
    (TokenType.COMMENT, re.compile(r"//[^\n]*")),
    (TokenType.STRING, re.compile(r'"(?:[^"\\]|\\.)*"')),
    (TokenType.PARAM, re.compile(r"&[A-Za-zА-Яа-я_][A-Za-zА-Яа-я0-9_]*")),
    (TokenType.NUMBER, re.compile(r"\d+(?:\.\d+)?")),
    (TokenType.IDENTIFIER, re.compile(r"[A-Za-zА-Яа-я_][A-Za-zА-Яа-я0-9_]*")),
    (TokenType.OPERATOR, re.compile(r"<=|>=|<>|!=|=|<|>|\+|-|\*|/")),
    (TokenType.PUNCT, re.compile(r"[,.()\[\];]")),
]


class Lexer:
    def tokenize(self, source: str) -> list[Token]:
        tokens: list[Token] = []
        i = 0
        line = 1
        line_start = 0
        n = len(source)

        while i < n:
            ch = source[i]

            if ch == "\n":
                line += 1
                i += 1
                line_start = i
                continue
            if ch.isspace():
                i += 1
                continue

            matched = False
            for ttype, pattern in TOKEN_PATTERNS:
                m = pattern.match(source, i)
                if m:
                    value = m.group(0)
                    if ttype == TokenType.IDENTIFIER:
                        if value.upper() in ALL_KEYWORDS:
                            ttype = TokenType.KEYWORD
                    tokens.append(Token(
                        type=ttype, value=value, pos=i,
                        line=line, col=i - line_start + 1,
                    ))
                    i = m.end()
                    matched = True
                    break

            if not matched:
                tokens.append(Token(
                    type=TokenType.UNKNOWN, value=ch, pos=i,
                    line=line, col=i - line_start + 1,
                ))
                i += 1

        return tokens
