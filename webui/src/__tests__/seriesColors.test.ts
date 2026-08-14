// Sticky per-run color assignment (PRD §1). The regression these tests exist
// to prevent: dropping or reordering a run silently recoloring the runs around
// it, so the legend you just read no longer matches the curves on screen.

import { describe, expect, it } from "vitest";
import {
  SERIES_COLOR_COUNT,
  assignColors,
  colorForSlot,
  type ColorAssignment,
} from "../desktop/panes/seriesColors";

const EMPTY: ColorAssignment = new Map<string, number>();

describe("colorForSlot", () => {
  it("maps 0-based slots onto the 1-based CSS custom properties", () => {
    expect(colorForSlot(0)).toBe("var(--t-series-1)");
    expect(colorForSlot(7)).toBe("var(--t-series-8)");
  });

  it("wraps past the end of the palette instead of naming a property no theme defines", () => {
    expect(colorForSlot(SERIES_COLOR_COUNT)).toBe("var(--t-series-1)");
    expect(colorForSlot(SERIES_COLOR_COUNT + 2)).toBe("var(--t-series-3)");
    expect(colorForSlot(-1)).toBe("var(--t-series-8)");
  });
});

describe("assignColors", () => {
  it("fills slots in activeKeys order on first assignment", () => {
    const got = assignColors(EMPTY, ["a", "b", "c"]);
    expect([...got]).toEqual([
      ["a", 0],
      ["b", 1],
      ["c", 2],
    ]);
  });

  it("keeps the surviving runs on their original slots when the first run is dropped", () => {
    const first = assignColors(EMPTY, ["a", "b", "c"]);
    const next = assignColors(first, ["b", "c"]);
    // Not 0 and 1 — b and c must not shift up into a's freed color.
    expect(next.get("b")).toBe(1);
    expect(next.get("c")).toBe(2);
    expect(next.has("a")).toBe(false);
    expect(next.size).toBe(2);
  });

  it("keeps colors stable when a run is removed from the middle", () => {
    const first = assignColors(EMPTY, ["a", "b", "c", "d"]);
    const next = assignColors(first, ["a", "c", "d"]);
    expect(next.get("a")).toBe(0);
    expect(next.get("c")).toBe(2);
    expect(next.get("d")).toBe(3);
  });

  it("gives a new run the freed lower slot rather than the next unused one", () => {
    const first = assignColors(EMPTY, ["a", "b", "c"]);
    const afterRemove = assignColors(first, ["b", "c"]);
    const withNew = assignColors(afterRemove, ["b", "c", "d"]);
    expect(withNew.get("d")).toBe(0);
    expect(withNew.get("b")).toBe(1);
    expect(withNew.get("c")).toBe(2);
  });

  it("assigns several new runs to the free slots in activeKeys order", () => {
    const prev = new Map([["b", 1]]);
    const got = assignColors(prev, ["b", "x", "y", "z"]);
    expect(got.get("x")).toBe(0);
    expect(got.get("y")).toBe(2);
    expect(got.get("z")).toBe(3);
  });

  it("changes nothing when activeKeys is reordered without changing membership", () => {
    const first = assignColors(EMPTY, ["a", "b", "c"]);
    const reordered = assignColors(first, ["c", "a", "b"]);
    expect(reordered.get("a")).toBe(0);
    expect(reordered.get("b")).toBe(1);
    expect(reordered.get("c")).toBe(2);
  });

  it("returns an empty map when nothing is selected", () => {
    const first = assignColors(EMPTY, ["a", "b"]);
    const cleared = assignColors(first, []);
    expect(cleared.size).toBe(0);
  });

  it("re-plotting a cleared run starts it over at the lowest slot", () => {
    const first = assignColors(EMPTY, ["a", "b"]);
    const cleared = assignColors(first, []);
    const again = assignColors(cleared, ["b"]);
    expect(again.get("b")).toBe(0);
  });

  it("never mutates prev", () => {
    const prev = new Map([
      ["a", 0],
      ["b", 1],
    ]);
    const snapshot = [...prev];
    const got = assignColors(prev, ["b", "c"]);
    expect([...prev]).toEqual(snapshot);
    expect(got).not.toBe(prev);
  });

  it("contains exactly the active keys and nothing else", () => {
    const prev = new Map([
      ["a", 0],
      ["gone", 1],
    ]);
    const got = assignColors(prev, ["a", "new"]);
    expect([...got.keys()].sort()).toEqual(["a", "new"]);
  });

  it("gives a repeated key one entry", () => {
    const got = assignColors(EMPTY, ["a", "a", "b"]);
    expect(got.size).toBe(2);
    expect(got.get("a")).toBe(0);
    expect(got.get("b")).toBe(1);
  });

  it("fills the whole palette without collision at the cap", () => {
    const keys = Array.from({ length: SERIES_COLOR_COUNT }, (_, i) => `run-${i}`);
    const got = assignColors(EMPTY, keys);
    expect(new Set(got.values()).size).toBe(SERIES_COLOR_COUNT);
    expect(got.get("run-7")).toBe(7);
  });

  it("wraps instead of throwing when the upstream cap is somehow exceeded", () => {
    const keys = Array.from({ length: SERIES_COLOR_COUNT + 2 }, (_, i) => `run-${i}`);
    const got = assignColors(EMPTY, keys);
    expect(got.size).toBe(SERIES_COLOR_COUNT + 2);
    for (const slot of got.values()) {
      expect(slot).toBeGreaterThanOrEqual(0);
      expect(slot).toBeLessThan(SERIES_COLOR_COUNT);
    }
  });
});
