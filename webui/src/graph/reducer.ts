/**
 * Pure reducer over incoming WebSocket frames. Given a current `GraphState`
 * and a frame, returns the next state. Two frame shapes matter here:
 *
 * - `message_trace`: any `event_type` ending in `.start` / `.end` / `.error`,
 *   plus its `node_name`, `seq`, `duration_ms`, and `payload`. Active edges
 *   are added on `.start` and resolved on `.end` / `.error`.
 * - `metric`: rolling latency snapshots used to keep mini-stats fresh between
 *   bursts of trace activity.
 *
 * Pi-node slot assignment is sticky: once a node name has been seen we keep
 * the same slot index for the rest of the page's life so the operator's eye
 * doesn't have to re-find pi-alpha after every refresh.
 */

import type {
  GraphEdgeData,
  GraphNodeData,
  GraphState,
  NodeRole,
} from "./types";

const STALE_WINDOW_MS = 30_000;

export interface MessageTraceFrame {
  type: "message_trace";
  node_name: string;
  event_type: string;
  seq: number;
  timestamp_ms: number;
  duration_ms: number | null;
  error: string | null;
  payload: Record<string, unknown>;
  priority?: boolean;
}

export interface MetricFrame {
  type: "metric";
  node_name: string;
  event_type: string;
  duration_ms: number | null;
  error: string | null;
  timestamp_ms: number;
}

export interface HelloFrame {
  type: "hello";
  node_name: string;
  uptime_s: number;
}

export interface GapMarkerFrame {
  type: "gap_marker";
  node_name: string;
  stream: string;
  /** [first_missing, last_missing] inclusive */
  missing: [number, number];
  timestamp_ms: number;
}

export interface StreamResetFrame {
  type: "stream_reset";
  node_name: string;
  stream: string;
  timestamp_ms: number;
}

export type Frame =
  | MessageTraceFrame
  | MetricFrame
  | HelloFrame
  | GapMarkerFrame
  | StreamResetFrame;

export function emptyState(): GraphState {
  return { nodes: {}, edges: {} };
}

export function reduce(state: GraphState, frame: Frame): GraphState {
  if (frame.type === "hello") {
    return ensurePiNode(state, frame.node_name, Date.now());
  }
  if (frame.type === "message_trace") {
    return reduceTrace(state, frame);
  }
  if (frame.type === "metric") {
    return reduceMetric(state, frame);
  }
  if (frame.type === "gap_marker") {
    return reduceGapMarker(state, frame);
  }
  if (frame.type === "stream_reset") {
    return reduceStreamReset(state, frame);
  }
  return state;
}

function reduceGapMarker(
  state: GraphState,
  frame: GapMarkerFrame,
): GraphState {
  const next = ensurePiNode(state, frame.node_name, frame.timestamp_ms);
  const id = piNodeId(frame.node_name);
  const node = next.nodes[id];
  const dropped = (node.droppedCount ?? 0) + (frame.missing[1] - frame.missing[0] + 1);
  return {
    ...next,
    nodes: {
      ...next.nodes,
      [id]: { ...node, droppedCount: dropped },
    },
  };
}

function reduceStreamReset(
  state: GraphState,
  frame: StreamResetFrame,
): GraphState {
  // A producer restart wipes the dropped count for that node — its prior
  // sequence numbers are no longer "missing", they're just from before.
  const next = ensurePiNode(state, frame.node_name, frame.timestamp_ms);
  const id = piNodeId(frame.node_name);
  const node = next.nodes[id];
  return {
    ...next,
    nodes: {
      ...next.nodes,
      [id]: { ...node, droppedCount: 0 },
    },
  };
}

/** Mark every Pi-node with no recent activity as stale. */
export function markStale(state: GraphState, nowMs: number): GraphState {
  const nodes: Record<string, GraphNodeData> = { ...state.nodes };
  let mutated = false;
  for (const id of Object.keys(nodes)) {
    const n = nodes[id];
    if (n.role !== "pi") continue;
    const stale =
      n.lastSeenMs !== undefined && nowMs - n.lastSeenMs > STALE_WINDOW_MS;
    if (stale !== !!n.stale) {
      nodes[id] = { ...n, stale };
      mutated = true;
    }
  }
  return mutated ? { ...state, nodes } : state;
}

