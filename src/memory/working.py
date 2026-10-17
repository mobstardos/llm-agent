"""Working memory: компактизация контекста."""
from __future__ import annotations

import logging
from typing import Any

from src.memory.base import Message
from src.memory.config import WorkingSettings
from src.memory.tokens import (
    count_messages_tokens, count_tokens, truncate_to_tokens,
)

logger = logging.getLogger(__name__)


class WorkingMemory:
    def __init__(self, settings: WorkingSettings):
        self.cfg = settings

    def build_context(
        self,
        system_prompt: str,
        project_profile: str,
        user_profile: str,
        recent_messages: list[Message],
        rolling_summary: str,
        recalled_chunks: list[str],
    ) -> dict[str, Any]:
        max_tokens = self.cfg.max_context_tokens
        b = self.cfg.budget

        system_budget = int(max_tokens * b.system)
        profile_budget = int(max_tokens * b.profile)
        recent_budget = int(max_tokens * b.recent)
        summary_budget = int(max_tokens * b.summary)
        recall_budget = int(max_tokens * b.recall)

        system_text = truncate_to_tokens(system_prompt, system_budget)
        profile_full = (project_profile or "") + "\n\n" + (user_profile or "")
        profile_text = truncate_to_tokens(profile_full.strip(), profile_budget)
        summary_text = truncate_to_tokens(rolling_summary or "", summary_budget)

        recall_text = ""
        if recalled_chunks:
            joined = "\n\n---\n\n".join(recalled_chunks)
            recall_text = truncate_to_tokens(joined, recall_budget)

        recent = self._fit_recent(recent_messages, recent_budget)

        combined_system = system_text
        if profile_text:
            combined_system += "\n\n### Профиль проекта\n\n" + profile_text
        if summary_text:
            combined_system += (
                "\n\n### Сводка предыдущего диалога\n\n" + summary_text
            )
        if recall_text:
            combined_system += (
                "\n\n### Релевантный контекст\n\n" + recall_text
            )

        messages: list[dict] = [
            {"role": "system", "content": combined_system},
        ]
        for m in recent:
            d: dict[str, Any] = {"role": m.role, "content": m.content or ""}
            if m.tool_calls:
                d["tool_calls"] = m.tool_calls
            if m.tool_call_id:
                d["tool_call_id"] = m.tool_call_id
            messages.append(d)

        used = count_messages_tokens(messages)
        stats = {
            "max_tokens": max_tokens,
            "used_tokens": used,
            "utilization": round(used / max_tokens, 3) if max_tokens else 0,
            "system_tokens": count_tokens(combined_system),
            "recent_messages": len(recent),
        }
        return {"messages": messages, "stats": stats}

    def _fit_recent(
        self, messages: list[Message], budget: int,
    ) -> list[Message]:
        if not messages:
            return []

        # Группируем assistant+tool
        groups: list[list[Message]] = []
        i = 0
        while i < len(messages):
            m = messages[i]
            if m.role == "assistant" and m.tool_calls:
                grp = [m]
                j = i + 1
                while j < len(messages) and messages[j].role == "tool":
                    grp.append(messages[j])
                    j += 1
                groups.append(grp)
                i = j
            else:
                groups.append([m])
                i += 1

        total = 0
        selected: list[Message] = []
        for grp in reversed(groups):
            grp_tokens = sum(count_tokens(g.content or "") + 20 for g in grp)
            if total + grp_tokens > budget:
                break
            selected = grp + selected
            total += grp_tokens
        return selected

    def needs_compaction(self, messages: list[Message]) -> bool:
        used = sum(count_tokens(m.content or "") for m in messages)
        return used > self.cfg.max_context_tokens * self.cfg.compact_threshold

    def needs_truncation(self, messages: list[Message]) -> bool:
        used = sum(count_tokens(m.content or "") for m in messages)
        return used > self.cfg.max_context_tokens * self.cfg.truncate_threshold
