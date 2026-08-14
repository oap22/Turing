// Custom single-select listbox: WKWebView draws native <select> option
// popups with OS chrome that CSS cannot reach, so a "just style the select"
// fix is not available. This reimplements the trigger + popup with the same
// visual vocabulary as the other overlay surfaces (Cheatsheet, Launcher) —
// bordered term-panel popup, term-raised/term-accent highlighted row — so it
// blends into the terminal theme instead of looking like a browser default.

import { useEffect, useId, useRef, useState } from "react";

interface Option {
  value: string;
  label: string;
}

interface Props {
  value: string;
  options: ReadonlyArray<Option>;
  onChange: (value: string) => void;
  label: string; // accessible name (aria-label)
  className?: string; // outer positioning classes from the call site
  size?: "xs" | "sm"; // the Home/DesktopShell header ones are 10px chrome
}

export default function Select({
  value,
  options,
  onChange,
  label,
  className = "",
  size = "sm",
}: Props) {
  const [open, setOpen] = useState(false);
  // Cursor is the keyboard-highlighted row, independent of `value` until
  // Enter commits it — arrowing through the list should not change the
  // selection (and thus fire onChange) on every keypress.
  const [cursor, setCursor] = useState(0);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const listRef = useRef<HTMLUListElement | null>(null);
  // Unique per component instance so two Selects open on the same screen
  // (e.g. two header pickers) don't collide on option ids.
  const instanceId = useId();
  const optionId = (i: number) => `${instanceId}-option-${i}`;

  // Typeahead buffer: printable keys accumulate here and reset after a short
  // idle gap, same as the native <select> jump-to-letter behavior this
  // control otherwise lost.
  const typeaheadRef = useRef<{ chars: string; timer: ReturnType<typeof setTimeout> | null }>({
    chars: "",
    timer: null,
  });

  const selectedIndex = Math.max(
    0,
    options.findIndex((o) => o.value === value),
  );
  const current = options[selectedIndex];

  // Click-outside close: a pointerdown listener on document, same pattern as
  // the other dismissible overlays in this app.
  useEffect(() => {
    if (!open) return;
    function onPointerDown(e: PointerEvent) {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("pointerdown", onPointerDown);
    return () => document.removeEventListener("pointerdown", onPointerDown);
  }, [open]);

  // Cursor starts on the currently selected option every time the popup
  // opens, so arrowing immediately moves relative to what's picked. Focus
  // moves to the listbox itself so arrow/Enter/Escape work without an extra
  // click — the trigger's keydown handler bails out while open (see below).
  useEffect(() => {
    if (open) {
      setCursor(selectedIndex);
      listRef.current?.focus();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open]);

  // Keep the highlighted row on screen as the cursor moves past the fold.
  useEffect(() => {
    if (!open) return;
    listRef.current
      ?.querySelector<HTMLElement>(`[data-index="${cursor}"]`)
      ?.scrollIntoView({ block: "nearest" });
  }, [cursor, open]);

  function commit(index: number) {
    const opt = options[index];
    if (opt) onChange(opt.value);
    setOpen(false);
    triggerRef.current?.focus();
  }

  function onTriggerKeyDown(e: React.KeyboardEvent) {
    if (open) return; // list owns key handling once it's open
    if (e.key === "Enter" || e.key === " " || e.key === "ArrowDown") {
      e.preventDefault();
      setOpen(true);
    }
  }

  function onListKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Escape") {
      e.preventDefault();
      setOpen(false);
      triggerRef.current?.focus();
      return;
    }
    if (e.key === "Tab") {
      // Don't trap focus — just let the popup close as it leaves.
      setOpen(false);
      return;
    }
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setCursor((c) => Math.min(options.length - 1, c + 1));
      return;
    }
    if (e.key === "ArrowUp") {
      e.preventDefault();
      setCursor((c) => Math.max(0, c - 1));
      return;
    }
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      commit(cursor);
      return;
    }
    // Restores the native <select>'s type-to-jump behavior: accumulate
    // printable characters into a short-lived buffer and move the cursor to
    // the first option whose label starts with it, case-insensitively.
    if (e.key.length === 1 && !e.metaKey && !e.ctrlKey && !e.altKey) {
      const ta = typeaheadRef.current;
      if (ta.timer) clearTimeout(ta.timer);
      ta.chars += e.key.toLowerCase();
      ta.timer = setTimeout(() => {
        ta.chars = "";
        ta.timer = null;
      }, 600);
      const match = options.findIndex((o) => o.label.toLowerCase().startsWith(ta.chars));
      if (match !== -1) setCursor(match);
    }
  }

  // The header pickers (Home, DesktopShell) are quiet 10px chrome that only
  // brightens on hover; the flywheel/metrics pickers are ordinary controls.
  const chrome =
    size === "xs"
      ? "px-1 text-[10px] text-term-dim hover:text-term-fg"
      : "px-2 py-0.5 text-xs text-term-fg";

  return (
    <div ref={rootRef} className={`relative ${className}`}>
      <button
        ref={triggerRef}
        type="button"
        aria-haspopup="listbox"
        aria-expanded={open}
        // aria-label completely overrides the accessible name computation —
        // it does not layer on top of the button's text content — so a bare
        // `label` here permanently announces "theme" and never the current
        // value. Fold the value into the label itself so both are spoken.
        aria-label={`${label}: ${current?.label ?? ""}`}
        onClick={() => setOpen((o) => !o)}
        onKeyDown={onTriggerKeyDown}
        className={`flex w-full items-center justify-between gap-1 border border-term-edge bg-term-bg font-mono ${chrome}`}
      >
        <span className="truncate">{current?.label ?? ""}</span>
        <span aria-hidden="true" className="shrink-0 text-term-dim">
          ▾
        </span>
      </button>
      {open && (
        // z-40: above pane content but below the modal overlays (Cheatsheet /
        // Launcher / UpdateOverlay), which sit at z-50.
        <ul
          ref={listRef}
          role="listbox"
          aria-label={label}
          // Focus stays on the <ul> for the whole session (see the effect
          // above), so the keyboard cursor is only a CSS class unless we also
          // tell AT where it is — aria-activedescendant plus a stable id per
          // option is the WAI-ARIA listbox pattern for that case.
          aria-activedescendant={options.length > 0 ? optionId(cursor) : undefined}
          tabIndex={-1}
          onKeyDown={onListKeyDown}
          className="absolute right-0 z-40 mt-1 max-h-48 min-w-full overflow-auto border border-term-edge bg-term-panel"
        >
          {options.map((opt, i) => (
            <li key={opt.value} data-index={i} role="presentation">
              <button
                id={optionId(i)}
                type="button"
                role="option"
                aria-selected={opt.value === value}
                onClick={() => commit(i)}
                onMouseEnter={() => setCursor(i)}
                className={`block w-full whitespace-nowrap px-2 py-1 text-left text-xs ${
                  i === cursor ? "bg-term-raised text-term-accent" : "text-term-fg"
                }`}
              >
                {opt.label}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
