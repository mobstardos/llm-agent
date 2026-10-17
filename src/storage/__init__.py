"""Cloud Storage — абстракция над S3/MinIO/local."""
from src.storage.base import StorageBackend, StorageObject
from src.storage.factory import create_storage

__all__ = ["StorageBackend", "StorageObject", "create_storage"]
