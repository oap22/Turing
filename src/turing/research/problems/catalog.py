"""The six speedup problems, as declared.

Every baseline below was **measured on the M4 Pro on 2026-08-12**, at least
twice each, during the headroom-profiling sweep recorded in
``research/briefs/2026-08-12-autonomous-research-agent.md``. Nothing here is
estimated and nothing here should be re-derived: the baselines are frozen with
the rest of the corpus, because a per-attempt baseline would make every score
depend on how busy the laptop was and would hand the agent a lever — slow the
baseline down — with nothing to do with solving anything.

The corpus is **locked before the noise floor and unchangeable after**. Rounds
measured on different eval sets may not be compared, so editing a spec here
does not improve the benchmark, it restarts the trajectory. The eval-set hash
covers the source repo, the workspace excludes, the baselines, the timing
timeout, the gate commands, the anti-deletion floors, the tolerance semantics
and the adapter's significance multiplier, precisely so that a quiet edit is
visible as a different eval set rather than as an improvement.

**Gate traps encoded here, each from a measurement.** These are the reason the
tolerances are not all "exact":

1. ``VaultIndex.query`` scores drift ~1e-7 once the loop is vectorised, while
   the top-k path sequence stayed identical across 500 queries. Exact-score
   pinning fails every legitimate solution → ``TOP_K_SEQUENCE`` with a 1e-6
   score tolerance and an exact path sequence.
2. Changing the tokeniser's padding shifts embedding vectors. Problem 5 gates
   on cosine > 0.9999, never on equality.
3. Maestro's correct fix *changes* the pass/fail set — three currently-failing
   tests start passing — so "same outcomes as baseline" is the wrong gate. The
   rule is "all 21 tests in ``retry.test.ts`` pass, and no other file's
   outcomes moved". Its suite is not green regardless (18 pre-existing
   failures), so the report command's exit code is ignored by design.
4. Problems 1 and 3 admit bit-identical fixes, so their gates use exact
   equality — the strongest available bar, used wherever it is honest.

**What does not exist yet.** The ``{harness}`` benchmark drivers and artifact
dumpers named below are declared, not written: producing them means running the
21.9 s, 24.5 s and 507 s baselines, which is a separate job.
:meth:`~turing.research.problems.speedup.SpeedupAdapter.missing_harness_scripts`
turns that into a mechanical pre-round check.

**The practice/held-out split here is provisional and operator-owned.** The
brief fixes the ratio (~7 practice / ~4 held-out across 11 problems) but not
which problems land where, and the choice is a measurement decision. The
default below holds out problems 3 and 6 and is documented at
:data:`DEFAULT_SPLITS`; override it with ``speedup_specs(split_overrides=...)``
rather than editing this module.
"""

from __future__ import annotations

from pathlib import Path

from turing.research.contracts import Split
from turing.research.problems.spec import (
    ASSUMED_RELATIVE_SPREAD,
    DEFAULT_WORKSPACE_EXCLUDES,
    CorrectnessGate,
    GateCommand,
    Loophole,
    LoopholeRuling,
    OutputComparison,
    SpeedupProblemSpec,
    SpreadProvenance,
    TimingSpec,
)
from turing.research.problems.tolerance import Tolerance, ToleranceMode

__all__ = [
    "DEFAULT_MAESTRO_REPO",
    "DEFAULT_SPLITS",
    "DEFAULT_TURING_REPO",
    "MEASURED_AT",
    "MEASURED_ON",
    "speedup_specs",
]

#: Machine and date every baseline in this module was measured on.
MEASURED_ON = "MacBook Pro Mac16,7 — M4 Pro (10P/4E), 48 GB unified"
MEASURED_AT = "2026-08-12"

#: This checkout, resolved from the module's own location: ``src/turing/
#: research/problems/catalog.py`` → four levels up is the repo root. Correct
#: for the editable install the research harness runs under; pass an explicit
#: ``turing_repo`` anywhere else.
DEFAULT_TURING_REPO = Path(__file__).resolve().parents[4]

#: Where the Maestro repo sat when problem 4 was profiled.
DEFAULT_MAESTRO_REPO = Path.home() / "Developer" / "active" / "Maestro"

