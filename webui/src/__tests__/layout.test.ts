// Layout core tests (issue #382) — the tiling engine is pure, so every case
// here is state-in/state-out with no DOM.

import { describe, expect, it } from "vitest";
import {
  closeFocused,
  closeLeafById,
  defaultLayout,
  deserialize,
  emptyLayout,
  focusEffect,
  focusLeaf,
  moveFocus,
  openPane,
  resize,
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

describe("defaultLayout", () => {
  it("leaves ws2 (⌘3) empty on first launch; queue/chat/obs stay reachable via the launcher, not preset here", () => {
    const state = defaultLayout();
    expect(state.workspaces).toHaveLength(5);
    expect(state.workspaces[2]).toEqual({ root: null, focus: null, zoom: false });
    // The other presets are unchanged: ws0/ws1/ws3 populated, ws4 empty.
    expect(state.workspaces[0].root).not.toBeNull();
    expect(state.workspaces[1].root).not.toBeNull();
    expect(state.workspaces[3].root).not.toBeNull();
    expect(state.workspaces[4]).toEqual({ root: null, focus: null, zoom: false });
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
