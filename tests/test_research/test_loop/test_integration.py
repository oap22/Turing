"""Instrument test: fake solver through noise-floor seeds, then two labeled passes.

Not a full round. The trajectory store, file-drop inbox, ntfy notifier and
operator CLI are the real implementations; the solver is a quality
multiplier that writes ``progress.txt`` and the verifier reads it back. No
network, no model calls, no real corpus.

The second pass runs at a higher multiplier, which is what a self-edit
would have bought. Loop 2 is not implemented; this only proves the
instrument can measure such a change.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from turing.research.contracts import (
    SCORE_SCALE_SPEEDUP,
    AttemptState,
    EscalationReason,
    EscalationVerdict,
    Problem,
    ProblemType,
    Split,
    VerificationResult,
    Verifier,
)
from turing.research.loop.cli import main as cli_main
from turing.research.loop.escalation import (
    FileDropDecisionInbox,
    NtfyEscalationNotifier,
    OperatorEscalationChannel,
)
from turing.research.loop.metrics import SaturationVerdict
from turing.research.loop.noise_floor import NoiseFloorConfig, NoiseFloorRunner
from turing.research.loop.protocols import SolverStep, SolverTask
from turing.research.loop.runner import RoundRunner
from turing.research.loop.trajectory import TrajectoryStore
from turing.research.problems.adapter import fingerprint_corpus

from .conftest import DEFAULT_CAP, ENGINE, FakeClock, RecordingNtfyClient, make_config

if TYPE_CHECKING:
    from turing.research.contracts import Attempt

    from .conftest import TempWorkspaceProvider

PROGRESS_FILE = "progress.txt"


@dataclass(frozen=True)
class WorkspaceReadingVerifier(Verifier):
    """Reads what the solver actually wrote. Frozen, like every verifier."""

    async def verify(self, workspace: Path) -> VerificationResult:
        path = workspace / PROGRESS_FILE
        value = float(path.read_text()) if path.exists() else 0.0
        return VerificationResult(
            problem_id=self.problem_id,
            verifier_id=self.verifier_id,
            score=value,
            passed_correctness=value > 0,
            score_scale=self.score_scale,
            raw_measurements={"progress": value},
        )


class WorkspaceWritingSolver:
    """Writes seed-dependent progress; ``quality`` stands in for the scaffold."""

    def __init__(self, quality: float = 1.0, *, escalate_on: str | None = None) -> None:
        self.quality = quality
        self._escalate_on = escalate_on
        self.escalated = False

    async def step(self, task: SolverTask, attempt: Attempt) -> SolverStep:
        if task.id == self._escalate_on and not self.escalated:
            self.escalated = True
            return SolverStep(
                tokens=5,
                note="every approach regressed",
                escalate=EscalationReason.NO_VIABLE_APPROACH,
            )
        value = self.quality * (1.0 + 0.01 * attempt.seed) * (attempt.step_index + 1)
        (attempt.workspace_path / PROGRESS_FILE).write_text(f"{value}")
        return SolverStep(tokens=10, wall_clock_seconds=0.5, note=f"wrote {value:.3f}")


def problem(
    problem_id: str,
    *,
    problem_type: ProblemType = ProblemType.SPEEDUP,
    split: Split = Split.PRACTICE,
) -> Problem:
    verifier = WorkspaceReadingVerifier(
        verifier_id=f"v-{problem_id}",
        problem_id=problem_id,
        description="reads progress.txt",
        score_scale=SCORE_SCALE_SPEEDUP,
    )
    return Problem(
        id=problem_id,
        problem_type=problem_type,
        goal=f"improve {problem_id}",
        workspace_template=Path("/nonexistent"),
        verifier=verifier,
        split=split,
    )


def corpus() -> list[Problem]:
    return [
        problem("speed-1"),
        problem("speed-2", split=Split.HELD_OUT),
        problem("kaggle-1", problem_type=ProblemType.KAGGLE),
    ]


async def test_full_loop_with_a_real_escalation_channel(
    tmp_path: Path, workspaces: TempWorkspaceProvider
) -> None:
    clock = FakeClock()
    store = TrajectoryStore(tmp_path / "results", "verifiable", clock=clock)
    inbox = FileDropDecisionInbox(tmp_path / "escalations", clock=clock)
    ntfy = RecordingNtfyClient()

    # The operator answers on the third poll exactly as they would from a phone
    # at 2am: by running the decision CLI against the inbox directory.
    polls = {"n": 0}

    async def operator_replies(_seconds: float) -> None:
        polls["n"] += 1
        if polls["n"] == 3:
            request = json.loads(next(inbox.directory.glob("*.request.json")).read_text())
            assert (
                cli_main(["--loop-dir", str(inbox.directory), request["request_id"], "continue"])
                == 0
            )

    channel = OperatorEscalationChannel(
        inbox,
        NtfyEscalationNotifier(ntfy, decision_hint="python -m turing.research.loop.cli"),  # type: ignore[arg-type]
        poll_interval_seconds=0.01,
        repush_interval_seconds=None,
        sleep=operator_replies,
        clock=clock,
    )

    # ---- the noise floor comes first: it has first claim on the budget ----- #
    floor_runner = RoundRunner(
        solver=WorkspaceWritingSolver(quality=1.0),
        workspaces=workspaces,
        trajectory=store,
        escalations=channel,
        clock=clock,
    )
    report = await NoiseFloorRunner(floor_runner, store).run(
        corpus(),
        NoiseFloorConfig(
            run_id="nf",
            eval_set_hash="",
            engine=ENGINE,
            seeds=(1, 2, 3),
            default_cap=DEFAULT_CAP,
        ),
    )
    assert store.noise_floor_path.exists()
    speed_floor = report.floor_for((ProblemType.SPEEDUP, Split.PRACTICE))
    assert speed_floor is not None
    assert speed_floor.is_degenerate is False  # the seeds really did differ
    # A seed run is not a round and never enters the trajectory.
    assert (await store.load_trajectory())["rounds"] == []

    # ---- round 0: the frozen-scaffold baseline ---------------------------- #
    runner = RoundRunner(
        solver=WorkspaceWritingSolver(quality=1.0, escalate_on="speed-1"),
        workspaces=workspaces,
        trajectory=store,
        escalations=channel,
        clock=clock,
    )
    base = await runner.run_round(corpus(), make_config(run_id="r00"), noise_floor=report)

    # The loop suspended on the escalation and resumed on `continue`.
    assert polls["n"] == 3
    assert len(list(inbox.directory.glob("*.decision.json"))) == 1
    assert base.record.escalation_count == 1
    assert ntfy.pushes and "escalation" in ntfy.pushes[0]
    speed_attempt = next(a for a in base.attempts if a.problem.id == "speed-1")
    assert speed_attempt.attempt.state is AttemptState.FAILED_WITHIN_CAP
    assert speed_attempt.decisions[0].verdict is EscalationVerdict.CONTINUE
    assert speed_attempt.best_result is not None  # it kept working after resuming
    # Round 0 has no parent, so no saturation verdict can be computed.
    assert all(a.verdict is SaturationVerdict.REFUSED_NO_PARENT for a in base.assessments)

    # ---- round 1: same eval set, a "better scaffold" ---------------------- #
    runner_r1 = RoundRunner(
        solver=WorkspaceWritingSolver(quality=2.0),
        workspaces=workspaces,
        trajectory=store,
        escalations=channel,
        clock=clock,
    )
    second = await runner_r1.run_round(
        corpus(),
        make_config(round_index=1, run_id="r01", parent_round_id="r00"),
        parent=base.record,
        noise_floor=report,
    )

    document = json.loads(store.trajectory_path.read_text())
    assert [row["round"] for row in document["rounds"]] == [0, 1]
    row = document["rounds"][1]
    assert row["parent_round"] == "r00"
    assert row["eval_set_hash"] == fingerprint_corpus(corpus())
    assert row["comparable_to_parent"] is True
    assert row["engine"]["scaffold_git_sha"] == "abc1234"
    assert row["human_interventions"] == 0  # nothing escalated in round 1
    assert row["noise_floor_measured"] is True

    # Per cell everywhere; no blended headline number anywhere in the row.
    assert set(row["primary"]) == {"speedup/practice", "speedup/held_out", "kaggle/practice"}
    assert set(row["delta"]) == set(row["primary"])
    assert all(isinstance(v, dict) for v in (row["primary"], row["delta"], row["cost_per_point"]))

    delta = second.record.delta_for(ProblemType.SPEEDUP, Split.PRACTICE)
    assert delta is not None
    assert delta.marginal_gain > 0
    assert delta.beats_noise_floor is True
    assert delta.cost_per_unit_gain is not None
    assert second.record.delta_for(ProblemType.KAGGLE, Split.PRACTICE) is not None
    assert all(a.verdict is SaturationVerdict.IMPROVING for a in second.assessments)

    # Artifacts are on disk in the prescribed layout.
    assert (store.loop_dir / "round-00" / "round.json").exists()
    assert (store.loop_dir / "round-01" / "attempts" / "kaggle-1.json").exists()
    assert (store.loop_dir / "noise-floor" / "noise-floor.json").exists()
    escalation_files = list((store.loop_dir / "round-00" / "escalations").glob("*.json"))
    assert len(escalation_files) == 1
    escalation = json.loads(escalation_files[0].read_text())
    assert escalation["decision"]["verdict"] == "continue"
    assert "advice" not in escalation["decision"]

    # Round 0's held-out cell was scored and reported, and stays out of the
    # loop-2 summary — the seam is the only path, and loop 1 never calls it.
    assert base.record.score_for(ProblemType.SPEEDUP, Split.HELD_OUT) is not None
    assert {ts.split for ts in second.record.self_edit_visible_scores()} == {Split.PRACTICE}


async def test_a_round_measured_on_a_new_eval_set_restarts_the_trajectory(
    tmp_path: Path, workspaces: TempWorkspaceProvider
) -> None:
    clock = FakeClock()
    store = TrajectoryStore(tmp_path / "results", "verifiable", clock=clock)
    inbox = FileDropDecisionInbox(tmp_path / "escalations", clock=clock)
    channel = OperatorEscalationChannel(inbox, None, poll_interval_seconds=0.01)
    runner = RoundRunner(
        solver=WorkspaceWritingSolver(),
        workspaces=workspaces,
        trajectory=store,
        escalations=channel,
        clock=clock,
    )
    base = await runner.run_round(corpus(), make_config(run_id="r00"))
    await runner.run_round(
        corpus()[:-1],
        make_config(
            round_index=1,
            run_id="r01",
            parent_round_id="r00",
        ),
        parent=base.record,
    )
    rows = json.loads(store.trajectory_path.read_text())["rounds"]
    assert rows[1]["comparable_to_parent"] is False
    assert rows[1]["trajectory_restart"] is True
    assert rows[1]["delta"] == {}
    assert all(entry["verdict"] == "refused_eval_set_changed" for entry in rows[1]["saturation"])
    with pytest.raises(Exception, match="trajectory restarts"):
        base.record.assert_comparable_to(
            type(base.record)(
                **{
                    **{
                        f.name: getattr(base.record, f.name)
                        for f in base.record.__dataclass_fields__.values()
                    },
                    "eval_set_hash": "corpus-v2",
                }
            )
        )
