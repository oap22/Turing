// Parser for `loop-*/round-NN/round.json` — the full round artifact that sits
// beside `trajectory.json` (#390 part 1).
//
// The trajectory row is a summary; this is the superset, written by
// `TrajectoryStore.write_round_record` via `encode_round_record`
// (`src/turing/research/loop/trajectory.py`). It carries the per-cell delta
// detail the row flattens away — `beats_noise_floor`, `cost_per_unit_gain`,
// the noise floor each gain is measured against — plus the lineage that says
// whether the round is comparable to its parent at all.
//
// Tolerant in the same way `flywheel.ts` is: every field is optional, because
// a round mid-write or from an older loop should render what it has rather
// than blanking. Nothing here throws.

/** `round-07`, matching `round_dir()`'s `f"round-{round_index:02d}"`. */
export function roundDirName(index: number): string {
  return `round-${String(index).padStart(2, "0")}`;
}

export interface CellDelta {
  cell: string;
  marginalGain: number | null;
  noiseFloor: number | null;
  beatsNoiseFloor: boolean | null;
  gainInNoiseUnits: number | null;
  costPerUnitGain: number | null;
}

export interface CellScore {
  cell: string;
  n: number | null;
  meanScore: number | null;
  correctnessPassRate: number | null;
}

/**
 * One row of `problems[]` — a single problem's own result, before it is
 * reduced into a cell mean.
 *
 * Problem ids are free-form path components (`cuda/matmul-speedup`,
 * `kaggle/titanic`, `flat-baseline`), and nothing else in the artifact carries
 * them: `type_scores[].scores` is a map the cell flattening drops, and the
 * trajectory row never had them. Not parsing this array is why a real,
 * supported problem id appeared nowhere in the pane as text — the operator
 * could see that `speedup/practice` scored 2.22 over n=2 but not *which two
 * problems* that was, so a corpus that silently changed composition looked
 * identical to one that did not.
 */
export interface ProblemScore {
  /** `cuda/matmul-speedup` — free-form, may contain slashes. */
  problemId: string;
  /** `"speedup/practice"`, the cell this problem is reduced into. */
  cell: string;
  score: number | null;
  /** `speedup_ratio` / `leaderboard_percentile` — scales are not comparable. */
  scoreScale: string | null;
  scored: boolean | null;
  passedCorrectness: boolean | null;
}

export interface RoundRecord {
  roundIndex: number | null;
  runId: string | null;
  parentRoundId: string | null;
  evalSetHash: string | null;
  createdAtMs: number | null;
  engine: {
    backend: string | null;
    orchestratorModel: string | null;
    substepModel: string | null;
    scaffoldGitSha: string | null;
  };
  scores: CellScore[];
  problems: ProblemScore[];
  deltas: CellDelta[];
  saturation: Array<{
    cell: string;
    verdict: string | null;
    reason: string | null;
  }>;
  cost: {
    wallClockSeconds: number | null;
    tokens: number | null;
    attempts: number | null;
  };
  escalationCount: number | null;
  gates: Array<{ name: string; passed: boolean }>;
  verdict: string | null;
}

type Obj = Record<string, unknown>;

function isObj(v: unknown): v is Obj {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function num(v: unknown): number | null {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function str(v: unknown): string | null {
  return typeof v === "string" ? v : null;
}

function bool(v: unknown): boolean | null {
  return typeof v === "boolean" ? v : null;
}

/** `"speedup/practice"`, the same key the trajectory row uses for cells. */
function cellKey(o: Obj): string {
  const type = str(o.problem_type) ?? "?";
  const split = str(o.split) ?? "?";
  return `${type}/${split}`;
}

function arr(v: unknown): Obj[] {
  return Array.isArray(v) ? v.filter(isObj) : [];
}

export function parseRoundRecord(text: string): RoundRecord | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return null;
  }
  if (!isObj(parsed)) return null;
  const o = parsed;

  const engine = isObj(o.engine) ? o.engine : {};
  const cost = isObj(o.cost) ? o.cost : {};

  return {
    roundIndex: num(o.round_index),
    runId: str(o.run_id),
    parentRoundId: str(o.parent_round_id),
    evalSetHash: str(o.eval_set_hash),
    createdAtMs: num(o.created_at_ms),
    engine: {
      backend: str(engine.backend),
      orchestratorModel: str(engine.orchestrator_model),
      substepModel: str(engine.substep_model),
      scaffoldGitSha: str(engine.scaffold_git_sha),
    },
    scores: arr(o.type_scores).map((ts) => ({
      cell: cellKey(ts),
      n: num(ts.n),
      meanScore: num(ts.mean_score),
      correctnessPassRate: num(ts.correctness_pass_rate),
    })),
    problems: arr(o.problems).map((p) => ({
      // "?" rather than dropping the row: a problem the pane cannot name still
      // ran and still moved the cell mean, and hiding it is the failure this
      // array was surfaced to fix.
      problemId: str(p.problem_id) ?? "?",
      cell: cellKey(p),
      score: num(p.score),
      scoreScale: str(p.score_scale),
      scored: bool(p.scored),
      passedCorrectness: bool(p.passed_correctness),
    })),
    deltas: arr(o.deltas).map((d) => ({
      cell: cellKey(d),
      marginalGain: num(d.marginal_gain),
      noiseFloor: num(d.noise_floor),
      beatsNoiseFloor: bool(d.beats_noise_floor),
      gainInNoiseUnits: num(d.gain_in_noise_units),
      costPerUnitGain: num(d.cost_per_unit_gain),
    })),
    saturation: arr(o.saturation).map((a) => ({
      cell: cellKey(a),
      verdict: str(a.verdict),
      reason: str(a.reason),
    })),
    cost: {
      wallClockSeconds: num(cost.wall_clock_seconds),
      tokens: num(cost.tokens),
      attempts: num(cost.attempts),
    },
    escalationCount: num(o.escalation_count),
    // `gates` is a plain name → bool map; a non-bool value is dropped rather
    // than coerced, since a truthy string would silently read as a pass.
    gates: isObj(o.gates)
      ? Object.entries(o.gates)
          .filter((e): e is [string, boolean] => typeof e[1] === "boolean")
          .map(([name, passed]) => ({ name, passed }))
      : [],
    verdict: str(o.verdict),
  };
}

