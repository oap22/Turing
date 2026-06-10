// Chat pane (ADR 0010 Slice E) — the secondary, ad-hoc work-direction surface
// alongside the question-queue manager (Slice C). The operator types a
// free-form prompt; the coordinator plans a DAG; planned subtasks stream back
// into the SAME thread; a per-subtask thumb feeds the load-bearing
// `episode_rewards` signal through the gateway's REUSED Slice C emitter.
//
// Laid out master-detail (issue #358): a thread list on the left, the selected
// thread's subtasks on the right with the prompt box pinned underneath. A null
// selection follows the newest thread, so a fresh submit comes into view on
// its own; clicking a thread pins it.
//
// State is driven by the `chat.snapshot` / `chat.delta` WS frames (see
// `reducer.ts`); the prompt box POSTs to `/api/chat/submit` and the per-subtask
// thumbs POST to the accept/reject/edit endpoints (see `api.ts`). The pane is
// optimistic only insofar as the authoritative state re-arrives as a
// `chat.delta`, so a failed POST self-heals on the next frame — same contract
// as the queue pane.

import { memo, useMemo, useRef, useState, type RefObject } from "react";
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
  return "border-term-edge bg-term-raised";
}

const BTN =
  "border px-2 py-0.5 text-[10px] uppercase tracking-wider disabled:opacity-40";

export default function ChatPane({ sessions }: Props) {
  // null = follow the newest thread; a session id pins the selection.
  const [pinnedId, setPinnedId] = useState<string | null>(null);
  // `sessions` arrives oldest-first (listChat), so the newest is last.
  const newest = sessions.length > 0 ? sessions[sessions.length - 1] : null;
  const pinned = pinnedId !== null ? sessions.find((s) => s.id === pinnedId) : undefined;
  const selected = pinned ?? newest;
  const reversedSessions = useMemo(() => [...sessions].reverse(), [sessions]);
  const promptRef = useRef<HTMLTextAreaElement>(null);

  return (
    <div
      data-testid="chat-pane"
      className="flex h-full bg-term-bg text-xs"
    >
      <aside
        aria-label="Chat thread list"
        className="flex w-64 shrink-0 flex-col border-r border-term-edge"
      >
        <div className="border-b border-term-edge bg-term-panel px-3 py-1.5">
          <span className="text-[11px] font-bold uppercase tracking-widest text-term-fg">
            threads
          </span>
          <span className="ml-2 tabular-nums text-[10px] text-term-dim">
            {sessions.length}
          </span>
        </div>
        <ol data-testid="chat-thread-list" className="flex-1 overflow-y-auto">
          {sessions.length === 0 && (
            <li className="p-2 text-term-dim">no threads yet</li>
          )}
          {reversedSessions.map((session) => (
            <ThreadListItem
              key={session.id}
              session={session}
              active={session.id === selected?.id}
              onSelect={() => setPinnedId(session.id)}
            />
          ))}
        </ol>
        <button
          type="button"
          data-testid="chat-new-thread"
          onClick={() => {
            setPinnedId(null);
            promptRef.current?.focus();
          }}
          className={`${BTN} m-2 border-term-edge text-term-dim hover:border-term-accent hover:text-term-accent`}
        >
          + new
        </button>
      </aside>
      <section className="flex min-w-0 flex-1 flex-col">
        <div className="border-b border-term-edge bg-term-panel px-3 py-1.5">
          <span className="text-[11px] font-bold uppercase tracking-widest text-term-fg">
            chat
          </span>
          <span className="ml-2 text-[10px] uppercase tracking-wider text-term-dim">
            ad-hoc task submission
          </span>
        </div>
        <div
          aria-label="Chat threads"
          className="flex-1 overflow-y-auto p-2"
          data-testid="chat-thread"
        >
          {!selected && (
            <p className="px-1 text-term-dim">
              no chats yet — submit a prompt below
            </p>
          )}
          {selected && <ChatThread session={selected} />}
        </div>
        <PromptBox onSubmitted={() => setPinnedId(null)} promptRef={promptRef} />
      </section>
    </div>
  );
}

