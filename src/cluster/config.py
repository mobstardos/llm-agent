"""Cluster config."""
from __future__ import annotations

import os
from pydantic import BaseModel, Field


class ClusterConfig(BaseModel):
    enabled: bool = Field(
        default_factory=lambda: os.getenv("CLUSTER_ENABLED", "false").lower() == "true"
    )
    instance_id: str = Field(
        default_factory=lambda: os.getenv("INSTANCE_ID", "local")
    )
    redis_url: str = Field(
        default_factory=lambda: os.getenv("REDIS_URL", "redis://localhost:6379/0")
    )
    database_url: str = Field(
        default_factory=lambda: os.getenv("DATABASE_URL", "")
    )
    heartbeat_interval_seconds: int = 30
    instance_ttl_seconds: int = 90
    channel_snapshot: str = "llmagent:snapshot"
    channel_ws_prefix: str = "llmagent:ws:"
    channel_instance_prefix: str = "llmagent:instance:"
