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

/**
 * Compile one node spec into a real `Node`, minting ids through `nextId()`.
 *
 * Returns `null` on anything malformed, which propagates all the way up: a
 * request is applied whole or not at all. A half-applied tree is the one
 * outcome worse than ignoring the file.
 */
function compileNode(spec: unknown): Node | null {
  if (!isObj(spec)) return null;

  if ("split" in spec) {
    if (spec.split !== "h" && spec.split !== "v") return null;
    if (spec.ratio !== undefined && !validRatio(spec.ratio)) return null;
    const a = compileNode(spec.a);
    const b = compileNode(spec.b);
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

  return { kind: "leaf", id: nextId(), pane: spec.pane, params };
}

/** Fold a flat pane list into a spine, nesting rightward. */
function compileSpine(
  specs: unknown,
  dir: "h" | "v",
  ratio: number,
): Node | null {
  if (!Array.isArray(specs) || specs.length === 0) return null;
  const nodes: Node[] = [];
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

function compileWorkspace(spec: unknown): Node | null | "invalid" {
  // Explicit null empties the workspace — distinct from omitting the key,
  // which leaves it untouched.
  if (spec === null) return null;
  if (!isObj(spec)) return "invalid";

  if (spec.tree !== undefined && spec.panes !== undefined) return "invalid";
  if (spec.dir !== undefined && spec.dir !== "h" && spec.dir !== "v")
    return "invalid";
  if (spec.ratio !== undefined && !validRatio(spec.ratio)) return "invalid";

  if (spec.tree !== undefined) return compileNode(spec.tree) ?? "invalid";

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
    for (const [key, spec] of Object.entries(req.workspaces)) {
      // 1-based on purpose: the file speaks in the numbers on the keys the
      // user presses (⌘1..⌘5) and in the README's workspace table. A 0 here
      // is out of range and rejects the request, which is a far better
      // failure than 0-based keys silently targeting the wrong workspace.
      if (!/^\d+$/.test(key)) return null;
      const idx = Number(key) - 1;
      if (idx < 0 || idx >= count) return null;

      const root = compileWorkspace(spec);
      if (root === "invalid") return null;

      // Zoom is a view mode the user set by hand; replacing a tree should not
      // silently leave the workspace zoomed on a pane that no longer exists.
      workspaces[idx] = { root, focus: firstLeafId(root), zoom: false };
      touched = true;
    }
  }

  if (!touched && active === state.active) return null;
  return { workspaces, active };
}
