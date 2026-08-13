// ⌘P launcher: fuzzy-filtered overlay over panes + runners. Selecting a pane
// opens it in the active workspace; selecting a runner opens a `term` pane
// pre-wired to it (`openPane("term", { runnerId })`).
//
// It is also the command surface for saved sessions (save / rename / switch /
// delete). Those live here rather than on ⌘-chords of their own because the
// keymap only has ⌘ to spend — ⌥/⌃ are reserved for whatever is running inside
// a `term` pane — and four more raw bindings would crowd out bindings that get
// pressed hundreds of times a day. The launcher is already the "do a thing I
// do occasionally" surface, and it is where users go looking.
//
// Save and rename need a name, so the launcher has a second mode: picking one
// of those commands turns the filter input into a name prompt (Escape backs
// out to the list rather than closing the overlay). Delete gets the same
// treatment as a yes/no confirm, because a deleted layout is not recoverable.

import { useEffect, useMemo, useRef, useState } from "react";
import type { PaneType } from "./layout";
import { RUNNERS } from "./runners";

// Just enough of a `Session` to list one — the launcher never needs the stored
// layout itself.
export interface SessionChoice {
  id: string;
  name: string;
}

interface Props {
  onClose: () => void;
  onOpenPane: (pane: PaneType) => void;
  onOpenRunner: (runnerId: string) => void;
  sessions: ReadonlyArray<SessionChoice>;
  activeSession: SessionChoice | null;
  onSaveSession: (name: string) => void;
  onRenameSession: (name: string) => void;
  onSwitchSession: (id: string) => void;
  onDeleteSession: () => void;
}

const PANE_LABELS: ReadonlyArray<{ pane: PaneType; label: string }> = [
  { pane: "term", label: "terminal" },
  { pane: "queue", label: "queue" },
  // Dormant: kept wired but reserved for the future Jetson-nano fleet
  // coordinator, so the label says so rather than promising a chat UI.
  { pane: "chat", label: "chat — future: nano agent network" },
  { pane: "obs", label: "observability" },
  { pane: "metrics", label: "metrics" },
  { pane: "images", label: "images" },
  { pane: "flywheel", label: "flywheel" },
  { pane: "agents", label: "agents" },
  { pane: "agentfeed", label: "agent feed" },
];

type Command = "save" | "rename" | "delete";

type Item =
  | { kind: "pane"; key: string; label: string; pane: PaneType }
  | { kind: "runner"; key: string; label: string; runnerId: string }
  | { kind: "session"; key: string; label: string; sessionId: string }
  | { kind: "command"; key: string; label: string; command: Command };

// Which sub-screen the overlay is on. "list" is the ordinary launcher; the
// other two are short-lived follow-ups to a command that needs an answer.
type Mode =
  | { kind: "list" }
  | { kind: "name"; command: "save" | "rename"; value: string }
  | { kind: "confirmDelete" };

// Simple case-insensitive subsequence match — no fuzzy-match library.
function fuzzyMatch(query: string, target: string): boolean {
  if (query === "") return true;
  const q = query.toLowerCase();
  const t = target.toLowerCase();
  let qi = 0;
  for (let ti = 0; ti < t.length && qi < q.length; ti++) {
    if (t[ti] === q[qi]) qi++;
  }
  return qi === q.length;
}