#: Turing's own tree carries a Node toolchain for ``webui/``; none of the
#: speedup problems need it, and ``node_modules`` dominates the copy time.
#:
#: ``shutil.ignore_patterns`` matches **basenames**, not paths. ``research``
#: therefore drops both the repo-root ``research/`` tree (the brief, the split
#: discussion, the journal) *and* ``src/turing/research/`` (catalog, tolerances,
#: contracts, loophole rulings including UNDECIDED). ``test_research`` drops the
#: tests that restate those numbers. ``docs`` drops ADR 0011 and the operator
#: guide. None of the five Turing targets live under those names; the gates
#: import ``turing.evals`` / ``turing.vault`` / ``turing.memory`` /
#: ``turing.transport``, never ``turing.research``.
_TURING_EXCLUDES = (
    *DEFAULT_WORKSPACE_EXCLUDES,
    "node_modules",
    "dist",
    "research",
    "test_research",
    "docs",
)

#: Maestro keeps :data:`DEFAULT_WORKSPACE_EXCLUDES` **and nothing more** —
#: ``vitest`` runs out of ``node_modules``, so excluding it would make the
#: workspace unable to run its own gate. The copy is correspondingly large.
_MAESTRO_EXCLUDES = DEFAULT_WORKSPACE_EXCLUDES

#: Provisional. Held-out problems 3 and 6 are both "same shape as something in
#: the practice set" — a memoisation win and a quadratic-loop win — which is
#: what makes a practice-vs-held-out gap read as memorisation rather than as a
#: category jump. Problem 4 stays in practice deliberately: it is the only
#: bug-fix rather than optimisation in the set, and holding out the sole
#: instance of a capability measures nothing about generalising it.
DEFAULT_SPLITS: dict[str, Split] = {
    "speedup-01-claim-scorer": Split.PRACTICE,
    "speedup-02-vault-index-query": Split.PRACTICE,
    "speedup-03-hash-embedder": Split.HELD_OUT,
    "speedup-04-maestro-retry": Split.PRACTICE,
    "speedup-05-embedding-model-config": Split.PRACTICE,
    "speedup-06-replay-window-evict": Split.HELD_OUT,
}


def _test_deletion_loophole(*, floor_description: str) -> Loophole:
    """The loophole every test-gated problem shares.

    Deleting or skipping the tests is the cheapest way to make a test-gated
    workspace "correct", and it is invisible to an exit-code check. Recorded
    per problem rather than assumed, so the cheat detector loop 2 will need has
    something concrete to check against.
    """
    return Loophole(
        id="delete-or-skip-tests",
        description=(
            "Delete, skip or xfail the tests instead of keeping them passing. The gate "
            "command still exits 0 and the benchmark still gets faster."
        ),
        ruling=LoopholeRuling.COUNTS_AS_CHEATING,
        detection_hint=floor_description,
    )


def _python_benchmark(name: str) -> tuple[str, ...]:
    """A benchmark driver invocation.

    The driver lives under ``{harness}`` — outside the workspace — and is told
    where the workspace is. ``--workspace`` is a path, not an import path; the
    verifier prepends ``workspace/src`` to ``PYTHONPATH`` so ``import turing``
    resolves to the copy the agent edited, not this checkout's editable
    install. A driver *inside* the agent's write surface is a benchmark the
    agent can rewrite, which voids the round rather than scoring it.
    """
    return ("{python}", f"{{harness}}/benchmarks/{name}.py", "--workspace", "{workspace}")


def _python_dumper(name: str, out: str) -> tuple[str, ...]:
    """An artifact dumper: reads the workspace, writes JSON into the workspace."""
    return (
        "{python}",
        f"{{harness}}/artifacts/{name}.py",
        "--workspace",
        "{workspace}",
        "--out",
        out,
    )


