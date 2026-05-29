// Chat pane (ADR 0010 Slice E) — the secondary, ad-hoc work-direction surface
// alongside the question-queue manager (Slice C). The operator types a
// free-form prompt; the coordinator plans a DAG; planned subtasks stream back
// into the SAME thread; a per-subtask thumb feeds the load-bearing
// `episode_rewards` signal through the gateway's REUSED Slice C emitter.
//
// State is driven by the `chat.snapshot` / `chat.delta` WS frames (see
// `reducer.ts`); the prompt box POSTs to `/api/chat/submit` and the per-subtask
// thumbs POST to the accept/reject/edit endpoints (see `api.ts`). The pane is
// optimistic only insofar as the authoritative state re-arrives as a
// `chat.delta`, so a failed POST self-heals on the next frame — same contract
// as the queue pane.

import { useState } from "react";
import type { ChatSession, ChatSubtask } from "../ws";
import {
  acceptSubtask,
  editSubtask,
  rejectSubtask,
  submitChat,
} from "./api";

interface Props {
  sessions: ChatSession[];
}

// Per-decision accent on a curated subtask, echoing the reward sign — the same
// palette the queue pane uses: accept +1.0 (emerald), reject −1.0 (rose),
// edit +0.3 (amber).
function decisionClass(decision: ChatSubtask["decision"]): string {
  if (decision === "accept") return "border-emerald-700 bg-emerald-950/40";
  if (decision === "reject") return "border-rose-700 bg-rose-950/40";
  if (decision === "edit") return "border-amber-700 bg-amber-950/40";
  return "border-neutral-800 bg-neutral-900";
}

export default function ChatPane({ sessions }: Props) {
  return (
    <div
      data-testid="chat-pane"
      className="flex h-full flex-col bg-neutral-950 text-xs"
    >
      <div className="border-b border-neutral-800 px-3 py-2 text-sm font-semibold text-neutral-200">
        chat
        <span className="ml-2 text-xs font-normal text-neutral-500">
          ad-hoc task submission
        </span>
      </div>
      <div className="flex-1 space-y-3 overflow-y-auto p-2" data-testid="chat-thread">
        {sessions.length === 0 && (
          <p className="px-1 text-neutral-600">
            no chats yet — submit a prompt below
          </p>
        )}
        {sessions.map((session) => (
          <ChatThread key={session.id} session={session} />
        ))}
      </div>
      <PromptBox />
    </div>
  );
}

function PromptBox() {
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);

  async function send(): Promise<void> {
    const text = prompt.trim();
    if (text === "" || busy) return;
    setBusy(true);
    try {
      await submitChat(text);
      // The submitted thread re-arrives as a `chat.delta`; clear the box.
      setPrompt("");
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      data-testid="chat-prompt-form"
      className="flex items-end gap-1 border-t border-neutral-800 p-2"
      onSubmit={(e) => {
        e.preventDefault();
        void send();
      }}
    >
      <textarea
        data-testid="chat-prompt-input"
        value={prompt}
        onChange={(e) => setPrompt(e.target.value)}
        onKeyDown={(e) => {
          // Enter submits; Shift+Enter inserts a newline.
          if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            void send();
          }
        }}
        rows={2}
        placeholder="ask for an ad-hoc task…"
        className="min-w-0 flex-1 resize-none rounded border border-neutral-700 bg-neutral-950 p-1 text-[11px] text-neutral-200"
      />
      <button
        type="submit"
        data-testid="chat-submit"
        disabled={busy || prompt.trim() === ""}
        className="rounded border border-sky-700 px-2 py-1 text-[11px] text-sky-200 hover:bg-sky-500/10 disabled:opacity-40"
      >
        send
      </button>
    </form>
  );
}

function ChatThread({ session }: { session: ChatSession }) {
  return (
    <section
      data-testid={`chat-session-${session.id}`}
      data-subtask-count={session.subtasks.length}
      className="rounded border border-neutral-800 bg-neutral-900/60"
    >
      <header className="border-b border-neutral-800 px-2 py-1 text-neutral-200">
        <span className="break-words">{session.prompt}</span>
        <span className="ml-2 font-mono text-[10px] text-neutral-500">
          {session.specialty}
        </span>
      </header>
      <ol className="space-y-1 p-1">
        {session.subtasks.length === 0 && (
          <li className="px-1 text-[10px] italic text-neutral-600">
            planning…
          </li>
        )}
        {session.subtasks.map((subtask) => (
          <SubtaskRow key={subtask.id} subtask={subtask} />
        ))}
      </ol>
    </section>
  );
}

