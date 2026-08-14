// Layout core tests (issue #382) — the tiling engine is pure, so every case
// here is state-in/state-out with no DOM.

import { describe, expect, it } from "vitest";
import {
  addWorkspace,
  closeEmptyActiveWorkspace,
  closeFocused,
  closeLeafById,
  defaultLayout,
  deserialize,
  emptyLayout,
  focusEffect,
  focusLeaf,
  isValidLayoutState,
  MAX_WORKSPACES,
  MIN_WORKSPACES,
  moveFocus,
  openPane,
  resize,
  rsiLayout,
  seedIds,
  sendToWs,
  serialize,
  swap,
  switchWs,
  toggleZoom,
  type LayoutState,
} from "../desktop/layout";

function withOneTerm(): LayoutState {
  return openPane(emptyLayout(), "term", undefined, "a");
}

describe("openPane dwindle", () => {
  it("becomes the root leaf in an empty workspace", () => {
    const state = withOneTerm();
    const ws = state.workspaces[0];
    expect(ws.root).toEqual({ kind: "leaf", id: "a", pane: "term", params: undefined });
    expect(ws.focus).toBe("a");
  });

  it("splits the focused leaf and focuses the new pane", () => {
    let state = withOneTerm();
    state = openPane(state, "term", undefined, "b");
    const ws = state.workspaces[0];
    expect(ws.root?.kind).toBe("split");
    expect(ws.focus).toBe("b");
    if (ws.root?.kind === "split") {
      expect(ws.root.a).toMatchObject({ id: "a" });
      expect(ws.root.b).toMatchObject({ id: "b" });
      // A 1600x900 nominal viewport is wider than tall, so the dwindle
      // algorithm should split it side-by-side ("h").
      expect(ws.root.dir).toBe("h");
      expect(ws.root.ratio).toBe(0.5);
    }
  });
});

describe("closeFocused", () => {
  it("promotes the sibling into the parent's slot", () => {
    let state = withOneTerm();
    state = openPane(state, "term", undefined, "b");
    // focus is "b"; closing it should leave just "a" as the root.
    state = closeFocused(state);
    const ws = state.workspaces[0];
    expect(ws.root).toEqual({ kind: "leaf", id: "a", pane: "term", params: undefined });
    expect(ws.focus).toBe("a");
  });

  it("allows an empty workspace when the sole leaf closes", () => {
    let state = withOneTerm();
    state = closeFocused(state);
    const ws = state.workspaces[0];
    expect(ws.root).toBeNull();
    expect(ws.focus).toBeNull();
  });

  it("clears zoom", () => {
    let state = withOneTerm();
    state = openPane(state, "term", undefined, "b");
    state = toggleZoom(state);
    expect(state.workspaces[0].zoom).toBe(true);
    state = closeFocused(state);
    expect(state.workspaces[0].zoom).toBe(false);
  });
});

