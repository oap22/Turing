"""Test η: Python TEMP/DISK thresholds match the TypeScript constants verbatim.

Reads ``webui/src/specs/thresholds.ts`` directly and parses the relevant
constants out of the source. Skips gracefully (with a comment naming the
trade-off) when the file is absent — sdist installs / minimal-image CI
won't have it, and we'd rather pass than blow up on a missing artefact.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from turing.coordinator.alerts.types import (
    DISK_DANGER,
    DISK_WARN,
    TEMP_DANGER,
    TEMP_WARN,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_THRESHOLDS_TS = _REPO_ROOT / "webui" / "src" / "specs" / "thresholds.ts"


def _extract_const(source: str, name: str) -> float | None:
    m = re.search(rf"export const {name}\s*=\s*([0-9.]+)\s*;", source)
    if m is None:
        return None
    return float(m.group(1))


def test_temp_constants_parity() -> None:
    if not _THRESHOLDS_TS.is_file():
        pytest.skip(
            "webui/src/specs/thresholds.ts not present "
            "(sdist install or minimal-image CI); accept the drift risk for that build"
        )
    src = _THRESHOLDS_TS.read_text()
    ts_warn = _extract_const(src, "TEMP_WARN")
    ts_danger = _extract_const(src, "TEMP_DANGER")
    assert ts_warn is not None, "TEMP_WARN not found in thresholds.ts"
    assert ts_danger is not None, "TEMP_DANGER not found in thresholds.ts"
    assert ts_warn == TEMP_WARN, f"TEMP_WARN drift: python={TEMP_WARN} ts={ts_warn}"
    assert ts_danger == TEMP_DANGER, f"TEMP_DANGER drift: python={TEMP_DANGER} ts={ts_danger}"


def test_disk_constants_parity() -> None:
    if not _THRESHOLDS_TS.is_file():
        pytest.skip(
            "webui/src/specs/thresholds.ts not present "
            "(sdist install or minimal-image CI); accept the drift risk for that build"
        )
    src = _THRESHOLDS_TS.read_text()
    ts_warn = _extract_const(src, "DISK_WARN")
    ts_danger = _extract_const(src, "DISK_DANGER")
    assert ts_warn is not None, "DISK_WARN not found in thresholds.ts"
    assert ts_danger is not None, "DISK_DANGER not found in thresholds.ts"
    assert ts_warn == DISK_WARN, f"DISK_WARN drift: python={DISK_WARN} ts={ts_warn}"
    assert ts_danger == DISK_DANGER, f"DISK_DANGER drift: python={DISK_DANGER} ts={ts_danger}"
