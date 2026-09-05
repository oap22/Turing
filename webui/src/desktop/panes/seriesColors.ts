// Sticky per-run color assignment for the metrics pane (PRD §1).
//
// A run claims a color the first time it's plotted and keeps it until it
// leaves the chart. Without that, the palette was indexed by position in the
// selection, so dropping the first run shifted every line below it to a new
// color and switching metric tabs could recolor the whole chart — the legend
// you'd just read stopped matching the curves you were watching.
//
// Pure module: no React, no module-level state. The caller owns the
// assignment map and threads the previous one back in.

/** how many categorical colors every theme defines (--t-series-1..8) */
export const SERIES_COLOR_COUNT = 8;

/** slot is 0-based; returns the CSS custom property reference, e.g. "var(--t-series-1)" for slot 0 */
export function colorForSlot(slot: number): string {
  // The CSS properties are 1-based, the slots are 0-based; wrapping keeps a
  // stray out-of-range slot rendering *some* color rather than a broken
  // `var(--t-series-0)` that resolves to nothing and paints the line black.
  const wrapped = ((Math.trunc(slot) % SERIES_COLOR_COUNT) + SERIES_COLOR_COUNT) % SERIES_COLOR_COUNT;
  return `var(--t-series-${wrapped + 1})`;
}

/** run key -> 0-based palette slot */
export type ColorAssignment = ReadonlyMap<string, number>;

/**
 * Reassign colors for the currently-active keys, preserving every sticky
 * assignment that is still active.
 */
export function assignColors(
  prev: ColorAssignment,
  activeKeys: readonly string[],
): ColorAssignment {
  // Defensive dedupe: a repeated key would otherwise burn two slots and show
  // up twice in the legend for one line.
  const keys = [...new Set(activeKeys)];

  const next = new Map<string, number>();
  const taken = new Set<number>();

  // Pass 1 — everything that survived keeps its exact slot. Keys in `prev`
  // that aren't active are simply never copied over, which is what frees
  // their slot for pass 2.
  for (const key of keys) {
    const slot = prev.get(key);
    if (slot !== undefined) {
      next.set(key, slot);
      taken.add(slot);
    }
  }

  // Pass 2 — new keys take the lowest free slot, in activeKeys order, so the
  // palette stays packed at the low end instead of drifting upward as runs
  // come and go (PRD §1.3).
  let cursor = 0;
  for (const key of keys) {
    if (next.has(key)) continue;
    while (taken.has(cursor)) cursor++;
    // The selection is capped at SERIES_COLOR_COUNT upstream, and that cap is
    // the only reason "no two lines share a color" (§3.1) actually holds.
    // Past the cap there is no correct answer, so wrap rather than throw —
    // a duplicated color is a far better failure than a crashed pane.
    next.set(key, cursor % SERIES_COLOR_COUNT);
    taken.add(cursor);
    cursor++;
  }

  return next;
}
