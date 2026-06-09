import { severityFor, type Severity } from "./thresholds";
import {
  EM_DASH,
  formatGiB,
  formatHardwareLabel,
  formatUptime,
  type PeerSpecsRow,
} from "./types";

// Tailwind tokens for the three severity tiers. Kept in one place so the
// test assertions can reference them directly.
export const SEVERITY_CLASSES: Record<Severity, string> = {
  ok: "text-term-fg",
  warn: "text-amber-300 bg-amber-900/20",
  danger: "text-rose-300 bg-rose-900/30",
};

const STALE_CLASS = "opacity-50";

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
      role="table"
      aria-label="Fleet hardware specifications"
      className="border-t border-term-edge bg-term-panel px-3 py-2 font-mono text-xs"
    >
      <div
        role="row"
        className="mb-1 grid grid-cols-[1.6fr_2.2fr_auto_auto_auto_auto_auto_auto] gap-x-3 text-[10px] uppercase tracking-widest text-term-dim"
      >
        <span role="columnheader">node</span>
        <span role="columnheader">hardware</span>
        <span role="columnheader" className="text-right">CPU%</span>
        <span role="columnheader" className="text-right">MEM</span>
        <span role="columnheader" className="text-right">DISK</span>
        <span role="columnheader" className="text-right">TEMP</span>
        <span role="columnheader" className="text-right">UP</span>
        <span role="columnheader" className="text-right">LOAD</span>
      </div>
      <ol
        role="rowgroup"
        className={
          "space-y-0.5" + (scrolls ? " max-h-48 overflow-y-auto" : "")
        }
      >
        {ordered.map((row) => {
          const severity = severityFor(row.specs);
          const classes = [
            "grid grid-cols-[1.6fr_2.2fr_auto_auto_auto_auto_auto_auto] gap-x-3",
            SEVERITY_CLASSES[severity],
            row.stale ? STALE_CLASS : "",
          ]
            .filter(Boolean)
            .join(" ");
          return (
            <li
              key={row.node_id}
              data-testid={`specs-row-${row.node_id}`}
              data-self={row.self ? "true" : "false"}
              data-severity={severity}
              data-stale={row.stale ? "true" : "false"}
              role="row"
              className={classes}
              style={{ minHeight: "32px", alignItems: "center" }}
            >
              <span role="cell" className="truncate">
                {row.node_name}
                {row.self ? <span className="text-term-accent"> ◆</span> : null}
              </span>
              <span
                role="cell"
                className="truncate text-term-dim"
                data-testid={`hw-${row.node_id}`}
              >
                {formatHardwareLabel(row.specs)}
              </span>
              <span role="cell" className="text-right tabular-nums">
                {fmtCpu(row.specs?.cpu_percent)}
              </span>
              <span role="cell" className="text-right tabular-nums">
                {fmtUsage(row.specs?.mem_used_bytes, row.specs?.ram_total_bytes)}
              </span>
              <span role="cell" className="text-right tabular-nums">
                {fmtUsage(row.specs?.disk_used_bytes, row.specs?.disk_total_bytes)}
              </span>
              <span role="cell" className="text-right tabular-nums">
                {fmtTemp(row.specs?.temp_celsius)}
              </span>
              <span role="cell" className="text-right tabular-nums">
                {formatUptime(row.specs?.uptime_seconds)}
              </span>
              <span role="cell" className="text-right tabular-nums">
                {fmtLoadavg(row.specs)}
              </span>
            </li>
          );
        })}
      </ol>
    </div>
  );
}
