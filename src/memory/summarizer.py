"""Генерация summary через LLM."""
from __future__ import annotations

import logging
from typing import Any

from src.memory.base import Message

logger = logging.getLogger(__name__)

SESSION_SUMMARY_PROMPT = """Сожми следующий диалог в краткое резюме (не более 300 слов).
Сохрани:
- Что пользователь хотел сделать
- Какие решения приняты и почему
- Какие файлы/БД/объекты затронуты
- Что осталось нерешённым
- Ошибки и как их чинили

Не сохраняй: полные тексты файлов, дубликаты, технический шум.

Диалог:
{dialogue}

Резюме:"""

DAILY_SUMMARY_PROMPT = """Сожми список сессий за день в единое резюме (не более 400 слов).
Выдели:
- Основные направления работы
- Достигнутые результаты
- Проблемы и их решения
- Незавершённые задачи

Сессии:
{sessions}

Резюме:"""


class Summarizer:
    def __init__(self, llm_client: Any):
        self.llm = llm_client

    async def summarize_session(
        self, messages: list[Message], model: str | None = None,
    ) -> str:
        if not messages or self.llm is None:
            return ""
        dialogue = self._render(messages)
        prompt = SESSION_SUMMARY_PROMPT.format(dialogue=dialogue)
        try:
            resp = await self.llm.chat(
                [{"role": "user", "content": prompt}], model=model,
            )
            return (resp.get("content") or "").strip()
        except Exception as e:
            logger.exception("Ошибка summary: %s", e)
            return ""

    async def summarize_day(
        self, sessions: list[dict], model: str | None = None,
    ) -> str:
        if not sessions or self.llm is None:
            return ""
        parts = []
        for s in sessions:
            title = s.get("title") or s.get("id", "?")
            summary = s.get("summary") or "(без сводки)"
            parts.append(f"## {title}\n{summary}")
        prompt = DAILY_SUMMARY_PROMPT.format(sessions="\n\n".join(parts))
        try:
            resp = await self.llm.chat(
                [{"role": "user", "content": prompt}], model=model,
            )
            return (resp.get("content") or "").strip()
        except Exception as e:
            logger.exception("Ошибка daily summary: %s", e)
            return ""

    @staticmethod
    def _render(messages: list[Message]) -> str:
        lines = []
        for m in messages:
            content = (m.content or "").strip()
            if not content:
                if m.tool_calls:
                    tools = ", ".join(
                        tc["function"]["name"] for tc in m.tool_calls
                    )
                    content = f"(вызовы: {tools})"
                else:
                    continue
            if len(content) > 2000:
                content = content[:2000] + "...[обрезано]"
            lines.append(f"[{m.role}]: {content}")
        return "\n".join(lines)
