"""Object storage adapter. Local filesystem by default; S3-compatible in full mode."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

from app.core.config import get_settings


class Storage(Protocol):
    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str: ...
    def url(self, key: str) -> str: ...


class LocalStorage:
    def __init__(self, root: str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        path = self.root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return self.url(key)

    def url(self, key: str) -> str:
        return f"file://{(self.root / key).resolve()}"


def build_storage() -> Storage:
    settings = get_settings()
    if settings.storage_backend == "s3":  # pragma: no cover - full mode only
        import boto3

        client = boto3.client("s3", endpoint_url=settings.s3_endpoint_url)
        bucket = settings.s3_bucket

        class S3Storage:
            def put(self, key: str, data: bytes, content_type="application/octet-stream") -> str:
                client.put_object(Bucket=bucket, Key=key, Body=data, ContentType=content_type)
                return self.url(key)

            def url(self, key: str) -> str:
                return f"s3://{bucket}/{key}"

        return S3Storage()
    return LocalStorage(os.path.abspath(settings.local_storage_dir))


storage: Storage = build_storage()
