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
    expect(rounds?.[0]).toMatchObject({
      index: 0,
      label: "propose",
      status: "pass",
    });
    expect(rounds?.[1]).toMatchObject({
      index: 1,
      label: "verify",
      status: "fail",
    });
  });

  it("parses a {rounds: [...]} object", () => {
    const text = JSON.stringify({
      rounds: [{ index: 0, phase: "plan", ok: true }],
    });
    const rounds = parseTrajectory(text);
    expect(rounds).toHaveLength(1);
    expect(rounds?.[0]).toMatchObject({
      index: 0,
      label: "plan",
      status: "pass",
    });
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

// The real producer is `src/turing/research/loop/trajectory.py`. REAL_ROW is a
// verbatim row emitted by `encode_trajectory_row`, so these tests fail if the
// pane's mapping drifts from the schema again (#390) — the previous mapping
// looked for `cell`/`passed`, which this row does not have and never had.
const REAL_ROW = {
  cap: null,
  comparable_to_parent: null,
  constraints: { eval_set_stable: true },
  correctness_pass_rate: { "kaggle/practice": 1.0, "speedup/practice": 1.0 },
  cost: { attempts: 3, tokens: 4000, wall_clock_seconds: 120.0 },
  cost_basis: null,
  cost_per_point: {},
  delta: {},
  delta_in_noise_units: {},
  engine: {
    backend: "claude",
    orchestrator_model: "opus",
    scaffold_git_sha: "abc1234",
    substep_model: "haiku",
  },
  eval_set_hash: "corpus-v1",
  human_interventions: 2,
  n: { "kaggle/practice": 1, "speedup/practice": 1 },
  noise_floor: {},
  parent_round: "r00",
  primary: { "kaggle/practice": 0.4, "speedup/practice": 2.0 },
  round: 1,
  run_id: "r01",
  saturation: [],
  seed: null,
  verdict: "baseline",
  verify_every_step: null,
};

function assessment(verdict: string, cell = "speedup/practice", gain?: number) {
  const [problem_type, split] = cell.split("/");
  return {
    problem_type,
    split,
    verdict,
    reason: `${cell}: ${verdict}`,
    marginal_gain: gain ?? null,
    noise_floor: 0.1,
    gain_in_noise_units: gain ?? null,
  };
}

describe("parseTrajectory — the real trajectory.json schema", () => {
  it("takes the round index from `round`", () => {
    expect(parseTrajectory(JSON.stringify([REAL_ROW]))?.[0].index).toBe(1);
  });

  it("labels the round with its cells, not a position string", () => {
    // The old mapping fell back to String(position) — every row labelled "0".
    const round = parseTrajectory(JSON.stringify([REAL_ROW]))?.[0];
    expect(round?.label).toBe("kaggle/practice speedup/practice");
  });

  it("summarises a third and later cell instead of overflowing the line", () => {
    const wide = {
      ...REAL_ROW,
      primary: { "a/val": 0.1, "b/val": 0.2, "c/val": 0.3, "d/val": 0.4 },
    };
    expect(parseTrajectory(JSON.stringify([wide]))?.[0].label).toBe(
      "a/val b/val +2",
    );
  });

  it("surfaces the driving-function numbers as detail", () => {
    const detail =
      parseTrajectory(JSON.stringify([REAL_ROW]))?.[0].detail ?? "";
    expect(detail).toContain("kaggle/practice 0.40");
    expect(detail).toContain("speedup/practice 2");
    expect(detail).toContain("4.0k tok");
    expect(detail).toContain("2m");
    expect(detail).toContain("2 esc");
    expect(detail).toContain("baseline");
    // The old mapping surfaced only these — the least useful fields in the row.
    expect(detail).not.toContain("corpus-v1");
    expect(detail).not.toContain("r01");
  });

  it("prefers gain in noise units over the raw delta", () => {
    const row = {
      ...REAL_ROW,
      primary: { "speedup/practice": 2.0 },
      delta: { "speedup/practice": 0.25 },
      delta_in_noise_units: { "speedup/practice": 1.8 },
    };
    expect(parseTrajectory(JSON.stringify([row]))?.[0].detail).toContain(
      "speedup/practice 2 +1.8σ",
    );
  });

  it("falls back to the raw delta when no noise floor was measured", () => {
    const row = {
      ...REAL_ROW,
      primary: { "speedup/practice": 2.0 },
      delta: { "speedup/practice": -0.25 },
    };
    expect(parseTrajectory(JSON.stringify([row]))?.[0].detail).toContain(
      "speedup/practice 2 -0.3",
    );
  });

  it("reads a failed gate as a failed round", () => {
    const row = { ...REAL_ROW, constraints: { eval_set_stable: false } };
    expect(parseTrajectory(JSON.stringify([row]))?.[0].status).toBe("fail");
  });

  it("reads an improving cell as a pass", () => {
    const row = {
      ...REAL_ROW,
      saturation: [assessment("improving", "speedup/practice", 1.9)],
    };
    expect(parseTrajectory(JSON.stringify([row]))?.[0].status).toBe("pass");
  });

  it("passes when any cell improves, even if another saturated", () => {
    const row = {
      ...REAL_ROW,
      saturation: [
        assessment("saturated", "kaggle/practice"),
        assessment("improving"),
      ],
    };
    // A round that helps one family while hurting another must not read flat.
    expect(parseTrajectory(JSON.stringify([row]))?.[0].status).toBe("pass");
  });

  it("distinguishes saturated from refused", () => {
    const saturated = { ...REAL_ROW, saturation: [assessment("saturated")] };
    expect(parseTrajectory(JSON.stringify([saturated]))?.[0].status).toBe(
      "saturated",
    );

    // Refusals are the honest answer when the measurement was never made —
    // they must not be summarised away as a flat round.
    const refused = {
      ...REAL_ROW,
      saturation: [
        assessment("refused_no_parent"),
        assessment("refused_eval_set_changed"),
      ],
    };
    expect(parseTrajectory(JSON.stringify([refused]))?.[0].status).toBe(
      "refused",
    );
  });

  it("does not claim a baseline round improved just because its gates are green", () => {
    // Round 0 has no parent, so nothing is assessed and there is no verdict
    // to report. Green gates say the round was valid, not that it moved.
    expect(parseTrajectory(JSON.stringify([REAL_ROW]))?.[0].status).toBe(
      "other",
    );
  });

  it("parses a real row from JSONL too", () => {
    const text = [
      JSON.stringify(REAL_ROW),
      JSON.stringify({ ...REAL_ROW, round: 2 }),
    ].join("\n");
    const rounds = parseTrajectory(text);
    expect(rounds).toHaveLength(2);
    expect(rounds?.[1].index).toBe(2);
  });

  it("still renders a row stripped of everything but its index", () => {
    const rounds = parseTrajectory(JSON.stringify([{ round: 7 }]));
    expect(rounds?.[0]).toMatchObject({ index: 7, status: "other" });
  });
});
