// The tiling core: a pure, framework-free binary-split layout per workspace
// (Hyprland/omarchy "dwindle" semantics). `DesktopShell.tsx` is the only
// consumer — it holds `LayoutState` in a `useReducer` and calls these
// functions to produce the next state. Every function here is pure: given
// the same inputs it returns the same (new) state, which is what makes the
// suite below exhaustive.

// The array is the single source of truth and `PaneType` derives from it, so
// anything that has to *validate* a pane name at runtime (the agent control
// file in `layoutRequest.ts`) checks against the same list the type is built
// from. Declaring the union separately would let the two drift apart, and the
// drift would only show up as a silently-rejected pane name.
import type { RsiConfig } from "./rsi";

export const PANE_TYPES = [
  "term",
  "queue",
  "chat",
  "obs",
  "metrics",
  "images",
  "flywheel",
  "agents",
  "agentfeed",
] as const;

export type PaneType = (typeof PANE_TYPES)[number];

export type Leaf = {
  kind: "leaf";
  id: string;
  pane: PaneType;
  params?: Record<string, unknown>;
};
export type Split = {
  kind: "split";
  dir: "h" | "v";
  ratio: number;
  a: Node;
  b: Node;
};
export type Node = Leaf | Split;
export type Workspace = {
  root: Node | null;
  focus: string | null;
  zoom: boolean;
};
export type LayoutState = { workspaces: Workspace[]; active: number };

export type Rect = { x: number; y: number; w: number; h: number };

// The workspace count is no longer fixed. `MIN_WORKSPACES` is the floor
// `closeEmptyActiveWorkspace()` will never cross — the three seeded
// workspaces below are always there even if every one of them is emptied
// out by hand.
// `MAX_WORKSPACES` is the ceiling `addWorkspace()` stops at: ⌘0 is already
// claimed by Home, so 9 is as far as a direct ⌘-digit chord can reach anyway.
export const MIN_WORKSPACES = 3;
export const MAX_WORKSPACES = 9;
// How many empty workspaces `emptyLayout()` seeds. Kept as its own name
// rather than reusing `MIN_WORKSPACES` at every call site: the two happen to
// be the same number today, but one is "the fewest you may ever prune down
// to" and the other is "how many a brand-new layout starts with" — different
// questions that should be free to diverge later without a silent coupling.
const DEFAULT_WORKSPACE_COUNT = MIN_WORKSPACES;
// A nominal 16:9 viewport used whenever layout math needs concrete pixels
// (dwindle split-direction choice, spatial focus navigation) but no real
// viewport is available (e.g. from a reducer action, off-screen).
const NOMINAL_VIEWPORT: Rect = { x: 0, y: 0, w: 1600, h: 900 };

let idCounter = 0;
export function nextId(): string {
  idCounter += 1;
  return `leaf-${idCounter}`;
}

export function emptyLayout(): LayoutState {
  const workspaces: Workspace[] = [];
  for (let i = 0; i < DEFAULT_WORKSPACE_COUNT; i++) {
    workspaces.push({ root: null, focus: null, zoom: false });
  }
  return { workspaces, active: 0 };
}

function leaf(
  pane: PaneType,
  id: string,
  params?: Record<string, unknown>,
): Leaf {
  return { kind: "leaf", id, pane, params };
}

function split(dir: "h" | "v", ratio: number, a: Node, b: Node): Split {
  return { kind: "split", dir, ratio, a, b };
}

