// Sticky top banner for hardware-safety alerts. Each row carries a
// "snooze 4h" button (#232); a snoozed row dims and shows the remaining
// window instead of the button.

import { useEffect, useState } from "react";
import type { Alert } from "./types";

interface Props {
  alerts: Alert[];
}

// Slow re-render so "snoozed 3h 58m" counts down and an expired snooze
// drops back to undimmed without waiting for a fresh frame.
const SNOOZE_TICK_MS = 60_000;

function formatValueAndThreshold(a: Alert): string {
  if (a.field === "temp_celsius") {
    return `${a.value.toFixed(1)}°C (>${a.threshold.toFixed(1)}°C)`;
  }
  if (a.field === "disk_pct") {
    return `${Math.round(a.value)}% (>${Math.round(a.threshold)}%)`;
  }
  return `${a.value} (>${a.threshold})`;
}

function severityClass(severity: Alert["severity"]): string {
  return severity === "danger"
    ? "border-rose-600 bg-rose-950/60 text-rose-100"
    : "border-amber-600 bg-amber-950/60 text-amber-100";
}

function formatRemaining(ms: number): string {
  const totalMin = Math.max(0, Math.ceil(ms / 60_000));
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

function rowKey(a: { node_id: string; field: string }): string {
  return `${a.node_id}::${a.field}`;
}

export default function AlertBanner({ alerts }: Props) {
  // Optimistic snooze expiries keyed by node_id::field — set from the POST
  // response so the row dims immediately, before the authoritative `update`
  // frame round-trips through the WS reducer.
  const [optimistic, setOptimistic] = useState<Record<string, number>>({});
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), SNOOZE_TICK_MS);
    return () => clearInterval(id);
  }, []);

  async function handleSnooze(alert: Alert): Promise<void> {
    const key = rowKey(alert);
    try {
      const res = await fetch(
        `/alerts/${encodeURIComponent(alert.node_id)}/${encodeURIComponent(
          alert.field,
        )}/snooze`,
        {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json" },
          body: "{}",
        },
      );
      if (!res.ok) return;
      const body = (await res.json()) as { snoozed_until_ms?: number };
      if (typeof body.snoozed_until_ms === "number") {
        const expiry = body.snoozed_until_ms;
        setOptimistic((prev) => ({ ...prev, [key]: expiry }));
      }
    } catch {
      // best-effort — the operator can click again
    }
  }

  if (alerts.length === 0) return null;
  return (
    <div
      data-testid="alert-banner"
      className="sticky top-0 z-50 flex flex-col gap-1 border-b border-term-edge bg-term-bg p-2"
    >
      {alerts.map((alert) => {
        const key = rowKey(alert);
        // Engine truth (reducer) wins; the optimistic value only bridges
        // the gap until the `update` frame arrives.
        const expiry = Math.max(
          alert.snoozed_until_ms ?? 0,
          optimistic[key] ?? 0,
        );
        const snoozed = expiry > now;
        return (
          <div
            key={key}
            data-testid="alert-row"
            data-severity={alert.severity}
            data-snoozed={snoozed ? "true" : "false"}
            className={`flex items-center gap-3 border px-3 py-1.5 text-sm ${severityClass(
              alert.severity,
            )}${snoozed ? " opacity-50" : ""}`}
          >
            <span aria-hidden="true">▌</span>
            <span className="font-semibold">{alert.node_name}</span>
            <span className="text-xs uppercase tracking-widest opacity-80">
              {alert.field.replace("_", " ")}
            </span>
            <span className="ml-auto font-mono">
              {formatValueAndThreshold(alert)}
            </span>
            {snoozed ? (
              <span
                data-testid="snooze-remaining"
                className="font-mono text-xs opacity-80"
              >
                snoozed {formatRemaining(expiry - now)}
              </span>
            ) : (
              <button
                type="button"
                data-testid="snooze-button"
                onClick={() => void handleSnooze(alert)}
                className="border border-current px-2 py-0.5 text-[10px] uppercase tracking-wider hover:bg-white/10"
              >
                snooze 4h
              </button>
            )}
          </div>
        );
      })}
    </div>
  );
}
