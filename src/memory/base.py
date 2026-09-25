"""Базовые типы памяти."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone


def now_ts() -> float:
    return datetime.now(timezone.utc).timestamp()


def new_id(prefix: str = "") -> str:
    return f"{prefix}{uuid.uuid4().hex[:16]}"


@dataclass
class Message:
    role: str
    content: str
    ts: float = field(default_factory=now_ts)
    tokens: int = 0
    tool_calls: list[dict] | None = None
    tool_call_id: str | None = None
    id: int | None = None
    session_id: str | None = None


@dataclass
class Event:
    type: str
    summary: str
    ts: float = field(default_factory=now_ts)
    session_id: str | None = None
    trace_id: str | None = None
    agent: str | None = None
    details: dict | None = None
    success: bool = True
    id: int | None = None


@dataclass
class Session:
    id: str
    started_at: float
    ended_at: float | None = None
    title: str | None = None
    summary: str | None = None
    message_count: int = 0
    tokens_used: int = 0


@dataclass
class Summary:
    scope: str
    content: str
    covers_from: float
    covers_to: float
    session_id: str | None = None
    created_at: float = field(default_factory=now_ts)
    id: int | None = None


@dataclass
class Chunk:
    id: str
    text: str
    vector: list[float]
    metadata: dict


@dataclass
class Procedure:
    id: str
    trigger: str
    steps: list[str]
    success_count: int = 0
    fail_count: int = 0
    last_used: float = 0.0
    tags: list[str] = field(default_factory=list)

    @property
    def success_rate(self) -> float:
        total = self.success_count + self.fail_count
        return (self.success_count / total) if total else 0.0


@dataclass
class RetrievedChunk:
    text: str
    score: float
    metadata: dict