// First-launch preset — three seeded workspaces, not five. ws0 is the two
// terminals you land on; ws1 is the results view (metrics beside
// images/flywheel); ws2 is the agent debug view (agents beside the agent
// feed), which used to sit at ⌘4 back when there was a fixed five-workspace
// spread. queue, chat and obs are still not preset at all — they stay
// reachable via the ⌘P launcher — and there is no seeded *empty* workspace
// anymore either: press ⌘N when you actually want a fourth one, and it
// folds away on its own (`closeEmptyActiveWorkspace`) the moment you close
// out of it while it is still empty.
// Existing persisted layouts (`localStorage["turing.sessions.v1"]`, and the
// legacy `turing.layout.v2`) are untouched by this default; it only applies
// when there's nothing to restore.
export function defaultLayout(): LayoutState {
  const state = emptyLayout();

  const t1 = leaf("term", nextId());
  const t2 = leaf("term", nextId());
  const ws0Root = split("h", 0.5, t1, t2);

  const metrics = leaf("metrics", nextId());
  const images = leaf("images", nextId());
  const flywheel = leaf("flywheel", nextId());
  const ws1Root = split("h", 0.55, metrics, split("v", 0.5, images, flywheel));

  const agents = leaf("agents", nextId());
  const agentfeed = leaf("agentfeed", nextId());
  const ws2Root = split("h", 0.65, agents, agentfeed);

  state.workspaces[0] = { root: ws0Root, focus: t1.id, zoom: false };
  state.workspaces[1] = { root: ws1Root, focus: metrics.id, zoom: false };
  state.workspaces[2] = { root: ws2Root, focus: agents.id, zoom: false };
  return state;
}

// The RSI-workstation seed. Mirrors `defaultLayout()`'s shape, but ws0 is
// handed over to the loop terminal: instead of two plain shells it gets a
// loop terminal (carrying the `rsi` params TermPane reads to pre-type
// `scripts/rsi-loop.sh`) alongside a scratch terminal for poking around the
// sandbox by hand. ws1 (metrics/images/flywheel) and ws2 (agents/agentfeed)
// are identical to `defaultLayout()`'s — those are the panes that watch the
// loop's results stream in, unchanged by what's driving it.
export function rsiLayout(slug: string, problem: string, config?: RsiConfig): LayoutState {
  const state = emptyLayout();

  const rsi = {
    slug,
    problem,
    ...(config?.engine ? { engine: config.engine } : {}),
    ...(config?.verifier ? { verifier: config.verifier } : {}),
  };
  const loopTerm = leaf("term", nextId(), { rsi });
  const scratchTerm = leaf("term", nextId());
  const ws0Root = split("h", 0.6, loopTerm, scratchTerm);

  const metrics = leaf("metrics", nextId());
  const images = leaf("images", nextId());
  const flywheel = leaf("flywheel", nextId());
  const ws1Root = split("h", 0.55, metrics, split("v", 0.5, images, flywheel));

  const agents = leaf("agents", nextId());
  const agentfeed = leaf("agentfeed", nextId());
  const ws2Root = split("h", 0.65, agents, agentfeed);

  state.workspaces[0] = { root: ws0Root, focus: loopTerm.id, zoom: false };
  state.workspaces[1] = { root: ws1Root, focus: metrics.id, zoom: false };
  state.workspaces[2] = { root: ws2Root, focus: agents.id, zoom: false };
  state.active = 0;
  return state;
}

export function rects(root: Node, viewport: Rect): Map<string, Rect> {
  const out = new Map<string, Rect>();
  function walk(node: Node, rect: Rect): void {
    if (node.kind === "leaf") {
      out.set(node.id, rect);
      return;
    }
    if (node.dir === "h") {
      const aw = rect.w * node.ratio;
      walk(node.a, { x: rect.x, y: rect.y, w: aw, h: rect.h });
      walk(node.b, { x: rect.x + aw, y: rect.y, w: rect.w - aw, h: rect.h });
    } else {
      const ah = rect.h * node.ratio;
      walk(node.a, { x: rect.x, y: rect.y, w: rect.w, h: ah });
      walk(node.b, { x: rect.x, y: rect.y + ah, w: rect.w, h: rect.h - ah });
    }
  }
  walk(root, viewport);
  return out;
}

function findLeaf(node: Node, id: string): Leaf | null {
  if (node.kind === "leaf") return node.id === id ? node : null;
  return findLeaf(node.a, id) ?? findLeaf(node.b, id);
}

function firstLeafId(node: Node): string {
  return node.kind === "leaf" ? node.id : firstLeafId(node.a);
}

