"""Глобальный конфиг: .env + settings.yaml + PostgreSQL.

Все настройки читаются здесь, кэшируются через lru_cache.
"""
import os
import logging
import re
from functools import lru_cache
from pathlib import Path

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "config"
SETTINGS_PATH = CONFIG_DIR / "settings.yaml"

_ENV_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(value):
    if isinstance(value, str):
        return _ENV_PATTERN.sub(
            lambda m: os.environ.get(m.group(1), ""), value,
        )
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


# ═══════════════════════════════════════════════════════════════════════
# Settings
# ═══════════════════════════════════════════════════════════════════════


class LLMSettings(BaseModel):
    api_key: str = Field(
        default_factory=lambda: os.getenv("LLM_API_KEY", ""),
    )
    base_url: str = Field(
        default_factory=lambda: os.getenv(
            "LLM_BASE_URL", "https://api.openai.com/v1",
        ),
    )
    model: str = Field(
        default_factory=lambda: os.getenv("LLM_MODEL", "gpt-4o"),
    )
    temperature: float = Field(
        default_factory=lambda: float(os.getenv("LLM_TEMPERATURE", "0.1")),
    )


class QwenProxySettings(BaseModel):
    host: str = Field(
        default_factory=lambda: os.getenv("QWENPROXY_HOST", "127.0.0.1"),
    )
    port: int = Field(
        default_factory=lambda: int(os.getenv("QWENPROXY_PORT", "7936")),
    )
    auto_start: bool = Field(
        default_factory=lambda: os.getenv(
            "QWENPROXY_AUTO_START", "true",
        ).lower() == "true",
    )
    enabled: bool = Field(
        default_factory=lambda: os.getenv(
            "QWENPROXY_ENABLED", "true",
        ).lower() == "true",
    )


class CacheSettings(BaseModel):
    enabled: bool = Field(
        default_factory=lambda: os.getenv(
            "CACHE_ENABLED", "true",
        ).lower() == "true",
    )
    ttl: int = Field(
        default_factory=lambda: int(os.getenv("CACHE_TTL", "3600")),
    )
    path: str = Field(
        default_factory=lambda: str(BASE_DIR / "data" / "cache.sqlite"),
    )
    replay_speed: float = Field(
        default_factory=lambda: float(os.getenv("CACHE_REPLAY_SPEED", "1.0")),
    )


class StreamingSettings(BaseModel):
    enabled: bool = Field(
        default_factory=lambda: os.getenv(
            "STREAMING_ENABLED", "true",
        ).lower() == "true",
    )
    replay_delay: float = Field(
        default_factory=lambda: float(
            os.getenv("STREAMING_REPLAY_DELAY", "0.01"),
        ),
    )


class PolicySettings(BaseModel):
    path: str = Field(
        default_factory=lambda: str(BASE_DIR / "data" / "policies.sqlite"),
    )
    ttl_days: int = Field(
        default_factory=lambda: int(os.getenv("POLICY_TTL_DAYS", "30")),
    )
    cleanup_interval_hours: float = Field(
        default_factory=lambda: float(
            os.getenv("POLICY_CLEANUP_INTERVAL_HOURS", "6"),
        ),
    )
    deleted_history_limit: int = Field(
        default_factory=lambda: int(
            os.getenv("POLICY_DELETED_HISTORY_LIMIT", "1000"),
        ),
    )
    backup_on_exit: bool = Field(
        default_factory=lambda: os.getenv(
            "POLICY_BACKUP_ON_EXIT", "true",
        ).lower() == "true",
    )
    backup_path: str = Field(
        default_factory=lambda: str(
            BASE_DIR / "data" / "policies-backup.json",
        ),
    )
    backup_dir: str = Field(
        default_factory=lambda: str(
            BASE_DIR / "data" / "policies_backups",
        ),
    )
    backup_interval_hours: float = Field(
        default_factory=lambda: float(
            os.getenv("POLICY_BACKUP_INTERVAL_HOURS", "24"),
        ),
    )
    backup_keep: int = Field(
        default_factory=lambda: int(os.getenv("POLICY_BACKUP_KEEP", "7")),
    )
    backup_initial_delay_minutes: float = Field(
        default_factory=lambda: float(
            os.getenv("POLICY_BACKUP_INITIAL_DELAY_MINUTES", "5"),
        ),
    )