describe("closeLeafById", () => {
  it("closes the focused leaf the same way closeFocused does", () => {
    let state = withOneTerm();
    state = openPane(state, "term", undefined, "b"); // focus is now "b"
    state = closeLeafById(state, 0, "b");
    const ws = state.workspaces[0];
    expect(ws.root).toEqual({ kind: "leaf", id: "a", pane: "term", params: undefined });
    expect(ws.focus).toBe("a");
  });

  it("closes a non-focused leaf, leaving focus untouched", () => {
    let state = withOneTerm(); // leaf "a" gets focus
    state = openPane(state, "term", undefined, "b"); // focus moves to "b"
    // Focus is "b"; close "a" (not focused) instead.
    state = closeLeafById(state, 0, "a");
    const ws = state.workspaces[0];
    expect(ws.root).toEqual({ kind: "leaf", id: "b", pane: "term", params: undefined });
    expect(ws.focus).toBe("b");
  });

  it("clears zoom only when the closed leaf was the focused one", () => {
    let state = withOneTerm();
    state = openPane(state, "term", undefined, "b"); // focus is "b"
    state = toggleZoom(state);
    expect(state.workspaces[0].zoom).toBe(true);
    // Close the non-focused leaf "a" — the zoomed (focused) pane "b" is
    // untouched, so zoom should survive.
    state = closeLeafById(state, 0, "a");
    expect(state.workspaces[0].zoom).toBe(true);
    expect(state.workspaces[0].focus).toBe("b");

    // Now close the focused leaf itself — zoom must clear.
    state = closeLeafById(state, 0, "b");
    expect(state.workspaces[0].zoom).toBe(false);
  });

  it("is a no-op for an out-of-range workspace index", () => {
    const state = withOneTerm();
    const result = closeLeafById(state, 99, "a");
    expect(result).toBe(state);
  });

  it("is a no-op for an id not present in that workspace's tree", () => {
    const state = withOneTerm();
    const result = closeLeafById(state, 0, "does-not-exist");
    expect(result).toBe(state);
  });

  it("handles any valid workspace index, not just the active one", () => {
    let state = emptyLayout();
    state = openPane({ ...state, active: 2 }, "term", undefined, "x");
    state = openPane({ ...state, active: 2 }, "term", undefined, "y");
    expect(state.active).toBe(2);
    // Operate on workspace 2 explicitly by index, independent of `active`.
    state = { ...state, active: 0 };
    state = closeLeafById(state, 2, "x");
    expect(state.workspaces[2].root).toEqual({ kind: "leaf", id: "y", pane: "term", params: undefined });
    expect(state.workspaces[0].root).toBeNull(); // untouched
  });
});

describe("moveFocus", () => {
  it("navigates an L-shape: left column stacked, right column single", () => {
    // Build: root h-split(a=top/bottom v-split of "top"/"bottom", b="right")
    let state = withOneTerm(); // "a" alone
    state = openPane(state, "term", undefined, "right"); // h-split: a | right, focus=right
    state = openPane(state, "term", undefined, "bottom"); // splits "right" (focused) -> right/bottom v-split
    // Layout: h-split( a , v-split( right, bottom ) )
    const ws = state.workspaces[0];
    expect(ws.focus).toBe("bottom");

    // From "bottom", moving up should land on "right".
    state = moveFocus(state, "up");
    expect(state.workspaces[0].focus).toBe("right");

    // From "right", moving left should land on "a".
    state = moveFocus(state, "left");
    expect(state.workspaces[0].focus).toBe("a");

    // No pane further left of "a" — no-op at the edge.
    const before = state.workspaces[0].focus;
    state = moveFocus(state, "left");
    expect(state.workspaces[0].focus).toBe(before);
  });
});

describe("swap", () => {
  it("swaps the focused leaf with its neighbor and follows it", () => {
    let state = withOneTerm();
    state = openPane(state, "term", undefined, "b"); // h-split a|b, focus b
    state = swap(state, "left");
    const ws = state.workspaces[0];
    expect(ws.focus).toBe("b");
    if (ws.root?.kind === "split") {
      // "b" is now on the left (where "a" used to be), "a" on the right.
      expect(ws.root.a).toMatchObject({ id: "b" });
      expect(ws.root.b).toMatchObject({ id: "a" });
    }
  });
});

describe("resize", () => {
  it("clamps ratio into [0.1, 0.9] and grows the focused side on +delta", () => {
    let state = withOneTerm();
    state = openPane(state, "term", undefined, "b"); // focus "b" is the `b` side
    state = resize(state, 0.35);
    let ws = state.workspaces[0];
    if (ws.root?.kind === "split") {
      // focus is "b" (the b side), so +delta shrinks `a`'s ratio.
      expect(ws.root.ratio).toBeCloseTo(0.15);
    }
    // Push far past the clamp bound.
    state = resize(state, -10);
    ws = state.workspaces[0];
    if (ws.root?.kind === "split") {
      expect(ws.root.ratio).toBe(0.9);
    }
  });
});

