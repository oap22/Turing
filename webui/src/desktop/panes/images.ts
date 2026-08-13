// Pure helpers for the images pane — file identity, ordering, and the
// follow-mode selection rule. Kept out of the component so the rule that
// fixes issue #391 is testable without a Tauri runtime.

export interface Entry {
  rel_path: string;
  is_dir: boolean;
  size: number;
  mtime_ms: number;
}

export interface ImageEntry extends Entry {
  root: string;
}

export const IMAGE_EXTS = ["png", "jpg", "jpeg", "gif", "svg", "webp"];

export function mimeFor(relPath: string): string {
  const ext = relPath.split(".").pop()?.toLowerCase() ?? "";
  if (ext === "svg") return "image/svg+xml";
  if (ext === "jpg" || ext === "jpeg") return "image/jpeg";
  if (ext === "gif") return "image/gif";
  if (ext === "webp") return "image/webp";
  return "image/png";
}

export function relTail(relPath: string): string {
  const parts = relPath.split("/");
  return parts[parts.length - 1] ?? relPath;
}

export function ageLabel(mtimeMs: number, nowMs: number): string {
  const s = Math.max(0, Math.floor((nowMs - mtimeMs) / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h`;
}

export function sameEntry(a: ImageEntry | null, b: ImageEntry | null): boolean {
  return !!a && !!b && a.root === b.root && a.rel_path === b.rel_path;
}

/**
 * What a refresh should select, given what is on disk now.
 *
 * The bug in #391 was that this decision only distinguished "no selection
 * yet" from "has a selection" — never "selection the pane made" from
 * "selection the user made". With `ssh-pull-assets` writing a new plot every
 * 30s, that meant every deliberate selection was overwritten within the
 * minute.
 *
 * The rule now: auto-select on first load (nothing chosen yet), keep
 * following while following is on, and otherwise leave the selection alone.
 * Turning following off is the caller's job — see `selectByUser` in the pane
 * — which is what makes a user's choice stick.
 *
 * Returns the entry to select, or `null` to leave the current selection be.
 */
export function nextSelection(
  merged: ImageEntry[],
  prev: ImageEntry | null,
  watchLatest: boolean,
): ImageEntry | null {
  const newest = merged[0];
  if (!newest) return null;
  // Nothing chosen yet — auto-select is correct here and always has been.
  if (!prev) return newest;
  if (!watchLatest) return null;
  return sameEntry(prev, newest) ? null : newest;
}

/**
 * How many newer images sit ahead of the selection.
 *
 * `files` is newest-first, so the selection's index is the count directly.
 * Drives the `[N new]` affordance that gets the user back to following.
 */
export function newerCount(
  files: ImageEntry[],
  selected: ImageEntry | null,
): number {
  if (!selected) return 0;
  const i = files.findIndex((f) => sameEntry(selected, f));
  return i > 0 ? i : 0;
}

/**
 * Step the selection through `files` by `delta` (-1 previous, +1 next) in the
 * same newest-first order the list shows. Clamps at both ends rather than
 * wrapping, so holding an arrow key settles instead of cycling forever.
 *
 * Returns `null` when there is nowhere to go.
 */
export function stepSelection(
  files: ImageEntry[],
  selected: ImageEntry | null,
  delta: number,
): ImageEntry | null {
  if (files.length === 0) return null;
  if (!selected) return files[0] ?? null;
  const i = files.findIndex((f) => sameEntry(selected, f));
  // Selection no longer on disk: fall back to the newest rather than blanking.
  if (i === -1) return files[0] ?? null;
  const next = Math.min(files.length - 1, Math.max(0, i + delta));
  return next === i ? null : (files[next] ?? null);
}
