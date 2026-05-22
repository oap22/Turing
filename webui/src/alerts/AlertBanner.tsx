// Sticky top banner — purely presentational. Snooze button arrives in slice 3.

import type { Alert } from "./types";

interface Props {
  alerts: Alert[];
}

function formatValueAndThreshold(a: Alert): string {
  if (a.field === "temp_celsius") {
    return `${a.value.toFixed(1)}°C (>${a.threshold.toFixed(1)}°C)`;
  }
  // Future fields (disk_pct, etc.) format in slice 2; fall through safely.
  return `${a.value} (>${a.threshold})`;
}

function severityClass(severity: Alert["severity"]): string {
  return severity === "danger"
    ? "border-rose-600 bg-rose-950/60 text-rose-100"
    : "border-amber-600 bg-amber-950/60 text-amber-100";
}

export default function AlertBanner({ alerts }: Props) {
  if (alerts.length === 0) return null;
  return (
    <div
      data-testid="alert-banner"
      className="sticky top-0 z-50 flex flex-col gap-1 border-b border-neutral-800 bg-neutral-950 p-2"
    >
      {alerts.map((alert) => (
        <div
          key={`${alert.node_id}::${alert.field}`}
          data-testid="alert-row"
          data-severity={alert.severity}
          className={`flex items-center gap-3 rounded border px-3 py-1.5 text-sm ${severityClass(alert.severity)}`}
        >
          <span className="font-semibold">{alert.node_name}</span>
          <span className="uppercase tracking-wide text-xs opacity-80">
            {alert.field.replace("_", " ")}
          </span>
          <span className="ml-auto font-mono">{formatValueAndThreshold(alert)}</span>
        </div>
      ))}
    </div>
  );
}
