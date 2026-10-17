"""Конфигурация памяти — читается из config/memory.yaml."""
from __future__ import annotations

import logging
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent.parent
DEFAULT_PATH = BASE_DIR / "config" / "memory.yaml"


class WorkingBudget(BaseModel):
    system: float = 0.15
    profile: float = 0.10
    recent: float = 0.45
    summary: float = 0.10
    recall: float = 0.20


class WorkingSettings(BaseModel):
    max_context_tokens: int = 32000
    compact_threshold: float = 0.60
    truncate_threshold: float = 0.80
    budget: WorkingBudget = Field(default_factory=WorkingBudget)


class RetentionSettings(BaseModel):
    raw_messages_days: int = 90
    events_days: int = 180
    summaries_days: int = 730


class AutoSummarySettings(BaseModel):
    session_end: bool = True
    daily: bool = True
    daily_hour: int = 3


class EpisodicSettings(BaseModel):
    db_path: str = "data/memory.sqlite"
    retention: RetentionSettings = Field(default_factory=RetentionSettings)
    auto_summary: AutoSummarySettings = Field(default_factory=AutoSummarySettings)
    max_message_chars: int = 50000


class SemanticSettings(BaseModel):
    profile_path: str = "data/project_profile.yaml"
    profile_md_path: str = "data/project_profile.md"
    auto_generate: bool = True
    regenerate_interval_hours: int = 24


class EmbedderSettings(BaseModel):
    model: str = "BAAI/bge-m3"
    device: str = "cpu"
    batch_size: int = 32
    max_length: int = 512
    cache_dir: str = "data/embedder_cache"


class ChunkingSettings(BaseModel):
    strategy: str = "ast"
    max_tokens: int = 400
    overlap_tokens: int = 50
    min_tokens: int = 20


class IndexingSettings(BaseModel):
    file_patterns: list[str] = Field(default_factory=lambda: [
        "**/*.py", "**/*.js", "**/*.ts", "**/*.md",
        "**/*.yaml", "**/*.yml", "**/*.json", "**/*.sql", "**/*.bsl",
    ])
    ignore_patterns: list[str] = Field(default_factory=lambda: [
        "**/.git/**", "**/node_modules/**", "**/__pycache__/**",
        "**/.venv/**", "**/dist/**", "**/build/**", "**/data/**",
    ])
    max_file_size_kb: int = 500
    incremental: bool = True
    file_watch: bool = True


class RetrievalSettings(BaseModel):
    top_k: int = 10
    min_score: float = 0.35
    rerank: bool = True
    dedup_by_file: bool = True
    max_per_file: int = 3


class VectorSettings(BaseModel):
    enabled: bool = True
    store_path: str = "data/vector.lance"
    table_name: str = "chunks"
    embedder: EmbedderSettings = Field(default_factory=EmbedderSettings)
    chunking: ChunkingSettings = Field(default_factory=ChunkingSettings)
    indexing: IndexingSettings = Field(default_factory=IndexingSettings)
    retrieval: RetrievalSettings = Field(default_factory=RetrievalSettings)


class ProceduralSettings(BaseModel):
    path: str = "data/procedures.yaml"
    enabled: bool = True
    min_success_count: int = 2
    min_success_rate: float = 0.7
    max_procedures: int = 500


class UserSettings(BaseModel):
    path: str = "data/user_profile.yaml"
    enabled: bool = True
    learn_from_approvals: bool = True
    learn_from_rejections: bool = True
    auto_analyze_interval_hours: int = 168


class GraphSettings(BaseModel):
    enabled: bool = True
    db_path: str = "data/graph.sqlite"
    parsers: dict[str, bool] = Field(default_factory=lambda: {
        "python": True, "javascript": False, "sql": True,
        "bsl": False, "xml": True,
    })
    rebuild_interval_hours: int = 24


class MCPSettings(BaseModel):
    enabled: bool = True
    server_id: str = "memory"


class MemoryConfig(BaseModel):
    enabled: bool = True
    working: WorkingSettings = Field(default_factory=WorkingSettings)
    episodic: EpisodicSettings = Field(default_factory=EpisodicSettings)
    semantic: SemanticSettings = Field(default_factory=SemanticSettings)
    vector: VectorSettings = Field(default_factory=VectorSettings)
    procedural: ProceduralSettings = Field(default_factory=ProceduralSettings)
    user: UserSettings = Field(default_factory=UserSettings)
    graph: GraphSettings = Field(default_factory=GraphSettings)
    mcp: MCPSettings = Field(default_factory=MCPSettings)

    def abs_path(self, path: str) -> Path:
        p = Path(path)
        return p if p.is_absolute() else BASE_DIR / p


@lru_cache
def load_memory_config() -> MemoryConfig:
    if not DEFAULT_PATH.exists():
        return MemoryConfig()
    try:
        with open(DEFAULT_PATH, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        return MemoryConfig(**data)
    except Exception as e:
        logger.exception("Ошибка чтения memory.yaml: %s", e)
        return MemoryConfig()