function SubtaskRow({ subtask }: { subtask: ChatSubtask }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);

  async function run(action: () => Promise<unknown>): Promise<void> {
    setBusy(true);
    try {
      await action();
    } finally {
      // Authoritative subtask state re-arrives as a chat.delta; clearing busy
      // is safe regardless of outcome.
      setBusy(false);
      setEditing(false);
    }
  }

  const isCurated = subtask.status === "curated";
  const cardClass = isCurated
    ? decisionClass(subtask.decision)
    : "border-neutral-800 bg-neutral-950";

  return (
    <li
      data-testid={`chat-subtask-${subtask.id}`}
      data-status={subtask.status}
      data-decision={subtask.decision ?? ""}
      className={`rounded border p-2 ${cardClass}`}
    >
      <div className="mb-0.5 flex flex-wrap items-center gap-x-2 font-mono text-[10px] text-neutral-500">
        <span className="rounded bg-neutral-800 px-1 text-neutral-300">
          {subtask.specialty}
        </span>
        <span data-testid={`chat-subtask-state-${subtask.id}`}>
          {subtask.status}
        </span>
        {subtask.consumed_upstreams.length > 0 && (
          <span
            data-testid={`chat-upstreams-${subtask.id}`}
            title={subtask.consumed_upstreams.join(", ")}
          >
            synthesis · {subtask.consumed_upstreams.length} upstream
            {subtask.consumed_upstreams.length === 1 ? "" : "s"}
          </span>
        )}
      </div>

      {subtask.content && (
        <div className="whitespace-pre-wrap break-words text-neutral-200">
          {subtask.content}
        </div>
      )}

      {subtask.status === "completed" && !editing && (
        <div className="mt-2 flex flex-wrap gap-1">
          <button
            type="button"
            data-testid={`chat-accept-${subtask.id}`}
            disabled={busy}
            onClick={() =>
              void run(() => acceptSubtask(subtask.session_id, subtask.id))
            }
            className="rounded border border-emerald-700 px-2 py-0.5 text-[11px] text-emerald-200 hover:bg-emerald-500/10 disabled:opacity-40"
          >
            👍
          </button>
          <button
            type="button"
            data-testid={`chat-reject-${subtask.id}`}
            disabled={busy}
            onClick={() =>
              void run(() => rejectSubtask(subtask.session_id, subtask.id))
            }
            className="rounded border border-rose-700 px-2 py-0.5 text-[11px] text-rose-200 hover:bg-rose-500/10 disabled:opacity-40"
          >
            👎
          </button>
          <button
            type="button"
            data-testid={`chat-edit-${subtask.id}`}
            disabled={busy}
            onClick={() => {
              setDraft(subtask.corrected_answer ?? subtask.content);
              setEditing(true);
            }}
            className="rounded border border-amber-700 px-2 py-0.5 text-[11px] text-amber-200 hover:bg-amber-500/10 disabled:opacity-40"
          >
            edit
          </button>
        </div>
      )}

      {subtask.status === "completed" && editing && (
        <div className="mt-2 space-y-1">
          <textarea
            data-testid={`chat-edit-input-${subtask.id}`}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={3}
            placeholder="corrected answer"
            className="w-full rounded border border-neutral-700 bg-neutral-950 p-1 text-[11px] text-neutral-200"
          />
          <div className="flex gap-1">
            <button
              type="button"
              data-testid={`chat-edit-submit-${subtask.id}`}
              disabled={busy || draft.trim() === ""}
              onClick={() =>
                void run(() =>
                  editSubtask(subtask.session_id, subtask.id, draft),
                )
              }
              className="rounded border border-amber-700 px-2 py-0.5 text-[11px] text-amber-200 hover:bg-amber-500/10 disabled:opacity-40"
            >
              save correction
            </button>
            <button
              type="button"
              data-testid={`chat-edit-cancel-${subtask.id}`}
              disabled={busy}
              onClick={() => setEditing(false)}
              className="rounded border border-neutral-700 px-2 py-0.5 text-[11px] text-neutral-400 hover:bg-white/5 disabled:opacity-40"
            >
              cancel
            </button>
          </div>
        </div>
      )}

      {isCurated && (
        <div
          data-testid={`chat-decision-${subtask.id}`}
          className="mt-2 font-mono text-[10px] uppercase tracking-wide text-neutral-400"
        >
          {subtask.decision}
          {subtask.decision === "edit" && subtask.corrected_answer && (
            <div className="mt-0.5 whitespace-pre-wrap break-words font-sans normal-case text-neutral-300">
              {subtask.corrected_answer}
            </div>
          )}
        </div>
      )}
    </li>
  );
}