class FileStateSettings(BaseModel):
    ttl: float = Field(
        default_factory=lambda: float(os.getenv("FILE_STATE_TTL", "1.0")),
    )
    max_files: int = Field(
        default_factory=lambda: int(
            os.getenv("FILE_STATE_MAX_FILES", "50000"),
        ),
    )


class LoopSettings(BaseModel):
    max_iterations: int = Field(
        default_factory=lambda: int(os.getenv("LOOP_MAX_ITERATIONS", "15")),
    )
    max_tokens: int = Field(
        default_factory=lambda: int(os.getenv("LOOP_MAX_TOKENS", "50000")),
    )
    max_duration_seconds: int = Field(
        default_factory=lambda: int(
            os.getenv("LOOP_MAX_DURATION_SECONDS", "600"),
        ),
    )
    soft_limit: float = Field(
        default_factory=lambda: float(os.getenv("LOOP_SOFT_LIMIT", "0.7")),
    )
    hard_limit: float = Field(
        default_factory=lambda: float(os.getenv("LOOP_HARD_LIMIT", "0.85")),
    )
    kill_limit: float = Field(
        default_factory=lambda: float(os.getenv("LOOP_KILL_LIMIT", "0.95")),
    )


class HarnessSettings(BaseModel):
    runs_dir: str = Field(
        default_factory=lambda: os.getenv(
            "HARNESS_RUNS_DIR", str(BASE_DIR / "data" / "harness_runs"),
        ),
    )
    keep_runs: bool = Field(
        default_factory=lambda: os.getenv(
            "HARNESS_KEEP_RUNS", "true",
        ).lower() == "true",
    )
    deterministic: bool = Field(
        default_factory=lambda: os.getenv(
            "HARNESS_DETERMINISTIC", "false",
        ).lower() == "true",
    )


# ═══════════════════════════════════════════════════════════════════════
# PostgreSQL + pgvector
# ═══════════════════════════════════════════════════════════════════════


class PostgresSettings(BaseModel):
    """Настройки основной PostgreSQL базы (pgvector)."""

    enabled: bool = Field(
        default_factory=lambda: os.getenv(
            "PG_ENABLED", "true",
        ).lower() == "true",
    )

    # Полный DSN (приоритет)
    database_url: str = Field(
        default_factory=lambda: os.getenv("DATABASE_URL", ""),
    )

    # Компоненты (если DATABASE_URL не задан)
    host: str = Field(
        default_factory=lambda: os.getenv("PG_APP_HOST", "localhost"),
    )
    port: int = Field(
        default_factory=lambda: int(os.getenv("PG_APP_PORT", "5432")),
    )
    user: str = Field(
        default_factory=lambda: os.getenv("PG_APP_USER", "llmagent"),
    )
    password: str = Field(
        default_factory=lambda: os.getenv("PG_APP_PASSWORD", "secret"),
    )
    database: str = Field(
        default_factory=lambda: os.getenv("PG_APP_DATABASE", "llmagent"),
    )

    # Pool
    pool_min: int = Field(
        default_factory=lambda: int(os.getenv("PG_POOL_MIN", "2")),
    )
    pool_max: int = Field(
        default_factory=lambda: int(os.getenv("PG_POOL_MAX", "20")),
    )

    def dsn(self) -> str:
        if self.database_url.strip():
            # Защита: в окружении могут быть чужие DATABASE_URL
            # (например, sqlite/file:). Принимаем только PostgreSQL.
            url = self.database_url.strip()
            if url.startswith(("postgres://", "postgresql://")):
                return url
            os.environ.pop("DATABASE_URL", None)
            logger.warning(
                "DATABASE_URL не похож на PostgreSQL DSN (%r) — "
                "игнорирую, собираю DSN из компонентов",
                url[:40],
            )
        return (
            f"postgresql://{self.user}:{self.password}@"
            f"{self.host}:{self.port}/{self.database}"
        )


