"""Tests for the adapter seam, the registry, and the corpus fingerprint."""

from __future__ import annotations

import pytest

from turing.research.contracts import ContractViolationError, ProblemType, Split
from turing.research.problems.adapter import (
    AdapterRegistry,
    ProblemAdapter,
    WorkspaceMaterialisationError,
    bind_eval_set_hash,
    fingerprint_corpus,
)
from turing.research.problems.catalog import speedup_specs
from turing.research.problems.kaggle import KaggleAdapter
from turing.research.problems.speedup import SpeedupAdapter

from .conftest import make_spec


def build_source_repo(root):
    """A miniature repo: source, tests, a .git directory and some build junk."""
    (root / "src").mkdir(parents=True)
    (root / "src" / "module.py").write_text("value = 1\n")
    (root / "tests").mkdir()
    (root / "tests" / "test_module.py").write_text("def test_ok():\n    assert True\n")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "module.cpython-311.pyc").write_bytes(b"\x00")
    return root


class TestProtocolConformance:
    def test_the_speedup_adapter_satisfies_the_protocol(self, harness_root, reference_root):
        adapter = SpeedupAdapter(
            harness_root=harness_root, reference_root=reference_root, specs=(make_spec(),)
        )
        assert isinstance(adapter, ProblemAdapter)

    def test_the_kaggle_stub_satisfies_the_protocol(self, tmp_path):
        """The stub has to have the finished shape, or the seam is not a seam."""
        assert isinstance(KaggleAdapter(data_root=tmp_path), ProblemAdapter)


class TestSpeedupAdapterLoad:
    def test_a_loaded_problem_carries_the_spec_excludes(self, harness_root, reference_root):
        """The loop and solver copy from Problem, not from the spec; the excludes have to ride."""
        adapter = SpeedupAdapter(
            harness_root=harness_root,
            reference_root=reference_root,
            specs=(make_spec(workspace_excludes=("research", "docs")),),
        )
        assert adapter.load()[0].workspace_excludes == ("research", "docs")

    def test_builds_one_problem_per_spec_with_a_bound_verifier(self, harness_root, reference_root):
        specs = (make_spec(problem_id="a"), make_spec(problem_id="b", split=Split.HELD_OUT))
        adapter = SpeedupAdapter(
            harness_root=harness_root, reference_root=reference_root, specs=specs
        )
        problems = adapter.load()
        assert [p.id for p in problems] == ["a", "b"]
        assert all(p.problem_type is ProblemType.SPEEDUP for p in problems)
        assert problems[1].is_held_out
        assert problems[0].verifier.problem_id == "a"

    def test_loading_twice_is_deterministic(self, harness_root, reference_root):
        """A corpus that varies by when it was loaded makes rounds incomparable."""
        adapter = SpeedupAdapter(
            harness_root=harness_root,
            reference_root=reference_root,
            specs=(make_spec(problem_id="a"),),
        )
        first, second = adapter.load(), adapter.load()
        assert fingerprint_corpus(first) == fingerprint_corpus(second)

    def test_rejects_duplicate_problem_ids(self, harness_root, reference_root):
        with pytest.raises(WorkspaceMaterialisationError, match="duplicate problem id"):
            SpeedupAdapter(
                harness_root=harness_root,
                reference_root=reference_root,
                specs=(make_spec(problem_id="a"), make_spec(problem_id="a")),
            )

    def test_spec_for_rejects_an_unknown_problem(self, harness_root, reference_root):
        adapter = SpeedupAdapter(
            harness_root=harness_root, reference_root=reference_root, specs=(make_spec(),)
        )
        with pytest.raises(WorkspaceMaterialisationError, match="not a speedup problem"):
            adapter.spec_for("nope")


