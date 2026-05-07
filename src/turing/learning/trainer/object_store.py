"""ObjectStore protocol + in-memory fake.

The real facility uploads to S3-compatible storage; the in-memory implementation
keeps unit tests free of network deps. Both expose the same minimal API the
publisher uses: put-by-key, get-by-key, list-keys.
"""

from __future__ import annotations

from typing import Iterable, Protocol


class ObjectStore(Protocol):
    def put(self, key: str, blob: bytes) -> None: ...
    def get(self, key: str) -> bytes: ...
    def keys(self) -> Iterable[str]: ...


class InMemoryObjectStore:
    def __init__(self) -> None:
        self._blobs: dict[str, bytes] = {}

    def put(self, key: str, blob: bytes) -> None:
        self._blobs[key] = blob

    def get(self, key: str) -> bytes:
        return self._blobs[key]

    def keys(self) -> Iterable[str]:
        return list(self._blobs.keys())