export default function Launcher({
  onClose,
  onOpenPane,
  onOpenRunner,
  sessions,
  activeSession,
  onSaveSession,
  onRenameSession,
  onSwitchSession,
  onDeleteSession,
}: Props) {
  const [query, setQuery] = useState("");
  const [cursor, setCursor] = useState(0);
  const [mode, setMode] = useState<Mode>({ kind: "list" });
  const inputRef = useRef<HTMLInputElement | null>(null);
  const listRef = useRef<HTMLUListElement | null>(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, [mode.kind]);

  // Keep the highlighted row visible: the list scrolls independently of the
  // cursor, so arrowing past the fold moved the selection out of sight.
  // "nearest" scrolls the minimum needed, so a cursor already on screen does
  // not jerk the list around.
  useEffect(() => {
    listRef.current
      ?.querySelector<HTMLElement>(`[data-index="${cursor}"]`)
      ?.scrollIntoView({ block: "nearest" });
  }, [cursor]);

  const items: Item[] = useMemo(
    () => [
      ...PANE_LABELS.map((p) => ({ kind: "pane" as const, key: `pane:${p.pane}`, label: p.label, pane: p.pane })),
      ...RUNNERS.map((r) => ({ kind: "runner" as const, key: `runner:${r.id}`, label: r.label, runnerId: r.id })),
      ...sessions
        // The session you are already in is not a switch target.
        .filter((s) => s.id !== activeSession?.id)
        .map((s) => ({
          kind: "session" as const,
          key: `session:${s.id}`,
          label: `switch to ${s.name}`,
          sessionId: s.id,
        })),
      { kind: "command" as const, key: "cmd:save", label: "session: save layout as…", command: "save" as const },
      ...(activeSession
        ? [
            {
              kind: "command" as const,
              key: "cmd:rename",
              label: `session: rename ${activeSession.name}…`,
              command: "rename" as const,
            },
            {
              kind: "command" as const,
              key: "cmd:delete",
              label: `session: delete ${activeSession.name}…`,
              command: "delete" as const,
            },
          ]
        : []),
    ],
    [sessions, activeSession],
  );

  const filtered = useMemo(
    () => items.filter((i) => fuzzyMatch(query, i.label)),
    [items, query],
  );

  function select(item: Item) {
    if (item.kind === "pane") {
      onOpenPane(item.pane);
    } else if (item.kind === "runner") {
      onOpenRunner(item.runnerId);
    } else if (item.kind === "session") {
      onSwitchSession(item.sessionId);
    } else if (item.command === "delete") {
      setMode({ kind: "confirmDelete" });
      return;
    } else {
      setMode({
        kind: "name",
        command: item.command,
        // Rename starts from the current name so a small edit is a small
        // amount of typing; save starts empty.
        value: item.command === "rename" ? (activeSession?.name ?? "") : "",
      });
      return;
    }
    onClose();
  }

  function commitName() {
    if (mode.kind !== "name") return;
    const name = mode.value.trim();
    if (name === "") return;
    if (mode.command === "save") onSaveSession(name);
    else onRenameSession(name);
    onClose();
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Escape") {
      e.preventDefault();
      // Back out of a sub-screen first; only the list closes the overlay.
      if (mode.kind === "list") onClose();
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
    if (mode.kind === "confirmDelete") {
      if (e.key === "Enter") {
        e.preventDefault();
        onDeleteSession();
        onClose();
      }
      return;
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setCursor((c) => Math.min(filtered.length - 1, c + 1));
      return;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      setCursor((c) => Math.max(0, c - 1));
      return;
    }
    if (e.key === "Enter") {
      e.preventDefault();
      const item = filtered[cursor];
      if (item) select(item);
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/50 pt-24"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-label="Launcher"
        onClick={(e) => e.stopPropagation()}
        className="w-[480px] border border-term-edge bg-term-panel"
      >
        <input
          ref={inputRef}
          value={mode.kind === "name" ? mode.value : query}
          onChange={(e) => {
            const value = e.target.value;
            if (mode.kind === "name") {
              setMode({ ...mode, value });
              return;
            }
            setQuery(value);
            setCursor(0);
          }}
          onKeyDown={onKeyDown}
          placeholder={
            mode.kind === "name"
              ? mode.command === "save"
                ? "name for this session…"
                : "new name…"
              : "panes, runners and sessions…"
          }
          className="w-full border-b border-term-edge bg-term-bg px-3 py-2 text-sm text-term-fg placeholder:text-term-dim focus:outline-none"
        />
        {mode.kind === "list" && (
          <ul ref={listRef} className="max-h-80 overflow-auto">
            {filtered.map((item, i) => (
              <li key={item.key} data-index={i}>
                <button
                  type="button"
                  onClick={() => select(item)}
                  className={`flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs ${
                    i === cursor ? "bg-term-raised text-term-accent" : "text-term-fg"
                  }`}
                >
                  <span className="w-14 shrink-0 text-[10px] uppercase tracking-wider text-term-dim">
                    {item.kind}
                  </span>
                  {item.label}
                </button>
              </li>
            ))}
            {filtered.length === 0 && (
              <li className="px-3 py-2 text-xs text-term-dim">no match</li>
            )}
          </ul>
        )}
        {mode.kind === "name" && (
          <div className="px-3 py-2 text-[10px] text-term-dim">
            {mode.command === "save"
              ? "saves the current pane layout under a new name · return save · esc back"
              : "renames the current session · return save · esc back"}
          </div>
        )}
        {mode.kind === "confirmDelete" && (
          <div className="px-3 py-2 text-xs text-term-fg">
            delete “{activeSession?.name}”?
            <div className="mt-1 text-[10px] text-term-dim">
              return delete · esc back · the layout is not recoverable
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
