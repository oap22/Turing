"""The seam between the loop's in-memory numbers and the files the desktop reads.

``metrics.py`` computes the four driving-function numbers and holds them in
memory; ``trajectory.py`` writes ``trajectory.json`` and the per-attempt logs
that loop 2 will eventually sample. Neither one writes ``metrics.jsonl``,
``metrics.json``, or ``.viewer.json`` — the files Owen's desktop app's metrics
and images panes actually watch. This module is what makes a run visible
*while it happens*, not only after ``trajectory.json`` is appended to at the
end of a round.

**A field the desktop cannot chart is a field that does not exist.** The pane
(``webui/src/desktop/panes/metrics.ts``) keeps only finite numeric values out
of each JSONL line and excludes exactly three keys from its chart series —
``step``, ``total_steps``, ``ts`` — because those are the axis and the wall
clock, not a forcing function. :data:`RESERVED_FIELDS` is that same set,
enforced here so a problem-supplied metric that happens to share one of those
names fails loudly at the point it is about to be silently swallowed, rather
than drawing a chart with a hole in it nobody notices for a week.

This module also carries the asymmetry the rest of the codebase calls "the
agent invents; the operator holds the ruler": :meth:`MetricsLine.to_json`
raises :class:`~turing.research.contracts.ContractViolationError` on a bad
*scored* metric, because that number is what the run is graded on and a
mis-reported one must stop the run. The same method silently drops a bad
*diagnostic* value, because that is the agent's own notebook and killing an
unattended overnight run over a malformed debug number would be absurd. Do
not "fix" that asymmetry into consistency; see the docstring on
:meth:`MetricsLine.to_json` for the full argument.
"""

from __future__ import annotations

