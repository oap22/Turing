// The home page: the first thing the app shows, and the place you land again
// when you're done with a workstation.
//
// It replaces the old startup `SessionPicker`, which only appeared when there
// happened to be more than one saved session and could do nothing but pick one.
// That made saved layouts a thing you had to already know about: with a single
// session the shell booted straight through and there was no surface anywhere
// that listed what you had. Home is that surface — it always renders first, it
// lists every workstation with enough detail to tell them apart, and it owns
// the full lifecycle (open, new, rename, remove) so tearing down a finished
// workstation doesn't mean entering it first just to delete it from ⌘p.
//
// "Workstation" is the user-facing word for what `sessions.ts` calls a Session:
// a named, saved pane layout across all five workspaces. Restoring one re-opens
// the same *shape* with fresh shells — no PTYs, scrollback or running processes
// come back — and the footer says so, because the row's "4 panes" would
// otherwise read as a promise the shell can't keep.
//
// Rendered instead of the shell rather than over it: mounting the pane tree
// underneath would spawn PTYs for a layout the user is about to replace.
// Keyboard conventions follow `Launcher.tsx` — same ArrowUp/ArrowDown/Enter/
// Escape handling, same name-prompt and confirm-before-destroy sub-modes.

import { useEffect, useMemo, useRef, useState } from "react";
import type { Node } from "./layout";
import type { Session } from "./sessions";
import { THEMES } from "./theme";

interface Props {
  sessions: ReadonlyArray<Session>;
  activeId: string | null;
  // Open a saved workstation.
  onOpen: (id: string) => void;
  // Create a fresh workstation (first-launch preset layout) and open it.
  onCreate: (name: string) => void;
  onRename: (id: string, name: string) => void;
  onRemove: (id: string) => void;
  // Escape: resume the last-used workstation without touching the list. Home
  // is a stop, not a toll booth.
  onResume: () => void;
  // Theme lives up in the shell (its top bar has the same picker), so Home
  // takes it as a prop rather than holding a second copy that would drift.
  // It belongs on the startup screen too: this is where you set the mood for
  // the session you're about to start, and the shell's own picker is behind
  // whichever workstation you happen to open.
  theme: string;
  onThemeChange: (id: string) => void;
  now?: number;
}

// figlet "standard", the same wordmark every terminal tool has printed on
// startup since forever. Written as an array rather than a template literal so
// no editor or formatter can eat the leading whitespace that shapes the
// letters. Rendered in the accent colour, so it re-skins with the theme.
const WORDMARK: ReadonlyArray<string> = [
  " _____ _   _ ____  ___ _   _  ____ ",
  "|_   _| | | |  _ \\|_ _| \\ | |/ ___|",
  "  | | | | | | |_) || ||  \\| | |  _ ",
  "  | | | |_| |  _ < | || |\\  | |_| |",
  "  |_|  \\___/|_| \\_\\___|_| \\_|\\____|",
];

type Mode =
  | { kind: "list" }
  | { kind: "name"; command: "new" | "rename"; id: string | null; value: string }
  | { kind: "confirmRemove"; id: string };

function walkLeaves(root: Node | null, visit: (leaf: Extract<Node, { kind: "leaf" }>) => void) {
  const stack: Node[] = root ? [root] : [];
  while (stack.length > 0) {
    const node = stack.pop()!;
    if (node.kind === "leaf") visit(node);
    else stack.push(node.a, node.b);
  }
}

export function paneCount(session: Session): number {
  let n = 0;
  for (const ws of session.layout.workspaces) walkLeaves(ws.root, () => (n += 1));
  return n;
}

// Which of the five workspaces have anything in them, as a fixed-width row of
// filled/empty marks. Fixed width on purpose: the marks line up column-wise
// down the list, so "this one has stuff on ⌘2" is readable at a glance.
export function workspaceMarks(session: Session): string {
  return session.layout.workspaces.map((ws) => (ws.root ? "●" : "·")).join("");
}

// What's actually in it, most-used pane type first: "3 term · metrics · agents".
// Counts are only shown above one, so the common single-of-each case stays
// quiet.
export function paneSummary(session: Session): string {
  const counts = new Map<string, number>();
  for (const ws of session.layout.workspaces) {
    walkLeaves(ws.root, (leaf) => counts.set(leaf.pane, (counts.get(leaf.pane) ?? 0) + 1));
  }
  if (counts.size === 0) return "empty";
  return [...counts.entries()]
    .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
    .map(([pane, n]) => (n > 1 ? `${n} ${pane}` : pane))
    .join(" · ");
}

// Coarse on purpose — the list is sorted by recency, so the exact minute never
// matters, only "today-ish vs a while ago".
export function relativeTime(then: number, now: number): string {
  const secs = Math.max(0, Math.round((now - then) / 1000));
  if (secs < 60) return "just now";
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  return `${days}d ago`;
}