// ---------------------------------------------------------------------------
// The round summary — `round-NN/metrics.json`
// ---------------------------------------------------------------------------
//
// Written by the reporting layer beside `round.json`. The pane reads exactly
// one thing from it — `comparable_to_parent` — because that field is the only
// place on disk that answers the operator-facing question, and `round.json`
// (`encode_round_record`) has no such field at all.
//
// Two files spell the field the same way and mean different things, on
// purpose (`docs/research-agent.md`): `trajectory.json`'s row answers "did the
// parent measure the same eval set?", computed from the two hashes alone,
// because `trajectory_restart` must derive from that and a lost attempt must
// not trip it. This summary answers the stricter question — "may these numbers
// be compared with the parent's?" — which is also false when this round lost
// an attempt, when the parent did, or when the parent is silent about whether
// it did. The pane is read by an operator, so it wants the stricter one.

export interface RoundSummary {
  roundIndex: number | null;
  runId: string | null;
  parentRoundId: string | null;
  /** `null` is a real value here: round 0 has no parent to be comparable to. */
  comparableToParent: boolean | null;
}

export function parseRoundSummary(text: string): RoundSummary | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return null;
  }
  if (!isObj(parsed)) return null;
  return {
    roundIndex: num(parsed.round_index),
    runId: str(parsed.run_id),
    parentRoundId: str(parsed.parent_round_id),
    comparableToParent: bool(parsed.comparable_to_parent),
  };
}

/** Where a comparability answer came from, so the label can say what it knows. */
export type ComparabilityBasis =
  /** The persisted `comparable_to_parent` in `round-NN/metrics.json`. */
  | "summary"
  /** The two eval-set hashes disagree — comparison is impossible, full stop. */
  | "eval-set-changed"
  /** Round 0, or a round that names no parent. */
  | "no-parent"
  /** Nothing on disk answers it; the pane must not guess. */
  | "unknown";

export interface Comparability {
  comparable: boolean | null;
  basis: ComparabilityBasis;
}

/** The summary must be the one belonging to this round, not a neighbour's. */
function summaryDescribes(summary: RoundSummary, record: RoundRecord): boolean {
  if (
    summary.runId !== null &&
    record.runId !== null &&
    summary.runId !== record.runId
  ) {
    return false;
  }
  return !(
    summary.roundIndex !== null &&
    record.roundIndex !== null &&
    summary.roundIndex !== record.roundIndex
  );
}

/**
 * Whether this round's numbers may be compared with its parent's.
 *
 * Read from the round summary, not recomputed. Recomputing from eval-set
 * hashes answers a strictly weaker question — matching hashes are necessary
 * for comparability but nowhere near sufficient — and that is precisely the
 * bug this replaced: a round that lost an attempt, refused both of its cells
 * with `refused_attempt_lost`, failed `all_attempts_completed` and recorded
 * `"comparable_to_parent": false` in its own summary still painted a green
 * "comparable to parent" in the same box as all of it, because the hashes
 * matched.
 *
 * An absent or unreadable summary therefore reports *unknown* rather than
 * falling back to the hash comparison. The fallback is not a weaker version of
 * the right answer, it is a different answer that happens to be true more
 * often, and it fails in the one direction that matters: it says "comparable"
 * — green, unqualified — for exactly the rounds whose measurement is broken.
 * Unknown costs a label on old rounds; the fallback costs an operator
 * believing a delta that no longer exists.
 *
 * The hash comparison is still allowed to *refuse*, never to affirm: a changed
 * eval set makes comparison impossible whatever any file says, so that
 * direction is sound on its own and is kept for rounds with no summary.
 */
export function comparableToParent(
  record: RoundRecord,
  parent: RoundRecord | null,
  summary: RoundSummary | null,
): Comparability {
  if (!record.parentRoundId) return { comparable: null, basis: "no-parent" };

  // The caller finds the parent by index (round-NN minus one), which is the
  // parent in the ordinary case but not guaranteed to be. If the candidate's
  // own run id isn't the one this round names as its parent, we have not found
  // the parent and its hash says nothing about this comparison.
  if (
    parent &&
    parent.runId === record.parentRoundId &&
    record.evalSetHash &&
    parent.evalSetHash &&
    record.evalSetHash !== parent.evalSetHash
  ) {
    return { comparable: false, basis: "eval-set-changed" };
  }

  if (
    summary &&
    summary.comparableToParent !== null &&
    summaryDescribes(summary, record)
  ) {
    return { comparable: summary.comparableToParent, basis: "summary" };
  }

  return { comparable: null, basis: "unknown" };
}
