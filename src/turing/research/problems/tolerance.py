"""Tolerance modes — how "the answer did not change" is decided.

A speedup problem is graded on two things: it got faster, and it still computes
the same answer. The second half is where speedup benchmarks quietly break,
because *the right tolerance is a property of the intended fix*, and getting it
wrong fails in whichever direction is hardest to notice:

* **Too tight** and every legitimate solution fails. Vectorising
  ``VaultIndex.query`` shifts the returned scores by ~1e-7 while leaving the
  top-k path sequence bit-identical across 500 queries — measured, 2026-08-12.
  A gate pinning exact scores rejects the fix the problem exists to elicit, and
  the problem reads as "nobody could solve it".
* **Too loose** and a wrong answer passes. If the embedding problem were graded
  on "vectors are roughly similar", returning zeros would score 9.5×.

So tolerance is declared per problem, with the reason recorded next to it, and
the modes are named after what they actually check rather than after a number.

The five modes and where each came from:

``EXACT``
    Bit-identical. Legitimate only where the intended fix provably cannot move
    the result — problem 1 (16 redundant embed calls collapsed to 1) and
    problem 3 (memoising a deterministic token→vector map). Where it applies it
    is the strongest gate available and should be used.
``RELATIVE``
    ``math.isclose`` with a declared ``rtol``/``atol``. The general fallback,
    for pipelines that are not reproducible bit-for-bit across processes.
``TOP_K_SEQUENCE``
    An ordered label sequence compared exactly, with parallel scores compared
    within a tolerance. Problem 2's gate: the *ranking* is the answer, the
    scores are floating-point residue. Comparing the labels as a set rather
    than a sequence would let a solution reorder the results and pass.
``COSINE``
    Per-vector cosine similarity above a threshold. Problem 5's gate: changing
    the tokeniser's padding length shifts embedding vectors, so exact equality
    rejects the intended fix, but direction is preserved and is what the
    vectors are used for.
``TEST_OUTCOME_SET``
    Per-file, per-test pass/fail outcomes compared against a pinned baseline,
    with one file required to go fully green. Problem 4's gate, and the mode
    that exists because the obvious gate is wrong: the correct fix *changes*
    the pass/fail set — three currently-failing tests start passing — so
    "same outcomes as baseline" fails the intended fix. The rule is "the
    pinned tests in the target file are still present and all pass, and no
    other file's outcomes moved". The identities matter: a count floor alone
    accepts 21 empty tests in place of the 21 real ones.

Comparisons operate on JSON-decoded structures. The candidate artifact is
written by a harness dumper into the workspace; the reference is read from a
root outside the agent's write surface. Keeping the reference outside is not
decoration — a pinned answer the graded party can edit is not a pinned answer.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from turing.research.contracts import ContractViolationError

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

__all__ = [
    "ComparisonOutcome",
    "Tolerance",
    "ToleranceMode",
    "compare",
]


class ToleranceMode(str, Enum):  # noqa: UP042
    """How a candidate artifact is compared against the pinned reference."""

    EXACT = "exact"
    RELATIVE = "relative"
    TOP_K_SEQUENCE = "top_k_sequence"
    COSINE = "cosine"
    TEST_OUTCOME_SET = "test_outcome_set"


@dataclass(frozen=True, slots=True)
class Tolerance:
    """A comparison rule plus the reason it is that rule.

    ``rationale`` is required and non-empty on purpose. Every tolerance in this
    corpus was chosen because of a *measured* property of the intended fix, and
    a bare ``1e-6`` in a config file six months from now is indistinguishable
    from a guess. Recording why is how the next person avoids "tightening" a
    tolerance that was loose for a reason.

    Only the fields relevant to ``mode`` may be set; the rest must stay at their
    defaults. A ``rtol`` sitting unused next to ``EXACT`` reads as a tolerance
    that is being applied and is not.
    """

    mode: ToleranceMode
    rationale: str
    rtol: float = 0.0
    atol: float = 0.0
    min_cosine: float = 0.0
    all_pass_scope: str = ""
    min_all_pass_count: int = 0

    def __post_init__(self) -> None:
        if not self.rationale.strip():
            raise ContractViolationError(
                "a tolerance must record why it is set where it is; an unexplained "
                "tolerance is indistinguishable from a guess and will be 'fixed' later"
            )
        if self.rtol < 0 or self.atol < 0:
            raise ContractViolationError("tolerances cannot be negative")
        if self.mode is ToleranceMode.EXACT and (self.rtol or self.atol or self.min_cosine):
            raise ContractViolationError(
                "EXACT admits no tolerance; declare RELATIVE or COSINE instead of "
                "attaching a slack value to a mode that ignores it"
            )
        if self.mode is ToleranceMode.RELATIVE and not (self.rtol or self.atol):
            raise ContractViolationError("RELATIVE needs a non-zero rtol or atol")
        if self.mode is ToleranceMode.TOP_K_SEQUENCE and not (self.rtol or self.atol):
            raise ContractViolationError(
                "TOP_K_SEQUENCE compares labels exactly and scores within a tolerance; "
                "declare the score tolerance"
            )
        if self.mode is ToleranceMode.COSINE and not 0 < self.min_cosine <= 1:
            raise ContractViolationError(
                f"COSINE needs a min_cosine in (0, 1], got {self.min_cosine!r}"
            )
        if self.mode is not ToleranceMode.COSINE and self.min_cosine:
            raise ContractViolationError("min_cosine only applies to COSINE")
        if self.mode is ToleranceMode.TEST_OUTCOME_SET:
            if not self.all_pass_scope:
                raise ContractViolationError(
                    "TEST_OUTCOME_SET needs the file that must go fully green; without "
                    "it the mode degenerates into 'nothing changed', which fails the "
                    "intended fix"
                )
            if self.min_all_pass_count <= 0:
                raise ContractViolationError(
                    "TEST_OUTCOME_SET needs a floor on how many tests the target file "
                    "must have; otherwise deleting the file passes the gate"
                )
        elif self.all_pass_scope or self.min_all_pass_count:
            raise ContractViolationError(
                "all_pass_scope/min_all_pass_count only apply to TEST_OUTCOME_SET"
            )


@dataclass(frozen=True, slots=True)
class ComparisonOutcome:
    """The verdict of one artifact comparison.

    ``worst_deviation`` is mode-dependent and exists so a near-miss is legible:
    the largest absolute difference for ``EXACT``, the largest relative
    difference for ``RELATIVE``/``TOP_K_SEQUENCE``, ``1 - min(cosine)`` for
    ``COSINE``, and the count of mismatched outcomes for ``TEST_OUTCOME_SET``.
    """

    passed: bool
    mode: ToleranceMode
    detail: str
    worst_deviation: float = 0.0

    def as_measurements(self) -> dict[str, float]:
        return {
            "comparison_passed": 1.0 if self.passed else 0.0,
            "comparison_worst_deviation": self.worst_deviation,
        }


class _ShapeMismatchError(Exception):
    """Internal: the two structures are not the same shape at all."""


def _walk(candidate: Any, reference: Any, path: str) -> Iterator[tuple[str, float, float]]:
    """Yield ``(path, candidate_number, reference_number)`` triples.

    Non-numeric leaves (strings, booleans, ``None``) are compared for equality
    in place and raise :class:`_ShapeMismatchError` when they differ — a JSON
    artifact carrying a label that changed is a changed answer, not a rounding
    difference.
    """
    if isinstance(reference, dict):
        if not isinstance(candidate, dict):
            raise _ShapeMismatchError(
                f"{path or '<root>'}: expected an object, got {type(candidate)}"
            )
        missing = sorted(set(reference) - set(candidate))
        extra = sorted(set(candidate) - set(reference))
        if missing or extra:
            raise _ShapeMismatchError(
                f"{path or '<root>'}: missing keys {missing}, extra keys {extra}"
            )
        for key in reference:
            yield from _walk(candidate[key], reference[key], f"{path}.{key}" if path else str(key))
        return
    if isinstance(reference, list):
        if not isinstance(candidate, list):
            raise _ShapeMismatchError(f"{path or '<root>'}: expected a list, got {type(candidate)}")
        if len(candidate) != len(reference):
            raise _ShapeMismatchError(
                f"{path or '<root>'}: length {len(candidate)} != reference {len(reference)}"
            )
        for index, (c, r) in enumerate(zip(candidate, reference, strict=True)):
            yield from _walk(c, r, f"{path}[{index}]")
        return
    if isinstance(reference, bool) or reference is None or isinstance(reference, str):
        if candidate != reference:
            raise _ShapeMismatchError(
                f"{path or '<root>'}: {candidate!r} != reference {reference!r}"
            )
        return
    if isinstance(reference, (int, float)):
        if isinstance(candidate, bool) or not isinstance(candidate, (int, float)):
            raise _ShapeMismatchError(f"{path or '<root>'}: expected a number, got {candidate!r}")
        yield path or "<root>", float(candidate), float(reference)
        return
    raise _ShapeMismatchError(f"{path or '<root>'}: unsupported reference type {type(reference)}")


def _relative_difference(candidate: float, reference: float) -> float:
    if reference == 0.0:
        return abs(candidate)
    return abs(candidate - reference) / abs(reference)


def _compare_numeric(
    candidate: Any,
    reference: Any,
    *,
    mode: ToleranceMode,
    rtol: float,
    atol: float,
) -> ComparisonOutcome:
    worst = 0.0
    worst_path = ""
    try:
        for path, c, r in _walk(candidate, reference, ""):
            if mode is ToleranceMode.EXACT:
                ok = c == r
                deviation = abs(c - r)
            else:
                ok = math.isclose(c, r, rel_tol=rtol, abs_tol=atol)
                deviation = _relative_difference(c, r)
            if deviation > worst:
                worst, worst_path = deviation, path
            if not ok:
                return ComparisonOutcome(
                    passed=False,
                    mode=mode,
                    detail=(
                        f"{path}: candidate {c!r} differs from reference {r!r} "
                        f"(deviation {deviation:.6g}, rtol={rtol:g}, atol={atol:g})"
                    ),
                    worst_deviation=deviation,
                )
    except _ShapeMismatchError as exc:
        return ComparisonOutcome(
            passed=False,
            mode=mode,
            detail=f"artifact shape differs from the pinned reference — {exc}",
            worst_deviation=math.inf,
        )
    where = f" (largest at {worst_path})" if worst_path else ""
    return ComparisonOutcome(
        passed=True,
        mode=mode,
        detail=f"matches the pinned reference; worst deviation {worst:.6g}{where}",
        worst_deviation=worst,
    )


def _as_records(payload: Any, label: str) -> Sequence[Any]:
    if not isinstance(payload, list):
        raise _ShapeMismatchError(f"{label} must be a list of top-k records, got {type(payload)}")
    return payload


def _compare_top_k(candidate: Any, reference: Any, tolerance: Tolerance) -> ComparisonOutcome:
    """Labels compared as an ordered sequence; scores compared numerically.

    The canonical artifact shape is a list of records, each ``{"labels": [...],
    "scores": [...]}``. Anything else is a shape mismatch and fails loudly —
    a comparator that silently finds no labels to compare is a gate that grades
    nothing.
    """
    mode = ToleranceMode.TOP_K_SEQUENCE
    try:
        cand_records = _as_records(candidate, "candidate artifact")
        ref_records = _as_records(reference, "reference artifact")
        if len(cand_records) != len(ref_records):
            raise _ShapeMismatchError(
                f"{len(cand_records)} query records vs reference {len(ref_records)}"
            )
        worst = 0.0
        for index, (c_rec, r_rec) in enumerate(zip(cand_records, ref_records, strict=True)):
            if not isinstance(c_rec, dict) or not isinstance(r_rec, dict):
                raise _ShapeMismatchError(f"record {index} is not an object")
            for key in ("labels", "scores"):
                if key not in c_rec or key not in r_rec:
                    raise _ShapeMismatchError(f"record {index} is missing {key!r}")
            c_labels, r_labels = c_rec["labels"], r_rec["labels"]
            if c_labels != r_labels:
                return ComparisonOutcome(
                    passed=False,
                    mode=mode,
                    detail=(
                        f"record {index}: top-k sequence {c_labels!r} != reference "
                        f"{r_labels!r}; the ranking is the answer, not the scores"
                    ),
                    worst_deviation=math.inf,
                )
            for position, (c, r) in enumerate(zip(c_rec["scores"], r_rec["scores"], strict=True)):
                c_val, r_val = float(c), float(r)
                deviation = _relative_difference(c_val, r_val)
                worst = max(worst, deviation)
                if not math.isclose(c_val, r_val, rel_tol=tolerance.rtol, abs_tol=tolerance.atol):
                    return ComparisonOutcome(
                        passed=False,
                        mode=mode,
                        detail=(
                            f"record {index} position {position}: score {c_val!r} vs "
                            f"reference {r_val!r} exceeds rtol={tolerance.rtol:g} "
                            f"atol={tolerance.atol:g}"
                        ),
                        worst_deviation=deviation,
                    )
    except (_ShapeMismatchError, ValueError, TypeError) as exc:
        return ComparisonOutcome(
            passed=False,
            mode=mode,
            detail=f"artifact shape differs from the pinned reference — {exc}",
            worst_deviation=math.inf,
        )
    return ComparisonOutcome(
        passed=True,
        mode=mode,
        detail=(
            f"top-k sequences identical across {len(cand_records)} records; "
            f"worst score deviation {worst:.6g}"
        ),
        worst_deviation=worst,
    )


def _vectors(payload: Any, label: str) -> list[tuple[str, list[float]]]:
    if isinstance(payload, dict):
        return [(str(key), [float(v) for v in payload[key]]) for key in sorted(payload)]
    if isinstance(payload, list):
        return [(str(i), [float(v) for v in row]) for i, row in enumerate(payload)]
    raise _ShapeMismatchError(f"{label} must be a list of vectors or a name→vector object")


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    if norm == 0.0:
        raise _ShapeMismatchError("zero-magnitude vector: cosine similarity is undefined")
    return dot / norm


def _compare_cosine(candidate: Any, reference: Any, tolerance: Tolerance) -> ComparisonOutcome:
    mode = ToleranceMode.COSINE
    try:
        cand = _vectors(candidate, "candidate artifact")
        ref = _vectors(reference, "reference artifact")
        if [name for name, _ in cand] != [name for name, _ in ref]:
            raise _ShapeMismatchError("vector keys differ from the pinned reference")
        worst_name, worst_cos = "", 1.0
        for (name, c_vec), (_, r_vec) in zip(cand, ref, strict=True):
            if len(c_vec) != len(r_vec):
                raise _ShapeMismatchError(
                    f"vector {name!r} has {len(c_vec)} dims, reference {len(r_vec)}"
                )
            similarity = _cosine(c_vec, r_vec)
            if similarity < worst_cos:
                worst_name, worst_cos = name, similarity
    except (_ShapeMismatchError, ValueError, TypeError) as exc:
        return ComparisonOutcome(
            passed=False,
            mode=mode,
            detail=f"artifact shape differs from the pinned reference — {exc}",
            worst_deviation=math.inf,
        )
    if worst_cos <= tolerance.min_cosine:
        return ComparisonOutcome(
            passed=False,
            mode=mode,
            detail=(
                f"vector {worst_name!r} has cosine {worst_cos:.8f} against the pinned "
                f"reference, at or below the {tolerance.min_cosine:.8f} threshold"
            ),
            worst_deviation=1.0 - worst_cos,
        )
    return ComparisonOutcome(
        passed=True,
        mode=mode,
        detail=(
            f"all {len(ref)} vectors above cosine {tolerance.min_cosine:.8f}; "
            f"worst {worst_cos:.8f} at {worst_name!r}"
        ),
        worst_deviation=1.0 - worst_cos,
    )


def _outcome_files(payload: Any, label: str) -> dict[str, dict[str, str]]:
    if not isinstance(payload, dict) or not isinstance(payload.get("files"), dict):
        raise _ShapeMismatchError(f"{label} must be an object with a 'files' mapping")
    files: dict[str, dict[str, str]] = {}
    for path, tests in payload["files"].items():
        if not isinstance(tests, dict):
            raise _ShapeMismatchError(f"{label}: file {path!r} must map test name → outcome")
        files[str(path)] = {str(name): str(outcome) for name, outcome in tests.items()}
    return files


def _compare_test_outcomes(
    candidate: Any, reference: Any, tolerance: Tolerance
) -> ComparisonOutcome:
    """Problem 4's gate: one file fully green, every other file unmoved.

    The scoped file is *excluded* from the unchanged-outcomes comparison. That
    exclusion is the entire point of this mode — the intended fix flips three
    of its tests from failing to passing, so a naive "outcomes unchanged" gate
    rejects the only correct solution.

    The scoped file's *names* are not excluded. A count floor without identities
    accepts wholesale replacement: 21 empty tests in place of the 21 real ones
    still report 21 passed. The pinned names must still be present and passing.
    Extra tests in that file are allowed; renaming or substituting them is not.
    """
    mode = ToleranceMode.TEST_OUTCOME_SET
    try:
        cand = _outcome_files(candidate, "candidate artifact")
        ref = _outcome_files(reference, "reference artifact")
    except _ShapeMismatchError as exc:
        return ComparisonOutcome(passed=False, mode=mode, detail=str(exc), worst_deviation=math.inf)

    scope = tolerance.all_pass_scope
    scoped = cand.get(scope)
    if scoped is None:
        return ComparisonOutcome(
            passed=False,
            mode=mode,
            detail=f"target file {scope!r} is absent from the candidate test report",
            worst_deviation=math.inf,
        )
    ref_scoped = ref.get(scope)
    if ref_scoped is None:
        return ComparisonOutcome(
            passed=False,
            mode=mode,
            detail=f"target file {scope!r} is absent from the pinned reference",
            worst_deviation=math.inf,
        )
    failing = sorted(name for name, outcome in scoped.items() if outcome != "passed")
    if failing:
        return ComparisonOutcome(
            passed=False,
            mode=mode,
            detail=f"{len(failing)} test(s) in {scope!r} did not pass: {failing[:5]}",
            worst_deviation=float(len(failing)),
        )
    if len(scoped) < tolerance.min_all_pass_count:
        return ComparisonOutcome(
            passed=False,
            mode=mode,
            detail=(
                f"{scope!r} reported {len(scoped)} tests, below the pinned floor of "
                f"{tolerance.min_all_pass_count}; tests were removed rather than fixed"
            ),
            worst_deviation=float(tolerance.min_all_pass_count - len(scoped)),
        )
    missing_names = sorted(name for name in ref_scoped if name not in scoped)
    if missing_names:
        return ComparisonOutcome(
            passed=False,
            mode=mode,
            detail=(
                f"{len(missing_names)} pinned test(s) in {scope!r} are missing from "
                f"the candidate: {missing_names[:5]}; replacing the file's tests "
                "with an equal number of empty ones is not a fix"
            ),
            worst_deviation=float(len(missing_names)),
        )

    mismatches: list[str] = []
    for path, ref_tests in ref.items():
        if path == scope:
            continue
        cand_tests = cand.get(path)
        if cand_tests is None:
            mismatches.append(f"{path}: absent from the candidate report")
            continue
        for name, outcome in ref_tests.items():
            actual = cand_tests.get(name)
            if actual != outcome:
                mismatches.append(f"{path}::{name}: {actual!r} was {outcome!r}")
    if mismatches:
        return ComparisonOutcome(
            passed=False,
            mode=mode,
            detail=(f"{len(mismatches)} outcome(s) outside {scope!r} moved: {mismatches[:5]}"),
            worst_deviation=float(len(mismatches)),
        )
    return ComparisonOutcome(
        passed=True,
        mode=mode,
        detail=(
            f"all {len(scoped)} tests in {scope!r} pass and {len(ref) - 1} other file(s) "
            "kept their pinned outcomes"
        ),
    )


def compare(tolerance: Tolerance, candidate: Any, reference: Any) -> ComparisonOutcome:
    """Compare a candidate artifact against the pinned reference under ``tolerance``.

    Args:
        tolerance: The declared rule, including why it is that rule.
        candidate: JSON-decoded artifact produced inside the workspace.
        reference: JSON-decoded pinned answer, read from outside the workspace.

    Returns:
        A :class:`ComparisonOutcome`. Never raises for a data problem — a
        malformed artifact is a *failed* comparison, because an unattended
        round cannot tell the difference between "the harness broke" and "the
        agent broke the artifact", and the safe reading of both is "not
        verified".
    """
    if tolerance.mode is ToleranceMode.EXACT:
        return _compare_numeric(candidate, reference, mode=ToleranceMode.EXACT, rtol=0.0, atol=0.0)
    if tolerance.mode is ToleranceMode.RELATIVE:
        return _compare_numeric(
            candidate,
            reference,
            mode=ToleranceMode.RELATIVE,
            rtol=tolerance.rtol,
            atol=tolerance.atol,
        )
    if tolerance.mode is ToleranceMode.TOP_K_SEQUENCE:
        return _compare_top_k(candidate, reference, tolerance)
    if tolerance.mode is ToleranceMode.COSINE:
        return _compare_cosine(candidate, reference, tolerance)
    return _compare_test_outcomes(candidate, reference, tolerance)
