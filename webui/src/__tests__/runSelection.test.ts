// Run-selection state machine tests (issue #382), keyed to the numbered points
// in `.scratch/prd-metrics-run-comparison.md`.

import { describe, expect, it } from "vitest";
import type { RunFile } from "../desktop/panes/metrics";
import {
  activePaths,
  applyViewerRuns,
  atCap,
  autoPath,
  canPick,
  clearAll,
  INITIAL_SELECTION,
  isHeldByAuto,
  lineCount,
  MAX_LINES,
  removePath,
  toggleAuto,
  togglePin,
  type RunSelection,
} from "../desktop/panes/runSelection";

/** Newest first, the order the pane hands run files to this module. */
function runs(...labels: string[]): RunFile[] {
  return labels.map((label, i) => ({
    rel_path: `${label}/metrics.jsonl`,
    mtime_ms: 1000 - i,
  }));
}

const p = (label: string) => `${label}/metrics.jsonl`;

/** Eight run files, i.e. exactly enough to fill the cap. */
const eight = runs("r0", "r1", "r2", "r3", "r4", "r5", "r6", "r7");

describe("INITIAL_SELECTION (5.1)", () => {
  it("opens with auto on, nothing pinned, not cleared", () => {
    expect(INITIAL_SELECTION).toEqual({ auto: true, pinned: [], cleared: false });
  });
});

describe("autoPath (2.2, 2.5)", () => {
  it("resolves to the newest run file", () => {
    expect(autoPath(INITIAL_SELECTION, runs("a", "b"))).toBe(p("a"));
  });

  it("is null with auto off or with no runs at all", () => {
    expect(autoPath({ auto: false, pinned: [], cleared: false }, runs("a"))).toBeNull();
    expect(autoPath(INITIAL_SELECTION, [])).toBeNull();
  });

  it("follows a newly-arrived newest run, dropping the previous line", () => {
    const before = runs("a", "b");
    const after = runs("new", "a", "b");
    expect(activePaths(INITIAL_SELECTION, before)).toEqual([p("a")]);
    expect(activePaths(INITIAL_SELECTION, after)).toEqual([p("new")]);
  });

  it("keeps a promoted run across the handover", () => {
    const before = runs("a", "b");
    const promoted = togglePin(INITIAL_SELECTION, before, p("a"));
    expect(activePaths(promoted, runs("new", "a", "b"))).toEqual([p("a")]);
  });
});

describe("activePaths / lineCount", () => {
  it("puts auto's run first, then pins in pick order", () => {
    const files = runs("a", "b", "c");
    let sel = togglePin(INITIAL_SELECTION, files, p("c"));
    sel = togglePin(sel, files, p("b"));
    expect(activePaths(sel, files)).toEqual([p("a"), p("c"), p("b")]);
    expect(lineCount(sel, files)).toBe(3);
  });

  it("dedupes a run that auto holds and a pin also claims", () => {
    const files = runs("a", "b");
    const sel: RunSelection = { auto: true, pinned: [p("a")], cleared: false };
    expect(activePaths(sel, files)).toEqual([p("a")]);
    expect(lineCount(sel, files)).toBe(1);
  });

  it("draws nothing once cleared", () => {
    expect(activePaths(clearAll(INITIAL_SELECTION), runs("a"))).toEqual([]);
  });
});

describe("isHeldByAuto (2.3)", () => {
  it("is true only for the newest run while auto is on", () => {
    const files = runs("a", "b");
    expect(isHeldByAuto(INITIAL_SELECTION, files, p("a"))).toBe(true);
    expect(isHeldByAuto(INITIAL_SELECTION, files, p("b"))).toBe(false);
    expect(isHeldByAuto(toggleAuto(INITIAL_SELECTION, files), files, p("a"))).toBe(false);
  });
});

