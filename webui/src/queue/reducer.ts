// Reducer over question-queue WS frames (ADR 0010 Slice C). State is a map
// keyed by item id. A `queue.snapshot` replaces the whole map (the frame the
// gateway sends on WS connect / from GET /api/queue); a `queue.delta` upserts
// the single item it carries. Mirrors `alerts/reducer.ts`, but the queue is a
// monotonic frontier — items move between columns and curate is terminal, so
// nothing is ever deleted from state.

import type { QueueDeltaFrame, QueueFrame, QueueItem, QueueSnapshotFrame } from "../ws";

export type QueueState = Record<string, QueueItem>;

export function emptyQueue(): QueueState {
  return {};
}

function fromSnapshot(frame: QueueSnapshotFrame): QueueState {
  const next: QueueState = {};
  for (const item of frame.items) next[item.id] = item;
  return next;
}

function applyDelta(state: QueueState, frame: QueueDeltaFrame): QueueState {
  return { ...state, [frame.item.id]: frame.item };
}

export function applyQueueFrame(state: QueueState, frame: QueueFrame): QueueState {
  if (frame.type === "queue.snapshot") return fromSnapshot(frame);
  if (frame.type === "queue.delta") return applyDelta(state, frame);
  return state;
}

export function listQueue(state: QueueState): QueueItem[] {
  // Oldest-first by creation (stable tiebreak on id), matching the Python
  // `QueueManager.all()` ordering so columns paint in a deterministic order.
  return Object.values(state).sort(
    (a, b) => a.created_at_ms - b.created_at_ms || a.id.localeCompare(b.id),
  );
}
