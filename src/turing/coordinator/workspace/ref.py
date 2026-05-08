"""WorkspaceRef — ``workspace://<task_id>/<key>`` URI for big-blob payloads."""

from __future__ import annotations

import re
from dataclasses import dataclass

_SCHEME = "workspace://"
_SAFE_TASK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


class InvalidWorkspaceRefError(ValueError):
    """Raised on malformed or traversal-style refs."""


@dataclass(frozen=True)
class WorkspaceRef:
    task_id: str
    key: str

    def __post_init__(self) -> None:
        if not _SAFE_TASK_RE.match(self.task_id):
            raise InvalidWorkspaceRefError(
                f"task_id must match {_SAFE_TASK_RE.pattern!r}, got {self.task_id!r}"
            )
        if not _SAFE_KEY_RE.match(self.key) or ".." in self.key:
            raise InvalidWorkspaceRefError(
                f"key must match {_SAFE_KEY_RE.pattern!r} and contain no '..', "
                f"got {self.key!r}"
            )

    def __str__(self) -> str:
        return f"{_SCHEME}{self.task_id}/{self.key}"

    @classmethod
    def parse(cls, raw: str) -> "WorkspaceRef":
        if not raw.startswith(_SCHEME):
            raise InvalidWorkspaceRefError(
                f"missing {_SCHEME!r} scheme: {raw!r}"
            )
        rest = raw[len(_SCHEME) :]
        if "/" not in rest:
            raise InvalidWorkspaceRefError(f"missing '/<key>' in {raw!r}")
        task_id, _, key = rest.partition("/")
        if not task_id or not key:
            raise InvalidWorkspaceRefError(f"empty task_id or key in {raw!r}")
        return cls(task_id=task_id, key=key)

    @staticmethod
    def is_workspace_uri(raw: str) -> bool:
        if not raw.startswith(_SCHEME):
            return False
        try:
            WorkspaceRef.parse(raw)
        except InvalidWorkspaceRefError:
            return False
        return True
