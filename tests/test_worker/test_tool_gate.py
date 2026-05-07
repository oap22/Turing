"""Tests for WorkerToolGate — worker-side enforcement of story 9.

A worker must refuse any subtask whose required_tools aren't a subset of its
manifest tools, even if the coordinator dispatched it. This is defence in
depth: a buggy or misconfigured coordinator can never make a research worker
run shell.
"""

from __future__ import annotations

import pytest

from turing.coordinator.scheduler import Subtask
from turing.worker.tools.gate import ToolGateError, WorkerToolGate


def test_gate_accepts_subtask_when_required_tools_are_a_subset() -> None:
    gate = WorkerToolGate(advertised_tools=("vault_query", "web_fetch"))
    gate.check(
        Subtask(
            subtask_id="sub-1",
            specialty_required="research-summarize",
            required_tools=("vault_query",),
        )
    )


def test_gate_accepts_subtask_with_no_required_tools() -> None:
    gate = WorkerToolGate(advertised_tools=("vault_query",))
    gate.check(
        Subtask(
            subtask_id="sub-1",
            specialty_required="research-summarize",
            required_tools=(),
        )
    )


def test_gate_refuses_subtask_with_unadvertised_tool() -> None:
    gate = WorkerToolGate(advertised_tools=("vault_query",))

    with pytest.raises(ToolGateError, match="shell"):
        gate.check(
            Subtask(
                subtask_id="sub-1",
                specialty_required="research-summarize",
                required_tools=("shell",),
            )
        )