describe("sendToWs", () => {
  it("moves the focused pane into an empty target workspace", () => {
    let state = withOneTerm();
    state = sendToWs(state, 1);
    expect(state.workspaces[0].root).toBeNull();
    expect(state.active).toBe(0);
    expect(state.workspaces[1].root).toEqual({
      kind: "leaf",
      id: "a",
      pane: "term",
      params: undefined,
    });
  });

  it("dwindles into a non-empty target workspace", () => {
    let state = withOneTerm();
    state = openPane(state, "term", { seed: 1 }, "target-seed");
    state = sendToWs(state, 1); // ws1 is empty -> seed it
    state = openPane(state, "term", undefined, "a2");
    // now ws0 has "a2" focused; send it into ws1 (non-empty, has target-seed)
    state = sendToWs(state, 1);
    const ws1 = state.workspaces[1];
    expect(ws1.root?.kind).toBe("split");
  });

  it("is a no-op sending into the active workspace itself", () => {
    const state = withOneTerm();
    const next = sendToWs(state, 0);
    expect(next).toBe(state);
  });
});

describe("toggleZoom", () => {
  it("flips the active workspace's zoom flag", () => {
    let state = withOneTerm();
    expect(state.workspaces[0].zoom).toBe(false);
    state = toggleZoom(state);
    expect(state.workspaces[0].zoom).toBe(true);
    state = toggleZoom(state);
    expect(state.workspaces[0].zoom).toBe(false);
  });
});

describe("serialize / deserialize", () => {
  it("round-trips a populated layout", () => {
    let state = withOneTerm();
    state = openPane(state, "term", undefined, "b");
    const json = serialize(state);
    const back = deserialize(json);
    expect(back).toEqual(state);
  });

  it("returns null for garbage input", () => {
    expect(deserialize("not json")).toBeNull();
    expect(deserialize("{}")).toBeNull();
    expect(deserialize(JSON.stringify({ workspaces: [], active: 0 }))).toBeNull();
  });

  it("rejects an out-of-range `active` index", () => {
    const valid = withOneTerm();
    const withActive = (active: number) => JSON.stringify({ ...valid, active });
    expect(deserialize(withActive(99))).toBeNull();
    expect(deserialize(withActive(-1))).toBeNull();
    expect(deserialize(withActive(1.5))).toBeNull();
  });
});

describe("isValidLayoutState", () => {
  function withWorkspaceCount(n: number): Record<string, unknown> {
    return {
      workspaces: Array.from({ length: n }, () => ({
        root: null,
        focus: null,
        zoom: false,
      })),
      active: 0,
    };
  }

  it("accepts a legacy five-workspace blob — a returning user's layout is not migrated on load", () => {
    expect(isValidLayoutState(withWorkspaceCount(5))).toBe(true);
  });

  it("accepts the new three-workspace default", () => {
    expect(isValidLayoutState(withWorkspaceCount(3))).toBe(true);
  });

  it("rejects a length below MIN_WORKSPACES", () => {
    expect(isValidLayoutState(withWorkspaceCount(2))).toBe(false);
  });

  it("rejects a length above MAX_WORKSPACES", () => {
    expect(isValidLayoutState(withWorkspaceCount(10))).toBe(false);
  });

  it("bounds `active` against the actual array length, not a constant", () => {
    expect(isValidLayoutState({ ...withWorkspaceCount(5), active: 4 })).toBe(true);
    expect(isValidLayoutState({ ...withWorkspaceCount(5), active: 5 })).toBe(false);
    expect(isValidLayoutState({ ...withWorkspaceCount(3), active: 2 })).toBe(true);
    expect(isValidLayoutState({ ...withWorkspaceCount(3), active: 3 })).toBe(false);
  });
});