const ThreadListItem = memo(function ThreadListItem({
  session,
  active,
  onSelect,
}: {
  session: ChatSession;
  active: boolean;
  onSelect: () => void;
}) {
  const streaming = session.subtasks.some((s) => s.status === "streaming");
  // Completed-but-unrewarded subtasks await the operator's thumb — the same
  // "you owe a decision" semantics as the App tab badge.
  const awaiting = session.subtasks.filter(
    (s) => s.status === "completed",
  ).length;
  return (
    <li>
      <button
        type="button"
        data-testid={`chat-thread-item-${session.id}`}
        data-active={active ? "true" : "false"}
        aria-pressed={active}
        onClick={onSelect}
        className={`block w-full border-b border-term-edge px-2 py-1.5 text-left ${
          active
            ? "bg-term-raised text-term-fg"
            : "text-term-dim hover:bg-term-panel hover:text-term-fg"
        }`}
      >
        <span className="line-clamp-2 break-words">{session.prompt}</span>
        <span className="mt-0.5 flex items-center gap-2 font-mono text-[10px]">
          <span className="text-term-accent">{session.specialty}</span>
          {streaming && <span className="term-cursor" aria-label="streaming" />}
          {awaiting > 0 && (
            <span
              data-testid={`chat-thread-awaiting-${session.id}`}
              aria-label={`${awaiting} subtasks awaiting a decision`}
              className="bg-term-panel px-1 tabular-nums text-amber-300"
            >
              {awaiting}
            </span>
          )}
        </span>
      </button>
    </li>
  );
});

function PromptBox({
  onSubmitted,
  promptRef,
}: {
  onSubmitted: () => void;
  promptRef: RefObject<HTMLTextAreaElement | null>;
}) {
  const [prompt, setPrompt] = useState("");
  const [busy, setBusy] = useState(false);

  async function send(): Promise<void> {
    const text = prompt.trim();
    if (text === "" || busy) return;
    setBusy(true);
    try {
      await submitChat(text);
      // The submitted thread re-arrives as a `chat.delta`; clear the box and
      // unpin so the new thread comes into view.
      setPrompt("");
      onSubmitted();
    } finally {
      setBusy(false);
    }
  }

  return (
    <form
      data-testid="chat-prompt-form"
      aria-label="Submit chat prompt"
      className="flex items-end gap-1 border-t border-term-edge bg-term-panel p-2"
      onSubmit={(e) => {
        e.preventDefault();
        void send();
      }}
    >
      <label className="sr-only" htmlFor="chat-prompt-input">
        Ad-hoc task prompt
      </label>
      <textarea
        ref={promptRef}
        id="chat-prompt-input"
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
        placeholder="> ask for an ad-hoc task… (starts a new thread)"
        className="min-w-0 flex-1 resize-none border border-term-edge bg-term-bg p-1 text-[11px] text-term-fg placeholder:text-term-dim focus:border-term-accent focus:outline-none"
      />
      <button
        type="submit"
        data-testid="chat-submit"
        disabled={busy || prompt.trim() === ""}
        aria-label="Submit chat prompt"
        className={`${BTN} border-term-accent py-1 text-term-accent hover:bg-cyan-500/10`}
      >
        send
      </button>
    </form>
  );
}

const ChatThread = memo(function ChatThread({ session }: { session: ChatSession }) {
  const headingId = `chat-session-heading-${session.id}`;
  return (
    <section
      data-testid={`chat-session-${session.id}`}
      data-subtask-count={session.subtasks.length}
      aria-labelledby={headingId}
      className="border border-term-edge bg-term-panel"
    >
      <header className="border-b border-term-edge px-2 py-1.5 text-sm leading-snug text-term-fg">
        <span id={headingId} className="break-words">
          {session.prompt}
        </span>
        <span className="ml-2 font-mono text-[10px] text-term-accent">
          {session.specialty}
        </span>
      </header>
      <ol className="space-y-1 p-1">
        {session.subtasks.length === 0 && (
          <li className="term-cursor px-1 text-[10px] text-term-dim">
            planning
          </li>
        )}
        {session.subtasks.map((subtask) => (
          <SubtaskRow key={subtask.id} subtask={subtask} />
        ))}
      </ol>
    </section>
  );
});

