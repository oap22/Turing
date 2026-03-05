"""Plugin loader for discovering and instantiating plugins from the filesystem."""

from __future__ import annotations

import importlib
import importlib.util
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from turing.plugins.base import Plugin

logger = structlog.get_logger("turing.plugins.loader")


@dataclass
class PluginManifest:
    """Parsed representation of a plugin's manifest.json."""

    name: str
    version: str
    description: str
    entry_point: str  # "module:ClassName"
    dependencies: list[str] = field(default_factory=list)
    config_schema: dict[str, Any] = field(default_factory=dict)
    path: Path = field(default_factory=lambda: Path("."))


class PluginLoader:
    """Discover and load plugins from the filesystem.

    Each plugin lives in its own directory under the plugins root and must
    contain a ``manifest.json`` file describing its entry point and metadata.
    """

    def __init__(self, config: Any | None = None) -> None:
        self._config = config

    def scan_directory(self, path: str | Path) -> list[PluginManifest]:
        """Scan a directory for plugin manifests.

        Each subdirectory containing a ``manifest.json`` file is treated as
        a potential plugin.

        Args:
            path: Root directory to scan for plugins.

        Returns:
            A list of parsed PluginManifest objects.
        """
        root = Path(path)
        manifests: list[PluginManifest] = []

        if not root.exists() or not root.is_dir():
            logger.warning("plugin_directory_not_found", path=str(root))
            return manifests

        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            manifest_file = entry / "manifest.json"
            if not manifest_file.exists():
                continue

            try:
                manifest = self._parse_manifest(manifest_file, entry)
                manifests.append(manifest)
                logger.info(
                    "plugin_manifest_found",
                    name=manifest.name,
                    version=manifest.version,
                    path=str(entry),
                )
            except Exception as exc:
                logger.error(
                    "plugin_manifest_error",
                    path=str(manifest_file),
                    error=str(exc),
                )

        return manifests

    def load_plugin(self, manifest: PluginManifest) -> Plugin:
        """Load and instantiate a plugin from its manifest.

        The manifest's ``entry_point`` field must be in the format
        ``module_name:ClassName``.  The module is loaded from the plugin
        directory, and the class is instantiated with no arguments.

        Args:
            manifest: The manifest describing the plugin to load.

        Returns:
            An instantiated Plugin object.

        Raises:
            ImportError: If the module cannot be loaded.
            AttributeError: If the class is not found in the module.
            TypeError: If the class is not a Plugin subclass.
        """
        module_name, class_name = manifest.entry_point.split(":", 1)

        # Build the module path from the plugin directory.
        plugin_dir = manifest.path
        module_file = plugin_dir / f"{module_name}.py"

        if module_name == "__init__":
            module_file = plugin_dir / "__init__.py"

        if not module_file.exists():
            raise ImportError(
                f"Module file '{module_file}' not found for plugin '{manifest.name}'"
            )

        # Create a unique module name to avoid collisions.
        unique_module_name = f"turing_plugin_{manifest.name}_{module_name}"

        spec = importlib.util.spec_from_file_location(unique_module_name, module_file)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot create module spec for '{module_file}'")

        module = importlib.util.module_from_spec(spec)
        sys.modules[unique_module_name] = module
        spec.loader.exec_module(module)

        # Get the plugin class.
        plugin_class = getattr(module, class_name, None)
        if plugin_class is None:
            raise AttributeError(
                f"Class '{class_name}' not found in module '{module_file}'"
            )

        # Instantiate and validate.
        instance = plugin_class()
        if not isinstance(instance, Plugin):
            raise TypeError(
                f"Class '{class_name}' in plugin '{manifest.name}' "
                f"does not inherit from Plugin"
            )

        logger.info(
            "plugin_loaded",
            name=manifest.name,
            version=manifest.version,
            class_name=class_name,
        )
        return instance

    def _parse_manifest(self, manifest_file: Path, plugin_dir: Path) -> PluginManifest:
        """Parse a manifest.json file into a PluginManifest."""
        with open(manifest_file, encoding="utf-8") as f:
            data = json.load(f)

        return PluginManifest(
            name=data["name"],
            version=data.get("version", "0.1.0"),
            description=data.get("description", ""),
            entry_point=data["entry_point"],
            dependencies=data.get("dependencies", []),
            config_schema=data.get("config_schema", {}),
            path=plugin_dir,
        )