describe("defaultLayout", () => {
  it("seeds exactly three workspaces: terminals, results, agent debug — no empty filler workspace", () => {
    const state = defaultLayout();
    expect(state.workspaces).toHaveLength(3);
    expect(state.active).toBe(0);

    // ws0: two terminals, split h 0.5, focused on the first.
    const ws0 = state.workspaces[0];
    expect(ws0.root).toMatchObject({ kind: "split", dir: "h", ratio: 0.5 });
    if (ws0.root?.kind === "split" && ws0.root.a.kind === "leaf") {
      expect(ws0.root.a).toMatchObject({ kind: "leaf", pane: "term" });
      expect(ws0.root.b).toMatchObject({ kind: "leaf", pane: "term" });
      expect(ws0.focus).toBe(ws0.root.a.id);
    }

    // ws1: the results view — metrics | (images / flywheel).
    const ws1 = state.workspaces[1];
    expect(ws1.root).toMatchObject({ kind: "split", dir: "h", ratio: 0.55 });
    if (ws1.root?.kind === "split" && ws1.root.a.kind === "leaf") {
      expect(ws1.root.a).toMatchObject({ kind: "leaf", pane: "metrics" });
      expect(ws1.root.b).toMatchObject({ kind: "split", dir: "v", ratio: 0.5 });
      expect(ws1.focus).toBe(ws1.root.a.id);
      if (ws1.root.b.kind === "split") {
        expect(ws1.root.b.a).toMatchObject({ kind: "leaf", pane: "images" });
        expect(ws1.root.b.b).toMatchObject({ kind: "leaf", pane: "flywheel" });
      }
    }

    // ws2: the agent debug view — what used to live at ⌘4.
    const ws2 = state.workspaces[2];
    expect(ws2.root).toMatchObject({ kind: "split", dir: "h", ratio: 0.65 });
    if (ws2.root?.kind === "split" && ws2.root.a.kind === "leaf") {
      expect(ws2.root.a).toMatchObject({ kind: "leaf", pane: "agents" });
      expect(ws2.root.b).toMatchObject({ kind: "leaf", pane: "agentfeed" });
      expect(ws2.focus).toBe(ws2.root.a.id);
    }
  });
});

describe("emptyLayout", () => {
  it("seeds MIN_WORKSPACES empty workspaces, all-null and active on the first", () => {
    const state = emptyLayout();
    expect(state.workspaces).toHaveLength(MIN_WORKSPACES);
    expect(state.active).toBe(0);
    for (const ws of state.workspaces) {
      expect(ws).toEqual({ root: null, focus: null, zoom: false });
    }
  });
});

describe("addWorkspace", () => {
  it("appends one empty workspace and focuses it", () => {
    const before = defaultLayout();
    const after = addWorkspace(before);
    expect(after.workspaces).toHaveLength(before.workspaces.length + 1);
    expect(after.active).toBe(after.workspaces.length - 1);
    expect(after.workspaces[after.active]).toEqual({
      root: null,
      focus: null,
      zoom: false,
    });
    // Every existing workspace survives untouched.
    before.workspaces.forEach((ws, i) => expect(after.workspaces[i]).toBe(ws));
  });

  it("no-ops at MAX_WORKSPACES, returning the very same state object", () => {
    let state = defaultLayout();
    while (state.workspaces.length < MAX_WORKSPACES) {
      state = addWorkspace(state);
    }
    expect(state.workspaces).toHaveLength(MAX_WORKSPACES);
    expect(addWorkspace(state)).toBe(state);
  });
});

