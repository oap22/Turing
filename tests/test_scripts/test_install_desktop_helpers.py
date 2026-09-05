"""Regression tests for scripts/install-desktop.sh's Linux helpers (#400).

Two behaviors, both extracted from the shipped script by a self-contained bash
harness (tests/scripts/test_install_desktop_helpers.sh, same convention as
test_gateway_unit_guard.sh):

1. ``xdg_data_home()`` — per the XDG Base Directory spec, $XDG_DATA_HOME is
   honored only when set AND absolute; empty or relative values fall back to
   ~/.local/share (mirroring config.rs::xdg_config_dir).
2. ``ere_escape()``/``appimage_pattern()`` — the pgrep/pkill -f pattern used
   to stop a running AppImage must match ONLY the AppImage process itself,
   never a ``tail -f`` on its log or an editor with the path in argv.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "install-desktop.sh"
HELPERS_TEST = REPO_ROOT / "tests" / "scripts" / "test_install_desktop_helpers.sh"

_BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(_BASH is None, reason="bash not available")


def test_install_script_present() -> None:
    assert SCRIPT.exists(), SCRIPT
    text = SCRIPT.read_text()
    # The harness extracts these by name; fail loudly here if they move.
    for fn in ("xdg_data_home()", "ere_escape()", "appimage_pattern()"):
        assert fn in text, f"{fn} missing from install-desktop.sh"


def test_linux_helper_behaviors() -> None:
    """Drives the bash harness across both helper behaviors.

    The load-bearing cases are the refusals: a relative $XDG_DATA_HOME must be
    ignored, and the pkill pattern must not match unrelated processes whose
    command line merely contains the AppImage path.
    """
    assert HELPERS_TEST.exists(), HELPERS_TEST
    result = subprocess.run(
        [_BASH, str(HELPERS_TEST)],
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "RESULT: PASS" in result.stdout, result.stdout