import asyncio
import json
import math
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from turing.research.contracts import (
    CORE_METRICS_FIELDS,
    DIAGNOSTIC_KEY_PREFIX,
    RESERVED_METRICS_FIELDS,
    AttemptState,
    ContractViolationError,
)
from turing.research.loop import integrity
from turing.research.loop.protocols import SystemClock
from turing.research.loop.verify import (
    HONESTY_LINE,
    format_run_verdict,
    verify_run,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from turing.research.loop.protocols import Clock

logger = structlog.get_logger(__name__)

__all__ = [
    "AGENT_DIAGNOSTICS_RELPATH",
    "METRICS_VERDICT_FILENAME",
    "RESERVED_FIELDS",
    "MetricsLine",
    "MetricsWriter",
    "Outcome",
    "ProgressTracker",
    "read_agent_diagnostics",
    "read_metrics_points",
    "usable_target",
    "write_attempt_summary",
    "write_attempt_verdict",
    "write_round_summary",
    "write_viewer_config",
]

#: Where :func:`write_attempt_verdict` records what ``verify`` said about this
#: directory, for a reader that cannot run ``verify`` itself — the desktop's
#: metrics pane. Deliberately **not** ``metrics.json``-adjacent in meaning: it
#: is not part of the hash chain, not reconciled against the log, and not a
#: run in its own right (``verify.find_runs`` keys on ``metrics.jsonl``). It is
#: a *report about* the other files, which is why nothing that checks them
#: reads it, and why re-running ``verify`` after it lands returns exactly what
#: it returned before.
METRICS_VERDICT_FILENAME = "metrics.verdict.json"


#: Keys the desktop's chart series builder treats as axis/meta rather than a
#: plottable series (``webui/src/desktop/panes/metrics.ts``,
#: ``EXCLUDED_SERIES_KEYS``). A problem-supplied metric using one of these
#: names would be silently swallowed by the pane; :meth:`MetricsLine.to_json`
#: rejects it loudly instead, at the point the line is built, rather than
#: leaving a gap in the chart that only an operator staring at the pane would
#: ever notice.
#:
#: Defined in ``contracts.py`` and re-exported here under the name this
#: module has always published. ``contracts`` is the one module both this one
#: and ``integrity`` can import, and ``contracts`` needs the set itself to
#: refuse a colliding ``score_scale`` where the scale is *declared* — so the
#: list lives there and nothing hand-copies it. See
#: :data:`turing.research.contracts.RESERVED_METRICS_FIELDS`.
RESERVED_FIELDS: frozenset[str] = RESERVED_METRICS_FIELDS


# --------------------------------------------------------------------------- #
# Outcome
# --------------------------------------------------------------------------- #


class Outcome(int, Enum):
    """A per-line, numeric encoding of :class:`~turing.research.contracts.AttemptState`.

    This exists only because the desktop pane drops every non-numeric field
    from its chart series. A string ``outcome`` column would satisfy an
    acceptance criterion that merely says "the outcome is recorded" while
    being completely invisible on the chart the criterion exists to serve.
    The human-readable state string still belongs in the round record and the
    attempt log — that is ``trajectory.py``'s business, not this module's;
    this enum's only job is to survive the pane's numeric-only filter.

    ``PAUSED`` gets its own code (5) rather than folding into ``RUNNING`` (0)
    because it is the state an attempt enters when a Claude subscription
    window closes mid-run, and resumability is load-bearing in this program.
    An operator glancing at the chart needs "working" and "waiting to be
    resumed" to look different, not identical.
    """

    RUNNING = 0
    SOLVED = 1
    FAILED_WITHIN_CAP = 2
    ESCALATED = 3
    ABANDONED = 4
    PAUSED = 5

    @classmethod
    def from_attempt_state(cls, state: AttemptState) -> Outcome:
        """Map an :class:`AttemptState` to its chart code.

        Looks the state up in :data:`_ATTEMPT_STATE_TO_OUTCOME`, an explicit,
        exhaustive dict rather than an if/elif chain or a ``.get`` with a
        default. A member of ``AttemptState`` added later and not mirrored
        here raises :class:`~turing.research.contracts.ContractViolationError`
        naming it, instead of silently charting an unrecognised state as
        ``RUNNING`` — which is exactly the kind of quiet miscoding this
        module exists to prevent elsewhere.
        """
        try:
            return _ATTEMPT_STATE_TO_OUTCOME[state]
        except KeyError:
            raise ContractViolationError(
                f"AttemptState.{state.name} has no Outcome mapping; a new terminal or "
                "in-progress state must be added to _ATTEMPT_STATE_TO_OUTCOME in "
                "results.py before it can be charted, rather than defaulting to RUNNING"
            ) from None


#: The mapping from :meth:`Outcome.from_attempt_state`'s docstring. Verified
#: exhaustive against ``contracts.py``'s eight-member ``AttemptState`` on
#: 2026-08-14 — do not re-derive it without re-checking that file, since the
#: whole point of the classmethod's raise is to catch drift between the two.
_ATTEMPT_STATE_TO_OUTCOME: dict[AttemptState, Outcome] = {
    AttemptState.PENDING: Outcome.RUNNING,
    AttemptState.RUNNING: Outcome.RUNNING,
    AttemptState.VERIFYING: Outcome.RUNNING,
    AttemptState.PASSED: Outcome.SOLVED,
    AttemptState.FAILED_WITHIN_CAP: Outcome.FAILED_WITHIN_CAP,
    AttemptState.ESCALATED: Outcome.ESCALATED,
    AttemptState.ABANDONED: Outcome.ABANDONED,
    AttemptState.PAUSED: Outcome.PAUSED,
}


# --------------------------------------------------------------------------- #
# ProgressTracker
# --------------------------------------------------------------------------- #


def usable_target(target: float | None) -> float | None:
    """``target`` when it is a finite number, ``None`` when it is not.

    **A target that is not finite is not a target.** ``nan`` names no value,
    ``+inf`` names one no attempt can ever reach, and ``-inf`` names one every
    attempt has already met — none of the three is a bar, and there is no
    meaningful fraction of the way to any of them.
    :meth:`ProgressTracker.observe` reached that conclusion first and refuses
    to normalise against such a target; this function is the same judgement,
    pulled out so every artifact describing one attempt agrees on it instead
    of each deciding separately. Today that is :func:`write_attempt_summary`'s
    ``target_score`` field and the reference line
    :func:`~turing.research.loop.plots.render_attempt_plots` draws.

    The alternative — writing the raw non-finite value through — is what made
    an entirely honest attempt produce **no ``metrics.json`` at all**.
    ``json.dumps(..., allow_nan=False)`` (see :func:`_write_summary_json` for
    why that flag is not negotiable) raised ``ValueError`` on the bare ``nan``
    token; ``runner.py``'s sanctioned "a reporting failure must not kill a
    research run" guard caught it exactly as designed and logged
    ``research.results.emit_failed``; and the summary silently never landed.
    ``python -m turing.research.loop.verify`` then reported
    ``metrics.json is missing`` and exited ``1`` on a run that had done
    nothing wrong. A verifier that cries wolf teaches its operator to ignore
    it, so the fix is at the emitter, where the unrepresentable value is,
    rather than at the check.

    This does **not** make a non-finite target harmless — it makes it
    *recordable*. ``PassCriterion.min_score`` is not validated at
    construction the way its sibling ``VerificationResult.score`` is (see the
    ``math.isfinite`` guard in ``contracts.py``), so a ``min_score=-inf``
    criterion still auto-passes an attempt at its first verification, and no
    amount of care in the reporting layer can undo that. Reporting can only
    decline to write down a bar that was never a bar.
    """
    if target is None or not math.isfinite(target):
        return None
    return target


class ProgressTracker:
    """The normalised 0 -> 1 forcing-function reading ``.viewer.json`` points at.

    Runs whose underlying metric is named differently — a speedup ratio, a
    validation loss, a leaderboard percentile — are only comparable on one
    chart axis if that axis means the same thing for all of them. ``progress``
    is that axis: 0 at the untouched starting point, 1 at the problem's target,
    wherever the raw score happens to land in between.

    **The baseline is the first score ever passed to :meth:`observe`.** There
    is no baseline field anywhere in ``contracts.py``, and the brief for this
    module makes adding one an explicit non-goal: the first verification an
    attempt runs is against the freshly materialised, untouched workspace, and
    that verification result *is* the baseline by construction. Nothing else
    needs recording.

    Scores are higher-is-better by construction —
    ``PassCriterion.satisfied_by`` is ``result.score >= self.min_score`` — so
    this tracker does not, and must not, carry a direction enum. A verifier
    that natively measures a lower-is-better quantity is responsible for
    inverting it before it ever reaches here.
    """

    def __init__(self, *, target: float | None) -> None:
        self._target = target
        self._baseline: float | None = None
        self._warned_degenerate = False

    @property
    def baseline(self) -> float | None:
        """The first observed score, or ``None`` before the first call to :meth:`observe`."""
        return self._baseline

    def observe(self, score: float) -> float | None:
        """Record one verified score and return its normalised progress.

        **The baseline is recorded on every first call, even when ``target``
        is ``None``.** The baseline is a fact about the run — the first score
        it measured — and does not depend on whether a target exists to
        measure progress against. Setting it happens unconditionally, before
        the ``target is None`` check returns early; a prior version returned
        before recording it, which left :attr:`baseline` (and therefore the
        ``baseline_score`` reconcile derives) stuck at ``None`` for every
        problem with no ``PassCriterion``, even though the log plainly
        contained score values.

        Returns ``None`` when ``target`` is ``None`` (no pass criterion, or a
        criterion with no ``min_score``) — there is nothing to normalise
        against. Otherwise the first call returns ``0.0`` whenever ``target``
        is genuinely above the baseline: ``(score - baseline) / (target -
        baseline)`` is ``0 / (target - baseline) == 0.0`` when ``score ==
        baseline``, which is exactly what the first call measures.

        When ``target <= baseline`` the bar was already met before any work
        happened. Reporting ``1.0`` here would draw a solved-looking curve
        for an attempt that did nothing — the same fake-curve failure
        ``metrics.py`` refuses elsewhere with its noise-floor and score-scale
        gates. So this refuses too: it returns ``None`` for every subsequent
        observation on this tracker, and logs
        ``research.results.degenerate_target`` at ``WARNING`` exactly **once**
        per tracker instance, not once per step. An unattended overnight
        attempt can run hundreds of steps; a warning on every one of them
        would drown the log and teach the operator to ignore warnings, which
        defeats the point of emitting one at all.

        A non-finite ``target`` (``nan``, ``+inf``, ``-inf``) takes this same
        refusal path rather than reaching the arithmetic below: every
        comparison against ``nan`` is ``False`` in Python, so ``target <=
        baseline`` alone would never catch it, and ``max(0.0, min(1.0, nan))``
        silently resolves to ``1.0`` — a fabricated "solved" reading. See the
        ``isfinite`` check at the top of this method.
        """
        if self._baseline is None:
            self._baseline = score
        if self._target is None:
            return None
        baseline = self._baseline
        target = self._target
        # ``math.isfinite`` must be checked *before* the ``target <= baseline``
        # comparison, not folded into it. Every comparison against NaN is
        # False in Python, so ``nan <= baseline`` never trips the degenerate
        # guard below and execution falls through to the division, producing
        # ``raw = nan`` and then ``max(0.0, min(1.0, nan)) == 1.0`` — NaN-blind
        # because ``min``/``max`` are not NaN-aware in this argument order.
        # That fabricates a solved-looking ``progress: 1.0`` for an attempt
        # whose own outcome says it failed. A non-finite target (NaN, +inf,
        # -inf) is exactly as degenerate as one at or below baseline — there
        # is no meaningful fraction of the way to an unreachable or undefined
        # target — so it takes the same refusal path, not a separate one.
        if not math.isfinite(target) or target <= baseline:
            if not self._warned_degenerate:
                logger.warning(
                    "research.results.degenerate_target",
                    target=target,
                    baseline=baseline,
                )
                self._warned_degenerate = True
            return None
        raw = (score - baseline) / (target - baseline)
        return max(0.0, min(1.0, raw))


# --------------------------------------------------------------------------- #
# MetricsLine
# --------------------------------------------------------------------------- #


#: The keys :meth:`MetricsLine.to_json` always emits itself (beyond
#: :data:`RESERVED_FIELDS`). A caller-supplied ``metrics`` entry sharing one
#: of these names would silently overwrite a core field — or, read the other
#: way, a core field would silently clobber the caller's score — so it is
#: rejected instead. Defined in ``contracts.py`` for the reason given on
#: :data:`RESERVED_FIELDS` above; the emission *order* is the one this
#: module's :meth:`MetricsLine.to_json` writes, and lives there.
_CORE_EMITTED_KEYS: frozenset[str] = CORE_METRICS_FIELDS

_DIAG_PREFIX = DIAGNOSTIC_KEY_PREFIX


@dataclass(frozen=True, slots=True)
class MetricsLine:
    """One solver step, ready to be appended to an attempt's ``metrics.jsonl``.

    Every field the desktop needs to draw the cap-consumption chart is on
    this line **as of the step it describes**, not as of when the writer was
    constructed. In particular ``total_steps`` is a per-line value, not a
    writer-level constant: :meth:`~turing.research.contracts.Cap.extend` is
    the sanctioned way an operator's ``EXTEND_CAP`` decision grows a cap
    mid-attempt, so the budget in force can legitimately change between two
    lines in the same file. Recording it once at writer construction would
    make every line after an extension lie about the budget the attempt was
    actually running against.

    ``cap_extensions`` rides on every line for the same reason and one more:
    the ``Cap`` docstring is explicit that *"a project that reached its score
    after three extensions is not comparable to one that did it inside the
    original budget"*, and nothing before this module made that fact visible
    anywhere. Charting it as its own series is what lets a reviewer tell the
    two kinds of run apart at a glance instead of cross-referencing the
    escalation log by hand.

    ``correctness_pass`` and ``progress`` are the only genuinely optional
    fields on this line. Every cap field
    (``total_steps``/``tokens_cap``/``steps_cap``/``wall_clock_cap_s``) is
    always present, never omitted: ``Cap.__post_init__`` rejects any
    non-positive dimension, so there is no ``None`` state for these to be in
    and no "omit when unset" branch to write for them.

    The four per-step diagnostic fields — ``step_wall_clock_s``,
    ``verify_wall_clock_s``, ``step_tokens``, ``made_progress`` — exist
    because ``progress`` alone says *whether* a run is going well but not
    *why not*. A step that burns tokens with ``made_progress`` at 0, or a
    ``verify_wall_clock_s`` that dwarfs ``step_wall_clock_s``, is visible on
    the same chart as the score once these ride along — which is the point.
    All four are already in the runner's hands at the point it builds a line;
    none requires a change to ``contracts.py``.

    ``consumed_steps`` is **not** the same quantity as ``step`` and must
    never be derived from it. ``step`` is the solver's step index — the
    chart's x-axis. ``consumed_steps`` is cap accounting: how many steps the
    attempt's budget has actually been charged for. The two coincide on the
    happy path, which is exactly what makes conflating them dangerous — the
    runner's ``_charge_failed_step`` calls ``record_consumption(...)``, which
    bumps ``consumed.steps`` **without** bumping ``step_index``, because a
    proposal call that happened still costs a step even when parse/apply then
    failed. On that path a run legitimately reports ``step=0`` alongside
    ``consumed_steps=2``, and a reconciler that derived one from the other
    would raise a false alarm on perfectly honest data. So ``consumed_steps``
    rides on every line as its own field, immediately after ``steps_cap`` —
    next to the cap it is measured against — rather than being folded into
    ``step``'s meaning. This also removes a real asymmetry: ``tokens_used``
    and ``tokens_cap`` were always both present, while steps only ever
    emitted an index and a cap, with the consumed figure nowhere. It earns
    its own chart series independently, too — ``consumed_steps`` climbing
    while ``step`` stays flat is exactly the "burning budget without making
    progress" signal these diagnostics exist to surface.
    """

    step: int
    total_steps: int
    ts: float
    outcome: Outcome
    correctness_pass: bool | None
    tokens_used: int
    tokens_cap: int
    steps_cap: int
    consumed_steps: int
    wall_clock_s: float
    wall_clock_cap_s: float
    cap_extensions: int
    step_wall_clock_s: float
    verify_wall_clock_s: float
    step_tokens: int
    made_progress: bool | None
    progress: float | None
    metrics: Mapping[str, float]
    diagnostics: Mapping[str, float] = field(default_factory=dict)

    def to_json(self) -> dict[str, float | int]:
        """Build the JSON-ready dict for this line, in emission order.

        **Validation asymmetry, on purpose.** ``metrics`` is the scored
        payload — the number this attempt is graded on — and is validated
        strictly: a reserved-field collision, a core-key collision, an empty
        key, a ``diag_``-prefixed key, or a non-finite / non-numeric / bool
        value all raise :class:`~turing.research.contracts.ContractViolationError`
        naming the offending key.

        The runner's own metrics key can no longer trip the *key* checks: it
        names the key after the problem's ``score_scale``, and
        ``contracts._reject_unusable_score_scale`` refuses a colliding, empty
        or ``diag_``-prefixed scale where the verifier declares it, so a
        corpus with a bad scale fails before the round starts rather than
        losing an attempt at its first verification. These checks stay, and
        stay strict, for the same reason the key list is now a single shared
        one: they are the definition of what a metrics key may be, they hold
        for any caller that assembles ``metrics`` from something other than a
        verifier's declared scale, and a colliding scale arriving here would
        mean the two had drifted apart. Keep them in step.

        ``diagnostics`` is the agent's own notebook and is validated
        leniently: a bad entry there — non-numeric, ``bool``, non-finite, an
        empty key, or a prefixed form that would collide with a core key — is
        **dropped silently**, never raised. A malformed scored metric is a
        harness bug that must stop the run; a malformed diagnostic is the
        agent writing junk into its own workspace, and killing an unattended
        research run over that would be absurd. This is the brief's *"the
        agent invents; the operator holds the ruler"* rule expressed in code
        — the ruler (``metrics``) is strict, the notebook (``diagnostics``)
        is not. Do not "fix" this into symmetry; see the module docstring.

        ``bool`` is rejected explicitly in both mappings even though it is a
        subclass of ``int`` in Python, because it would otherwise chart as a
        0/1 series under a name that reads like a continuous metric. A caller
        that actually wants a 0/1 flag charted converts it itself.

        ``ts`` and ``wall_clock_s`` are checked for finiteness, and so are
        ``step_wall_clock_s``, ``verify_wall_clock_s``, ``step_tokens``, and
        (when not ``None``) ``progress`` — every numeric field on this line
        that is not already covered by :func:`_validate_scored_metric`. A
        non-finite value in any of these used to reach the JSONL file, where
        the desktop pane's tolerant parser silently drops the whole line
        rather than erroring, deleting a step from the chart with no signal
        anywhere. Raising here, before the line is ever serialised, is what
        keeps that failure loud instead of invisible.
        """
        if not math.isfinite(self.ts):
            raise ContractViolationError(f"ts must be finite, got {self.ts!r}")
        if not math.isfinite(self.wall_clock_s):
            raise ContractViolationError(f"wall_clock_s must be finite, got {self.wall_clock_s!r}")
        # These four were previously unguarded: MetricsPane.tsx's
        # parseMetricsText silently `continue`s past any line JSON.parse
        # rejects, so one non-finite float here used to delete the entire
        # step from the chart with no error anywhere. Fail here instead,
        # where the traceback still names the caller that built the line.
        if not math.isfinite(self.step_wall_clock_s):
            raise ContractViolationError(
                f"step_wall_clock_s must be finite, got {self.step_wall_clock_s!r}"
            )
        if not math.isfinite(self.verify_wall_clock_s):
            raise ContractViolationError(
                f"verify_wall_clock_s must be finite, got {self.verify_wall_clock_s!r}"
            )
        if not math.isfinite(self.step_tokens):
            raise ContractViolationError(f"step_tokens must be finite, got {self.step_tokens!r}")
        if self.progress is not None and not math.isfinite(self.progress):
            raise ContractViolationError(f"progress must be finite, got {self.progress!r}")

        result: dict[str, float | int] = {
            "step": self.step,
            "total_steps": self.total_steps,
            "ts": self.ts,
        }
        result["outcome_code"] = int(self.outcome)
        if self.correctness_pass is not None:
            result["correctness_pass"] = 1 if self.correctness_pass else 0
        result["tokens_used"] = self.tokens_used
        result["tokens_cap"] = self.tokens_cap
        result["steps_cap"] = self.steps_cap
        result["consumed_steps"] = self.consumed_steps
        result["wall_clock_s"] = self.wall_clock_s
        result["wall_clock_cap_s"] = self.wall_clock_cap_s
        result["cap_extensions"] = self.cap_extensions
        result["step_wall_clock_s"] = self.step_wall_clock_s
        result["verify_wall_clock_s"] = self.verify_wall_clock_s
        result["step_tokens"] = self.step_tokens
        if self.made_progress is not None:
            result["made_progress"] = 1 if self.made_progress else 0
        if self.progress is not None:
            result["progress"] = self.progress

        for key, value in self.metrics.items():
            _validate_scored_metric(key, value)
            result[key] = value

        for key, value in self.diagnostics.items():
            _maybe_add_diagnostic(result, key, value)

        return result


def _validate_scored_metric(key: str, value: object) -> None:
    """Raise unless ``key``/``value`` may safely enter the scored ``metrics`` namespace."""
    if key in RESERVED_FIELDS:
        raise ContractViolationError(
            f"metrics key {key!r} is reserved by the desktop as an axis/meta field "
            "(step, total_steps, ts) and cannot also be a scored series"
        )
    if key in _CORE_EMITTED_KEYS:
        raise ContractViolationError(
            f"metrics key {key!r} collides with a core field this line already emits"
        )
    if not key:
        raise ContractViolationError("metrics key must not be empty")
    if key.startswith(_DIAG_PREFIX):
        raise ContractViolationError(
            f"metrics key {key!r} uses the {_DIAG_PREFIX!r} prefix reserved for "
            "agent-authored diagnostics; a scored metric must not be able to enter "
            "the agent's own namespace"
        )
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractViolationError(
            f"metrics[{key!r}] must be a finite int or float, got {value!r}"
        )
    if not math.isfinite(value):
        raise ContractViolationError(f"metrics[{key!r}] must be finite, got {value!r}")


def _maybe_add_diagnostic(result: dict[str, float | int], key: str, value: object) -> None:
    """Add ``diag_<key>: value`` to ``result`` iff it is safe to; drop silently otherwise.

    See :meth:`MetricsLine.to_json` for why this never raises where
    :func:`_validate_scored_metric` does.
    """
    if not key:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return
    if not math.isfinite(value):
        return
    prefixed = key if key.startswith(_DIAG_PREFIX) else f"{_DIAG_PREFIX}{key}"
    if prefixed in _CORE_EMITTED_KEYS or prefixed in RESERVED_FIELDS:
        return
    result[prefixed] = value


# --------------------------------------------------------------------------- #
# MetricsWriter
# --------------------------------------------------------------------------- #


class MetricsWriter:
    """Appends :class:`MetricsLine` records to one attempt's ``metrics.jsonl``.

    **Append-only, by construction and by contract.** The desktop pane tails
    this file by byte offset (``fs_tail``); anything that rewrote earlier
    bytes would make the pane re-read garbage the next time it polls. Every
    call to :meth:`append` opens the file in ``"a"`` mode and writes exactly
    one line — never a rewrite, never a truncate.

    Each successful append also advances a sha256 hash chain
    (:mod:`turing.research.loop.integrity`) and rewrites the small sidecar
    file next to the JSONL. See :meth:`append` for the exact ordering, which
    is a correctness property, not a style choice.

    **Refuses to splice onto an existing chain.** ``__init__`` seeds
    ``chain_head`` from this writer's own ``header`` and starts ``_seq`` at
    0, unconditionally — so if ``path`` already holds lines chained from a
    *different* header (a prior, already-honest attempt at the same
    ``output_dir``), appending on top produces one file with two seeds
    spliced into it: the byte-for-byte same shape as real tampering, and
    — because the desktop pane tails and charts every parseable line with no
    knowledge of ``_chain`` — a single continuous curve drawn out of two
    attempts, with ``step`` and ``tokens_used`` resetting mid-chart while
    ``metrics.json`` holds only the second attempt's totals. Both attempts
    were completely honest; the file lies anyway. So this constructor
    refuses outright: a non-empty ``metrics.jsonl`` already on disk raises
    :class:`~turing.research.contracts.ContractViolationError` rather than
    starting a second chain over it, mirroring the refusal
    ``workspace.py``'s :class:`~turing.research.loop.workspace.CopyTreeWorkspaceProvider`
    already uses for the same shape of problem — *"already exists; an attempt must
    start from a fresh workspace."* A **missing** or **empty** file is not a
    chain and is not refused; those are exactly the states a fresh attempt
    directory is in.

    **The same refusal fires on a non-empty sidecar even with no log.**
    ``runner._rotate_stale_metrics`` moves the whole trio aside as one atomic
    set, but the *window* the crash lands in (a hard kill, a full disk,
    ``SIGKILL`` mid-rotation) can still leave this directory holding a real
    ``metrics.chain.json`` with no ``metrics.jsonl`` beside it — the residual
    a startup self-heal has not yet reached. That sidecar carries a real
    chain head and a real header naming a *different* attempt; a writer that
    only checked ``metrics.jsonl`` would see "missing" here, treat this as a
    fresh directory, and start appending — advancing the chain from *this*
    attempt's seed while the sidecar still claims the previous attempt's
    header, or getting silently overwritten by this attempt's own first
    append before anyone could tell the two apart. Either way the evidence
    that a rotation was left half-done disappears, which is strictly worse
    than refusing: the caller is expected to finish healing the leftover
    rotation (see :func:`~turing.research.loop.runner._rotate_stale_metrics`)
    before constructing a writer here, not to have this class paper over it.
    A **missing or empty** sidecar is not refused, for the same reason a
    missing or empty ``metrics.jsonl`` is not: both are exactly the states a
    fresh attempt directory is in.

    This constructor deliberately does **not** resume the prior chain from
    the sidecar. The header carries this attempt's own ``attempt_id`` and
    ``started_at_ms``, distinct from whatever produced the existing file;
    resuming would mix two attempts' step numbering into one chain while
    making it verify clean — strictly worse than failing loudly, because it
    would hide the exact problem this refusal exists to surface. Re-driving
    an attempt over a used ``output_dir`` is handled by the caller rotating
    the old files aside before constructing a new writer, not by this class
    growing resume logic.
    """

    def __init__(
        self,
        path: Path,
        *,
        header: Mapping[str, object],
        clock: Clock | None = None,
    ) -> None:
        sidecar_path = path.parent / integrity.CHAIN_SIDECAR_FILENAME
        jsonl_present = path.exists() and path.stat().st_size > 0
        sidecar_present = sidecar_path.exists() and sidecar_path.stat().st_size > 0
        if jsonl_present or sidecar_present:
            present = path.name if jsonl_present else sidecar_path.name
            raise ContractViolationError(
                f"{path.parent} already holds a non-empty {present!r}; a MetricsWriter "
                "must start from a fresh chain, not splice new lines onto one seeded by "
                "a different attempt, and not write beside a sidecar that names one — "
                "rotate the existing metrics.jsonl and its metrics.chain.json sidecar "
                "aside (as a matched pair) before constructing a new writer over this path"
            )
        self._path = path
        self._sidecar_path = sidecar_path
        self._header = dict(header)
        # Held only so this writer's constructor shape matches its neighbours
        # (``TrajectoryStore``) and so tests can inject a deterministic clock.
        # The writer never stamps ``ts`` itself — the caller puts it on the
        # ``MetricsLine`` — so ``self._clock`` is never actually read.
        self._clock = clock or SystemClock()
        self._seed_hash = integrity.seed_hash(self._header)
        self._chain_head = self._seed_hash
        self._seq = 0
        self._line_count = 0

    @property
    def path(self) -> Path:
        return self._path

    @property
    def line_count(self) -> int:
        return self._line_count

    @property
    def chain_head(self) -> str:
        """The current running digest — the seed hash before the first append."""
        return self._chain_head

    async def append(self, line: MetricsLine) -> None:
        """Serialise, chain, and append one line, then rewrite the sidecar.

        The whole thing runs as one :func:`asyncio.to_thread` call, in this
        order: build the JSON payload, chain it, add ``_chain`` **last** so
        it is provably not part of its own hash, append the JSONL line, write
        the sidecar, and only then advance the writer's counters.

        **This ordering is a correctness property.** If the JSONL write
        succeeds and the sidecar write then fails, the next verification
        reports a line-count mismatch — a visible, correct failure. If the
        counters advanced *before* the writes landed, a failed write would
        silently desynchronise the writer's idea of the chain from what is
        actually on disk, breaking every append after it in a way nothing
        would surface until someone ran ``verify``.

        A :class:`~turing.research.contracts.ContractViolationError` raised
        by ``line.to_json()`` propagates unchanged: it is not caught, no
        partial line is written, and nothing is logged-and-continued. A
        malformed metric is a bug in the caller, and swallowing it here would
        turn into a chart with a silent hole instead of an attempt that
        visibly stopped.
        """
        await asyncio.to_thread(self._append_sync, line)

    def _append_sync(self, line: MetricsLine) -> None:
        payload: dict[str, object] = dict(line.to_json())
        digest = integrity.chain_next(self._chain_head, payload)
        payload[integrity.CHAIN_FIELD] = f"{self._seq}:{digest}"

        self._path.parent.mkdir(parents=True, exist_ok=True)
        # allow_nan=False for the same reason _write_summary_json sets it:
        # MetricsLine.to_json already rejects every non-finite scored/reserved
        # field, so this should never actually fire in practice — but a JSONL
        # writer is exactly the kind of code a future edit adds a new numeric
        # field to without remembering the finiteness check, and this is the
        # backstop that turns that mistake into a loud write-time failure
        # instead of an unparseable line the desktop pane silently drops.
        with self._path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(payload, separators=(",", ":"), sort_keys=False, allow_nan=False)
            )
            handle.write("\n")

        new_lines = self._seq + 1
        sidecar = {
            "version": integrity.CHAIN_VERSION,
            "algorithm": integrity.CHAIN_ALGORITHM,
            "header": self._header,
            "seed": self._seed_hash,
            "final": digest,
            "lines": new_lines,
        }
        self._sidecar_path.write_text(
            json.dumps(sidecar, sort_keys=False, allow_nan=False) + "\n", encoding="utf-8"
        )

        self._chain_head = digest
        self._seq = new_lines
        self._line_count = new_lines