describe("closeEmptyActiveWorkspace", () => {
  // The rule this replaces used to sweep every trailing empty workspace
  // after *any* close or plain workspace switch. That swept away a
  // workspace ⌘N had just created the instant the user glanced elsewhere
  // (switching away lifted the only guard protecting it), and it silently
  // shrank a returning user's legacy five-workspace layout on their first
  // close or switch. The replacement only ever acts on the workspace the
  // user is currently standing in, and only when it is both last and empty.

  it("drops a trailing empty ACTIVE workspace above the floor", () => {
    let state = defaultLayout(); // 3 populated
    state = addWorkspace(state); // ws4 (index 3), empty, and active
    expect(state.active).toBe(3);
    const next = closeEmptyActiveWorkspace(state);
    expect(next.workspaces).toHaveLength(3);
    expect(next.active).toBe(2);
  });

  it("does NOT drop a trailing empty workspace that is not active", () => {
    let state = defaultLayout();
    state = addWorkspace(state); // ws4, empty, active
    state = switchWs(state, 0); // step off it — ws4 is now trailing, empty, and NOT active
    expect(state.workspaces).toHaveLength(4);
    const next = closeEmptyActiveWorkspace(state);
    expect(next).toBe(state); // same reference: nothing qualifies
    expect(next.workspaces).toHaveLength(4);
  });

  it("never drops below MIN_WORKSPACES even when the active workspace is empty", () => {
    // All-empty and already at the floor: nothing eligible to trim without
    // going under MIN_WORKSPACES, so this must be a true no-op.
    const state = emptyLayout();
    expect(state.workspaces).toHaveLength(MIN_WORKSPACES);
    expect(closeEmptyActiveWorkspace(state)).toBe(state);
  });

  it("does NOT drop a non-trailing empty workspace, even if it is active", () => {
    let state = defaultLayout();
    state = addWorkspace(state); // ws4, empty
    state = addWorkspace(state); // ws5, empty, active
    state = switchWs(state, 3); // ws4 (index 3) is now active, but not last
    const next = closeEmptyActiveWorkspace(state);
    expect(next).toBe(state);
    expect(next.workspaces).toHaveLength(5);
  });

  it("is the identity (same reference) when nothing qualifies", () => {
    const state = defaultLayout();
    expect(closeEmptyActiveWorkspace(state)).toBe(state);
  });

  it("leaves `active` pointing at a valid workspace after dropping", () => {
    let state = defaultLayout();
    state = addWorkspace(state);
    const next = closeEmptyActiveWorkspace(state);
    expect(next.active).toBeGreaterThanOrEqual(0);
    expect(next.active).toBeLessThan(next.workspaces.length);
  });
});

describe("seedIds", () => {
  it("bumps the id counter past ids already present in a restored tree, so the next mint can't collide", () => {
    // A leaf id far beyond anything the module's own counter would have
    // reached in this test run — stands in for ids minted by a *previous*
    // module instance (e.g. a prior page load) that this instance's
    // `idCounter`, restarting at 0, doesn't know about.
    let state = openPane(emptyLayout(), "term", undefined, "leaf-9999");
    const json = serialize(state);
    const restored = deserialize(json)!;
    expect(restored).not.toBeNull();

    seedIds(restored);

    // Mint a new leaf the same way DesktopShell does after a restore: via
    // openPane's default id (nextId()), not an explicit id.
    const next = openPane(restored, "term");
    const priorIds = new Set(["leaf-9999"]);
    const newLeafIds = new Set<string>();
    function collect(node: (typeof restored.workspaces)[number]["root"]): void {
      if (!node) return;
      if (node.kind === "leaf") {
        newLeafIds.add(node.id);
        return;
      }
      collect(node.a);
      collect(node.b);
    }
    collect(next.workspaces[next.active].root);

    // Exactly one new id was minted, and it must not collide with any id
    // already present in the restored tree.
    const added = [...newLeafIds].filter((id) => !priorIds.has(id));
    expect(added.length).toBe(1);
    expect(priorIds.has(added[0])).toBe(false);
    const m = /^leaf-(\d+)$/.exec(added[0]);
    expect(m).not.toBeNull();
    expect(Number(m![1])).toBeGreaterThan(9999);
  });
});

describe("focusLeaf", () => {
  it("focuses a leaf that exists in the active workspace", () => {
    const base = defaultLayout();
    const ws = base.workspaces[base.active];
    const ids: string[] = [];
    (function collect(n) {
      if (!n) return;
      if (n.kind === "leaf") return void ids.push(n.id);
      collect(n.a);
      collect(n.b);
    })(ws.root);
    const target = ids.find((id) => id !== ws.focus)!;

    const next = focusLeaf(base, target);
    expect(next.workspaces[next.active].focus).toBe(target);
    // Non-focus state is untouched.
    expect(next.workspaces[next.active].root).toBe(ws.root);
  });

  it("is a no-op for an id that is not in the active workspace", () => {
    const base = defaultLayout();
    expect(focusLeaf(base, "leaf-does-not-exist")).toBe(base);
  });

  it("is a no-op when the leaf is already focused", () => {
    const base = defaultLayout();
    const focused = base.workspaces[base.active].focus!;
    // Same object back, so React skips the re-render.
    expect(focusLeaf(base, focused)).toBe(base);
  });

  it("is a no-op on an empty workspace", () => {
    const base = emptyLayout();
    expect(focusLeaf(base, "leaf-1")).toBe(base);
  });
});

