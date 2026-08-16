"""``python -m turing.research.loop.run`` — the driver's refusals.

The driver's happy path needs a real Claude subscription, the frozen baselines,
and hours of compute, so what is tested here is the part that must hold without
any of that: it refuses, at zero compute, for a reason that names the missing
piece. Every one of these is a refusal the operator will meet before their
first real round.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, ClassVar

import pytest

from turing.research.contracts import ContractViolationError
from turing.research.loop import run as run_cli
from turing.research.loop.metrics import MIN_NOISE_FLOOR_SEEDS
from turing.research.loop.runner import RoundRunner

from .conftest import FakeSolver, ScriptedEscalationChannel, TempWorkspaceProvider, make_problem

if TYPE_CHECKING:
    from pathlib import Path

    from turing.research.loop.trajectory import TrajectoryStore


def _args(tmp_path: Path, *extra: str) -> list[str]:
    return [
        "--loop-slug",
        "test-loop",
        "--harness-root",
        str(tmp_path / "harness"),
        "--reference-root",
        str(tmp_path / "reference"),
        "--scaffold-repo",
        str(tmp_path / "scaffold"),
        *extra,
    ]


class TestTheDriverRefusesWhatIsNotWired:
    def test_a_round_above_zero_names_both_missing_pieces(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = run_cli.main(_args(tmp_path, "--round", "1"))
        assert code == run_cli.EXIT_REFUSED
        err = capsys.readouterr().err
        assert "round-NN/round.json" in err
        assert "self-edit" in err
        assert "Drive --round 0." in err

    def test_missing_harness_scripts_refuse_before_any_compute(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The catalog declares its benchmark drivers; they are not written yet.

        This is the refusal a real operator hits first, and it must land
        before a workspace is copied or a model is called — which is why the
        corpus is built before the backend.
        """
        code = run_cli.main(_args(tmp_path))
        assert code == run_cli.EXIT_REFUSED
        err = capsys.readouterr().err
        assert "declared harness script(s) do not exist" in err
        assert "at zero compute" in err

    def test_a_scaffold_repo_that_is_not_a_checkout_is_refused(self, tmp_path: Path) -> None:
        """``scaffold_git_sha`` is lineage, so it is read, never assumed."""
        with pytest.raises(ContractViolationError, match="is not a git checkout"):
            run_cli._scaffold_sha(tmp_path / "not-a-repo")


class TestTheParser:
    def test_resume_is_the_default(self, tmp_path: Path) -> None:
        assert run_cli.build_parser().parse_args(_args(tmp_path)).resume is True

    def test_no_resume_turns_it_off(self, tmp_path: Path) -> None:
        parsed = run_cli.build_parser().parse_args(_args(tmp_path, "--no-resume"))
        assert parsed.resume is False

    def test_the_default_seed_set_can_measure_a_floor(self, tmp_path: Path) -> None:
        parsed = run_cli.build_parser().parse_args(_args(tmp_path))
        assert len(parsed.seeds) >= MIN_NOISE_FLOOR_SEEDS
        assert len(set(parsed.seeds)) == len(parsed.seeds)


# --------------------------------------------------------------------------- #
# --dry-run and --yes, reached through main() with the expensive edges faked
#
# Only three things are faked: the corpus (two scripted problems instead of the
# speedup catalog, whose harness scripts do not exist yet), the scaffold sha
# (no git checkout in a tmp dir) and the Anthropic SDK client. Everything else
# is real — in particular ``fingerprint_corpus`` and ``bind_eval_set_hash``,
# because the round-2 blocker was exactly that the driver hashed the corpus
# with the adapter's ``eval_set_material`` and the runners re-derived it
# without, so every real ``--yes`` run refused itself after creating the
# results tree, and a fixture that stubbed the hash to ``"f" * 64`` never saw
# it. The fake adapter's material is therefore deliberately non-empty.
# --------------------------------------------------------------------------- #


class _FakeAdapter:
    specs: tuple = ()  # type: ignore[type-arg]

    def missing_harness_scripts(self) -> tuple:  # type: ignore[type-arg]
        return ()

    def eval_set_material(self) -> tuple[str, ...]:
        return ("significance_multiplier=1.5", "spec:s1", "spec:s2")

    def harness_identity(self) -> tuple[str, ...]:
        return ("python_executable=/fake/python",)


class _FakeBackend:
    constructed = 0
    closed = 0

    @classmethod
    def from_settings(cls, settings: object, **kwargs: object) -> _FakeBackend:
        _FakeBackend.constructed += 1
        return cls()

    async def aclose(self) -> None:
        _FakeBackend.closed += 1


