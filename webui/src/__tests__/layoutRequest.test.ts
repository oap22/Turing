// Agent-driven pane control (#388) — the `.layout.json` compiler.
//
// The acceptance criteria are the motivating case ("workspace 3 shows the
// agent panes, workspace 4 goes empty, no relaunch") and the safety rule that
// a corrupt file leaves the layout exactly as it was.

import { describe, expect, it } from "vitest";
import {
  applyLayoutRequest,
  parseLayoutRequest,
} from "../desktop/layoutRequest";
import {
  type LayoutState,
  type Node,
  defaultLayout,
  isValidLayoutState,
  seedIds,
} from "../desktop/layout";

function leaves(node: Node | null): Node[] {
  if (!node) return [];
  return node.kind === "leaf" ? [node] : [...leaves(node.a), ...leaves(node.b)];
}

function paneNames(state: LayoutState, ws: number): string[] {
  return leaves(state.workspaces[ws].root).map((n) =>
    n.kind === "leaf" ? n.pane : "",
  );
}

function allIds(state: LayoutState): string[] {
  return state.workspaces.flatMap((w) =>
    leaves(w.root).map((n) => (n.kind === "leaf" ? n.id : "")),
  );
}

describe("applyLayoutRequest — the motivating case", () => {
  it("sets up one workspace and empties another in a single file", () => {
    const before = defaultLayout();
    const next = applyLayoutRequest(before, {
      workspaces: {
        "3": {
          panes: [{ pane: "agents" }, { pane: "agentfeed" }],
          dir: "h",
          ratio: 0.65,
        },
        "4": null,
      },
    });
    expect(next).not.toBeNull();
    // ⌘3 is index 2, ⌘4 is index 3 — the file speaks in the numbers on the keys.
    expect(paneNames(next!, 2)).toEqual(["agents", "agentfeed"]);
    expect(next!.workspaces[3].root).toBeNull();
    expect(next!.workspaces[3].focus).toBeNull();
  });

  it("leaves workspaces the file does not mention alone", () => {
    const before = defaultLayout();
    const next = applyLayoutRequest(before, {
      workspaces: { "3": { panes: [{ pane: "agents" }] } },
    });
    // The terminals on ⌘1 must survive — that is the point of per-workspace
    // authority rather than whole-app replacement.
    expect(next!.workspaces[0]).toBe(before.workspaces[0]);
    expect(next!.workspaces[1]).toBe(before.workspaces[1]);
  });

  it("produces a state the shell's own validator accepts", () => {
    const next = applyLayoutRequest(defaultLayout(), {
      workspaces: { "1": { panes: [{ pane: "metrics" }, { pane: "images" }] } },
    });
    expect(isValidLayoutState(next)).toBe(true);
  });

  it("switches the visible workspace when asked", () => {
    const next = applyLayoutRequest(defaultLayout(), { active: 4 });
    expect(next?.active).toBe(3);
  });

  it("returns null when the request asks for nothing", () => {
    const before = defaultLayout();
    expect(applyLayoutRequest(before, {})).toBeNull();
    expect(
      applyLayoutRequest(before, { active: before.active + 1 }),
    ).toBeNull();
  });
});

