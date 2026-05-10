"""Discover the bundled SPA assets directory inside the installed wheel.

The wheel ships ``turing/_webui_dist/`` at the package root (see
``[tool.hatch.build.targets.wheel].force-include`` in pyproject.toml). In
editable installs the same directory shows up at ``<repo>/webui/dist`` —
this helper walks both shapes so the gateway can serve assets either way.
"""

from __future__ import annotations

from pathlib import Path


def spa_assets_path() -> Path | None:
    """Return the SPA assets directory if it exists, else None.

    The gateway treats ``None`` as "no SPA available" — it still serves the
    WebSocket and ``/healthz`` but ``/`` returns 404. That's the right
    behaviour during dev when the operator hasn't run ``npm run build`` yet.
    """
    # Wheel install: <site-packages>/turing/_webui_dist/
    here = Path(__file__).resolve().parent.parent
    bundled = here / "_webui_dist"
    if (bundled / "index.html").is_file():
        return bundled

    # Editable install: walk up to the repo root and look at webui/dist/
    repo_root = here.parent.parent
    editable = repo_root / "webui" / "dist"
    if (editable / "index.html").is_file():
        return editable

    return None