# --------------------------------------------------------------------------- #
# Agent diagnostics
# --------------------------------------------------------------------------- #


#: Where an agent may write its own intermediate numbers, relative to its
#: workspace. Optional by definition — an agent is never required to write
#: this file — which is why an absent file is not an error condition anywhere
#: in :func:`read_agent_diagnostics`.
AGENT_DIAGNOSTICS_RELPATH = Path(".turing") / "metrics.json"

_MAX_DIAGNOSTICS_KEYS = 50
_MAX_DIAGNOSTICS_FILE_BYTES = 256 * 1024


async def read_agent_diagnostics(workspace: Path) -> dict[str, float]:
    """Read an agent's own ``.turing/metrics.json``, tolerating every failure.

    The brief's policy: *"The agent builds its own validation splits,
    diagnostics, and internal measures freely... None may ever count as the
    official score."* The ``diag_`` prefix applied downstream by
    :meth:`MetricsLine.to_json` is what makes that structural rather than a
    promise; this function is what makes reading the file itself safe to call
    unattended, every step, of every attempt, forever.

    **Every failure mode returns ``{}``; none raises.** A missing file (the
    normal case — the agent is not required to write one), an unreadable
    file, invalid JSON, valid JSON that is not an object, a permission error,
    or a dangling symlink are all just "no diagnostics this step". This file
    is optional by definition, and a research run must never die because the
    agent wrote a broken notebook.

    Keys are returned **unprefixed** — :meth:`MetricsLine.to_json` applies
    ``diag_``. Only ``int``/``float`` values that are not ``bool`` and are
    finite survive; the rest are dropped without individual logging (see
    :meth:`MetricsLine.to_json` for the matching rule on the scored side).

    The result is capped at 50 keys, taken in **sorted key order** for
    determinism, because an agent that writes thousands of series would
    otherwise make every line enormous and the pane unusable; truncation
    logs once at ``WARNING`` (event ``research.results.diagnostics_truncated``)
    with the count actually seen. A file over 256 KiB is rejected **without
    being parsed**, logging once at ``WARNING``
    (``research.results.diagnostics_too_large``) — the agent writes into this
    path unattended, so an unbounded read here would be a denial-of-service
    on the operator's own loop. A simply-absent file logs at ``DEBUG``, not
    ``WARNING``: absent is the common case, and a warning every step would
    train the operator to ignore warnings.
    """
    return await asyncio.to_thread(_read_agent_diagnostics_sync, workspace)


