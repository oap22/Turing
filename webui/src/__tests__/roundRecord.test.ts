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
  parseRoundSummary,
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

describe("parseRoundRecord — problems[]", () => {
  // Verbatim from a real `round-01/round.json`. The nested id is the point:
  // problem ids are free-form path components, and this one appeared nowhere
  // in the pane as text until these rows existed.
  const REAL_PROBLEMS = {
    ...REAL_ROUND,
    problems: [
      {
        passed_correctness: true,
        problem_id: "cuda/matmul-speedup",
        problem_type: "speedup",
        score: 2.4,
        score_scale: "speedup_ratio",
        scored: true,
        split: "practice",
      },
      {
        passed_correctness: true,
        problem_id: "kaggle/titanic",
        problem_type: "kaggle",
        score: 55.0,
        score_scale: "leaderboard_percentile",
        scored: true,
        split: "held_out",
      },
    ],
  };

  it("keeps the nested problem id intact and keys it to its cell", () => {
    const rec = parseRoundRecord(JSON.stringify(REAL_PROBLEMS))!;
    expect(rec.problems).toEqual([
      {
        problemId: "cuda/matmul-speedup",
        cell: "speedup/practice",
        score: 2.4,
        scoreScale: "speedup_ratio",
        scored: true,
        passedCorrectness: true,
      },
      {
        problemId: "kaggle/titanic",
        cell: "kaggle/held_out",
        score: 55,
        scoreScale: "leaderboard_percentile",
        scored: true,
        passedCorrectness: true,
      },
    ]);
  });

  it("keeps an unscored or failed problem rather than dropping it", () => {
    const rec = parseRoundRecord(
      JSON.stringify({
        problems: [
          {
            problem_id: "cuda/matmul-speedup",
            problem_type: "speedup",
            split: "practice",
            scored: false,
            passed_correctness: false,
          },
        ],
      }),
    )!;
    expect(rec.problems[0].scored).toBe(false);
    expect(rec.problems[0].passedCorrectness).toBe(false);
    expect(rec.problems[0].score).toBeNull();
  });

  it("names an unnamed problem rather than rendering nothing", () => {
    const rec = parseRoundRecord(JSON.stringify({ problems: [{ score: 1 }] }))!;
    expect(rec.problems[0].problemId).toBe("?");
    expect(rec.problems[0].cell).toBe("?/?");
  });

  it("is an empty list for a round record that predates problems[]", () => {
    expect(parseRoundRecord(JSON.stringify(REAL_ROUND))!.problems).toEqual([]);
  });
});

describe("parseRoundSummary", () => {
  // The real `round-01/metrics.json` for the round that lost an attempt.
  const REAL_SUMMARY = {
    schema_version: 1,
    round_index: 1,
    run_id: "r01",
    parent_round_id: "r00",
    eval_set_hash: "6b7ba05",
    seed: 7,
    comparable_to_parent: false,
    cells: [],
    verdict: "1 of 4 attempt(s) failed and are absent from every cell",
  };

  it("reads the persisted comparability and its lineage", () => {
    expect(parseRoundSummary(JSON.stringify(REAL_SUMMARY))).toEqual({
      roundIndex: 1,
      runId: "r01",
      parentRoundId: "r00",
      comparableToParent: false,
    });
  });

  it("keeps a baseline's null distinct from a false", () => {
    const s = parseRoundSummary(
      JSON.stringify({ round_index: 0, comparable_to_parent: null }),
    )!;
    expect(s.comparableToParent).toBeNull();
  });

  it("returns null for anything that isn't a JSON object", () => {
    expect(parseRoundSummary("")).toBeNull();
    expect(parseRoundSummary("{ mid-write")).toBeNull();
    expect(parseRoundSummary("[]")).toBeNull();
  });

  it("refuses a non-boolean comparability instead of coercing it", () => {
    // "false" is truthy; coercing it would paint the round green.
    const s = parseRoundSummary(
      JSON.stringify({ comparable_to_parent: "false" }),
    )!;
    expect(s.comparableToParent).toBeNull();
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

  /** The round summary that sits beside it. */
  const summary = (runId: string, comparable: boolean | null) =>
    parseRoundSummary(
      JSON.stringify({ run_id: runId, comparable_to_parent: comparable }),
    )!;

  const parent = rec("a", "r01", null);
  const child = rec("a", "r02", "r01");

  it("reports what the round summary persisted, not what the hashes imply", () => {
    // The regression: a round that lost an attempt keeps a stable eval set, so
    // hash-only recomputation called it comparable and painted it green beside
    // a failed all_attempts_completed gate and two refused_attempt_lost cells.
    expect(comparableToParent(child, parent, summary("r02", false))).toEqual({
      comparable: false,
      basis: "summary",
    });
  });

  it("is true only when the summary says so", () => {
    expect(comparableToParent(child, parent, summary("r02", true))).toEqual({
      comparable: true,
      basis: "summary",
    });
  });

  it("is unknown — never comparable — when no summary was written", () => {
    // Matching hashes are necessary for comparability, not sufficient. The
    // fallback fails in the one direction that matters, so there is none.
    expect(comparableToParent(child, parent, null)).toEqual({
      comparable: null,
      basis: "unknown",
    });
  });

  it("is unknown when the summary is present but unreadable", () => {
    // `parseRoundSummary` hands the pane null for a mid-write file; that must
    // land in the same place as a missing one, not in the hash fallback.
    expect(
      comparableToParent(child, parent, parseRoundSummary("{ mid-write")),
    ).toEqual({ comparable: null, basis: "unknown" });
  });

  it("ignores a summary that describes a different round", () => {
    // A stale read from the previously-expanded round must not answer for
    // this one — the same misattribution the loop/index stamping prevents.
    expect(comparableToParent(child, parent, summary("rXX", true))).toEqual({
      comparable: null,
      basis: "unknown",
    });
  });

  it("is unknown for a baseline with no parent, summary or not", () => {
    const baseline = rec("a", "r00", null);
    expect(comparableToParent(baseline, null, null)).toEqual({
      comparable: null,
      basis: "no-parent",
    });
    expect(comparableToParent(baseline, parent, summary("r00", null))).toEqual({
      comparable: null,
      basis: "no-parent",
    });
  });

  it("still refuses on its own when the eval set changed", () => {
    // The hash comparison may refuse but never affirm: a changed eval set
    // makes the comparison impossible whatever any file says, so this holds
    // with no summary and outranks a summary that disagrees.
    expect(comparableToParent(rec("b", "r02", "r01"), parent, null)).toEqual({
      comparable: false,
      basis: "eval-set-changed",
    });
    expect(
      comparableToParent(rec("b", "r02", "r01"), parent, summary("r02", true)),
    ).toEqual({ comparable: false, basis: "eval-set-changed" });
  });

  it("does not refuse from a round that is not actually the parent", () => {
    // The pane finds the parent by index, which is usually right but is not
    // guaranteed; a stranger's hash says nothing about this comparison.
    expect(
      comparableToParent(child, rec("b", "rXX", null), summary("r02", true)),
    ).toEqual({ comparable: true, basis: "summary" });
  });

  it("falls back to the summary when a hash is missing rather than guessing", () => {
    expect(
      comparableToParent(rec("", "r02", "r01"), parent, summary("r02", true)),
    ).toEqual({ comparable: true, basis: "summary" });
    expect(comparableToParent(rec("", "r02", "r01"), parent, null)).toEqual({
      comparable: null,
      basis: "unknown",
    });
  });
});
