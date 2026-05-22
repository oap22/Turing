// Tiny reducer over alert WS frames. State is a map keyed by
// `${node_id}::${field}` so the same peer can flare on multiple fields
// without one alert clobbering another. `cleared` drops the key.

import type { Alert, AlertFrame } from "./types";

export type AlertsState = Record<string, Alert>;

export function emptyAlerts(): AlertsState {
  return {};
}

function keyOf(a: { node_id: string; field: string }): string {
  return `${a.node_id}::${a.field}`;
}

export function applyAlert(state: AlertsState, frame: AlertFrame): AlertsState {
  const k = keyOf(frame);
  if (frame.state === "cleared") {
    if (!(k in state)) return state;
    const next = { ...state };
    delete next[k];
    return next;
  }
  // `alerting` (and, in later slices, `update`): replace the row.
  const { type: _type, ...alert } = frame;
  return { ...state, [k]: alert as Alert };
}

export function listAlerts(state: AlertsState): Alert[] {
  return Object.values(state);
}