class TestMissingHarnessScripts:
    def test_reports_declared_scripts_that_do_not_exist(self, harness_root, reference_root):
        adapter = SpeedupAdapter(
            harness_root=harness_root, reference_root=reference_root, specs=(make_spec(),)
        )
        missing = adapter.missing_harness_scripts()
        assert harness_root / "benchmarks/fake.py" in missing

    def test_reports_nothing_once_the_scripts_are_present(self, harness_root, reference_root):
        (harness_root / "benchmarks").mkdir()
        (harness_root / "benchmarks" / "fake.py").write_text("")
        adapter = SpeedupAdapter(
            harness_root=harness_root, reference_root=reference_root, specs=(make_spec(),)
        )
        assert adapter.missing_harness_scripts() == ()


class TestMaterialiseWorkspace:
    async def test_copies_the_source_repo_into_a_fresh_directory(
        self, tmp_path, harness_root, reference_root
    ):
        source = build_source_repo(tmp_path / "source")
        adapter = SpeedupAdapter(
            harness_root=harness_root,
            reference_root=reference_root,
            specs=(make_spec(source_root=source),),
        )
        problem = adapter.load()[0]
        destination = tmp_path / "attempt-1"
        result = await adapter.materialise_workspace(problem, destination)

        assert result == destination
        assert (destination / "src" / "module.py").read_text() == "value = 1\n"
        assert (destination / "tests" / "test_module.py").exists()

    async def test_excludes_git_so_an_attempt_cannot_rewrite_history(
        self, tmp_path, harness_root, reference_root
    ):
        source = build_source_repo(tmp_path / "source")
        adapter = SpeedupAdapter(
            harness_root=harness_root,
            reference_root=reference_root,
            specs=(make_spec(source_root=source),),
        )
        destination = tmp_path / "attempt-1"
        await adapter.materialise_workspace(adapter.load()[0], destination)

        assert not (destination / ".git").exists()
        assert not (destination / "__pycache__").exists()

    async def test_leaves_the_source_repo_untouched(self, tmp_path, harness_root, reference_root):
        source = build_source_repo(tmp_path / "source")
        adapter = SpeedupAdapter(
            harness_root=harness_root,
            reference_root=reference_root,
            specs=(make_spec(source_root=source),),
        )
        destination = tmp_path / "attempt-1"
        await adapter.materialise_workspace(adapter.load()[0], destination)
        (destination / "src" / "module.py").write_text("value = 2\n")

        assert (source / "src" / "module.py").read_text() == "value = 1\n"

    async def test_refuses_a_destination_that_already_has_contents(
        self, tmp_path, harness_root, reference_root
    ):
        """A workspace inherited from a previous attempt turns 'solved it' into a lie."""
        source = build_source_repo(tmp_path / "source")
        adapter = SpeedupAdapter(
            harness_root=harness_root,
            reference_root=reference_root,
            specs=(make_spec(source_root=source),),
        )
        destination = tmp_path / "attempt-1"
        destination.mkdir()
        (destination / "leftover.py").write_text("from the last attempt\n")

        with pytest.raises(WorkspaceMaterialisationError, match="already has contents"):
            await adapter.materialise_workspace(adapter.load()[0], destination)

    async def test_an_empty_existing_destination_is_fine(
        self, tmp_path, harness_root, reference_root
    ):
        source = build_source_repo(tmp_path / "source")
        adapter = SpeedupAdapter(
            harness_root=harness_root,
            reference_root=reference_root,
            specs=(make_spec(source_root=source),),
        )
        destination = tmp_path / "attempt-1"
        destination.mkdir()
        await adapter.materialise_workspace(adapter.load()[0], destination)
        assert (destination / "src" / "module.py").exists()

    async def test_a_missing_source_repo_raises_rather_than_scoring_zero(
        self, tmp_path, harness_root, reference_root
    ):
        adapter = SpeedupAdapter(
            harness_root=harness_root,
            reference_root=reference_root,
            specs=(make_spec(source_root=tmp_path / "absent"),),
        )
        with pytest.raises(WorkspaceMaterialisationError, match="does not exist"):
            await adapter.materialise_workspace(adapter.load()[0], tmp_path / "attempt-1")

    async def test_does_not_copy_the_eval_set_into_the_workspace(
        self, tmp_path, harness_root, reference_root
    ):
        """The solver's cwd must not contain the catalog, the brief, or the floors."""
        source = tmp_path / "source"
        (source / "src" / "turing" / "research" / "problems").mkdir(parents=True)
        (source / "src" / "turing" / "research" / "problems" / "catalog.py").write_text(
            "DEFAULT_SPLITS = {'held-out': 'secret'}\n"
        )
        (source / "src" / "turing" / "vault").mkdir(parents=True)
        (source / "src" / "turing" / "vault" / "index.py").write_text("query = 1\n")
        (source / "research" / "briefs").mkdir(parents=True)
        (source / "research" / "briefs" / "brief.md").write_text("held out: 3 and 6\n")
        (source / "tests" / "test_research").mkdir(parents=True)
        (source / "tests" / "test_research" / "test_catalog.py").write_text("floor = 52\n")
        (source / "tests" / "test_evals").mkdir(parents=True)
        (source / "tests" / "test_evals" / "test_ok.py").write_text(
            "def test_ok():\n    assert True\n"
        )
        (source / "docs" / "adr").mkdir(parents=True)
        (source / "docs" / "adr" / "0011.md").write_text("the split\n")

        adapter = SpeedupAdapter(
            harness_root=harness_root,
            reference_root=reference_root,
            specs=(
                make_spec(
                    source_root=source,
                    workspace_excludes=speedup_specs()[0].workspace_excludes,
                ),
            ),
        )
        destination = tmp_path / "attempt-1"
        await adapter.materialise_workspace(adapter.load()[0], destination)

        assert (destination / "src" / "turing" / "vault" / "index.py").is_file()
        assert (destination / "tests" / "test_evals" / "test_ok.py").is_file()
        assert not (destination / "src" / "turing" / "research").exists()
        assert not (destination / "research").exists()
        assert not (destination / "tests" / "test_research").exists()
        assert not (destination / "docs").exists()

    def test_rejects_a_reference_root_inside_the_source_repo(self, tmp_path, harness_root):
        source = tmp_path / "source"
        source.mkdir()
        with pytest.raises(WorkspaceMaterialisationError, match="reference_root"):
            SpeedupAdapter(
                harness_root=harness_root,
                reference_root=source / "research" / "reference",
                specs=(make_spec(source_root=source),),
            )

    def test_rejects_a_harness_root_inside_the_source_repo(self, tmp_path, reference_root):
        source = tmp_path / "source"
        source.mkdir()
        with pytest.raises(WorkspaceMaterialisationError, match="harness_root"):
            SpeedupAdapter(
                harness_root=source / "research" / "harness",
                reference_root=reference_root,
                specs=(make_spec(source_root=source),),
            )


