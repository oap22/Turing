"""Tests for the tamper-evidence layer — ``turing.research.loop.integrity``.

The tampering tests are the point of this file: a chain module whose suite
only exercises the happy path has verified nothing about what it claims to
detect. Every attack below builds a chain with the module's own primitives
(so the fixtures never depend on ``results.MetricsWriter``, which lives in a
different file and is not under test here), then damages it exactly the way
an editor, a `sed`, or a crash would, and asserts the damage is caught.

One test is the deliberate exception: :func:`test_a_consistently_forged_file_verifies_clean`
tampers with the log **and** rewrites the sidecar and header to match, and
asserts the result is a *clean* verdict. That is not a bug in the chain — it
is the R2 hole the module docstring names. The test exists so nobody six
months from now reads a green ``verify_metrics_chain`` result as proof of
authenticity.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
from typing import TYPE_CHECKING, Any

import pytest

from turing.research.contracts import (
    ContractViolationError,
    EscalationReason,
    EscalationVerdict,
)
from turing.research.loop import integrity as integrity_module
from turing.research.loop import results as results_module
from turing.research.loop import verify
from turing.research.loop.integrity import (
    CHAIN_ALGORITHM,
    CHAIN_FIELD,
    CHAIN_SIDECAR_FILENAME,
    CHAIN_VERSION,
    ChainState,
    ChainVerdict,
    ReconcileState,
    ReconcileVerdict,
    canonical_json,
    chain_next,
    reconcile_summary,
    seed_hash,
    verify_metrics_chain,
)
from turing.research.loop.protocols import SolverStep

from .conftest import (
    FakeSolver,
    ScriptedEscalationChannel,
    make_config,
    make_problem,
    make_runner,
)

if TYPE_CHECKING:
    from pathlib import Path

    from turing.research.loop.trajectory import TrajectoryStore

    from .conftest import FakeClock, TempWorkspaceProvider

# --------------------------------------------------------------------------- #
# Fixture builders — deliberately independent of results.MetricsWriter
# --------------------------------------------------------------------------- #


def _header(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "attempt_id": "attempt-1",
        "problem_id": "problem-1",
        "round_id": "round-00",
        "seed": 7,
        "score_scale": "speedup",
        "started_at_ms": 1_700_000_000_000,
    }
    base.update(overrides)
    return base


def _line(step: int, **overrides: object) -> dict[str, object]:
    """A minimal, well-formed metrics line body (no ``_chain`` yet).

    ``consumed_steps`` defaults to ``step`` — the two coincide on the happy
    path this fixture models. A test that needs the round-2 divergence (a
    failed proposal call that costs a step without advancing ``step_index``)
    passes ``consumed_steps=`` explicitly as an override.
    """
    base: dict[str, object] = {
        "step": step,
        "total_steps": 50,
        "ts": 1_700_000_000.0 + step,
        "outcome_code": 0,
        "tokens_used": 1000 * (step + 1),
        "tokens_cap": 1_000_000,
        "steps_cap": 50,
        "consumed_steps": step,
        "wall_clock_s": 10.0 * (step + 1),
        "wall_clock_cap_s": 10800.0,
        "cap_extensions": 0,
        "step_wall_clock_s": 8.0,
        "verify_wall_clock_s": 2.0,
        "step_tokens": 1000,
        "speedup": 1.0 + 0.1 * step,
    }
    base.update(overrides)
    return base


def _build_chain(
    directory: Path, header: dict[str, object], lines: list[dict[str, object]]
) -> None:
    """Write a genuine ``metrics.jsonl`` + sidecar pair, the way a writer would.

    Mirrors ``MetricsWriter.append``'s contract exactly: the digest is taken
    over the payload with ``_chain`` absent, the chain field is added last as
    ``f"{seq}:{digest}"``, and the sidecar is rewritten with the final state
    after every line.
    """
    directory.mkdir(parents=True, exist_ok=True)
    chain_head = seed_hash(header)
    serialised_lines: list[str] = []
    for seq, body in enumerate(lines):
        digest = chain_next(chain_head, body)
        record = dict(body)
        record[CHAIN_FIELD] = f"{seq}:{digest}"
        serialised_lines.append(json.dumps(record, separators=(",", ":")))
        chain_head = digest
    (directory / "metrics.jsonl").write_text(
        "".join(line + "\n" for line in serialised_lines), encoding="utf-8"
    )
    sidecar = {
        "version": CHAIN_VERSION,
        "algorithm": CHAIN_ALGORITHM,
        "header": header,
        "seed": seed_hash(header),
        "final": chain_head,
        "lines": len(lines),
    }
    (directory / CHAIN_SIDECAR_FILENAME).write_text(json.dumps(sidecar), encoding="utf-8")


def _read_sidecar(directory: Path) -> dict[str, object]:
    parsed: dict[str, object] = json.loads(
        (directory / CHAIN_SIDECAR_FILENAME).read_text(encoding="utf-8")
    )
    return parsed


def _write_sidecar(directory: Path, sidecar: dict[str, object]) -> None:
    (directory / CHAIN_SIDECAR_FILENAME).write_text(json.dumps(sidecar), encoding="utf-8")


def _read_jsonl_lines(directory: Path) -> list[str]:
    text = (directory / "metrics.jsonl").read_text(encoding="utf-8")
    return text.splitlines()


def _write_jsonl_lines(directory: Path, lines: list[str]) -> None:
    (directory / "metrics.jsonl").write_text(
        "".join(line + "\n" for line in lines), encoding="utf-8"
    )


def _write_raw_jsonl(directory: Path, lines: list[dict[str, object]]) -> None:
    """Write a bare ``metrics.jsonl`` with no chain machinery — for reconcile tests."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "metrics.jsonl").write_text(
        "".join(json.dumps(line, separators=(",", ":")) + "\n" for line in lines),
        encoding="utf-8",
    )


def _write_summary(directory: Path, summary: dict[str, object]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "metrics.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")


def _clean_verifiable_run(directory: Path, *, n: int = 3) -> None:
    """Write a run directory that passes *both* ``verify_metrics_chain`` and
    ``reconcile_summary`` cleanly — used only by the CLI tests below, which
    need a directory the real ``verify`` tool calls a genuine ``OK`` on.

    ``metrics.json`` is derived straight from ``lines`` (not hand-typed
    separately) so this stays honest as the ``_line`` fixture evolves — the
    same failure mode this file exists to catch elsewhere.
    """
    header = _header()
    lines = [_line(i) for i in range(n)]
    _build_chain(directory, header, lines)
    last = lines[-1]
    score_values = [line["speedup"] for line in lines]
    summary: dict[str, object] = {
        "schema_version": 1,
        "steps_recorded": n,
        "consumed_steps": last["consumed_steps"],
        "consumed_tokens": last["tokens_used"],
        "consumed_wall_clock_seconds": last["wall_clock_s"],
        "cap_extensions": last["cap_extensions"],
        "final_progress": None,
        "outcome": last["outcome_code"],
        "baseline_score": score_values[0],
        "best_score": score_values[-1],
    }
    _write_summary(directory, summary)


def _unfinished_verifiable_run(directory: Path, *, n: int = 3) -> None:
    """A run whose chain is intact and whose ``metrics.json`` was never written.

    Deliberately *builds* that shape rather than writing a summary and
    deleting it: this is what an attempt looks like on disk between its first
    metrics append and its single end-of-attempt summary write, and a fixture
    that reached it by deletion would model a different (and less honest)
    story than the one the INCOMPLETE state exists for.
    """
    _build_chain(directory, _header(), [_line(i) for i in range(n)])


def _tamper_one_value(directory: Path, *, index: int = 1) -> None:
    """Mutate one field on one line, the way `test_mutating_one_value...` does,
    so the chain no longer verifies. Used by the CLI's tampered-directory tests.
    """
    raw_lines = _read_jsonl_lines(directory)
    payload = json.loads(raw_lines[index])
    payload["tokens_used"] = payload["tokens_used"] + 1
    raw_lines[index] = json.dumps(payload, separators=(",", ":"))
    _write_jsonl_lines(directory, raw_lines)


# --------------------------------------------------------------------------- #
# Mid-append fixtures — these ones DO go through results.MetricsWriter
# --------------------------------------------------------------------------- #


def _real_metrics_line(step: int) -> results_module.MetricsLine:
    """One ``MetricsLine`` for the real writer, in this file's ``_line`` shape."""
    return results_module.MetricsLine(
        step=step,
        total_steps=50,
        ts=1_700_000_000.0 + step,
        outcome=results_module.Outcome.RUNNING,
        correctness_pass=None,
        tokens_used=1000 * (step + 1),
        tokens_cap=1_000_000,
        steps_cap=50,
        consumed_steps=step,
        wall_clock_s=10.0 * (step + 1),
        wall_clock_cap_s=10_800.0,
        cap_extensions=0,
        step_wall_clock_s=8.0,
        verify_wall_clock_s=2.0,
        step_tokens=1000,
        made_progress=None,
        progress=None,
        metrics={"speedup": 1.0 + 0.1 * step},
        diagnostics={},
    )


async def _writer_snapshots(directory: Path, *, appends: int) -> list[tuple[str, str]]:
    """Drive a **real** ``MetricsWriter`` and snapshot the pair after each append.

    Every byte the mid-append tests put on disk comes from here rather than
    from this file's hand-rolled ``_build_chain``. That matters for exactly
    this defect: the shape under test is one the *writer* produces, in the gap
    between its two writes, and a hand-built approximation of it would be
    testing the fixture's idea of the race instead of the writer's.

    Returns ``[(jsonl_text, sidecar_text)]`` — index ``k`` is the pair as it
    stood after ``k + 1`` appends. Leaves the directory holding the last
    (fully consistent) snapshot.
    """
    writer = results_module.MetricsWriter(directory / "metrics.jsonl", header=_header())
    snapshots: list[tuple[str, str]] = []
    for step in range(appends):
        await writer.append(_real_metrics_line(step))
        snapshots.append(
            (
                (directory / "metrics.jsonl").read_text(encoding="utf-8"),
                (directory / CHAIN_SIDECAR_FILENAME).read_text(encoding="utf-8"),
            )
        )
    return snapshots


def _writer_snapshots_sync(directory: Path, *, appends: int) -> list[tuple[str, str]]:
    """:func:`_writer_snapshots` for the plain (non-``async``) CLI tests.

    ``verify.main()`` calls ``asyncio.run()`` itself, so the tests that
    exercise the real exit code cannot be coroutines — see
    ``_run_verify_cli_subprocess`` below for the same constraint stated at
    length. They still need the writer's genuine bytes, so the driving loop
    is opened here instead.
    """
    return asyncio.run(_writer_snapshots(directory, appends=appends))


def _put_pair(directory: Path, jsonl_text: str, sidecar_text: str) -> None:
    (directory / "metrics.jsonl").write_text(jsonl_text, encoding="utf-8")
    (directory / CHAIN_SIDECAR_FILENAME).write_text(sidecar_text, encoding="utf-8")


# --------------------------------------------------------------------------- #
# canonical_json
# --------------------------------------------------------------------------- #


class TestCanonicalJson:
    def test_key_order_does_not_affect_the_output(self) -> None:
        a = canonical_json({"b": 1, "a": 2})
        b = canonical_json({"a": 2, "b": 1})
        assert a == b

    def test_a_different_value_produces_different_output(self) -> None:
        a = canonical_json({"a": 1})
        b = canonical_json({"a": 2})
        assert a != b

    def test_int_and_float_of_the_same_magnitude_are_not_the_same_text(self) -> None:
        """1 and 1.0 are ``==`` in Python but must not hash identically.

        A caller that silently coerced an int metric to float (or vice
        versa) between two runs would otherwise be invisible to the chain.
        """
        assert canonical_json({"a": 1}) != canonical_json({"a": 1.0})

    def test_negative_and_positive_zero_are_not_the_same_text(self) -> None:
        """A known ``json.dumps`` quirk, not a bug: ``-0.0`` and ``0.0`` are
        ``==`` in Python but ``repr``-different in JSON text, so they
        canonicalise — and therefore hash — differently. Documented here so
        nobody "fixes" it later.
        """
        assert canonical_json({"a": -0.0}) != canonical_json({"a": 0.0})

    def test_a_very_large_integer_round_trips_exactly(self) -> None:
        huge = 10**30 + 7
        text = canonical_json({"a": huge})
        assert str(huge) in text
        assert json.loads(text) == {"a": huge}

    def test_nan_and_infinity_are_emitted_as_bare_non_json_tokens(self) -> None:
        """``canonical_json`` does not guard against non-finite floats — that
        is ``MetricsLine.to_json``'s job, upstream of this module (see the
        module and function docstrings). Documented here as a probe, not a
        requirement: this asserts the (unsafe) default behaviour so nobody
        is surprised by it later.
        """
        text = canonical_json({"a": float("nan"), "b": float("inf"), "c": float("-inf")})
        assert '"a":NaN' in text
        assert '"b":Infinity' in text
        assert '"c":-Infinity' in text
        with pytest.raises(json.JSONDecodeError):
            # A strict JSON parser (unlike Python's permissive json.loads
            # default) would reject this — most non-Python consumers will.
            json.loads(text, parse_constant=_reject_constant)

    def test_unicode_is_ascii_escaped(self) -> None:
        text = canonical_json({"a": "café"})
        assert "café" not in text
        assert "\\u00e9" in text
        assert json.loads(text) == {"a": "café"}

    def test_matches_the_documented_json_dumps_call(self) -> None:
        payload = {"z": 1, "a": [1, 2, 3], "m": {"x": 1}}
        assert canonical_json(payload) == json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )


