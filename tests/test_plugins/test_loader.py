"""Tests for the plugin-loader allow-list (issues #239, #346).

``load_plugin`` runs arbitrary code via ``exec_module``. The allow-list must
be enforced *before* any plugin code executes, and it is fail-closed: an
unset allow-list loads nothing; ``["*"]`` is the explicit opt-in wildcard.
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
    def test_no_allowlist_rejects_everything(self) -> None:
        """Fail-closed default (issue #346): unset allow-list loads nothing."""
        loader = PluginLoader(config=SimpleNamespace(allowed_plugins=None))
        with pytest.raises(PermissionError, match="fail-closed"):
            loader._enforce_allowlist(_manifest("anything", Path(".")))

    def test_missing_config_rejects_everything(self) -> None:
        loader = PluginLoader(config=None)
        with pytest.raises(PermissionError, match="fail-closed"):
            loader._enforce_allowlist(_manifest("anything", Path(".")))

    def test_wildcard_permits_everything(self) -> None:
        loader = PluginLoader(config=SimpleNamespace(allowed_plugins=["*"]))
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


def _booby_trapped_plugin(tmp_path: Path, name: str) -> PluginManifest:
    """Create a plugin whose module body explodes if it is ever executed."""
    plugin_dir = tmp_path / name
    plugin_dir.mkdir()
    (plugin_dir / "plugin.py").write_text(
        "raise RuntimeError('plugin code executed — allow-list was bypassed')\n"
    )
    return _manifest(name, plugin_dir)


class TestLoadPluginAllowlist:
    def test_disallowed_plugin_code_never_executes(self, tmp_path: Path) -> None:
        """``load_plugin`` must raise before ``exec_module`` runs the module
        body — proving the allow-list gates code execution, not just the
        ``Plugin``-subclass check."""
        manifest = _booby_trapped_plugin(tmp_path, "evil")
        loader = PluginLoader(config=SimpleNamespace(allowed_plugins=["weather"]))

        # PermissionError (allow-list) — NOT RuntimeError (module body ran).
        with pytest.raises(PermissionError, match="allow-list"):
            loader.load_plugin(manifest)

    def test_default_unset_allowlist_never_executes_plugin_code(self, tmp_path: Path) -> None:
        """Issue #346 regression: with the default ``allowed_plugins=None``,
        a plugin dropped into ``plugins/`` must NOT be executed."""
        manifest = _booby_trapped_plugin(tmp_path, "dropped_in")
        loader = PluginLoader(config=SimpleNamespace(allowed_plugins=None))

        with pytest.raises(PermissionError, match="fail-closed"):
            loader.load_plugin(manifest)

    def test_wildcard_loads_discovered_plugin(self, tmp_path: Path) -> None:
        """``["*"]`` is the explicit opt-in: discovered plugins load."""
        plugin_dir = tmp_path / "demo"
        plugin_dir.mkdir()
        (plugin_dir / "plugin.py").write_text(
            "from turing.plugins.base import Plugin\n"
            "\n"
            "class DemoPlugin(Plugin):\n"
            "    @property\n"
            "    def name(self) -> str:\n"
            "        return 'demo'\n"
            "\n"
            "    @property\n"
            "    def description(self) -> str:\n"
            "        return 'demo plugin'\n"
        )
        manifest = _manifest("demo", plugin_dir)
        loader = PluginLoader(config=SimpleNamespace(allowed_plugins=["*"]))

        plugin = loader.load_plugin(manifest)
        assert plugin.name == "demo"

    def test_explicit_list_excluding_plugin_never_executes_code(self, tmp_path: Path) -> None:
        manifest = _booby_trapped_plugin(tmp_path, "excluded")
        loader = PluginLoader(config=SimpleNamespace(allowed_plugins=["weather", "calendar"]))

        with pytest.raises(PermissionError, match="allow-list"):
            loader.load_plugin(manifest)