function replaceLeafWithNode(node: Node, id: string, replacement: Node): Node {
  if (node.kind === "leaf") {
    return node.id === id ? replacement : node;
  }
  return {
    ...node,
    a: replaceLeafWithNode(node.a, id, replacement),
    b: replaceLeafWithNode(node.b, id, replacement),
  };
}

function mapLeaves(node: Node, replacements: Map<string, Leaf>): Node {
  if (node.kind === "leaf") {
    return replacements.get(node.id) ?? node;
  }
  return {
    ...node,
    a: mapLeaves(node.a, replacements),
    b: mapLeaves(node.b, replacements),
  };
}

// Remove a leaf, promoting its sibling into the parent's slot. Returns null
// when the removed leaf was the workspace's sole content.
function removeLeaf(node: Node, id: string): Node | null {
  if (node.kind === "leaf") {
    return node.id === id ? null : node;
  }
  if (node.a.kind === "leaf" && node.a.id === id) return node.b;
  if (node.b.kind === "leaf" && node.b.id === id) return node.a;
  const newA = removeLeaf(node.a, id);
  const newB = removeLeaf(node.b, id);
  if (newA === null) return newB;
  if (newB === null) return newA;
  if (newA !== node.a || newB !== node.b) return { ...node, a: newA, b: newB };
  return node;
}

function findParent(
  node: Node,
  id: string,
): { parent: Split; isA: boolean } | null {
  if (node.kind === "leaf") return null;
  if (node.a.kind === "leaf" && node.a.id === id)
    return { parent: node, isA: true };
  if (node.b.kind === "leaf" && node.b.id === id)
    return { parent: node, isA: false };
  return findParent(node.a, id) ?? findParent(node.b, id);
}

function withRatio(node: Node, target: Split, ratio: number): Node {
  if (node === target) return { ...node, ratio };
  if (node.kind === "leaf") return node;
  return {
    ...node,
    a: withRatio(node.a, target, ratio),
    b: withRatio(node.b, target, ratio),
  };
}

function withDir(node: Node, target: Split, dir: "h" | "v"): Node {
  if (node === target) return { ...node, dir };
  if (node.kind === "leaf") return node;
  return {
    ...node,
    a: withDir(node.a, target, dir),
    b: withDir(node.b, target, dir),
  };
}

function center(r: Rect): { x: number; y: number } {
  return { x: r.x + r.w / 2, y: r.y + r.h / 2 };
}

function clamp(n: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, n));
}

function updateWs(
  state: LayoutState,
  index: number,
  ws: Workspace,
): LayoutState {
  const workspaces = state.workspaces.slice();
  workspaces[index] = ws;
  return { ...state, workspaces };
}

function nearestLeafId(root: Node, fromRect: Rect): string | null {
  const rectMap = rects(root, NOMINAL_VIEWPORT);
  const fromCenter = center(fromRect);
  let best: string | null = null;
  let bestDist = Infinity;
  for (const [id, r] of rectMap) {
    const c = center(r);
    const dist = Math.hypot(c.x - fromCenter.x, c.y - fromCenter.y);
    if (dist < bestDist) {
      best = id;
      bestDist = dist;
    }
  }
  return best;
}

type Dir4 = "left" | "right" | "up" | "down";

function inDirection(
  dir: Dir4,
  from: { x: number; y: number },
  to: { x: number; y: number },
): boolean {
  if (dir === "left") return to.x < from.x;
  if (dir === "right") return to.x > from.x;
  if (dir === "up") return to.y < from.y;
  return to.y > from.y;
}

function perpendicularOverlap(dir: Dir4, a: Rect, b: Rect): number {
  if (dir === "left" || dir === "right") {
    return Math.max(0, Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y));
  }
  return Math.max(0, Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x));
}

