// Cross-pane selection link (#390 item 3): clicking `[metrics]` on a round in
// the flywheel pane points the metrics pane at that round's runs.
//
// **Why an in-app channel and not `.viewer.json`.** Issue #388 settled that
// `.viewer.json`-shaped files are the agent → app direction: an agent (or a
// hand edit) writes them, the app only ever reads them. If the app wrote the
// file to express a click, it would stomp whatever an agent had written and
// create a produce/consume loop between the app and its own watcher. So the
// click travels as a plain in-process event instead, and the file path keeps
// working exactly as before for agents.
//
// **Precedence: last action wins.** The metrics pane holds exactly ONE
// pending run request at a time (`RunRequest` below), whichever source it
// came from. A flywheel click replaces a `.viewer.json` request; a later
// `.viewer.json` change replaces the click; a second click replaces that
// again. Because there is a single slot, a request that could not be honoured
// yet (its runs are not on disk) can never fire late over a newer one — being
// replaced IS being superseded, honoured or not.
//
// **The mapping, stated once.** A round is `loop` (the `loop-*` directory
// name) plus its round index. It maps to every live run of that round: each
// `<loop>/round-NN/attempts/<problem-id>/metrics.jsonl` on disk, where
// `<problem-id>` may itself be nested (`cuda/matmul-speedup`). Rotated
// `prior-N/` chains and dot-named staging directories are excluded, mirroring
// `RoundRunner._viewer_runs` — a superseded generation must not be charted as
// if the click asked for it. The active series is deliberately left alone: the
// click chooses *which runs*, and the operator's series tab keeps meaning what
// it meant. A round with no run files (yet) resolves to nothing; the caller
// keeps its current chart and the request stays pending, so metrics landing
// later can still satisfy it — the same graceful deferral `.viewer.json`
// requests already get.

import { matchesViewerRuns, runIdOf } from "./metrics";
import { roundDirName } from "./roundRecord";

/** One flywheel round, addressed the way the results tree spells it. */
export interface RoundTarget {
  /** The `loop-*` directory name under the results root. */
  loop: string;
  /** The round index — `roundDirName` turns it into `round-NN`. */
  round: number;
}

/**
 * The metrics pane's single pending run request — from a flywheel click or
 * from a `.viewer.json` delivery. One type for both so the pane can hold them
 * in one slot, which is what makes "last action wins" structural rather than
 * a set of guards.
 */
export type RunRequest =
  | { source: "flywheel"; target: RoundTarget }
  | { source: "viewer"; runs: string[] };

//: `runner._PRIOR_DIR_PATTERN` — the names `_rotate_stale_metrics` mints for
//: superseded chains. Only ever the last segment before a rotated
//: `metrics.jsonl`, and `contracts._reject_unsafe_problem_id` refuses problem
//: ids that would collide with it.
const PRIOR_DIR = /^prior-\d+$/;

/**
 * Does this run file belong to the target round's live generation?
 *
 * `relPath` is a `metrics.jsonl` path relative to the results root; its run id
 * (the containing directory, per `runIdOf`) must sit at
 * `<loop>/round-NN/attempts/…` and its final segment must be neither a
 * rotated `prior-N` chain nor a dot-named staging directory — the same two
 * exclusions `RoundRunner._viewer_runs` makes when it emits `.viewer.json`,
 * for the same reason: those directories hold a superseded or uncommitted
 * generation, not a run of this round.
 */
export function matchesRound(relPath: string, target: RoundTarget): boolean {
  const segments = runIdOf(relPath).split("/");
  if (segments[0] !== target.loop) return false;
  if (segments[1] !== roundDirName(target.round)) return false;
  if (segments[2] !== "attempts" || segments.length < 4) return false;
  const last = segments[segments.length - 1];
  return !PRIOR_DIR.test(last) && !last.startsWith(".");
}

/**
 * Which of the discovered run files a request selects.
 *
 * `relPaths` are run-file paths as `fs_list` reports them; the return value is
 * the subset the request names, in discovery order — exactly what the metrics
 * pane's `selected` state holds. An empty answer means "nothing on this
 * machine satisfies it (yet)", never an error: the caller keeps its current
 * selection and may resolve the same request again when the file list changes.
 */
export function resolveRunRequest(req: RunRequest, relPaths: readonly string[]): string[] {
  if (req.source === "viewer") {
    return relPaths.filter((p) => matchesViewerRuns(p, req.runs));
  }
  return relPaths.filter((p) => matchesRound(p, req.target));
}

// ---------------------------------------------------------------------------
// The channel itself
// ---------------------------------------------------------------------------
//
// A module-level set of subscribers, not the Tauri event bus in `tauri.ts`:
// that bus exists to survive Tauri's listener-registry corruption and every
// event on it originates in Rust. This event never leaves the webview, so a
// plain synchronous fan-out is the whole implementation. Delivery is to the
// panes mounted right now — a click is a momentary gesture, so it is
// deliberately NOT replayed to a metrics pane opened later, which would apply
// a stale gesture the operator has long moved past. With no metrics pane
// mounted anywhere in the layout, a click simply lands nowhere.

type Listener = (target: RoundTarget) => void;

const listeners = new Set<Listener>();

/** Flywheel side: announce the round the operator asked metrics to show. */
export function publishMetricsTarget(target: RoundTarget): void {
  // Snapshot, so a listener unsubscribing during dispatch cannot skip a
  // sibling — same rule as the event bus in tauri.ts.
  for (const cb of [...listeners]) {
    try {
      cb(target);
    } catch {
      // A pane's render bug is not the channel's fault; siblings still hear.
    }
  }
}

/** Metrics side: hear clicks while mounted. Returns the unsubscribe. */
export function subscribeMetricsTarget(cb: Listener): () => void {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
}

/** Test seam: drop every subscriber so files cannot leak into each other. */
export function __resetPaneLinkForTests(): void {
  listeners.clear();
}
