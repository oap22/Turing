// Flywheel wheel geometry (#390 part 2). The issue's constraints are the
// spec: legible in a small tiled pane at arbitrary aspect ratios, sane at 2
// rounds and at 50, and never a degenerate path.

import { describe, expect, it } from "vitest";
import { wedgePath, wheelGeometry } from "../desktop/panes/wheel";

const seq = (n: number) => Array.from({ length: n }, (_, i) => i);

function radiusOf(g: { cx: number; cy: number }, x: number, y: number): number {
  return Math.hypot(x - g.cx, y - g.cy);
}

describe("wheelGeometry", () => {
  it("makes one segment per round", () => {
    const g = wheelGeometry(seq(6), 400, 400)!;
    expect(g.segments).toHaveLength(6);
    expect(g.segments.map((s) => s.ordinal)).toEqual([0, 1, 2, 3, 4, 5]);
  });

  it("carries the round's own index, not just its position", () => {
    // Loop dirs can start at a non-zero round; identity must survive.
    const g = wheelGeometry([4, 5, 6], 400, 400)!;
    expect(g.segments.map((s) => s.index)).toEqual([4, 5, 6]);
  });

  it("starts at 12 o'clock and runs clockwise", () => {
    const g = wheelGeometry(seq(4), 400, 400)!;
    expect(g.segments[0].startAngle).toBeCloseTo(-Math.PI / 2);
    expect(g.segments[1].startAngle).toBeGreaterThan(g.segments[0].startAngle);
  });

  it("never lets a wedge span a full turn, even with one round", () => {
    // A 2π wedge puts the arc's start and end on the same point, which SVG
    // renders as nothing at all.
    for (const n of [1, 2, 3]) {
      const g = wheelGeometry(seq(n), 400, 400)!;
      for (const s of g.segments) {
        expect(s.endAngle - s.startAngle).toBeLessThan(Math.PI * 2);
        expect(s.endAngle).toBeGreaterThan(s.startAngle);
      }
    }
  });

  it("keeps wedges non-overlapping and ordered", () => {
    const g = wheelGeometry(seq(9), 400, 400)!;
    for (let i = 1; i < g.segments.length; i++) {
      expect(g.segments[i].startAngle).toBeGreaterThanOrEqual(
        g.segments[i - 1].endAngle,
      );
    }
  });

  it("fits the short side in a letterbox rather than overflowing it", () => {
    // The pane can be dragged to any aspect ratio; a circle sized off the
    // long side would be clipped top and bottom.
    const g = wheelGeometry(seq(5), 900, 300)!;
    expect(g.rOuter).toBeLessThanOrEqual(150);
    expect(g.cy - g.rOuter).toBeGreaterThanOrEqual(0);
    expect(g.cy + g.rOuter).toBeLessThanOrEqual(300);
  });

  it("returns null when the box is too small to be legible", () => {
    // The caller keeps the scannable list instead of drawing a smudge.
    expect(wheelGeometry(seq(5), 40, 40)).toBeNull();
    expect(wheelGeometry(seq(5), 900, 30)).toBeNull();
    expect(wheelGeometry(seq(5), 0, 0)).toBeNull();
    expect(wheelGeometry(seq(5), NaN, NaN)).toBeNull();
  });

  it("returns null for no rounds", () => {
    expect(wheelGeometry([], 400, 400)).toBeNull();
  });

  it("labels every wedge when there are few", () => {
    const g = wheelGeometry(seq(2), 400, 400)!;
    expect(g.segments.every((s) => s.label !== null)).toBe(true);
  });

  it("drops labels rather than overlapping them when there are many", () => {
    const g = wheelGeometry(seq(50), 300, 300)!;
    expect(g.segments).toHaveLength(50);
    expect(g.segments.every((s) => s.label === null)).toBe(true);
    // The ring itself still has shape — that's the degradation, not a blank.
    expect(g.segments.every((s) => s.path.startsWith("M "))).toBe(true);
  });

  it("keeps the ring inside its box at every size", () => {
    for (const [w, h] of [
      [400, 400],
      [900, 300],
      [300, 900],
      [120, 120],
    ] as const) {
      const g = wheelGeometry(seq(7), w, h);
      if (!g) continue;
      expect(g.cx - g.rOuter).toBeGreaterThanOrEqual(0);
      expect(g.cy - g.rOuter).toBeGreaterThanOrEqual(0);
      expect(g.cx + g.rOuter).toBeLessThanOrEqual(w);
      expect(g.cy + g.rOuter).toBeLessThanOrEqual(h);
    }
  });

  it("puts labels inside the ring band", () => {
    const g = wheelGeometry(seq(6), 400, 400)!;
    for (const s of g.segments) {
      if (!s.label) continue;
      const r = radiusOf(g, s.label.x, s.label.y);
      expect(r).toBeGreaterThanOrEqual(g.rInner);
      expect(r).toBeLessThanOrEqual(g.rOuter);
    }
  });

  it("has a hollow centre so the ring reads as a wheel", () => {
    const g = wheelGeometry(seq(6), 400, 400)!;
    expect(g.rInner).toBeGreaterThan(0);
    expect(g.rInner).toBeLessThan(g.rOuter);
  });

  it("is deterministic, so a live append does not reshuffle history", () => {
    const a = wheelGeometry(seq(5), 400, 400)!;
    const b = wheelGeometry(seq(5), 400, 400)!;
    expect(a.segments.map((s) => s.path)).toEqual(
      b.segments.map((s) => s.path),
    );
  });

  it("grows the ring by one wedge when a round lands", () => {
    const before = wheelGeometry(seq(4), 400, 400)!;
    const after = wheelGeometry(seq(5), 400, 400)!;
    expect(after.segments).toHaveLength(before.segments.length + 1);
    expect(after.segments[0].startAngle).toBeCloseTo(
      before.segments[0].startAngle,
    );
  });
});

describe("wedgePath", () => {
  it("emits a closed path with both arcs", () => {
    const p = wedgePath(100, 100, 40, 60, 0, 1);
    expect(p).toMatch(/^M /);
    expect(p).toMatch(/ Z$/);
    expect(p.match(/A /g)).toHaveLength(2);
  });

  it("sets the large-arc flag past a half turn", () => {
    expect(wedgePath(0, 0, 4, 6, 0, Math.PI * 1.5)).toContain("0 1 1");
    expect(wedgePath(0, 0, 4, 6, 0, Math.PI * 0.5)).toContain("0 0 1");
  });

  it("emits no NaN for any finite input", () => {
    // A NaN in a path makes SVG drop the whole element silently.
    for (const a1 of [0.1, Math.PI, Math.PI * 1.9]) {
      expect(wedgePath(50, 50, 10, 30, 0, a1)).not.toContain("NaN");
    }
  });
});
