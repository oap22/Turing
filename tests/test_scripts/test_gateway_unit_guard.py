"""Regression test for the turing-gateway.service ExecStart guard (ADR 0010 §6).

The guard decides — from TURING_GATEWAY_STANDALONE + TURING_GATEWAY_ENABLED —
whether to start the dedicated gateway or hold as a no-op, and must never start
a second gateway while the coordinator's in-process one still owns the port.
The decision logic lives in a `.service` inline shell string, so it is
exercised by a self-contained bash harness (tests/scripts/*.sh) that extracts
the real body and stubs the python/sleep execs.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
UNIT = REPO_ROOT / "scripts" / "coordinator" / "turing-gateway.service"
GUARD_TEST = REPO_ROOT / "tests" / "scripts" / "test_gateway_unit_guard.sh"

_BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(_BASH is None, reason="bash not available")


def test_gateway_unit_present() -> None:
    assert UNIT.exists(), UNIT
    assert "ExecStart=/bin/sh -c" in UNIT.read_text()


def test_standalone_guard_decisions() -> None:
    """Standalone starts ONLY with STANDALONE truthy AND ENABLED explicitly false.

    Drives the harness across the env matrix; the load-bearing cases are the
    refusals (STANDALONE=1 while ENABLED is still truthy/unset) that prevent a
    port double-bind crash-loop.
    """
    assert GUARD_TEST.exists(), GUARD_TEST
    result = subprocess.run(
        [_BASH, str(GUARD_TEST)],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESULT: PASS" in result.stdout, result.stdout
