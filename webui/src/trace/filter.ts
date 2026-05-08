/** Pure filter helpers for the trace pane.
 *
 * The /api/events route does the heavy lifting on the historical fetch,
 * but the SPA layers live WS events on top — that means the same filter
 * logic has to run client-side over the in-memory list. Keeping it pure
 * + unit-tested means a regression in either path stays cheap to fix.
 */

import type { TraceEvent, TraceFilter } from "./types";

export function applyFilter(
  events: TraceEvent[],
  filter: TraceFilter,
): TraceEvent[] {
  return events.filter((e) => matches(e, filter));
}

export function matches(event: TraceEvent, filter: TraceFilter): boolean {
  if (filter.nodes.length > 0 && !filter.nodes.includes(event.node_name)) {
    return false;
  }
  if (filter.eventTypes.length > 0 && !filter.eventTypes.includes(event.event_type)) {
    return false;
  }
  if (
    filter.minDurationMs !== null &&
    (event.duration_ms ?? 0) < filter.minDurationMs
  ) {
    return false;
  }
  return true;
}

/** Convert a TraceFilter into the URLSearchParams the gateway expects. */
export function toQuery(filter: TraceFilter, sinceMs?: number): string {
  const params = new URLSearchParams();
  for (const n of filter.nodes) params.append("node", n);
  for (const et of filter.eventTypes) params.append("event_type", et);
  if (filter.minDurationMs !== null) {
    params.set("min_duration_ms", String(filter.minDurationMs));
  }
  if (sinceMs !== undefined) {
    params.set("since_ms", String(sinceMs));
  }
  return params.toString();
}
