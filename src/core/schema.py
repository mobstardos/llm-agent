"""Pydantic-схемы деклараций: Agent, MCP, Capability.

Центральный контракт системы. Всё остальное валидируется этими моделями.
"""
from __future__ import annotations

import re
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator

CURRENT_SCHEMA_VERSION = "1.5.0"

# ═══════════════════════════════════════════════════════════════════════
# Enums
# ═══════════════════════════════════════════════════════════════════════


class RequirementLevel(str, Enum):
    HARD = "hard"
    SOFT = "soft"


class DangerLevel(str, Enum):
    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"
    EXTERNAL = "external"


class AgentStatus(str, Enum):
    ACTIVE = "active"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    DISABLED = "disabled"
    FAILED = "failed"


class ProviderHealth(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


class LoopType(str, Enum):
    REASONING = "reasoning"
    VERIFICATION = "verification"
    REFLECTION = "reflection"
    RETRY = "retry"
    FEEDBACK = "feedback"
    IMPROVEMENT = "improvement"
    WATCHDOG = "watchdog"
    ESCALATION = "escalation"
    BATCH = "batch"


# ═══════════════════════════════════════════════════════════════════════
# Требования
# ═══════════════════════════════════════════════════════════════════════


class PackageRequirement(BaseModel):
    name: str
    version: str | None = None
    level: RequirementLevel = RequirementLevel.HARD
    message: str | None = None


class EnvRequirement(BaseModel):
    name: str
    level: RequirementLevel = RequirementLevel.HARD
    message: str | None = None


class PathRequirement(BaseModel):
    name: str
    env: str | None = None
    path: str | None = None
    type: str = "dir"
    writable: bool = False
    level: RequirementLevel = RequirementLevel.HARD
    message: str | None = None


class ExternalRequirement(BaseModel):
    id: str
    host: str
    port: int
    protocol: str = "tcp"
    path: str = ""
    timeout_seconds: float = 5.0
    level: RequirementLevel = RequirementLevel.HARD
    message: str | None = None


class CapabilityRequirement(BaseModel):
    id: str
    ops: list[str] = Field(default_factory=list)
    level: RequirementLevel = RequirementLevel.HARD
    message: str | None = None


class Requirements(BaseModel):
    python_packages: list[PackageRequirement] = Field(default_factory=list)
    env_vars: list[EnvRequirement] = Field(default_factory=list)
    paths: list[PathRequirement] = Field(default_factory=list)
    external: list[ExternalRequirement] = Field(default_factory=list)
    capabilities: list[CapabilityRequirement] = Field(default_factory=list)


class Dependencies(BaseModel):
    hard: list[str] = Field(default_factory=list)
    soft: list[str] = Field(default_factory=list)


class RoutingHints(BaseModel):
    keywords: list[str] = Field(default_factory=list)
    negative_keywords: list[str] = Field(default_factory=list)
    description_for_router: str = ""


class RuntimeLimits(BaseModel):
    max_steps: int = 15
    max_result_chars: int = 30000
    timeout_seconds: int = 600


class UIHints(BaseModel):
    icon: str = "🤖"
    color: str = "#3b82f6"
    category: str = "general"


# ═══════════════════════════════════════════════════════════════════════
# Agent
# ═══════════════════════════════════════════════════════════════════════

_ID_RE = re.compile(r"^[a-z][a-z0-9_]*$")


class AgentSchema(BaseModel):
    id: str
    schema_version: str = CURRENT_SCHEMA_VERSION
    title: str
    version: str = "1.0.0"
    description: str = ""

    requires: Requirements = Field(default_factory=Requirements)
    mcp_servers: list[str] = Field(default_factory=list)
    depends_on: Dependencies = Field(default_factory=Dependencies)
    provides: list[str] = Field(default_factory=list)

    priority: int = 10
    routing_hints: RoutingHints = Field(default_factory=RoutingHints)
    prompt_vars: list[str] = Field(default_factory=list)

    mode: DangerLevel = DangerLevel.READ
    dangerous_tools: list[str] = Field(default_factory=list)

    prompt: str = "prompt.md"
    user_template: str = "user.md"

    runtime: RuntimeLimits = Field(default_factory=RuntimeLimits)
    ui: UIHints = Field(default_factory=UIHints)

    # Метаданные
    source_dir: str | None = None
    prompt_text: str | None = None
    user_template_text: str | None = None

    @field_validator("id")
    @classmethod
    def validate_id(cls, v: str) -> str:
        if not _ID_RE.match(v):
            raise ValueError(f"id должен соответствовать {_ID_RE.pattern}: {v}")
        return v


# ═══════════════════════════════════════════════════════════════════════
# MCP Server
# ═══════════════════════════════════════════════════════════════════════


class ToolSchema(BaseModel):
    name: str
    danger: DangerLevel = DangerLevel.READ
    description: str = ""
    params: dict[str, Any] = Field(default_factory=dict)


class ProcessPolicy(BaseModel):
    startup_timeout_seconds: int = 30
    restart_policy: str = "on_failure"
    max_restarts: int = 3
    backoff_seconds: list[int] = Field(default_factory=lambda: [1, 2, 5, 15])
    health_check_interval_seconds: int = 30


class MCPServerSchema(BaseModel):
    id: str
    schema_version: str = CURRENT_SCHEMA_VERSION
    title: str = ""
    description: str = ""

    command: str = "python"
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)

    requires: Requirements = Field(default_factory=Requirements)
    tools: list[ToolSchema] = Field(default_factory=list)
    mode: DangerLevel = DangerLevel.WRITE

    process: ProcessPolicy = Field(default_factory=ProcessPolicy)

    source_dir: str | None = None

    @field_validator("id")
    @classmethod
    def validate_id(cls, v: str) -> str:
        if not _ID_RE.match(v):
            raise ValueError(f"id должен соответствовать {_ID_RE.pattern}: {v}")
        return v


