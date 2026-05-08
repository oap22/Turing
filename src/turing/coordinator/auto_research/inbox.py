"""inbox_directory_for — derive ``vault/inbox/auto_research/<specialty>/<date>/``."""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

_SAFE_SPECIALTY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def inbox_directory_for(*, specialty: str, at: datetime) -> Path:
    if not _SAFE_SPECIALTY_RE.match(specialty):
        raise ValueError(f"specialty must match {_SAFE_SPECIALTY_RE.pattern!r}")
    date_str = at.strftime("%Y-%m-%d")
    return Path("vault") / "inbox" / "auto_research" / specialty / date_str
