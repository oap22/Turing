// Startup session picker: the one thing that renders *instead of* the shell,
// and only when there is a real choice to make.
//
// Two rules shape it. First, it must not be a tax: with a single saved session
// (which is every user who has never saved one — the migrated "default"
// counts) `DesktopShell` never mounts this at all and boots straight into the
// layout, exactly like before sessions existed. Second, it renders before the
// pane tree rather than over it, because mounting the shell underneath would
// spawn PTYs for the last-used layout only to have the user pick a different
// one a keystroke later.
//
// Escape (or the footer hint's implied default) takes the last-used session,
// so the fastest path is still "launch, press Enter or Escape, keep working".
// Styling and keyboard conventions follow `Launcher.tsx`: same overlay scrim,
// same panel chrome, same ArrowUp/ArrowDown/Enter/Escape handling, same
// scroll-the-cursor-into-view effect.

import { useEffect, useRef, useState } from "react";
import type { Session } from "./sessions";

interface Props {
  sessions: ReadonlyArray<Session>;
  activeId: string | null;
  onPick: (id: string) => void;
  onSkip: () => void;
}

function paneCount(session: Session): number {
  let n = 0;
  for (const ws of session.layout.workspaces) {
    const stack = ws.root ? [ws.root] : [];
    while (stack.length > 0) {
      const node = stack.pop()!;
      if (node.kind === "leaf") n += 1;
      else stack.push(node.a, node.b);
    }
  }
  return n;
}

export default function SessionPicker({ sessions, activeId, onPick, onSkip }: Props) {
  // Start on the last-used session so a bare Enter is the same as Escape.
  const [cursor, setCursor] = useState(() => {
    const i = sessions.findIndex((s) => s.id === activeId);
    return i >= 0 ? i : 0;
  });
  const boxRef = useRef<HTMLDivElement | null>(null);
  const listRef = useRef<HTMLUListElement | null>(null);

  // Unlike the Launcher there is no text input to hold focus, so the dialog
  // itself takes it (tabIndex -1) — otherwise the keys below never arrive.
  useEffect(() => {
    boxRef.current?.focus();
  }, []);

  useEffect(() => {
    listRef.current
      ?.querySelector<HTMLElement>(`[data-index="${cursor}"]`)
      ?.scrollIntoView({ block: "nearest" });
  }, [cursor]);

  function onKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Escape") {
      e.preventDefault();
      onSkip();
      return;
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setCursor((c) => Math.min(sessions.length - 1, c + 1));
      return;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      setCursor((c) => Math.max(0, c - 1));
      return;
    }
    if (e.key === "Enter") {
      e.preventDefault();
      const session = sessions[cursor];
      if (session) onPick(session.id);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/50 pt-24">
      <div
        ref={boxRef}
        role="dialog"
        aria-label="Sessions"
        tabIndex={-1}
        onKeyDown={onKeyDown}
        className="w-[480px] border border-term-edge bg-term-panel focus:outline-none"
      >
        <div className="border-b border-term-edge px-3 py-2 text-[11px] uppercase tracking-widest text-term-dim">
          sessions
        </div>
        <ul ref={listRef} className="max-h-80 overflow-auto">
          {sessions.map((session, i) => (
            <li key={session.id} data-index={i}>
              <button
                type="button"
                onClick={() => onPick(session.id)}
                className={`flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs ${
                  i === cursor ? "bg-term-raised text-term-accent" : "text-term-fg"
                }`}
              >
                <span className="w-14 shrink-0 text-[10px] uppercase tracking-wider text-term-dim">
                  {session.id === activeId ? "last" : ""}
                </span>
                <span className="min-w-0 flex-1 truncate">{session.name}</span>
                <span className="shrink-0 text-[10px] text-term-dim">
                  {paneCount(session)} panes
                </span>
              </button>
            </li>
          ))}
        </ul>
        <div className="border-t border-term-edge px-3 py-2 text-[10px] text-term-dim">
          ↑↓ choose · return open · esc last used · restored sessions open fresh shells
        </div>
      </div>
    </div>
  );
}
