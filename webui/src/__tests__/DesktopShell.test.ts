// Reducer-wiring tests for `DesktopShell`'s `layoutReducer` — specifically,
// when `shouldPruneEmptyActiveWorkspace` is allowed to trigger
// `closeEmptyActiveWorkspace`. This is the layer the "fold an untouched
// workspace" bug actually lived in: `layout.ts`'s pure functions were never
// wrong, the *wiring* — first "prune on every workspace switch," then "prune
// on every action of a type that COULD have emptied something, regardless of
// whether it did" — was. See `closeEmptyActiveWorkspace` in `layout.ts` and
// `shouldPruneEmptyActiveWorkspace` in `DesktopShell.tsx` for the rule itself.

import { describe, expect, it } from "vitest";
import { layoutReducer, type ShellAction } from "../desktop/DesktopShell";
import {
  addWorkspace,
  defaultLayout,
  MAX_WORKSPACES,
  switchWs,
  type LayoutState,
  type Node,
} from "../desktop/layout";

function switchTo(i: number): ShellAction {
  return { type: "keymap", action: { type: "ws", i } };
}

// The leftmost leaf's id — enough to name a real, existing leaf to close
// without the test needing to know the exact shape `defaultLayout()` seeds.
function firstLeafId(node: Node | null): string {
  if (!node) throw new Error("workspace has no root");
  return node.kind === "leaf" ? node.id : firstLeafId(node.a);
}