class TestAdapterRegistry:
    def test_routes_a_problem_type_to_its_adapter(self, tmp_path, harness_root, reference_root):
        speedup = SpeedupAdapter(
            harness_root=harness_root, reference_root=reference_root, specs=(make_spec(),)
        )
        kaggle = KaggleAdapter(data_root=tmp_path)
        registry = AdapterRegistry()
        registry.register(speedup)
        registry.register(kaggle)

        assert registry.get(ProblemType.SPEEDUP) is speedup
        assert registry.get(ProblemType.KAGGLE) is kaggle
        assert set(registry.types()) == {ProblemType.SPEEDUP, ProblemType.KAGGLE}

    def test_refuses_two_adapters_for_one_family(self, harness_root, reference_root):
        registry = AdapterRegistry()
        registry.register(
            SpeedupAdapter(
                harness_root=harness_root, reference_root=reference_root, specs=(make_spec(),)
            )
        )
        with pytest.raises(ContractViolationError, match="already registered"):
            registry.register(
                SpeedupAdapter(
                    harness_root=harness_root,
                    reference_root=reference_root,
                    specs=(make_spec(),),
                )
            )

    def test_an_unregistered_family_is_a_loud_error(self):
        with pytest.raises(ContractViolationError, match="no adapter registered"):
            AdapterRegistry().get(ProblemType.KAGGLE)

    def test_load_all_is_stable_regardless_of_registration_order(
        self, harness_root, reference_root
    ):
        registry = AdapterRegistry()
        registry.register(
            SpeedupAdapter(
                harness_root=harness_root,
                reference_root=reference_root,
                specs=(make_spec(problem_id="a"), make_spec(problem_id="b")),
            )
        )
        assert [p.id for p in registry.load_all()] == ["a", "b"]


