// Agent-driven pane control (#388) — the `.layout.json` control file.
//
// The same idea as `.viewer.json`, one level up: instead of "which series a
// pane shows", this says *which panes exist, where, and on which workspace*.
// An agent (or a human with `vim`) writes a plain file under the results root
// and the running app rearranges itself. No relaunch, no transport, no auth,
// no hand-editing the persisted layout blob.
//
// **Declarative, not a command queue.** The file states the layout it wants
// and the app makes reality match. Re-applying it is a no-op, which is what
// makes it safe to rewrite on every change and safe to re-read after a
// partial write.
//
// **Per-workspace authority.** A workspace the file mentions is replaced
// wholesale; a workspace it does not mention is left alone. That is what lets
// "set up workspace 3 and empty workspace 4" be one file that does not
// disturb the terminals on workspace 1. Opening and closing individual panes
// are expressed by listing or omitting them — there are no imperative ops.
//
// **Everything here is pure.** Compiling a request to a `LayoutState` has no
// side effects, so the whole surface is testable without a Tauri runtime; the
// shell owns reading the file and dispatching the result.

import {
  type LayoutState,
  type Node,
  type PaneType,
  PANE_TYPES,
  nextId,
} from "./layout";
import { RUNNERS } from "./runners";

/** Watched under the `results` root, beside `.viewer.json`. */
export const LAYOUT_FILE_REL = ".layout.json";

// Matches the clamp `resize()` applies (layout.ts), so a ratio written here
// cannot reach a state dragging a divider could not.
const MIN_RATIO = 0.1;
const MAX_RATIO = 0.9;
const DEFAULT_RATIO = 0.5;

export interface PaneSpec {
  pane: PaneType;
  /** Only for `term`, and only an id from the static runner table. */
  runnerId?: string;
}

export interface SplitSpec {
  split: "h" | "v";
  ratio?: number;
  a: NodeSpec;
  b: NodeSpec;
}

export type NodeSpec = PaneSpec | SplitSpec;

export interface WorkspaceSpec {
  /** Explicit tree. Mutually exclusive with `panes`. */
  tree?: NodeSpec;
  /** Shorthand: a spine of panes split in one direction. */
  panes?: PaneSpec[];
  /** Direction for the `panes` shorthand. Default `h`. */
  dir?: "h" | "v";
  /** Ratio for the `panes` shorthand. Default 0.5. */
  ratio?: number;
}

export interface LayoutRequest {
  /** Keyed by the number you press ⌘ with: "1".."5". `null` empties. */
  workspaces?: Record<string, WorkspaceSpec | null>;
  /** Switch the visible workspace, same 1-based numbering. */
  active?: number;
}