export default function Home({
  sessions,
  activeId,
  onOpen,
  onCreate,
  onRename,
  onRemove,
  onResume,
  theme,
  onThemeChange,
  now,
}: Props) {
  // Start on the last-used workstation so a bare Return resumes it, same as
  // Escape. Recomputed only on mount; the list is stable while Home is up
  // except for removals, which the clamp below handles.
  const [cursor, setCursor] = useState(() => {
    const i = sessions.findIndex((s) => s.id === activeId);
    return i >= 0 ? i : 0;
  });
  const [mode, setMode] = useState<Mode>({ kind: "list" });
  const boxRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const listRef = useRef<HTMLUListElement | null>(null);
  const stamp = useMemo(() => now ?? Date.now(), [now]);

  // Removing the last row would otherwise leave the cursor pointing past the
  // end, and the next Return would do nothing.
  const clamped = Math.min(cursor, Math.max(0, sessions.length - 1));

  // In list mode the dialog itself holds focus (there is no input to hold it),
  // so tabIndex -1 plus this. In name mode the input takes over.
  useEffect(() => {
    if (mode.kind === "name") inputRef.current?.focus();
    else boxRef.current?.focus();
  }, [mode.kind]);

  useEffect(() => {
    listRef.current
      ?.querySelector<HTMLElement>(`[data-index="${clamped}"]`)
      ?.scrollIntoView({ block: "nearest" });
  }, [clamped]);

  const selected = sessions[clamped] ?? null;

  function startNew() {
    setMode({ kind: "name", command: "new", id: null, value: "" });
  }

  function startRename(session: Session) {
    // Seeded with the current name so a small edit is a small amount of typing.
    setMode({ kind: "name", command: "rename", id: session.id, value: session.name });
  }

  function commitName() {
    if (mode.kind !== "name") return;
    const name = mode.value.trim();
    if (name === "") return;
    if (mode.command === "new") onCreate(name);
    else if (mode.id) {
      onRename(mode.id, name);
      setMode({ kind: "list" });
    }
  }

  function onKeyDown(e: React.KeyboardEvent) {
    // Never swallow the shell's own ⌘-chords (theme select, ⌘q, …).
    if (e.metaKey || e.ctrlKey || e.altKey) return;

    if (e.key === "Escape") {
      e.preventDefault();
      // Back out of a sub-mode first; only the plain list resumes.
      if (mode.kind === "list") onResume();
      else setMode({ kind: "list" });
      return;
    }
    if (mode.kind === "name") {
      if (e.key === "Enter") {
        e.preventDefault();
        commitName();
      }
      return;
    }
    if (mode.kind === "confirmRemove") {
      if (e.key === "Enter") {
        e.preventDefault();
        onRemove(mode.id);
        setMode({ kind: "list" });
      }
      return;
    }

    if (e.key === "ArrowDown" || e.key === "j") {
      e.preventDefault();
      setCursor(Math.min(sessions.length - 1, clamped + 1));
      return;
    }
    if (e.key === "ArrowUp" || e.key === "k") {
      e.preventDefault();
      setCursor(Math.max(0, clamped - 1));
      return;
    }
    if (e.key === "n") {
      e.preventDefault();
      startNew();
      return;
    }
    if (e.key === "r") {
      e.preventDefault();
      if (selected) startRename(selected);
      return;
    }
    if (e.key === "d" || e.key === "Backspace" || e.key === "Delete") {
      e.preventDefault();
      if (selected) setMode({ kind: "confirmRemove", id: selected.id });
      return;
    }
    if (e.key === "Enter") {
      e.preventDefault();
      // With nothing saved yet, Return is the same as `n` — an empty home page
      // whose only key did nothing would be a dead end.
      if (selected) onOpen(selected.id);
      else startNew();
    }
  }

  const removing = mode.kind === "confirmRemove" ? mode.id : null;

  return (
    <div
      ref={boxRef}
      role="dialog"
      aria-label="Workstations"
      tabIndex={-1}
      onKeyDown={onKeyDown}
      className="relative flex h-full flex-col items-center overflow-auto bg-term-bg text-term-fg focus:outline-none"
    >
      {/* Corner chrome, deliberately quiet: the eye should land on the
          wordmark, not here. The theme picker is a real control though — this
          is the screen where a session's look gets chosen. */}
      <div className="flex w-full shrink-0 items-center gap-3 px-3 py-2 text-[10px] text-term-dim">
        <span className="uppercase tracking-widest">turing desktop</span>
        <select
          value={theme}
          onChange={(e) => onThemeChange(e.target.value)}
          aria-label="theme"
          className="ml-auto border border-term-edge bg-term-bg px-1 text-[10px] text-term-dim"
        >
          {THEMES.map((t) => (
            <option key={t.id} value={t.id}>
              {t.label}
            </option>
          ))}
        </select>
      </div>

      <div className="flex w-full flex-1 flex-col items-center justify-center gap-6 px-6 py-6">
        {/* Every token here is a theme variable, so the whole screen re-skins
            with the picker above — nothing is hard-coded to the turing palette.
            `select-none` because a startup banner is decoration, not text you
            ever want to drag-select. */}
        <div className="flex flex-col items-center gap-2 select-none">
          <pre
            aria-label="turing"
            className="overflow-hidden text-[10px] leading-[1.15] text-term-accent sm:text-xs"
          >
            {WORDMARK.join("\n")}
          </pre>
          <div className="text-[10px] tracking-[0.3em] text-term-dim uppercase">
            operator surface
          </div>
        </div>

        <div className="w-full max-w-3xl border border-term-edge bg-term-panel">
          <div className="flex items-baseline gap-3 border-b border-term-edge px-3 py-2 text-[10px] uppercase tracking-widest text-term-dim">
            <span>workstations</span>
            <span className="ml-auto normal-case tracking-normal">
              {sessions.length} saved
            </span>
          </div>

          {mode.kind === "name" && (
            <div className="border-b border-term-edge px-3 py-2">
              <input
                ref={inputRef}
                value={mode.value}
                onChange={(e) => setMode({ ...mode, value: e.target.value })}
                onKeyDown={onKeyDown}
                placeholder={
                  mode.command === "new" ? "name for the new workstation…" : "new name…"
                }
                aria-label={mode.command === "new" ? "new workstation name" : "new name"}
                className="w-full border border-term-edge bg-term-bg px-2 py-1 text-sm text-term-fg placeholder:text-term-dim focus:outline-none"
              />
              <div className="mt-1 text-[10px] text-term-dim">
                {mode.command === "new"
                  ? "opens a fresh workstation with the default panes · return create · esc back"
                  : "return save · esc back"}
              </div>
            </div>
          )}

          <ul ref={listRef} className="max-h-[45vh] overflow-auto">
            {sessions.map((session, i) => {
              const cursored = i === clamped;
              return (
                <li key={session.id} data-index={i}>
                  <div
                    className={`flex items-center gap-3 border-b border-term-edge/40 px-3 py-2 text-xs last:border-b-0 ${
                      cursored ? "bg-term-raised text-term-accent" : "text-term-fg"
                    }`}
                  >
                    {/* The cursor is a prompt caret, not a highlight bar alone:
                        the raised row washes out on the lower-contrast themes
                        (nord, everforest), and this reads on all nine. */}
                    <span className="w-3 shrink-0 text-term-accent">{cursored ? "❯" : ""}</span>
                    {/* A row awaiting confirmation stops being an open target:
                        leaving the button live would mean the click that says
                        "are you sure?" is one stray Return away from opening the
                        thing you asked to delete. */}
                    {removing === session.id ? (
                      <div className="flex min-w-0 flex-1 items-baseline gap-3">
                        <span className="min-w-0 max-w-[12rem] flex-1 truncate">
                          {session.name}
                        </span>
                        <span className="flex-1 text-[10px] text-term-fg">
                          remove? <span className="text-term-dim">return yes · esc no</span>
                        </span>
                      </div>
                    ) : (
                      <>
                        <button
                          type="button"
                          onClick={() => onOpen(session.id)}
                          onMouseEnter={() => setCursor(i)}
                          className="flex min-w-0 flex-1 items-baseline gap-3 text-left"
                        >
                          <span className="min-w-0 max-w-[12rem] flex-1 truncate">
                            {session.name}
                          </span>
                          {session.id === activeId && (
                            <span className="shrink-0 border border-term-edge px-1 text-[9px] uppercase tracking-wider text-term-dim">
                              last
                            </span>
                          )}
                          <span className="min-w-0 flex-1 truncate text-[10px] text-term-dim">
                            {paneSummary(session)}
                          </span>
                          <span className="shrink-0 font-mono text-[10px] tracking-widest text-term-dim">
                            {workspaceMarks(session)}
                          </span>
                          <span className="w-14 shrink-0 text-right text-[10px] text-term-dim">
                            {paneCount(session)} {paneCount(session) === 1 ? "pane" : "panes"}
                          </span>
                          <span className="w-14 shrink-0 text-right text-[10px] text-term-dim">
                            {relativeTime(session.updatedAt, stamp)}
                          </span>
                        </button>
                        <button
                          type="button"
                          aria-label={`remove ${session.name}`}
                          onClick={() => setMode({ kind: "confirmRemove", id: session.id })}
                          className="shrink-0 px-1 leading-none text-term-dim hover:text-term-accent"
                        >
                          ×
                        </button>
                      </>
                    )}
                  </div>
                </li>
              );
            })}
            {sessions.length === 0 && (
              <li className="px-3 py-6 text-center text-xs text-term-dim">
                no workstations yet — press n (or return) to make one
              </li>
            )}
          </ul>

          <div className="border-t border-term-edge px-3 py-2 text-[10px] text-term-dim">
            ↑↓ choose · return open · n new · r rename · d remove · esc last used
          </div>
        </div>

        <div className="text-center text-[10px] text-term-dim">
          workstations reopen the same layout with fresh shells · ⌘0 comes back here
        </div>
      </div>
    </div>
  );
}
