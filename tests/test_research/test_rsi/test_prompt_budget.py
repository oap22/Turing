"""Prompt-size bounds and accounting: the loop pays for every prompt byte, so it measures them.

Three holes these tests close:

* a prompt over Linux's 128 KiB per-argument limit made ``execve`` fail with
  ``E2BIG``, which the engine reported as "could not start claude" (exit
  127) — the loop then aborted after three rounds with "is the claude CLI
  installed?", hiding that ``SCAFFOLD.md`` had outgrown argv;
* the self-edit summary grew without bound: one table row per round, and a
  notes tail capped in lines but not characters;
* nothing recorded how big any prompt was, so token efficiency across a
  trajectory could not be measured, let alone improved.
"""

from __future__ import annotations

import asyncio
import errno
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from turing.research.contracts import ContractViolationError
from turing.research.rsi import engine as engine_module
from turing.research.rsi.cheat import run_git
from turing.research.rsi.contracts import RoundRecord, RoundSummary, SelfEditInputs
from turing.research.rsi.engine import (
    PROMPT_MAX_BYTES,
    PROMPT_TOO_LARGE_EXIT,
    ClaudeCliEngine,
    CodexCliEngine,
    FakeEngine,
)
from turing.research.rsi.loop import StopReason, build_round_prompt
from turing.research.rsi.scaffold import (
    NOTES_TAIL_LINES,
    NOTES_TAIL_MAX_CHARS,
    SCAFFOLD_FILENAME,
    SCAFFOLD_MAX_BYTES,
    SUMMARY_ROUNDS_HEAD,
    SUMMARY_ROUNDS_TAIL,
    ScaffoldSelfEditStep,
    build_self_edit_inputs,
    compact_notes_tail,
    render_self_edit_prompt,
    scaffold_size_problem,
)
from turing.research.rsi.taxonomy import FailureCategory

from .test_loop import StubSelfEdit, _config, _events, _loop, _records, score_step
from .test_scaffold import (
    VERIFIER_CMD,
    CallableEngine,
    git,
    repo,  # noqa: F401  (pytest fixture, found by name)
)

if TYPE_CHECKING:
    from .conftest import RsiDirs


# --------------------------------------------------------------------------- #
# Engine: argv cap
# --------------------------------------------------------------------------- #


def _marker_cli(tmp_path: Path, name: str) -> Path:
    """A stand-in CLI that leaves a file behind when it actually ran."""
    cli = tmp_path / name
    cli.write_text("#!/bin/sh\ntouch RAN\nexit 0\n", encoding="utf-8")
    cli.chmod(0o755)
    return cli


