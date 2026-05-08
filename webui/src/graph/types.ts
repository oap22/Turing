/** Shared graph-state types. */

export type NodeRole = "pi" | "tool" | "memory" | "llm";

export interface GraphNodeData {
  id: string;
  role: NodeRole;
  label: string;
  /** Pi-node only: the slot index that fixes its column position. */
  slot?: number;
  /** Pi-node only: stale when no events have arrived within the window. */
  stale?: boolean;
  /** Pi-node only: in-flight calls. */
  inFlight?: number;
  /** Pi-node only: which LLM provider it's currently routing to, if any. */
  currentProvider?: string;
  /** LLM-provider only: in-flight requests + 60s rolling avg latency. */
  rollingAvgMs?: number;
  /** Last time we saw activity touching this node (epoch ms). */
  lastSeenMs?: number;
}

export interface GraphEdgeData {
  id: string;
  source: string;
  target: string;
  /** Active edges pulse; completed edges briefly show final duration. */
  active: boolean;
  startMs: number;
  /** Set on the matching `.end` event. */
  endMs?: number;
  /** Last latency pulse the edge displays (ms). */
  latencyMs?: number;
  /** Stream this edge belongs to (e.g. `llm.complete`, `tool.dispatch`). */
  stream: string;
  /** Per-stream sequence number from the producing node. */
  seq: number;
}

export interface GraphState {
  nodes: Record<string, GraphNodeData>;
  edges: Record<string, GraphEdgeData>;
}
