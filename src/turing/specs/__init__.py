"""Per-node hardware/runtime specs collection for the fleet specs panel.

See ADR-0001 / issue #214 for the panel's role in the operator UI.
"""

from turing.specs.collector import NodeSpecs, collect_specs

__all__ = ["NodeSpecs", "collect_specs"]