class TestFingerprintCorpus:
    def _problems(self, harness_root, reference_root, **kwargs):
        return SpeedupAdapter(
            harness_root=harness_root,
            reference_root=reference_root,
            specs=(make_spec(**kwargs),),
        ).load()

    def test_is_stable_across_processes(self, harness_root, reference_root):
        """SHA-256, not hash(): a salted hash would give a new eval-set identity per run."""
        first = fingerprint_corpus(self._problems(harness_root, reference_root))
        second = fingerprint_corpus(self._problems(harness_root, reference_root))
        assert first == second
        assert len(first) == 64

    def test_moves_when_a_problem_changes_split(self, harness_root, reference_root):
        practice = fingerprint_corpus(
            self._problems(harness_root, reference_root, split=Split.PRACTICE)
        )
        held_out = fingerprint_corpus(
            self._problems(harness_root, reference_root, split=Split.HELD_OUT)
        )
        assert practice != held_out

    def test_moves_when_extra_material_changes(self, harness_root, reference_root):
        problems = self._problems(harness_root, reference_root)
        assert fingerprint_corpus(problems, extra=("baseline=10",)) != fingerprint_corpus(
            problems, extra=("baseline=11",)
        )

    def test_a_repinned_baseline_changes_the_eval_set_identity(self, harness_root, reference_root):
        def hashed(baseline: float) -> str:
            adapter = SpeedupAdapter(
                harness_root=harness_root,
                reference_root=reference_root,
                specs=(make_spec(baseline_seconds=baseline),),
            )
            return fingerprint_corpus(adapter.load(), extra=adapter.eval_set_material())

        assert hashed(10.0) != hashed(11.0)

    def test_a_widened_significance_band_changes_the_eval_set_identity(
        self, harness_root, reference_root
    ):
        """1.0 vs 5.0 is a different definition of 'counts as a speedup', not a re-score."""

        def hashed(multiplier: float) -> str:
            adapter = SpeedupAdapter(
                harness_root=harness_root,
                reference_root=reference_root,
                specs=(make_spec(),),
                significance_multiplier=multiplier,
            )
            return fingerprint_corpus(adapter.load(), extra=adapter.eval_set_material())

        assert hashed(1.0) != hashed(5.0)

    def test_refuses_an_empty_corpus(self):
        with pytest.raises(ContractViolationError, match="empty corpus"):
            fingerprint_corpus([])

    def test_bind_derives_when_the_caller_promises_nothing(self, harness_root, reference_root):
        problems = self._problems(harness_root, reference_root)
        assert bind_eval_set_hash(problems) == fingerprint_corpus(problems)

    def test_bind_refuses_a_promised_hash_the_corpus_does_not_produce(
        self, harness_root, reference_root
    ):
        problems = self._problems(harness_root, reference_root)
        with pytest.raises(ContractViolationError, match="does not match the corpus fingerprint"):
            bind_eval_set_hash(problems, "corpus-v1")

    def test_dropping_a_problem_moves_the_bound_hash(self, harness_root, reference_root):
        full = SpeedupAdapter(
            harness_root=harness_root,
            reference_root=reference_root,
            specs=(make_spec(problem_id="a"), make_spec(problem_id="b")),
        ).load()
        assert bind_eval_set_hash(full) != bind_eval_set_hash(full[:1])
