"""A sha256 hash chain over ``metrics.jsonl``, and what it does and does not prove.

**This module makes tampering detectable. It does not make it impossible, and
no docstring, log line, or CLI output built on this module may claim that it
does.** ``research/OPEN-QUESTIONS.md`` R2 records that the ``turing`` sandbox
user is provisioned and never used — ``research_workspace_root`` defaults to
the *operator's* home and nothing drops privileges — so agent-authored code
runs as the operator today. A process that can write anywhere the operator
can write can rewrite this entire chain from its header down — recomputing
every digest from a forged header and rewriting the sidecar to match — and
produce a file that verifies clean. Nothing below narrows that hole, and
nothing can from inside this module: the header is bound *into* the chain,
never anchored to anything outside it, so a self-consistent forgery is
indistinguishable from a genuine run by construction. Prevention needs R2
(privilege separation, a results root the agent cannot write) and Q11 (the
harness made physically unreachable from the agent). Until those land, a
green verdict from this module means "not casually altered", not "provably
genuine".

What the header binding *does* buy is that the full recomputation above is
now the *cheapest* forgery available, rather than one of two.
:func:`verify_metrics_chain` re-derives the seed by hashing the header on
disk instead of reading the sidecar's recorded ``seed`` as the chain head,
so **relabelling** an otherwise-untouched chain — editing ``attempt_id``,
``problem_id``, ``round_id``, ``seed``, ``score_scale`` or ``started_at_ms``
in one small JSON object, recomputing nothing, and leaving every byte of
``metrics.jsonl`` alone — is caught. Before that check existed it was not:
a verified-clean chain could be silently reassigned to a different attempt,
round, seed, or score scale by a text edit that hashed nothing at all.

Second, narrower point that must not be blurred: a valid chain proves a
number was not changed *after it was recorded*. It says nothing about
whether the number is *meaningful*. A wrong verifier produces wrong numbers
that chain perfectly.

**How it works.** Every :class:`~turing.research.loop.results.MetricsLine`
written to an attempt's ``metrics.jsonl`` carries a ``_chain`` field: the
string ``"<seq>:<digest>"``, where ``digest`` is the sha256 of the previous
line's digest concatenated with this line's :func:`canonical_json` form
(the line itself, with ``_chain`` absent). ``seq`` is 0-based and strictly
increasing. A companion sidecar, ``metrics.chain.json``, records the run
header, the seed digest, the running final digest, and the line count, and
is rewritten after every append so an interrupted run — this program's own
brief says subscription windows close unpredictably — is still verifiable.
:func:`verify_metrics_chain` re-derives that seed by hashing the recorded
header, recomputes the chain from it, and reports the first line where it
diverges from what is on disk. The recorded seed is compared against the
re-derived one, never taken as the chain head on its own.

That catches an edit to the log alone. It does **not** catch an edit to the
log made consistently with a rewritten sidecar and header — see the test
named for exactly that in ``test_integrity.py``. :func:`reconcile_summary`
closes a different, easier hole: an honest log with a doctored ``metrics.json``
summary written over it. It re-derives every summary field from the raw
lines and never trusts the summary's own arithmetic.

**Reconciliation has three outcomes, not two** (:class:`ReconcileState`).
``metrics.json`` is written once, at the very end of an attempt, while the
chain is extended after every step — so "chain present, summary absent" is
the ordinary on-disk shape of a run that is still going, or of one that was
killed before it finished, and this program's own brief says subscription
windows close unpredictably. Reporting that as a *failure* was a false
alarm that never cleared: a killed attempt's trio is rotated into
``prior-N/`` on the operator's honest re-drive, and a rotated directory
holding an intact chain and no summary would have reported FAIL for the life
of the results root, on data nobody touched. Absence is therefore
:attr:`ReconcileState.INCOMPLETE` — its own state, never ``ok``. A summary
that is *present* and disagrees, or is unreadable, is still
:attr:`ReconcileState.FAILED` exactly as before; deleting a summary to dodge
a mismatch buys nothing, because ``verify``'s exit code is non-zero for
INCOMPLETE too.

A FAIL is not automatically tampering, either: an honest re-run of an
attempt over an already-used ``output_dir`` is refused by
``results.MetricsWriter`` before it can splice a second header into an
existing chain — the caller rotates the prior attempt's whole
``metrics.jsonl`` / ``metrics.chain.json`` / ``metrics.json`` trio aside into
a numbered ``prior-N/`` subdirectory first, so this module is never handed
the *spliced-chain* shape (two headers, two ``seq``-0 restarts, one file) it
could not otherwise tell apart from real alteration. That guarantee is
narrower than it sounds: a rotated-aside directory is itself walked and
checked by ``verify`` like any other, and this module cannot distinguish a
rotated-aside file left malformed by a process killed mid-write from one
someone tampered with after the fact — both report the identical FAIL. See
``docs/research-agent.md``'s "Integrity, and its limits" for what a
rotated-aside prior attempt looks like on disk, real examples of both a clean
and a FAILing one, and how an operator reads either.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING

from turing.research.contracts import ContractViolationError

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

__all__ = [
    "CHAIN_ALGORITHM",
    "CHAIN_FIELD",
    "CHAIN_SIDECAR_FILENAME",
    "CHAIN_VERSION",
    "ChainVerdict",
    "LogTail",
    "ReconcileState",
    "ReconcileVerdict",
    "canonical_json",
    "chain_next",
    "read_log_tail",
    "reconcile_summary",
    "seed_hash",
    "verify_metrics_chain",
]

CHAIN_ALGORITHM = "sha256"
CHAIN_VERSION = 1
CHAIN_FIELD = "_chain"
CHAIN_SIDECAR_FILENAME = "metrics.chain.json"

#: The sidecar's own required top-level keys. Anything missing makes it
#: unreadable rather than partially trusted.
_SIDECAR_REQUIRED_KEYS = frozenset({"version", "header", "seed", "final", "lines"})

#: Every key ``MetricsLine.to_json`` (``results.py``) emits itself, i.e. the
#: reserved axis/meta fields plus every core per-step field it writes before
#: the caller's own ``metrics``/``diagnostics`` entries. This list is copied
#: by hand rather than imported from ``results.py``: ``results.py`` imports
#: *this* module (:func:`chain_next` seeds its hash chain), so importing back
#: would be circular. A key renamed in ``MetricsLine.to_json`` and not
#: mirrored here would make :func:`reconcile_summary` misclassify a core
#: field as a score-series value or vice versa — keep the two lists in sync
#: by hand if that method ever changes.
_CORE_LINE_KEYS = frozenset(
    {
        "step",
        "total_steps",
        "ts",
        "outcome_code",
        "correctness_pass",
        "tokens_used",
        "tokens_cap",
        "steps_cap",
        "consumed_steps",
        "wall_clock_s",
        "wall_clock_cap_s",
        "cap_extensions",
        "step_wall_clock_s",
        "verify_wall_clock_s",
        "step_tokens",
        "made_progress",
        "progress",
    }
)

_DIAG_PREFIX = "diag_"

_METRICS_JSONL_FILENAME = "metrics.jsonl"
_METRICS_SUMMARY_FILENAME = "metrics.json"


# --------------------------------------------------------------------------- #
# Canonicalisation and the chain itself
# --------------------------------------------------------------------------- #


def canonical_json(payload: Mapping[str, object]) -> str:
    """The one serialisation every hash in this module is taken over.

    ``sort_keys=True`` — unlike the *emitted* line, which keeps insertion
    order for a human reading the file top to bottom — because hashing must
    not depend on the order a dict happened to be built in. Do not "tidy"
    this into a single shared call with the line writer's ``json.dumps``: the
    line writer is intentionally unsorted, this is intentionally sorted, and
    collapsing the two would make every historical chain unverifiable the
    next time someone reorders a dataclass field.

    ``separators=(",", ":")`` for a byte-for-byte stable, whitespace-free
    string: two payloads that differ in any value must never canonicalise to
    the same text, and two payloads that differ only in dict-construction
    order must always canonicalise to the same text.

    ``ensure_ascii=True`` so the digest does not depend on which encoding a
    reader's terminal or editor guesses for non-ASCII bytes.

    This function assumes ``payload`` is already JSON-safe — finite numbers,
    no ``bool`` where an ``int``/``float`` is expected, etc. Rejecting an
    unsafe value is :meth:`~turing.research.loop.results.MetricsLine.to_json`'s
    job, upstream of here; by the time a payload reaches this module it has
    already passed that gate. ``json.dumps`` will still happily emit the bare
    tokens ``NaN``/``Infinity``/``-Infinity`` for a non-finite float if one
    somehow arrives here, which is not valid JSON and which most other
    parsers reject — see ``test_integrity.py`` for that probed explicitly.
    """
    return json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def seed_hash(header: Mapping[str, object]) -> str:
    """The chain's starting digest — the run header, hashed alone.

    Every subsequent digest incorporates this one, so altering the header
    (the attempt/problem/round identity, the seed, the declared score scale)
    changes every digest after it.

    That alone would not bind the header to anything, because the sidecar
    records the resulting seed as a plain string next to the header it came
    from. :func:`verify_metrics_chain` closes the loop: it calls *this*
    function on the header it read from ``metrics.chain.json`` and compares
    the result against the recorded ``seed``, rather than trusting the
    recorded seed as the chain head. The re-hash plus that comparison is
    what makes the header part of the chain rather than free-floating
    metadata next to it — without it, editing the header and leaving the
    seed alone would cost nothing and still verify clean.
    """
    return hashlib.sha256(canonical_json(header).encode("utf-8")).hexdigest()


def chain_next(previous: str, payload: Mapping[str, object]) -> str:
    """Advance the chain by one line: ``sha256(previous + canonical_json(payload))``.

    ``payload`` must **not** already contain :data:`CHAIN_FIELD` — this
    raises :class:`~turing.research.contracts.ContractViolationError` if it
    does. Hashing a payload that contains its own digest is not merely
    circular, it is a bug that would only surface months later as a file
    nobody can verify: the digest a reader recomputes would depend on the
    digest they are trying to confirm.
    """
    if CHAIN_FIELD in payload:
        raise ContractViolationError(
            f"chain_next payload already contains {CHAIN_FIELD!r}; a payload must be hashed "
            "before its own chain field is added, never after"
        )
    return hashlib.sha256((previous + canonical_json(payload)).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# Verifying the chain
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class ChainVerdict:
    """The result of recomputing one attempt's chain from its seed.

    ``ok=False`` is returned, never raised, for every failure mode this
    module knows about — a sidecar this module cannot make sense of is
    exactly the situation an operator is trying to detect, not an internal
    bug to crash on.
    """

    ok: bool
    lines_checked: int
    first_bad_index: int | None
    reason: str


async def verify_metrics_chain(directory: Path) -> ChainVerdict:
    """Recompute ``directory``'s chain from its seed and compare to disk.

    All filesystem work runs in :func:`asyncio.to_thread`, matching
    :mod:`turing.research.loop.trajectory`'s write idiom — this function is
    read-only but the round runner calls it alongside other async I/O and
    must not block the event loop while doing so.
    """
    return await asyncio.to_thread(_verify_metrics_chain_sync, directory)


def _verify_metrics_chain_sync(directory: Path) -> ChainVerdict:
    """The blocking half of :func:`verify_metrics_chain`.

    Reads ``metrics.jsonl`` and :data:`CHAIN_SIDECAR_FILENAME` from
    ``directory``. Returns ``ok=False`` with a reason naming what broke —
    never an exception — for: a missing sidecar, a sidecar that is not valid
    JSON or is missing a required key, a chain-version mismatch, a
    ``header`` that is not a JSON object or that cannot be hashed, a
    ``seed`` that does not match the recorded header's re-derived hash, a
    missing JSONL, a line that is not valid JSON, a line missing its
    ``_chain`` field, a ``seq`` out of order, a digest that does not match,
    a line count that disagrees with the sidecar, or a final digest that
    disagrees with the sidecar.

    **The chain head is re-derived, never read.** ``seed_hash(header)`` is
    recomputed from the header as it sits on disk and compared against the
    sidecar's recorded ``seed``; only then is it used as the chain head. A
    header edited in place without recomputing anything — the whole
    ``attempt_id``/``problem_id``/``round_id``/``seed``/``score_scale``/
    ``started_at_ms`` identity of the run — therefore FAILs, where reading
    ``sidecar["seed"]`` straight off disk would have passed it. A *missing*
    ``header`` key is caught one step earlier, by the required-keys check.

    ``first_bad_index`` is the 0-based index of the first line whose
    recomputed digest (or sequence number) no longer matches what is
    recorded, and is ``None`` exactly when the failure cannot be pinned to
    one line — a missing or malformed sidecar, a missing JSONL, a line-count
    or final-digest disagreement discovered only after every individual line
    checked out.

    An **empty** ``metrics.jsonl`` whose sidecar reports ``"lines": 0`` is
    ``ok=True``: a run that has not recorded anything yet is not a corrupt
    run, and a writer that has not appended its first line has nothing to be
    caught tampering with.

    This function does not, and cannot, distinguish "genuinely untouched"
    from "rewritten consistently from a forged header" — see the module
    docstring and the test in ``test_integrity.py`` that asserts the latter
    verifies clean on purpose.
    """
    sidecar_path = directory / CHAIN_SIDECAR_FILENAME
    jsonl_path = directory / _METRICS_JSONL_FILENAME

    if not sidecar_path.is_file():
        return ChainVerdict(
            ok=False,
            lines_checked=0,
            first_bad_index=None,
            reason=f"{CHAIN_SIDECAR_FILENAME} is missing",
        )
    try:
        sidecar_text = sidecar_path.read_text(encoding="utf-8")
    except OSError as exc:
        return ChainVerdict(
            ok=False,
            lines_checked=0,
            first_bad_index=None,
            reason=f"{CHAIN_SIDECAR_FILENAME} could not be read: {exc}",
        )
    try:
        sidecar = json.loads(sidecar_text)
    except json.JSONDecodeError as exc:
        return ChainVerdict(
            ok=False,
            lines_checked=0,
            first_bad_index=None,
            reason=f"{CHAIN_SIDECAR_FILENAME} is not valid JSON: {exc}",
        )
    if not isinstance(sidecar, dict):
        return ChainVerdict(
            ok=False,
            lines_checked=0,
            first_bad_index=None,
            reason=f"{CHAIN_SIDECAR_FILENAME} does not contain a JSON object",
        )
    missing_keys = _SIDECAR_REQUIRED_KEYS - sidecar.keys()
    if missing_keys:
        return ChainVerdict(
            ok=False,
            lines_checked=0,
            first_bad_index=None,
            reason=f"{CHAIN_SIDECAR_FILENAME} is missing key(s): {sorted(missing_keys)}",
        )
    if sidecar["version"] != CHAIN_VERSION:
        return ChainVerdict(
            ok=False,
            lines_checked=0,
            first_bad_index=None,
            reason=f"chain version {sidecar['version']!r} != expected {CHAIN_VERSION!r}",
        )
    seed = sidecar["seed"]
    if not isinstance(seed, str):
        return ChainVerdict(
            ok=False,
            lines_checked=0,
            first_bad_index=None,
            reason=f"{CHAIN_SIDECAR_FILENAME} 'seed' is not a string",
        )

    # Bind the header into the chain instead of taking the recorded seed on
    # trust. Without this, `seed` is the chain head and `header` is merely
    # the metadata sitting beside it, so all six identity fields could be
    # rewritten with a text editor — no hashing, not one byte of
    # metrics.jsonl touched — and this function would still report OK.
    header = sidecar["header"]
    if not isinstance(header, dict):
        # Deliberately not `dict(header)`: coercion succeeds on a list of
        # pairs and on other shapes that are not what a writer ever emits,
        # which would hash *something* and compare it, turning a malformed
        # sidecar into a confusing seed mismatch instead of naming the
        # header as the problem.
        return ChainVerdict(
            ok=False,
            lines_checked=0,
            first_bad_index=None,
            reason=f"{CHAIN_SIDECAR_FILENAME} 'header' is not a JSON object",
        )
    try:
        recomputed_seed = seed_hash(header)
    except (TypeError, ValueError, RecursionError) as exc:
        # `json.loads` yields only JSON-native types, so `canonical_json`
        # cannot hit an unserialisable value here today, and it tolerates
        # the non-finite floats `json.loads` accepts from the bare
        # `NaN`/`Infinity` tokens (those simply produce a digest that does
        # not match, i.e. the FAIL below). This guard exists so that stops
        # being load-bearing: a damaged sidecar must always leave through a
        # verdict, never as a traceback out of the verifier.
        return ChainVerdict(
            ok=False,
            lines_checked=0,
            first_bad_index=None,
            reason=f"{CHAIN_SIDECAR_FILENAME} 'header' could not be hashed: {exc}",
        )
    if recomputed_seed != seed:
        return ChainVerdict(
            ok=False,
            lines_checked=0,
            first_bad_index=None,
            reason=(
                f"{CHAIN_SIDECAR_FILENAME} 'seed' does not match the recorded header "
                "(header altered)"
            ),
        )

    if not jsonl_path.is_file():
        return ChainVerdict(
            ok=False,
            lines_checked=0,
            first_bad_index=None,
            reason=f"{_METRICS_JSONL_FILENAME} is missing",
        )
    try:
        jsonl_text = jsonl_path.read_text(encoding="utf-8")
    except OSError as exc:
        return ChainVerdict(
            ok=False,
            lines_checked=0,
            first_bad_index=None,
            reason=f"{_METRICS_JSONL_FILENAME} could not be read: {exc}",
        )

    # The writer is strictly append-only, one "\n"-terminated line per call,
    # so a well-formed file's text always ends with "\n". Splitting on "\n"
    # therefore always leaves a trailing empty string to discard. Any *other*
    # empty entry is an interior blank line, which is not something the
    # writer produces — it is treated as a malformed record below rather
    # than silently skipped, because this is the tamper-evidence layer, not
    # the desktop's tolerant reader.
    raw_lines = jsonl_text.split("\n")
    if raw_lines and raw_lines[-1] == "":
        raw_lines.pop()

    chain_head = seed
    for index, raw_line in enumerate(raw_lines):
        try:
            payload = json.loads(raw_line)
        except json.JSONDecodeError:
            return ChainVerdict(
                ok=False,
                lines_checked=index,
                first_bad_index=index,
                reason=f"line {index} is not valid JSON",
            )
        if not isinstance(payload, dict):
            return ChainVerdict(
                ok=False,
                lines_checked=index,
                first_bad_index=index,
                reason=f"line {index} is not a JSON object",
            )
        chain_value = payload.get(CHAIN_FIELD)
        if not isinstance(chain_value, str) or ":" not in chain_value:
            return ChainVerdict(
                ok=False,
                lines_checked=index,
                first_bad_index=index,
                reason=f"line {index} has no valid {CHAIN_FIELD!r} field",
            )
        seq_text, _, digest = chain_value.partition(":")
        try:
            seq = int(seq_text)
        except ValueError:
            return ChainVerdict(
                ok=False,
                lines_checked=index,
                first_bad_index=index,
                reason=f"line {index} has a non-integer sequence number {seq_text!r}",
            )
        if seq != index:
            return ChainVerdict(
                ok=False,
                lines_checked=index,
                first_bad_index=index,
                reason=f"line {index} has sequence {seq}, expected {index} (out of order)",
            )
        body = {k: v for k, v in payload.items() if k != CHAIN_FIELD}
        expected_digest = chain_next(chain_head, body)
        if expected_digest != digest:
            return ChainVerdict(
                ok=False,
                lines_checked=index,
                first_bad_index=index,
                reason=f"line {index} digest does not match the recomputed chain",
            )
        chain_head = digest

    lines_checked = len(raw_lines)
    if lines_checked != sidecar["lines"]:
        return ChainVerdict(
            ok=False,
            lines_checked=lines_checked,
            first_bad_index=None,
            reason=(
                f"{_METRICS_JSONL_FILENAME} has {lines_checked} line(s), "
                f"{CHAIN_SIDECAR_FILENAME} reports {sidecar['lines']}"
            ),
        )
    if chain_head != sidecar["final"]:
        return ChainVerdict(
            ok=False,
            lines_checked=lines_checked,
            first_bad_index=None,
            reason="final digest does not match the sidecar's recorded final digest",
        )
    return ChainVerdict(ok=True, lines_checked=lines_checked, first_bad_index=None, reason="ok")


# --------------------------------------------------------------------------- #
# Reconciling the summary against the raw log
# --------------------------------------------------------------------------- #


class ReconcileState(str, Enum):  # noqa: UP042
    """Whether a summary agreed with its log, disagreed with it, or is absent.

    Three states rather than a bool because "this run's summary is wrong" and
    "this run has no summary yet" are different facts about a run, and only
    the first is a finding about the *data*. ``metrics.json`` is written once,
    after an attempt terminates; ``metrics.jsonl`` and its sidecar are
    extended after every step. An attempt observed between those two moments —
    in flight, or killed by a closed subscription window — has an intact chain
    and no summary through no fault of anyone's, and that shape is
    *permanent* once the operator re-drives: ``runner._rotate_stale_metrics``
    moves aside whichever of the trio exist, so the ``prior-N/`` directory
    keeps its chain and its missing summary forever.

    Collapsing that into ``ok=False`` made the verifier report FAIL, forever,
    on untouched honest data — the exact "cries wolf" failure this layer
    exists to avoid. Collapsing it into ``ok=True`` would be worse: callers
    read ``ok`` as "this run is good", and an unfinished run is not a good
    run, it is one whose numbers are not final. Hence a distinct state, and
    :attr:`ReconcileVerdict.ok` deliberately stays ``False`` for it.

    :attr:`INCOMPLETE` is reached *only* by a summary that is not there at
    all. A summary that exists but is unreadable, is not a JSON object, or
    disagrees with the log in any field is :attr:`FAILED`, unchanged — this
    distinction weakens no check. Nor does deleting a lone summary buy an
    escape hatch: that moves a run from FAILED to INCOMPLETE, and
    ``turing.research.loop.verify`` exits non-zero (``2``) on INCOMPLETE too
    — a doctored *summary alone*, with an honest chain still sitting beside
    it, cannot be edited to a green exit.

    That guarantee is about the summary in isolation and does not extend to
    the trio as a whole — this module's own module-level docstring already
    concedes a self-consistent forgery (every file a check reads, rewritten
    together so nothing disagrees with anything else) is undetectable by
    construction, because nothing here is anchored outside the chain. Two
    edits from that conceded family reach a green ``verify`` through *this*
    function specifically, reproduced for real, and neither one computes a
    digest:

    * Delete ``metrics.jsonl`` and ``metrics.chain.json`` outright and leave
      a freely-edited ``metrics.json`` (and the plots) behind.
      ``turing.research.loop.verify.find_runs`` walks for directories
      containing ``metrics.jsonl`` alone, so with the chain gone this
      directory is not a run any more — the doctored summary is never read,
      let alone checked, and the CLI exits ``0`` as long as the results root
      holds one other honest run.
    * Truncate ``metrics.jsonl`` to empty, rewrite the sidecar as
      ``{"lines": 0, "final": <the sidecar's own already-recorded "seed",
      copied verbatim>}``, and null out ``metrics.json``'s derived fields
      (``steps_recorded: 0``, ``baseline_score: null``, ``best_score: null``,
      and so on). An empty log with ``lines: 0`` is on-disk-identical to a
      writer that has not appended its first line yet, which
      :func:`verify_metrics_chain` reports ``ok=True`` for by design — and
      :func:`reconcile_summary` has nothing left to disagree with. Real
      output: ``OK (0 line(s) checked)``, exit ``0``.

    Neither example sharpens the module docstring's limit; both are inside
    it. What this enum must not claim is that :class:`ReconcileVerdict`
    closes that hole for the trio the way it closes it for a lone summary —
    it does not, and was never built to.
    """

    OK = "ok"
    INCOMPLETE = "incomplete"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class ReconcileVerdict:
    """The result of re-deriving ``metrics.json`` from ``metrics.jsonl``.

    ``mismatches`` names the exact fields that disagree, so a doctored
    summary is not merely flagged but pointed at. It is empty for both
    :attr:`ReconcileState.OK` and :attr:`ReconcileState.INCOMPLETE`, and for
    the FAILED cases where the summary could not be read far enough to
    compare any field at all — so ``mismatches`` alone never distinguishes
    the states. Read :attr:`state`.

    ``ok`` is a derived property, not a field, so it cannot drift from
    ``state``: it is ``True`` for exactly one of the three states. Existing
    callers that treat it as "this run is good" keep working unchanged, and
    a caller that needs to tell an unfinished run from a broken one must ask
    for :attr:`state` explicitly rather than getting the two silently
    conflated.
    """

    state: ReconcileState
    mismatches: tuple[str, ...]
    reason: str

    @property
    def ok(self) -> bool:
        """``True`` only for :attr:`ReconcileState.OK` — never for INCOMPLETE."""
        return self.state is ReconcileState.OK


def _numbers_close(a: object, b: object, *, rel_tol: float = 1e-9, abs_tol: float = 1e-9) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return bool(a) == bool(b) and type(a) is type(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(float(a), float(b), rel_tol=rel_tol, abs_tol=abs_tol)
    return a == b


def _check_exact(mismatches: list[str], field: str, actual: object, expected: object) -> None:
    if actual != expected:
        mismatches.append(field)


def _check_close(
    mismatches: list[str],
    field: str,
    actual: object,
    expected: object,
    *,
    rel_tol: float = 1e-9,
    abs_tol: float = 1e-9,
) -> None:
    if actual is None and expected is None:
        return
    if actual is None or expected is None:
        mismatches.append(field)
        return
    if not _numbers_close(actual, expected, rel_tol=rel_tol, abs_tol=abs_tol):
        mismatches.append(field)


@dataclass(frozen=True, slots=True)
class LogTail:
    """Everything ``metrics.jsonl`` alone says about an attempt that ended.

    This exists so the two call sites that must agree about what an attempt's
    raw log says cannot drift apart. :func:`reconcile_summary` re-derives
    ``metrics.json``'s fields from the log and fails the run when they
    disagree; ``runner.py`` writes a terminal ``metrics.json`` for an attempt
    whose exception was contained mid-run, and that summary has to reconcile
    against this same log or the containment would trade an ``INCOMPLETE``
    verdict for a ``FAILED`` one — a strictly worse outcome, since ``FAILED``
    is this tool's word for dishonesty. Two hand-written copies of "the last
    line", "the last line carrying ``progress``" and "the first emitted
    score-series value" would be exactly the drift this module warns about
    around :data:`_CORE_LINE_KEYS`; one reader, two consumers, no drift.

    Values are the **raw** JSON objects read off the file, never coerced.
    :func:`reconcile_summary` compares them for equality against a summary
    written by someone else and must see what is actually on disk — coercing
    an ``int`` to a ``float`` here would quietly decide a comparison this
    module exists to make honestly. A consumer that needs typed values (the
    runner does, to hand them to ``write_attempt_summary``) coerces at its
    own call site and is responsible for what it does when that fails.

    A missing, unreadable or empty ``metrics.jsonl`` yields a tail with
    ``line_count == 0`` and every field ``None`` rather than an exception:
    "the attempt wrote no lines" is an ordinary state on both call sites
    (an attempt killed before its first append), not an error condition.
    """

    #: The sidecar's ``header`` object, or ``None`` when the sidecar is
    #: missing or unreadable. The only place an attempt's identity survives
    #: on disk when the in-memory ``Attempt`` died with its exception.
    header: Mapping[str, object] | None
    #: Number of parseable JSON-object lines — ``metrics.json``'s
    #: ``steps_recorded``. Unparseable lines are skipped, not counted:
    #: reporting them is :func:`verify_metrics_chain`'s finding to raise, and
    #: double-reporting the same corruption is what this module avoids.
    line_count: int
    #: The last parseable line, or ``None`` when there is none. Every field
    #: ``reconcile_summary`` defines as "the last line's X" comes from here.
    last_line: Mapping[str, object] | None
    #: The value of ``progress`` on the *last* line that carries the key at
    #: all — ``None`` when no line does, which is not the same fact as a line
    #: carrying ``progress: null`` and is deliberately not distinguished,
    #: because ``metrics.json`` cannot record the difference either.
    final_progress: object | None
    #: The first emitted score-series value across the whole log, in file
    #: order — ``metrics.json``'s ``baseline_score``.
    baseline_score: object | None
    #: Every emitted score-series value, in file order. ``best_score`` has to
    #: be one of these when it is not ``None``; see :func:`reconcile_summary`
    #: for why that check is deliberately weak.
    score_values: tuple[object, ...]


def read_log_tail(directory: Path) -> LogTail:
    """Read ``directory``'s metrics log and sidecar into a :class:`LogTail`.

    Synchronous and blocking by design: both callers already run it off the
    event loop (:func:`reconcile_summary` through :func:`asyncio.to_thread`,
    the runner through the same), and an async wrapper here would only add a
    second way to call it that one of them would eventually get wrong.

    "Score-series value" means the same thing it means in
    :func:`reconcile_summary`: any key on a line that is not a core field
    :meth:`~turing.research.loop.results.MetricsLine.to_json` always emits,
    not ``diag_``-prefixed, and not :data:`CHAIN_FIELD`. That definition
    lives in exactly one place — here — for the reason :class:`LogTail`
    gives.
    """
    header: Mapping[str, object] | None = None
    sidecar_path = directory / CHAIN_SIDECAR_FILENAME
    try:
        sidecar_raw = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        sidecar_raw = None
    if isinstance(sidecar_raw, dict) and isinstance(sidecar_raw.get("header"), dict):
        header = sidecar_raw["header"]

    lines: list[dict[str, object]] = []
    try:
        jsonl_text = (directory / _METRICS_JSONL_FILENAME).read_text(encoding="utf-8")
    except OSError:
        jsonl_text = ""
    for raw_line in jsonl_text.split("\n"):
        if not raw_line:
            continue
        try:
            parsed = json.loads(raw_line)
        except json.JSONDecodeError:
            # A malformed line is verify_metrics_chain's finding to raise,
            # not this function's — skipped rather than double-reported.
            continue
        if isinstance(parsed, dict):
            lines.append(parsed)

    final_progress: object | None = None
    for line in reversed(lines):
        if "progress" in line:
            final_progress = line["progress"]
            break

    baseline_score: object | None = None
    score_values: list[object] = []
    for line in lines:
        for key, value in line.items():
            if key == CHAIN_FIELD or key in _CORE_LINE_KEYS or key.startswith(_DIAG_PREFIX):
                continue
            score_values.append(value)
            if baseline_score is None:
                baseline_score = value

    return LogTail(
        header=header,
        line_count=len(lines),
        last_line=lines[-1] if lines else None,
        final_progress=final_progress,
        baseline_score=baseline_score,
        score_values=tuple(score_values),
    )


async def reconcile_summary(directory: Path) -> ReconcileVerdict:
    """Re-derive ``metrics.json`` from ``metrics.jsonl`` alone and compare.

    All filesystem work runs in :func:`asyncio.to_thread`, matching this
    module's other public function and :mod:`turing.research.loop.trajectory`.
    """
    return await asyncio.to_thread(_reconcile_summary_sync, directory)


def _reconcile_summary_sync(directory: Path) -> ReconcileVerdict:
    """The blocking half of :func:`reconcile_summary`.

    This closes the easiest forgery this module knows about: an honest,
    correctly-chained log with a doctored summary written over it. Every
    field below is re-derived from the raw lines; none is trusted from the
    summary itself.

    ============================  =========================================
    ``metrics.json`` field        Must equal, derived from the log
    ============================  =========================================
    ``steps_recorded``            number of lines
    ``consumed_steps``            last line's ``consumed_steps`` field
    ``consumed_tokens``           last line's ``tokens_used``
    ``consumed_wall_clock_seconds`` last line's ``wall_clock_s`` (within ``1e-6``)
    ``cap_extensions``            last line's ``cap_extensions``
    ``final_progress``            the last line carrying ``progress``, or ``None``
    ``outcome``                   last line's ``outcome_code``
    ``baseline_score``            the first emitted score-series value, or ``None``
    ``best_score``                when not ``None``, must appear as *some* emitted
                                   score-series value; when ``None``, no check at all
    ============================  =========================================

    **``consumed_steps`` is compared against the line's own ``consumed_steps``
    field, never against ``step`` (round 2 — this was the original bug).**
    ``step`` is the solver's step index, the chart's x-axis; ``consumed_steps``
    is cap accounting, and the two only coincide on the happy path. The
    runner's ``_charge_failed_step`` calls ``record_consumption(...)``, which
    bumps ``consumed.steps`` **without** bumping ``step_index`` — a proposal
    call that happened still costs a step even when parse/apply then failed.
    On that path a run legitimately reports ``step_index=0`` alongside
    ``consumed.steps=2``, so deriving ``consumed_steps`` from ``step`` raises
    a false mismatch on perfectly honest data. ``MetricsLine`` carries
    ``consumed_steps`` as its own emitted field for exactly this reason; do
    not "fix" this back to reading ``step``.

    ``best_score`` is checked **weakly on purpose**: it only has to be a
    value the run actually emitted, not the maximum. ``best`` is chosen in
    ``runner.py`` by ``_is_better``, whose tie-breaking rule this module must
    not re-implement — a second copy of that rule would drift from the first
    and start raising false alarms on a perfectly honest run. Asserting the
    reported best is a number the run actually measured still catches a
    fabricated score without coupling to logic that lives elsewhere. Do not
    "strengthen" this into a max-of-log check.

    **When ``best_score`` is ``None``, this checks nothing at all — do not
    "strengthen" that to "the log emitted scores, so a correct summary should
    have picked one" (clarified 2026-08-14, round 3).** ``runner.py``'s
    ``_is_better`` deliberately refuses a harness-failure result as ``best``:
    *"a harness-failure result is not a grade and must never become best."*
    An attempt whose every verify came back a harness failure legitimately
    has ``best_score = None`` while its log legitimately carries ``0.0``
    score values — ``problems/speedup.py`` produces exactly this shape
    whenever the benchmark cannot get a usable measurement. Flagging that as
    a mismatch is a second, negated copy of ``_is_better``'s refusal rule,
    which is precisely the drift this docstring warns against two paragraphs
    up. A ``None`` ``best_score`` is therefore accepted unconditionally; only
    a *non-``None``* ``best_score`` is checked against the log.

    A line's "score-series" value is whichever of its keys is neither a core
    field :class:`~turing.research.loop.results.MetricsLine.to_json` always
    emits, nor prefixed ``diag_``, nor :data:`CHAIN_FIELD` — see
    ``_CORE_LINE_KEYS`` above for why that set is hand-copied rather than
    imported.

    Every derivation above is delegated to :func:`read_log_tail` rather than
    inlined here, because ``runner.py`` has to make the *same* derivations to
    write a terminal summary for an attempt whose exception it contained, and
    a second copy would drift into raising false mismatches on honest data —
    see :class:`LogTail`. What stays here is the comparison and the verdict,
    which is this function's own job and nobody else's.

    A missing ``metrics.json`` is :attr:`ReconcileState.INCOMPLETE` — not
    ``ok``, and not a FAIL either — with an empty ``mismatches`` tuple and a
    reason naming the summary as absent. See :class:`ReconcileState` for why
    that is its own state: absence is what an unfinished or killed attempt
    looks like on disk, permanently so once its trio is rotated into
    ``prior-N/``, and calling it a failure was a false alarm that never
    cleared. Absence is still not a pass; ``verify`` exits non-zero on it.

    This function deliberately does **not** consult the chain before deciding
    that. Re-reading and re-hashing ``metrics.jsonl`` here would duplicate
    :func:`verify_metrics_chain`'s entire job for one branch, and two copies
    of that logic is precisely the drift this module warns about elsewhere.
    The composition — "intact chain **and** absent summary means unfinished;
    *broken* chain means failure whatever the summary does" — belongs to the
    caller holding both verdicts, which is
    :func:`turing.research.loop.verify.verify_run`. A caller that ignores the
    chain verdict and reads INCOMPLETE as benign on its own is reading this
    verdict out of context.

    When ``metrics.jsonl`` has zero valid lines, the fields above that are
    defined only in terms of "the last line" (``consumed_steps``,
    ``consumed_tokens``, ``consumed_wall_clock_seconds``, ``cap_extensions``,
    ``outcome``, ``final_progress``) have no value to re-derive and are not
    checked; ``steps_recorded``, ``baseline_score`` and ``best_score`` still
    are, since those are well-defined (``0``, ``None``, "nothing emitted")
    even for an empty log.
    """
    summary_path = directory / _METRICS_SUMMARY_FILENAME

    if not summary_path.is_file():
        # The only path to INCOMPLETE. Note it is reported the same way for a
        # zero-line log as for a long one: a run that recorded nothing and
        # never wrote a summary is, if anything, more obviously unfinished
        # than one that recorded fifty steps, and inventing a fourth state
        # for it would buy the operator nothing it could act on differently.
        # The chain check still reports the line count either way, so an
        # empty log is visible in the output rather than hidden by this.
        return ReconcileVerdict(
            state=ReconcileState.INCOMPLETE,
            mismatches=(),
            reason=(
                f"{_METRICS_SUMMARY_FILENAME} is missing "
                "(the attempt never reached its summary write)"
            ),
        )
    try:
        summary_raw = json.loads(summary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        # Present but unreadable is a failure, not an incompletion: the
        # writer emits this file whole, so a half-written or unparseable one
        # is damage, and treating damage as "not finished yet" would hide it.
        return ReconcileVerdict(
            state=ReconcileState.FAILED,
            mismatches=(),
            reason=f"{_METRICS_SUMMARY_FILENAME} is not valid JSON: {exc}",
        )
    if not isinstance(summary_raw, dict):
        return ReconcileVerdict(
            state=ReconcileState.FAILED,
            mismatches=(),
            reason=f"{_METRICS_SUMMARY_FILENAME} does not contain a JSON object",
        )
    summary: Mapping[str, object] = summary_raw

    tail = read_log_tail(directory)
    steps_recorded = tail.line_count
    last = tail.last_line
    final_progress = tail.final_progress
    baseline_score = tail.baseline_score
    score_values = tail.score_values

    mismatches: list[str] = []
    _check_exact(mismatches, "steps_recorded", summary.get("steps_recorded"), steps_recorded)
    if last is not None:
        _check_exact(
            mismatches,
            "consumed_steps",
            summary.get("consumed_steps"),
            last.get("consumed_steps"),
        )
        _check_exact(
            mismatches, "consumed_tokens", summary.get("consumed_tokens"), last.get("tokens_used")
        )
        _check_close(
            mismatches,
            "consumed_wall_clock_seconds",
            summary.get("consumed_wall_clock_seconds"),
            last.get("wall_clock_s"),
            rel_tol=0.0,
            abs_tol=1e-6,
        )
        _check_exact(
            mismatches, "cap_extensions", summary.get("cap_extensions"), last.get("cap_extensions")
        )
        _check_exact(mismatches, "outcome", summary.get("outcome"), last.get("outcome_code"))
        _check_close(mismatches, "final_progress", summary.get("final_progress"), final_progress)

    _check_close(mismatches, "baseline_score", summary.get("baseline_score"), baseline_score)

    # best_score is None is left unchecked entirely — see the docstring
    # above. A harness-failure attempt legitimately has best_score=None
    # while its log legitimately carries score values (e.g. speedup.py's
    # 0.0 on a harness failure); asserting "score values imply a non-None
    # best" would be a second, negated copy of runner.py's _is_better
    # refusal rule, and it drifted in exactly the way this module warns
    # against. Do not re-add that branch.
    best_score = summary.get("best_score")
    if best_score is not None and not any(
        _numbers_close(best_score, value) for value in score_values
    ):
        mismatches.append("best_score")

    state = ReconcileState.OK if not mismatches else ReconcileState.FAILED
    reason = (
        "ok"
        if state is ReconcileState.OK
        else f"{len(mismatches)} field(s) disagree with the raw log"
    )
    return ReconcileVerdict(state=state, mismatches=tuple(mismatches), reason=reason)
