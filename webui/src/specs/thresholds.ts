// Operator-facing severity thresholds for the fleet specs panel.
// Numbers are hardcoded for slice 3/3 (#217); configuration is out of
// scope per the PRD. ``severityFor`` returns the worst severity across
// every live field — a single danger pegs the row to danger.

import type { NodeSpecs } from "./types";

export const CPU_WARN = 80;
export const CPU_DANGER = 95;

export const MEM_WARN = 80;
export const MEM_DANGER = 92;

export const DISK_WARN = 85;
export const DISK_DANGER = 95;

// Conservative — Pi 5 throttles at 85 °C; we want warning room.
export const TEMP_WARN = 75;
export const TEMP_DANGER = 82;

export type Severity = "ok" | "warn" | "danger";

const ORDER: Record<Severity, number> = { ok: 0, warn: 1, danger: 2 };

function worst(a: Severity, b: Severity): Severity {
  return ORDER[a] >= ORDER[b] ? a : b;
}

function gradeNumeric(value: number, warn: number, danger: number): Severity {
  if (value >= danger) return "danger";
  if (value >= warn) return "warn";
  return "ok";
}

function pct(used: number, total: number): number {
  if (total <= 0) return 0;
  return (used / total) * 100;
}

// Returns the worst severity across CPU%, memory utilisation, disk
// utilisation, and temperature. Static fields don't participate.
export function severityFor(specs: NodeSpecs | null | undefined): Severity {
  if (!specs) return "ok";
  let result: Severity = "ok";
  result = worst(result, gradeNumeric(specs.cpu_percent, CPU_WARN, CPU_DANGER));
  result = worst(
    result,
    gradeNumeric(pct(specs.mem_used_bytes, specs.ram_total_bytes), MEM_WARN, MEM_DANGER),
  );
  result = worst(
    result,
    gradeNumeric(
      pct(specs.disk_used_bytes, specs.disk_total_bytes),
      DISK_WARN,
      DISK_DANGER,
    ),
  );
  if (specs.temp_celsius !== null) {
    result = worst(result, gradeNumeric(specs.temp_celsius, TEMP_WARN, TEMP_DANGER));
  }
  return result;
}
