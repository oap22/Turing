"""Migration-chain guards for parallel work (issue #356).

Two PRs that each add a migration off the same parent merge cleanly in git
(different filenames) but leave alembic with two heads, breaking
``alembic upgrade head`` for everyone. Alembic itself may not be importable
in the test env (see ``test_alembic_check_unsupported.py``), so these guards
parse the ``revision`` / ``down_revision`` declarations out of the migration
files directly, mirroring ``test_alembic_002.py``.
"""

from __future__ import annotations

import re
from pathlib import Path

VERSIONS_DIR = Path("alembic/versions")

_REVISION_RE = re.compile(r'^revision(?:\s*:\s*[^=\n]+)?\s*=\s*["\']([^"\']+)["\']', re.MULTILINE)
_DOWN_REVISION_RE = re.compile(
    r'^down_revision(?:\s*:\s*[^=\n]+)?\s*=\s*(None|["\'][^"\']*["\'])', re.MULTILINE
)


def _chain() -> dict[str, str | None]:
    """Map of revision -> down_revision parsed from every migration file."""
    files = sorted(VERSIONS_DIR.glob("*.py"))
    assert files, f"no migration files found under {VERSIONS_DIR}"
    chain: dict[str, str | None] = {}
    for path in files:
        text = path.read_text(encoding="utf-8")
        revision_match = _REVISION_RE.search(text)
        down_match = _DOWN_REVISION_RE.search(text)
        assert revision_match, f"{path.name}: no `revision = ...` declaration found"
        assert down_match, f"{path.name}: no `down_revision = ...` declaration found"
        revision = revision_match.group(1)
        raw_down = down_match.group(1)
        down = None if raw_down == "None" else raw_down.strip("\"'")
        assert revision not in chain, f"duplicate revision id {revision!r} ({path.name})"
        chain[revision] = down
    return chain


def test_every_down_revision_exists():
    chain = _chain()
    for revision, down in chain.items():
        if down is not None:
            assert down in chain, (
                f"revision {revision!r} declares down_revision {down!r}, "
                "which matches no migration file"
            )


def test_exactly_one_root():
    roots = [rev for rev, down in _chain().items() if down is None]
    assert len(roots) == 1, f"expected exactly one root migration, found {roots}"


def test_single_head():
    chain = _chain()
    parents = {down for down in chain.values() if down is not None}
    heads = sorted(set(chain) - parents)
    assert len(heads) == 1, (
        f"alembic chain has {len(heads)} heads {heads}; two parallel PRs likely "
        "added migrations off the same parent — re-point down_revision to the tip"
    )
