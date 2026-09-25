"""Фасад эпизодической памяти."""
from __future__ import annotations

import logging

from src.memory.base import Event, Message, Session, Summary, now_ts
from src.memory.config import EpisodicSettings
from src.memory.store import EpisodicStore
from src.memory.summarizer import Summarizer

logger = logging.getLogger(__name__)


class EpisodicMemory:
    def __init__(
        self,
        settings: EpisodicSettings,
        db_path: str,
        summarizer: Summarizer | None = None,
    ):
        self.cfg = settings
        self.store = EpisodicStore(db_path)
        self.summarizer = summarizer
        self._current_session_id: str | None = None

    def start_session(self, title: str | None = None) -> Session:
        sess = self.store.create_session(title)
        self._current_session_id = sess.id
        return sess

    def current_session_id(self) -> str | None:
        return self._current_session_id

    async def end_session(
        self, session_id: str | None = None, model: str | None = None,
    ) -> str:
        sid = session_id or self._current_session_id
        if not sid:
            return ""
        self.store.end_session(sid)
        if self.summarizer:
            msgs = self.store.get_messages(sid)
            if msgs:
                summary = await self.summarizer.summarize_session(msgs, model=model)
                if summary:
                    self.store.set_session_summary(sid, summary)
                    return summary
        return ""

    def add_message(
        self, role: str, content: str,
        tool_calls: list | None = None,
        tool_call_id: str | None = None,
        tokens: int = 0,
    ) -> int:
        sid = self._current_session_id
        if not sid:
            sess = self.start_session()
            sid = sess.id
        msg = Message(
            role=role, content=content, tool_calls=tool_calls,
            tool_call_id=tool_call_id, tokens=tokens, session_id=sid,
        )
        return self.store.add_message(msg, max_chars=self.cfg.max_message_chars)

    def get_recent_messages(self, limit: int = 50) -> list[Message]:
        sid = self._current_session_id
        if not sid:
            return []
        return self.store.get_messages(sid, limit=limit)

    def log_event(
        self, type_: str, summary: str,
        agent: str | None = None,
        trace_id: str | None = None,
        details: dict | None = None,
        success: bool = True,
    ) -> int:
        event = Event(
            type=type_, summary=summary, agent=agent,
            trace_id=trace_id, details=details, success=success,
            session_id=self._current_session_id,
        )
        return self.store.add_event(event)

    def find_similar_events(self, terms: list[str], limit: int = 5) -> list[Event]:
        return self.store.find_similar_events(terms, limit=limit)

    def recent_events(self, limit: int = 50) -> list[Event]:
        return self.store.get_events(limit=limit)

    def add_summary(
        self, scope: str, content: str,
        covers_from: float, covers_to: float,
        session_id: str | None = None,
    ) -> int:
        return self.store.add_summary(Summary(
            scope=scope, content=content,
            covers_from=covers_from, covers_to=covers_to,
            session_id=session_id,
        ))

    def get_recent_summaries(
        self, scope: str | None = None, limit: int = 5,
    ) -> list[Summary]:
        return self.store.get_summaries(scope=scope, limit=limit)

    def rolling_summary(self) -> str:
        parts: list[str] = []
        daily = self.store.get_summaries(scope="daily", limit=3)
        for s in reversed(daily):
            parts.append(s.content)
        session = self.store.get_summaries(
            scope="session",
            session_id=self._current_session_id,
            limit=1,
        )
        for s in reversed(session):
            parts.append(s.content)
        return "\n\n---\n\n".join(parts)

    def cleanup(self) -> dict[str, int]:
        return self.store.cleanup(self.cfg.retention)