describe("applyLayoutRequest — tree shapes", () => {
  it("builds an explicit nested tree with directions and ratios", () => {
    // Workspace 5 (index 4), which starts empty — asking for this exact shape
    // on ⌘2 would be a no-op, since it is what defaultLayout() already has.
    const next = applyLayoutRequest(defaultLayout(), {
      workspaces: {
        "5": {
          tree: {
            split: "h",
            ratio: 0.55,
            a: { pane: "metrics" },
            b: {
              split: "v",
              ratio: 0.5,
              a: { pane: "images" },
              b: { pane: "flywheel" },
            },
          },
        },
      },
    });
    const root = next!.workspaces[4].root!;
    expect(root.kind).toBe("split");
    expect(root).toMatchObject({ kind: "split", dir: "h", ratio: 0.55 });
    expect(paneNames(next!, 4)).toEqual(["metrics", "images", "flywheel"]);
  });

  it("is a no-op when the file asks for the layout already on screen", () => {
    // defaultLayout()'s ⌘2 is exactly this shape. Asking for it must change
    // nothing at all rather than rebuilding identical panes.
    expect(
      applyLayoutRequest(defaultLayout(), {
        workspaces: {
          "2": {
            tree: {
              split: "h",
              ratio: 0.55,
              a: { pane: "metrics" },
              b: {
                split: "v",
                ratio: 0.5,
                a: { pane: "images" },
                b: { pane: "flywheel" },
              },
            },
          },
        },
      }),
    ).toBeNull();
  });

  it("folds the panes shorthand into a spine", () => {
    const next = applyLayoutRequest(defaultLayout(), {
      workspaces: {
        "1": { panes: [{ pane: "term" }, { pane: "term" }, { pane: "term" }] },
      },
    });
    expect(paneNames(next!, 0)).toEqual(["term", "term", "term"]);
  });

  it("takes a single pane without a split", () => {
    const next = applyLayoutRequest(defaultLayout(), {
      workspaces: { "1": { panes: [{ pane: "obs" }] } },
    });
    expect(next!.workspaces[0].root).toMatchObject({
      kind: "leaf",
      pane: "obs",
    });
  });

  it("focuses the first pane of a replaced workspace", () => {
    const next = applyLayoutRequest(defaultLayout(), {
      workspaces: { "1": { panes: [{ pane: "agents" }, { pane: "term" }] } },
    });
    const first = leaves(next!.workspaces[0].root)[0];
    expect(next!.workspaces[0].focus).toBe(
      first.kind === "leaf" ? first.id : null,
    );
  });

  it("clears zoom so a workspace cannot stay zoomed on a pane that is gone", () => {
    const before = defaultLayout();
    before.workspaces[0] = { ...before.workspaces[0], zoom: true };
    const next = applyLayoutRequest(before, {
      workspaces: { "1": { panes: [{ pane: "term" }] } },
    });
    expect(next!.workspaces[0].zoom).toBe(false);
  });

  it("mints unique leaf ids that do not collide with the existing tree", () => {
    const before = defaultLayout();
    const next = applyLayoutRequest(before, {
      workspaces: {
        "3": { panes: [{ pane: "agents" }, { pane: "agentfeed" }] },
      },
    })!;
    const ids = allIds(next);
    expect(new Set(ids).size).toBe(ids.length);
    // And seeding stays consistent, so the next nextId() cannot reissue one.
    expect(() => seedIds(next)).not.toThrow();
  });
});

describe("applyLayoutRequest — malformed input is ignored whole", () => {
  const before = defaultLayout();

  it.each([
    ["not an object", "nope"],
    ["a null request", null],
    [
      "an unknown pane type",
      { workspaces: { "1": { panes: [{ pane: "nonsense" }] } } },
    ],
    [
      "a workspace number of 0",
      { workspaces: { "0": { panes: [{ pane: "term" }] } } },
    ],
    [
      "a workspace number past the end",
      { workspaces: { "9": { panes: [{ pane: "term" }] } } },
    ],
    [
      "a non-numeric workspace key",
      { workspaces: { three: { panes: [{ pane: "term" }] } } },
    ],
    [
      "a ratio above the drag limit",
      { workspaces: { "1": { panes: [{ pane: "term" }], ratio: 0.95 } } },
    ],
    [
      "a ratio below the drag limit",
      { workspaces: { "1": { panes: [{ pane: "term" }], ratio: 0 } } },
    ],
    [
      "a negative ratio",
      {
        workspaces: {
          "1": {
            tree: {
              split: "h",
              ratio: -1,
              a: { pane: "term" },
              b: { pane: "term" },
            },
          },
        },
      },
    ],
    [
      "a bad split direction",
      {
        workspaces: {
          "1": {
            tree: {
              split: "diagonal",
              a: { pane: "term" },
              b: { pane: "term" },
            },
          },
        },
      },
    ],
    [
      "a split missing a child",
      { workspaces: { "1": { tree: { split: "h", a: { pane: "term" } } } } },
    ],
    ["an empty pane list", { workspaces: { "1": { panes: [] } } }],
    [
      "both tree and panes",
      {
        workspaces: {
          "1": { tree: { pane: "term" }, panes: [{ pane: "term" }] },
        },
      },
    ],
    ["a workspace spec with neither key", { workspaces: { "1": {} } }],
    ["an active workspace out of range", { active: 9 }],
    ["a non-integer active", { active: 1.5 }],
    ["workspaces that isn't an object", { workspaces: [] }],
  ])("rejects %s", (_label, req) => {
    expect(applyLayoutRequest(before, req)).toBeNull();
  });

  it("rejects the whole request when only one workspace is bad", () => {
    // Never a half-applied tree: the good workspace must not land either.
    const next = applyLayoutRequest(before, {
      workspaces: {
        "3": { panes: [{ pane: "agents" }] },
        "4": { panes: [{ pane: "nonsense" }] },
      },
    });
    expect(next).toBeNull();
  });

  it("ignores unknown extra keys so the format can grow", () => {
    const next = applyLayoutRequest(before, {
      workspaces: { "1": { panes: [{ pane: "term" }], somethingNew: true } },
      alsoNew: 1,
    });
    expect(next).not.toBeNull();
    expect(paneNames(next!, 0)).toEqual(["term"]);
  });
});

