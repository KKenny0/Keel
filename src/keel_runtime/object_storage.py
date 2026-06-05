"""Object storage adapters for job snapshots."""

from __future__ import annotations

import os
from collections.abc import Iterable
from io import BytesIO
from typing import Protocol

from keel_runtime.errors import StorageSyncError


class ObjectStorage(Protocol):
    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        """Write bytes to a storage key."""

    def get_bytes(self, key: str) -> bytes:
        """Read bytes from a storage key."""

    def list_keys(self, prefix: str) -> list[str]:
        """List full storage keys under a prefix."""


class InMemoryObjectStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        self.objects[key] = data

    def get_bytes(self, key: str) -> bytes:
        try:
            return self.objects[key]
        except KeyError as exc:
            raise FileNotFoundError(key) from exc

    def list_keys(self, prefix: str) -> list[str]:
        return sorted(key for key in self.objects if key.startswith(prefix))


class S3ObjectStorage:
    def __init__(
        self,
        bucket: str,
        *,
        prefix: str = "",
        endpoint_url: str | None = None,
        region_name: str | None = None,
        client: object | None = None,
    ) -> None:
        if not bucket:
            raise ValueError("S3 bucket cannot be empty")
        self.bucket = bucket
        self.prefix = prefix.strip("/")
        self.client = client or self._create_client(endpoint_url, region_name)

    @classmethod
    def from_env(cls) -> S3ObjectStorage:
        return cls(
            bucket=os.environ["KEEL_S3_BUCKET"],
            prefix=os.getenv("KEEL_S3_PREFIX", ""),
            endpoint_url=os.getenv("KEEL_S3_ENDPOINT_URL"),
            region_name=os.getenv("AWS_REGION") or os.getenv("AWS_DEFAULT_REGION"),
        )

    def put_bytes(self, key: str, data: bytes, content_type: str | None = None) -> None:
        kwargs = {
            "Bucket": self.bucket,
            "Key": self._key(key),
            "Body": data,
        }
        if content_type is not None:
            kwargs["ContentType"] = content_type
        self.client.put_object(**kwargs)

    def get_bytes(self, key: str) -> bytes:
        response = self.client.get_object(Bucket=self.bucket, Key=self._key(key))
        body = response["Body"]
        if isinstance(body, bytes):
            return body
        if isinstance(body, BytesIO):
            return body.getvalue()
        return body.read()

    def list_keys(self, prefix: str) -> list[str]:
        full_prefix = self._key(prefix)
        keys: list[str] = []
        if hasattr(self.client, "get_paginator"):
            paginator = self.client.get_paginator("list_objects_v2")
            pages: Iterable[dict] = paginator.paginate(Bucket=self.bucket, Prefix=full_prefix)
        else:
            pages = [self.client.list_objects_v2(Bucket=self.bucket, Prefix=full_prefix)]

        for page in pages:
            for item in page.get("Contents", []):
                key = item["Key"]
                keys.append(self._strip_prefix(key))
        return sorted(keys)

    def _key(self, key: str) -> str:
        clean_key = key.strip("/")
        if not self.prefix:
            return clean_key
        return f"{self.prefix}/{clean_key}"

    def _strip_prefix(self, key: str) -> str:
        if not self.prefix:
            return key
        prefix = f"{self.prefix}/"
        return key.removeprefix(prefix)

    @staticmethod
    def _create_client(endpoint_url: str | None, region_name: str | None) -> object:
        try:
            import boto3
        except ImportError as exc:
            raise StorageSyncError("boto3 is required for S3/MinIO storage") from exc
        return boto3.client("s3", endpoint_url=endpoint_url, region_name=region_name)
