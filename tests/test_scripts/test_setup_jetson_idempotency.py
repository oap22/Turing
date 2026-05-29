"""Idempotency regression tests for scripts/setup-jetson.sh (ADR 0010, Slice J).

The full provisioning script can only run on a real Jetson, so the load-bearing
kernel — the idempotent hostname prompt that must make a re-run a clean no-op —
is exercised via a self-contained bash harness (tests/scripts/*.sh) plus a
static syntax check here. See docs/adr/0010-slices.md §J.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP_SCRIPT = REPO_ROOT / "scripts" / "setup-jetson.sh"
HOSTNAME_TEST = REPO_ROOT / "tests" / "scripts" / "test_setup_jetson_hostname.sh"

_BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(_BASH is None, reason="bash not available")


def test_setup_jetson_is_syntactically_valid() -> None:
    """`bash -n` must accept the script (no syntax regressions from edits)."""
    assert SETUP_SCRIPT.exists(), SETUP_SCRIPT
    result = subprocess.run(
        [_BASH, "-n", str(SETUP_SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_hostname_prompt_is_rerun_safe() -> None:
    """The hostname prompt keeps the current value on an unattended re-run.

    Drives the bash harness that mirrors the script's resolve logic; the
    critical case is a closed stdin (EOF) re-run not hanging or aborting.
    """
    assert HOSTNAME_TEST.exists(), HOSTNAME_TEST
    result = subprocess.run(
        [_BASH, str(HOSTNAME_TEST)],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESULT: PASS" in result.stdout, result.stdout
