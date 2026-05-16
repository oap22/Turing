"""Custom Hatch build hook.

Conditionally bundles the operator-UI SPA (``webui/dist``) into the wheel
**only if** it has been pre-built. This lets ``pip install -e .`` succeed in
a fresh worktree or clone without first running ``npm run build`` inside
``webui/`` — backend-only contributors don't need a Node toolchain.

When ``webui/dist`` is present (CI/release builds, or after a local
``npm run build``), the directory is force-included at the same wheel path
as before (``turing/_webui_dist``), so the gateway's
``turing.gateway.spa_assets_path()`` resolves correctly and the SPA is
served.

See issue #195.
"""

from __future__ import annotations

import os
from typing import Any

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class WebuiDistBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict[str, Any]) -> None:
        del version  # unused; required by hook interface
        webui_dist = os.path.join(self.root, "webui", "dist")
        if os.path.isdir(webui_dist):
            build_data.setdefault("force_include", {})[webui_dist] = "turing/_webui_dist"