// Picks the leaf `moveFocus`/`swap` should target: nearest center strictly in
// `dir` from the focused leaf, breaking ties by maximum perpendicular overlap.
function pickNeighbor(root: Node, focusId: string, dir: Dir4): string | null {
  const rectMap = rects(root, NOMINAL_VIEWPORT);
  const cur = rectMap.get(focusId);
  if (!cur) return null;
  const curCenter = center(cur);
  let best: string | null = null;
  let bestDist = Infinity;
  let bestOverlap = -Infinity;
  for (const [id, r] of rectMap) {
    if (id === focusId) continue;
    const c = center(r);
    if (!inDirection(dir, curCenter, c)) continue;
    const dist = Math.hypot(c.x - curCenter.x, c.y - curCenter.y);
    const overlap = perpendicularOverlap(dir, cur, r);
    if (
      dist < bestDist - 1e-9 ||
      (Math.abs(dist - bestDist) <= 1e-9 && overlap > bestOverlap)
    ) {
      best = id;
      bestDist = dist;
      bestOverlap = overlap;
    }
  }
  return best;
}

function insertLeafInto(
  root: Node | null,
  focusId: string | null,
  newLeaf: Leaf,
): { root: Node; focus: string } {
  if (!root) return { root: newLeaf, focus: newLeaf.id };
  const targetId =
    focusId && findLeaf(root, focusId) ? focusId : firstLeafId(root);
  const targetLeaf = findLeaf(root, targetId);
  if (!targetLeaf) return { root: newLeaf, focus: newLeaf.id };
  const rectMap = rects(root, NOMINAL_VIEWPORT);
  const r = rectMap.get(targetId) ?? NOMINAL_VIEWPORT;
  const dir: "h" | "v" = r.w >= r.h ? "h" : "v";
  const newSplit = split(dir, 0.5, targetLeaf, newLeaf);
  return {
    root: replaceLeafWithNode(root, targetId, newSplit),
    focus: newLeaf.id,
  };
}

export function openPane(
  state: LayoutState,
  pane: PaneType,
  params?: Record<string, unknown>,
  id: string = nextId(),
): LayoutState {
  const ws = state.workspaces[state.active];
  const newLeaf = leaf(pane, id, params);
  const { root, focus } = insertLeafInto(ws.root, ws.focus, newLeaf);
  return updateWs(state, state.active, { root, focus, zoom: ws.zoom });
}

export function closeFocused(state: LayoutState): LayoutState {
  const ws = state.workspaces[state.active];
  if (!ws.root || !ws.focus) return state;
  const closedRect = rects(ws.root, NOMINAL_VIEWPORT).get(ws.focus) ?? null;
  const newRoot = removeLeaf(ws.root, ws.focus);
  const newFocus = newRoot
    ? closedRect
      ? nearestLeafId(newRoot, closedRect)
      : firstLeafId(newRoot)
    : null;
  return updateWs(state, state.active, {
    root: newRoot,
    focus: newFocus,
    zoom: false,
  });
}

// Like `closeFocused`, but closes a specific leaf by id in a specific
// workspace, regardless of what's currently focused there — backs the ×
// button every pane's title row gets, which must be able to close *that*
// pane even when it isn't the focused one. Same sibling-promotion semantics;
// refocuses (nearest remaining leaf to the closed one's rect) only when the
// closed leaf was the one that had focus, otherwise the existing focus is
// left untouched. An invalid workspace index or an id not present in that
// workspace's tree is a no-op.
export function closeLeafById(
  state: LayoutState,
  wsIdx: number,
  leafId: string,
): LayoutState {
  if (wsIdx < 0 || wsIdx >= state.workspaces.length) return state;
  const ws = state.workspaces[wsIdx];
  if (!ws.root || !findLeaf(ws.root, leafId)) return state;

  const closedRect = rects(ws.root, NOMINAL_VIEWPORT).get(leafId) ?? null;
  const newRoot = removeLeaf(ws.root, leafId);
  const wasFocused = ws.focus === leafId;

  const newFocus = wasFocused
    ? newRoot
      ? closedRect
        ? nearestLeafId(newRoot, closedRect)
        : firstLeafId(newRoot)
      : null
    : ws.focus;

  return updateWs(state, wsIdx, {
    root: newRoot,
    focus: newFocus,
    zoom: wasFocused ? false : ws.zoom,
  });
}

