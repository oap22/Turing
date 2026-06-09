"""ADR numbering guard for parallel work (issue #356).

Two agents that each add ``docs/adr/0011-*.md`` merge cleanly in git but
leave the decision log ambiguous. ADR numbers are claimed once; new ADRs
take the next free number. The pre-existing duplicates (0001 x2; 0010 x2,
where the slices file is a companion to the 0010 ADR proper) are
grandfathered at exactly their current counts — adding a *third* file under
either number still fails.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

ADR_DIR = Path("docs/adr")

GRANDFATHERED_DUPLICATES = {"0001": 2, "0010": 2}


def test_adr_numbers_unique_outside_grandfathered():
    files = sorted(ADR_DIR.glob("*.md"))
    assert files, f"no ADR files found under {ADR_DIR}"

    numbers: Counter[str] = Counter()
    for path in files:
        prefix = path.name.split("-", 1)[0]
        assert len(prefix) == 4 and prefix.isdigit(), (
            f"{path.name}: ADR filenames must start with a 4-digit number"
        )
        numbers[prefix] += 1

    next_free = int(max(numbers)) + 1
    for number, count in sorted(numbers.items()):
        allowed = GRANDFATHERED_DUPLICATES.get(number, 1)
        assert count <= allowed, (
            f"ADR number {number} is used by {count} files (allowed: {allowed}); "
            f"numbers are claimed once — the next free number is {next_free:04d}"
        )
