"""NATS .env regression tests for scripts/setup-jetson.sh (ADR 0010 §9, Slice H).

The full provisioning script can only run on a real Jetson, so the load-bearing
kernel — the Phase 4 resolve that adds TURING_NATS_URL and TURING_NATS_NKEY_SEED,
prompting (or taking env-var fallbacks) on a fresh install and PRESERVING the
existing .env on a re-run — is exercised via a self-contained bash harness
(tests/scripts/*.sh) plus a static syntax check here. See
docs/adr/0010-discord-retirement-and-coordinator-bringup.md §9.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP_SCRIPT = REPO_ROOT / "scripts" / "setup-jetson.sh"
NATS_TEST = REPO_ROOT / "tests" / "scripts" / "test_setup_jetson_env_nats.sh"

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


def test_env_emits_nats_fields() -> None:
    """The generated worker .env must declare both NATS fields.

    Guards against the heredoc losing the fields the systemd ExecStartPre guard
    (and TuringConfig) now depend on.
    """
    body = SETUP_SCRIPT.read_text()
    assert "TURING_NATS_URL=" in body, body
    assert "TURING_NATS_NKEY_SEED=" in body, body


def test_nats_resolve_is_fresh_and_rerun_safe() -> None:
    """Fresh install resolves/prompts; re-run preserves the existing .env.

    Drives the bash harness that extracts and runs the script's real resolve
    block: typed + env-var fallback win on a fresh box, a missing required
    field aborts, and an already-present .env is kept untouched even with a
    closed stdin or stale exports.
    """
    assert NATS_TEST.exists(), NATS_TEST
    result = subprocess.run(
        [_BASH, str(NATS_TEST)],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESULT: PASS" in result.stdout, result.stdout