// Focus a specific leaf in the active workspace by id. Backs click
// coherence: pointing at a pane makes it the focused pane, so the accent
// border tracks the mouse as well as the keyboard. A leaf id that isn't in
// the active workspace's tree is a no-op, as is refocusing what is already
// focused (returning the same object keeps React from re-rendering).
export function focusLeaf(state: LayoutState, leafId: string): LayoutState {
  const ws = state.workspaces[state.active];
  if (!ws.root || !findLeaf(ws.root, leafId)) return state;
  if (ws.focus === leafId) return state;
  return updateWs(state, state.active, { ...ws, focus: leafId });
}

// What caused a layout transition. This is the whole guard against stealing
// focus at the wrong moment, so it is tracked explicitly at the dispatch site
// rather than inferred from the state diff:
//
//   "keyboard" — a focus-moving key action. Moves DOM focus AND warps the
//                cursor, the way a tiling WM does.
//   "pointer"  — a click inside a pane. Moves DOM focus but never warps: the
//                mouse is already where the user put it, and yanking it to
//                the pane centre mid-click would be hostile.
//   "passive"  — everything else (initial mount/restore, persistence, resize,
//                zoom, split-direction toggle, viewport changes). Never
//                touches focus; this is what stops the app from grabbing the
//                pointer on launch or on every render.
export type FocusSource = "keyboard" | "pointer" | "passive";

export interface FocusEffect {
  leafId: string;
  warp: boolean;
}

// Decide whether a transition should move DOM focus (and the cursor).
//
// Deliberately does *not* require the focused leaf id to have changed:
// `swap` keeps the same id focused while physically relocating it, and a
// re-issued focus key should still re-assert DOM focus if something else
// (an overlay closing, a click elsewhere) took it away. `prev` is still
// taken so the rule can tighten later without touching call sites.
export function focusEffect(
  _prev: LayoutState,
  next: LayoutState,
  source: FocusSource,
): FocusEffect | null {
  if (source === "passive") return null;
  const leafId = next.workspaces[next.active]?.focus;
  if (!leafId) return null;
  return { leafId, warp: source === "keyboard" };
}

export function moveFocus(state: LayoutState, dir: Dir4): LayoutState {
  const ws = state.workspaces[state.active];
  if (!ws.root || !ws.focus) return state;
  const target = pickNeighbor(ws.root, ws.focus, dir);
  if (!target) return state;
  return updateWs(state, state.active, { ...ws, focus: target });
}

export function swap(state: LayoutState, dir: Dir4): LayoutState {
  const ws = state.workspaces[state.active];
  if (!ws.root || !ws.focus) return state;
  const targetId = pickNeighbor(ws.root, ws.focus, dir);
  if (!targetId) return state;
  const focusLeaf = findLeaf(ws.root, ws.focus);
  const targetLeaf = findLeaf(ws.root, targetId);
  if (!focusLeaf || !targetLeaf) return state;
  const replacements = new Map<string, Leaf>([
    [ws.focus, targetLeaf],
    [targetId, focusLeaf],
  ]);
  const newRoot = mapLeaves(ws.root, replacements);
  return updateWs(state, state.active, {
    ...ws,
    root: newRoot,
    focus: ws.focus,
  });
}

export function resize(state: LayoutState, delta: number): LayoutState {
  const ws = state.workspaces[state.active];
  if (!ws.root || !ws.focus) return state;
  const found = findParent(ws.root, ws.focus);
  if (!found) return state;
  const { parent, isA } = found;
  const newRatio = clamp(parent.ratio + (isA ? delta : -delta), 0.1, 0.9);
  const newRoot = withRatio(ws.root, parent, newRatio);
  return updateWs(state, state.active, { ...ws, root: newRoot });
}

export function toggleDir(state: LayoutState): LayoutState {
  const ws = state.workspaces[state.active];
  if (!ws.root || !ws.focus) return state;
  const found = findParent(ws.root, ws.focus);
  if (!found) return state;
  const { parent } = found;
  const newRoot = withDir(ws.root, parent, parent.dir === "h" ? "v" : "h");
  return updateWs(state, state.active, { ...ws, root: newRoot });
}

