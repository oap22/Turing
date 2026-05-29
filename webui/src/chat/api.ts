// Thin client for the gateway's chat-pane endpoints (ADR 0010 Slice E). The
// bearer cookie set by /token-handoff rides on `credentials: "same-origin"`, so
// no token lives in JS — same pattern as `queue/api.ts`.
//
// `submitChat` returns the opened `ChatSession`; the per-subtask thumb calls
// return the updated `ChatSubtask`. Every call returns null on any failure
// (best-effort: the authoritative state still arrives as a `chat.delta` over
// the WS, so the UI never depends on this response).

import type { ChatSession, ChatSubtask } from "../ws";

async function postJSON<T>(path: string, body: unknown): Promise<T | null> {
  try {
    const res = await fetch(path, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body ?? {}),
    });
    if (!res.ok) return null;
    return (await res.json()) as T;
  } catch {
    return null;
  }
}

// Submit a free-form prompt; the coordinator plans a DAG and streams subtasks
// back into the returned session's thread over the WS.
export function submitChat(
  prompt: string,
  specialty?: string,
): Promise<ChatSession | null> {
  return postJSON<ChatSession>("/api/chat/submit", {
    prompt,
    ...(specialty ? { specialty } : {}),
  });
}

const sub = (sessionId: string, subtaskId: string) =>
  `/api/chat/${encodeURIComponent(sessionId)}/${encodeURIComponent(subtaskId)}`;

export function acceptSubtask(
  sessionId: string,
  subtaskId: string,
): Promise<ChatSubtask | null> {
  return postJSON<ChatSubtask>(`${sub(sessionId, subtaskId)}/accept`, {});
}

export function rejectSubtask(
  sessionId: string,
  subtaskId: string,
): Promise<ChatSubtask | null> {
  return postJSON<ChatSubtask>(`${sub(sessionId, subtaskId)}/reject`, {});
}

// The edit endpoint takes `{ corrected_answer }` (Body(..., embed=True) on the
// FastAPI side); the operator's correction is the new answer of record.
export function editSubtask(
  sessionId: string,
  subtaskId: string,
  correctedAnswer: string,
): Promise<ChatSubtask | null> {
  return postJSON<ChatSubtask>(`${sub(sessionId, subtaskId)}/edit`, {
    corrected_answer: correctedAnswer,
  });
}