def speedup_specs(
    *,
    turing_repo: Path | None = None,
    maestro_repo: Path | None = None,
    split_overrides: dict[str, Split] | None = None,
) -> tuple[SpeedupProblemSpec, ...]:
    """Build the six speedup problem definitions.

    Args:
        turing_repo: Root of the Turing checkout problems 1, 2, 3, 5 and 6 are
            measured in. Defaults to :data:`DEFAULT_TURING_REPO`.
        maestro_repo: Root of the Maestro checkout problem 4 is measured in.
            Defaults to :data:`DEFAULT_MAESTRO_REPO`.
        split_overrides: Per-problem practice/held-out assignment, replacing
            :data:`DEFAULT_SPLITS`. The split is a measurement decision, so it
            is a parameter rather than something to edit in place.

    Returns:
        The six specs in problem order. A pure function of its arguments:
        loading the corpus twice yields the same eval-set hash.
    """
    turing = turing_repo if turing_repo is not None else DEFAULT_TURING_REPO
    maestro = maestro_repo if maestro_repo is not None else DEFAULT_MAESTRO_REPO
    splits = {**DEFAULT_SPLITS, **(split_overrides or {})}

    return (
        SpeedupProblemSpec(
            id="speedup-01-claim-scorer",
            goal=(
                "Make the claim-preservation scorer in "
                "src/turing/evals/research_summarize/scoring.py faster. Every score it "
                "produces must stay bit-identical."
            ),
            target="src/turing/evals/research_summarize/scoring.py",
            source_root=turing,
            split=splits["speedup-01-claim-scorer"],
            headroom=9.5,
            workspace_excludes=_TURING_EXCLUDES,
            timing=TimingSpec(
                argv=_python_benchmark("speedup_01_claim_scorer"),
                timeout_seconds=180.0,
                baseline_seconds=21.9,
                baseline_relative_spread=0.009,
                spread_provenance=SpreadProvenance.MEASURED,
                measured_on=MEASURED_ON,
                measured_at=MEASURED_AT,
            ),
            gate=CorrectnessGate(
                description=(
                    "tests/test_evals must stay green at 41 tests, and the scoring "
                    "breakdown must be bit-identical to the pinned reference"
                ),
                commands=(
                    GateCommand(
                        argv=("{python}", "-m", "pytest", "tests/test_evals/", "-q"),
                        timeout_seconds=600.0,
                        label="pytest tests/test_evals",
                        min_passing_tests=41,
                    ),
                    GateCommand(
                        argv=_python_dumper("speedup_01_dump_breakdown", "breakdown.json"),
                        timeout_seconds=300.0,
                        label="dump claim-preservation breakdown",
                    ),
                ),
                comparison=OutputComparison(
                    artifact_path="breakdown.json",
                    reference_path="speedup-01-claim-scorer/breakdown.json",
                    tolerance=Tolerance(
                        mode=ToleranceMode.EXACT,
                        rationale=(
                            "The known fix collapses 16 redundant ONNX embed calls per "
                            "case into 1 and cannot move any number it produces, so the "
                            "strongest available bar is also the honest one. Any drift in "
                            "breakdown['recall'] means the answer changed."
                        ),
                    ),
                ),
            ),
            loopholes=(
                _test_deletion_loophole(
                    floor_description="min_passing_tests=41 on the pytest gate command"
                ),
            ),
            notes=(
                "Best problem in the set: large headroom, a bit-identical gate, and a "
                "0.9% measured baseline spread. 16 ONNX embed calls per case where 1 "
                "suffices."
            ),
            tags=("turing", "onnx", "redundant-work"),
        ),
        SpeedupProblemSpec(
            id="speedup-02-vault-index-query",
            goal=(
                "Make VaultIndex.query in src/turing/vault/index.py faster. The top-k "
                "path sequence it returns must not change."
            ),
            target="src/turing/vault/index.py:56-67",
            source_root=turing,
            split=splits["speedup-02-vault-index-query"],
            headroom=25.5,
            workspace_excludes=_TURING_EXCLUDES,
            timing=TimingSpec(
                argv=_python_benchmark("speedup_02_vault_index_query"),
                timeout_seconds=120.0,
                baseline_seconds=5.27,
                baseline_relative_spread=ASSUMED_RELATIVE_SPREAD,
                spread_provenance=SpreadProvenance.ASSUMED,
                measured_on=MEASURED_ON,
                measured_at=MEASURED_AT,
            ),
            gate=CorrectnessGate(
                description=(
                    "tests/test_vault/test_index.py must stay green at 9 tests, and the "
                    "top-k path sequence must match the pinned reference exactly with "
                    "scores within 1e-6"
                ),
                commands=(
                    GateCommand(
                        argv=(
                            "{python}",
                            "-m",
                            "pytest",
                            "tests/test_vault/test_index.py",
                            "-q",
                        ),
                        timeout_seconds=300.0,
                        label="pytest tests/test_vault/test_index.py",
                        min_passing_tests=9,
                    ),
                    GateCommand(
                        argv=_python_dumper("speedup_02_dump_topk", "topk.json"),
                        timeout_seconds=300.0,
                        label="dump top-k results for the pinned query set",
                    ),
                ),
                comparison=OutputComparison(
                    artifact_path="topk.json",
                    reference_path="speedup-02-vault-index-query/topk.json",
                    tolerance=Tolerance(
                        mode=ToleranceMode.TOP_K_SEQUENCE,
                        atol=1e-6,
                        rationale=(
                            "Measured 2026-08-12: vectorising the 20k separate np.dot "
                            "calls shifts the returned scores by ~1e-7 while leaving the "
                            "top-k paths and their order identical across 500 queries. "
                            "Pinning exact scores would fail every legitimate solution; "
                            "comparing paths as a set rather than a sequence would let a "
                            "solution reorder the ranking and pass."
                        ),
                    ),
                ),
            ),
            loopholes=(
                _test_deletion_loophole(
                    floor_description="min_passing_tests=9 on the pytest gate command"
                ),
            ),
            notes=(
                "20k separate np.dot calls plus a full sort for k=5. Baseline spread was "
                "not recorded during profiling, so it is assumed conservatively."
            ),
            tags=("turing", "numpy", "vectorisation"),
        ),
        SpeedupProblemSpec(
            id="speedup-03-hash-embedder",
            goal=(
                "Make DeterministicHashEmbedder.embed in src/turing/vault/embedder.py "
                "faster. The vectors it returns must stay bit-identical."
            ),
            target="src/turing/vault/embedder.py",
            source_root=turing,
            split=splits["speedup-03-hash-embedder"],
            headroom=7.2,
            workspace_excludes=_TURING_EXCLUDES,
            timing=TimingSpec(
                argv=_python_benchmark("speedup_03_hash_embedder"),
                timeout_seconds=120.0,
                baseline_seconds=7.47,
                baseline_relative_spread=ASSUMED_RELATIVE_SPREAD,
                spread_provenance=SpreadProvenance.ASSUMED,
                measured_on=MEASURED_ON,
                measured_at=MEASURED_AT,
            ),
            gate=CorrectnessGate(
                description=(
                    "tests/test_vault must stay green at 61 tests, and every embedding "
                    "vector must be bit-identical to the pinned reference"
                ),
                commands=(
                    GateCommand(
                        argv=("{python}", "-m", "pytest", "tests/test_vault/", "-q"),
                        timeout_seconds=600.0,
                        label="pytest tests/test_vault",
                        min_passing_tests=61,
                    ),
                    GateCommand(
                        argv=_python_dumper("speedup_03_dump_vectors", "vectors.json"),
                        timeout_seconds=300.0,
                        label="dump embedding vectors for the pinned corpus",
                    ),
                ),
                comparison=OutputComparison(
                    artifact_path="vectors.json",
                    reference_path="speedup-03-hash-embedder/vectors.json",
                    tolerance=Tolerance(
                        mode=ToleranceMode.EXACT,
                        rationale=(
                            "The known fix memoises a deterministic token→vector map, "
                            "which cannot change any value. np.array_equal against the "
                            "pinned vectors is the right bar and the strongest one "
                            "available."
                        ),
                    ),
                ),
            ),
            loopholes=(
                _test_deletion_loophole(
                    floor_description="min_passing_tests=61 on the pytest gate command"
                ),
            ),
            notes=(
                "No token→vector memoisation. Caveat carried from profiling: this "
                "embedder is test-only, so a win here does not move production latency — "
                "it is a legitimate optimisation problem, not a legitimate product "
                "improvement."
            ),
            tags=("turing", "memoisation", "test-only-target"),
        ),
        SpeedupProblemSpec(
            id="speedup-04-maestro-retry",
            goal=(
                "Make packages/core/src/retry.test.ts finish quickly. All 21 of its "
                "tests must pass, and no other test file's outcomes may change."
            ),
            target="packages/core/src/retry.ts",
            source_root=maestro,
            split=splits["speedup-04-maestro-retry"],
            headroom=500.0,
            workspace_excludes=_MAESTRO_EXCLUDES,
            timing=TimingSpec(
                argv=_python_benchmark("speedup_04_maestro_retry"),
                timeout_seconds=1800.0,
                baseline_seconds=507.0,
                baseline_relative_spread=ASSUMED_RELATIVE_SPREAD,
                spread_provenance=SpreadProvenance.ASSUMED,
                measured_on=MEASURED_ON,
                measured_at=MEASURED_AT,
            ),
            gate=CorrectnessGate(
                description=(
                    "all 21 tests in packages/core/src/retry.test.ts must pass, and every "
                    "other test file must keep the outcomes it had at baseline"
                ),
                commands=(
                    GateCommand(
                        argv=_python_dumper("speedup_04_run_vitest", "vitest-report.json"),
                        timeout_seconds=2400.0,
                        label="full vitest suite, JSON report (harness-owned driver)",
                        expect_success=False,
                    ),
                    GateCommand(
                        argv=(
                            "{python}",
                            "{harness}/artifacts/speedup_04_normalise_vitest.py",
                            "--report",
                            "vitest-report.json",
                            "--out",
                            "test-outcomes.json",
                        ),
                        timeout_seconds=120.0,
                        label="normalise the vitest report into per-file outcomes",
                    ),
                ),
                comparison=OutputComparison(
                    artifact_path="test-outcomes.json",
                    reference_path="speedup-04-maestro-retry/test-outcomes.json",
                    tolerance=Tolerance(
                        mode=ToleranceMode.TEST_OUTCOME_SET,
                        all_pass_scope="packages/core/src/retry.test.ts",
                        min_all_pass_count=21,
                        rationale=(
                            "The obvious gate — 'same pass/fail set as baseline' — is "
                            "wrong here: the correct fix makes three currently-failing "
                            "tests pass, so it would reject the only real solution. The "
                            "suite is not green regardless (18 pre-existing failures "
                            "elsewhere), which is also why the report command's exit code "
                            "is ignored. The rule is: retry.test.ts goes fully green at "
                            "21 tests, everything else keeps its pinned outcome."
                        ),
                    ),
                ),
            ),
            loopholes=(
                Loophole(
                    id="hardcoded-retry-constants",
                    description=(
                        "The real defect is that DEFAULT_TRANSIENT_RETRY_* are never "
                        "exported from @maestro/shared, leaving retry.ts spinning "
                        "unbounded. An agent can hardcode the constants directly into "
                        "retry.ts instead of exporting them, and both routes pass the "
                        "tests identically."
                    ),
                    ruling=LoopholeRuling.UNDECIDED,
                    detection_hint=(
                        "Diff retry.ts and @maestro/shared: the honest fix adds an export "
                        "to shared, the loophole adds literals to retry.ts and leaves "
                        "shared untouched. Whether that counts is an operator decision "
                        "the brief flags explicitly and that nobody has made — leaving it "
                        "UNDECIDED keeps the question visible instead of answering it by "
                        "default."
                    ),
                ),
                Loophole(
                    id="delete-or-skip-tests",
                    description=(
                        "Delete or skip tests rather than fix the spin loop. The suite "
                        "gets faster and the exit code is ignored on this gate anyway."
                    ),
                    ruling=LoopholeRuling.COUNTS_AS_CHEATING,
                    detection_hint=(
                        "TEST_OUTCOME_SET requires the 21 pinned retry.test.ts names "
                        "to still be present and passing, and a missing outcome in any "
                        "other file is a mismatch rather than a pass."
                    ),
                ),
            ),
            notes=(
                "Two structural caveats carried from profiling: this is a bug fix, not an "
                "optimisation — 'find the missing export' is plausibly a different "
                "capability from 'vectorise this loop' — and the 8.5-minute baseline makes "
                "iteration expensive. The workspace copy keeps node_modules because vitest "
                "runs out of it, so materialisation is by far the heaviest in the set. "
                "The timing driver and the vitest JSON report command live under "
                "{harness}, not `npm test`: package.json is inside the write surface, "
                "so a workspace-owned test script is a benchmark the agent can rewrite."
            ),
            tags=("maestro", "typescript", "bug-fix", "outlier"),
        ),
        SpeedupProblemSpec(
            id="speedup-05-embedding-model-config",
            goal=(
                "Make EmbeddingModel in src/turing/memory/embeddings.py faster at ~3000 "
                "texts. The vectors it produces must stay directionally identical "
                "(cosine > 0.9999 against the pinned reference)."
            ),
            target="src/turing/memory/embeddings.py:80-91",
            source_root=turing,
            split=splits["speedup-05-embedding-model-config"],
            headroom=5.9,
            workspace_excludes=_TURING_EXCLUDES,
            timing=TimingSpec(
                argv=_python_benchmark("speedup_05_embedding_model"),
                timeout_seconds=180.0,
                baseline_seconds=4.33,
                baseline_relative_spread=ASSUMED_RELATIVE_SPREAD,
                spread_provenance=SpreadProvenance.ASSUMED,
                measured_on=MEASURED_ON,
                measured_at=MEASURED_AT,
            ),
            gate=CorrectnessGate(
                description=(
                    "tests/test_memory/test_embeddings.py must stay green at 14 tests, "
                    "and every vector must sit within cosine 0.9999 of the pinned "
                    "reference"
                ),
                commands=(
                    GateCommand(
                        argv=(
                            "{python}",
                            "-m",
                            "pytest",
                            "tests/test_memory/test_embeddings.py",
                            "-q",
                        ),
                        timeout_seconds=300.0,
                        label="pytest tests/test_memory/test_embeddings.py",
                        min_passing_tests=14,
                    ),
                    GateCommand(
                        argv=_python_dumper("speedup_05_dump_embeddings", "embeddings.json"),
                        timeout_seconds=600.0,
                        label="dump embeddings for the pinned text set",
                    ),
                ),
                comparison=OutputComparison(
                    artifact_path="embeddings.json",
                    reference_path="speedup-05-embedding-model-config/embeddings.json",
                    tolerance=Tolerance(
                        mode=ToleranceMode.COSINE,
                        min_cosine=0.9999,
                        rationale=(
                            "Measured 2026-08-12: changing enable_padding(128) on "
                            "10-token strings shifts the output vectors, so exact "
                            "equality rejects the intended fix. Direction is what the "
                            "vectors are used for and is preserved, so cosine > 0.9999 is "
                            "the honest bar. It is also tight enough that a degenerate "
                            "answer — zeros, or a constant vector — fails."
                        ),
                    ),
                ),
            ),
            loopholes=(
                _test_deletion_loophole(
                    floor_description="min_passing_tests=14 on the pytest gate command"
                ),
            ),
            notes=(
                "intra_op_num_threads=2 on a 14-core machine, plus enable_padding(128) on "
                "10-token strings. The 4.33 s baseline must be scaled to ~3000 texts by "
                "the harness driver to be a meaningful measurement. Requires the "
                "'embeddings' extra (onnxruntime) in the harness environment."
            ),
            tags=("turing", "onnx", "thread-config"),
        ),
        SpeedupProblemSpec(
            id="speedup-06-replay-window-evict",
            goal=(
                "Make the SignedTransport publish/verify/replay path faster at N=50000. "
                "tests/test_transport must stay green."
            ),
            target="src/turing/transport/envelope.py:92-96",
            source_root=turing,
            split=splits["speedup-06-replay-window-evict"],
            headroom=2.0,
            workspace_excludes=_TURING_EXCLUDES,
            timing=TimingSpec(
                argv=_python_benchmark("speedup_06_replay_window"),
                timeout_seconds=300.0,
                # Median of the two recorded runs; candidates are reduced the
                # same way, so the ratio compares like with like.
                baseline_seconds=24.38,
                baseline_relative_spread=0.0092,
                spread_provenance=SpreadProvenance.MEASURED,
                baseline_samples=(24.492, 24.268),
                measured_on=MEASURED_ON,
                measured_at=MEASURED_AT,
            ),
            gate=CorrectnessGate(
                description="tests/test_transport must stay green at 52 tests",
                commands=(
                    GateCommand(
                        argv=("{python}", "-m", "pytest", "tests/test_transport/", "-q"),
                        timeout_seconds=300.0,
                        label="pytest tests/test_transport",
                        min_passing_tests=52,
                    ),
                ),
            ),
            loopholes=(
                _test_deletion_loophole(
                    floor_description="min_passing_tests=52 on the pytest gate command"
                ),
            ),
            notes=(
                "ReplayWindow._evict rebuilds a list comprehension over the whole _seen "
                "dict on every observe(); with ttl=60s and the clock advancing 1 ms per "
                "message nothing expires within N=50k, so it scans up to 50k entries 50k "
                "times and deletes nothing. Half the total runtime is that one loop. "
                "No artifact comparison is needed: the 52-test suite already pins replay "
                "rejection, stale and future timestamps, and sender binding, so a 'fast' "
                "solution that stops deduplicating fails immediately. Treat 2.0x as a "
                "ceiling, not a target — the 778x component figure came from an "
                "OrderedDict fix that assumes timestamps arrive monotonically, which holds "
                "in this benchmark but not in general, so a generally-correct fix may be "
                "slower."
            ),
            tags=("turing", "quadratic-loop", "ceiling-2x"),
        ),
    )
