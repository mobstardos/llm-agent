"""Pydantic-схемы для harness.

Используем общий ScenarioSpec из src.core.schema для совместимости.
Здесь — расширенные структуры для конкретных аспектов.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class LlmMockResponse(BaseModel):
    messages_hash: str
    response: dict


class McpMockResponses(BaseModel):
    servers: list[str] = Field(default_factory=list)
    responses: dict[str, str] = Field(default_factory=dict)


class FixtureProject(BaseModel):
    files: dict[str, str] = Field(default_factory=dict)
    git_init: bool = False


class FixtureSeedDB(BaseModel):
    target: str = "sqlite"     # sqlite | postgres | mysql
    dump_path: str = ""


class FixtureEnvVars(BaseModel):
    vars: dict[str, str] = Field(default_factory=dict)


class AssertionResult(BaseModel):
    type: str
    passed: bool
    severity: str = "hard"     # hard | soft
    message: str = ""


class ScenarioRun(BaseModel):
    scenario_id: str
    passed: bool
    hard_failures: int = 0
    soft_failures: int = 0
    duration_ms: float = 0.0
    iterations: int = 0
    tokens_input: int = 0
    tokens_output: int = 0
    tool_calls_count: int = 0
    exit_reason: str = ""
    assertions: list[AssertionResult] = Field(default_factory=list)
    trace_id: str = ""
    run_dir: str = ""
    error: str = ""
