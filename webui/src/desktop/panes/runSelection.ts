// Run selection for the metrics pane — which runs are drawn on the one active
// metric chart. Pure state machine (no React, no I/O) so the rules the spec
// argues about (`.scratch/prd-metrics-run-comparison.md`) can be tested without
// a DOM: auto-follows-newest, the 8-line cap, and the sticky-empty `clear`.

import { runLabelOf, type RunFile } from "./metrics";

export interface RunSelection {
  /** the `auto` row is on */
  auto: boolean;
  /** explicitly pinned run file rel_paths, in the order they were picked */
  pinned: string[];
  /** sticky-empty: set by clearAll, blocks .viewer.json until a manual pick */
  cleared: boolean;
}

/** Opening the pane always looks the same — the selection never persists. */
export const INITIAL_SELECTION: RunSelection = { auto: true, pinned: [], cleared: false };

// Eight, because the theme defines eight categorical series colors and "no two
// lines share a color" is the invariant that makes the legend readable.
export const MAX_LINES = 8;

/** rel_path of the run `auto` points at (the newest run file = runFiles[0]), or null. */
export function autoPath(sel: RunSelection, runFiles: RunFile[]): string | null {
  // Derived, never stored: when a newer run lands at the head of the list auto
  // follows it on its own and the run it used to hold simply stops being drawn.
  if (!sel.auto || runFiles.length === 0) return null;
  return runFiles[0].rel_path;
}

/** every rel_path that should be plotted, auto's run first, then pinned in pick order, deduped. */
export function activePaths(sel: RunSelection, runFiles: RunFile[]): string[] {
  const auto = autoPath(sel, runFiles);
  const out: string[] = [];
  const seen = new Set<string>();
  for (const path of auto === null ? sel.pinned : [auto, ...sel.pinned]) {
    // A run pinned while auto already held it would otherwise be drawn twice,
    // in two colors, as two legend entries for the same run.
    if (seen.has(path)) continue;
    seen.add(path);
    out.push(path);
  }
  return out;
}

/** how many lines are currently drawn (auto counts) — i.e. activePaths().length */
export function lineCount(sel: RunSelection, runFiles: RunFile[]): number {
  return activePaths(sel, runFiles).length;
}

/** true when lineCount has reached MAX_LINES */
export function atCap(sel: RunSelection, runFiles: RunFile[]): boolean {
  return lineCount(sel, runFiles) >= MAX_LINES;
}

/** true when this run's picker row is dimmed because `auto` currently holds it */
export function isHeldByAuto(sel: RunSelection, runFiles: RunFile[], path: string): boolean {
  return autoPath(sel, runFiles) === path;
}

/** true when the picker row for `path` is clickable (see rules below) */
export function canPick(sel: RunSelection, runFiles: RunFile[], path: string): boolean {
  if (!runFiles.some((f) => f.rel_path === path)) return false;
  // The cap only ever blocks picks that would draw one more line. Unpinning
  // (frees a line) and promoting auto's run (net-neutral) stay live at 8/8,
  // otherwise the pane would deadlock with no way back under the cap.
  return lineCount(pickResult(sel, runFiles, path), runFiles) <= MAX_LINES;
}

// What a picker click *wants* to do, cap ignored — shared by `canPick` (which
// asks whether the result fits) and `togglePin` (which refuses it if it doesn't).
function pickResult(sel: RunSelection, runFiles: RunFile[], path: string): RunSelection {
  if (sel.pinned.includes(path)) {
    // Checked before the promote branch so a run that is somehow both pinned
    // and auto-held (reachable via .viewer.json) can't be pinned a second time.
    return { ...sel, pinned: sel.pinned.filter((p) => p !== path), cleared: false };
  }
  if (isHeldByAuto(sel, runFiles, path)) {
    // Promote: the dimmed row becomes a real pin and auto steps aside, so this
    // run survives the next handover instead of vanishing when a run lands.
    return { auto: false, pinned: [...sel.pinned, path], cleared: false };
  }
  return { ...sel, pinned: [...sel.pinned, path], cleared: false };
}

export function toggleAuto(sel: RunSelection, runFiles: RunFile[]): RunSelection {
  if (sel.auto) return { ...sel, auto: false, cleared: false };
  const next: RunSelection = { ...sel, auto: true, cleared: false };
  // Refuse rather than draw a ninth line; the user clears something first.
  if (lineCount(next, runFiles) > MAX_LINES) return sel;
  return next;
}

export function togglePin(sel: RunSelection, runFiles: RunFile[], path: string): RunSelection {
  const next = pickResult(sel, runFiles, path);
  if (lineCount(next, runFiles) > MAX_LINES) return sel;
  return next;
}

/** the legend `x`: drop whatever draws this path */
export function removePath(sel: RunSelection, runFiles: RunFile[], path: string): RunSelection {
  const heldByAuto = isHeldByAuto(sel, runFiles, path);
  const pinned = sel.pinned.includes(path);
  // The `x` means "this line goes away" with no exceptions, so a run drawn by
  // auto *and* a pin has to lose both claims or the line would stay put.
  if (!heldByAuto && !pinned) return sel;
  return {
    auto: heldByAuto ? false : sel.auto,
    pinned: pinned ? sel.pinned.filter((p) => p !== path) : sel.pinned,
    cleared: false,
  };
}

export function clearAll(sel: RunSelection): RunSelection {
  // Sticky empty: clear means clear, even against a `.viewer.json` write.
  return { ...sel, auto: false, pinned: [], cleared: true };
}

/** apply a .viewer.json `runs` list (run LABELS, not paths) */
export function applyViewerRuns(
  sel: RunSelection,
  runFiles: RunFile[],
  labels: string[],
): RunSelection {
  if (sel.cleared) return sel;

  const pathByLabel = new Map<string, string>();
  for (const file of runFiles) {
    const label = runLabelOf(file.rel_path);
    if (!pathByLabel.has(label)) pathByLabel.set(label, file.rel_path);
  }

  const paths: string[] = [];
  for (const label of labels) {
    const path = pathByLabel.get(label);
    // A label naming a run that hasn't appeared yet (or a typo) is skipped
    // rather than failing the write — the rest of the list still applies.
    if (path === undefined || paths.includes(path)) continue;
    if (paths.length >= MAX_LINES) break;
    paths.push(path);
  }

  // Nothing in the file matched anything on disk: leave the user's selection
  // alone instead of blanking the chart on a stale or mistyped control file.
  if (paths.length === 0) return sel;

  return { auto: false, pinned: paths, cleared: false };
}
