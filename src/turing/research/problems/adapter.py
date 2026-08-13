"""The adapter seam — how a problem family plugs into a generic core.

The brief's most reusable decision is that Turing's core "takes a goal plus a
script returning a number and is indifferent to which": *"Kaggle is one
translator behind that interface, not the interface itself."* This module is
that interface.

A :class:`ProblemAdapter` does exactly two things for one problem family:

1. **Enumerate** its problems as :class:`~turing.research.contracts.Problem`
   records, each carrying a frozen verifier.
2. **Materialise** a fresh workspace for one attempt at one of them.

Everything family-specific — that speedup problems are timed subprocesses,
that Kaggle problems have leaderboards, that a future medical-ML family has
whatever it has — lives behind those two methods. The solver and the round
loop never import a concrete adapter, and adding a family never edits them.

**Why ``load`` is synchronous and ``materialise_workspace`` is not.** ``load``
enumerates the corpus: it must be fast, deterministic and side-effect-free,
because the eval-set hash is computed from its output and a corpus that varies
by when it was loaded makes rounds incomparable. Per-attempt I/O — copying a
repo, fetching competition data — is the expensive, ordinary-async part and
belongs in ``materialise_workspace``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Protocol, runtime_checkable

import structlog

from turing.research.contracts import ContractViolationError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from turing.research.contracts import Problem, ProblemType

logger = structlog.get_logger("turing.research.problems.adapter")

__all__ = [
    "AdapterRegistry",
    "ProblemAdapter",
    "WorkspaceMaterialisationError",
    "bind_eval_set_hash",
    "fingerprint_corpus",
]


class WorkspaceMaterialisationError(ContractViolationError):
    """A fresh workspace could not be prepared for an attempt.

    Raised rather than reported: an attempt with no workspace has nothing to
    grade, so there is no result to return. The loop turns this into an
    escalation (``EscalationReason.HARNESS_FAILURE``), which is the correct
    outcome — the agent cannot fix a missing source repo, and it may not quit.
    """


@runtime_checkable
class ProblemAdapter(Protocol):
    """Loads one problem family and materialises workspaces for it."""

    @property
    def problem_type(self) -> ProblemType:
        """The family this adapter is responsible for."""
        ...

    def load(self) -> tuple[Problem, ...]:
        """Enumerate this family's problems, each with its frozen verifier.

        Must be deterministic: the same adapter configuration yields the same
        problems in the same order, or ``eval_set_hash`` moves for reasons
        unrelated to the corpus and every cross-round comparison is void.
        """
        ...

    async def materialise_workspace(self, problem: Problem, destination: Path) -> Path:
        """Prepare a fresh working directory for one attempt.

        ``destination`` must not already exist as a non-empty directory —
        attempts are independent by construction, and a workspace inherited
        from a previous attempt silently turns "solved it" into "it was already
        solved".

        Returns:
            The path the agent should treat as its cwd. Usually
            ``destination``, but an adapter may nest (a Kaggle adapter might
            place data beside the code).
        """
        ...


@dataclass(slots=True)
class AdapterRegistry:
    """Routes a problem type to the adapter that owns it.

    Deliberately holds *instances*, not classes: adapters need roots, runners
    and credentials that only the caller can supply, and a registry that
    constructs them would have to know what each family needs — which is
    exactly the coupling the seam exists to prevent.
    """

    _adapters: dict[ProblemType, ProblemAdapter] = field(default_factory=dict)

    def register(self, adapter: ProblemAdapter) -> None:
        problem_type = adapter.problem_type
        if problem_type in self._adapters:
            raise ContractViolationError(
                f"an adapter for {problem_type.value!r} is already registered; two "
                "adapters for one family would make the corpus depend on ordering"
            )
        self._adapters[problem_type] = adapter
        logger.debug("research.adapter.registered", problem_type=problem_type.value)

    def get(self, problem_type: ProblemType) -> ProblemAdapter:
        try:
            return self._adapters[problem_type]
        except KeyError:
            raise ContractViolationError(
                f"no adapter registered for {problem_type.value!r}"
            ) from None

    def types(self) -> tuple[ProblemType, ...]:
        return tuple(self._adapters)

    def load_all(self) -> tuple[Problem, ...]:
        """Every registered family's problems, ordered by family then adapter order.

        Sorting by the family's enum value keeps the corpus stable regardless
        of registration order, which the eval-set hash depends on.
        """
        problems: list[Problem] = []
        for problem_type in sorted(self._adapters, key=lambda t: t.value):
            problems.extend(self._adapters[problem_type].load())
        return tuple(problems)


def fingerprint_corpus(problems: Sequence[Problem], *, extra: Sequence[str] = ()) -> str:
    """Hash a corpus into the ``eval_set_hash`` a round record must carry.

    Rounds measured on different eval sets may not be compared at all, so the
    hash has to move whenever the corpus does — and *only* then. It covers each
    problem's id, family, split, goal and verifier identity, in the order the
    adapters produced them, plus whatever family-specific material the caller
    passes in ``extra`` (the speedup adapter passes its significance
    multiplier and each spec's
    :meth:`~turing.research.problems.spec.SpeedupProblemSpec.fingerprint_material`,
    so a changed baseline, a loosened tolerance, a different source repo, or a
    widened significance band changes the hash).

    SHA-256 over a canonical string rather than :func:`hash`, because
    :func:`hash` is salted per process and would produce a different eval-set
    identity every time the loop restarted.
    """
    if not problems:
        raise ContractViolationError("cannot fingerprint an empty corpus")
    digest = hashlib.sha256()
    for problem in problems:
        digest.update(
            "|".join(
                (
                    problem.id,
                    problem.problem_type.value,
                    problem.split.value,
                    problem.goal,
                    problem.verifier.verifier_id,
                    problem.verifier.score_scale,
                )
            ).encode()
        )
        digest.update(b"\x00")
    for item in extra:
        digest.update(item.encode())
        digest.update(b"\x00")
    return digest.hexdigest()


def bind_eval_set_hash(problems: Sequence[Problem], promised: str = "") -> str:
    """Derive the eval-set hash from the corpus actually in hand.

    A caller-promised string is verified, never trusted: drop a problem and
    reuse the old hash and the loop would report a comparable delta on a
    changed eval set — the failure the brief calls fiction. An empty promise
    means derive only.
    """
    derived = fingerprint_corpus(problems)
    if promised and promised != derived:
        raise ContractViolationError(
            f"eval_set_hash {promised!r} does not match the corpus fingerprint "
            f"{derived!r}; a promised hash that the corpus does not produce is "
            "how silent eval drift fakes a curve"
        )
    return derived