describe("re-applying a file is genuinely a no-op", () => {
  // Panes are keyed by leaf id in the shell, so a fresh id unmounts and
  // remounts the pane. For a `term` that kills the pty and respawns an
  // autorun runner — an agent rewriting .layout.json every turn would kill
  // the test run it just started. Idempotency here is a hard requirement,
  // not a nicety.
  const req = {
    workspaces: {
      "3": {
        panes: [{ pane: "agents" }, { pane: "term", runnerId: "pytest" }],
      },
    },
  };

  it("returns null the second time, leaving the layout untouched", () => {
    const once = applyLayoutRequest(defaultLayout(), req)!;
    expect(once).not.toBeNull();
    expect(applyLayoutRequest(once, req)).toBeNull();
  });

  it("keeps the very same leaf ids across a re-apply", () => {
    const once = applyLayoutRequest(defaultLayout(), req)!;
    const idsBefore = allIds(once);
    const twice = applyLayoutRequest(once, req);
    // null means nothing changed at all, which is the strongest form of this.
    expect(twice).toBeNull();
    expect(allIds(once)).toEqual(idsBefore);
  });

  it("keeps the workspace object itself by reference", () => {
    const once = applyLayoutRequest(defaultLayout(), req)!;
    // `active: 2` is a real move (the default is workspace 1), so the request
    // does something — but the workspace it names must be reused as-is.
    const twice = applyLayoutRequest(once, { ...req, active: 2 })!;
    // Only `active` moved, so the workspace must be the same object — a new
    // object with equal contents would still remount nothing, but this
    // proves reconciliation reused the tree rather than rebuilding it.
    expect(twice.workspaces[2]).toBe(once.workspaces[2]);
  });

  it("preserves the pane the user focused, rather than snapping to the first", () => {
    const once = applyLayoutRequest(defaultLayout(), req)!;
    const second = leaves(once.workspaces[2].root)[1];
    const moved: LayoutState = {
      ...once,
      workspaces: once.workspaces.map((w, i) =>
        i === 2
          ? { ...w, focus: second.kind === "leaf" ? second.id : null }
          : w,
      ),
    };
    expect(applyLayoutRequest(moved, req)).toBeNull();
  });

  it("preserves a hand-set zoom when nothing changed", () => {
    const once = applyLayoutRequest(defaultLayout(), req)!;
    const zoomed: LayoutState = {
      ...once,
      workspaces: once.workspaces.map((w, i) =>
        i === 2 ? { ...w, zoom: true } : w,
      ),
    };
    expect(applyLayoutRequest(zoomed, req)).toBeNull();
  });

  it("still replaces panes that genuinely changed, and only those", () => {
    const once = applyLayoutRequest(defaultLayout(), req)!;
    const before = leaves(once.workspaces[2].root);
    // Swap only the second pane's type; the first must survive untouched.
    const next = applyLayoutRequest(once, {
      workspaces: { "3": { panes: [{ pane: "agents" }, { pane: "metrics" }] } },
    })!;
    const after = leaves(next.workspaces[2].root);
    expect(after[0]).toBe(before[0]);
    expect(after[1]).not.toBe(before[1]);
    expect(paneNames(next, 2)).toEqual(["agents", "metrics"]);
  });

  it("treats a changed runnerId as a different pane", () => {
    const once = applyLayoutRequest(defaultLayout(), req)!;
    const next = applyLayoutRequest(once, {
      workspaces: {
        "3": {
          panes: [
            { pane: "agents" },
            { pane: "term", runnerId: "pytest-full" },
          ],
        },
      },
    })!;
    const before = leaves(once.workspaces[2].root)[1];
    const after = leaves(next.workspaces[2].root)[1];
    expect(after).not.toBe(before);
    expect(after).toMatchObject({ params: { runnerId: "pytest-full" } });
  });

  it("treats a changed ratio as a change without remounting the panes", () => {
    const once = applyLayoutRequest(defaultLayout(), req)!;
    const next = applyLayoutRequest(once, {
      workspaces: { "3": { ...req.workspaces["3"], ratio: 0.7 } },
    })!;
    expect(next.workspaces[2].root).toMatchObject({ ratio: 0.7 });
    // The leaves are unchanged, so the panes keep their ids and their ptys.
    expect(allIds(next)).toEqual(allIds(once));
  });
});