// ── helpers ─────────────────────────────────────────────────────────

function reduceTrace(
  state: GraphState,
  frame: MessageTraceFrame,
): GraphState {
  let next = ensurePiNode(state, frame.node_name, frame.timestamp_ms);
  const stream = frame.event_type.replace(/\.(start|end|error)$/, "");
  const target = inferTarget(stream, frame.payload);
  if (target) {
    next = ensureRoleNode(next, target);
  }

  const edgeId = `${frame.node_name}::${stream}::${frame.seq}`;
  const edges = { ...next.edges };
  if (frame.event_type.endsWith(".start") && target) {
    const edge: GraphEdgeData = {
      id: edgeId,
      source: piNodeId(frame.node_name),
      target: target.id,
      active: true,
      stream,
      seq: frame.seq,
      startMs: frame.timestamp_ms,
    };
    edges[edgeId] = edge;
  } else {
    const existing = edges[startEdgeId(frame.node_name, stream, frame.seq)];
    if (existing) {
      edges[existing.id] = {
        ...existing,
        active: false,
        endMs: frame.timestamp_ms,
        latencyMs: frame.duration_ms ?? undefined,
      };
    }
  }
  return { ...next, edges };
}

function reduceMetric(state: GraphState, frame: MetricFrame): GraphState {
  let next = ensurePiNode(state, frame.node_name, frame.timestamp_ms);
  const stream = frame.event_type.replace(/\.(start|end|error)$/, "");
  if (stream.startsWith("llm.")) {
    const llmId = `llm::cloud`; // refined when the trace payload arrives
    const node = next.nodes[llmId];
    if (node) {
      const prev = node.rollingAvgMs ?? 0;
      const sample = frame.duration_ms ?? prev;
      const blended = prev === 0 ? sample : prev * 0.8 + sample * 0.2;
      next = {
        ...next,
        nodes: {
          ...next.nodes,
          [llmId]: { ...node, rollingAvgMs: Math.round(blended) },
        },
      };
    }
  }
  return next;
}

function ensurePiNode(
  state: GraphState,
  name: string,
  nowMs: number,
): GraphState {
  const id = piNodeId(name);
  const existing = state.nodes[id];
  const slot = existing?.slot ?? nextSlot(state);
  const node: GraphNodeData = {
    id,
    role: "pi",
    label: name,
    slot,
    inFlight: existing?.inFlight ?? 0,
    currentProvider: existing?.currentProvider,
    rollingAvgMs: existing?.rollingAvgMs,
    droppedCount: existing?.droppedCount,
    lastSeenMs: nowMs,
    stale: false,
  };
  return { ...state, nodes: { ...state.nodes, [id]: node } };
}

function ensureRoleNode(
  state: GraphState,
  spec: { id: string; role: NodeRole; label: string },
): GraphState {
  if (state.nodes[spec.id]) return state;
  const node: GraphNodeData = { id: spec.id, role: spec.role, label: spec.label };
  return { ...state, nodes: { ...state.nodes, [spec.id]: node } };
}

function inferTarget(
  stream: string,
  payload: Record<string, unknown>,
): { id: string; role: NodeRole; label: string } | null {
  if (stream.startsWith("llm.")) {
    const provider = String(payload?.provider ?? "cloud");
    return { id: `llm::${provider}`, role: "llm", label: provider };
  }
  if (stream.startsWith("tool.")) {
    const tool = String(payload?.tool ?? "tool");
    return { id: `tool::${tool}`, role: "tool", label: tool };
  }
  if (stream.startsWith("memory.")) {
    return { id: `memory::store`, role: "memory", label: "memory" };
  }
  return null;
}

function piNodeId(name: string): string {
  return `pi::${name}`;
}

function startEdgeId(node: string, stream: string, seq: number): string {
  return `${node}::${stream}::${seq}`;
}

function nextSlot(state: GraphState): number {
  let max = -1;
  for (const n of Object.values(state.nodes)) {
    if (n.role === "pi" && n.slot !== undefined) {
      max = Math.max(max, n.slot);
    }
  }
  return max + 1;
}