def _reject_constant(value: str) -> float:
    raise json.JSONDecodeError(f"non-finite constant {value!r} is not valid JSON", value, 0)


# --------------------------------------------------------------------------- #
# seed_hash / chain_next
# --------------------------------------------------------------------------- #


class TestSeedAndChainNext:
    def test_seed_hash_is_the_sha256_of_the_canonical_header(self) -> None:
        header = _header()
        expected = hashlib.sha256(canonical_json(header).encode("utf-8")).hexdigest()
        assert seed_hash(header) == expected

    def test_chain_next_raises_when_the_payload_already_carries_a_chain_field(self) -> None:
        with pytest.raises(ContractViolationError, match=CHAIN_FIELD):
            chain_next("deadbeef", {"step": 0, CHAIN_FIELD: "0:deadbeef"})

    def test_chain_next_is_deterministic_and_sensitive_to_the_previous_digest(self) -> None:
        payload = {"step": 0, "tokens_used": 10}
        first = chain_next("aaa", payload)
        again = chain_next("aaa", payload)
        different_previous = chain_next("bbb", payload)
        assert first == again
        assert first != different_previous
        assert len(first) == 64  # sha256 hex digest


# --------------------------------------------------------------------------- #
# verify_metrics_chain — the happy path
# --------------------------------------------------------------------------- #


class TestVerifyMetricsChainHappyPath:
    async def test_a_clean_chain_of_n_lines_verifies_ok_with_lines_checked_equal_to_n(
        self, tmp_path: Path
    ) -> None:
        header = _header()
        lines = [_line(i) for i in range(6)]
        _build_chain(tmp_path, header, lines)

        verdict = await verify_metrics_chain(tmp_path)

        assert verdict == ChainVerdict(
            state=ChainState.OK, lines_checked=6, first_bad_index=None, reason=verdict.reason
        )
        assert verdict.ok is True

    async def test_an_empty_jsonl_with_a_sidecar_reporting_zero_lines_is_ok(
        self, tmp_path: Path
    ) -> None:
        _build_chain(tmp_path, _header(), [])

        verdict = await verify_metrics_chain(tmp_path)

        assert verdict.ok is True
        assert verdict.lines_checked == 0
        assert verdict.first_bad_index is None


# --------------------------------------------------------------------------- #
# verify_metrics_chain — tampering
# --------------------------------------------------------------------------- #


