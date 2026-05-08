/** Shared types for the message-trace pane. */

export interface TraceEvent {
  timestamp_ms: number;
  node_name: string;
  event_type: string;
  seq?: number;
  duration_ms: number | null;
  error?: string | null;
  payload: Record<string, unknown>;
}

export interface TraceFilter {
  /** Multi-select; empty = all nodes. */
  nodes: string[];
  /** Multi-select; empty = all event types. */
  eventTypes: string[];
  /** ms; null = no min duration filter. */
  minDurationMs: number | null;
}

export const EMPTY_FILTER: TraceFilter = {
  nodes: [],
  eventTypes: [],
  minDurationMs: null,
};
