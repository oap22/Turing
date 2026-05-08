import { describe, expect, it } from "vitest";
import { EMPTY_FILTER } from "../types";
import { applyFilter, toQuery } from "../filter";

const E = (overrides: Partial<{ duration_ms: number | null; node_name: string; event_type: string }>) => ({
  timestamp_ms: 1000,
  node_name: overrides.node_name ?? "pi-alpha",
  event_type: overrides.event_type ?? "tool.dispatch.end",
  duration_ms: overrides.duration_ms ?? 10,
  payload: {},
});

describe("trace filter", () => {
  it("passes everything through with an empty filter", () => {
    const events = [E({}), E({ node_name: "pi-beta" })];
    expect(applyFilter(events, EMPTY_FILTER)).toEqual(events);
  });

  it("filters by node", () => {
    const events = [E({}), E({ node_name: "pi-beta" })];
    const out = applyFilter(events, { ...EMPTY_FILTER, nodes: ["pi-beta"] });
    expect(out).toHaveLength(1);
    expect(out[0].node_name).toBe("pi-beta");
  });

  it("filters by event type", () => {
    const events = [
      E({ event_type: "tool.dispatch.end" }),
      E({ event_type: "llm.complete.end" }),
    ];
    const out = applyFilter(events, {
      ...EMPTY_FILTER,
      eventTypes: ["llm.complete.end"],
    });
    expect(out).toHaveLength(1);
    expect(out[0].event_type).toBe("llm.complete.end");
  });

  it("filters by min duration", () => {
    const events = [E({ duration_ms: 10 }), E({ duration_ms: 200 })];
    const out = applyFilter(events, { ...EMPTY_FILTER, minDurationMs: 100 });
    expect(out).toHaveLength(1);
    expect(out[0].duration_ms).toBe(200);
  });

  it("handles null durations as 0", () => {
    const events = [E({ duration_ms: null })];
    const out = applyFilter(events, { ...EMPTY_FILTER, minDurationMs: 1 });
    expect(out).toHaveLength(0);
  });
});

describe("toQuery", () => {
  it("emits multi-valued params as repeated keys", () => {
    const q = toQuery({
      nodes: ["a", "b"],
      eventTypes: ["x.end", "y.end"],
      minDurationMs: 100,
    });
    expect(q).toContain("node=a");
    expect(q).toContain("node=b");
    expect(q).toContain("event_type=x.end");
    expect(q).toContain("event_type=y.end");
    expect(q).toContain("min_duration_ms=100");
  });

  it("includes since_ms when provided", () => {
    const q = toQuery(EMPTY_FILTER, 5000);
    expect(q).toContain("since_ms=5000");
  });
});
