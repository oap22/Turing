"""Tests for the Kaggle stub.

There is nothing to test about behaviour — both methods raise. What is worth
testing is the *rule* the stub carries, because the rule is the part that
becomes worthless the moment it is written down after a result exists.
"""

from __future__ import annotations

import pytest

from turing.research.contracts import ProblemType
from turing.research.problems.kaggle import (
    KAGGLE_CORPUS_SIZE,
    KAGGLE_SELECTION_CRITERION,
    KaggleAdapter,
)


class TestKaggleAdapterIsAnExplicitStub:
    def test_load_raises_and_says_why(self, tmp_path):
        adapter = KaggleAdapter(data_root=tmp_path)
        with pytest.raises(NotImplementedError, match="not been selected yet"):
            adapter.load()

    async def test_materialise_workspace_raises(self, tmp_path):
        adapter = KaggleAdapter(data_root=tmp_path)
        with pytest.raises(NotImplementedError):
            await adapter.materialise_workspace(None, tmp_path)  # type: ignore[arg-type]

    def test_it_still_claims_its_problem_type(self, tmp_path):
        assert KaggleAdapter(data_root=tmp_path).problem_type is ProblemType.KAGGLE

    def test_the_error_carries_the_selection_criterion(self, tmp_path):
        with pytest.raises(NotImplementedError) as excinfo:
            KaggleAdapter(data_root=tmp_path).load()
        assert KAGGLE_SELECTION_CRITERION in str(excinfo.value)


class TestSelectionCriterion:
    def test_it_is_mechanical_and_about_feasibility(self):
        assert "completes end-to-end" in KAGGLE_SELECTION_CRITERION
        assert "under N hours" in KAGGLE_SELECTION_CRITERION
        assert "baseline solver" in KAGGLE_SELECTION_CRITERION

    def test_it_explicitly_forbids_selecting_on_expected_score(self):
        """Tasks entering because the agent does well on them void every number."""
        assert "never expected score" in KAGGLE_SELECTION_CRITERION
        assert "looks promising" in KAGGLE_SELECTION_CRITERION

    def test_the_family_is_five_problems_not_one(self):
        """A type with one problem in it is a demonstration, not a measurement."""
        assert KAGGLE_CORPUS_SIZE == 5

    def test_the_reason_the_corpus_cannot_grow_incrementally_is_recorded(self):
        from turing.research.problems import kaggle

        assert kaggle.__doc__ is not None
        assert "locked before the noise floor" in kaggle.__doc__
