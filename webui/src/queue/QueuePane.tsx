// Question-queue manager pane (ADR 0010 Slice C) — the primary work-direction
// surface that replaces the retired Discord task bot. Renders the human-gated
// research frontier as five columns:
//
//   proposed → approved → in-flight → drafted → curated
//
// State is driven by the `queue.snapshot` / `queue.delta` WS frames (see
// `reducer.ts`); curation actions POST to the gateway's approve/accept/reject/
// edit endpoints (see `api.ts`), which write the load-bearing `episode_rewards`
// signal. The pane is optimistic only insofar as the authoritative item state
// re-arrives as a `queue.delta`, so a failed POST self-heals on the next frame.

import { memo, useMemo, useState } from "react";
import type { QueueItem, QueueStatus } from "../ws";
import {
  acceptQuestion,
  approveQuestion,
  editQuestion,
  rejectQuestion,
} from "./api";

interface Props {
  items: QueueItem[];
}

// Column order and human labels for the frontier. The keys are the wire
// `QueueStatus` values so a frame's `status` indexes a column directly.
const COLUMNS: ReadonlyArray<{ status: QueueStatus; label: string }> = [
  { status: "proposed", label: "proposed" },
  { status: "approved", label: "approved" },
  { status: "in-flight", label: "in-flight" },
  { status: "drafted", label: "drafted" },
  { status: "curated", label: "curated" },
];

// Per-decision accent on a curated card, echoing the reward sign:
// accept +1.0 (emerald), reject −1.0 (rose), edit +0.3 (amber).
function decisionClass(decision: QueueItem["decision"]): string {
  if (decision === "accept") return "border-emerald-700 bg-emerald-950/40";
  if (decision === "reject") return "border-rose-700 bg-rose-950/40";
  if (decision === "edit") return "border-amber-700 bg-amber-950/40";
  return "border-term-edge bg-term-raised";
}

// Shared button look: sharp, quiet, uppercase — the accent colour carries
// the semantics.
const BTN =
  "border px-2 py-0.5 text-[10px] uppercase tracking-wider disabled:opacity-40";

export default function QueuePane({ items }: Props) {
  const byStatus = useMemo(() => {
    const buckets: Record<QueueStatus, QueueItem[]> = {
      proposed: [],
      approved: [],
      "in-flight": [],
      drafted: [],
      curated: [],
    };
    for (const item of items) buckets[item.status].push(item);
    return buckets;
  }, [items]);

  return (
    <div
      data-testid="queue-pane"
      className="flex h-full flex-col bg-term-bg text-xs"
    >
      <div className="border-b border-term-edge bg-term-panel px-3 py-1.5">
        <span className="text-[11px] font-bold uppercase tracking-widest text-term-fg">
          queue
        </span>
        <span className="ml-2 text-[10px] uppercase tracking-wider text-term-dim">
          human-gated research frontier
        </span>
      </div>
      <div className="grid flex-1 grid-cols-5 gap-px overflow-hidden bg-term-edge">
        {COLUMNS.map((col) => (
          <Column
            key={col.status}
            status={col.status}
            label={col.label}
            items={byStatus[col.status]}
          />
        ))}
      </div>
    </div>
  );
}

function Column({
  status,
  label,
  items,
}: {
  status: QueueStatus;
  label: string;
  items: QueueItem[];
}) {
  const headingId = `queue-column-heading-${status}`;
  return (
    <section
      data-testid={`queue-column-${status}`}
      data-count={items.length}
      aria-labelledby={headingId}
      className="flex min-w-0 flex-col bg-term-bg"
    >
      <header className="flex items-center justify-between border-b border-term-edge px-2 py-1 text-[10px] uppercase tracking-widest text-term-dim">
        <span id={headingId}>{label}</span>
        <span
          aria-label={`${items.length} ${label} questions`}
          className="tabular-nums text-term-dim"
        >
          {items.length}
        </span>
      </header>
      <ol aria-labelledby={headingId} className="flex-1 space-y-1 overflow-y-auto p-1">
        {items.map((item) => (
          <QueueCard key={item.id} item={item} />
        ))}
      </ol>
    </section>
  );
}

