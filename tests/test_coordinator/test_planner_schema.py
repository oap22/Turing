"""Tests for the DAG / planner contract defined in ADR 0001."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from turing.coordinator.planner.schema import (
    DAG,
    NeedsSubtaskResponse,
    PlannerRoute,
    Specialty,
    Subtask,
)

# --- Subtask --------------------------------------------------------------


def _minimal_subtask(**overrides):
    base = dict(
        id="st_a",
        specialty_required="research-discover",
        prompt="Find papers on X.",
        depends_on=[],
        required_tools=[],
        inputs={},
        output_key="workspace://tsk_1/st_a/result",
    )
    base.update(overrides)
    return base


class TestSubtask:
    def test_minimal_valid(self):
        st = Subtask(**_minimal_subtask())
        assert st.id == "st_a"
        assert st.max_retries == 1  # default per ADR
        assert st.timeout_s == 120  # default per ADR

    def test_specialty_must_be_known(self):
        with pytest.raises(ValidationError):
            Subtask(**_minimal_subtask(specialty_required="not-a-real-specialty"))

    def test_specialty_enum_covers_adr_roster(self):
        expected = {
            "research-discover",
            "research-deep",
            "research-summarize",
            "synthesis",
            "judge",
            "planner",
        }
        assert {s.value for s in Specialty} == expected

    def test_output_key_must_be_workspace_uri(self):
        with pytest.raises(ValidationError):
            Subtask(**_minimal_subtask(output_key="s3://nope/x"))

    def test_max_retries_bounds(self):
        with pytest.raises(ValidationError):
            Subtask(**_minimal_subtask(max_retries=4))
        with pytest.raises(ValidationError):
            Subtask(**_minimal_subtask(max_retries=-1))

    def test_timeout_bounds(self):
        with pytest.raises(ValidationError):
            Subtask(**_minimal_subtask(timeout_s=4))
        with pytest.raises(ValidationError):
            Subtask(**_minimal_subtask(timeout_s=1801))


# --- DAG ------------------------------------------------------------------


def _two_subtask_dag():
    return dict(
        task_id="tsk_01HX",
        version=1,
        subtasks=[
            _minimal_subtask(),
            _minimal_subtask(
                id="st_b",
                specialty_required="research-summarize",
                depends_on=["st_a"],
                inputs={"papers": "workspace://tsk_1/st_a/result"},
                output_key="workspace://tsk_1/st_b/result",
            ),
        ],
    )


class TestDAG:
    def test_two_subtask_dag_valid(self):
        dag = DAG(**_two_subtask_dag())
        assert len(dag.subtasks) == 2
        assert dag.version == 1

    def test_subtasks_must_be_non_empty(self):
        with pytest.raises(ValidationError):
            DAG(task_id="tsk_1", version=1, subtasks=[])

    def test_unique_subtask_ids(self):
        d = _two_subtask_dag()
        d["subtasks"][1]["id"] = "st_a"
        with pytest.raises(ValidationError, match="unique"):
            DAG(**d)

    def test_dangling_depends_on_rejected(self):
        d = _two_subtask_dag()
        d["subtasks"][1]["depends_on"] = ["st_zzz"]
        with pytest.raises(ValidationError, match="depends_on"):
            DAG(**d)

    def test_cycle_rejected(self):
        d = _two_subtask_dag()
        d["subtasks"][0]["depends_on"] = ["st_b"]
        with pytest.raises(ValidationError, match="cycle"):
            DAG(**d)

    def test_self_cycle_rejected(self):
        d = _two_subtask_dag()
        d["subtasks"][0]["depends_on"] = ["st_a"]
        with pytest.raises(ValidationError, match="cycle"):
            DAG(**d)

    def test_inputs_must_reference_declared_dependency(self):
        d = _two_subtask_dag()
        # st_b's input references a subtask it does NOT declare in depends_on
        d["subtasks"][1]["depends_on"] = []
        with pytest.raises(ValidationError, match="inputs"):
            DAG(**d)

    def test_inputs_workspace_uri_must_match_a_dependencys_output_key(self):
        d = _two_subtask_dag()
        d["subtasks"][1]["inputs"] = {"papers": "workspace://tsk_1/st_NONEXISTENT/result"}
        with pytest.raises(ValidationError, match="inputs"):
            DAG(**d)

    def test_topological_order_helper(self):
        dag = DAG(**_two_subtask_dag())
        order = dag.topological_order()
        assert order.index("st_a") < order.index("st_b")

    def test_version_1_locked(self):
        d = _two_subtask_dag()
        d["version"] = 2
        with pytest.raises(ValidationError):
            DAG(**d)


# --- NeedsSubtaskResponse -------------------------------------------------


class TestNeedsSubtask:
    def test_valid_fragment(self):
        resp = NeedsSubtaskResponse(
            status="needs_subtask",
            reason="research-summarize cannot do discovery",
            fragment={
                "subtasks": [
                    _minimal_subtask(id="st_a_sub1", specialty_required="research-deep"),
                ],
                "rejoin_after": ["st_a_sub1"],
            },
        )
        assert resp.fragment.rejoin_after == ["st_a_sub1"]

    def test_status_locked(self):
        with pytest.raises(ValidationError):
            NeedsSubtaskResponse(
                status="something_else",
                reason="x",
                fragment={"subtasks": [_minimal_subtask()], "rejoin_after": ["st_a"]},
            )

    def test_rejoin_after_must_reference_fragment_subtask(self):
        with pytest.raises(ValidationError, match="rejoin_after"):
            NeedsSubtaskResponse(
                status="needs_subtask",
                reason="x",
                fragment={
                    "subtasks": [_minimal_subtask(id="st_x")],
                    "rejoin_after": ["st_NOT_IN_FRAGMENT"],
                },
            )

    def test_fragment_size_bounded(self):
        # ADR says default cap is 5 subtasks
        many = [
            _minimal_subtask(id=f"st_x{i}", output_key=f"workspace://tsk/st_x{i}/result")
            for i in range(6)
        ]
        with pytest.raises(ValidationError, match="fragment"):
            NeedsSubtaskResponse(
                status="needs_subtask",
                reason="too many",
                fragment={"subtasks": many, "rejoin_after": ["st_x0"]},
            )


# --- PlannerRoute ---------------------------------------------------------


class TestPlannerRoute:
    def test_route_values(self):
        assert {r.value for r in PlannerRoute} == {"trivial", "local_13b", "claude_sonnet"}


# --- Fixtures roundtrip ---------------------------------------------------


FIXTURE_DIR = (
    Path(__file__).parents[1] / ".." / "src" / "turing" / "coordinator" / "planner" / "fixtures"
)


@pytest.mark.parametrize(
    "fixture_name,model",
    [
        ("trivial_single_specialty.json", DAG),
        ("two_subtask_research.json", DAG),
        ("needs_subtask_response.json", NeedsSubtaskResponse),
    ],
)
def test_fixture_parses(fixture_name, model):
    path = FIXTURE_DIR.resolve() / fixture_name
    data = json.loads(path.read_text())
    parsed = model.model_validate(data)
    # Round-trip: dumped JSON re-parses identically
    redumped = parsed.model_dump(mode="json")
    model.model_validate(redumped)