def _read_agent_diagnostics_sync(workspace: Path) -> dict[str, float]:
    path = workspace / AGENT_DIAGNOSTICS_RELPATH
    try:
        exists = path.is_file()
    except OSError:
        return {}
    if not exists:
        logger.debug("research.results.diagnostics_absent", path=str(path))
        return {}

    try:
        size = path.stat().st_size
    except OSError:
        return {}
    if size > _MAX_DIAGNOSTICS_FILE_BYTES:
        logger.warning(
            "research.results.diagnostics_too_large",
            path=str(path),
            size_bytes=size,
            limit_bytes=_MAX_DIAGNOSTICS_FILE_BYTES,
        )
        return {}

    try:
        raw_text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        parsed = json.loads(raw_text)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}

    valid: dict[str, float] = {}
    for key, value in parsed.items():
        if not isinstance(key, str) or not key:
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if not math.isfinite(value):
            continue
        valid[key] = float(value)

    if len(valid) > _MAX_DIAGNOSTICS_KEYS:
        logger.warning(
            "research.results.diagnostics_truncated",
            path=str(path),
            total_keys=len(valid),
            kept=_MAX_DIAGNOSTICS_KEYS,
        )

    return dict(sorted(valid.items())[:_MAX_DIAGNOSTICS_KEYS])