class TestEngineArgvCap:
    @pytest.mark.parametrize("make", [ClaudeCliEngine, CodexCliEngine])
    async def test_prompt_over_cap_is_refused_before_the_cli_starts(
        self, tmp_path: Path, make: Any
    ) -> None:
        cli = _marker_cli(tmp_path, "cli")
        prompt = "x" * (PROMPT_MAX_BYTES + 1)
        result = await make(str(cli)).run(prompt, cwd=tmp_path, timeout_seconds=5)
        assert result.exit_code == PROMPT_TOO_LARGE_EXIT
        assert result.exit_code != 127, "must not read as 'CLI not found'"
        assert not result.timed_out
        assert str(PROMPT_MAX_BYTES + 1) in result.stderr
        assert "SCAFFOLD.md" in result.stderr
        assert not (tmp_path / "RAN").exists()

    async def test_prompt_at_cap_runs(self, tmp_path: Path) -> None:
        cli = _marker_cli(tmp_path, "cli")
        result = await ClaudeCliEngine(str(cli)).run(
            "y" * PROMPT_MAX_BYTES, cwd=tmp_path, timeout_seconds=10
        )
        assert result.exit_code == 0
        assert (tmp_path / "RAN").exists()

    def test_cap_counts_bytes_not_characters(self) -> None:
        # A multi-byte prompt under the cap in characters can still be over it in bytes.
        prompt = "é" * (PROMPT_MAX_BYTES // 2 + 1)
        assert len(prompt) < PROMPT_MAX_BYTES
        refused = engine_module.prompt_too_large(prompt, command="claude", started=0.0)
        assert refused is not None and refused.exit_code == PROMPT_TOO_LARGE_EXIT

    def test_cap_sits_under_the_linux_per_argument_limit(self) -> None:
        assert PROMPT_MAX_BYTES < 131072

    @pytest.mark.parametrize("make", [ClaudeCliEngine, CodexCliEngine])
    async def test_e2big_from_the_os_is_named_not_reported_as_missing_binary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, make: Any
    ) -> None:
        # Another OS (or a smaller limit) may refuse a prompt the cap allowed.
        async def boom(*args: Any, **kwargs: Any) -> Any:
            raise OSError(errno.E2BIG, "Argument list too long")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
        result = await make("some-cli").run("p", cwd=tmp_path, timeout_seconds=5)
        assert result.exit_code == PROMPT_TOO_LARGE_EXIT
        assert "too long" in result.stderr and "shrink" in result.stderr

    async def test_other_os_errors_still_read_as_not_started(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def boom(*args: Any, **kwargs: Any) -> Any:
            raise OSError(errno.EACCES, "Permission denied")

        monkeypatch.setattr(asyncio, "create_subprocess_exec", boom)
        result = await ClaudeCliEngine("some-cli").run("p", cwd=tmp_path, timeout_seconds=5)
        assert result.exit_code == 127
        assert "could not start" in result.stderr


# --------------------------------------------------------------------------- #
# Self-edit summary compaction
# --------------------------------------------------------------------------- #


class TestNotesTail:
    def test_short_notes_pass_through_unchanged(self) -> None:
        assert compact_notes_tail("a\nb\nc") == "a\nb\nc"

    def test_line_cap_keeps_the_last_lines(self) -> None:
        text = "\n".join(f"line {i}" for i in range(100))
        tail = compact_notes_tail(text)
        assert tail.splitlines()[0] == f"line {100 - NOTES_TAIL_LINES}"
        assert len(tail.splitlines()) == NOTES_TAIL_LINES

    def test_character_cap_keeps_the_newest_text_and_says_what_it_dropped(self) -> None:
        # Forty lines that are each far longer than the cap allows in total.
        lines = [f"{i:02d}" + "x" * 3_000 for i in range(NOTES_TAIL_LINES)]
        tail = compact_notes_tail("\n".join(lines))
        assert len(tail) <= NOTES_TAIL_MAX_CHARS + 80
        first, *rest = tail.splitlines()
        assert first.startswith("[notes tail cut: ")
        assert rest[-1] == lines[-1], "the newest line survives whole"
        assert all(line in lines for line in rest), "the cut lands on a line boundary"
        dropped = int(first.split(": ")[1].split(" ")[0])
        assert dropped == len("\n".join(lines)) - len("\n".join(rest))

    def test_one_giant_line_is_cut_mid_line(self) -> None:
        tail = compact_notes_tail("z" * 50_000)
        first, body = tail.split("\n", 1)
        assert first.startswith("[notes tail cut: ")
        assert body == "z" * NOTES_TAIL_MAX_CHARS

    def test_refuses_negative_bounds(self) -> None:
        with pytest.raises(ContractViolationError):
            compact_notes_tail("x", lines=-1)

    def test_build_inputs_applies_the_character_cap(self, rsi_dirs: RsiDirs) -> None:
        (rsi_dirs.sandbox / "NOTES.md").write_text("\n".join("y" * 4_000 for _ in range(40)))
        inputs = build_self_edit_inputs(
            rsi_dirs.config, [], rsi_dirs.sandbox, forbidden=[VERIFIER_CMD]
        )
        assert len(inputs.notes_tail) <= NOTES_TAIL_MAX_CHARS + 80


def _summaries(n: int) -> tuple[RoundSummary, ...]:
    out = []
    for i in range(1, n + 1):
        cats: tuple[str, ...] = ()
        if i % 3 == 0:
            cats = ("no_progress",)
        if i % 7 == 0:
            cats = ("verifier_failed",)
        out.append(
            RoundSummary(
                round=i,
                score=None if "verifier_failed" in cats else float(i) / 10,
                passed="verifier_failed" not in cats,
                categories=cats,
                wall_seconds=10.0 * i,
            )
        )
    return tuple(out)


def _inputs_with(rounds: tuple[RoundSummary, ...], scaffold: str = "# S\n") -> SelfEditInputs:
    return SelfEditInputs(
        round_index=len(rounds),
        best_score=max((r.score for r in rounds if r.score is not None), default=None),
        rounds=rounds,
        taxonomy_counts={},
        scaffold_text=scaffold,
        notes_tail="",
    )


class TestRoundsTableElision:
    def test_few_rounds_are_all_listed(self) -> None:
        n = SUMMARY_ROUNDS_HEAD + SUMMARY_ROUNDS_TAIL
        prompt = render_self_edit_prompt(_inputs_with(_summaries(n)))
        assert "elided" not in prompt
        assert all(f"\n| {i} | " in prompt for i in range(1, n + 1))

    def test_long_run_keeps_head_and_tail_and_aggregates_the_middle(self) -> None:
        n = 60
        prompt = render_self_edit_prompt(_inputs_with(_summaries(n)))
        head = range(1, SUMMARY_ROUNDS_HEAD + 1)
        tail = range(n - SUMMARY_ROUNDS_TAIL + 1, n + 1)
        assert all(f"\n| {i} | " in prompt for i in (*head, *tail))
        middle = range(SUMMARY_ROUNDS_HEAD + 1, n - SUMMARY_ROUNDS_TAIL + 1)
        assert not any(f"\n| {i} | " in prompt for i in middle)
        row = next(line for line in prompt.splitlines() if "elided" in line)
        assert row.startswith(f"| {middle.start}–{middle.stop - 1} |")
        expected = [s for s in _summaries(n) if s.round in middle]
        best = max(s.score for s in expected if s.score is not None)
        assert f"best {best:.6g}" in row
        assert f"{sum(1 for s in expected if s.passed)}/{len(expected)}" in row
        assert f"{len(expected)} rounds elided" in row
        assert f"no_progress×{sum('no_progress' in s.categories for s in expected)}" in row
        assert f"verifier_failed×{sum('verifier_failed' in s.categories for s in expected)}" in row

    def test_prompt_size_is_flat_in_rounds(self) -> None:
        small = len(render_self_edit_prompt(_inputs_with(_summaries(20))))
        large = len(render_self_edit_prompt(_inputs_with(_summaries(400))))
        # Only the digits of larger round numbers and wall times differ.
        assert large - small < 120, (small, large)

    def test_prompt_states_the_cap_and_the_current_size(self) -> None:
        scaffold = "# Sé\n"
        prompt = render_self_edit_prompt(_inputs_with(_summaries(2), scaffold=scaffold))
        assert f"Hard cap: {SCAFFOLD_MAX_BYTES} bytes" in prompt
        assert f"(it is {len(scaffold.encode())} bytes now)" in prompt


# --------------------------------------------------------------------------- #
# Scaffold size cap: enforced by the step and, independently, by the loop
# --------------------------------------------------------------------------- #


class TestScaffoldCap:
    def test_size_problem_boundary(self) -> None:
        assert scaffold_size_problem(SCAFFOLD_MAX_BYTES) is None
        problem = scaffold_size_problem(SCAFFOLD_MAX_BYTES + 1)
        assert problem is not None and "over the" in problem

    def test_cap_leaves_room_under_the_argv_cap(self) -> None:
        body = build_round_prompt(
            round_no=1, results_dir=Path("/r"), scaffold_text="", verifier_command="sh v.sh"
        )
        assert SCAFFOLD_MAX_BYTES + len(body.encode()) < PROMPT_MAX_BYTES

    async def test_step_rejects_an_oversized_scaffold_and_restores_it(
        self,
        rsi_dirs: RsiDirs,
        repo: Path,  # noqa: F811
    ) -> None:
        original = (repo / SCAFFOLD_FILENAME).read_text()
        head = git(repo, "rev-parse", "HEAD")

        def grow(cwd: Path) -> None:
            (cwd / SCAFFOLD_FILENAME).write_text("# S\n" + "advice " * (SCAFFOLD_MAX_BYTES // 6))

        step = ScaffoldSelfEditStep(CallableEngine(grow), rsi_dirs.config, repo, timeout_seconds=5)
        inputs = build_self_edit_inputs(rsi_dirs.config, [], repo, forbidden=[VERIFIER_CMD])
        assert await step.propose(inputs) is None
        assert (repo / SCAFFOLD_FILENAME).read_text() == original
        assert git(repo, "rev-parse", "HEAD") == head
        assert git(repo, "status", "--porcelain") == ""

    async def test_step_keeps_a_scaffold_exactly_at_the_cap(
        self,
        rsi_dirs: RsiDirs,
        repo: Path,  # noqa: F811
    ) -> None:
        text = "# S\n" + "a" * (SCAFFOLD_MAX_BYTES - 4)
        assert len(text.encode()) == SCAFFOLD_MAX_BYTES

        def edit(cwd: Path) -> None:
            (cwd / SCAFFOLD_FILENAME).write_text(text)

        step = ScaffoldSelfEditStep(CallableEngine(edit), rsi_dirs.config, repo, timeout_seconds=5)
        inputs = build_self_edit_inputs(rsi_dirs.config, [], repo, forbidden=[VERIFIER_CMD])
        assert await step.propose(inputs) is not None

    async def test_loop_rejects_an_oversized_scaffold_a_custom_step_committed(
        self, rsi_dirs: RsiDirs
    ) -> None:
        # A step that skips the built-in check cannot smuggle a huge scaffold past the loop.
        results = rsi_dirs.results
        big = "# S\n" + "b" * SCAFFOLD_MAX_BYTES
        step = StubSelfEdit(rsi_dirs.sandbox, text=big)
        engine = FakeEngine(script=[score_step(v, results=results) for v in (1, 2, 3, 4)])
        cfg = _config(rsi_dirs, rounds=4, self_edit_every=2, self_edit_budget=2)
        outcome = await _loop(rsi_dirs, engine, config=cfg, self_edit=step).run()
        assert outcome.stop_reason is StopReason.ROUNDS_EXHAUSTED
        assert outcome.self_edits == 0
        events = _events(results)
        assert [e["event"] for e in events] == ["self_edit_rejected", "self_edit_rejected"]
        assert all("over the" in e["reason"] for e in events)
        assert (rsi_dirs.sandbox / SCAFFOLD_FILENAME).read_text() != big
        # Rounds after the rejection still ran on the small scaffold.
        assert not engine.calls[2].prompt.startswith(big)
        assert len(engine.calls[2].prompt.encode()) < PROMPT_MAX_BYTES


# --------------------------------------------------------------------------- #
# Accounting
# --------------------------------------------------------------------------- #


class TestPromptAccounting:
    def test_round_record_carries_prompt_chars_and_round_trips(self) -> None:
        rec = RoundRecord(round=1, started=1, ended=2, exit=0, prompt_chars=1234)
        payload = rec.to_json()
        assert list(payload)[:4] == ["round", "started", "ended", "exit"]
        assert payload["prompt_chars"] == 1234
        assert RoundRecord.from_json(json.loads(json.dumps(payload))) == rec

    def test_old_lines_without_the_key_load_with_none(self) -> None:
        rec = RoundRecord.from_json({"round": 1, "started": 1, "ended": 2, "exit": 0})
        assert rec.prompt_chars is None
        assert rec.to_json()["prompt_chars"] is None

    def test_negative_or_bool_prompt_chars_refused(self) -> None:
        with pytest.raises(ContractViolationError):
            RoundRecord(round=1, started=1, ended=2, exit=0, prompt_chars=-1)
        with pytest.raises(ContractViolationError):
            RoundRecord(round=1, started=1, ended=2, exit=0, prompt_chars=True)

    async def test_every_round_line_records_the_prompt_the_engine_saw(
        self, rsi_dirs: RsiDirs
    ) -> None:
        results = rsi_dirs.results
        engine = FakeEngine(script=[score_step(v, results=results) for v in (1, 2, 3)])
        cfg = _config(rsi_dirs, rounds=3, self_edit_every=0)
        await _loop(rsi_dirs, engine, config=cfg).run()
        recs = _records(results)
        assert [r["prompt_chars"] for r in recs] == [len(c.prompt) for c in engine.calls]
        assert all(r["prompt_chars"] > 0 for r in recs)

    async def test_self_edit_events_record_prompt_and_scaffold_sizes(
        self, rsi_dirs: RsiDirs
    ) -> None:
        results = rsi_dirs.results
        step = StubSelfEdit(rsi_dirs.sandbox, text="GOOD ADVICE\n")
        engine = FakeEngine(script=[score_step(v, results=results) for v in (3, 3, 3, 4)])
        cfg = _config(rsi_dirs, rounds=4, self_edit_every=2, self_edit_budget=1)
        await _loop(rsi_dirs, engine, config=cfg, self_edit=step).run()
        edit = next(e for e in _events(results) if e["event"] == "self_edit")
        assert edit["prompt_chars"] == len(render_self_edit_prompt(step.seen[0]))
        assert edit["scaffold_bytes_before"] == len(step.seen[0].scaffold_text.encode())
        assert edit["scaffold_bytes_after"] == len(b"GOOD ADVICE\n")
        # The rounds under the new scaffold show its cost on their own lines.
        recs = _records(results)
        assert recs[2]["prompt_chars"] == len(engine.calls[2].prompt)
        assert recs[2]["prompt_chars"] < recs[0]["prompt_chars"]  # the default scaffold is longer

    async def test_rejected_self_edit_event_still_records_the_prompt_size(
        self, rsi_dirs: RsiDirs
    ) -> None:
        results = rsi_dirs.results
        step = StubSelfEdit(rsi_dirs.sandbox, also_touch="helper.py")
        engine = FakeEngine(script=[score_step(v, results=results) for v in (1, 2)])
        cfg = _config(rsi_dirs, rounds=2, self_edit_every=2, self_edit_budget=1)
        await _loop(rsi_dirs, engine, config=cfg, self_edit=step).run()
        rejected = next(e for e in _events(results) if e["event"] == "self_edit_rejected")
        assert rejected["prompt_chars"] == len(render_self_edit_prompt(step.seen[0]))

    async def test_loop_compacts_a_huge_notes_tail_before_the_self_edit(
        self, rsi_dirs: RsiDirs
    ) -> None:
        results = rsi_dirs.results
        step = StubSelfEdit(rsi_dirs.sandbox, text="fine\n")

        def bloat(cwd: Path) -> None:
            (cwd / "NOTES.md").write_text("\n".join("n" * 5_000 for _ in range(40)))

        engine = FakeEngine(
            script=[score_step(1, results=results), score_step(2, results=results, extra=bloat)]
        )
        cfg = _config(rsi_dirs, rounds=2, self_edit_every=2, self_edit_budget=1)
        await _loop(rsi_dirs, engine, config=cfg, self_edit=step).run()
        assert len(step.seen) == 1
        assert len(step.seen[0].notes_tail) <= NOTES_TAIL_MAX_CHARS + 80
        assert step.seen[0].notes_tail.startswith("[notes tail cut: ")

    async def test_engine_refusal_for_a_huge_prompt_is_an_engine_error_round(
        self, rsi_dirs: RsiDirs, tmp_path: Path
    ) -> None:
        # The real engine, a scaffold seeded by the operator over the argv cap:
        # the round is an engine_error whose stderr says why, not a mystery 127.
        (rsi_dirs.sandbox / SCAFFOLD_FILENAME).write_text("s" * (PROMPT_MAX_BYTES + 10))
        cli = _marker_cli(tmp_path, "claude")
        cfg = _config(rsi_dirs, rounds=1, self_edit_every=0)
        loop = _loop(rsi_dirs, FakeEngine(script=[]), config=cfg)
        loop.engine = ClaudeCliEngine(str(cli))
        outcome = await loop.run()
        assert outcome.stop_reason is StopReason.ROUNDS_EXHAUSTED
        rec = _records(rsi_dirs.results)[0]
        assert rec["exit"] == PROMPT_TOO_LARGE_EXIT
        assert FailureCategory.ENGINE_ERROR.value in rec["categories"]
        assert rec["prompt_chars"] > PROMPT_MAX_BYTES
        assert not (tmp_path / "RAN").exists()

    async def test_git_env_is_neutral_for_the_size_check(self, rsi_dirs: RsiDirs) -> None:
        # cat-file on the committed blob is how the loop measures the scaffold.
        results = rsi_dirs.results
        step = StubSelfEdit(rsi_dirs.sandbox, text="tiny\n")
        engine = FakeEngine(script=[score_step(v, results=results) for v in (1, 2)])
        cfg = _config(rsi_dirs, rounds=2, self_edit_every=2, self_edit_budget=1)
        await _loop(rsi_dirs, engine, config=cfg, self_edit=step).run()
        edit = next(e for e in _events(results) if e["event"] == "self_edit")
        size = await run_git(
            rsi_dirs.sandbox, "cat-file", "-s", f"{edit['scaffold_sha']}:{SCAFFOLD_FILENAME}"
        )
        assert int(size.stdout.strip()) == edit["scaffold_bytes_after"] == 5