# ═══════════════════════════════════════════════════════════════════════
# Capability
# ═══════════════════════════════════════════════════════════════════════


class OperationSchema(BaseModel):
    id: str
    description: str = ""
    input: list[str] = Field(default_factory=list)
    output: str = ""


class HealthCheckSpec(BaseModel):
    method: str = "http_get"
    url: str | None = None
    binary: str | None = None
    host: str | None = None
    port: int | None = None
    timeout: float = 5.0


class ProviderSchema(BaseModel):
    id: str
    display_name: str = ""
    type: str = "local"
    priority: int = 50
    base_url: str | None = None
    api_key: str | None = None
    model: str | None = None
    binary: str | None = None
    langs: list[str] = Field(default_factory=list)
    supports: list[str] = Field(default_factory=list)
    cost_hint: str = "free"
    health: HealthCheckSpec | None = None


class CapabilitySchema(BaseModel):
    id: str
    schema_version: str = CURRENT_SCHEMA_VERSION
    title: str = ""
    description: str = ""

    operations: list[OperationSchema] = Field(default_factory=list)
    providers: list[ProviderSchema] = Field(default_factory=list)

    strategy: str = "fallback"
    tie_breaker: list[str] = Field(
        default_factory=lambda: ["health", "latency", "locality", "cost", "alphabetical"]
    )

    source_path: str | None = None


# ═══════════════════════════════════════════════════════════════════════
# Loop Spec
# ═══════════════════════════════════════════════════════════════════════


class LoopBudget(BaseModel):
    max_iterations: int = 15
    max_tokens: int = 50000
    max_duration_seconds: int = 600
    max_cost: float | None = None


class LoopTrigger(BaseModel):
    kind: str = "manual"  # manual | event | schedule
    event: str | None = None
    interval_seconds: float | None = None


class LoopStep(BaseModel):
    kind: str  # llm_call | tool_calls | check | wait | backoff | branch | sub_loop | emit
    params: dict[str, Any] = Field(default_factory=dict)


class LoopSpec(BaseModel):
    id: str
    type: LoopType = LoopType.REASONING
    title: str = ""
    description: str = ""

    trigger: LoopTrigger = Field(default_factory=LoopTrigger)
    budget: LoopBudget = Field(default_factory=LoopBudget)
    body: list[LoopStep] = Field(default_factory=list)

    on_success: str | None = None
    on_failure: str | None = None
    on_timeout: str | None = None

    metrics: list[str] = Field(default_factory=list)
    trace_level: str = "normal"


# ═══════════════════════════════════════════════════════════════════════
# Harness
# ═══════════════════════════════════════════════════════════════════════


class FixtureSpec(BaseModel):
    type: str
    params: dict[str, Any] = Field(default_factory=dict)


class AssertionSpec(BaseModel):
    type: str
    severity: str = "hard"  # hard | soft
    params: dict[str, Any] = Field(default_factory=dict)


class ChaosSpec(BaseModel):
    inject: list[dict] = Field(default_factory=list)
    expect: list[dict] = Field(default_factory=list)


class ScenarioSpec(BaseModel):
    id: str
    title: str
    category: str = "general"
    tags: list[str] = Field(default_factory=list)

    fixture: FixtureSpec | None = None
    prompt: str = ""

    options: dict[str, Any] = Field(default_factory=dict)

    loop: str = "reasoning"  # какой loop использовать
    assertions: list[AssertionSpec] = Field(default_factory=list)
    metrics: list[str] = Field(default_factory=list)

    chaos: ChaosSpec | None = None
