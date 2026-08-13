// Flywheel trajectory parser tests (issue #382) — three accepted shapes plus
// the garbage-input null case.

import { describe, expect, it } from "vitest";
import { parseTrajectory } from "../desktop/panes/flywheel";

describe("parseTrajectory", () => {
  it("parses a JSON array of rounds", () => {
    const text = JSON.stringify([
      { round: 0, cell: "propose", passed: true, extra: "x" },
      { round: 1, cell: "verify", passed: false },
    ]);
    const rounds = parseTrajectory(text);
    expect(rounds).not.toBeNull();
    expect(rounds).toHaveLength(2);
    expect(rounds?.[0]).toMatchObject({ index: 0, label: "propose", status: "pass" });
    expect(rounds?.[1]).toMatchObject({ index: 1, label: "verify", status: "fail" });
  });

  it("parses a {rounds: [...]} object", () => {
    const text = JSON.stringify({
      rounds: [{ index: 0, phase: "plan", ok: true }],
    });
    const rounds = parseTrajectory(text);
    expect(rounds).toHaveLength(1);
    expect(rounds?.[0]).toMatchObject({ index: 0, label: "plan", status: "pass" });
  });

  it("parses JSONL, one round per line", () => {
    const text = [
      '{"round":0,"step":"a","success":true}',
      '{"round":1,"step":"b","success":false}',
    ].join("\n");
    const rounds = parseTrajectory(text);
    expect(rounds).toHaveLength(2);
    expect(rounds?.[0]).toMatchObject({ index: 0, label: "a", status: "pass" });
    expect(rounds?.[1]).toMatchObject({ index: 1, label: "b", status: "fail" });
  });

  it("returns null when nothing parses", () => {
    expect(parseTrajectory("")).toBeNull();
    expect(parseTrajectory("not json, not jsonl either {{{")).toBeNull();
  });

  it("marks an unrecognized status field as 'other'", () => {
    const text = JSON.stringify([{ round: 0, slug: "x" }]);
    const rounds = parseTrajectory(text);
    expect(rounds?.[0].status).toBe("other");
  });
});
