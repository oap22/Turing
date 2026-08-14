// RSI workstations — the command-building half. An RSI workstation is an
// experiment, not a saved shape: creating one seeds a layout (see
// `rsiLayout()` in layout.ts) whose loop terminal is pre-typed with a command
// built here. The loop engine itself lives outside this codebase's type
// system entirely, in `scripts/rsi-loop.sh` — a shell script that runs a
// continuous Claude Code loop in a sandbox directory. The contract between
// the two halves is exactly the argv this module builds: this file never
// runs anything, it only describes what TermPane should pre-type.

import { REPO, RESULTS_ROOT_TOKEN, type Runner } from "./runners";

// Lowercases, collapses anything outside [a-z0-9] into single hyphens, and
// trims. Used both as the workstation's sandbox directory name
// (~/turing-workspace/rsi-<slug>) and as the results subdirectory
// (loop-rsi-<slug>), so it has to be filesystem- and shell-safe on its own —
// no quoting rescues a bad slug used as a directory name.
export function rsiSlug(name: string): string {
  let slug = name.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
  slug = slug.slice(0, 40).replace(/-+$/g, "");
  return slug === "" ? "experiment" : slug;
}

// Pre-typed text goes into a PTY via `pty_write`, character stream, no
// newline translation. A literal `\n` embedded in the problem statement would
// submit the command before the rest of it was even typed, so every
// whitespace run (including newlines/tabs) is collapsed to one space before
// quoting. Single-quoted with the standard shell escape for an embedded
// single quote: close the quote, emit an escaped quote, reopen.
export function shellQuoteSingle(text: string): string {
  const collapsed = text.replace(/\s+/g, " ").trim();
  return "'" + collapsed.replaceAll("'", "'\\''") + "'";
}

export interface RsiParams {
  slug: string;
  problem: string;
}

export function isRsiParams(x: unknown): x is RsiParams {
  if (!x || typeof x !== "object") return false;
  const o = x as Record<string, unknown>;
  return (
    typeof o.slug === "string" &&
    /^[a-z0-9-]+$/.test(o.slug) &&
    typeof o.problem === "string" &&
    o.problem !== ""
  );
}

// The runner TermPane pre-types for an RSI workstation's loop terminal.
// `autorun: false` is load-bearing, not a stylistic default: creating (or
// reopening) an RSI workstation must never silently start a Claude Code loop
// that spends API money the moment the pane mounts. The user reviews the
// pre-typed command and presses Enter themselves.
export function rsiRunner(params: RsiParams): Runner {
  return {
    id: `rsi-${params.slug}`,
    label: `rsi loop: ${params.slug}`,
    command: `scripts/rsi-loop.sh --slug ${params.slug} --results-root ${RESULTS_ROOT_TOKEN} --rounds 10 --problem ${shellQuoteSingle(params.problem)}`,
    cwd: REPO,
    group: "research",
    autorun: false,
  };
}
