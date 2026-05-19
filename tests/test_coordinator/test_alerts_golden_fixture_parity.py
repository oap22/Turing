"""Guard that the golden frame fixture is byte-identical on Py and TS sides.

The vitest banner test imports the copy in
``webui/src/alerts/__tests__/fixtures/`` because tsconfig limits include to
``src``. This test enforces the contract that both copies stay in lockstep.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_PY = _ROOT / "tests" / "test_coordinator" / "fixtures" / "alert_frames_golden.json"
_TS = _ROOT / "webui" / "src" / "alerts" / "__tests__" / "fixtures" / "alert_frames_golden.json"


def test_golden_fixture_parity() -> None:
    if not _TS.is_file():
        pytest.skip("webui fixture absent (sdist install)")
    assert _PY.read_bytes() == _TS.read_bytes(), (
        "Golden frame fixture drift — keep tests/test_coordinator/fixtures and "
        "webui/src/alerts/__tests__/fixtures in lockstep."
    )
