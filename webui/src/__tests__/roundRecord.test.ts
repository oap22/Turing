// `round-NN/round.json` parser (#390 part 1).
//
// REAL_ROUND is a record emitted verbatim by `encode_round_record`
// (`src/turing/research/loop/trajectory.py`), so this fails if the producer
// and the pane drift apart — the failure mode that made the trajectory
// mapping useless in the first place.

import { describe, expect, it } from "vitest";
import {
  comparableToParent,
  parseRoundRecord,
  roundDirName,
} from "../desktop/panes/roundRecord";

const REAL_ROUND = {
  cost: { attempts: 3, tokens: 4000, wall_clock_seconds: 120.0 },
  created_at_ms: 1,
  deltas: [],
  engine: {
    backend: "claude",
    orchestrator_model: "opus",
    scaffold_git_sha: "abc1234",
    substep_model: "haiku",
  },
  escalation_count: 1,
  eval_set_hash: "corpus-v1",
  gates: { eval_set_stable: true },
  noise_floors: [],
  parent_round_id: "r01",
  round_index: 2,
  run_id: "r02",
  saturation: [],
  type_scores: [
    {
      correctness_pass_rate: 1.0,
      correctness_passes: 1,
      mean_score: 0.4,
      n: 1,
      problem_type: "kaggle",
      scores: { k1: 0.4 },
      split: "practice",
    },
    {
      correctness_pass_rate: 1.0,
      correctness_passes: 1,
      mean_score: 2.0,
      n: 1,
      problem_type: "speedup",
      scores: { s1: 2.0 },
      split: "practice",
    },
  ],
  verdict: "baseline",
};

describe("roundDirName", () => {
  it("zero-pads to two digits, matching round_dir()", () => {
    expect(roundDirName(0)).toBe("round-00");
    expect(roundDirName(7)).toBe("round-07");
    expect(roundDirName(42)).toBe("round-42");
    // Past 99 the producer's f"{i:02d}" also just widens; don't truncate.
    expect(roundDirName(123)).toBe("round-123");
  });
});

describe("parseRoundRecord — the real schema", () => {
  const rec = parseRoundRecord(JSON.stringify(REAL_ROUND))!;

  it("reads lineage", () => {
    expect(rec.roundIndex).toBe(2);
    expect(rec.runId).toBe("r02");
    expect(rec.parentRoundId).toBe("r01");
    expect(rec.evalSetHash).toBe("corpus-v1");
  });

  it("reads engine identity including the scaffold sha", () => {
    expect(rec.engine).toEqual({
      backend: "claude",
      orchestratorModel: "opus",
      substepModel: "haiku",
      scaffoldGitSha: "abc1234",
    });
  });

  it("flattens type_scores into cell-keyed scores", () => {
    expect(rec.scores).toEqual([
      {
        cell: "kaggle/practice",
        n: 1,
        meanScore: 0.4,
        correctnessPassRate: 1.0,
      },
      {
        cell: "speedup/practice",
        n: 1,
        meanScore: 2.0,
        correctnessPassRate: 1.0,
      },
    ]);
  });

  it("reads cost and escalation count", () => {
    expect(rec.cost).toEqual({
      wallClockSeconds: 120,
      tokens: 4000,
      attempts: 3,
    });
    expect(rec.escalationCount).toBe(1);
  });

  it("turns the gates map into named pass/fail rows", () => {
    expect(rec.gates).toEqual([{ name: "eval_set_stable", passed: true }]);
  });

  it("reads per-cell deltas with the noise-floor verdict", () => {
    const withDeltas = {
      ...REAL_ROUND,
      deltas: [
        {
          problem_type: "speedup",
          split: "practice",
          marginal_gain: 0.25,
          noise_floor: 0.1,
          beats_noise_floor: true,
          gain_in_noise_units: 2.5,
          cost_per_unit_gain: 1600,
        },
      ],
    };
    const parsed = parseRoundRecord(JSON.stringify(withDeltas))!;
    expect(parsed.deltas).toEqual([
      {
        cell: "speedup/practice",
        marginalGain: 0.25,
        noiseFloor: 0.1,
        beatsNoiseFloor: true,
        gainInNoiseUnits: 2.5,
        costPerUnitGain: 1600,
      },
    ]);
  });

  it("keeps a false beats_noise_floor distinct from a missing one", () => {
    const mk = (v: unknown) =>
      parseRoundRecord(
        JSON.stringify({
          ...REAL_ROUND,
          deltas: [{ problem_type: "s", split: "p", beats_noise_floor: v }],
        }),
      )!.deltas[0].beatsNoiseFloor;
    expect(mk(false)).toBe(false);
    expect(mk(undefined)).toBeNull();
    expect(mk(null)).toBeNull();
    // A string must not coerce — "false" would read as a pass.
    expect(mk("true")).toBeNull();
  });
});

