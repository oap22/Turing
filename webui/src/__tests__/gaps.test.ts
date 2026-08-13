// Render-layer gap math. The tiling engine itself stays gap-free (see
// layout.test.ts, which is pixel-exact against the raw viewport); these cases
// cover the inset `DesktopShell` applies on top of it.

import { describe, expect, it } from "vitest";
import { applyGaps, GAPS_IN_PX, GAPS_OUT_PX } from "../desktop/gaps";
import { rects } from "../desktop/layout";
import type { Node } from "../desktop/layout";

const VIEWPORT = { x: 0, y: 0, w: 1600, h: 900 };

describe("applyGaps", () => {
  it("shrinks the rect by the inset on every side", () => {
    expect(applyGaps({ x: 10, y: 20, w: 100, h: 200 }, 8)).toEqual({
      x: 18,
      y: 28,
      w: 84,
      h: 184,
    });
  });

  it("is a no-op at zero inset", () => {
    const r = { x: 3, y: 4, w: 5, h: 6 };
    expect(applyGaps(r, 0)).toEqual(r);
  });

  it("clamps to zero instead of inverting when the rect is smaller than the gap", () => {
    expect(applyGaps({ x: 0, y: 0, w: 4, h: 2 }, 5)).toEqual({ x: 5, y: 5, w: 0, h: 0 });
  });

  it("does not mutate its input", () => {
    const r = { x: 1, y: 1, w: 10, h: 10 };
    applyGaps(r, 2);
    expect(r).toEqual({ x: 1, y: 1, w: 10, h: 10 });
  });
});

describe("gaps composed the way DesktopShell composes them", () => {
  const tree: Node = {
    kind: "split",
    dir: "h",
    ratio: 0.5,
    a: { kind: "leaf", id: "a", pane: "term" },
    b: { kind: "leaf", id: "b", pane: "term" },
  };

  it("leaves exactly GAPS_OUT_PX between the outermost panes and the window edge", () => {
    const tiled = applyGaps(VIEWPORT, GAPS_OUT_PX);
    const map = rects(tree, tiled);
    const a = applyGaps(map.get("a")!, GAPS_IN_PX / 2);
    const b = applyGaps(map.get("b")!, GAPS_IN_PX / 2);

    // Outer edge = outer gap + the pane's own half of the inner gap.
    const outer = GAPS_OUT_PX + GAPS_IN_PX / 2;
    expect(a.x).toBe(VIEWPORT.x + outer);
    expect(a.y).toBe(VIEWPORT.y + outer);
    expect(VIEWPORT.x + VIEWPORT.w - (b.x + b.w)).toBe(outer);
    expect(VIEWPORT.y + VIEWPORT.h - (b.y + b.h)).toBe(outer);
  });

  it("leaves exactly GAPS_IN_PX between two adjacent panes", () => {
    const map = rects(tree, applyGaps(VIEWPORT, GAPS_OUT_PX));
    const a = applyGaps(map.get("a")!, GAPS_IN_PX / 2);
    const b = applyGaps(map.get("b")!, GAPS_IN_PX / 2);
    expect(b.x - (a.x + a.w)).toBe(GAPS_IN_PX);
  });

  it("insets a zoomed pane by the same amount as a tiled one", () => {
    const zoomed = applyGaps(applyGaps(VIEWPORT, GAPS_OUT_PX), GAPS_IN_PX / 2);
    expect(zoomed.x).toBe(GAPS_OUT_PX + GAPS_IN_PX / 2);
    expect(zoomed.w).toBe(VIEWPORT.w - 2 * (GAPS_OUT_PX + GAPS_IN_PX / 2));
  });
});
