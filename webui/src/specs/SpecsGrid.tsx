import {
  EM_DASH,
  formatGiB,
  formatHardwareLabel,
  formatUptime,
  type PeerSpecsRow,
} from "./types";

export interface SpecsGridProps {
  rows: PeerSpecsRow[];
}

const SCROLL_THRESHOLD = 6;

function sortRows(rows: PeerSpecsRow[]): PeerSpecsRow[] {
  const self = rows.filter((r) => r.self);
  const peers = rows
    .filter((r) => !r.self)
    .slice()
    .sort((a, b) => a.node_name.localeCompare(b.node_name));
  return [...self, ...peers];
}

function fmtCpu(value: number | undefined): string {
  if (typeof value !== "number" || Number.isNaN(value)) return EM_DASH;
  return `${value.toFixed(1)}%`;
}

function fmtTemp(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return EM_DASH;
  return `${value.toFixed(1)}°C`;
}

function fmtLoadavg(specs: PeerSpecsRow["specs"]): string {
  if (specs === null) return EM_DASH;
  return `${specs.loadavg_1m.toFixed(2)} ${specs.loadavg_5m.toFixed(2)} ${specs.loadavg_15m.toFixed(2)}`;
}

function fmtUsage(used: number | undefined, total: number | undefined): string {
  if (typeof used !== "number" || typeof total !== "number" || total <= 0) return EM_DASH;
  const pct = (used / total) * 100;
  return `${formatGiB(used)} / ${formatGiB(total)} (${pct.toFixed(0)}%)`;
}

export default function SpecsGrid({ rows }: SpecsGridProps) {
  const ordered = sortRows(rows);
  // Scroll only when there's more rows than fit comfortably (~32 px per row,
  // matches the PRD wireframe). Below threshold we let the grid auto-size.
  const scrolls = ordered.length > SCROLL_THRESHOLD;
  return (
    <div
      data-testid="specs-grid"
      data-scrolls={scrolls ? "true" : "false"}
      className="border-t border-neutral-800 px-3 py-2 font-mono text-xs"
    >
      <div className="mb-1 grid grid-cols-[1.6fr_2.2fr_auto_auto_auto_auto_auto_auto] gap-x-3 text-neutral-500">
        <span>node</span>
        <span>hardware</span>
        <span className="text-right">CPU%</span>
        <span className="text-right">MEM</span>
        <span className="text-right">DISK</span>
        <span className="text-right">TEMP</span>
        <span className="text-right">UP</span>
        <span className="text-right">LOAD</span>
      </div>
      <ol
        className={
          "space-y-0.5" + (scrolls ? " max-h-48 overflow-y-auto" : "")
        }
      >
        {ordered.map((row) => (
          <li
            key={row.node_id}
            data-testid={`specs-row-${row.node_id}`}
            data-self={row.self ? "true" : "false"}
            className="grid grid-cols-[1.6fr_2.2fr_auto_auto_auto_auto_auto_auto] gap-x-3 text-neutral-200"
            style={{ minHeight: "32px", alignItems: "center" }}
          >
            <span className="truncate">{row.node_name}</span>
            <span className="truncate text-neutral-400" data-testid={`hw-${row.node_id}`}>
              {formatHardwareLabel(row.specs)}
            </span>
            <span className="text-right tabular-nums">
              {fmtCpu(row.specs?.cpu_percent)}
            </span>
            <span className="text-right tabular-nums">
              {fmtUsage(row.specs?.mem_used_bytes, row.specs?.ram_total_bytes)}
            </span>
            <span className="text-right tabular-nums">
              {fmtUsage(row.specs?.disk_used_bytes, row.specs?.disk_total_bytes)}
            </span>
            <span className="text-right tabular-nums">
              {fmtTemp(row.specs?.temp_celsius)}
            </span>
            <span className="text-right tabular-nums">
              {formatUptime(row.specs?.uptime_seconds)}
            </span>
            <span className="text-right tabular-nums">{fmtLoadavg(row.specs)}</span>
          </li>
        ))}
      </ol>
    </div>
  );
}