export function toggleZoom(state: LayoutState): LayoutState {
  const ws = state.workspaces[state.active];
  return updateWs(state, state.active, { ...ws, zoom: !ws.zoom });
}

// Creates a workspace beyond the seeded three and switches to it in one
// step: pressing ⌘N is "I want a place to put something new," not "add a
// row to the header strip," so landing anywhere else would just make the
// user press ⌘9 (or whatever the new index is) right afterward. No-ops —
// returning the very same `state` object — at `MAX_WORKSPACES`, which lets a
// caller tell "did this do anything" with `===` instead of re-deriving it
// from array length.
export function addWorkspace(state: LayoutState): LayoutState {
  if (state.workspaces.length >= MAX_WORKSPACES) return state;
  const workspaces = [
    ...state.workspaces,
    { root: null, focus: null, zoom: false },
  ];
  return { workspaces, active: workspaces.length - 1 };
}

// Folds away the single workspace the user just emptied out of, and nothing
// else. This replaces an earlier version that swept *every* trailing empty
// workspace after *any* close or plain workspace switch, which had two bugs
// baked into the rule itself: ⌘N followed by glancing at ⌘1 destroyed the
// workspace ⌘N had just created (switching away lifted the "never drop
// active" guard, so the fresh empty ws4 vanished the instant it stopped
// being active — before the user had touched it at all), and a legacy
// five-workspace layout lost ws5 and then ws4 on the very first close or
// switch after restoring it, because a plain switch was enough to trigger
// the sweep.
//
// The fix is to prune only in direct response to the one action that can
// make a *specific* workspace disposable: closing something out of it. The
// condition is narrow on purpose — active, last, and empty — because that is
// the only shape that is unambiguous: the workspace the user is standing in,
// at the end of the row, with nothing left in it. Nothing about switching
// workspaces, and nothing about any workspace other than the active one, is
// ever enough to trigger this. `MIN_WORKSPACES` is still the floor — the
// seeded three never fold away no matter how empty they get.
//
// This one rule is also what makes ⌘W double as "close the workspace" when
// there is no pane left to close: ⌘W closes the focused *thing*, and when a
// workspace is empty there is no leaf to focus, so `closeFocused` is
// already a no-op there — the workspace itself is the thing left to close,
// and this is what closes it. Same call, same helper, no special case.
//
// Returns the same `state` object when the active workspace does not
// qualify, which is what lets this be called unconditionally after every
// action that could plausibly have just emptied it — see `DesktopShell.tsx`
// — without forcing an extra re-render each time.
export function closeEmptyActiveWorkspace(state: LayoutState): LayoutState {
  const lastIndex = state.workspaces.length - 1;
  if (
    state.workspaces.length <= MIN_WORKSPACES ||
    state.active !== lastIndex ||
    state.workspaces[lastIndex].root !== null
  ) {
    return state;
  }
  return {
    workspaces: state.workspaces.slice(0, lastIndex),
    active: lastIndex - 1,
  };
}

export function switchWs(state: LayoutState, i: number): LayoutState {
  if (i < 0 || i >= state.workspaces.length) return state;
  return { ...state, active: i };
}

export function sendToWs(state: LayoutState, i: number): LayoutState {
  if (i < 0 || i >= state.workspaces.length || i === state.active) return state;
  const ws = state.workspaces[state.active];
  if (!ws.root || !ws.focus) return state;
  const movingLeaf = findLeaf(ws.root, ws.focus);
  if (!movingLeaf) return state;

  const closedRect = rects(ws.root, NOMINAL_VIEWPORT).get(ws.focus) ?? null;
  const newSrcRoot = removeLeaf(ws.root, ws.focus);
  const newSrcFocus = newSrcRoot
    ? closedRect
      ? nearestLeafId(newSrcRoot, closedRect)
      : firstLeafId(newSrcRoot)
    : null;
  const srcWs: Workspace = {
    root: newSrcRoot,
    focus: newSrcFocus,
    zoom: false,
  };

  const targetWs = state.workspaces[i];
  const { root: newTargetRoot, focus: newTargetFocus } = insertLeafInto(
    targetWs.root,
    targetWs.focus,
    movingLeaf,
  );

  const workspaces = state.workspaces.map((w, idx) => {
    if (idx === state.active) return srcWs;
    if (idx === i)
      return {
        root: newTargetRoot,
        focus: newTargetFocus,
        zoom: targetWs.zoom,
      };
    return w;
  });
  return { ...state, workspaces };
}