describe("layoutReducer — pruning wiring", () => {
  // This is the bug report's `layoutRequest` repro: ⌘N creates and switches
  // to ws4 (index 3, empty), then an agent writes a `.layout.json` that only
  // edits workspace 1 and omits `active` entirely. `applyLayoutRequest`
  // leaves `active` right where it was — pointed at the still-empty ws4 — and
  // the old type-based trigger folded it away even though this action never
  // touched workspace 4 at all. Confirmed (per the task) to fail against the
  // pre-fix `pruningRelevant`-based wiring, which fires on every
  // `layoutRequest` regardless of what it changed.
  it("a layoutRequest editing a different workspace and omitting active leaves the empty trailing ws alone", () => {
    let state = defaultLayout();
    state = layoutReducer(state, { type: "keymap", action: { type: "newWs" } }); // ws4 (index 3), empty, active
    expect(state.workspaces).toHaveLength(4);
    expect(state.active).toBe(3);

    const next = layoutReducer(state, {
      type: "layoutRequest",
      // Workspace 1 only — no mention of "4", and `active` is omitted, so
      // this request cannot possibly be "the action that emptied ws4."
      req: { workspaces: { "1": { panes: [{ pane: "chat" }] } } },
    });

    expect(next.workspaces).toHaveLength(4);
    expect(next.active).toBe(3);
    expect(next.workspaces[3].root).toBeNull();
  });

  // Same root cause via the other action `pruningRelevant` used to treat as
  // automatically fold-worthy: `closeLeaf` targeting a leaf that lives in a
  // workspace other than the empty trailing one.
  it("a closeLeaf targeting a leaf in a different workspace leaves the empty trailing ws alone", () => {
    let state = defaultLayout();
    state = layoutReducer(state, { type: "keymap", action: { type: "newWs" } }); // ws4 (index 3), empty, active
    expect(state.workspaces).toHaveLength(4);

    const ws0Root = state.workspaces[0].root;
    const leafId = firstLeafId(ws0Root);

    const next = layoutReducer(state, {
      type: "closeLeaf",
      ws: 0,
      leafId,
    });

    expect(next.workspaces).toHaveLength(4);
    expect(next.workspaces[3].root).toBeNull();
  });

  // Regression guard: the intended case still works. Closing the last pane
  // that lives IN the active trailing workspace is exactly "this action
  // emptied it," and must still fold.
  it("closeLeaf on the last leaf of the active trailing workspace still folds it away", () => {
    let state: LayoutState = addWorkspace(defaultLayout()); // ws4, empty, active
    state = layoutReducer(state, { type: "openPane", pane: "term" }); // seed ws4 with one leaf
    expect(state.workspaces).toHaveLength(4);
    const leafId = firstLeafId(state.workspaces[3].root);

    const next = layoutReducer(state, { type: "closeLeaf", ws: 3, leafId });
    expect(next.workspaces).toHaveLength(3);
  });

  // Path (b): ⌘W is explicit user intent to dispose of whatever's focused,
  // and must still fold an already-empty extra workspace even though nothing
  // changed — `closeFocused` is a no-op when there's no leaf to close, so
  // this is the only way ⌘W ever closes a workspace as opposed to a pane.
  it("⌘W in an already-empty trailing extra workspace still folds it away", () => {
    let state = defaultLayout();
    state = layoutReducer(state, { type: "keymap", action: { type: "newWs" } }); // ws4, empty, active
    expect(state.workspaces).toHaveLength(4);

    const next = layoutReducer(state, { type: "keymap", action: { type: "close" } });
    expect(next.workspaces).toHaveLength(3);
    expect(next.active).toBe(2);
  });

  // The seeded three are permanent furniture — MIN_WORKSPACES guards against
  // ever going below them, even when one of them happens to be both empty
  // and last, which it is here: three workspaces total, all empty, standing
  // in the last one.
  it("⌘W in an empty seeded workspace (index < MIN_WORKSPACES) stays a no-op", () => {
    let state: LayoutState = {
      ...defaultLayout(),
      workspaces: [
        { root: null, focus: null, zoom: false },
        { root: null, focus: null, zoom: false },
        { root: null, focus: null, zoom: false },
      ],
    };
    state = switchWs(state, 2);
    const next = layoutReducer(state, { type: "keymap", action: { type: "close" } });
    expect(next.workspaces).toHaveLength(3);
    expect(next.active).toBe(2);
  });

  it("⌘N then a plain switch away and back: the created workspace survives", () => {
    // The bug this reducer wiring previously had (before it was tightened to
    // a type-based check and then, in this fix, to a before/after check):
    // switching away from a workspace ⌘N just created used to lift the one
    // thing protecting it (being active), destroying it before it was ever
    // touched. A plain "ws" switch was never in `pruningRelevant`'s list and
    // still isn't in `shouldPruneEmptyActiveWorkspace`'s, so this must keep
    // passing.
    let state = defaultLayout();
    state = layoutReducer(state, { type: "keymap", action: { type: "newWs" } });
    expect(state.workspaces).toHaveLength(4);
    expect(state.active).toBe(3);

    state = layoutReducer(state, switchTo(0));
    expect(state.workspaces).toHaveLength(4);

    state = layoutReducer(state, switchTo(3));
    expect(state.active).toBe(3);
    expect(state.workspaces).toHaveLength(4);
    expect(state.workspaces[3].root).toBeNull();
  });

  // A layoutRequest CAN be the genuine cause of the fold — when it actually
  // empties the workspace that is (still, unchanged) active, as opposed to
  // merely leaving `active` pointed at one that was already empty.
  it("a layoutRequest that actually empties the active workspace folds it away", () => {
    let state = defaultLayout();
    state = layoutReducer(state, { type: "keymap", action: { type: "newWs" } }); // ws4 (index 3), empty, active
    state = layoutReducer(state, { type: "openPane", pane: "term" }); // give ws4 real content
    expect(state.workspaces[3].root).not.toBeNull();

    const next = layoutReducer(state, {
      type: "layoutRequest",
      req: { workspaces: { "4": null } },
    });

    expect(next.workspaces).toHaveLength(3);
    expect(next.active).toBe(2);
  });

  it("⌘N at MAX_WORKSPACES is still a no-op returning the same reference", () => {
    let state = defaultLayout();
    while (state.workspaces.length < MAX_WORKSPACES) {
      state = layoutReducer(state, { type: "keymap", action: { type: "newWs" } });
    }
    expect(state.workspaces).toHaveLength(MAX_WORKSPACES);
    const next = layoutReducer(state, { type: "keymap", action: { type: "newWs" } });
    expect(next).toBe(state);
  });
});