class BackupSettings(BaseModel):
    enabled: bool = Field(
        default_factory=lambda: os.getenv(
            "BACKUP_ENABLED", "true",
        ).lower() == "true",
    )
    interval_hours: float = Field(
        default_factory=lambda: float(
            os.getenv("BACKUP_INTERVAL_HOURS", "24"),
        ),
    )
    initial_delay_seconds: float = Field(
        default_factory=lambda: float(
            os.getenv("BACKUP_INITIAL_DELAY_SECONDS", "300"),
        ),
    )
    keep_last: int = Field(
        default_factory=lambda: int(os.getenv("BACKUP_KEEP_LAST", "7")),
    )
    dir: str = Field(
        default_factory=lambda: os.getenv(
            "BACKUP_DIR", str(BASE_DIR / "data" / "db_backups"),
        ),
    )


class OllamaSettings(BaseModel):
    enabled: bool = Field(
        default_factory=lambda: os.getenv(
            "OLLAMA_ENABLED", "true",
        ).lower() == "true",
    )
    url: str = Field(
        default_factory=lambda: os.getenv(
            "OLLAMA_URL", "http://127.0.0.1:11434",
        ),
    )
    model: str = Field(
        default_factory=lambda: os.getenv(
            "OLLAMA_MODEL", "qwen2.5:1.5b-instruct",
        ),
    )
    keep_alive: str = Field(
        default_factory=lambda: os.getenv("OLLAMA_KEEP_ALIVE", "5m"),
    )
    timeout: float = Field(
        default_factory=lambda: float(os.getenv("OLLAMA_TIMEOUT", "60")),
    )

    # EnrichmentWorker
    enrich_batch_size: int = Field(
        default_factory=lambda: int(os.getenv("ENRICH_BATCH_SIZE", "50")),
    )
    enrich_interval_seconds: float = Field(
        default_factory=lambda: float(
            os.getenv("ENRICH_INTERVAL", "30"),
        ),
    )
    enrich_concurrency: int = Field(
        default_factory=lambda: int(os.getenv("ENRICH_CONCURRENCY", "4")),
    )


class AgeSettings(BaseModel):
    enabled: bool = Field(
        default_factory=lambda: os.getenv(
            "AGE_ENABLED", "false",
        ).lower() == "true",
    )
    dsn: str = Field(
        default_factory=lambda: os.getenv(
            "AGE_DSN",
            "postgresql://llmagent:secret@localhost:5433/llmagent",
        ),
    )
    graph: str = Field(
        default_factory=lambda: os.getenv("AGE_GRAPH", "llm_graph"),
    )


class KafkaSettings(BaseModel):
    enabled: bool = Field(
        default_factory=lambda: os.getenv(
            "KAFKA_ENABLED", "false",
        ).lower() == "true",
    )
    bootstrap_servers: str = Field(
        default_factory=lambda: os.getenv(
            "KAFKA_BOOTSTRAP_SERVERS", "localhost:9092",
        ),
    )
    topic_prefix: str = Field(
        default_factory=lambda: os.getenv(
            "KAFKA_TOPIC_PREFIX", "llmagent.cdc",
        ),
    )


class StorageStrategySettings(BaseModel):
    """Какую стратегию использовать для векторов/episodic."""

    # lancedb | postgres | dual (пишем в оба, читаем из primary)
    vector_store: str = Field(
        default_factory=lambda: os.getenv(
            "PRIMARY_VECTOR_STORE", "postgres",
        ),
    )
    # sqlite | postgres | dual
    episodic_store: str = Field(
        default_factory=lambda: os.getenv(
            "PRIMARY_EPISODIC_STORE", "postgres",
        ),
    )
    # sqlite | postgres | dual
    graph_store: str = Field(
        default_factory=lambda: os.getenv(
            "PRIMARY_GRAPH_STORE", "postgres",
        ),
    )
    dual_write: bool = Field(
        default_factory=lambda: os.getenv(
            "DUAL_WRITE_ENABLED", "false",
        ).lower() == "true",
    )


class InstanceSettings(BaseModel):
    id: str = Field(
        default_factory=lambda: os.getenv("INSTANCE_ID", ""),
    )
    cluster_enabled: bool = Field(
        default_factory=lambda: os.getenv(
            "CLUSTER_ENABLED", "false",
        ).lower() == "true",
    )
    redis_url: str = Field(
        default_factory=lambda: os.getenv(
            "REDIS_URL", "redis://localhost:6379/0",
        ),
    )


