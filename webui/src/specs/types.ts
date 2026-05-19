// Mirrors the per-node specs block published by ``PresenceService``.
// Full schema landed in slice 2/3 (#216). ``null`` for ``temp_celsius``
// always means "not collected on this OS" rather than "stale".
export interface NodeSpecs {
  // Static (collected once at startup)
  model_name: string;
  os: string;
  arch: string;
  cpu_cores: number;
  ram_total_bytes: number;
  disk_total_bytes: number;

  // Live (recollected per heartbeat)
  cpu_percent: number;
  mem_used_bytes: number;
  disk_used_bytes: number;
  temp_celsius: number | null;
  uptime_seconds: number;
  loadavg_1m: number;
  loadavg_5m: number;
  loadavg_15m: number;
}

// One row in the specs grid. Mirrors a single entry from ``GET /peers``,
// trimmed to the fields the grid actually renders.
export interface PeerSpecsRow {
  node_id: string;
  node_name: string;
  self: boolean;
  specs: NodeSpecs | null;
  // True when the gateway hasn't heard from this peer within the
  // presence-staleness window (#217). Self-row is always false. Stale
  // rows still render their last-known values, dimmed.
  stale: boolean;
}

export const EM_DASH = "—";

const GIB = 1024 ** 3;

// Format raw bytes as a compact human label. We use binary units (GiB)
// since psutil totals are based on 1024 multiples.
export function formatGiB(bytes: number | null | undefined): string {
  if (typeof bytes !== "number" || !Number.isFinite(bytes) || bytes <= 0) return EM_DASH;
  const value = bytes / GIB;
  // Whole numbers for >= 1 GiB to keep the row tight; one decimal below.
  return value >= 10
    ? `${Math.round(value)} GiB`
    : `${value.toFixed(1)} GiB`;
}

export function formatHardwareLabel(specs: NodeSpecs | null): string {
  if (specs === null) return EM_DASH;
  const ram = formatGiB(specs.ram_total_bytes);
  // Compact model name — strip "Model B Rev x.y" and "Developer Kit" noise.
  const shortModel = specs.model_name
    .replace(/Raspberry Pi (\d+).*$/i, "Pi $1")
    .replace(/NVIDIA Jetson ([A-Za-z]+ ?[A-Za-z]*).*$/i, "Jetson $1")
    .replace(/MacBookPro\d+,\d+/, "MacBook Pro")
    .trim();
  return `${shortModel} · ${ram} · ${specs.cpu_cores}c`;
}

export function formatUptime(seconds: number | null | undefined): string {
  if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) return EM_DASH;
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.floor(hours / 24);
  return `${days}d`;
}