export function serialize(state: LayoutState): string {
  return JSON.stringify(state);
}

function isNode(x: unknown): x is Node {
  if (!x || typeof x !== "object") return false;
  const o = x as Record<string, unknown>;
  if (o.kind === "leaf") {
    return typeof o.id === "string" && typeof o.pane === "string";
  }
  if (o.kind === "split") {
    return (
      (o.dir === "h" || o.dir === "v") &&
      typeof o.ratio === "number" &&
      isNode(o.a) &&
      isNode(o.b)
    );
  }
  return false;
}

function isWorkspace(x: unknown): x is Workspace {
  if (!x || typeof x !== "object") return false;
  const o = x as Record<string, unknown>;
  const rootOk = o.root === null || isNode(o.root);
  const focusOk = o.focus === null || typeof o.focus === "string";
  return rootOk && focusOk && typeof o.zoom === "boolean";
}

// Exported so `sessions.ts` can validate a layout that arrives already parsed
// (nested inside a stored session) without re-serializing it just to hand it
// to `deserialize`, and without growing a second definition of "is this a
// layout" that could drift from this one.
// Accepts any workspace count from `MIN_WORKSPACES` through `MAX_WORKSPACES`
// inclusive — not just `DEFAULT_WORKSPACE_COUNT`. This is deliberately NOT a
// migration point: a layout persisted back when the preset seeded five
// workspaces validates and loads exactly as it was written, five workspaces
// and all. Nothing here truncates or pads an existing layout to the new
// default; only `defaultLayout()`/`rsiLayout()` (brand-new layouts, nothing
// to restore) seed three. Dropping a returning user's fourth and fifth
// workspace on load would be silent data loss dressed up as a migration.
export function isValidLayoutState(x: unknown): x is LayoutState {
  if (!x || typeof x !== "object") return false;
  const o = x as Record<string, unknown>;
  if (
    !Array.isArray(o.workspaces) ||
    o.workspaces.length < MIN_WORKSPACES ||
    o.workspaces.length > MAX_WORKSPACES
  )
    return false;
  if (!o.workspaces.every(isWorkspace)) return false;
  return (
    Number.isInteger(o.active) &&
    (o.active as number) >= 0 &&
    (o.active as number) < o.workspaces.length
  );
}

export function deserialize(s: string): LayoutState | null {
  try {
    const parsed: unknown = JSON.parse(s);
    return isValidLayoutState(parsed) ? parsed : null;
  } catch {
    return null;
  }
}

// After restoring a persisted `LayoutState` (whose leaf ids were minted by a
// previous module instance), bump the module-local id counter past the
// highest `leaf-<n>` id already present in the tree. Without this, a fresh
// page load's `idCounter` restarts at 0 and the first `nextId()` call mints
// an id that already exists in the restored tree (duplicate React keys,
// misrouted tree edits via `replaceLeafWithNode`/`mapLeaves`, which key off
// leaf id). `defaultLayout()` mints its own fresh ids and needs no seeding.
export function seedIds(state: LayoutState): void {
  let maxSeen = 0;
  function walk(node: Node | null): void {
    if (!node) return;
    if (node.kind === "leaf") {
      const m = /^leaf-(\d+)$/.exec(node.id);
      if (m) maxSeen = Math.max(maxSeen, Number(m[1]));
      return;
    }
    walk(node.a);
    walk(node.b);
  }
  for (const ws of state.workspaces) walk(ws.root);
  if (maxSeen > idCounter) idCounter = maxSeen;
}
