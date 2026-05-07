"""Adapter registry — verify, stage, and promote LoRA adapters."""

from turing.coordinator.adapters.manifest import AdapterManifest
from turing.coordinator.adapters.registry import (
    AdapterRegistry,
    AdapterState,
    BaseModelMismatchError,
    HashMismatchError,
    UnknownAdapterError,
)

__all__ = [
    "AdapterManifest",
    "AdapterRegistry",
    "AdapterState",
    "BaseModelMismatchError",
    "HashMismatchError",
    "UnknownAdapterError",
]
