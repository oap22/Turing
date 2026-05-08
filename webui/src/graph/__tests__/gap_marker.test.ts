import { describe, expect, it } from "vitest";
import { emptyState, reduce } from "../reducer";

describe("gap_marker reducer", () => {
  it("increments droppedCount on the named Pi-node", () => {
    let state = emptyState();
    state = reduce(state, {
      type: "gap_marker",
      node_name: "pi-alpha",
      stream: "tool.dispatch",
      missing: [3, 5],
      timestamp_ms: 1000,
    });
    expect(state.nodes["pi::pi-alpha"].droppedCount).toBe(3);
  });

  it("accumulates across multiple gap markers", () => {
    let state = emptyState();
    state = reduce(state, {
      type: "gap_marker",
      node_name: "pi-alpha",
      stream: "x",
      missing: [2, 2],
      timestamp_ms: 1,
    });
    state = reduce(state, {
      type: "gap_marker",
      node_name: "pi-alpha",
      stream: "y",
      missing: [10, 12],
      timestamp_ms: 2,
    });
    expect(state.nodes["pi::pi-alpha"].droppedCount).toBe(4);
  });

  it("clears droppedCount on stream_reset", () => {
    let state = emptyState();
    state = reduce(state, {
      type: "gap_marker",
      node_name: "pi-alpha",
      stream: "x",
      missing: [2, 4],
      timestamp_ms: 1,
    });
    state = reduce(state, {
      type: "stream_reset",
      node_name: "pi-alpha",
      stream: "x",
      timestamp_ms: 2,
    });
    expect(state.nodes["pi::pi-alpha"].droppedCount).toBe(0);
  });

  it("does not bleed across nodes", () => {
    let state = emptyState();
    state = reduce(state, {
      type: "gap_marker",
      node_name: "pi-alpha",
      stream: "x",
      missing: [2, 4],
      timestamp_ms: 1,
    });
    state = reduce(state, {
      type: "hello",
      node_name: "pi-beta",
      uptime_s: 0,
    });
    expect(state.nodes["pi::pi-beta"].droppedCount).toBeUndefined();
  });
});