describe("focusEffect", () => {
  const state = defaultLayout();
  const focused = state.workspaces[state.active].focus!;

  it("never fires for passive transitions", () => {
    // Mount/restore, persistence, resize, viewport changes: the app must not
    // grab keyboard focus or yank the pointer on launch.
    expect(focusEffect(state, state, "passive")).toBeNull();
  });

  it("focuses and warps for keyboard navigation", () => {
    expect(focusEffect(state, state, "keyboard")).toEqual({ leafId: focused, warp: true });
  });

  it("focuses without warping for pointer input", () => {
    // The mouse is already where the user put it.
    expect(focusEffect(state, state, "pointer")).toEqual({ leafId: focused, warp: false });
  });

  it("fires even when the focused id is unchanged", () => {
    // `swap` keeps the same id focused while physically relocating it, so a
    // changed-id test would miss it.
    const swapped = swap(state, "right");
    expect(swapped.workspaces[swapped.active].focus).toBe(focused);
    expect(focusEffect(state, swapped, "keyboard")).toEqual({ leafId: focused, warp: true });
  });

  it("returns null when the target workspace has no focused leaf", () => {
    const empty = emptyLayout();
    expect(focusEffect(state, empty, "keyboard")).toBeNull();
  });

  it("follows focus across a workspace switch", () => {
    const moved = switchWs(state, 1);
    const nextFocus = moved.workspaces[moved.active].focus;
    const effect = focusEffect(state, moved, "keyboard");
    if (nextFocus) expect(effect).toEqual({ leafId: nextFocus, warp: true });
    else expect(effect).toBeNull();
  });
});

describe("rsiLayout", () => {
  it("is a valid layout state", () => {
    const state = rsiLayout("demo", "solve x");
    expect(isValidLayoutState(state)).toBe(true);
  });

  it("seeds ws0 with the loop terminal carrying rsi params, focused, workspace active", () => {
    const state = rsiLayout("demo", "solve x");
    expect(state.active).toBe(0);
    const ws0 = state.workspaces[0];
    expect(ws0.root?.kind).toBe("split");
    if (ws0.root?.kind !== "split") throw new Error("expected a split");
    expect(ws0.root.a.kind).toBe("leaf");
    if (ws0.root.a.kind !== "leaf") throw new Error("expected a leaf");
    expect(ws0.root.a.pane).toBe("term");
    expect(ws0.root.a.params?.rsi).toEqual({ slug: "demo", problem: "solve x" });
    expect(ws0.focus).toBe(ws0.root.a.id);
    // The scratch terminal alongside it carries no rsi params.
    expect(ws0.root.b.kind).toBe("leaf");
    if (ws0.root.b.kind === "leaf") {
      expect(ws0.root.b.pane).toBe("term");
      expect(ws0.root.b.params?.rsi).toBeUndefined();
    }
  });

  it("mirrors defaultLayout's ws1 (results) and ws2 (agent debug) shapes, and has exactly three workspaces", () => {
    const rsi = rsiLayout("demo", "solve x");
    const def = defaultLayout();
    expect(rsi.workspaces).toHaveLength(3);

    function paneShape(node: ReturnType<typeof rsiLayout>["workspaces"][number]["root"]): unknown {
      if (!node) return null;
      if (node.kind === "leaf") return node.pane;
      return { dir: node.dir, ratio: node.ratio, a: paneShape(node.a), b: paneShape(node.b) };
    }

    expect(paneShape(rsi.workspaces[1].root)).toEqual(paneShape(def.workspaces[1].root));
    expect(paneShape(rsi.workspaces[2].root)).toEqual(paneShape(def.workspaces[2].root));
  });

  it("preserves the leaf params of the loop terminal through serialize/deserialize", () => {
    const state = rsiLayout("demo", "solve x");
    const restored = deserialize(serialize(state))!;
    expect(restored.workspaces[0].root).toEqual(state.workspaces[0].root);
  });
});
