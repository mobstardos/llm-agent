"""S3/MinIO backend через boto3/aioboto3."""
from __future__ import annotations

import logging
import mimetypes
import time
from datetime import datetime, timezone
from pathlib import Path

from src.storage.base import StorageObject, UploadResult

logger = logging.getLogger(__name__)


class S3Backend:
    """S3-совместимый backend (AWS S3, MinIO, DigitalOcean Spaces, ...)."""

    id = "s3"
    kind = "s3"

    def __init__(
        self,
        endpoint_url: str | None = None,
        access_key: str = "",
        secret_key: str = "",
        region: str = "us-east-1",
        bucket: str = "",
        use_ssl: bool = True,
        presign_expires: int = 3600,
    ):
        self.endpoint_url = endpoint_url or None
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region
        self.bucket = bucket
        self.use_ssl = use_ssl
        self.presign_expires = presign_expires

    async def _client(self):
        try:
            import aioboto3
        except ImportError:
            raise RuntimeError("aioboto3 не установлен: pip install aioboto3")

        session = aioboto3.Session()
        return session.client(
            "s3",
            endpoint_url=self.endpoint_url,
            aws_access_key_id=self.access_key,
            aws_secret_access_key=self.secret_key,
            region_name=self.region,
            use_ssl=self.use_ssl,
        )

    def available(self) -> bool:
        try:
            import aioboto3  # noqa: F401
            return True
        except ImportError:
            return False

    async def upload(
        self, local_path: str | Path, key: str | None = None,
        content_type: str | None = None,
    ) -> UploadResult:
        p = Path(local_path)
        if not p.exists():
            return UploadResult(
                key=key or "", size=0, success=False,
                error=f"Файл не найден: {p}",
            )

        k = key or p.name
        if content_type is None:
            ct, _ = mimetypes.guess_type(str(p))
            content_type = ct or "application/octet-stream"

        try:
            async with await self._client() as client:
                await client.upload_file(
                    str(p), self.bucket, k,
                    ExtraArgs={"ContentType": content_type},
                )
            return UploadResult(
                key=k, size=p.stat().st_size, success=True,
            )
        except Exception as e:
            logger.exception("S3 upload failed")
            return UploadResult(
                key=k, size=0, success=False, error=str(e),
            )

    async def download(
        self, key: str, local_path: str | Path,
    ) -> bool:
        p = Path(local_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            async with await self._client() as client:
                await client.download_file(self.bucket, key, str(p))
            return True
        except Exception as e:
            logger.exception("S3 download failed")
            return False

    async def list(
        self, prefix: str = "", limit: int = 1000,
    ) -> list[StorageObject]:
        result: list[StorageObject] = []
        try:
            async with await self._client() as client:
                paginator = client.get_paginator("list_objects_v2")
                async for page in paginator.paginate(
                    Bucket=self.bucket, Prefix=prefix,
                ):
                    for obj in page.get("Contents", []):
                        result.append(StorageObject(
                            key=obj["Key"],
                            size=obj.get("Size", 0),
                            last_modified=obj.get("LastModified"),
                            etag=obj.get("ETag", "").strip('"'),
                        ))
                        if len(result) >= limit:
                            return result
        except Exception as e:
            logger.exception("S3 list failed")
        return result

    async def delete(self, key: str) -> bool:
        try:
            async with await self._client() as client:
                await client.delete_object(Bucket=self.bucket, Key=key)
            return True
        except Exception as e:
            logger.exception("S3 delete failed")
            return False

    async def exists(self, key: str) -> bool:
        try:
            async with await self._client() as client:
                await client.head_object(Bucket=self.bucket, Key=key)
            return True
        except Exception:
            return False

    async def presign(
        self, key: str, expires_sec: int = 3600,
    ) -> str:
        try:
            async with await self._client() as client:
                url = await client.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": self.bucket, "Key": key},
                    ExpiresIn=expires_sec,
                )
                return url
        except Exception as e:
            logger.exception("S3 presign failed")
            return ""

    async def list_buckets(self) -> list[str]:
        try:
            async with await self._client() as client:
                resp = await client.list_buckets()
                return [b["Name"] for b in resp.get("Buckets", [])]
        except Exception:
            return []
