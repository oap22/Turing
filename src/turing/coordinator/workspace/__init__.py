"""Workspace addressing: NATS-backed object store with auto-promote + 24h GC."""

from __future__ import annotations

from turing.coordinator.workspace.auto_promote import auto_promote
from turing.coordinator.workspace.client import (
    InMemoryWorkspaceClient,
    WorkspaceClient,
)
from turing.coordinator.workspace.gc import WorkspaceGC
from turing.coordinator.workspace.ref import (
    InvalidWorkspaceRefError,
    WorkspaceRef,
)

__all__ = [
    "InMemoryWorkspaceClient",
    "InvalidWorkspaceRefError",
    "WorkspaceClient",
    "WorkspaceGC",
    "WorkspaceRef",
    "auto_promote",
]
