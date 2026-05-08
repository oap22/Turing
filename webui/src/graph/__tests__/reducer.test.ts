// Vitest unit tests for the graph reducer. Run via `npm run test` (CI image).
// We don't wire vitest into pyproject — it lives alongside Vite.

import { describe, expect, it } from "vitest";
import { emptyState, markStale, reduce, type Frame } from "../reducer";

const NOW = 1_000_000;

function trace(overrides: Partial<Frame> & { type: "message_trace" }): Frame {
  return {
    type: "message_trace",
    node_name: "pi-alpha",
    event_type: "tool.dispatch.start",
    seq: 1,
    timestamp_ms: NOW,
    duration_ms: null,
    error: null,
    payload: { tool: "shell" },
    ...overrides,
  } as Frame;
}

describe("graph reducer — Pi nodes", () => {
  it("creates a Pi node on the first hello frame", () => {
    const state = reduce(emptyState(), {
      type: "hello",
      node_name: "pi-alpha",
      uptime_s: 0,
    });
    expect(state.nodes["pi::pi-alpha"].label).toBe("pi-alpha");
    expect(state.nodes["pi::pi-alpha"].slot).toBe(0);
  });

  it("assigns sticky slot indices in arrival order", () => {
    let state = emptyState();
    state = reduce(state, { type: "hello", node_name: "pi-alpha", uptime_s: 0 });
    state = reduce(state, { type: "hello", node_name: "pi-beta", uptime_s: 0 });
    state = reduce(state, { type: "hello", node_name: "pi-gamma", uptime_s: 0 });
    expect(state.nodes["pi::pi-alpha"].slot).toBe(0);
    expect(state.nodes["pi::pi-beta"].slot).toBe(1);
    expect(state.nodes["pi::pi-gamma"].slot).toBe(2);
  });

  it("preserves the slot when a node is seen again", () => {
    let state = emptyState();
    state = reduce(state, { type: "hello", node_name: "pi-alpha", uptime_s: 0 });
    state = reduce(state, { type: "hello", node_name: "pi-beta", uptime_s: 0 });
    // pi-alpha re-announces — its slot must not change.
    state = reduce(state, { type: "hello", node_name: "pi-alpha", uptime_s: 1 });
    expect(state.nodes["pi::pi-alpha"].slot).toBe(0);
  });
});

describe("graph reducer — edges", () => {
  it("adds an active edge on .start and resolves it on .end", () => {
    let state = emptyState();
    state = reduce(state, trace({ event_type: "llm.complete.start", payload: { provider: "cloud" } }));
    const edgeId = "pi-alpha::llm.complete::1";
    expect(state.edges[edgeId].active).toBe(true);

    state = reduce(state, trace({
      event_type: "llm.complete.end",
      duration_ms: 42,
      payload: { provider: "cloud" },
    }));
    expect(state.edges[edgeId].active).toBe(false);
    expect(state.edges[edgeId].latencyMs).toBe(42);
  });

  it("creates the target node implicitly", () => {
    const state = reduce(
      emptyState(),
      trace({ event_type: "tool.dispatch.start", payload: { tool: "shell" } }),
    );
    expect(state.nodes["tool::shell"]).toBeDefined();
  });
});

describe("graph reducer — stale state", () => {
  it("marks Pi nodes stale after the window", () => {
    let state = reduce(emptyState(), {
      type: "hello",
      node_name: "pi-alpha",
      uptime_s: 0,
    });
    state = markStale(state, NOW + 60_000);
    expect(state.nodes["pi::pi-alpha"].stale).toBe(true);
  });

  it("clears stale on fresh activity", () => {
    let state = reduce(emptyState(), {
      type: "hello",
      node_name: "pi-alpha",
      uptime_s: 0,
    });
    state = markStale(state, NOW + 60_000);
    expect(state.nodes["pi::pi-alpha"].stale).toBe(true);
    state = reduce(state, trace({ timestamp_ms: NOW + 60_500 }));
    expect(state.nodes["pi::pi-alpha"].stale).toBe(false);
  });
});
