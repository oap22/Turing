// ⌘P launcher: fuzzy-filtered overlay over panes + runners. Selecting a pane
// opens it in the active workspace; selecting a runner opens a `term` pane
// pre-wired to it (`openPane("term", { runnerId })`).

import { useEffect, useMemo, useRef, useState } from "react";
import type { PaneType } from "./layout";
import { RUNNERS } from "./runners";

interface Props {
  onClose: () => void;
  onOpenPane: (pane: PaneType) => void;
  onOpenRunner: (runnerId: string) => void;
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

type Item =
  | { kind: "pane"; key: string; label: string; pane: PaneType }
  | { kind: "runner"; key: string; label: string; runnerId: string };

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

export default function Launcher({ onClose, onOpenPane, onOpenRunner }: Props) {
  const [query, setQuery] = useState("");
  const [cursor, setCursor] = useState(0);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const listRef = useRef<HTMLUListElement | null>(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

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
    ],
    [],
  );

  const filtered = useMemo(
    () => items.filter((i) => fuzzyMatch(query, i.label)),
    [items, query],
  );

  function select(item: Item) {
    if (item.kind === "pane") onOpenPane(item.pane);
    else onOpenRunner(item.runnerId);
    onClose();
  }

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Escape") {
      e.preventDefault();
      onClose();
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
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            setCursor(0);
          }}
          onKeyDown={onKeyDown}
          placeholder="panes and runners…"
          className="w-full border-b border-term-edge bg-term-bg px-3 py-2 text-sm text-term-fg placeholder:text-term-dim focus:outline-none"
        />
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
      </div>
    </div>
  );
}
