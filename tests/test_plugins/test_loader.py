"""Tests for the plugin-loader allow-list (issue #239).

``load_plugin`` runs arbitrary code via ``exec_module``. The allow-list must
be enforced *before* any plugin code executes.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from turing.plugins.loader import PluginLoader, PluginManifest


def _manifest(name: str, path: Path) -> PluginManifest:
    return PluginManifest(
        name=name,
        version="0.1.0",
        description="",
        entry_point="plugin:DemoPlugin",
        path=path,
    )


class TestEnforceAllowlist:
    def test_no_allowlist_permits_everything(self) -> None:
        loader = PluginLoader(config=SimpleNamespace(allowed_plugins=None))
        loader._enforce_allowlist(_manifest("anything", Path(".")))  # must not raise

    def test_missing_config_permits_everything(self) -> None:
        loader = PluginLoader(config=None)
        loader._enforce_allowlist(_manifest("anything", Path(".")))  # must not raise

    def test_allowlisted_plugin_is_permitted(self) -> None:
        loader = PluginLoader(config=SimpleNamespace(allowed_plugins=["weather"]))
        loader._enforce_allowlist(_manifest("weather", Path(".")))  # must not raise

    def test_plugin_outside_allowlist_is_rejected(self) -> None:
        loader = PluginLoader(config=SimpleNamespace(allowed_plugins=["weather"]))
        with pytest.raises(PermissionError, match="allow-list"):
            loader._enforce_allowlist(_manifest("evil", Path(".")))

    def test_empty_allowlist_rejects_all_plugins(self) -> None:
        loader = PluginLoader(config=SimpleNamespace(allowed_plugins=[]))
        with pytest.raises(PermissionError, match="allow-list"):
            loader._enforce_allowlist(_manifest("weather", Path(".")))


class TestLoadPluginAllowlist:
    def test_disallowed_plugin_code_never_executes(self, tmp_path: Path) -> None:
        """``load_plugin`` must raise before ``exec_module`` runs the module
        body — proving the allow-list gates code execution, not just the
        ``Plugin``-subclass check."""
        plugin_dir = tmp_path / "evil"
        plugin_dir.mkdir()
        # A module body that explodes if it is ever executed.
        (plugin_dir / "plugin.py").write_text(
            "raise RuntimeError('plugin code executed — allow-list was bypassed')\n"
        )
        manifest = _manifest("evil", plugin_dir)
        loader = PluginLoader(config=SimpleNamespace(allowed_plugins=["weather"]))

        # PermissionError (allow-list) — NOT RuntimeError (module body ran).
        with pytest.raises(PermissionError, match="allow-list"):
            loader.load_plugin(manifest)