function isObj(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function isPaneType(v: unknown): v is PaneType {
  return typeof v === "string" && (PANE_TYPES as readonly string[]).includes(v);
}

function validRatio(v: unknown): boolean {
  return (
    typeof v === "number" &&
    Number.isFinite(v) &&
    v >= MIN_RATIO &&
    v <= MAX_RATIO
  );
}

// An id-less tree. Compilation produces one of these and reconciliation turns
// it into a real `Node`, which is what keeps the whole pipeline pure: minting
// ids during compile made compiling the same file twice produce different
// trees, and a different tree means every pane is torn down and rebuilt.
type ProtoLeaf = {
  kind: "leaf";
  pane: PaneType;
  params?: Record<string, unknown>;
};
type ProtoSplit = {
  kind: "split";
  dir: "h" | "v";
  ratio: number;
  a: ProtoNode;
  b: ProtoNode;
};
type ProtoNode = ProtoLeaf | ProtoSplit;

// Depth and size caps. A real layout is a handful of panes; these exist only
// so a hostile or corrupt file cannot recurse the compiler (and the walks
// downstream of it) into a RangeError. That matters more than it sounds:
// the reducer runs inside React's render phase, so a throw there escapes the
// watcher's try/catch, unmounts the root with no error boundary to catch it,
// and the same file re-crashes the app on every relaunch.
const MAX_DEPTH = 32;
const MAX_PANES = 64;

/**
 * Compile one node spec into an id-less `ProtoNode`.
 *
 * Returns `null` on anything malformed, which propagates all the way up: a
 * request is applied whole or not at all. A half-applied tree is the one
 * outcome worse than ignoring the file.
 */
function compileNode(spec: unknown, depth = 0): ProtoNode | null {
  if (depth > MAX_DEPTH) return null;
  if (!isObj(spec)) return null;

  if ("split" in spec) {
    if (spec.split !== "h" && spec.split !== "v") return null;
    if (spec.ratio !== undefined && !validRatio(spec.ratio)) return null;
    const a = compileNode(spec.a, depth + 1);
    const b = compileNode(spec.b, depth + 1);
    if (!a || !b) return null;
    return {
      kind: "split",
      dir: spec.split,
      ratio: spec.ratio === undefined ? DEFAULT_RATIO : (spec.ratio as number),
      a,
      b,
    };
  }

  if (!isPaneType(spec.pane)) return null;

  // A runner id, never a command string. Anything that can write into the
  // results root would otherwise have shell execution on this machine; an id
  // can only name something already in the static table.
  let params: Record<string, unknown> | undefined;
  if (spec.runnerId !== undefined) {
    if (spec.pane !== "term") return null;
    if (typeof spec.runnerId !== "string") return null;
    if (!RUNNERS.some((r) => r.id === spec.runnerId)) return null;
    params = { runnerId: spec.runnerId };
  }

  return { kind: "leaf", pane: spec.pane, params };
}

function sameParams(
  a?: Record<string, unknown>,
  b?: Record<string, unknown>,
): boolean {
  const ak = a ? Object.keys(a) : [];
  const bk = b ? Object.keys(b) : [];
  if (ak.length !== bk.length) return false;
  return ak.every((k) => Object.is(a?.[k], b?.[k]));
}

/**
 * Turn a `ProtoNode` into a real `Node`, reusing the live tree wherever it
 * already matches.
 *
 * This is what makes re-applying the same file a genuine no-op. Panes are
 * keyed by leaf id in the shell, so a fresh id unmounts and remounts the
 * pane — for a `term` that kills the pty and respawns the runner, so an
 * agent rewriting `.layout.json` on every turn would kill the test run it
 * just started. Matching subtrees are returned by *reference* so the caller
 * can detect "nothing changed" with `===`.
 */
function reconcile(proto: ProtoNode, existing: Node | null): Node {
  if (proto.kind === "leaf") {
    if (
      existing &&
      existing.kind === "leaf" &&
      existing.pane === proto.pane &&
      sameParams(existing.params, proto.params)
    ) {
      return existing;
    }
    return {
      kind: "leaf",
      id: nextId(),
      pane: proto.pane,
      params: proto.params,
    };
  }

  const exSplit = existing && existing.kind === "split" ? existing : null;
  const a = reconcile(proto.a, exSplit ? exSplit.a : null);
  const b = reconcile(proto.b, exSplit ? exSplit.b : null);
  if (
    exSplit &&
    exSplit.dir === proto.dir &&
    exSplit.ratio === proto.ratio &&
    exSplit.a === a &&
    exSplit.b === b
  ) {
    return exSplit;
  }
  return { kind: "split", dir: proto.dir, ratio: proto.ratio, a, b };
}

function countLeaves(proto: ProtoNode): number {
  return proto.kind === "leaf"
    ? 1
    : countLeaves(proto.a) + countLeaves(proto.b);
}

function containsLeaf(node: Node | null, id: string): boolean {
  if (!node) return false;
  if (node.kind === "leaf") return node.id === id;
  return containsLeaf(node.a, id) || containsLeaf(node.b, id);
}

/** Fold a flat pane list into a spine, nesting rightward. */
function compileSpine(
  specs: unknown,
  dir: "h" | "v",
  ratio: number,
): ProtoNode | null {
  if (!Array.isArray(specs) || specs.length === 0) return null;
  if (specs.length > MAX_PANES) return null;
  const nodes: ProtoNode[] = [];
  for (const s of specs) {
    const node = compileNode(s);
    if (!node) return null;
    nodes.push(node);
  }
  let acc = nodes[nodes.length - 1];
  for (let i = nodes.length - 2; i >= 0; i--) {
    acc = { kind: "split", dir, ratio, a: nodes[i], b: acc };
  }
  return acc;
}

function compileWorkspace(spec: unknown): ProtoNode | null | "invalid" {
  // Explicit null empties the workspace — distinct from omitting the key,
  // which leaves it untouched.
  if (spec === null) return null;
  if (!isObj(spec)) return "invalid";

  if (spec.tree !== undefined && spec.panes !== undefined) return "invalid";
  if (spec.dir !== undefined && spec.dir !== "h" && spec.dir !== "v")
    return "invalid";
  if (spec.ratio !== undefined && !validRatio(spec.ratio)) return "invalid";

  if (spec.tree !== undefined) {
    const proto = compileNode(spec.tree);
    if (!proto || countLeaves(proto) > MAX_PANES) return "invalid";
    return proto;
  }

  if (spec.panes !== undefined) {
    const dir = (spec.dir as "h" | "v" | undefined) ?? "h";
    const ratio =
      spec.ratio === undefined ? DEFAULT_RATIO : (spec.ratio as number);
    return compileSpine(spec.panes, dir, ratio) ?? "invalid";
  }

  // Neither key: nothing asked for. Treated as invalid rather than as an
  // empty workspace, so a typo'd key can never silently wipe panes.
  return "invalid";
}

/** The leftmost leaf — what a workspace focuses when its tree is replaced. */
function firstLeafId(node: Node | null): string | null {
  if (!node) return null;
  return node.kind === "leaf" ? node.id : firstLeafId(node.a);
}

export function parseLayoutRequest(text: string): LayoutRequest | null {
  try {
    const parsed: unknown = JSON.parse(text);
    return isObj(parsed) ? (parsed as LayoutRequest) : null;
  } catch {
    return null;
  }
}

/**
 * Apply a request to `state`, returning the next state — or `null` when the
 * request is unusable or asks for nothing, in which case the caller keeps the
 * layout exactly as it was.
 *
 * Unknown *extra* keys are ignored so the format can grow without breaking
 * older files. Structural errors — an unknown pane, a workspace number out of
 * range, a ratio outside what a divider drag could produce, a runner id that
 * isn't in the table — reject the whole request.
 */
export function applyLayoutRequest(
  state: LayoutState,
  req: unknown,
): LayoutState | null {
  if (!isObj(req)) return null;
  const count = state.workspaces.length;

  let active = state.active;
  if (req.active !== undefined) {
    if (!Number.isInteger(req.active)) return null;
    const idx = (req.active as number) - 1;
    if (idx < 0 || idx >= count) return null;
    active = idx;
  }

  const workspaces = state.workspaces.slice();
  let touched = false;

  if (req.workspaces !== undefined) {
    if (!isObj(req.workspaces)) return null;

    // Two passes: compile everything first, and only then commit. Compiling
    // is pure, so a request that turns out to be invalid halfway through has
    // changed nothing — including not having minted any leaf ids.
    const compiled: Array<{ idx: number; proto: ProtoNode | null }> = [];
    for (const [key, spec] of Object.entries(req.workspaces)) {
      // 1-based on purpose: the file speaks in the numbers on the keys the
      // user presses (⌘1..⌘5) and in the README's workspace table. A 0 here
      // is out of range and rejects the request, which is a far better
      // failure than 0-based keys silently targeting the wrong workspace.
      if (!/^\d+$/.test(key)) return null;
      const idx = Number(key) - 1;
      if (idx < 0 || idx >= count) return null;

      const proto = compileWorkspace(spec);
      if (proto === "invalid") return null;
      compiled.push({ idx, proto });
    }

    for (const { idx, proto } of compiled) {
      const ws = workspaces[idx];
      // Reconcile rather than rebuild: a pane whose type and params already
      // match keeps its leaf id, and an unchanged subtree comes back by
      // reference. Without this, re-applying the same file would remount
      // every pane — killing running ptys and losing terminal scrollback.
      const root = proto === null ? null : reconcile(proto, ws.root);

      // Keep the user's focus if the pane they were on is still there;
      // otherwise fall back to the first pane in the new tree.
      const focus =
        ws.focus && containsLeaf(root, ws.focus) ? ws.focus : firstLeafId(root);
      // Zoom is a view mode the user set by hand. Keep it when nothing
      // actually changed; clear it when the tree did, so a workspace cannot
      // stay zoomed on a pane that no longer exists.
      const zoom = root === ws.root ? ws.zoom : false;

      if (root === ws.root && focus === ws.focus && zoom === ws.zoom) continue;
      workspaces[idx] = { root, focus, zoom };
      touched = true;
    }
  }

  if (!touched && active === state.active) return null;
  return { workspaces, active };
}
