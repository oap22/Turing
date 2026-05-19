import { EM_DASH, type PeerSpecsRow } from "./types";

export interface SpecsGridProps {
  rows: PeerSpecsRow[];
}

// Self-row pinned first, peers sorted alphabetically by node_name. The
// component is presentational only — App.tsx owns the polling.
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

export default function SpecsGrid({ rows }: SpecsGridProps) {
  const ordered = sortRows(rows);
  return (
    <div
      data-testid="specs-grid"
      className="border-t border-neutral-800 px-3 py-2 font-mono text-xs"
    >
      <div className="mb-1 grid grid-cols-[1fr_auto_auto] gap-x-4 text-neutral-500">
        <span>node</span>
        <span className="text-right">CPU%</span>
        <span className="text-right">TEMP</span>
      </div>
      <ol className="space-y-0.5">
        {ordered.map((row) => (
          <li
            key={row.node_id}
            data-testid={`specs-row-${row.node_id}`}
            data-self={row.self ? "true" : "false"}
            className="grid grid-cols-[1fr_auto_auto] gap-x-4 text-neutral-200"
          >
            <span className="truncate">{row.node_name}</span>
            <span className="text-right tabular-nums">
              {fmtCpu(row.specs?.cpu_percent)}
            </span>
            <span className="text-right tabular-nums">
              {fmtTemp(row.specs?.temp_celsius)}
            </span>
          </li>
        ))}
      </ol>
    </div>
  );
}