const SubtaskRow = memo(function SubtaskRow({ subtask }: { subtask: ChatSubtask }) {
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
    : "border-term-edge bg-term-bg";

  return (
    <li
      data-testid={`chat-subtask-${subtask.id}`}
      data-status={subtask.status}
      data-decision={subtask.decision ?? ""}
      aria-busy={busy ? "true" : "false"}
      className={`border p-2 ${cardClass}`}
    >
      <div className="mb-0.5 flex flex-wrap items-center gap-x-2 font-mono text-[10px] text-term-dim">
        <span className="border border-term-edge bg-term-panel px-1 text-term-accent">
          {subtask.specialty}
        </span>
        <span
          data-testid={`chat-subtask-state-${subtask.id}`}
          className={subtask.status === "streaming" ? "term-cursor" : undefined}
        >
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
        <div className="whitespace-pre-wrap break-words text-sm leading-snug text-term-fg">
          {subtask.content}
        </div>
      )}

      {subtask.status === "completed" && !editing && (
        <div className="mt-2 flex flex-wrap gap-1">
          <button
            type="button"
            data-testid={`chat-accept-${subtask.id}`}
            disabled={busy}
            aria-label={`Accept completed subtask ${subtask.index + 1}`}
            onClick={() =>
              void run(() => acceptSubtask(subtask.session_id, subtask.id))
            }
            className={`${BTN} border-emerald-700 text-emerald-200 hover:bg-emerald-500/10`}
          >
            ✓ accept
          </button>
          <button
            type="button"
            data-testid={`chat-reject-${subtask.id}`}
            disabled={busy}
            aria-label={`Reject completed subtask ${subtask.index + 1}`}
            onClick={() =>
              void run(() => rejectSubtask(subtask.session_id, subtask.id))
            }
            className={`${BTN} border-rose-700 text-rose-200 hover:bg-rose-500/10`}
          >
            ✗ reject
          </button>
          <button
            type="button"
            data-testid={`chat-edit-${subtask.id}`}
            disabled={busy}
            aria-label={`Edit completed subtask ${subtask.index + 1}`}
            onClick={() => {
              setDraft(subtask.corrected_answer ?? subtask.content);
              setEditing(true);
            }}
            className={`${BTN} border-amber-700 text-amber-200 hover:bg-amber-500/10`}
          >
            ✎ edit
          </button>
        </div>
      )}

      {subtask.status === "completed" && editing && (
        <div className="mt-2 space-y-1">
          <label className="sr-only" htmlFor={`chat-edit-input-${subtask.id}`}>
            Corrected subtask answer
          </label>
          <textarea
            id={`chat-edit-input-${subtask.id}`}
            data-testid={`chat-edit-input-${subtask.id}`}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={3}
            placeholder="corrected answer"
            className="w-full border border-term-edge bg-term-bg p-1 text-sm leading-snug text-term-fg placeholder:text-term-dim focus:border-term-accent focus:outline-none"
          />
          <div className="flex gap-1">
            <button
              type="button"
              data-testid={`chat-edit-submit-${subtask.id}`}
              disabled={busy || draft.trim() === ""}
              aria-label={`Save correction for subtask ${subtask.index + 1}`}
              onClick={() =>
                void run(() =>
                  editSubtask(subtask.session_id, subtask.id, draft),
                )
              }
              className={`${BTN} border-amber-700 text-amber-200 hover:bg-amber-500/10`}
            >
              save correction
            </button>
            <button
              type="button"
              data-testid={`chat-edit-cancel-${subtask.id}`}
              disabled={busy}
              aria-label={`Cancel correction for subtask ${subtask.index + 1}`}
              onClick={() => setEditing(false)}
              className={`${BTN} border-term-edge text-term-dim hover:bg-white/5`}
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
            <div className="mt-0.5 whitespace-pre-wrap break-words text-sm normal-case leading-snug text-neutral-300">
              {subtask.corrected_answer}
            </div>
          )}
        </div>
      )}
    </li>
  );
});
