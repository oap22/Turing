"""Structural invariants, asserted against the parsed source.

Behavioural tests prove the solver does not quit *today*. These prove it
cannot be made to quit by an ordinary edit: they check where the terminal
states are written, not just what happens when they are. A future change that
adds a second write site for ``ABANDONED`` fails the suite rather than
depending on a reviewer noticing.

Loop 2 will edit this repo's scaffold autonomously and unattended. Invariants
that live only in review comments do not survive that.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from turing.research import solver as solver_pkg
from turing.research.contracts import FrozenVerifierError, Problem
from turing.research.solver import ProposalContext, Solver, SolverPolicy
from turing.research.solver import solver as solver_module

from .conftest import FakeClock, LadderBackend, ScriptedChannel

if TYPE_CHECKING:
    from turing.research.contracts import Attempt
    from turing.research.solver import InMemoryCheckpointStore, WorkspaceManager

SOLVER_SOURCE = Path(solver_module.__file__)


def _functions_referencing(member: str) -> set[str]:
    """Names of the functions in ``solver.py`` that mention ``AttemptState.<member>``."""
    tree = ast.parse(SOLVER_SOURCE.read_text(encoding="utf-8"))
    owners: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        for sub in ast.walk(node):
            if (
                isinstance(sub, ast.Attribute)
                and sub.attr == member
                and isinstance(sub.value, ast.Name)
                and sub.value.id == "AttemptState"
            ):
                owners.add(node.name)
    return owners


class TestTerminalStatesHaveExactlyOneWriteSite:
    @pytest.mark.parametrize(
        ("member", "owner"),
        [
            ("ABANDONED", "_abandon"),
            ("FAILED_WITHIN_CAP", "_fail_within_cap"),
            ("PASSED", "_mark_passed"),
        ],
    )
    def test_only_one_method_mentions_each_terminal_state(self, member: str, owner: str) -> None:
        owners = _functions_referencing(member)
        assert owners, f"AttemptState.{member} is never referenced — has it been renamed?"
        assert owners == {owner}, (
            f"AttemptState.{member} is written outside {owner}(): {sorted(owners)}. "
            "Each terminal state has one guarded entrance on purpose."
        )

    def test_abandonment_is_gated_on_an_operator_verdict(self) -> None:
        """The guard, read out of the source rather than assumed."""
        tree = ast.parse(SOLVER_SOURCE.read_text(encoding="utf-8"))
        bodies = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_abandon"
        ]
        assert len(bodies) == 1
        source = ast.unparse(bodies[0])
        assert "EscalationVerdict.ABANDON" in source
        assert "raise EscalationProtocolError" in source

    def test_the_cap_failure_is_gated_on_a_tripped_cap(self) -> None:
        tree = ast.parse(SOLVER_SOURCE.read_text(encoding="utf-8"))
        bodies = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "_fail_within_cap"
        ]
        assert len(bodies) == 1
        source = ast.unparse(bodies[0])
        assert "cap_exhausted" in source
        assert "raise SolverError" in source

    def test_the_loop_never_reaches_around_a_frozen_record(self) -> None:
        """``object.__setattr__`` would defeat every immutability guarantee here."""
        assert "object.__setattr__" not in SOLVER_SOURCE.read_text(encoding="utf-8")

    def test_solver_does_not_pass_cap_or_consumed_through_evolve(self) -> None:
        """The attempt machine, not evolve kwargs, is how budget moves."""
        tree = ast.parse(SOLVER_SOURCE.read_text(encoding="utf-8"))
        offenders: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "evolve"):
                continue
            for keyword in node.keywords:
                if keyword.arg in {"cap", "consumed"}:
                    offenders.append(keyword.arg)
        assert not offenders, (
            f"solver.py still passes {offenders} through evolve; "
            "use apply_cap_extension / record_consumption"
        )


class TestTheVerifierIsOutOfReach:
    def test_the_proposal_context_is_not_typed_to_carry_one(self) -> None:
        forbidden = {"Verifier", "Problem"}
        for name, annotation in ProposalContext.__annotations__.items():
            tokens = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", str(annotation)))
            assert not tokens & forbidden, f"{name} would carry the frozen bar into a model call"

    async def test_a_full_run_leaves_the_verifier_untouched(
        self,
        attempt: Attempt,
        problem: Problem,
        store: InMemoryCheckpointStore,
        manager: WorkspaceManager,
        clock: FakeClock,
    ) -> None:
        before = problem.verifier
        solver = Solver(
            backend=LadderBackend(),
            store=store,
            workspaces=manager,
            escalations=ScriptedChannel(),
            policy=SolverPolicy(target_score=2.0),
            clock=clock,
        )
        await solver.run(attempt, problem)

        assert problem.verifier is before
        with pytest.raises(Exception):  # noqa: B017 - frozen dataclasses vary by Python build
            problem.verifier.description = "an easier bar"  # type: ignore[misc]

    def test_binding_a_mutable_verifier_is_impossible(self, template_dir: Path) -> None:
        """Belt and braces: contracts refuse it, and this module relies on that."""
        from dataclasses import dataclass

        from turing.research.contracts import ProblemType, Split, Verifier

        with pytest.raises(FrozenVerifierError):

            @dataclass(frozen=True)
            class Writable(Verifier):
                def __setattr__(self, name: str, value: object) -> None:
                    object.__setattr__(self, name, value)

                async def verify(self, workspace: Path):  # type: ignore[no-untyped-def]
                    raise NotImplementedError

        assert Problem is not None
        assert ProblemType is not None
        assert Split is not None


class TestThePublicSurface:
    def test_the_package_exports_what_it_documents(self) -> None:
        for name in solver_pkg.__all__:
            assert hasattr(solver_pkg, name), f"{name} is exported but missing"

    def test_the_solver_has_no_self_termination_affordance(self) -> None:
        public = {name for name in dir(Solver) if not name.startswith("_")}
        assert public == {
            "run",
            "resume",
            "policy",
            "propose_and_apply",
            "backend_resume_token",
        }
        assert not public & {"quit", "abort", "abandon", "fail", "stop", "give_up"}

    def test_nothing_here_implements_loop_two(self) -> None:
        """Loop 1 only: no self-editing, no cheat detection, no rollback.

        A cheap tripwire, but the sequencing it protects is load-bearing — a
        cheat detector built after the first self-editing round cannot tell
        you which earlier rounds were real.
        """
        package_root = Path(solver_pkg.__file__).parent
        for path in package_root.glob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                # Docstrings may *name* the seam — that is how it stays
                # findable. Calling it is what would make this loop 2.
                assert not (
                    isinstance(node, ast.Attribute) and node.attr == "self_edit_visible_scores"
                ), f"{path.name} calls the loop-2 seam"
