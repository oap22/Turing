// Mirrors the per-node specs block published by ``PresenceService``
// (#215). Future slices grow this record in place; ``null`` always means
// "not collected on this OS" rather than "stale".
export interface NodeSpecs {
  cpu_percent: number;
  temp_celsius: number | null;
}

// One row in the specs grid. Mirrors a single entry from ``GET /peers``,
// trimmed to the fields the grid actually renders.
export interface PeerSpecsRow {
  node_id: string;
  node_name: string;
  self: boolean;
  specs: NodeSpecs | null;
}

export const EM_DASH = "—";
