"""Regression tests for scripts/setup-coordinator.sh (ADR 0010 §5/§6, Slice G2).

The full provisioning script can only run inside WSL2 Ubuntu, so the load-bearing
kernels that CAN run off-WSL2 are exercised via a self-contained bash harness
(tests/scripts/test_setup_coordinator_guards.sh) plus the static syntax /
shellcheck / env-var checks here. See docs/adr/0010-slices.md §G2.

The harness covers, in isolation:
  • require_wsl2 — the host refusal (rejects a missing/non-WSL osrelease, accepts
    a WSL/Microsoft one) so the systemd + Tailscale-in-WSL2 assumptions never run
    on the wrong host.
  • Phase 5 seed-durability ordering — the coordinator.seed sentinel that arms the
    re-run skip guard is committed only AFTER the worker seeds are persisted, the
    conf is rendered, and the coordinator seed/URL are in .env (recon #3).
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP_SCRIPT = REPO_ROOT / "scripts" / "setup-coordinator.sh"
COORDINATOR_DIR = REPO_ROOT / "scripts" / "coordinator"
GUARDS_TEST = REPO_ROOT / "tests" / "scripts" / "test_setup_coordinator_guards.sh"

_BASH = shutil.which("bash")
_SHELLCHECK = shutil.which("shellcheck")
pytestmark = pytest.mark.skipif(_BASH is None, reason="bash not available")


def test_setup_coordinator_is_syntactically_valid() -> None:
    """`bash -n` must accept the script (no syntax regressions from edits)."""
    assert SETUP_SCRIPT.exists(), SETUP_SCRIPT
    result = subprocess.run(
        [_BASH, "-n", str(SETUP_SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(_SHELLCHECK is None, reason="shellcheck not available")
def test_setup_coordinator_is_shellcheck_clean() -> None:
    """shellcheck must report no findings on the shipped script."""
    result = subprocess.run(
        [_SHELLCHECK, str(SETUP_SCRIPT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_systemd_unit_templates_exist() -> None:
    """The three turing-* unit templates Phase 7 installs must be present."""
    for unit in (
        "turing-coordinator.service",
        "turing-gateway.service",
        "turing-vault-watcher.service",
    ):
        assert (COORDINATOR_DIR / unit).exists(), f"missing unit template: {unit}"


def test_coordinator_role_flags_are_written() -> None:
    """Phase 4.2 must enable mesh + gateway so __main__.py actually starts them.

    `python -m turing` gates the mesh node, the alerts dispatcher, and the
    in-process gateway on these flags (src/turing/__main__.py steps 6/8/8b);
    without them the coordinator boots the agent loop alone (recon #2).
    """
    body = SETUP_SCRIPT.read_text()
    assert "TURING_MESH_ENABLED true" in body, body
    assert "TURING_GATEWAY_ENABLED true" in body, body


def test_coordinator_nats_seed_and_url_written_to_env() -> None:
    """Phase 5.3b must persist the coordinator's own seed + URL to .env.

    Without these the coordinator reads config.nats_nkey_seed=None and cannot
    authenticate to its own bus when mesh is enabled (recon #2/#3).
    """
    body = SETUP_SCRIPT.read_text()
    assert "TURING_NATS_NKEY_SEED=" in body, body
    assert "TURING_NATS_URL=" in body, body
    assert "TURING_VAULT_PATH=" in body, body


def test_tls_cert_has_subject_alt_name() -> None:
    """The self-signed NATS cert must carry a SAN — modern TLS clients verify it.

    A cert without subjectAltName fails the workers' TLS handshake (recon #5).
    """
    body = SETUP_SCRIPT.read_text()
    assert "subjectAltName=" in body, body
    assert "-addext" in body, body


def test_guards_harness_passes() -> None:
    """Drive the bash harness: require_wsl2 refusal + Phase 5 seed ordering.

    The harness extracts and runs the real require_wsl2 function and asserts the
    sentinel-commit ordering invariant; a closed stdin must not hang or abort.
    """
    assert GUARDS_TEST.exists(), GUARDS_TEST
    result = subprocess.run(
        [_BASH, str(GUARDS_TEST)],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESULT: PASS" in result.stdout, result.stdout
