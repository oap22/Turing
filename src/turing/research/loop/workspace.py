"""Fresh working directory per attempt, seeded from the problem's template.

Copying rather than working in place is what makes attempts independent and
re-runnable at a new seed, and what keeps the source repos the speedup problems
came from unmodified. The noise floor depends on it directly: three seeds of
"the same config" are only the same config if each starts from a byte-identical
workspace. ``problem.workspace_excludes`` is applied at copy time — the catalog,
the brief and the loophole rulings are not part of that byte-identical seed.

This provider is the in-process convenience implementation. The brief's real
isolation boundary is a separate ``turing`` macOS user owning
``~/turing-workspace/<project-id>/`` — a directory is a convention, a user
account is a boundary the OS enforces. That belongs to the environment setup,
not to this class; :class:`CopyTreeWorkspaceProvider` only guarantees that the
directory is fresh.
"""

from __future__ import annotations

import asyncio
import shutil
from typing import TYPE_CHECKING

import structlog

from turing.research.contracts import ContractViolationError

if TYPE_CHECKING:
    from pathlib import Path

    from turing.research.contracts import Problem

logger = structlog.get_logger(__name__)

__all__ = ["CopyTreeWorkspaceProvider"]


class CopyTreeWorkspaceProvider:
    """Materialises ``<root>/<attempt_id>/`` from ``problem.workspace_template``.

    Implements :class:`~turing.research.loop.protocols.WorkspaceProvider`.
    """

    def __init__(self, root: Path, *, clean_existing: bool = True) -> None:
        self._root = root
        self._clean_existing = clean_existing

    @property
    def root(self) -> Path:
        return self._root

    async def materialise(self, problem: Problem, *, attempt_id: str) -> Path:
        target = self._root / attempt_id
        template = problem.workspace_template
        await asyncio.to_thread(self._copy, template, target, problem.workspace_excludes)
        logger.info(
            "research.workspace.materialised",
            problem_id=problem.id,
            attempt_id=attempt_id,
            template=str(template),
            workspace=str(target),
        )
        return target

    def _copy(self, template: Path, target: Path, excludes: tuple[str, ...] = ()) -> None:
        if target.exists():
            if not self._clean_existing:
                raise ContractViolationError(
                    f"{target} already exists; an attempt must start from a fresh workspace "
                    "or its seed is not the only thing that changed"
                )
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        if template.is_dir():
            shutil.copytree(
                template,
                target,
                ignore=shutil.ignore_patterns(*excludes) if excludes else None,
                symlinks=True,
                ignore_dangling_symlinks=True,
            )
        else:
            # A template that is a single file (or missing entirely, as in the
            # bench cycle) still yields a usable empty workspace.
            target.mkdir(parents=True, exist_ok=True)
            if template.is_file():
                shutil.copy2(template, target / template.name)