class AppSettings(BaseModel):
    project_root: str = Field(
        default_factory=lambda: os.getenv("PROJECT_ROOT", str(BASE_DIR)),
    )
    web_host: str = Field(
        default_factory=lambda: os.getenv("WEB_HOST", "127.0.0.1"),
    )
    web_port: int = Field(
        default_factory=lambda: int(os.getenv("WEB_PORT", "8000")),
    )

    # Подсистемы
    llm: LLMSettings = Field(default_factory=LLMSettings)
    qwenproxy: QwenProxySettings = Field(default_factory=QwenProxySettings)
    cache: CacheSettings = Field(default_factory=CacheSettings)
    streaming: StreamingSettings = Field(default_factory=StreamingSettings)
    policies: PolicySettings = Field(default_factory=PolicySettings)
    file_state: FileStateSettings = Field(default_factory=FileStateSettings)
    loop: LoopSettings = Field(default_factory=LoopSettings)
    harness: HarnessSettings = Field(default_factory=HarnessSettings)

    # Новые (Части 1–5)
    postgres: PostgresSettings = Field(default_factory=PostgresSettings)
    backup: BackupSettings = Field(default_factory=BackupSettings)
    ollama: OllamaSettings = Field(default_factory=OllamaSettings)
    age: AgeSettings = Field(default_factory=AgeSettings)
    kafka: KafkaSettings = Field(default_factory=KafkaSettings)
    storage_strategy: StorageStrategySettings = Field(
        default_factory=StorageStrategySettings,
    )
    instance: InstanceSettings = Field(default_factory=InstanceSettings)

    # MySQL / SQLite
    mysql_host: str = Field(
        default_factory=lambda: os.getenv("MYSQL_HOST", ""),
    )
    mysql_port: int = Field(
        default_factory=lambda: int(os.getenv("MYSQL_PORT", "3306")),
    )
    mysql_user: str = Field(
        default_factory=lambda: os.getenv("MYSQL_USER", ""),
    )
    mysql_password: str = Field(
        default_factory=lambda: os.getenv("MYSQL_PASSWORD", ""),
    )
    mysql_database: str = Field(
        default_factory=lambda: os.getenv("MYSQL_DATABASE", ""),
    )
    sqlite_path: str = Field(
        default_factory=lambda: os.getenv("SQLITE_PATH", ""),
    )

    # PostgreSQL (для MCP-серверов, отдельно от PG_APP)
    pg_host: str = Field(
        default_factory=lambda: os.getenv("PG_HOST", ""),
    )
    pg_port: int = Field(
        default_factory=lambda: int(os.getenv("PG_PORT", "5432")),
    )
    pg_user: str = Field(
        default_factory=lambda: os.getenv("PG_USER", ""),
    )
    pg_password: str = Field(
        default_factory=lambda: os.getenv("PG_PASSWORD", ""),
    )
    pg_database: str = Field(
        default_factory=lambda: os.getenv("PG_DATABASE", ""),
    )

    # 1C
    onec_base_url: str = Field(
        default_factory=lambda: os.getenv("ONEC_BASE_URL", ""),
    )
    onec_user: str = Field(
        default_factory=lambda: os.getenv("ONEC_USER", ""),
    )
    onec_password: str = Field(
        default_factory=lambda: os.getenv("ONEC_PASSWORD", ""),
    )

    # ─── Convenience ─────────────────────────────────────
    @property
    def use_postgres(self) -> bool:
        return self.postgres.enabled

    @property
    def use_lancedb(self) -> bool:
        return self.storage_strategy.vector_store in ("lancedb", "dual")

    @property
    def use_sqlite_memory(self) -> bool:
        return self.storage_strategy.episodic_store in ("sqlite", "dual")

    @property
    def use_sqlite_graph(self) -> bool:
        return self.storage_strategy.graph_store in ("sqlite", "dual")


@lru_cache
def get_settings() -> AppSettings:
    return AppSettings()


@lru_cache
def get_yaml_config() -> dict:
    if not SETTINGS_PATH.exists():
        return {}
    with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    return _expand_env(cfg)
