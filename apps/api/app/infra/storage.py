"""Object storage adapter. Local filesystem by default; S3-compatible in full mode.

Hardening shared by both backends:

* **Key validation.** :func:`validate_object_key` rejects an empty, absolute,
  backslash-bearing, null-byte-bearing, ``..``-traversing or non-normalized key
  *before* any I/O, so a crafted key can never escape the storage root / bucket
  prefix.
* **Tenant-scoped prefixes.** :func:`tenant_object_key` builds a key under
  ``{organization_id}/{workspace_id}/`` from server-side context; a tenant never
  supplies a raw key, so it cannot address another tenant's objects.
* **Size bound.** An upload larger than ``s3_max_object_bytes`` is rejected up
  front (:class:`ObjectTooLargeError`), never streamed to the backend.
* **Private by default.** S3 uploads set no public ACL; objects inherit the
  bucket's private default. Server-side encryption is expected to be enforced at
  the bucket level (documented, not overridden per object).
* **Bounded + sanitized.** The S3 client uses bounded connect/read timeouts and a
  bounded retry count; a bucket name, endpoint or key is never logged, and a
  driver error becomes an :class:`ObjectStorageUnavailableError` with a static
  message. Production buckets are never auto-created.
"""

from __future__ import annotations

import os
import posixpath
from pathlib import Path
from typing import Any, Protocol

from app.core.config import Settings, get_settings
from app.core.errors import (
    InvalidObjectKeyError,
    ObjectStorageUnavailableError,
    ObjectTooLargeError,
)
from app.core.tracing import STORAGE_SIGN_URL, STORAGE_UPLOAD, start_span


def validate_object_key(key: str) -> str:
    """Return ``key`` if it is a safe relative object key, else raise.

    Rejects anything that could escape the storage root / bucket prefix or smuggle
    a path-traversal: empty, absolute, backslash, null byte, ``.``/``..`` or empty
    segments, and any key not already in normalized form.
    """
    if not isinstance(key, str) or key == "":
        raise InvalidObjectKeyError("object key must be a non-empty string")
    if "\x00" in key:
        raise InvalidObjectKeyError("object key must not contain a null byte")
    if key.startswith("/"):
        raise InvalidObjectKeyError("object key must be relative, not absolute")
    if "\\" in key:
        raise InvalidObjectKeyError("object key must not contain a backslash")
    segments = key.split("/")
    if any(seg in ("", ".", "..") for seg in segments):
        raise InvalidObjectKeyError("object key must not contain empty or relative segments")
    if posixpath.normpath(key) != key:
        raise InvalidObjectKeyError("object key is not in normalized form")
    return key


def _validate_key_segment(segment: str, *, label: str) -> str:
    """Return ``segment`` if it is a single safe path segment, else raise.

    A tenant identifier must occupy exactly one path segment: no separators, null
    byte or ``.``/``..`` traversal. This is what stops a hostile organization or
    workspace id from smuggling extra segments (or an escape) into a composed key.
    """
    if not isinstance(segment, str) or segment == "":
        raise InvalidObjectKeyError(f"{label} must be a non-empty string")
    if "\x00" in segment:
        raise InvalidObjectKeyError(f"{label} must not contain a null byte")
    if "/" in segment or "\\" in segment:
        raise InvalidObjectKeyError(f"{label} must not contain a path separator")
    if segment in (".", ".."):
        raise InvalidObjectKeyError(f"{label} must not be a relative segment")
    return segment


def tenant_object_key(organization_id: str, workspace_id: str, *parts: str) -> str:
    """Build a validated, tenant-scoped object key from server-side context.

    Every component is validated, not only the caller-supplied relative parts: the
    organization and workspace identifiers must each be a single safe path segment,
    and the *fully composed* key is re-validated. A hostile tenant identifier can
    therefore not inject a separator or a path traversal into the physical key.
    """
    _validate_key_segment(organization_id, label="organization id")
    _validate_key_segment(workspace_id, label="workspace id")
    relative = "/".join(parts)
    validate_object_key(relative)
    composed = f"{organization_id}/{workspace_id}/{relative}"
    return validate_object_key(composed)


class Storage(Protocol):
    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str: ...
    def get(self, key: str) -> bytes: ...
    def head(self, key: str) -> bool: ...
    def delete(self, key: str) -> None: ...
    def url(self, key: str) -> str: ...
    def signed_url(self, key: str, expires_in: int | None = None) -> str: ...
    def ping(self) -> bool: ...
    def close(self) -> None: ...


class LocalStorage:
    """Local-filesystem storage. Mirrors the S3 contract (validation, size, head)."""

    def __init__(self, root: str, *, max_object_bytes: int) -> None:
        self.root = Path(root)
        self._max = max_object_bytes
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        validate_object_key(key)
        return self.root / key

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        path = self._path(key)
        if len(data) > self._max:
            raise ObjectTooLargeError()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return self.url(key)

    def get(self, key: str) -> bytes:
        path = self._path(key)
        try:
            return path.read_bytes()
        except FileNotFoundError as exc:
            raise ObjectStorageUnavailableError("object not found") from exc

    def head(self, key: str) -> bool:
        return self._path(key).is_file()

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def url(self, key: str) -> str:
        return f"file://{self._path(key).resolve()}"

    def signed_url(self, key: str, expires_in: int | None = None) -> str:
        # No signing for local files; return the file URL for dev convenience.
        return self.url(key)

    def ping(self) -> bool:
        return self.root.is_dir() and os.access(self.root, os.W_OK)

    def close(self) -> None:
        return None


