"""Фабрика storage backend по env."""
from __future__ import annotations

import logging
import os

from src.storage.base import StorageBackend
from src.storage.local_backend import LocalBackend
from src.storage.s3_backend import S3Backend

logger = logging.getLogger(__name__)


def create_storage(kind: str | None = None) -> StorageBackend:
    """Создаёт backend. По умолчанию — из env STORAGE_BACKEND."""
    kind = kind or os.getenv("STORAGE_BACKEND", "local").lower()

    if kind == "s3" or kind == "minio":
        endpoint = os.getenv("S3_ENDPOINT", "").strip() or None
        return S3Backend(
            endpoint_url=endpoint,
            access_key=os.getenv("S3_ACCESS_KEY", ""),
            secret_key=os.getenv("S3_SECRET_KEY", ""),
            region=os.getenv("S3_REGION", "us-east-1"),
            bucket=os.getenv("S3_BUCKET", "llmagent"),
            use_ssl=os.getenv("S3_USE_SSL", "true").lower() == "true",
        )

    # Local — дефолт
    base_dir = os.getenv(
        "STORAGE_LOCAL_DIR",
        "data/storage",
    )
    return LocalBackend(base_dir)
