// Reducer over chat-pane WS frames (ADR 0010 Slice E). State is a map keyed by
// session id; each session holds its subtasks in plan order. A `chat.snapshot`
// replaces the whole map (the frame the gateway sends on WS connect / from
// GET /api/chat); a `chat.delta` upserts either a single session (the `submit`
// action) or a single subtask within its session (every other action). Mirrors
// `queue/reducer.ts`, but chat state is two-level (sessions → subtasks).

import type {
  ChatDeltaFrame,
  ChatFrame,
  ChatSession,
  ChatSnapshotFrame,
} from "../ws";

export type ChatState = Record<string, ChatSession>;

export function emptyChat(): ChatState {
  return {};
}

function fromSnapshot(frame: ChatSnapshotFrame): ChatState {
  const next: ChatState = {};
  for (const session of frame.sessions) next[session.id] = session;
  return next;
}

function applyDelta(state: ChatState, frame: ChatDeltaFrame): ChatState {
  if (frame.action === "submit") {
    // A new thread; preserve any subtasks already streamed for it (an out-of-
    // order delta could arrive first), but a fresh submit normally has none.
    const existing = state[frame.session.id];
    const merged: ChatSession = existing
      ? { ...frame.session, subtasks: existing.subtasks }
      : frame.session;
    return { ...state, [frame.session.id]: merged };
  }
  // Subtask-level delta: upsert the single subtask within its session.
  const session = state[frame.session_id];
  const subtasks = session ? [...session.subtasks] : [];
  const idx = subtasks.findIndex((s) => s.id === frame.subtask.id);
  if (idx >= 0) subtasks[idx] = frame.subtask;
  else subtasks.push(frame.subtask);
  const base: ChatSession =
    session ??
    ({
      id: frame.session_id,
      prompt: "",
      specialty: frame.subtask.specialty,
      created_at_ms: frame.subtask.created_at_ms,
      subtasks: [],
    } satisfies ChatSession);
  return { ...state, [frame.session_id]: { ...base, subtasks } };
}

export function applyChatFrame(state: ChatState, frame: ChatFrame): ChatState {
  if (frame.type === "chat.snapshot") return fromSnapshot(frame);
  if (frame.type === "chat.delta") return applyDelta(state, frame);
  return state;
}

export function listChat(state: ChatState): ChatSession[] {
  // Oldest-first by creation (stable tiebreak on id), matching the Python
  // `ChatManager.sessions()` ordering. Subtasks within a session are sorted by
  // their stable plan `index`.
  return Object.values(state)
    .map((session) => ({
      ...session,
      subtasks: [...session.subtasks].sort((a, b) => a.index - b.index),
    }))
    .sort(
      (a, b) => a.created_at_ms - b.created_at_ms || a.id.localeCompare(b.id),
    );
}
