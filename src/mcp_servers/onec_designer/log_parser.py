"""Парсер логов 1С.

Форматы:
  - "12:34:56 Ошибка: ..." (русская локаль)
  - "12:34:56 ERROR: ..." (английская)
  - Сложные сообщения из CheckModules
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class LogMessage:
    level: str          # info | warning | error
    text: str
    line_num: int = 0
    file: str = ""
    raw: str = ""


LEVEL_PATTERNS = [
    (re.compile(r"^(?:\d{2}:\d{2}:\d{2}\s+)?(?:Ошибка|ERROR)[:\s]", re.I), "error"),
    (re.compile(r"^(?:\d{2}:\d{2}:\d{2}\s+)?(?:Предупреждение|WARNING)[:\s]", re.I), "warning"),
    (re.compile(r"^(?:\d{2}:\d{2}:\d{2}\s+)?(?:Информация|INFO)[:\s]", re.I), "info"),
]

# Паттерны ошибок CheckModules
CHECK_ERROR = re.compile(
    r"^\s*(.+?)\((\d+)(?:,\s*(\d+))?\):\s*(.+)$"
)


def parse_1c_log(path: str | Path) -> list[LogMessage]:
    """Читает лог 1С и возвращает структурированные сообщения."""
    p = Path(path)
    if not p.exists():
        return []

    try:
        raw = p.read_bytes()
        text = _decode_log(raw)
    except Exception as e:
        logger.warning("Не прочитать лог %s: %s", p, e)
        return []

    messages: list[LogMessage] = []
    for i, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue

        level = "info"
        for pattern, lvl in LEVEL_PATTERNS:
            if pattern.match(line):
                level = lvl
                break

        # CheckModules
        m = CHECK_ERROR.match(line)
        if m:
            messages.append(LogMessage(
                level="error",
                text=m.group(4).strip(),
                line_num=int(m.group(2)),
                file=m.group(1).strip(),
                raw=line,
            ))
            continue

        messages.append(LogMessage(
            level=level, text=line.strip(), line_num=i, raw=line,
        ))

    return messages


def _decode_log(data: bytes) -> str:
    """Логи 1С — CP1251 или UTF-8-BOM."""
    for enc in ("utf-8-sig", "utf-8", "cp1251", "cp866"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def summarize_messages(messages: list[LogMessage]) -> dict:
    """Сводка по логам."""
    by_level = {"info": 0, "warning": 0, "error": 0}
    for m in messages:
        by_level[m.level] = by_level.get(m.level, 0) + 1
    return {
        "total": len(messages),
        "by_level": by_level,
        "errors": [m.text for m in messages if m.level == "error"][:50],
        "warnings": [m.text for m in messages if m.level == "warning"][:50],
    }
