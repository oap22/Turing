// Thin client for the gateway's question-queue curation endpoints
// (ADR 0010 Slice C). The bearer cookie set by /token-handoff rides on
// `credentials: "same-origin"`, so no token lives in JS — same pattern as the
// alert-snooze POST in `alerts/AlertBanner.tsx`.
//
// Each call returns the updated item's frame on success, or null on any
// failure (best-effort: the authoritative state still arrives as a
// `queue.delta` over the WS, so the UI never depends on this response).

import { gatewayFetch } from "../desktop/gateway";
import type { QueueItem } from "../ws";

async function postCuration(path: string, body: unknown): Promise<QueueItem | null> {
  try {
    const res = await gatewayFetch(path, {
      method: "POST",
      body: JSON.stringify(body ?? {}),
    });
    if (!res.ok) return null;
    return (await res.json()) as QueueItem;
  } catch {
    return null;
  }
}

export function approveQuestion(id: string): Promise<QueueItem | null> {
  return postCuration(`/api/queue/approve/${encodeURIComponent(id)}`, {});
}

export function acceptQuestion(id: string): Promise<QueueItem | null> {
  return postCuration(`/api/queue/accept/${encodeURIComponent(id)}`, {});
}

export function rejectQuestion(id: string): Promise<QueueItem | null> {
  return postCuration(`/api/queue/reject/${encodeURIComponent(id)}`, {});
}

// The edit endpoint takes `{ corrected_answer }` (Body(..., embed=True) on the
// FastAPI side); the operator's correction is the new answer of record.
export function editQuestion(
  id: string,
  correctedAnswer: string,
): Promise<QueueItem | null> {
  return postCuration(`/api/queue/edit/${encodeURIComponent(id)}`, {
    corrected_answer: correctedAnswer,
  });
}