@pytest.fixture
def faked_driver(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Everything past argument parsing except the corpus, the sha and the SDK.

    Returns the results root, which must not exist before the call and — for
    a dry run or a refused run — after it either.
    """
    results_root = tmp_path / "results"
    scaffold = tmp_path / "scaffold"
    scaffold.mkdir()
    (scaffold / ".git").mkdir()
    monkeypatch.setenv("TURING_RESEARCH_RESULTS_ROOT", str(results_root))
    monkeypatch.setenv("TURING_RESEARCH_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    monkeypatch.setattr(
        run_cli,
        "_load_corpus",
        lambda h, r: (
            _FakeAdapter(),
            (make_problem("s1", scores=(1.0,)), make_problem("s2", scores=(2.0,))),
        ),
    )
    monkeypatch.setattr(run_cli, "_scaffold_sha", lambda repo: "d" * 40)
    monkeypatch.setattr(
        "turing.research.backends.claude.ClaudeBackend.from_settings",
        _FakeBackend.from_settings,
    )
    _FakeBackend.constructed = 0
    _FakeBackend.closed = 0
    return results_root


@pytest.fixture
def fake_runners(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[RoundRunner]:
    """Swap the production solver for the scripted one; the runners stay real.

    ``_build_runner`` is the seam: the real ``RoundRunner`` and
    ``NoiseFloorRunner`` drive the fake corpus end to end against the real
    ``TrajectoryStore`` the driver built, so a ``--yes`` run through
    ``main()`` exercises the same resume, lock and finished-round paths a
    real one does. Returns the runners built, in order.
    """
    built: list[RoundRunner] = []

    def _build(*, solver: object, store: TrajectoryStore, settings: object, escalations_dir: Path):  # type: ignore[no-untyped-def]
        runner = RoundRunner(
            solver=FakeSolver(),
            workspaces=TempWorkspaceProvider(tmp_path / "workspaces"),
            trajectory=store,
            escalations=ScriptedEscalationChannel(),
        )
        built.append(runner)
        return runner

    monkeypatch.setattr(run_cli, "_build_runner", _build)
    return built


class TestDryRunAndYes:
    def test_a_dry_run_exits_zero_and_creates_nothing_under_the_results_root(
        self, faked_driver: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert not faked_driver.exists()
        code = run_cli.main(_args(tmp_path, "--dry-run"))
        out = capsys.readouterr()
        assert code == run_cli.EXIT_OK, out.err
        assert not faked_driver.exists(), sorted(faked_driver.rglob("*"))
        assert "dry run" in out.out
        assert "nothing was written under the results root" in out.out
        # The plan names what the real run would use.
        assert "seeds     [1, 2, 3]" in out.out
        assert "cap       " in out.out
        # The backend client is built (a missing credential refuses here) and closed.
        assert _FakeBackend.constructed == 1
        assert _FakeBackend.closed == 1

    def test_a_dry_run_validates_the_seed_set_like_a_real_run(
        self, faked_driver: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``--seeds 1 --dry-run`` used to exit 0 while ``--seeds 1`` refused."""
        code = run_cli.main(_args(tmp_path, "--seeds", "1", "--dry-run"))
        err = capsys.readouterr().err
        assert code == run_cli.EXIT_REFUSED
        assert f"at least {MIN_NOISE_FLOOR_SEEDS} seeds" in err
        assert not faked_driver.exists()

    def test_a_dry_run_validates_distinct_seeds_too(
        self, faked_driver: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = run_cli.main(_args(tmp_path, "--seeds", "1", "1", "2", "--dry-run"))
        assert code == run_cli.EXIT_REFUSED
        assert "seeds must be distinct" in capsys.readouterr().err

    def test_a_real_run_without_yes_refuses_after_preflight_and_writes_nothing(
        self, faked_driver: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = run_cli.main(_args(tmp_path))
        out = capsys.readouterr()
        assert code == run_cli.EXIT_REFUSED
        assert "--yes" in out.err
        assert "spends subscription compute" in out.err
        # One line, as promised.
        assert len([line for line in out.err.splitlines() if line.strip()]) == 1
        # Preflight ran (the plan printed) but the results root was not touched.
        assert "loop      " in out.out
        assert not faked_driver.exists()
        assert _FakeBackend.closed == 1

    def test_yes_is_not_needed_for_a_dry_run(
        self, faked_driver: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert run_cli.main(_args(tmp_path, "--dry-run")) == run_cli.EXIT_OK
        assert "--yes" not in capsys.readouterr().err

    def test_the_parser_accepts_yes(self, tmp_path: Path) -> None:
        parsed = run_cli.build_parser().parse_args(_args(tmp_path, "--yes"))
        assert parsed.yes is True
        assert run_cli.build_parser().parse_args(_args(tmp_path)).yes is False

    def test_help_says_a_run_must_acknowledge_spending(self) -> None:
        text = run_cli.build_parser().format_help()
        assert "--yes" in text
        assert "subscription compute" in text


# --------------------------------------------------------------------------- #
# The real hash on both sides, the lock, and the finished-round preflight —
# all through main() (round-2 adversarial review of RES-17)
# --------------------------------------------------------------------------- #


class _RecordingFloorRunner:
    """Stands in for ``NoiseFloorRunner`` to prove the driver *reaches* it.

    Records the call it was made with and returns a report-shaped object;
    with ``--noise-floor-only`` the driver stops right after.
    """

    calls: ClassVar[list[tuple[tuple[str, ...], object, bool]]] = []

    def __init__(self, runner: object, store: object) -> None:
        self.skipped_seeds: tuple[int, ...] = ()

    async def run(self, corpus, config, *, resume: bool = True):  # type: ignore[no-untyped-def]
        from types import SimpleNamespace

        _RecordingFloorRunner.calls.append((tuple(p.id for p in corpus), config, resume))
        return SimpleNamespace(floors=(), seeds=config.seeds)


class TestTheRealHashBindsOnBothSides:
    """Round-2 blocker: the driver promised ``fingerprint_corpus(corpus, extra=…)``
    and the runners re-derived ``fingerprint_corpus(corpus)``; every real
    ``--yes`` run refused itself after creating the results tree, and the
    dry run said 0."""

    def test_a_dry_run_binds_the_hash_the_runners_bind_and_exits_zero(
        self, faked_driver: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = run_cli.main(_args(tmp_path, "--dry-run"))
        out = capsys.readouterr()
        assert code == run_cli.EXIT_OK, out.err
        assert not faked_driver.exists()
        # The printed hash *is* the one both runners will derive.
        from turing.research.problems.adapter import fingerprint_corpus

        adapter, corpus = run_cli._load_corpus(tmp_path, tmp_path)
        expected = fingerprint_corpus(corpus, extra=adapter.eval_set_material())
        assert expected in out.out
        assert _FakeBackend.closed == 1

    def test_a_yes_run_reaches_the_floor_runner_without_refusing(
        self,
        faked_driver: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _RecordingFloorRunner.calls = []
        monkeypatch.setattr(run_cli, "NoiseFloorRunner", _RecordingFloorRunner)
        code = run_cli.main(_args(tmp_path, "--yes", "--noise-floor-only"))
        out = capsys.readouterr()
        assert code == run_cli.EXIT_OK, out.err
        assert "refused" not in out.err
        assert len(_RecordingFloorRunner.calls) == 1
        corpus_ids, config, resume = _RecordingFloorRunner.calls[0]
        assert corpus_ids == ("s1", "s2")
        assert resume is True
        # The config carries the material the hash was made with, and the
        # hash it carries is the one the runners re-derive from that material.
        from turing.research.problems.adapter import bind_eval_set_hash

        adapter, corpus = run_cli._load_corpus(tmp_path, tmp_path)
        assert config.eval_set_material == adapter.eval_set_material()
        assert (
            bind_eval_set_hash(corpus, config.eval_set_hash, extra=config.eval_set_material)
            == config.eval_set_hash
        )
        assert config.harness_identity == adapter.harness_identity()
        assert _FakeBackend.closed == 1

    def test_the_lock_is_released_after_a_yes_run(
        self, faked_driver: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _RecordingFloorRunner.calls = []
        monkeypatch.setattr(run_cli, "NoiseFloorRunner", _RecordingFloorRunner)
        assert run_cli.main(_args(tmp_path, "--yes", "--noise-floor-only")) == run_cli.EXIT_OK
        assert not (faked_driver / "loop-test-loop" / ".driver.lock").exists()


class TestTheDriverEndToEnd:
    """``--yes`` through ``main()`` with the real runners and a scripted solver."""

    def test_a_yes_run_finishes_and_the_identical_rerun_is_a_no_op(
        self,
        faked_driver: Path,
        fake_runners: list[RoundRunner],
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        code = run_cli.main(_args(tmp_path, "--yes"))
        out = capsys.readouterr()
        assert code == run_cli.EXIT_OK, out.err
        assert "round 00: 2 attempt(s)" in out.out
        loop_dir = faked_driver / "loop-test-loop"
        assert (loop_dir / "round-00" / "round.json").exists()
        assert (loop_dir / "noise-floor" / "noise-floor.json").exists()
        assert not (loop_dir / ".driver.lock").exists()
        floor_before = (loop_dir / "noise-floor" / "noise-floor.json").read_bytes()
        record_before = (loop_dir / "round-00" / "round.json").read_bytes()

        code = run_cli.main(_args(tmp_path, "--yes"))
        out = capsys.readouterr()
        assert code == run_cli.EXIT_OK, out.err
        assert "already finished" in out.out
        assert (loop_dir / "noise-floor" / "noise-floor.json").read_bytes() == floor_before
        assert (loop_dir / "round-00" / "round.json").read_bytes() == record_before
        # The second invocation re-derived the floor from reused seeds and
        # returned the finished round without driving: the runners it built
        # (the first two belong to the first invocation) never called the
        # solver.
        assert len(fake_runners) >= 3
        assert all(r._solver.calls == [] for r in fake_runners[2:])  # type: ignore[attr-defined]

    def test_a_finished_round_under_other_seeds_is_refused_in_preflight(
        self,
        faked_driver: Path,
        fake_runners: list[RoundRunner],
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        assert run_cli.main(_args(tmp_path, "--yes")) == run_cli.EXIT_OK
        capsys.readouterr()
        # Same slug, a different seed set: round 0 would seed with 4, not 1.
        _RecordingFloorRunner.calls = []
        monkeypatch.setattr(run_cli, "NoiseFloorRunner", _RecordingFloorRunner)
        code = run_cli.main(_args(tmp_path, "--yes", "--seeds", "4", "5", "6"))
        err = capsys.readouterr().err
        assert code == run_cli.EXIT_REFUSED
        assert "measured differently" in err
        assert "seed" in err
        assert "new loop slug" in err
        # Refused *before* the floor was re-driven under the new seeds, and
        # the backend was closed on the way out (once by the first run, once
        # by this refusal).
        assert _RecordingFloorRunner.calls == []
        assert _FakeBackend.closed == 2
        # And the dry run says the same thing.
        code = run_cli.main(_args(tmp_path, "--dry-run", "--seeds", "4", "5", "6"))
        assert code == run_cli.EXIT_REFUSED
        assert "measured differently" in capsys.readouterr().err

    def test_no_resume_against_a_finished_round_is_refused(
        self,
        faked_driver: Path,
        fake_runners: list[RoundRunner],
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        assert run_cli.main(_args(tmp_path, "--yes")) == run_cli.EXIT_OK
        capsys.readouterr()
        code = run_cli.main(_args(tmp_path, "--yes", "--no-resume"))
        err = capsys.readouterr().err
        assert code == run_cli.EXIT_REFUSED
        assert "--no-resume cannot re-measure into a finished index" in err


class TestOneDriverPerResultsTree:
    def test_a_live_lock_refuses_the_dry_run_and_the_real_run_naming_the_pid(
        self,
        faked_driver: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import json
        import os

        loop_dir = faked_driver / "loop-test-loop"
        loop_dir.mkdir(parents=True)
        lock = loop_dir / ".driver.lock"
        lock.write_text(
            json.dumps({"pid": os.getpid(), "started_at_ms": 1, "host": os.uname().nodename})
        )
        before = sorted(p.relative_to(faked_driver) for p in faked_driver.rglob("*"))
        _RecordingFloorRunner.calls = []
        monkeypatch.setattr(run_cli, "NoiseFloorRunner", _RecordingFloorRunner)

        code = run_cli.main(_args(tmp_path, "--dry-run"))
        err = capsys.readouterr().err
        assert code == run_cli.EXIT_REFUSED
        assert f"pid {os.getpid()}" in err
        assert "one driver owns a results tree" in err

        code = run_cli.main(_args(tmp_path, "--yes"))
        err = capsys.readouterr().err
        assert code == run_cli.EXIT_REFUSED
        assert f"pid {os.getpid()}" in err
        assert _RecordingFloorRunner.calls == []
        # Neither invocation touched the tree, and the lock is still the
        # other driver's.
        assert sorted(p.relative_to(faked_driver) for p in faked_driver.rglob("*")) == before
        assert json.loads(lock.read_text())["started_at_ms"] == 1
        assert _FakeBackend.closed == 2

    def test_a_stale_lock_is_reclaimed_by_a_yes_run(
        self,
        faked_driver: Path,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        import json
        import os
        import subprocess

        with subprocess.Popen(["true"]) as proc:
            proc.wait()
        dead = proc.pid  # exited: not alive
        loop_dir = faked_driver / "loop-test-loop"
        loop_dir.mkdir(parents=True)
        (loop_dir / ".driver.lock").write_text(
            json.dumps({"pid": dead, "started_at_ms": 1, "host": os.uname().nodename})
        )
        _RecordingFloorRunner.calls = []
        monkeypatch.setattr(run_cli, "NoiseFloorRunner", _RecordingFloorRunner)
        code = run_cli.main(_args(tmp_path, "--yes", "--noise-floor-only"))
        assert code == run_cli.EXIT_OK, capsys.readouterr().err
        assert len(_RecordingFloorRunner.calls) == 1
        assert not (loop_dir / ".driver.lock").exists()