# --------------------------------------------------------------------------- #
# Summaries and viewer config
# --------------------------------------------------------------------------- #


def _write_summary_json(path: Path, payload: dict[str, object]) -> None:
    """The write idiom shared by every whole-file JSON writer in this module.

    Deliberately **not** ``trajectory._write_json``'s tmp-file-then-``replace``
    idiom: these are not append-only logs another process tails by byte
    offset, they are small summaries rewritten wholesale, and the spec this
    module implements calls for the same to_thread/mkdir/``json.dumps``
    shape :meth:`MetricsWriter.append` already uses.

    ``allow_nan=False`` for the same reason ``trajectory._write_json`` sets
    it: Python's ``json`` module emits bare ``NaN``/``Infinity`` tokens by
    default, which is not valid JSON — every other reader, including the
    desktop, rejects the file outright, while Python's own ``json.loads``
    happily accepts it back, so a reconcile check reading the file with the
    same library would report a clean chain over a file nothing else can
    parse. Better to fail here, at the write, where the traceback still
    names the caller, than to ship an unreadable file with a passing verdict.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=False, allow_nan=False) + "\n", encoding="utf-8"
    )


async def write_attempt_summary(
    directory: Path,
    *,
    problem_id: str,
    attempt_id: str,
    round_id: str,
    seed: int,
    problem_type: str,
    split: str,
    score_scale: str,
    outcome: Outcome,
    final_state: str,
    best_score: float | None,
    best_passed_correctness: bool | None,
    baseline_score: float | None,
    target_score: float | None,
    final_progress: float | None,
    consumed_steps: int,
    consumed_tokens: int,
    consumed_wall_clock_seconds: float,
    cap_extensions: int,
    escalation_count: int,
    steps_recorded: int,
) -> Path:
    """Write ``directory / "metrics.json"`` — the citable, non-chart summary of one attempt.

    A JSON **object**, not an array: the desktop's tolerant ``parseMetricsText``
    parses a bare object to zero chart points, which is intended — this file
    is read by a human or a reconciliation check, not drawn as a series.
    ``schema_version`` is written first, then one key per argument in the
    order the arguments are declared above, so this function's own source is
    the authoritative field order.

    **``steps_recorded`` counts lines in ``metrics.jsonl``, not solver
    steps.** Edit D appends one final metrics line carrying the attempt's
    terminal outcome after the in-loop lines are done, so a run's log always
    has one more line than it has in-loop steps. ``steps_recorded`` therefore
    legitimately **exceeds ``consumed_steps`` by one** on every honest run —
    that is not a bug, and a reconciler must not treat it as one.

    **A non-finite ``target_score`` is written as ``null``, not raised on.**
    ``PassCriterion.min_score`` is the one number reaching this function that
    nothing upstream checks for finiteness — ``VerificationResult.score``
    (the source of ``best_score`` and ``baseline_score``) is guarded in
    ``contracts.py``, and ``final_progress`` is clamped into ``[0, 1]`` or
    ``None`` by :meth:`ProgressTracker.observe`. Left raw it made
    ``json.dumps(..., allow_nan=False)`` raise, which ``runner.py``'s
    sanctioned reporting guard swallowed, which left an entirely honest
    attempt with **no ``metrics.json`` at all** and ``verify`` reporting
    ``metrics.json is missing``. See :func:`usable_target` for the full
    argument and for why ``None`` is the honest value rather than a
    placeholder: :meth:`ProgressTracker.observe` has already refused to
    normalise against such a target, so ``final_progress`` is ``null`` and no
    line carries ``progress``; recording a ``target_score`` next to that
    would have the summary claim a bar the rest of the run did not measure
    against.

    Dropping it is **not** silent. ``research.results.target_score_dropped``
    is logged once at ``WARNING``, deliberately under a different event name
    from :meth:`ProgressTracker.observe`'s ``research.results.degenerate_target``
    so the two do not have to be told apart by counting: the tracker's fires
    only once a score has actually been observed, so an attempt whose solver
    died before its first verification would otherwise record a garbage
    criterion nowhere at all.
    """
    resolved_target = usable_target(target_score)
    if target_score is not None and resolved_target is None:
        logger.warning(
            "research.results.target_score_dropped",
            problem_id=problem_id,
            attempt_id=attempt_id,
            # ``repr`` rather than the float itself: a structlog JSON renderer
            # serialises with the stdlib too, and handing it the same bare
            # ``nan`` that made this file unwritable would put an unparseable
            # token in the log reporting the unparseable token.
            target_score=repr(target_score),
        )

    payload: dict[str, object] = {
        "schema_version": 1,
        "problem_id": problem_id,
        "attempt_id": attempt_id,
        "round_id": round_id,
        "seed": seed,
        "problem_type": problem_type,
        "split": split,
        "score_scale": score_scale,
        "outcome": int(outcome),
        "final_state": final_state,
        "best_score": best_score,
        "best_passed_correctness": best_passed_correctness,
        "baseline_score": baseline_score,
        "target_score": resolved_target,
        "final_progress": final_progress,
        "consumed_steps": consumed_steps,
        "consumed_tokens": consumed_tokens,
        "consumed_wall_clock_seconds": consumed_wall_clock_seconds,
        "cap_extensions": cap_extensions,
        "escalation_count": escalation_count,
        "steps_recorded": steps_recorded,
    }
    path = directory / "metrics.json"
    await asyncio.to_thread(_write_summary_json, path, payload)

    # After the summary, never before: `verify_run` reconciles `metrics.json`
    # against the log, so a check run one line earlier would report every
    # honest attempt as INCOMPLETE ("summary is missing") forever.
    #
    # Guarded, because this is reporting *about* reporting. The summary is
    # already on disk and the caller's plots still have to be drawn; a verdict
    # that could not be produced must degrade to "no verdict file", which the
    # pane shows as `unverified`, rather than take the emission block down
    # with it.
    try:
        await write_attempt_verdict(directory)
    except Exception:
        logger.exception("research.results.verdict_failed", directory=str(directory))

    return path


async def write_attempt_verdict(directory: Path, *, checked_by: str = "loop") -> Path | None:
    """Record what ``verify`` says about ``directory``, for a reader that cannot run it.

    The desktop's metrics pane reads files out of the results root; it has no
    Python, so it cannot recompute a hash chain and has never had any way to
    tell an operator whether the curve on screen verifies. This writes the
    answer down at the one moment it is cheap and unambiguous — the attempt is
    over, nothing is appending — and the pane reads it (see
    ``webui/src/desktop/panes/metrics.ts``, ``parseVerdictFile``).

    **Not a second verifier.** Every field comes from
    :func:`~turing.research.loop.verify.verify_run` and
    :func:`~turing.research.loop.verify.format_run_verdict`, the same two
    functions ``python -m turing.research.loop.verify`` calls. A reimplemented
    check here would eventually disagree with the CLI, and an operator holding
    a green badge and a red terminal would have no way to decide which to
    believe.

    **A directory with no ``metrics.jsonl`` gets no verdict at all** and this
    returns ``None``. ``verify.find_runs`` keys on that file, so such a
    directory is not a run; stamping ``failed`` ("metrics.chain.json is
    missing") on it would manufacture a finding about a run that does not
    exist — the cries-wolf failure
    :mod:`turing.research.loop.integrity` exists to avoid, arriving by a new
    route.

    ``lines_checked`` is the field the badge turns on: the pane compares it
    against the number of lines it parsed itself, and shows ``stale`` when
    they differ, because a verdict about 40 lines says nothing about the 41st.
    ``chain_head`` is the sidecar's recorded final digest, copied through for
    an operator correlating two reports of the same run; it is absent when the
    sidecar cannot be read, which is itself one of the states ``verify``
    reports. ``note`` carries
    :data:`~turing.research.loop.verify.HONESTY_LINE` verbatim, for the same
    reason the CLI prints it on every invocation: a bare ``"state": "ok"``
    would be read as proof of authenticity that no chain in this program can
    provide.

    The verdict file is **not** part of anything it reports on — see
    :data:`METRICS_VERDICT_FILENAME`. It is rewritten wholesale on every call,
    so a re-emitted summary never leaves an older verdict beside a newer log.
    """
    if not (directory / "metrics.jsonl").exists():
        return None

    verdict = await verify_run(directory)
    payload: dict[str, object] = {
        "schema_version": 1,
        "state": verdict.state.value,
        "lines_checked": 0 if verdict.chain is None else verdict.chain.lines_checked,
        "checked_at_ms": SystemClock().now_ms(),
        "checked_by": checked_by,
        "chain_head": await asyncio.to_thread(_read_chain_head, directory),
        "detail": format_run_verdict(verdict),
        "note": HONESTY_LINE,
    }
    path = directory / METRICS_VERDICT_FILENAME
    await asyncio.to_thread(_write_summary_json, path, payload)
    return path


def _read_chain_head(directory: Path) -> str | None:
    """The sidecar's recorded final digest, or ``None`` if it cannot be read.

    Read rather than recomputed on purpose: this is a *label* for correlating
    two reports about the same run, not evidence. Whether the recorded head is
    the one the log actually produces is exactly the question
    :func:`~turing.research.loop.integrity.verify_metrics_chain` already
    answered above, and its answer is the ``state`` field.
    """
    try:
        raw = json.loads((directory / integrity.CHAIN_SIDECAR_FILENAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if isinstance(raw, dict) and isinstance(raw.get("final"), str):
        final: str = raw["final"]
        return final
    return None


async def write_round_summary(
    directory: Path,
    *,
    round_index: int,
    run_id: str,
    parent_round_id: str | None,
    eval_set_hash: str,
    seed: int,
    comparable_to_parent: bool | None,
    cells: Sequence[Mapping[str, object]],
    wall_clock_seconds: float,
    tokens: int,
    attempts: int,
    escalations: int,
    verdict: str,
) -> Path:
    """Write ``directory / "metrics.json"`` — the per-cell summary of one round.

    ``cells`` is written through **verbatim**, one entry per ``(problem_type,
    split)`` cell the caller built from ``TypeScore``/``RoundDelta``. This
    function does not reduce, average, or pool it, and must never grow a
    corpus-wide scalar field: ``RoundRecord`` itself has no aggregate score
    for exactly the reason ``metrics.py``'s module docstring gives — a speedup
    ratio and a leaderboard percentile do not share a scale, and a round that
    helps one family while hurting another would read as flat once blended.

    ``comparable_to_parent`` is ``None``, not ``True``, for a round with no
    parent (round 0): "comparable to parent" is not a yes/no fact about a
    round that has no parent to be comparable to, and ``None`` is what
    ``trajectory.py``'s ``append_round`` already writes for that same round
    in ``trajectory.json``. The two files must agree on this field.
    """
    payload: dict[str, object] = {
        "schema_version": 1,
        "round_index": round_index,
        "run_id": run_id,
        "parent_round_id": parent_round_id,
        "eval_set_hash": eval_set_hash,
        "seed": seed,
        "comparable_to_parent": comparable_to_parent,
        "cells": [dict(cell) for cell in cells],
        "wall_clock_seconds": wall_clock_seconds,
        "tokens": tokens,
        "attempts": attempts,
        "escalations": escalations,
        "verdict": verdict,
    }
    path = directory / "metrics.json"
    await asyncio.to_thread(_write_summary_json, path, payload)
    return path


async def write_viewer_config(
    results_root: Path,
    *,
    runs: Sequence[str],
    primary_series: str = "progress",
    titles: Mapping[str, str] | None = None,
) -> Path:
    """Write ``results_root / ".viewer.json"``, pointing the desktop's metrics pane.

    ``progress`` is the default primary series precisely because it is the
    one axis every run shares regardless of what its raw ``score_scale`` is
    called. ``runs`` are expected to already be relative-to-``results_root``,
    POSIX-separated path strings — that conversion is the caller's job (the
    round runner knows its own directory layout); this function writes them
    through unchanged.
    """
    resolved_titles: dict[str, str] = (
        {"progress": "progress toward target (0 = baseline, 1 = target)"}
        if titles is None
        else dict(titles)
    )
    payload: dict[str, object] = {
        "series": primary_series,
        "runs": list(runs),
        "titles": resolved_titles,
    }
    path = results_root / ".viewer.json"
    await asyncio.to_thread(_write_summary_json, path, payload)
    return path


# --------------------------------------------------------------------------- #
# Reading metrics back
# --------------------------------------------------------------------------- #


async def read_metrics_points(path: Path) -> tuple[dict[str, float], ...]:
    """Read a ``metrics.jsonl`` file back as plot-ready points.

    Used by the runner to hand :func:`~turing.research.loop.plots.render_attempt_plots`
    the same numbers it just wrote, without re-parsing inline at the call
    site. Blank lines and lines that fail to parse as a JSON object are
    skipped silently, matching the desktop pane's own tolerant reader — this
    function is a plotting input, not a validator, and the line that wrote
    this file already validated it once via :meth:`MetricsLine.to_json`. Only
    finite ``int``/``float`` values survive per point; a missing file yields
    an empty tuple rather than raising, for the same reason.
    """
    return await asyncio.to_thread(_read_metrics_points_sync, path)


def _read_metrics_points_sync(path: Path) -> tuple[dict[str, float], ...]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ()

    points: list[dict[str, float]] = []
    for raw_line in text.split("\n"):
        if not raw_line:
            continue
        try:
            parsed = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if not isinstance(parsed, dict):
            continue
        point: dict[str, float] = {}
        for key, value in parsed.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if not math.isfinite(value):
                continue
            point[key] = float(value)
        points.append(point)
    return tuple(points)
