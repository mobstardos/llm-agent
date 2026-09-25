"""Pydantic-модели журнала."""
from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class ActionCategory(str, Enum):
    READ = "read"
    WRITE = "write"
    PATCH = "patch"
    DELETE = "delete"
    MOVE = "move"
    QUERY = "query"
    EXTERNAL = "external"
    ADMIN = "admin"


class SnapshotPhase(str, Enum):
    BEFORE = "before"
    AFTER = "after"


class Snapshot(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    action_id: str
    file_path: str
    phase: SnapshotPhase
    hash: str
    size_bytes: int | None = None
    mode: int | None = None
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class Action(BaseModel):
    """Одно действие агента."""

    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    trace_id: str | None = None
    session_id: str | None = None
    parent_id: str | None = None
    agent: str
    tool: str
    args: dict[str, Any] = Field(default_factory=dict)
    result_summary: str | None = None
    result_hash: str | None = None
    success: bool = True
    error: str | None = None
    duration_ms: float | None = None
    category: ActionCategory = ActionCategory.READ
    importance: float | None = None
    reversible: bool = False
    inverse_op: dict[str, Any] | None = None
    affects_files: list[str] = Field(default_factory=list)
    affects_rows: dict[str, Any] | None = None
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )

    def to_db(self) -> dict:
        return {
            "id": self.id,
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "parent_id": self.parent_id,
            "agent": self.agent,
            "tool": self.tool,
            "args": self.args,
            "result_summary": self.result_summary,
            "result_hash": self.result_hash,
            "success": self.success,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "category": self.category.value,
            "importance": self.importance,
            "reversible": self.reversible,
            "inverse_op": self.inverse_op,
            "affects_files": self.affects_files,
            "affects_rows": self.affects_rows,
            "created_at": self.created_at,
        }


class Checkpoint(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    label: str | None = None
    action_id: str | None = None
    files_snapshot: dict[str, str] = Field(default_factory=dict)
    reason: str = "manual"
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
    )


class DependencyKind(str, Enum):
    CAUSED_BY = "caused_by"
    DEPENDS_ON = "depends_on"
    CONFLICTS_WITH = "conflicts_with"
    SUPERSEDES = "supersedes"


# ═══════════════════════════════════════════════════════════════════════
# Утилиты
# ═══════════════════════════════════════════════════════════════════════


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def classify_tool(tool_name: str) -> tuple[ActionCategory, bool]:
    """Возвращает (категория, обратимо ли).

    tool_name вида 'filesystem__write_file'.
    """
    t = tool_name.split("__")[-1].lower()

    # Read-only
    if t in ("read_file", "read_file_range", "list_files", "list_tree",
             "search_in_files", "find_by_glob", "find_by_name",
             "find_by_content", "find_by_mtime", "find_by_size", "grep",
             "file_info", "disk_usage", "diff_files", "hash_file",
             "count_lines", "dir_stats", "list_archive", "current_root",
             "read_symlink"):
        return ActionCategory.READ, False

    # Write файлов
    if t in ("write_file",):
        return ActionCategory.WRITE, True
    if t in ("apply_patch",):
        return ActionCategory.PATCH, True
    if t in ("delete_file", "delete_dir"):
        return ActionCategory.DELETE, True
    if t in ("move_file", "rename_file", "copy_file", "move_dir"):
        return ActionCategory.MOVE, True
    if t in ("create_dir", "symlink", "chmod", "create_archive",
             "extract_archive", "create_7z", "extract_7z", "extract_rar"):
        return ActionCategory.WRITE, True

    # БД
    if t in ("run_query", "list_tables", "describe_table",
             "list_schemas", "driver_info", "current_driver",
             "list_objects", "read_object", "read_module",
             "mongo_find", "mongo_collections", "mongo_aggregate",
             "es_search", "es_indices", "es_mapping", "es_count",
             "mssql_query", "mssql_tables", "mssql_describe",
             "redis_get", "redis_keys", "redis_info"):
        return ActionCategory.QUERY, False

    if t in ("execute_write_query", "execute_migration",
             "mongo_insert", "mongo_update", "mongo_delete",
             "redis_set", "redis_delete", "redis_publish"):
        return ActionCategory.QUERY, True

    # Git — write, но откат через git reset
    if t in ("commit", "add", "reset", "checkout", "branch_create"):
        return ActionCategory.WRITE, True

    # Внешние
    if t in ("http_get", "http_post", "http_put", "http_delete",
             "http_patch", "http_request", "graphql_query",
             "websocket_send_receive", "load_test", "sse_read",
             "navigate", "click", "fill", "select_option", "press_key",
             "hover", "evaluate", "reload", "go_back", "go_forward"):
        return ActionCategory.EXTERNAL, False

    # Shell, Docker, K8s — необратимые в общем случае
    if t in ("run", "build_run", "docker_build", "docker_run",
             "docker_stop", "docker_compose_up", "docker_compose_down",
             "k8s_exec_pod", "k8s_apply_manifest", "k8s_delete_manifest",
             "k8s_apply_file", "k8s_scale_deployment",
             "k8s_restart_deployment", "k8s_rollout_undo",
             "load_config", "update_db", "restore_ib", "create_ib",
             "build_cf", "build_cfe", "build_epf",
             "migration_up", "migration_down", "migration_stamp",
             "git_push"):
        return ActionCategory.ADMIN, False

    # Всё остальное — admin с осторожностью
    return ActionCategory.ADMIN, False


def is_reversible_tool(tool_name: str) -> bool:
    _, reversible = classify_tool(tool_name)
    return reversible