describe("togglePin", () => {
  it("promotes the auto-held run: auto off, run pinned (2.4)", () => {
    const files = runs("a", "b");
    expect(togglePin(INITIAL_SELECTION, files, p("a"))).toEqual({
      auto: false,
      pinned: [p("a")],
      cleared: false,
    });
  });

  it("promotes even at the cap, since the net line count is unchanged (2.4, 3.1)", () => {
    // auto + 7 pins = 8 lines; promoting auto's run is still 8.
    let sel = INITIAL_SELECTION;
    for (const f of eight.slice(1)) sel = togglePin(sel, eight, f.rel_path);
    expect(atCap(sel, eight)).toBe(true);

    const promoted = togglePin(sel, eight, p("r0"));
    expect(promoted.auto).toBe(false);
    expect(promoted.pinned).toContain(p("r0"));
    expect(lineCount(promoted, eight)).toBe(MAX_LINES);
  });

  it("refuses a ninth line at the cap (3.1, 3.2)", () => {
    const files = runs("r0", "r1", "r2", "r3", "r4", "r5", "r6", "r7", "r8");
    let sel = INITIAL_SELECTION;
    for (const f of files.slice(1, 8)) sel = togglePin(sel, files, f.rel_path);
    expect(lineCount(sel, files)).toBe(MAX_LINES);

    expect(togglePin(sel, files, p("r8"))).toBe(sel);
  });

  it("unpins a pinned run", () => {
    const files = runs("a", "b", "c");
    const sel = togglePin(togglePin(INITIAL_SELECTION, files, p("b")), files, p("c"));
    expect(togglePin(sel, files, p("b")).pinned).toEqual([p("c")]);
  });

  it("unpins rather than re-pinning a run that auto also holds", () => {
    const files = runs("a", "b");
    const sel: RunSelection = { auto: true, pinned: [p("a")], cleared: false };
    expect(togglePin(sel, files, p("a"))).toEqual({ auto: true, pinned: [], cleared: false });
  });
});

describe("canPick (3.1, 3.3)", () => {
  const nine = runs("r0", "r1", "r2", "r3", "r4", "r5", "r6", "r7", "r8");

  function filled(): RunSelection {
    let sel = INITIAL_SELECTION;
    for (const f of nine.slice(1, 8)) sel = togglePin(sel, nine, f.rel_path);
    return sel;
  }

  it("is true below the cap", () => {
    expect(canPick(INITIAL_SELECTION, nine, p("r3"))).toBe(true);
  });

  it("is false at the cap for a row that would add a line", () => {
    expect(canPick(filled(), nine, p("r8"))).toBe(false);
  });

  it("stays true at the cap for an unpin and for the auto promote", () => {
    expect(canPick(filled(), nine, p("r1"))).toBe(true);
    expect(canPick(filled(), nine, p("r0"))).toBe(true);
  });

  it("is false for a path with no run file", () => {
    expect(canPick(INITIAL_SELECTION, nine, "ghost/metrics.jsonl")).toBe(false);
  });
});

describe("toggleAuto", () => {
  it("turns auto off and back on", () => {
    const files = runs("a", "b");
    const off = toggleAuto(INITIAL_SELECTION, files);
    expect(off).toEqual({ auto: false, pinned: [], cleared: false });
    expect(toggleAuto(off, files)).toEqual({ auto: true, pinned: [], cleared: false });
  });

  it("refuses to turn on when that would be a ninth line", () => {
    let sel: RunSelection = { auto: false, pinned: [], cleared: false };
    for (const f of eight) sel = togglePin(sel, eight, f.rel_path);
    expect(lineCount(sel, eight)).toBe(MAX_LINES);
    // auto's run (r0) is already pinned here, so flipping auto on adds no line.
    expect(toggleAuto(sel, eight).auto).toBe(true);

    const nine = runs("new", ...["r0", "r1", "r2", "r3", "r4", "r5", "r6", "r7"]);
    expect(toggleAuto(sel, nine)).toBe(sel);
  });
});

describe("removePath (4.1, 4.2)", () => {
  it("switches auto off when the x lands on auto's line", () => {
    const files = runs("a", "b");
    const sel = togglePin(INITIAL_SELECTION, files, p("b"));
    expect(removePath(sel, files, p("a"))).toEqual({
      auto: false,
      pinned: [p("b")],
      cleared: false,
    });
  });

  it("drops a pinned run", () => {
    const files = runs("a", "b", "c");
    const sel = togglePin(togglePin(INITIAL_SELECTION, files, p("b")), files, p("c"));
    expect(removePath(sel, files, p("b"))).toEqual({
      auto: true,
      pinned: [p("c")],
      cleared: false,
    });
  });

  it("drops both claims when a run is auto's and pinned", () => {
    const files = runs("a", "b");
    const sel: RunSelection = { auto: true, pinned: [p("a"), p("b")], cleared: false };
    expect(removePath(sel, files, p("a"))).toEqual({
      auto: false,
      pinned: [p("b")],
      cleared: false,
    });
    expect(activePaths(removePath(sel, files, p("a")), files)).toEqual([p("b")]);
  });

  it("is a no-op for a run that isn't drawn", () => {
    const files = runs("a", "b");
    expect(removePath(INITIAL_SELECTION, files, p("b"))).toBe(INITIAL_SELECTION);
  });
});

