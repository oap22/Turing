"""Per-attempt working directories and the writes that are allowed into them.

Each attempt gets a fresh directory under a configurable root — by default
``~/turing-workspace/<problem-id>/<attempt-id>/`` — seeded from the problem's
``workspace_template``. Copying rather than working in place is what keeps
attempts independent and re-runnable at a new seed, and what keeps the source
repos the speedup problems came from unmodified.

**What this module does and does not enforce.** It resolves every proposed
path against the real workspace root, so ``..`` segments, absolute paths and
symlinks pointing out of the tree are refused. That is a strong guarantee
about writes *the solver makes*. It is not a guarantee about the agent's work
as a whole: the agent's job is authoring Python and running it, and a Python
process writes anywhere its OS user can write regardless of cwd. The enforced
boundary is a separate ``turing`` **macOS user account** that owns this root
and has no reach into the operator's vault, SSH keys, MCP credentials or
Claude credentials. Creating that user is out of scope here by instruction and
is documented — with its setup script — by the docs module. A directory is a
convention; a user account is a boundary the OS enforces.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from turing.research.solver.errors import (
    WorkspaceEscapeError,
    WorkspaceTemplateError,
)

if TYPE_CHECKING:
    from turing.research.contracts import Problem
    from turing.research.solver.models import Proposal

__all__ = ["Workspace", "WorkspaceManager"]

logger = structlog.get_logger("turing.research.solver.workspace")


@dataclass(frozen=True, slots=True)
class Workspace:
    """A prepared working directory, and the only writable area of an attempt."""

    path: Path

    @property
    def real_path(self) -> Path:
        """The root with every symlink resolved. All containment is judged here."""
        return Path(os.path.realpath(self.path))

    def resolve(self, relative: str) -> Path:
        """Resolve a proposed relative path, or refuse it.

        Refuses absolute paths, empty paths, and anything that resolves — after
        following symlinks on every existing component — outside the workspace
        root. Resolution is done on the whole candidate rather than on its
        parent so that a template symlink such as ``data -> /etc`` cannot be
        used as a tunnel.
        """
        cleaned = relative.strip()
        if not cleaned:
            raise WorkspaceEscapeError("empty relative path")
        candidate = Path(cleaned)
        if candidate.is_absolute() or cleaned.startswith(("/", "~")):
            raise WorkspaceEscapeError(
                f"{relative!r} is absolute; the workspace is the only writable area"
            )
        if "\x00" in cleaned:
            raise WorkspaceEscapeError("relative path contains a NUL byte")
        root = self.real_path
        resolved = Path(os.path.realpath(self.path / candidate))
        if resolved != root and root not in resolved.parents:
            raise WorkspaceEscapeError(
                f"{relative!r} resolves to {resolved} which is outside {root}"
            )
        return resolved

    async def write_text(self, relative: str, content: str) -> Path:
        """Write one file inside the workspace, atomically.

        Atomic per file via a temp file plus ``os.replace``: a crash leaves
        either the old contents or the new ones, never a half-written source
        file that the next verification would score as a syntax error.
        Atomicity *across* files is not attempted — that is what makes
        proposals full-content and idempotent, so re-applying repairs a
        partially applied set.
        """
        target = self.resolve(relative)
        await asyncio.to_thread(_write_atomic, target, content)
        return target

    async def apply(self, proposal: Proposal) -> tuple[str, ...]:
        """Apply every edit in a proposal. Idempotent: safe to repeat verbatim."""
        written: list[str] = []
        for edit in proposal.edits:
            await self.write_text(edit.relative_path, edit.content)
            written.append(edit.relative_path)
        if written:
            logger.debug(
                "research.solver.workspace.applied",
                workspace=str(self.path),
                proposal_id=proposal.proposal_id,
                digest=proposal.digest(),
                files=len(written),
            )
        return tuple(written)


def _write_atomic(target: Path, content: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.parent / f".{target.name}.{uuid.uuid4().hex}.tmp"
    try:
        tmp.write_text(content, encoding="utf-8")
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


class WorkspaceManager:
    """Creates and re-attaches attempt workspaces under one root."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root).expanduser()

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, problem_id: str, attempt_id: str) -> Path:
        """Where an attempt's workspace lives.

        Exposed so whoever constructs the :class:`~turing.research.contracts.Attempt`
        derives the same path the solver will insist on — the round
        orchestrator sets ``workspace_path``, and a disagreement would surface
        as a refused workspace rather than a silent second directory.
        """
        return self._root / problem_id / attempt_id

    async def prepare(self, problem: Problem, workspace_path: Path) -> Workspace:
        """Materialise a workspace, or re-attach to an existing one.

        **Re-attaches without copying when the directory already has contents.**
        That branch is the resume path and it is load-bearing: re-seeding from
        the template would silently discard every change made before the
        interruption, turning "an interruption costs the remainder of an
        attempt" back into "an interruption costs the attempt".
        """
        target = Path(workspace_path).expanduser()
        self._assert_contained(target)
        if await asyncio.to_thread(_has_contents, target):
            logger.info(
                "research.solver.workspace.reattached",
                problem_id=problem.id,
                workspace=str(target),
            )
            return Workspace(path=target)

        template = Path(problem.workspace_template).expanduser()
        if not await asyncio.to_thread(template.is_dir):
            raise WorkspaceTemplateError(
                f"workspace template {template} for problem {problem.id!r} is not a directory"
            )
        await asyncio.to_thread(_copy_template, template, target, problem.workspace_excludes)
        logger.info(
            "research.solver.workspace.prepared",
            problem_id=problem.id,
            workspace=str(target),
            template=str(template),
        )
        return Workspace(path=target)

    def attach(self, workspace_path: Path) -> Workspace:
        """Wrap an existing directory without touching it."""
        target = Path(workspace_path).expanduser()
        self._assert_contained(target)
        return Workspace(path=target)

    def _assert_contained(self, target: Path) -> None:
        """Refuse a workspace outside the configured root.

        The root is the directory the ``turing`` user owns. An attempt whose
        workspace sat outside it would be unprotected by the OS-level boundary
        even though every in-process check still passed.
        """
        root = Path(os.path.realpath(self._root)) if self._root.exists() else self._root.absolute()
        absolute = target if target.is_absolute() else target.absolute()
        resolved = Path(os.path.realpath(absolute)) if absolute.exists() else absolute
        if resolved != root and root not in resolved.parents:
            raise WorkspaceEscapeError(
                f"workspace {target} is outside the workspace root {self._root}"
            )


def _has_contents(path: Path) -> bool:
    return path.is_dir() and any(path.iterdir())


def _copy_template(template: Path, target: Path, excludes: tuple[str, ...] = ()) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    # ``symlinks=True`` keeps a link as a link rather than copying whatever it
    # points at into the workspace. Combined with ``Workspace.resolve``, a
    # template containing ``data -> /etc`` yields a dead end rather than a
    # tunnel; copying the target instead would import the outside data.
    # ``workspace_excludes`` has to be applied here, not only in the adapter:
    # this is the copy the solver actually runs.
    shutil.copytree(
        template,
        target,
        ignore=shutil.ignore_patterns(*excludes) if excludes else None,
        symlinks=True,
        ignore_dangling_symlinks=True,
        dirs_exist_ok=True,
    )