const QueueCard = memo(function QueueCard({ item }: { item: QueueItem }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);

  async function run(action: () => Promise<unknown>): Promise<void> {
    setBusy(true);
    try {
      await action();
    } finally {
      // The authoritative item state re-arrives as a queue.delta; clearing busy
      // is safe regardless of outcome.
      setBusy(false);
      setEditing(false);
    }
  }

  const isCurated = item.status === "curated";
  const cardClass = isCurated
    ? decisionClass(item.decision)
    : "border-term-edge bg-term-raised";

  return (
    <li
      data-testid={`queue-card-${item.id}`}
      data-status={item.status}
      data-decision={item.decision ?? ""}
      aria-busy={busy ? "true" : "false"}
      className={`border p-2 ${cardClass}`}
    >
      <div className="flex items-start justify-between gap-2">
        <span className="break-words text-sm leading-snug text-term-fg">
          {item.prompt}
        </span>
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-0.5 font-mono text-[10px] text-term-dim">
        <span className="border border-term-edge bg-term-panel px-1 text-term-accent">
          {item.specialty}
        </span>
        <span>by {item.proposed_by}</span>
        {item.consumed_upstreams.length > 0 && (
          <span
            data-testid={`queue-upstreams-${item.id}`}
            title={item.consumed_upstreams.join(", ")}
          >
            synthesis · {item.consumed_upstreams.length} upstream
            {item.consumed_upstreams.length === 1 ? "" : "s"}
          </span>
        )}
      </div>

      {item.status === "proposed" && (
        <div className="mt-2">
          <button
            type="button"
            data-testid={`queue-approve-${item.id}`}
            disabled={busy}
            aria-label={`Approve question: ${item.prompt}`}
            onClick={() => void run(() => approveQuestion(item.id))}
            className={`${BTN} border-sky-700 text-sky-200 hover:bg-sky-500/10`}
          >
            approve
          </button>
        </div>
      )}

      {item.status === "drafted" && !editing && (
        <div className="mt-2 flex flex-wrap gap-1">
          <button
            type="button"
            data-testid={`queue-accept-${item.id}`}
            disabled={busy}
            aria-label={`Accept drafted answer for: ${item.prompt}`}
            onClick={() => void run(() => acceptQuestion(item.id))}
            className={`${BTN} border-emerald-700 text-emerald-200 hover:bg-emerald-500/10`}
          >
            accept
          </button>
          <button
            type="button"
            data-testid={`queue-reject-${item.id}`}
            disabled={busy}
            aria-label={`Reject drafted answer for: ${item.prompt}`}
            onClick={() => void run(() => rejectQuestion(item.id))}
            className={`${BTN} border-rose-700 text-rose-200 hover:bg-rose-500/10`}
          >
            reject
          </button>
          <button
            type="button"
            data-testid={`queue-edit-${item.id}`}
            disabled={busy}
            aria-label={`Edit drafted answer for: ${item.prompt}`}
            onClick={() => {
              setDraft(item.corrected_answer ?? "");
              setEditing(true);
            }}
            className={`${BTN} border-amber-700 text-amber-200 hover:bg-amber-500/10`}
          >
            edit
          </button>
        </div>
      )}

      {item.status === "drafted" && editing && (
        <div className="mt-2 space-y-1">
          <label className="sr-only" htmlFor={`queue-edit-input-${item.id}`}>
            Corrected answer
          </label>
          <textarea
            id={`queue-edit-input-${item.id}`}
            data-testid={`queue-edit-input-${item.id}`}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            rows={3}
            placeholder="corrected answer"
            className="w-full border border-term-edge bg-term-bg p-1 text-sm leading-snug text-term-fg placeholder:text-term-dim focus:border-term-accent focus:outline-none"
          />
          <div className="flex gap-1">
            <button
              type="button"
              data-testid={`queue-edit-submit-${item.id}`}
              disabled={busy || draft.trim() === ""}
              aria-label={`Save correction for: ${item.prompt}`}
              onClick={() => void run(() => editQuestion(item.id, draft))}
              className={`${BTN} border-amber-700 text-amber-200 hover:bg-amber-500/10`}
            >
              save correction
            </button>
            <button
              type="button"
              data-testid={`queue-edit-cancel-${item.id}`}
              disabled={busy}
              aria-label={`Cancel correction for: ${item.prompt}`}
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
          data-testid={`queue-decision-${item.id}`}
          className="mt-2 font-mono text-[10px] uppercase tracking-wide text-neutral-400"
        >
          {item.decision}
          {item.decision === "edit" && item.corrected_answer && (
            <div className="mt-0.5 whitespace-pre-wrap break-words text-sm normal-case leading-snug text-neutral-300">
              {item.corrected_answer}
            </div>
          )}
        </div>
      )}
    </li>
  );
});