describe("a hostile file cannot crash the shell", () => {
  // The reducer runs in React's render phase, outside the watcher's
  // try/catch and with no error boundary above it, so a throw here would
  // unmount the whole app — and re-crash on every relaunch, since the file
  // is still on disk.
  function deepTree(depth: number): unknown {
    let node: unknown = { pane: "term" };
    for (let i = 0; i < depth; i++)
      node = { split: "h", a: { pane: "term" }, b: node };
    return node;
  }

  it("rejects a tree past the depth cap instead of throwing", () => {
    const before = defaultLayout();
    for (const depth of [40, 1000, 20000]) {
      let result: unknown;
      expect(() => {
        result = applyLayoutRequest(before, {
          workspaces: { "1": { tree: deepTree(depth) } },
        });
      }).not.toThrow();
      expect(result).toBeNull();
    }
  });

  it("still accepts a tree of reasonable depth", () => {
    const next = applyLayoutRequest(defaultLayout(), {
      workspaces: { "1": { tree: deepTree(8) } },
    });
    expect(next).not.toBeNull();
  });

  it("rejects an absurd pane count instead of building it", () => {
    const panes = Array.from({ length: 5000 }, () => ({ pane: "term" }));
    expect(
      applyLayoutRequest(defaultLayout(), { workspaces: { "1": { panes } } }),
    ).toBeNull();
  });
});

describe("term panes take a runner id, never a command", () => {
  const before = defaultLayout();

  it("accepts an id from the runner table and passes it as params", () => {
    const next = applyLayoutRequest(before, {
      workspaces: { "1": { panes: [{ pane: "term", runnerId: "pytest" }] } },
    });
    expect(next!.workspaces[0].root).toMatchObject({
      kind: "leaf",
      pane: "term",
      params: { runnerId: "pytest" },
    });
  });

  it("rejects a runner id that is not in the table", () => {
    expect(
      applyLayoutRequest(before, {
        workspaces: {
          "1": { panes: [{ pane: "term", runnerId: "rm -rf /" }] },
        },
      }),
    ).toBeNull();
  });

  it("rejects a runner id on a pane that is not a term", () => {
    expect(
      applyLayoutRequest(before, {
        workspaces: {
          "1": { panes: [{ pane: "metrics", runnerId: "pytest" }] },
        },
      }),
    ).toBeNull();
  });

  it("gives a plain term pane no params at all", () => {
    const next = applyLayoutRequest(before, {
      workspaces: { "1": { panes: [{ pane: "term" }] } },
    });
    expect(next!.workspaces[0].root).toMatchObject({ params: undefined });
  });
});

describe("parseLayoutRequest", () => {
  it("parses an object", () => {
    expect(parseLayoutRequest('{"active":2}')).toEqual({ active: 2 });
  });

  it("returns null for a partial write rather than throwing", () => {
    // The watcher can read the file mid-write; that must be a quiet no-op.
    expect(
      parseLayoutRequest('{"workspaces": {"3": {"panes": [{"pane": "age'),
    ).toBeNull();
    expect(parseLayoutRequest("")).toBeNull();
    expect(parseLayoutRequest("[]")).toBeNull();
    expect(parseLayoutRequest("null")).toBeNull();
  });
});
