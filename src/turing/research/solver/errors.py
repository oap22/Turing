"""Failures the solver itself can raise.

Deliberately *not* subclasses of
:class:`turing.research.contracts.ContractViolationError`. That family is
documented as "programming errors, not runtime conditions", and everything
here is a runtime condition an unattended solver will genuinely meet: a model
proposing a path outside its workspace, a missing template, a corrupt
checkpoint. They are separate so a caller can tell "the design was violated in
code" apart from "the run went wrong tonight".
"""

from __future__ import annotations

__all__ = [
    "CheckpointError",
    "ProposalError",
    "SolverError",
    "WorkspaceEscapeError",
    "WorkspaceTemplateError",
]


class SolverError(Exception):
    """Base for every failure the within-project solver raises."""


class WorkspaceEscapeError(SolverError):
    """A proposed write resolved outside the attempt's workspace.

    The workspace is the agent's *only* declared writable area. This check is
    the in-process half of that guarantee and it is not the strong half: a
    Python program the agent authors and runs writes anywhere its OS user can
    write, regardless of cwd. The enforced boundary is a separate ``turing``
    macOS user account, documented by the docs module. This error catches the
    realistic accident — a relative path with ``..`` in it, or a template
    symlink pointing out of the tree — and makes a deliberate attempt loud.
    """


class WorkspaceTemplateError(SolverError):
    """A problem's ``workspace_template`` could not be materialised."""


class ProposalError(SolverError):
    """A backend returned a proposal the solver cannot act on."""


class CheckpointError(SolverError):
    """A checkpoint could not be written, read, or was written out of order.

    Raised rather than swallowed: checkpoints are the only thing standing
    between a closed subscription window and a lost attempt, so a silent
    checkpoint failure would be discovered as an unexplained restart hours
    later.
    """
