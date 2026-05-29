/**
 * Connects to the gateway's authenticated WebSocket and reconnects with
 * exponential backoff. The browser presents the cookie set by
 * /token-handoff, so no token has to live in JS.
 */

// ── Question-queue frames (ADR 0010 Slice C) ────────────────────────────────
//
// The webui queue pane's wire contract with `turing-gateway`. Mirrors the
// Python `QueueManager.snapshot_frame()` / `_emit_delta()` output and the
// `QueueItem.to_frame()` shape exactly. The gateway sends one `queue.snapshot`
// on WS connect, then a `queue.delta` per mutation; the pane replaces state on
// a snapshot and upserts the single item on a delta.

export type QueueStatus =
  | "proposed"
  | "approved"
  | "in-flight"
  | "drafted"
  | "curated";

export type QueueDecision = "accept" | "reject" | "edit";

// The transition that produced a delta. `add` is the initial insert; the
// curation actions double as the decision name.
export type QueueAction =
  | "add"
  | "approve"
  | "dispatch"
  | "draft"
  | QueueDecision;

export interface QueueItem {
  id: string;
  prompt: string;
  specialty: string;
  status: QueueStatus;
  origin_task_id: string | null;
  origin_question_id: string | null;
  proposed_by: string;
  episode_id: string | null;
  consumed_upstreams: string[];
  created_at_ms: number;
  approved_at_ms: number | null;
  dispatched_at_ms: number | null;
  drafted_at_ms: number | null;
  curated_at_ms: number | null;
  decision: QueueDecision | null;
  corrected_answer: string | null;
}

export interface QueueSnapshotFrame {
  type: "queue.snapshot";
  items: QueueItem[];
  timestamp_ms: number;
}

export interface QueueDeltaFrame {
  type: "queue.delta";
  action: QueueAction;
  item: QueueItem;
  timestamp_ms: number;
}

export type QueueFrame = QueueSnapshotFrame | QueueDeltaFrame;

interface ConnectOptions<T> {
  url: string;
  onFrame: (frame: T) => void;
  onStatus?: (status: "open" | "closed" | "reconnecting") => void;
}

const MIN_DELAY_MS = 250;
const MAX_DELAY_MS = 15_000;

export function connectGatewayWS<T = unknown>(opts: ConnectOptions<T>): () => void {
  let ws: WebSocket | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  let backoff = MIN_DELAY_MS;
  let stopped = false;

  function open() {
    if (stopped) return;
    ws = new WebSocket(opts.url);
    ws.onopen = () => {
      backoff = MIN_DELAY_MS;
      opts.onStatus?.("open");
    };
    ws.onmessage = (event) => {
      try {
        const frame = JSON.parse(event.data) as T;
        opts.onFrame(frame);
      } catch {
        // ignore malformed frames; the gateway only emits JSON
      }
    };
    ws.onclose = () => {
      opts.onStatus?.("closed");
      if (stopped) return;
      opts.onStatus?.("reconnecting");
      timer = setTimeout(open, backoff);
      backoff = Math.min(MAX_DELAY_MS, backoff * 2);
    };
    ws.onerror = () => {
      ws?.close();
    };
  }

  open();

  return () => {
    stopped = true;
    if (timer) clearTimeout(timer);
    ws?.close();
  };
}