class S3Storage:
    """Hardened S3-compatible storage.

    Constructed with an already-built client + resolved settings so it is
    unit-testable with an injected fake client and never imports ``boto3`` at test
    time. Every key is validated; every driver error is sanitized.
    """

    def __init__(
        self,
        client: Any,
        *,
        bucket: str,
        max_object_bytes: int,
        signed_url_ttl_seconds: int,
    ) -> None:
        self._client = client
        self._bucket = bucket
        self._max = max_object_bytes
        self._ttl = signed_url_ttl_seconds

    def put(self, key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
        validate_object_key(key)
        if len(data) > self._max:
            raise ObjectTooLargeError()
        # Dependency span carries only the bounded operation + outcome — never the
        # object key, bucket or endpoint.
        with start_span(
            STORAGE_UPLOAD,
            kind="client",
            attributes={"component": "storage", "dependency": "s3", "operation": "put"},
        ) as span:
            try:
                # No ACL argument: the object inherits the bucket's private default.
                self._client.put_object(
                    Bucket=self._bucket, Key=key, Body=data, ContentType=content_type
                )
            except Exception as exc:
                raise ObjectStorageUnavailableError() from exc
            span.set_attribute("outcome", "success")
        return self.url(key)

    def get(self, key: str) -> bytes:
        validate_object_key(key)
        try:
            response = self._client.get_object(Bucket=self._bucket, Key=key)
            return response["Body"].read()
        except Exception as exc:
            raise ObjectStorageUnavailableError() from exc

    def head(self, key: str) -> bool:
        validate_object_key(key)
        try:
            self._client.head_object(Bucket=self._bucket, Key=key)
            return True
        except Exception:
            return False

    def delete(self, key: str) -> None:
        validate_object_key(key)
        try:
            self._client.delete_object(Bucket=self._bucket, Key=key)
        except Exception as exc:
            raise ObjectStorageUnavailableError() from exc

    def url(self, key: str) -> str:
        validate_object_key(key)
        return f"s3://{self._bucket}/{key}"

    def signed_url(self, key: str, expires_in: int | None = None) -> str:
        validate_object_key(key)
        # Bound the expiry to the configured ceiling regardless of caller input.
        ttl = self._ttl if expires_in is None else min(expires_in, self._ttl)
        if ttl <= 0:
            raise InvalidObjectKeyError("signed url expiry must be positive")
        with start_span(
            STORAGE_SIGN_URL,
            kind="client",
            attributes={"component": "storage", "dependency": "s3", "operation": "sign_url"},
        ) as span:
            try:
                url = self._client.generate_presigned_url(
                    "get_object",
                    Params={"Bucket": self._bucket, "Key": key},
                    ExpiresIn=ttl,
                )
            except Exception as exc:
                raise ObjectStorageUnavailableError() from exc
            span.set_attribute("outcome", "success")
            return url

    def ping(self) -> bool:
        try:
            self._client.head_bucket(Bucket=self._bucket)
            return True
        except Exception:
            return False

    def close(self) -> None:
        try:
            close = getattr(self._client, "close", None)
            if close is not None:
                close()
        except Exception:  # pragma: no cover - best-effort shutdown
            pass


def build_s3_client(settings: Settings) -> Any:  # pragma: no cover - full mode only
    """Build a bounded, timeout-guarded boto3 S3 client. Lazy import."""
    import boto3
    from botocore.config import Config

    config = Config(
        connect_timeout=settings.s3_operation_timeout_seconds,
        read_timeout=settings.s3_operation_timeout_seconds,
        retries={"max_attempts": settings.s3_max_retries, "mode": "standard"},
    )
    kwargs: dict[str, Any] = {"config": config, "use_ssl": settings.s3_use_ssl}
    if settings.s3_endpoint_url:
        kwargs["endpoint_url"] = settings.s3_endpoint_url
    if settings.s3_region:
        kwargs["region_name"] = settings.s3_region
    if settings.s3_access_key_id and settings.s3_secret_access_key:
        kwargs["aws_access_key_id"] = settings.s3_access_key_id
        kwargs["aws_secret_access_key"] = settings.s3_secret_access_key
    return boto3.client("s3", **kwargs)


def build_storage(settings: Settings | None = None) -> Storage:
    settings = settings or get_settings()
    if settings.storage_backend == "s3":  # pragma: no cover - full mode only
        client = build_s3_client(settings)
        return S3Storage(
            client,
            bucket=settings.s3_bucket,  # type: ignore[arg-type]  # validated in Settings
            max_object_bytes=settings.s3_max_object_bytes,
            signed_url_ttl_seconds=settings.s3_signed_url_ttl_seconds,
        )
    return LocalStorage(
        os.path.abspath(settings.local_storage_dir),
        max_object_bytes=settings.s3_max_object_bytes,
    )


storage: Storage = build_storage()