describe("parseRoundRecord — degrades instead of blanking", () => {
  it("returns null only for input that isn't a JSON object", () => {
    expect(parseRoundRecord("")).toBeNull();
    expect(parseRoundRecord("{ mid-write")).toBeNull();
    expect(parseRoundRecord("[]")).toBeNull();
    expect(parseRoundRecord("null")).toBeNull();
    expect(parseRoundRecord("42")).toBeNull();
  });

  it("renders an almost-empty record with nulls rather than failing", () => {
    const rec = parseRoundRecord("{}")!;
    expect(rec).not.toBeNull();
    expect(rec.roundIndex).toBeNull();
    expect(rec.scores).toEqual([]);
    expect(rec.gates).toEqual([]);
    expect(rec.engine.backend).toBeNull();
  });

  it("drops non-boolean gate values instead of coercing them", () => {
    // A truthy string would otherwise render as a passing gate.
    const rec = parseRoundRecord(
      JSON.stringify({ gates: { a: true, b: "yes", c: 0 } }),
    )!;
    expect(rec.gates).toEqual([{ name: "a", passed: true }]);
  });

  it("ignores non-object entries inside the arrays", () => {
    const rec = parseRoundRecord(
      JSON.stringify({
        type_scores: [null, 5, { problem_type: "x", split: "y" }],
      }),
    )!;
    expect(rec.scores).toHaveLength(1);
    expect(rec.scores[0].cell).toBe("x/y");
  });

  it("marks an unnamed cell rather than producing 'undefined/undefined'", () => {
    const rec = parseRoundRecord(
      JSON.stringify({ type_scores: [{ mean_score: 1 }] }),
    )!;
    expect(rec.scores[0].cell).toBe("?/?");
  });

  it("rejects NaN and Infinity, which JSON.parse can produce via 1e999", () => {
    const rec = parseRoundRecord('{"cost": {"tokens": 1e999}}')!;
    expect(rec.cost.tokens).toBeNull();
  });
});

describe("comparableToParent", () => {
  /** A record with the given eval-set hash, own run id, and parent run id. */
  const rec = (hash: string, runId: string, parentId: string | null) =>
    parseRoundRecord(
      JSON.stringify({
        eval_set_hash: hash,
        run_id: runId,
        parent_round_id: parentId,
      }),
    )!;

  const parent = rec("a", "r01", null);

  it("is true when parent and child share an eval set", () => {
    expect(comparableToParent(rec("a", "r02", "r01"), parent)).toBe(true);
  });

  it("is false when the eval set changed under the comparison", () => {
    // Comparing across eval sets is what the contracts refuse to do; the
    // detail view must say so rather than show a meaningless delta.
    expect(comparableToParent(rec("b", "r02", "r01"), parent)).toBe(false);
  });

  it("is null for a baseline with no parent", () => {
    expect(comparableToParent(rec("a", "r00", null), null)).toBeNull();
    expect(comparableToParent(rec("a", "r00", null), parent)).toBeNull();
  });

  it("is null when the candidate is not actually the named parent", () => {
    // The pane finds the parent by index, which is usually right but is not
    // guaranteed. Comparing against the wrong round would be worse than
    // saying nothing.
    expect(
      comparableToParent(rec("a", "r02", "r01"), rec("a", "rXX", null)),
    ).toBeNull();
  });

  it("is null when either hash is missing rather than guessing", () => {
    expect(comparableToParent(rec("", "r02", "r01"), parent)).toBeNull();
    expect(
      comparableToParent(rec("a", "r02", "r01"), rec("", "r01", null)),
    ).toBeNull();
  });
});