class TestVerifyMetricsChainTampering:
    async def test_mutating_one_value_in_the_middle_is_caught_at_that_line(
        self, tmp_path: Path
    ) -> None:
        _build_chain(tmp_path, _header(), [_line(i) for i in range(5)])
        raw_lines = _read_jsonl_lines(tmp_path)
        payload = json.loads(raw_lines[2])
        payload["tokens_used"] = payload["tokens_used"] + 1  # value changed, digest left stale
        raw_lines[2] = json.dumps(payload, separators=(",", ":"))
        _write_jsonl_lines(tmp_path, raw_lines)

        verdict = await verify_metrics_chain(tmp_path)

        assert verdict.ok is False
        assert verdict.first_bad_index == 2

    async def test_deleting_an_interior_line_is_caught_at_the_first_divergent_position(
        self, tmp_path: Path
    ) -> None:
        _build_chain(tmp_path, _header(), [_line(i) for i in range(5)])
        raw_lines = _read_jsonl_lines(tmp_path)
        del raw_lines[2]  # sidecar still claims 5 lines; jsonl now has 4
        _write_jsonl_lines(tmp_path, raw_lines)

        verdict = await verify_metrics_chain(tmp_path)

        assert verdict.ok is False
        # position 2 now holds what was originally line 3 (seq=3), so the
        # out-of-order sequence check fires there first.
        assert verdict.first_bad_index == 2

    async def test_reordering_two_lines_is_caught(self, tmp_path: Path) -> None:
        _build_chain(tmp_path, _header(), [_line(i) for i in range(5)])
        raw_lines = _read_jsonl_lines(tmp_path)
        raw_lines[1], raw_lines[2] = raw_lines[2], raw_lines[1]
        _write_jsonl_lines(tmp_path, raw_lines)

        verdict = await verify_metrics_chain(tmp_path)

        assert verdict.ok is False
        assert verdict.first_bad_index == 1

    async def test_appending_a_fabricated_line_with_a_plausible_chain_field_is_caught(
        self, tmp_path: Path
    ) -> None:
        _build_chain(tmp_path, _header(), [_line(i) for i in range(3)])
        raw_lines = _read_jsonl_lines(tmp_path)
        forged_body = _line(3)
        # A plausible-looking digest — sha256 of *something* — but not
        # actually chained from the true previous digest, which an attacker
        # forging only the log (not the sidecar) does not know how to
        # reconstruct correctly without also rewriting everything before it.
        forged_digest = hashlib.sha256(b"plausible-but-wrong").hexdigest()
        forged_body[CHAIN_FIELD] = f"3:{forged_digest}"
        raw_lines.append(json.dumps(forged_body, separators=(",", ":")))
        _write_jsonl_lines(tmp_path, raw_lines)
        sidecar = _read_sidecar(tmp_path)
        sidecar["lines"] = 4  # attacker also patches the obvious counter
        _write_sidecar(tmp_path, sidecar)

        verdict = await verify_metrics_chain(tmp_path)

        assert verdict.ok is False
        assert verdict.first_bad_index == 3

    async def test_truncating_the_tail_is_caught_by_the_line_count_check(
        self, tmp_path: Path
    ) -> None:
        _build_chain(tmp_path, _header(), [_line(i) for i in range(5)])
        raw_lines = _read_jsonl_lines(tmp_path)
        _write_jsonl_lines(tmp_path, raw_lines[:3])  # sidecar still says 5

        verdict = await verify_metrics_chain(tmp_path)

        assert verdict.ok is False
        assert verdict.first_bad_index is None
        assert verdict.lines_checked == 3

    async def test_splicing_in_a_line_from_a_different_run_is_caught(self, tmp_path: Path) -> None:
        run_a = tmp_path / "run-a"
        run_b = tmp_path / "run-b"
        _build_chain(run_a, _header(attempt_id="a"), [_line(i) for i in range(3)])
        _build_chain(run_b, _header(attempt_id="b", seed=99), [_line(i) for i in range(3)])
        a_lines = _read_jsonl_lines(run_a)
        b_lines = _read_jsonl_lines(run_b)
        a_lines[1] = b_lines[1]  # same position, foreign chain history
        _write_jsonl_lines(run_a, a_lines)

        verdict = await verify_metrics_chain(run_a)

        assert verdict.ok is False
        assert verdict.first_bad_index == 1

    async def test_a_crash_between_the_log_append_and_the_sidecar_write_is_a_visible_line_count_mismatch(
        self, tmp_path: Path
    ) -> None:
        """Mirrors ``MetricsWriter.append``'s documented ordering guarantee:
        the JSONL write can succeed while the sidecar write that follows it
        does not, and the result must be a *visible* mismatch, not a chain
        that silently accepts the extra line.
        """
        header = _header()
        _build_chain(tmp_path, header, [_line(i) for i in range(4)])
        sidecar_before_crash = _read_sidecar(tmp_path)
        chain_head = str(sidecar_before_crash["final"])
        # The writer appended a genuinely well-formed, correctly-chained 5th
        # line — then the process died before the sidecar rewrite landed.
        fifth = _line(4)
        digest = chain_next(chain_head, fifth)
        record = dict(fifth)
        record[CHAIN_FIELD] = f"4:{digest}"
        with (tmp_path / "metrics.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, separators=(",", ":")) + "\n")
        # sidecar left exactly as it was after line 4 (lines=4, final=<old>)

        verdict = await verify_metrics_chain(tmp_path)

        assert verdict.ok is False
        assert verdict.first_bad_index is None
        assert verdict.lines_checked == 5

    async def test_a_consistently_forged_file_verifies_clean(self, tmp_path: Path) -> None:
        """**The R2 hole, asserted on purpose.**

        An attacker who can rewrite the log can also rewrite the header and
        the sidecar to match. Recomputing the whole chain from a forged
        header over forged values produces a file that is, by this module's
        own definition, internally consistent — because internal
        consistency is *all* a hash chain can check when nothing outside the
        chain anchors the header to anything real. See the module docstring
        and ``OPEN-QUESTIONS.md`` R2/Q11. This test is not a bug report; a
        change that makes it fail is a change that broke the module's
        honesty about its own limits, and needs to be reverted, not "fixed".
        """
        forged_header = _header(attempt_id="totally-legitimate-attempt")
        forged_lines = [_line(i, speedup=99.0) for i in range(4)]  # implausibly good numbers

        _build_chain(tmp_path, forged_header, forged_lines)

        verdict = await verify_metrics_chain(tmp_path)

        assert verdict.ok is True

    async def test_missing_sidecar_is_ok_false_with_no_line_index(self, tmp_path: Path) -> None:
        _build_chain(tmp_path, _header(), [_line(0)])
        (tmp_path / CHAIN_SIDECAR_FILENAME).unlink()

        verdict = await verify_metrics_chain(tmp_path)

        assert verdict.ok is False
        assert verdict.first_bad_index is None

    async def test_a_corrupted_sidecar_is_ok_false_with_no_line_index(self, tmp_path: Path) -> None:
        _build_chain(tmp_path, _header(), [_line(0)])
        (tmp_path / CHAIN_SIDECAR_FILENAME).write_text("{not valid json", encoding="utf-8")

        verdict = await verify_metrics_chain(tmp_path)

        assert verdict.ok is False
        assert verdict.first_bad_index is None

    async def test_missing_jsonl_is_ok_false(self, tmp_path: Path) -> None:
        _build_chain(tmp_path, _header(), [_line(0)])
        (tmp_path / "metrics.jsonl").unlink()

        verdict = await verify_metrics_chain(tmp_path)

        assert verdict.ok is False
        assert verdict.first_bad_index is None

    async def test_sidecar_version_mismatch_is_caught(self, tmp_path: Path) -> None:
        _build_chain(tmp_path, _header(), [_line(i) for i in range(2)])
        sidecar = _read_sidecar(tmp_path)
        sidecar["version"] = CHAIN_VERSION + 1
        _write_sidecar(tmp_path, sidecar)

        verdict = await verify_metrics_chain(tmp_path)

        assert verdict.ok is False
        assert verdict.first_bad_index is None

    async def test_sidecar_line_count_disagreeing_with_the_actual_count_is_caught(
        self, tmp_path: Path
    ) -> None:
        _build_chain(tmp_path, _header(), [_line(i) for i in range(3)])
        sidecar = _read_sidecar(tmp_path)
        sidecar["lines"] = 4
        _write_sidecar(tmp_path, sidecar)

        verdict = await verify_metrics_chain(tmp_path)

        assert verdict.ok is False
        assert verdict.first_bad_index is None


# --------------------------------------------------------------------------- #
# Verifying an attempt that is still being written
# --------------------------------------------------------------------------- #


class TestAWriterMidAppendIsNotAFailure:
    """The false alarm: ``MetricsWriter`` appends the JSONL line and rewrites
    the sidecar *after* it, and rewrites that sidecar by truncating it — so a
    verifier reading during either window sees an honest, untouched attempt in
    a shape that reads as damage. Measured against a real writer, 1633 of 1640
    checks reported FAIL on data nobody had touched: 1284 unparseable sidecars
    (the truncate window), 348 line-count mismatches, and one truncated final
    line. ``docs/research-agent.md`` invites verifying a round *while it runs*,
    so an operator met this routinely — the cries-wolf failure this whole layer
    exists to eliminate, firing at exit ``1``.

    **None of these tests depends on timing.** They reproduce the exact
    intermediate on-disk state a real writer leaves — built by driving a real
    ``MetricsWriter``, not by hand — and substitute "the writer lands its next
    write" for the verifier's pause (:func:`integrity._pause`), which is the
    only seam at which the real sequence can be replayed deterministically. A
    test that raced a thread and hoped would flake, and a flaky integrity test
    gets deleted along with the coverage it was carrying.
    """

    @staticmethod
    def _pause_that(monkeypatch: pytest.MonkeyPatch, action: Any) -> list[int]:
        """Replace the verifier's pause with ``action``; return a call log.

        The list is asserted on directly: this fix is only correct if it is
        *bounded*, and the pause count is what bounds it.
        """
        calls: list[int] = []

        def fake_pause(seconds: float) -> None:
            calls.append(len(calls))
            action(len(calls))

        monkeypatch.setattr(integrity_module, "_pause", fake_pause)
        return calls

    async def test_the_sidecar_landing_during_the_pause_verifies_clean(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The reported defect, verbatim: N+1 lines against a sidecar reporting N.

        This is the state a real writer is in between its two writes. The
        writer completes it microseconds later; the verifier must not have
        already shouted "chain failure" by then.
        """
        snapshots = await _writer_snapshots(tmp_path, appends=5)
        jsonl_after_5, sidecar_after_5 = snapshots[4]
        _, sidecar_after_4 = snapshots[3]
        _put_pair(tmp_path, jsonl_after_5, sidecar_after_4)

        # Confirm the fixture really is the bad shape before the writer catches up.
        assert json.loads(sidecar_after_4)["lines"] == 4
        assert jsonl_after_5.count("\n") == 5

        calls = self._pause_that(
            monkeypatch,
            lambda _n: (tmp_path / CHAIN_SIDECAR_FILENAME).write_text(
                sidecar_after_5, encoding="utf-8"
            ),
        )
        verdict = await verify_metrics_chain(tmp_path)

        assert verdict.state is ChainState.OK
        assert verdict.ok is True
        assert verdict.lines_checked == 5
        assert calls == [0], "one re-read is enough; the verifier must not spin"

    async def test_a_sidecar_caught_mid_rewrite_verifies_clean_once_it_lands(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The *dominant* shape in practice, and the one an enumeration of
        "line count or final digest" would have missed: ``write_text``
        truncates the sidecar before writing it, so a reader can catch it
        empty and call it unparseable JSON.
        """
        snapshots = await _writer_snapshots(tmp_path, appends=4)
        jsonl_text, sidecar_text = snapshots[3]
        _put_pair(tmp_path, jsonl_text, "")

        calls = self._pause_that(
            monkeypatch,
            lambda _n: (tmp_path / CHAIN_SIDECAR_FILENAME).write_text(
                sidecar_text, encoding="utf-8"
            ),
        )
        verdict = await verify_metrics_chain(tmp_path)

        assert verdict.state is ChainState.OK
        assert verdict.lines_checked == 4
        assert calls == [0]

    async def test_a_torn_final_line_completed_during_the_pause_verifies_clean(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``Path.read_text`` reads a long log in chunks, so a reader can catch
        the writer's own append part-way and see a truncated **final** line —
        2 of 1204 concurrent reads, measured. That failure *is* pinned to a
        line index, which is why the retry rule is "not pinned to an interior
        line" rather than "not pinned to a line at all".
        """
        snapshots = await _writer_snapshots(tmp_path, appends=4)
        jsonl_text, sidecar_text = snapshots[3]
        torn = jsonl_text[: -(len(jsonl_text.rsplit("\n", 2)[1]) // 2)]
        assert not torn.endswith("\n")
        _put_pair(tmp_path, torn, sidecar_text)

        calls = self._pause_that(
            monkeypatch,
            lambda _n: (tmp_path / "metrics.jsonl").write_text(jsonl_text, encoding="utf-8"),
        )
        verdict = await verify_metrics_chain(tmp_path)

        assert verdict.state is ChainState.OK
        assert verdict.lines_checked == 4
        assert calls == [0]

    async def test_a_log_under_continuous_append_is_in_flight_not_a_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A file nobody stops writing may never present a consistent snapshot.

        The verifier must not pick either lie available to it: not OK (those
        numbers are not final), and not FAIL (nothing is wrong with the data).
        It reports IN_FLIGHT, ``ok`` stays ``False``, and — the point of the
        bound — it stops after a fixed number of reads instead of spinning.
        """
        snapshots = await _writer_snapshots(tmp_path, appends=8)
        _put_pair(tmp_path, snapshots[3][0], snapshots[2][1])

        calls = self._pause_that(
            monkeypatch,
            lambda n: (tmp_path / "metrics.jsonl").write_text(
                snapshots[3 + n][0], encoding="utf-8"
            ),
        )
        verdict = await verify_metrics_chain(tmp_path)

        assert verdict.state is ChainState.IN_FLIGHT
        assert verdict.ok is False
        assert "a writer is appending" in verdict.reason
        assert "line(s)" in verdict.reason, "the underlying numbers stay in the reason"
        assert len(calls) == integrity_module._SNAPSHOT_ATTEMPTS - 1

    async def test_an_in_flight_chain_is_reported_incomplete_by_verify_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """IN_FLIGHT reuses the existing three-code contract rather than adding
        a fourth: ``2`` already means "nothing failed, these numbers are not
        final", which is exactly what a round still being written is. It
        composes with the summary check without help — a live attempt has no
        ``metrics.json`` either, so both halves say "unfinished".
        """
        snapshots = await _writer_snapshots(tmp_path, appends=8)
        _put_pair(tmp_path, snapshots[3][0], snapshots[2][1])
        self._pause_that(
            monkeypatch,
            lambda n: (tmp_path / "metrics.jsonl").write_text(
                snapshots[3 + n][0], encoding="utf-8"
            ),
        )

        run_verdict = await verify.verify_run(tmp_path)

        assert run_verdict.state is verify.RunState.INCOMPLETE
        assert run_verdict.ok is False

    def test_an_in_flight_chain_exits_2_and_does_not_claim_the_chain_is_intact(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        snapshots = _writer_snapshots_sync(tmp_path, appends=8)
        _put_pair(tmp_path, snapshots[3][0], snapshots[2][1])
        self._pause_that(
            monkeypatch,
            lambda n: (tmp_path / "metrics.jsonl").write_text(
                snapshots[3 + n][0], encoding="utf-8"
            ),
        )

        assert verify.main([str(tmp_path)]) == verify.EXIT_INCOMPLETE
        out = capsys.readouterr().out
        assert "INCOMPLETE" in out
        assert "FAIL" not in out
        assert "chain still being written" in out
        assert "chain intact" not in out, "the verifier must not claim what it did not check"
        assert verify.HONESTY_LINE in out

    def test_the_in_flight_json_record_keeps_ok_false_and_names_the_state(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: Any
    ) -> None:
        snapshots = _writer_snapshots_sync(tmp_path, appends=8)
        _put_pair(tmp_path, snapshots[3][0], snapshots[2][1])
        self._pause_that(
            monkeypatch,
            lambda n: (tmp_path / "metrics.jsonl").write_text(
                snapshots[3 + n][0], encoding="utf-8"
            ),
        )

        assert verify.main([str(tmp_path), "--json"]) == verify.EXIT_INCOMPLETE
        record = json.loads(capsys.readouterr().out.splitlines()[0])

        assert record["ok"] is False
        assert record["state"] == "incomplete"
        assert record["chain"]["ok"] is False
        assert record["chain"]["state"] == "in_flight"

    async def test_a_quiet_clean_run_is_still_verified_in_a_single_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No re-read, no pause, no cost on the overwhelmingly common path."""
        _build_chain(tmp_path, _header(), [_line(i) for i in range(3)])
        calls = self._pause_that(monkeypatch, lambda _n: None)

        verdict = await verify_metrics_chain(tmp_path)

        assert verdict.ok is True
        assert calls == []


class TestTheReReadWeakensNoDetection:
    """Every genuine forgery that failed before the re-read must still fail,
    with the same reason and the same ``first_bad_index``.

    The discriminator is not a guess about intent: a tamper is *stable*, and
    two reads of a stable file are byte-identical, so the second look changes
    nothing about the verdict. A live writer is the only thing that can make
    the bytes move, and the only softening a moving file can buy is
    IN_FLIGHT — which is still non-zero, still names the directory, and is
    still not a pass. See ``_verify_metrics_chain_sync`` on why that widens
    no evasion.
    """

    @staticmethod
    def _count_pauses(monkeypatch: pytest.MonkeyPatch) -> list[float]:
        calls: list[float] = []
        monkeypatch.setattr(integrity_module, "_pause", calls.append)
        return calls

    async def test_a_tampered_interior_line_fails_without_any_re_read(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An interior line is not reachable by the race — the log is strictly
        append-only and no append rewrites a byte before the end — so this
        must not even pause, let alone soften.
        """
        _build_chain(tmp_path, _header(), [_line(i) for i in range(5)])
        _tamper_one_value(tmp_path, index=2)
        calls = self._count_pauses(monkeypatch)

        verdict = await verify_metrics_chain(tmp_path)

        assert verdict.state is ChainState.FAILED
        assert verdict.first_bad_index == 2
        assert calls == [], "a tamper is never re-read"

    async def test_a_truncated_log_still_fails(self, tmp_path: Path) -> None:
        _build_chain(tmp_path, _header(), [_line(i) for i in range(5)])
        _write_jsonl_lines(tmp_path, _read_jsonl_lines(tmp_path)[:3])

        verdict = await verify_metrics_chain(tmp_path)

        assert verdict.state is ChainState.FAILED
        assert verdict.lines_checked == 3

    async def test_a_wrong_final_digest_still_fails(self, tmp_path: Path) -> None:
        _build_chain(tmp_path, _header(), [_line(i) for i in range(3)])
        sidecar = _read_sidecar(tmp_path)
        sidecar["final"] = hashlib.sha256(b"not the final digest").hexdigest()
        _write_sidecar(tmp_path, sidecar)

        verdict = await verify_metrics_chain(tmp_path)

        assert verdict.state is ChainState.FAILED
        assert verdict.first_bad_index is None
        assert "final digest" in verdict.reason

    async def test_a_forged_header_still_fails(self, tmp_path: Path) -> None:
        _build_chain(tmp_path, _header(), [_line(i) for i in range(3)])
        sidecar = _read_sidecar(tmp_path)
        sidecar["header"] = _header(attempt_id="someone-elses-attempt")
        _write_sidecar(tmp_path, sidecar)

        verdict = await verify_metrics_chain(tmp_path)

        assert verdict.state is ChainState.FAILED
        assert HEADER_ALTERED_REASON in verdict.reason

    async def test_a_sidecar_count_mismatch_that_never_resolves_still_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The crash-between-the-two-writes shape, which is byte-identical to
        the race and separable from it only by *time*: nothing lands during
        the pause, so the second read sees the same bytes and the verdict
        stands. This is the property the whole fix hangs on — if a stable
        mismatch could be waited out, the writer's deliberate ordering
        guarantee would have been thrown away in the verifier instead.
        """
        snapshots = await _writer_snapshots(tmp_path, appends=5)
        _put_pair(tmp_path, snapshots[4][0], snapshots[3][1])
        calls = self._count_pauses(monkeypatch)

        verdict = await verify_metrics_chain(tmp_path)

        assert verdict.state is ChainState.FAILED
        assert verdict.first_bad_index is None
        assert verdict.lines_checked == 5
        assert "metrics.chain.json reports 4" in verdict.reason
        assert len(calls) == 1, "one re-read settles it; the verifier must not spin"

    async def test_a_stable_torn_final_line_still_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A process killed mid-write leaves a truncated final line for good.
        ``docs/research-agent.md`` tells operators that shape reports FAIL on a
        rotated ``prior-N/`` path, and it still does — the last-line retry
        buys a *live* writer one look, not a permanently damaged file a pass.
        """
        snapshots = await _writer_snapshots(tmp_path, appends=4)
        jsonl_text, sidecar_text = snapshots[3]
        _put_pair(tmp_path, jsonl_text[: len(jsonl_text) // 2], sidecar_text)
        calls = self._count_pauses(monkeypatch)

        verdict = await verify_metrics_chain(tmp_path)

        assert verdict.state is ChainState.FAILED
        assert verdict.ok is False
        assert len(calls) == 1

    def test_a_run_whose_chain_fails_is_still_exit_1_even_with_no_summary(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        """The composition guarantee ``_combine`` has always made: an absent
        summary never softens a broken chain. IN_FLIGHT must not have opened a
        second door to that.
        """
        _build_chain(tmp_path, _header(), [_line(i) for i in range(4)])
        _tamper_one_value(tmp_path, index=1)

        assert verify.main([str(tmp_path)]) == verify.EXIT_FAILED
        assert "FAIL" in capsys.readouterr().out

    async def test_a_present_disagreeing_summary_outranks_an_in_flight_chain(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A doctored summary is a finding about the data whatever the log is
        doing while it is read, so it must not be downgraded to "incomplete"
        by keeping a writer running.
        """
        snapshots = await _writer_snapshots(tmp_path, appends=8)
        _put_pair(tmp_path, snapshots[3][0], snapshots[2][1])
        _write_summary(tmp_path, {"steps_recorded": 999, "best_score": 1234.0})
        advanced = 3

        def keep_appending(_seconds: float) -> None:
            nonlocal advanced
            advanced += 1
            (tmp_path / "metrics.jsonl").write_text(snapshots[advanced][0], encoding="utf-8")

        monkeypatch.setattr(integrity_module, "_pause", keep_appending)

        chain = await verify_metrics_chain(tmp_path)
        assert chain.state is ChainState.IN_FLIGHT

        _put_pair(tmp_path, snapshots[3][0], snapshots[2][1])
        advanced = 3
        run_verdict = await verify.verify_run(tmp_path)

        assert run_verdict.state is verify.RunState.FAILED


# --------------------------------------------------------------------------- #
# The header is bound into the chain — round 6, the relabelling hole
# --------------------------------------------------------------------------- #


HEADER_ALTERED_REASON = "metrics.chain.json 'seed' does not match the recorded header"


class TestHeaderIsBoundIntoTheChain:
    """``_verify_metrics_chain_sync`` must **re-derive** the chain head by
    hashing the header on disk, never read ``sidecar["seed"]`` and trust it.

    Before this, the header — ``attempt_id``, ``problem_id``, ``round_id``,
    ``seed``, ``score_scale``, ``started_at_ms``, i.e. the entire answer to
    *which run is this?* — was unauthenticated metadata sitting next to the
    chain. A verified-clean chain could be silently **relabelled** onto a
    different attempt, round, seed, or score scale by editing one small JSON
    object: no hashing, not one byte of ``metrics.jsonl`` changed, and
    ``verify`` still printed ``OK`` and exited 0.

    That is strictly cheaper and narrower than the R2 hole this module
    discloses (see :func:`test_a_consistently_forged_file_verifies_clean`,
    which must keep passing): R2 costs a full chain recomputation and needs
    privilege separation to close, whereas this cost a text edit and is
    closeable here.
    """

    @pytest.mark.parametrize(
        ("field", "forged"),
        [
            ("attempt_id", "some-other-attempt"),
            ("problem_id", "some-other-problem"),
            ("round_id", "round-99"),
            ("seed", 8),
            ("score_scale", "leaderboard_percentile"),
            ("started_at_ms", 1_800_000_000_000),
        ],
    )
    async def test_editing_one_header_field_and_recomputing_nothing_is_caught(
        self, tmp_path: Path, field: str, forged: object
    ) -> None:
        """One field, edited in place. ``seed``/``final``/``lines`` untouched,
        ``metrics.jsonl`` untouched — the whole attack is a text edit.
        """
        _build_chain(tmp_path, _header(), [_line(i) for i in range(4)])
        jsonl_before = (tmp_path / "metrics.jsonl").read_bytes()
        sidecar = _read_sidecar(tmp_path)
        seed_before, final_before, lines_before = (
            sidecar["seed"],
            sidecar["final"],
            sidecar["lines"],
        )
        header = dict(sidecar["header"])  # type: ignore[call-overload]
        assert header[field] != forged, "fixture assumption broken: field not actually changed"
        header[field] = forged
        sidecar["header"] = header
        _write_sidecar(tmp_path, sidecar)

        verdict = await verify_metrics_chain(tmp_path)

        assert verdict.ok is False
        assert HEADER_ALTERED_REASON in verdict.reason
        assert verdict.first_bad_index is None
        assert verdict.lines_checked == 0
        # The attack really did recompute nothing: everything else is as the
        # honest writer left it. If any of these drifted, the test would be
        # catching some *other* tamper and proving nothing about the header.
        assert (tmp_path / "metrics.jsonl").read_bytes() == jsonl_before
        after = _read_sidecar(tmp_path)
        assert (after["seed"], after["final"], after["lines"]) == (
            seed_before,
            final_before,
            lines_before,
        )

    async def test_forging_all_six_header_fields_at_once_is_caught(self, tmp_path: Path) -> None:
        """The full relabelling: this chain now claims to be a different
        attempt, at a different problem, in a different round, under a
        different seed, on a different score scale, started at a different
        time — and every number in it is the honest run's.
        """
        _build_chain(tmp_path, _header(), [_line(i) for i in range(4)])
        jsonl_before = (tmp_path / "metrics.jsonl").read_bytes()
        sidecar = _read_sidecar(tmp_path)
        sidecar["header"] = _header(
            attempt_id="attempt-999",
            problem_id="problem-999",
            round_id="round-99",
            seed=999,
            score_scale="leaderboard_percentile",
            started_at_ms=1_800_000_000_000,
        )
        _write_sidecar(tmp_path, sidecar)

        verdict = await verify_metrics_chain(tmp_path)

        assert verdict.ok is False
        assert HEADER_ALTERED_REASON in verdict.reason
        assert (tmp_path / "metrics.jsonl").read_bytes() == jsonl_before

    async def test_swapping_in_another_runs_header_wholesale_is_caught(
        self, tmp_path: Path
    ) -> None:
        """The relabelling that matters most in practice: take run A's clean
        chain and hand it run B's identity, so B appears to have produced A's
        numbers.
        """
        run_a = tmp_path / "run-a"
        run_b = tmp_path / "run-b"
        _build_chain(run_a, _header(attempt_id="a"), [_line(i, speedup=1.0) for i in range(3)])
        _build_chain(run_b, _header(attempt_id="b", seed=99), [_line(i) for i in range(3)])
        sidecar_a = _read_sidecar(run_a)
        sidecar_a["header"] = _read_sidecar(run_b)["header"]
        _write_sidecar(run_a, sidecar_a)

        verdict = await verify_metrics_chain(run_a)

        assert verdict.ok is False
        assert HEADER_ALTERED_REASON in verdict.reason

    async def test_a_missing_header_key_fails_rather_than_raising(self, tmp_path: Path) -> None:
        _build_chain(tmp_path, _header(), [_line(i) for i in range(2)])
        sidecar = _read_sidecar(tmp_path)
        del sidecar["header"]
        _write_sidecar(tmp_path, sidecar)

        verdict = await verify_metrics_chain(tmp_path)

        assert verdict.ok is False
        assert "header" in verdict.reason
        assert verdict.first_bad_index is None

    @pytest.mark.parametrize(
        "not_a_dict",
        [
            None,
            "attempt-1",
            42,
            ["attempt_id", "attempt-1"],
            [["attempt_id", "attempt-1"], ["seed", 7]],
        ],
        ids=["null", "string", "number", "flat-list", "list-of-pairs"],
    )
    async def test_a_non_dict_header_fails_rather_than_raising(
        self, tmp_path: Path, not_a_dict: object
    ) -> None:
        """A FAIL naming the header, not a traceback, and not a coercion.

        ``list-of-pairs`` is the case that makes ``dict(header)`` the wrong
        way to handle this: it coerces *successfully*, so a bare ``dict()``
        would hash a shape no writer ever emits and report a confusing seed
        mismatch instead of naming the header as malformed.
        """
        _build_chain(tmp_path, _header(), [_line(i) for i in range(2)])
        sidecar = _read_sidecar(tmp_path)
        sidecar["header"] = not_a_dict
        _write_sidecar(tmp_path, sidecar)

        verdict = await verify_metrics_chain(tmp_path)

        assert verdict.ok is False
        assert "'header' is not a JSON object" in verdict.reason
        assert verdict.first_bad_index is None

    async def test_a_header_carrying_a_non_finite_float_fails_rather_than_raising(
        self, tmp_path: Path
    ) -> None:
        """``json.loads`` accepts the bare ``NaN`` token even though it is not
        valid JSON and the writer's ``allow_nan=False`` can never emit it. A
        header carrying one must leave through a verdict, not a traceback.
        """
        _build_chain(tmp_path, _header(), [_line(i) for i in range(2)])
        sidecar = _read_sidecar(tmp_path)
        sidecar["header"] = _header(seed=float("nan"))
        # json.dumps' default allow_nan=True writes the bare NaN token.
        (tmp_path / CHAIN_SIDECAR_FILENAME).write_text(json.dumps(sidecar), encoding="utf-8")

        verdict = await verify_metrics_chain(tmp_path)

        assert verdict.ok is False
        assert HEADER_ALTERED_REASON in verdict.reason

    async def test_a_header_that_cannot_be_hashed_fails_rather_than_raising(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard around ``seed_hash``. Nothing ``json.loads`` produces can
        make ``canonical_json`` raise today, so this forces the raise: the
        point is that the verifier still returns a verdict if that ever stops
        being true (a stricter ``canonical_json``, a new header value type),
        rather than letting an exception escape a function whose entire
        contract is "returns ``ok=False``, never raises".
        """
        _build_chain(tmp_path, _header(), [_line(i) for i in range(2)])

        def _explode(payload: object) -> str:
            raise ValueError("cannot serialise that")

        monkeypatch.setattr(integrity_module, "canonical_json", _explode)

        verdict = await verify_metrics_chain(tmp_path)

        assert verdict.ok is False
        assert "'header' could not be hashed" in verdict.reason
        assert "cannot serialise that" in verdict.reason
        assert verdict.first_bad_index is None

    async def test_a_self_consistent_forgery_still_verifies_clean_after_the_header_check(
        self, tmp_path: Path
    ) -> None:
        """The R2 limit, re-asserted from the *other* side.

        :func:`test_a_consistently_forged_file_verifies_clean` above already
        pins this. This one exists because the header check is exactly the
        kind of change that could invert it by accident, and because it must
        keep passing *for the right reason*: the seed the forger recorded is
        genuinely the hash of the header they recorded, so re-deriving it
        changes nothing. Asserting that equality directly means a future
        version that passed this test by, say, skipping the check whenever
        the header looks unusual would not survive.
        """
        forged_header = _header(attempt_id="totally-legitimate-attempt", seed=4321)
        _build_chain(tmp_path, forged_header, [_line(i, speedup=99.0) for i in range(4)])
        sidecar = _read_sidecar(tmp_path)

        assert seed_hash(sidecar["header"]) == sidecar["seed"]  # type: ignore[arg-type]

        verdict = await verify_metrics_chain(tmp_path)

        assert verdict.ok is True


# --------------------------------------------------------------------------- #
# The false-alarm guard: honest runs must keep passing
#
# This work produced five separate false alarms on honest data, each found by
# driving a run shape nobody had specified and each surviving a fully green
# suite. A verifier that cries wolf teaches its operator to ignore it, which
# is worse than no verifier at all. The header re-hash introduces a brand new
# way to raise one — the sidecar's header goes to disk through
# ``json.dumps`` and comes back through ``json.loads``, so any value whose
# round trip is not byte-identical under ``canonical_json`` would make every
# honest run FAIL. These drive the real, unmocked ``RoundRunner``.
# --------------------------------------------------------------------------- #


class TestHonestRunsStillVerifyCleanAfterTheHeaderCheck:
    def _assert_round_trip_is_exact(self, metrics_dir: Path) -> None:
        """The property the header check rests on, asserted directly.

        ``seed_hash`` runs on the in-memory header at write time and on the
        JSON round-tripped header at verify time. If those two ever disagree
        for an honest run, every honest run FAILs. Asserting the equality
        itself — not merely that the verdict came back green — names the
        exact reason a future regression here would be a false alarm.
        """
        sidecar = json.loads((metrics_dir / CHAIN_SIDECAR_FILENAME).read_text(encoding="utf-8"))
        assert seed_hash(sidecar["header"]) == sidecar["seed"], (
            f"round-tripped header does not re-hash to the recorded seed in {metrics_dir} — "
            "this is the false alarm the header check must never introduce"
        )

    async def test_a_plain_runner_attempt_still_verifies_clean(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        problem = make_problem("s1")
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)

        await runner.run_attempt(problem, make_config(), output_dir=store.round_dir(0))

        metrics_dir = store.round_dir(0) / "attempts" / "s1"
        self._assert_round_trip_is_exact(metrics_dir)
        verdict = await verify_metrics_chain(metrics_dir)
        assert verdict.ok is True, verdict.reason

    async def test_a_non_ascii_problem_id_still_verifies_clean(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """``problem_id`` lands in the header verbatim, and
        ``canonical_json`` sets ``ensure_ascii=True`` — so the digest must not
        depend on how the sidecar's bytes were encoded on the way to disk and
        back. A ``problem_id`` outside ASCII is the shape that would expose
        it. (It is also a directory name, which macOS may normalise; the
        header string in the sidecar is unaffected either way, which is
        precisely what makes this worth pinning.)
        """
        problem = make_problem("problème-λ-Ω-1")
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)

        await runner.run_attempt(problem, make_config(), output_dir=store.round_dir(0))

        metrics_dir = store.round_dir(0) / "attempts" / "problème-λ-Ω-1"
        self._assert_round_trip_is_exact(metrics_dir)
        verdict = await verify_metrics_chain(metrics_dir)
        assert verdict.ok is True, verdict.reason

    async def test_a_harness_failure_attempt_still_verifies_clean(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """The run shape that produced round 3's false alarm, re-driven
        against the new check.
        """
        problem = make_problem("s1", harness_failure=True)
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)

        await runner.run_attempt(problem, make_config(), output_dir=store.round_dir(0))

        metrics_dir = store.round_dir(0) / "attempts" / "s1"
        self._assert_round_trip_is_exact(metrics_dir)
        verdict = await verify_metrics_chain(metrics_dir)
        assert verdict.ok is True, verdict.reason

    async def test_both_generations_of_a_rotated_redrive_still_verify_clean(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """A re-drive rotates the prior attempt's whole trio into ``prior-N/``,
        where ``verify``'s directory walk finds and checks it like any other
        run. The rotated generation's sidecar was written by a *different*
        attempt's header — the case most likely to be mishandled by a check
        that assumed one header per directory — so both generations are
        asserted, and asserted to carry genuinely different identities.
        """
        problem = make_problem("s1")
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        output_dir = store.round_dir(0)

        first = await runner.run_attempt(problem, make_config(), output_dir=output_dir)
        second = await runner.run_attempt(problem, make_config(), output_dir=output_dir)
        assert second.attempt.attempt_id != first.attempt.attempt_id

        metrics_dir = output_dir / "attempts" / "s1"
        rotated_dir = metrics_dir / "prior-1"
        assert rotated_dir.is_dir(), sorted(p.name for p in metrics_dir.iterdir())

        for directory in (metrics_dir, rotated_dir):
            self._assert_round_trip_is_exact(directory)
            verdict = await verify_metrics_chain(directory)
            assert verdict.ok is True, f"{directory}: {verdict.reason}"

        # The two generations really do carry different headers, so the check
        # above was exercised against two distinct identities rather than the
        # same one twice.
        current_header = json.loads((metrics_dir / CHAIN_SIDECAR_FILENAME).read_text())["header"]
        rotated_header = json.loads((rotated_dir / CHAIN_SIDECAR_FILENAME).read_text())["header"]
        assert current_header["attempt_id"] == second.attempt.attempt_id
        assert rotated_header["attempt_id"] == first.attempt.attempt_id

    def test_the_verify_cli_still_exits_0_on_a_real_runner_attempt(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """End to end through the real CLI, as an operator runs it — the same
        invocation that printed ``OK (4 line(s) checked)`` over a fully forged
        header before this fix. Sync + subprocess for the reason
        :class:`TestVerifyCliAgainstARealRun` documents below.
        """
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        problem = make_problem("s1")
        asyncio.run(runner.run_attempt(problem, make_config(), output_dir=store.round_dir(0)))
        metrics_dir = store.round_dir(0) / "attempts" / "s1"

        result = _run_verify_cli_subprocess(str(metrics_dir))

        assert result.returncode == 0, result.stdout + result.stderr
        assert "OK" in result.stdout

    def test_the_verify_cli_exits_1_on_a_real_attempt_with_a_forged_header(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """The reproduction that made this a blocker, now inverted.

        A real ``RoundRunner`` attempt, all six header fields forged,
        ``seed``/``final``/``lines`` untouched and not one byte of
        ``metrics.jsonl`` changed. This printed ``OK`` and exited 0 before.
        """
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        problem = make_problem("s1")
        asyncio.run(runner.run_attempt(problem, make_config(), output_dir=store.round_dir(0)))
        metrics_dir = store.round_dir(0) / "attempts" / "s1"

        jsonl_before = (metrics_dir / "metrics.jsonl").read_bytes()
        sidecar = json.loads((metrics_dir / CHAIN_SIDECAR_FILENAME).read_text(encoding="utf-8"))
        sidecar["header"] = _header(
            attempt_id="attempt-999",
            problem_id="problem-999",
            round_id="round-99",
            seed=999,
            score_scale="leaderboard_percentile",
            started_at_ms=1_800_000_000_000,
        )
        (metrics_dir / CHAIN_SIDECAR_FILENAME).write_text(json.dumps(sidecar), encoding="utf-8")
        assert (metrics_dir / "metrics.jsonl").read_bytes() == jsonl_before

        result = _run_verify_cli_subprocess(str(metrics_dir))

        assert result.returncode == 1, result.stdout + result.stderr
        assert "FAIL" in result.stdout


# --------------------------------------------------------------------------- #
# reconcile_summary
# --------------------------------------------------------------------------- #


def _matched_lines_and_summary() -> tuple[list[dict[str, object]], dict[str, object]]:
    lines: list[dict[str, object]] = [
        {
            "step": 0,
            "consumed_steps": 0,
            "tokens_used": 1000,
            "wall_clock_s": 10.0,
            "cap_extensions": 0,
            "outcome_code": 0,
            "progress": 0.0,
            "speedup": 1.0,
        },
        {
            "step": 1,
            "consumed_steps": 1,
            "tokens_used": 2500,
            "wall_clock_s": 25.5,
            "cap_extensions": 0,
            "outcome_code": 1,
            "progress": 1.0,
            "speedup": 2.0,
        },
    ]
    summary: dict[str, object] = {
        "schema_version": 1,
        "steps_recorded": 2,
        "consumed_steps": 1,
        "consumed_tokens": 2500,
        "consumed_wall_clock_seconds": 25.5,
        "cap_extensions": 0,
        "final_progress": 1.0,
        "outcome": 1,
        "baseline_score": 1.0,
        "best_score": 2.0,
    }
    return lines, summary


class TestReconcileSummary:
    async def test_a_matched_pair_reconciles_clean(self, tmp_path: Path) -> None:
        lines, summary = _matched_lines_and_summary()
        _write_raw_jsonl(tmp_path, lines)
        _write_summary(tmp_path, summary)

        verdict = await reconcile_summary(tmp_path)

        assert verdict == ReconcileVerdict(
            state=ReconcileState.OK, mismatches=(), reason=verdict.reason
        )
        assert verdict.ok is True

    async def test_an_altered_token_count_is_named_and_nothing_unrelated_is(
        self, tmp_path: Path
    ) -> None:
        lines, summary = _matched_lines_and_summary()
        summary["consumed_tokens"] = 999_999
        _write_raw_jsonl(tmp_path, lines)
        _write_summary(tmp_path, summary)

        verdict = await reconcile_summary(tmp_path)

        assert verdict.ok is False
        assert verdict.mismatches == ("consumed_tokens",)

    async def test_consumed_steps_reconciles_from_its_own_field_when_it_diverges_from_step(
        self, tmp_path: Path
    ) -> None:
        """Round-2 regression — the gate's exact reproduction.

        A solver whose exception carried accounted tokens makes
        ``_charge_failed_step`` call ``record_consumption(...)``, which bumps
        ``consumed.steps`` **without** bumping ``step_index``
        (``_spend_carried_on_error``: "a proposal call that happened still
        costs a step even when parse/apply then failed"). The line therefore
        legitimately reports ``step=0`` alongside ``consumed_steps=2``.
        Deriving ``consumed_steps`` from ``step`` (the original bug) would
        raise a false mismatch here; reading the line's own ``consumed_steps``
        field must not.
        """
        lines: list[dict[str, object]] = [
            {
                "step": 0,
                "consumed_steps": 2,
                "tokens_used": 154,
                "wall_clock_s": 1.0,
                "cap_extensions": 0,
                "outcome_code": 4,  # ABANDONED
                "speedup": 1.0,
            },
        ]
        summary: dict[str, object] = {
            "schema_version": 1,
            "steps_recorded": 1,
            "consumed_steps": 2,
            "consumed_tokens": 154,
            "consumed_wall_clock_seconds": 1.0,
            "cap_extensions": 0,
            "final_progress": None,
            "outcome": 4,
            "baseline_score": 1.0,
            "best_score": 1.0,
        }
        _write_raw_jsonl(tmp_path, lines)
        _write_summary(tmp_path, summary)

        verdict = await reconcile_summary(tmp_path)

        assert verdict.ok is True, verdict.mismatches

    async def test_consumed_steps_mismatch_is_named_when_it_disagrees_with_its_own_field(
        self, tmp_path: Path
    ) -> None:
        """The fix must not weaken the check — a genuinely doctored
        ``consumed_steps`` is still caught, independent of what ``step`` says.
        """
        lines, summary = _matched_lines_and_summary()
        summary["consumed_steps"] = 999
        _write_raw_jsonl(tmp_path, lines)
        _write_summary(tmp_path, summary)

        verdict = await reconcile_summary(tmp_path)

        assert verdict.ok is False
        assert verdict.mismatches == ("consumed_steps",)

    async def test_best_score_matching_any_emitted_score_not_just_the_last_is_accepted(
        self, tmp_path: Path
    ) -> None:
        lines, summary = _matched_lines_and_summary()
        summary["best_score"] = 1.0  # the *first* (baseline) score, not the last
        _write_raw_jsonl(tmp_path, lines)
        _write_summary(tmp_path, summary)

        verdict = await reconcile_summary(tmp_path)

        assert verdict.ok is True

    async def test_best_score_absent_from_the_log_is_rejected(self, tmp_path: Path) -> None:
        lines, summary = _matched_lines_and_summary()
        summary["best_score"] = 500.0  # never emitted anywhere in the log
        _write_raw_jsonl(tmp_path, lines)
        _write_summary(tmp_path, summary)

        verdict = await reconcile_summary(tmp_path)

        assert verdict.ok is False
        assert "best_score" in verdict.mismatches

    async def test_steps_recorded_disagreeing_with_the_actual_line_count_is_named(
        self, tmp_path: Path
    ) -> None:
        lines, summary = _matched_lines_and_summary()
        summary["steps_recorded"] = 99
        _write_raw_jsonl(tmp_path, lines)
        _write_summary(tmp_path, summary)

        verdict = await reconcile_summary(tmp_path)

        assert verdict.ok is False
        assert "steps_recorded" in verdict.mismatches

    async def test_final_progress_disagreeing_is_named(self, tmp_path: Path) -> None:
        lines, summary = _matched_lines_and_summary()
        summary["final_progress"] = 0.42
        _write_raw_jsonl(tmp_path, lines)
        _write_summary(tmp_path, summary)

        verdict = await reconcile_summary(tmp_path)

        assert verdict.ok is False
        assert "final_progress" in verdict.mismatches

    async def test_outcome_disagreeing_with_the_last_lines_outcome_code_is_named(
        self, tmp_path: Path
    ) -> None:
        lines, summary = _matched_lines_and_summary()
        summary["outcome"] = 2  # log's last line says outcome_code=1
        _write_raw_jsonl(tmp_path, lines)
        _write_summary(tmp_path, summary)

        verdict = await reconcile_summary(tmp_path)

        assert verdict.ok is False
        assert "outcome" in verdict.mismatches

    async def test_consumed_wall_clock_seconds_within_tolerance_is_accepted(
        self, tmp_path: Path
    ) -> None:
        lines, summary = _matched_lines_and_summary()
        summary["consumed_wall_clock_seconds"] = 25.5 + 1e-9  # far inside the 1e-6 tolerance
        _write_raw_jsonl(tmp_path, lines)
        _write_summary(tmp_path, summary)

        verdict = await reconcile_summary(tmp_path)

        assert verdict.ok is True

    async def test_consumed_wall_clock_seconds_outside_tolerance_is_named(
        self, tmp_path: Path
    ) -> None:
        lines, summary = _matched_lines_and_summary()
        summary["consumed_wall_clock_seconds"] = 25.5 + 1e-3  # well outside 1e-6
        _write_raw_jsonl(tmp_path, lines)
        _write_summary(tmp_path, summary)

        verdict = await reconcile_summary(tmp_path)

        assert verdict.ok is False
        assert "consumed_wall_clock_seconds" in verdict.mismatches

    async def test_baseline_score_is_the_first_emitted_score_not_the_minimum_or_last(
        self, tmp_path: Path
    ) -> None:
        lines: list[dict[str, object]] = [
            {
                "step": 0,
                "consumed_steps": 0,
                "tokens_used": 10,
                "wall_clock_s": 1.0,
                "cap_extensions": 0,
                "outcome_code": 0,
                "speedup": 5.0,
            },
            {
                "step": 1,
                "consumed_steps": 1,
                "tokens_used": 20,
                "wall_clock_s": 2.0,
                "cap_extensions": 0,
                "outcome_code": 0,
                "speedup": 1.0,
            },
            {
                "step": 2,
                "consumed_steps": 2,
                "tokens_used": 30,
                "wall_clock_s": 3.0,
                "cap_extensions": 0,
                "outcome_code": 1,
                "speedup": 9.0,
            },
        ]
        summary: dict[str, object] = {
            "schema_version": 1,
            "steps_recorded": 3,
            "consumed_steps": 2,
            "consumed_tokens": 30,
            "consumed_wall_clock_seconds": 3.0,
            "cap_extensions": 0,
            "final_progress": None,
            "outcome": 1,
            "baseline_score": 5.0,  # the first line's speedup, not min(1.0) or last(9.0)
            "best_score": 9.0,
        }
        _write_raw_jsonl(tmp_path, lines)
        _write_summary(tmp_path, summary)

        verdict = await reconcile_summary(tmp_path)

        assert verdict.ok is True

    async def test_diagnostic_keys_are_never_mistaken_for_the_score_series(
        self, tmp_path: Path
    ) -> None:
        """A ``diag_`` value must never satisfy ``best_score`` or ``baseline_score`` —
        the operator's ruler and the agent's notebook stay structurally separate.
        """
        lines: list[dict[str, object]] = [
            {
                "step": 0,
                "consumed_steps": 0,
                "tokens_used": 10,
                "wall_clock_s": 1.0,
                "cap_extensions": 0,
                "outcome_code": 0,
                "speedup": 1.0,
                "diag_peak_rss_mb": 412.0,
            },
        ]
        summary: dict[str, object] = {
            "schema_version": 1,
            "steps_recorded": 1,
            "consumed_steps": 0,
            "consumed_tokens": 10,
            "consumed_wall_clock_seconds": 1.0,
            "cap_extensions": 0,
            "final_progress": None,
            "outcome": 0,
            "baseline_score": 1.0,
            "best_score": 412.0,  # the diagnostic value, not a real score
        }
        _write_raw_jsonl(tmp_path, lines)
        _write_summary(tmp_path, summary)

        verdict = await reconcile_summary(tmp_path)

        assert verdict.ok is False
        assert "best_score" in verdict.mismatches

    async def test_missing_metrics_json_is_incomplete_not_ok_and_not_failed(
        self, tmp_path: Path
    ) -> None:
        """Absence is its own state. ``metrics.json`` is written once, when an
        attempt ends, so "log present, summary absent" is what an unfinished
        run looks like — not a finding about the data. It is still not a pass:
        ``ok`` stays ``False``, because callers read ``ok`` as "this run is
        good" and an unfinished run's numbers are not final.
        """
        lines, _ = _matched_lines_and_summary()
        _write_raw_jsonl(tmp_path, lines)

        verdict = await reconcile_summary(tmp_path)

        assert verdict.state is ReconcileState.INCOMPLETE
        assert verdict.ok is False
        assert verdict.mismatches == ()
        assert "missing" in verdict.reason.lower()

    async def test_a_present_but_unparseable_summary_is_failed_not_incomplete(
        self, tmp_path: Path
    ) -> None:
        """Only *absence* is INCOMPLETE. The writer emits ``metrics.json``
        whole, so a file that is there but does not parse is damage, and
        damage read as "not finished yet" would be exactly the softening this
        state must not become.
        """
        lines, _ = _matched_lines_and_summary()
        _write_raw_jsonl(tmp_path, lines)
        (tmp_path / "metrics.json").write_text("{not json at all", encoding="utf-8")

        verdict = await reconcile_summary(tmp_path)

        assert verdict.state is ReconcileState.FAILED
        assert verdict.ok is False

    async def test_a_present_summary_that_is_not_an_object_is_failed_not_incomplete(
        self, tmp_path: Path
    ) -> None:
        lines, _ = _matched_lines_and_summary()
        _write_raw_jsonl(tmp_path, lines)
        (tmp_path / "metrics.json").write_text("[1, 2, 3]", encoding="utf-8")

        verdict = await reconcile_summary(tmp_path)

        assert verdict.state is ReconcileState.FAILED
        assert verdict.ok is False

    async def test_a_doctored_summary_still_fails_exactly_as_before(self, tmp_path: Path) -> None:
        """The guard on the whole change: introducing INCOMPLETE must not have
        softened the case this module exists for. A summary that is present
        and disagrees is FAILED, with the disagreeing field still named.
        """
        lines, summary = _matched_lines_and_summary()
        summary["best_score"] = 1_000.0
        _write_raw_jsonl(tmp_path, lines)
        _write_summary(tmp_path, summary)

        verdict = await reconcile_summary(tmp_path)

        assert verdict.state is ReconcileState.FAILED
        assert verdict.ok is False
        assert verdict.mismatches == ("best_score",)


# --------------------------------------------------------------------------- #
# _CORE_LINE_KEYS — the hand-copied list must not silently drift
# --------------------------------------------------------------------------- #


class TestCoreLineKeysStaySynced:
    def test_core_line_keys_matches_what_metrics_line_to_json_actually_emits(self) -> None:
        """``integrity._CORE_LINE_KEYS`` is hand-copied from ``results.py``
        (see the comment directly above its definition in ``integrity.py``
        for why: ``results.py`` imports ``integrity`` to seed its hash chain,
        so importing back would be circular). Nothing at runtime keeps the
        two lists in sync — this test is the only thing that does. A key
        renamed, added, or removed in ``results.RESERVED_FIELDS`` or
        ``results._CORE_EMITTED_KEYS`` without a matching edit to the
        hand-copied set in ``integrity.py`` would silently make
        ``reconcile_summary`` misclassify a core field as a score-series
        value (or vice versa) — this asserts that never happens unnoticed.

        Importing both ``results`` and ``integrity`` here is fine even though
        ``results.py`` cannot import ``integrity`` back: the circularity is a
        constraint on the *modules*, not on a test that imports each of them
        independently.
        """
        emitted_core_keys = results_module.RESERVED_FIELDS | results_module._CORE_EMITTED_KEYS
        hand_copied_keys = integrity_module._CORE_LINE_KEYS

        missing_from_integrity = emitted_core_keys - hand_copied_keys
        extra_in_integrity = hand_copied_keys - emitted_core_keys

        assert not missing_from_integrity and not extra_in_integrity, (
            "turing.research.loop.integrity._CORE_LINE_KEYS has drifted from the "
            "keys turing.research.loop.results.MetricsLine.to_json actually emits "
            "(results.RESERVED_FIELDS | results._CORE_EMITTED_KEYS). "
            f"Missing from integrity._CORE_LINE_KEYS: {sorted(missing_from_integrity)}. "
            f"Present in integrity._CORE_LINE_KEYS but no longer emitted: "
            f"{sorted(extra_in_integrity)}. "
            "Update the hand-copied set at the comment above "
            "_CORE_LINE_KEYS's definition in integrity.py to match."
        )


# --------------------------------------------------------------------------- #
# best_score is None — round 3, blocker 1 regression
# --------------------------------------------------------------------------- #


class TestBestScoreNoneIsUncheckedAgainstARealRun:
    """Regression for round 3: ``reconcile_summary`` must check nothing when
    ``best_score`` is ``None``, even though the log legitimately carries
    non-null score values.

    ``runner.py``'s ``_is_better`` deliberately refuses a harness-failure
    result as ``best`` ("a harness-failure result is not a grade and must
    never become best"), so an attempt whose every verify comes back a
    harness failure legitimately ends with ``best_score = None`` in its
    summary while every line in its log carries a ``0.0`` score value —
    exactly what ``problems/speedup.py`` produces in production whenever the
    benchmark cannot get a usable measurement. A ``best_score is None``
    branch that infers "the log emitted scores, so a correct summary should
    have picked one" is a false alarm on this perfectly honest run.
    """

    async def test_an_attempt_where_every_verify_harness_fails_reconciles_clean(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        problem = make_problem("s1", harness_failure=True)
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)

        outcome = await runner.run_attempt(problem, make_config(), output_dir=store.round_dir(0))

        assert outcome.attempt.state.name == "FAILED_WITHIN_CAP"
        metrics_dir = store.round_dir(0) / "attempts" / "s1"
        summary = json.loads((metrics_dir / "metrics.json").read_text(encoding="utf-8"))
        assert summary["best_score"] is None
        jsonl_lines = [
            json.loads(line) for line in (metrics_dir / "metrics.jsonl").read_text().splitlines()
        ]
        score_scale = problem.verifier.score_scale
        assert any(score_scale in line and line[score_scale] == 0.0 for line in jsonl_lines), (
            "fixture assumption broken: expected at least one emitted score-series "
            "value in the log for this regression to be meaningful"
        )

        verdict = await reconcile_summary(metrics_dir)

        assert verdict.ok is True, verdict.mismatches


# --------------------------------------------------------------------------- #
# The `verify` CLI — acceptance criterion 20
# --------------------------------------------------------------------------- #


class TestVerifyCli:
    """``python -m turing.research.loop.verify`` had zero test coverage
    anywhere in the repo before this class — ``verify.py`` did not exist yet
    when this file was first authored (a planner-side file-ownership gap, not
    an implementer mistake), and the CLI has since been written by another
    agent. This closes acceptance criterion 20.
    """

    def test_exits_0_on_a_clean_directory_and_prints_ok_and_the_qualifier(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        _clean_verifiable_run(tmp_path)

        exit_code = verify.main([str(tmp_path)])
        out = capsys.readouterr().out

        assert exit_code == 0
        assert "OK" in out
        assert verify.HONESTY_LINE in out

    def test_exits_1_on_a_tampered_directory_and_still_prints_the_qualifier(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        _clean_verifiable_run(tmp_path)
        _tamper_one_value(tmp_path)

        exit_code = verify.main([str(tmp_path)])
        out = capsys.readouterr().out

        assert exit_code == 1
        assert "FAIL" in out
        assert verify.HONESTY_LINE in out

    def test_json_output_carries_the_qualifier_on_both_a_clean_and_a_tampered_run(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        """A machine consumer must never receive a stronger claim than a
        human one — the ``--json`` output is checked for the qualifier
        exactly as the human-readable output is above, on both outcomes.
        """
        clean_dir = tmp_path / "clean"
        tampered_dir = tmp_path / "tampered"
        _clean_verifiable_run(clean_dir)
        _clean_verifiable_run(tampered_dir)
        _tamper_one_value(tampered_dir)

        clean_exit = verify.main([str(clean_dir), "--json"])
        clean_out = capsys.readouterr().out
        tampered_exit = verify.main([str(tampered_dir), "--json"])
        tampered_out = capsys.readouterr().out

        assert clean_exit == 0
        assert tampered_exit == 1
        # every non-final line is a JSON record; the qualifier also always
        # appears verbatim as its own trailing plain-text line.
        clean_record = json.loads(clean_out.splitlines()[0])
        tampered_record = json.loads(tampered_out.splitlines()[0])
        assert clean_record["ok"] is True
        assert tampered_record["ok"] is False
        assert clean_record["note"] == verify.HONESTY_LINE
        assert tampered_record["note"] == verify.HONESTY_LINE
        assert verify.HONESTY_LINE in clean_out
        assert verify.HONESTY_LINE in tampered_out

    def test_an_empty_directory_is_not_read_as_a_clean_pass(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        """A wholly empty directory has nothing to verify. Someone pointing
        this at the wrong path and seeing a cheerful ``OK`` would be a real
        hazard, so this pins the deliberate behaviour: no runs found is a
        failure, not a vacuous success, and the exit code says so.
        """
        exit_code = verify.main([str(tmp_path)])
        out = capsys.readouterr().out

        assert exit_code == 1
        assert verify.HONESTY_LINE in out

    def test_a_directory_containing_no_runs_at_all_is_also_not_a_clean_pass(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        """Distinct trap from the empty-directory case above: this directory
        is non-empty but holds nothing ``metrics.jsonl``-shaped anywhere
        under it — an operator pointing the tool at the wrong root entirely.
        """
        (tmp_path / "notes.txt").write_text("not a run", encoding="utf-8")
        nested = tmp_path / "unrelated" / "subdir"
        nested.mkdir(parents=True)
        (nested / "readme.md").write_text("also not a run", encoding="utf-8")

        exit_code = verify.main([str(tmp_path)])
        out = capsys.readouterr().out

        assert exit_code == 1
        assert "no metrics.jsonl found" in out
        assert verify.HONESTY_LINE in out


# --------------------------------------------------------------------------- #
# The `verify` CLI, driven against a real RoundRunner — closes the last
# fixture-only gap: TestVerifyCli above only ever ran the CLI against
# _clean_verifiable_run, a hand-built fixture; TestIntegrityAgainstARealRun
# (test_runner_emits.py) drives the real runner but calls
# verify_metrics_chain/reconcile_summary directly, never through the CLI. So
# nothing in the suite ran verify.main() over real runner output — the same
# gap in miniature that let the original happy-path bug through.
# --------------------------------------------------------------------------- #


def _run_verify_cli_subprocess(*args: str) -> subprocess.CompletedProcess[str]:
    """Invoke the real ``python -m turing.research.loop.verify`` entry point.

    A genuine subprocess, not ``verify.main()`` called in-process: ``main()``
    calls ``asyncio.run()`` internally (see ``verify.py``), and this suite's
    tests need a real event loop of their own (``store``/``workspaces`` are
    plain fixtures, but producing the run drives ``runner.run_attempt``,
    which is a coroutine) to build the fixture the CLI is pointed at. Calling
    ``asyncio.run()`` from inside an already-running loop raises
    ``RuntimeError: asyncio.run() cannot be called from a running event
    loop``, so the CLI is exercised the way an operator actually invokes it:
    as a separate process. This also genuinely exercises argument parsing and
    the process exit code, which is what acceptance criterion 20 is about.
    """
    return subprocess.run(
        [sys.executable, "-m", "turing.research.loop.verify", *args],
        capture_output=True,
        text=True,
        timeout=30,
    )


class TestVerifyCliAgainstARealRun:
    """``verify.main()`` over a real, unmocked ``RoundRunner``'s output.

    Every prior CLI test in :class:`TestVerifyCli` drives the CLI against
    ``_clean_verifiable_run``, a hand-built fixture the emitter never
    actually produces. This class is the missing link: a real attempt, run
    through the real runner, verified through the real CLI entry point —
    the same shape that caught both the terminal-line bug (round 1) and the
    ``consumed_steps``-vs-``step`` bug (round 2).

    These tests are deliberately **not** ``async def``. ``verify.main()``
    calls ``asyncio.run()`` internally, and with this project's
    ``asyncio_mode = "auto"`` an ``async def`` test already runs inside a
    pytest-asyncio-managed event loop — nesting ``asyncio.run()`` inside that
    loop raises unconditionally, so there is no way to drive the real CLI
    entry point from inside one. The fixture (the real attempt) is instead
    produced with an explicit, self-contained ``asyncio.run()`` in a plain
    sync test, and the CLI itself is invoked as a subprocess (see
    ``_run_verify_cli_subprocess``), exercising its real ``asyncio.run()``
    with no nesting involved.
    """

    def test_verify_cli_exits_0_on_a_real_runner_attempt(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        problem = make_problem("s1")
        outcome = asyncio.run(
            runner.run_attempt(problem, make_config(), output_dir=store.round_dir(0))
        )
        assert outcome.attempt.state.name == "FAILED_WITHIN_CAP"

        metrics_dir = store.round_dir(0) / "attempts" / "s1"
        result = _run_verify_cli_subprocess(str(metrics_dir))

        assert result.returncode == 0, result.stdout + result.stderr
        assert "OK" in result.stdout
        assert verify.HONESTY_LINE in result.stdout

    def test_verify_cli_exits_0_on_a_real_runner_attempt_that_passed(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """Same, for the other terminal shape: a ``PassCriterion`` is met and
        the attempt ends ``PASSED`` rather than running out the cap.
        """
        from turing.research.loop.runner import PassCriterion

        problem = make_problem("s1", scores=(1.0, 5.0))
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        config = make_config(pass_criteria={"s1": PassCriterion(min_score=2.0)})
        outcome = asyncio.run(runner.run_attempt(problem, config, output_dir=store.round_dir(0)))
        assert outcome.attempt.state.name == "PASSED"

        metrics_dir = store.round_dir(0) / "attempts" / "s1"
        result = _run_verify_cli_subprocess(str(metrics_dir))

        assert result.returncode == 0, result.stdout + result.stderr
        assert "OK" in result.stdout
        assert verify.HONESTY_LINE in result.stdout


# --------------------------------------------------------------------------- #
# INCOMPLETE — the seventh false alarm, and the one that never cleared
#
# `metrics.json` is written once, when an attempt ends; the chain is extended
# after every step. "Intact chain, no summary" is therefore the ordinary shape
# of a run that is still going or was killed — and, once the operator's honest
# re-drive rotates that trio into `prior-N/`, a *permanent* shape. Reporting it
# as FAIL made the whole results root exit 1 forever on data nobody touched.
# Reporting it as OK would be worse. It is its own state, with its own exit
# code, and the tests below pin all three states against both hand-built
# fixtures (for exact exit codes) and a real, unmocked RoundRunner (for the
# scenario itself — hand-built fixtures hid every earlier false alarm here).
# --------------------------------------------------------------------------- #


class TestVerifyExitCodeContract:
    """One test per exit code, plus the precedence rule between two of them.

    These use ``verify.main()`` in-process (as :class:`TestVerifyCli` does)
    rather than a subprocess: the exit-code *contract* is what is under test,
    and ``main``'s return value is that contract. The real subprocess exit
    status is exercised against real runner output in
    :class:`TestIncompleteAgainstARealRun` below.
    """

    def test_exit_0_when_every_run_is_complete_and_clean(self, tmp_path: Path, capsys: Any) -> None:
        _clean_verifiable_run(tmp_path)

        exit_code = verify.main([str(tmp_path)])
        out = capsys.readouterr().out

        assert exit_code == verify.EXIT_OK == 0
        assert "OK" in out
        assert verify.HONESTY_LINE in out

    def test_exit_1_when_a_run_actually_failed(self, tmp_path: Path, capsys: Any) -> None:
        _clean_verifiable_run(tmp_path)
        _tamper_one_value(tmp_path)

        exit_code = verify.main([str(tmp_path)])
        out = capsys.readouterr().out

        assert exit_code == verify.EXIT_FAILED == 1
        assert "FAIL" in out
        assert verify.HONESTY_LINE in out

    def test_exit_2_when_a_run_is_incomplete_and_nothing_failed(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        """The state this whole change exists for: an intact chain with no
        summary beside it. Not 0 — a pre-writeup gate must still refuse to
        wave through numbers that are not final — and not 1, because nothing
        is wrong with what is on disk.
        """
        _unfinished_verifiable_run(tmp_path)

        exit_code = verify.main([str(tmp_path)])
        out = capsys.readouterr().out

        assert exit_code == verify.EXIT_INCOMPLETE == 2
        assert "INCOMPLETE" in out
        assert "FAIL" not in out
        assert verify.HONESTY_LINE in out

    def test_a_real_failure_outranks_an_incomplete_run(self, tmp_path: Path, capsys: Any) -> None:
        """Both present in one tree: the exit code must name the failure. An
        operator who only reads the integer must be steered to the broken
        chain, not to the attempt that simply has not finished.
        """
        _unfinished_verifiable_run(tmp_path / "still-running")
        _clean_verifiable_run(tmp_path / "tampered")
        _tamper_one_value(tmp_path / "tampered")

        exit_code = verify.main([str(tmp_path)])
        out = capsys.readouterr().out

        assert exit_code == verify.EXIT_FAILED
        # Both are still *named* in the output — only the single integer has
        # to choose between them.
        assert "INCOMPLETE" in out
        assert "FAIL" in out

    def test_a_broken_chain_with_no_summary_is_a_failure_not_an_incomplete_run(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        """The composition is asymmetric on purpose. "Unfinished" explains a
        missing *summary*; it never explains a chain that does not recompute.
        If it did, deleting one file would soften a tamper from 1 to 2.
        """
        _unfinished_verifiable_run(tmp_path)
        _tamper_one_value(tmp_path)

        exit_code = verify.main([str(tmp_path)])
        out = capsys.readouterr().out

        assert exit_code == verify.EXIT_FAILED
        assert "FAIL" in out
        assert verify.HONESTY_LINE in out

    def test_no_runs_found_is_a_failure_not_an_incomplete_run(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        """A path with nothing in it is not half-done, it is the wrong path —
        so it keeps the harder code rather than the gentler new one.
        """
        exit_code = verify.main([str(tmp_path)])
        capsys.readouterr()

        assert exit_code == verify.EXIT_FAILED
        assert exit_code != verify.EXIT_INCOMPLETE

    def test_incomplete_is_distinct_in_json_and_ok_stays_false(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        """A machine consumer must be able to tell the three apart, and must
        not start reading "unfinished" as "good": ``ok`` — the field every
        pre-existing script keys on — stays ``False``, and ``state`` is what
        carries the distinction.
        """
        _unfinished_verifiable_run(tmp_path)

        exit_code = verify.main([str(tmp_path), "--json"])
        out = capsys.readouterr().out

        assert exit_code == verify.EXIT_INCOMPLETE
        record = json.loads(out.splitlines()[0])
        assert record["ok"] is False
        assert record["state"] == "incomplete"
        assert record["chain"]["ok"] is True
        assert record["reconcile"]["state"] == "incomplete"
        assert record["reconcile"]["mismatches"] == []
        assert record["note"] == verify.HONESTY_LINE
        assert verify.HONESTY_LINE in out

    def test_the_qualifier_still_prints_verbatim_on_an_incomplete_run(
        self, tmp_path: Path, capsys: Any
    ) -> None:
        """HONESTY_LINE is unconditional. A third state is exactly the kind of
        new branch through ``main`` that could quietly skip it.
        """
        _unfinished_verifiable_run(tmp_path)

        verify.main([str(tmp_path)])
        human_out = capsys.readouterr().out
        verify.main([str(tmp_path), "--json"])
        json_out = capsys.readouterr().out

        assert human_out.splitlines()[-1] == verify.HONESTY_LINE
        assert json_out.splitlines()[-1] == verify.HONESTY_LINE


class _StallsAfterOneStepSolver:
    """Completes one real step, then blocks forever on the next.

    Stands in for a solver that was still working when the process went away.
    The attempt is torn down by cancelling the task driving ``run_attempt`` —
    a genuine ``asyncio`` cancellation, not a hand-built file layout — which
    unwinds out of the runner before it reaches ``write_attempt_summary``,
    leaving exactly what a closed subscription window leaves behind: a
    ``metrics.jsonl`` and sidecar with real appended lines, and no summary.
    """

    def __init__(self) -> None:
        self.calls = 0
        self.stalled = asyncio.Event()

    async def step(self, task: Any, attempt: Any) -> SolverStep:
        self.calls += 1
        if self.calls > 1:
            self.stalled.set()
            await asyncio.Event().wait()  # never returns; the caller cancels us
        return SolverStep(tokens=10, note="work")


def _drive_a_real_attempt_until_it_is_killed(
    *,
    store: TrajectoryStore,
    workspaces: TempWorkspaceProvider,
    clock: FakeClock,
    problem: Any,
    output_dir: Path,
) -> None:
    """Start a real ``run_attempt`` and cancel it mid-flight.

    The attempt is cancelled only once the solver has been asked for a
    *second* step, which guarantees the first step's metrics line — and the
    sidecar rewritten alongside it — are already durably on disk. That
    ordering is the whole point: the resulting directory must hold a chain
    worth verifying, not an empty one.
    """
    solver = _StallsAfterOneStepSolver()
    runner = make_runner(solver=solver, store=store, workspaces=workspaces, clock=clock)
    config = make_config()

    async def _drive() -> None:
        task = asyncio.create_task(runner.run_attempt(problem, config, output_dir=output_dir))
        await solver.stalled.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_drive())


class TestIncompleteAgainstARealRun:
    """The two reproductions, driven through the real runner and the real CLI.

    Hand-built fixtures hid all six earlier false alarms in this file, so the
    scenario itself is reproduced against an unmocked ``RoundRunner``: a real
    attempt, really cancelled mid-flight, really re-driven afterwards, really
    verified through ``python -m turing.research.loop.verify``.

    Not ``async def``, for the reason :class:`TestVerifyCliAgainstARealRun`
    documents: ``verify.main()`` calls ``asyncio.run()`` internally and this
    project runs tests in ``asyncio_mode = "auto"``, so the fixture is built
    under an explicit ``asyncio.run()`` in a sync test and the CLI is invoked
    as a subprocess.
    """

    def test_an_attempt_killed_mid_run_reports_incomplete_not_fail(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """Scenario 1: verifying a round while it is in progress, or right
        after a kill. The chain is intact, the summary was never written, and
        the operator is told the run is unfinished rather than accused.
        """
        _drive_a_real_attempt_until_it_is_killed(
            store=store,
            workspaces=workspaces,
            clock=clock,
            problem=make_problem("s1"),
            output_dir=store.round_dir(0),
        )

        metrics_dir = store.round_dir(0) / "attempts" / "s1"
        # Fixture assumptions, stated rather than assumed: a real chain with
        # at least one real line, and genuinely no summary.
        assert (metrics_dir / "metrics.jsonl").stat().st_size > 0
        assert not (metrics_dir / "metrics.json").exists()

        result = _run_verify_cli_subprocess(str(metrics_dir))

        assert result.returncode == verify.EXIT_INCOMPLETE, result.stdout + result.stderr
        assert "INCOMPLETE" in result.stdout
        assert "FAIL" not in result.stdout
        assert verify.HONESTY_LINE in result.stdout

    def test_a_killed_attempt_rotated_into_prior_n_does_not_fail_the_root_forever(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """Scenario 2, the permanent one — kill, then re-drive.

        ``_rotate_stale_metrics`` moves only the files that exist, so the
        killed attempt's ``prior-N/`` directory holds an intact chain and no
        ``metrics.json`` for as long as the results root survives. Under the
        old two-state verdict the whole root exited 1 forever, on completely
        honest data, with the honest repair run sitting green right next to
        it. Both generations are checked here in one CLI invocation over the
        whole results root, exactly as an operator runs it.
        """
        problem = make_problem("s1")
        output_dir = store.round_dir(0)

        _drive_a_real_attempt_until_it_is_killed(
            store=store,
            workspaces=workspaces,
            clock=clock,
            problem=problem,
            output_dir=output_dir,
        )
        # The operator re-drives; this one runs to completion and writes its
        # summary. The runner rotates the killed generation aside first.
        redriven = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        outcome = asyncio.run(redriven.run_attempt(problem, make_config(), output_dir=output_dir))
        assert outcome.attempt.state.name == "FAILED_WITHIN_CAP"

        metrics_dir = output_dir / "attempts" / "s1"
        prior_dirs = sorted(p for p in metrics_dir.iterdir() if p.is_dir())
        assert len(prior_dirs) == 1, f"expected one rotated-aside dir, got {prior_dirs}"
        prior_dir = prior_dirs[0]
        # The shape the defect report named: chain rotated, summary never
        # existed to rotate.
        assert (prior_dir / "metrics.jsonl").is_file()
        assert (prior_dir / "metrics.chain.json").is_file()
        assert not (prior_dir / "metrics.json").exists()
        assert (metrics_dir / "metrics.json").is_file()

        result = _run_verify_cli_subprocess(str(store.loop_dir.parent))

        assert result.returncode == verify.EXIT_INCOMPLETE, result.stdout + result.stderr
        assert "FAIL" not in result.stdout
        prior_line = next(
            line for line in result.stdout.splitlines() if line.startswith(str(prior_dir) + ":")
        )
        current_line = next(
            line for line in result.stdout.splitlines() if line.startswith(str(metrics_dir) + ":")
        )
        assert "INCOMPLETE" in prior_line
        assert "chain intact" in prior_line
        assert current_line.split(": ", 1)[1].startswith("OK")
        assert verify.HONESTY_LINE in result.stdout

    def test_a_rotated_killed_attempt_whose_chain_is_damaged_still_fails(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """The check this must not have weakened, on the same real fixture.

        ``docs/research-agent.md`` records that a process killed *mid-write*
        leaves a truncated final line, which rotation carries into
        ``prior-N/`` unchanged. That is a broken chain, and a broken chain is
        a failure whether or not a summary is missing beside it — INCOMPLETE
        must never become a way for real damage to travel as "unfinished".
        """
        problem = make_problem("s1")
        output_dir = store.round_dir(0)

        _drive_a_real_attempt_until_it_is_killed(
            store=store,
            workspaces=workspaces,
            clock=clock,
            problem=problem,
            output_dir=output_dir,
        )
        metrics_dir = output_dir / "attempts" / "s1"
        jsonl = metrics_dir / "metrics.jsonl"
        # A kill that landed mid-append, not between appends: the last line
        # is truncated where the write stopped.
        text = jsonl.read_text(encoding="utf-8")
        jsonl.write_text(text[: -(len(text) // 3)], encoding="utf-8")

        redriven = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        asyncio.run(redriven.run_attempt(problem, make_config(), output_dir=output_dir))

        result = _run_verify_cli_subprocess(str(store.loop_dir.parent))

        assert result.returncode == verify.EXIT_FAILED, result.stdout + result.stderr
        assert "FAIL" in result.stdout
        assert verify.HONESTY_LINE in result.stdout


class TestReadLogTailIsTheOneReaderOfARawLog:
    """``reconcile_summary`` no longer owns these derivations alone.

    ``runner.py`` has to make exactly the same ones to write a terminal
    ``metrics.json`` for an attempt whose exception it contained, and a second
    hand-written copy of "the last line" / "the last line carrying
    ``progress``" / "the first emitted score-series value" would drift into
    raising false mismatches on honest data -- the failure mode this module's
    ``_CORE_LINE_KEYS`` comment already warns about. These tests pin the
    shared reader against a real, unmocked attempt rather than a hand-built
    fixture, because hand-built fixtures hid all six earlier false alarms in
    this file.
    """

    async def test_the_tail_matches_the_summary_the_same_attempt_wrote(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        output_dir = store.round_dir(0)
        await runner.run_attempt(
            make_problem("s1", scores=(1.0, 2.0)), make_config(), output_dir=output_dir
        )
        metrics_dir = output_dir / "attempts" / "s1"

        tail = integrity_module.read_log_tail(metrics_dir)
        summary = json.loads((metrics_dir / "metrics.json").read_text())
        lines = [
            json.loads(raw)
            for raw in (metrics_dir / "metrics.jsonl").read_text().splitlines()
            if raw
        ]

        assert tail.line_count == len(lines) == summary["steps_recorded"]
        assert tail.last_line == lines[-1]
        assert tail.baseline_score == summary["baseline_score"]
        assert summary["best_score"] in tail.score_values
        # The header is the only place the attempt's identity survives on disk
        # once the in-memory ``Attempt`` is gone with its exception.
        assert tail.header is not None
        assert tail.header["attempt_id"] == summary["attempt_id"]
        assert tail.header["problem_id"] == "s1"

        # And the whole point: a summary built from this tail reconciles.
        assert (await reconcile_summary(metrics_dir)).state is ReconcileState.OK

    async def test_a_directory_with_no_log_reads_as_empty_rather_than_raising(
        self, tmp_path: Path
    ) -> None:
        """ "The attempt wrote no lines" is an ordinary state on both call sites.

        An attempt killed before its first append leaves exactly this, and the
        runner's containment path has to be able to ask about it without
        catching an exception to find out.
        """
        tail = integrity_module.read_log_tail(tmp_path)
        assert tail.line_count == 0
        assert tail.last_line is None
        assert tail.header is None
        assert tail.score_values == ()
        assert tail.baseline_score is None
        assert tail.final_progress is None


# --------------------------------------------------------------------------- #
# Round-8 regression -- reconciliation was vacuous for a crashed close-out
# --------------------------------------------------------------------------- #


class TestReconcileChecksTheSummaryAgainstTheHeaderBesideIt:
    """Every other field ``reconcile_summary`` checks is definitional at write
    time, so on its own it could never catch a dishonest close-out.

    ``runner._close_out_crashed_attempt`` derives its terminal ``metrics.json``
    from the same ``read_log_tail`` call reconciliation then re-derives from,
    which makes the resource numbers agree by construction -- deliberately so,
    since a summary built from anything else would trade an ``INCOMPLETE``
    verdict for a ``FAILED`` one. The consequence nobody had drawn is that
    those checks can only detect a *later edit*: the close-out's own new
    assertions -- which attempt, which round, which seed, and what killed it
    -- were compared against nothing at all.

    The proof was a summary claiming a different ``round_id`` and ``seed``
    than the chain header **in the same directory**, reconciling with zero
    mismatches. Identity now comes off the header the log actually carries.
    """

    @staticmethod
    async def _real_attempt(
        store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> Path:
        runner = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        output_dir = store.round_dir(0)
        await runner.run_attempt(
            make_problem("s1", scores=(1.0, 2.0)), make_config(), output_dir=output_dir
        )
        return output_dir / "attempts" / "s1"

    @pytest.mark.parametrize(
        ("field", "forged"),
        [
            ("attempt_id", "some-other-attempt"),
            ("problem_id", "some-other-problem"),
            ("round_id", "r99"),
            ("seed", 4321),
        ],
    )
    async def test_a_summary_naming_a_different_run_than_the_log_is_a_mismatch(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
        field: str,
        forged: object,
    ) -> None:
        """Driven against a real attempt, then one identity field edited.

        Nothing else is touched -- not a line, not the sidecar, not a single
        resource number -- so every pre-existing check still agrees. The whole
        finding is that the summary and the log beside it describe different
        runs.
        """
        metrics_dir = await self._real_attempt(store, workspaces, clock)
        summary = json.loads((metrics_dir / "metrics.json").read_text(encoding="utf-8"))
        assert summary[field] != forged, "fixture assumption broken: field not actually changed"
        summary[field] = forged
        _write_summary(metrics_dir, summary)

        verdict = await reconcile_summary(metrics_dir)
        assert verdict.state is ReconcileState.FAILED
        assert verdict.mismatches == (field,)
        # The chain itself is untouched: the two checks report different
        # faults and this one is not a chain break.
        assert (await verify_metrics_chain(metrics_dir)).ok is True

    async def test_the_cross_generation_close_out_shape_no_longer_reconciles(
        self,
        store: TrajectoryStore,
        workspaces: TempWorkspaceProvider,
        clock: FakeClock,
    ) -> None:
        """The exact artifact defect 2 produced, checked from the other side.

        Even if a summary asserting a later generation's ``round_id`` and
        ``seed`` were somehow written over an earlier generation's chain
        again, reconciliation now names both fields instead of blessing the
        file. Two independent guards, because the writer's guard only protects
        the writer.
        """
        metrics_dir = await self._real_attempt(store, workspaces, clock)
        summary = json.loads((metrics_dir / "metrics.json").read_text(encoding="utf-8"))
        summary["round_id"] = "gen2"
        summary["seed"] = 99
        summary["final_state"] = "crashed_in_harness:OSError"
        _write_summary(metrics_dir, summary)

        verdict = await reconcile_summary(metrics_dir)
        assert verdict.state is ReconcileState.FAILED
        assert sorted(verdict.mismatches) == ["round_id", "seed"]
        assert (await verify.verify_run(metrics_dir)).state is verify.RunState.FAILED

    async def test_honest_runs_of_every_shape_still_reconcile_clean(
        self, store: TrajectoryStore, workspaces: TempWorkspaceProvider, clock: FakeClock
    ) -> None:
        """The false-alarm half, and the only reason to trust the check above.

        Four honest shapes in one results tree: a plain attempt, an attempt
        that interrupted its operator, a re-driven attempt (whose live
        directory carries the *second* generation's identity), and the
        rotated ``prior-1/`` generation beneath it (whose summary and header
        must still agree with each other after being moved together).
        """
        output_dir = store.round_dir(0)
        plain = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        await plain.run_attempt(
            make_problem("plain", scores=(1.0, 2.0)), make_config(), output_dir=output_dir
        )

        escalating = make_runner(
            solver=FakeSolver(
                (
                    SolverStep(tokens=10, note="work"),
                    SolverStep(tokens=10, note="stuck", escalate=EscalationReason.HARNESS_FAILURE),
                    SolverStep(tokens=10, note="work"),
                )
            ),
            store=store,
            workspaces=workspaces,
            clock=clock,
            escalations=ScriptedEscalationChannel([EscalationVerdict.CONTINUE]),
        )
        await escalating.run_attempt(
            make_problem("escalated", scores=(1.0,)), make_config(), output_dir=output_dir
        )

        redriven = make_runner(solver=FakeSolver(), store=store, workspaces=workspaces, clock=clock)
        problem = make_problem("redriven", scores=(1.0, 2.0))
        await redriven.run_attempt(problem, make_config(), output_dir=output_dir)
        await redriven.run_attempt(
            problem, make_config(run_id="r01", seed=99), output_dir=output_dir
        )

        directories = [
            output_dir / "attempts" / "plain",
            output_dir / "attempts" / "escalated",
            output_dir / "attempts" / "redriven",
            output_dir / "attempts" / "redriven" / "prior-1",
        ]
        for directory in directories:
            verdict = await reconcile_summary(directory)
            assert verdict.state is ReconcileState.OK, f"{directory}: {verdict.mismatches}"

        # The two generations genuinely carry different identities -- this
        # would be a false alarm waiting to happen if the check compared a
        # summary against anything but its own directory's header.
        live = json.loads((output_dir / "attempts" / "redriven" / "metrics.json").read_text())
        rotated = json.loads(
            (output_dir / "attempts" / "redriven" / "prior-1" / "metrics.json").read_text()
        )
        assert (live["round_id"], live["seed"]) == ("r01", 99)
        assert (rotated["round_id"], rotated["seed"]) == ("r00", 7)

    async def test_a_summary_with_no_sidecar_beside_it_is_not_flagged_by_identity(
        self, tmp_path: Path
    ) -> None:
        """A missing header is ``verify_metrics_chain``'s finding, not this one.

        Reporting one fault as two findings is the drift this module warns
        about elsewhere, and a summary that has no header to disagree with has
        not been caught claiming anything.
        """
        lines = [_line(i) for i in range(3)]
        _write_raw_jsonl(tmp_path, lines)
        last = lines[-1]
        _write_summary(
            tmp_path,
            {
                "attempt_id": "whatever-it-likes",
                "round_id": "r99",
                "seed": 4321,
                "steps_recorded": len(lines),
                "consumed_steps": last["consumed_steps"],
                "consumed_tokens": last["tokens_used"],
                "consumed_wall_clock_seconds": last["wall_clock_s"],
                "cap_extensions": last["cap_extensions"],
                "final_progress": None,
                "outcome": last["outcome_code"],
                "baseline_score": lines[0]["speedup"],
                "best_score": last["speedup"],
            },
        )

        verdict = await reconcile_summary(tmp_path)
        assert verdict.state is ReconcileState.OK, verdict.mismatches

    async def test_a_summary_that_omits_an_identity_field_is_not_flagged(
        self, tmp_path: Path
    ) -> None:
        """ "Makes no claim" is not "makes a false claim".

        ``results.write_attempt_summary`` emits all four fields
        unconditionally, so a summary missing one came from outside that
        writer -- something reconciliation is not the place to re-litigate,
        and something that must not turn into a false ``FAILED`` on a
        directory whose numbers all agree.
        """
        lines = [_line(i) for i in range(3)]
        _build_chain(tmp_path, _header(), lines)
        last = lines[-1]
        _write_summary(
            tmp_path,
            {
                "steps_recorded": len(lines),
                "consumed_steps": last["consumed_steps"],
                "consumed_tokens": last["tokens_used"],
                "consumed_wall_clock_seconds": last["wall_clock_s"],
                "cap_extensions": last["cap_extensions"],
                "final_progress": None,
                "outcome": last["outcome_code"],
                "baseline_score": lines[0]["speedup"],
                "best_score": last["speedup"],
            },
        )

        verdict = await reconcile_summary(tmp_path)
        assert verdict.state is ReconcileState.OK, verdict.mismatches