describe("clearAll (4.3)", () => {
  it("empties everything, auto included, and goes sticky-empty", () => {
    const files = runs("a", "b");
    const sel = togglePin(INITIAL_SELECTION, files, p("b"));
    expect(clearAll(sel)).toEqual({ auto: false, pinned: [], cleared: true });
    expect(lineCount(clearAll(sel), files)).toBe(0);
  });
});

describe("applyViewerRuns (6.1, 6.4)", () => {
  const files = runs("a", "b", "c");

  it("replaces the selection with the file's runs, auto off", () => {
    const sel = togglePin(INITIAL_SELECTION, files, p("c"));
    expect(applyViewerRuns(sel, files, ["b", "c"])).toEqual({
      auto: false,
      pinned: [p("b"), p("c")],
      cleared: false,
    });
  });

  it("maps labels (not paths) through runLabelOf", () => {
    const nested: RunFile[] = [{ rel_path: "run-42/ckpt/metrics.json", mtime_ms: 1 }];
    expect(applyViewerRuns(INITIAL_SELECTION, nested, ["run-42"]).pinned).toEqual([
      "run-42/ckpt/metrics.json",
    ]);
  });

  it("keeps the first 8 in file order and drops the rest (6.4)", () => {
    const many = runs("r0", "r1", "r2", "r3", "r4", "r5", "r6", "r7", "r8", "r9");
    const labels = ["r9", "r8", "r7", "r6", "r5", "r4", "r3", "r2", "r1", "r0"];
    const sel = applyViewerRuns(INITIAL_SELECTION, many, labels);
    expect(sel.pinned).toEqual(labels.slice(0, MAX_LINES).map(p));
    expect(lineCount(sel, many)).toBe(MAX_LINES);
  });

  it("ignores labels that match no run file, and duplicates", () => {
    expect(applyViewerRuns(INITIAL_SELECTION, files, ["nope", "b", "b"]).pinned).toEqual([p("b")]);
  });

  it("leaves the selection alone when nothing in the list matches", () => {
    const sel = togglePin(INITIAL_SELECTION, files, p("b"));
    expect(applyViewerRuns(sel, files, ["nope", "also-nope"])).toBe(sel);
    expect(applyViewerRuns(sel, files, [])).toBe(sel);
  });
});

describe("sticky empty (6.2, 6.3)", () => {
  const files = runs("a", "b", "c");

  it("blocks .viewer.json after a clear", () => {
    const cleared = clearAll(togglePin(INITIAL_SELECTION, files, p("b")));
    expect(applyViewerRuns(cleared, files, ["c"])).toBe(cleared);
  });

  it("is released by a manual pin, handing control back to the file", () => {
    const cleared = clearAll(INITIAL_SELECTION);
    const picked = togglePin(cleared, files, p("c"));
    expect(picked).toEqual({ auto: false, pinned: [p("c")], cleared: false });
    expect(applyViewerRuns(picked, files, ["b"]).pinned).toEqual([p("b")]);
  });

  it("is released by toggling auto", () => {
    const released = toggleAuto(clearAll(INITIAL_SELECTION), files);
    expect(released).toEqual({ auto: true, pinned: [], cleared: false });
    expect(applyViewerRuns(released, files, ["b"]).pinned).toEqual([p("b")]);
  });

  it("is released by a legend x that actually removes a line", () => {
    const stuck: RunSelection = { auto: true, pinned: [], cleared: true };
    expect(removePath(stuck, files, p("a"))).toEqual({
      auto: false,
      pinned: [],
      cleared: false,
    });
  });
});

describe("purity", () => {
  it("never mutates the selection it is handed", () => {
    const files = runs("a", "b", "c");
    const sel: RunSelection = { auto: true, pinned: [p("b")], cleared: false };
    const snapshot = JSON.stringify(sel);
    togglePin(sel, files, p("c"));
    togglePin(sel, files, p("b"));
    toggleAuto(sel, files);
    removePath(sel, files, p("a"));
    clearAll(sel);
    applyViewerRuns(sel, files, ["c"]);
    activePaths(sel, files);
    expect(JSON.stringify(sel)).toBe(snapshot);
  });
});
