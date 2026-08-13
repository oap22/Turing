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

/**
 * Whether this round may be compared to its parent at all.
 *
 * Lineage is not decoration: rounds measured on different eval sets must not
 * be compared, and `assert_comparable_to` in the contracts refuses to. The
 * detail view says so explicitly rather than showing a delta that means
 * nothing. `null` when there is no parent to compare against (round 0).
 */
export function comparableToParent(
  record: RoundRecord,
  parent: RoundRecord | null,
): boolean | null {
  if (!parent || !record.parentRoundId) return null;
  // The caller finds the parent by index (round-NN minus one), which is the
  // parent in the ordinary case but not guaranteed to be. If the candidate's
  // own run id isn't the one this round names as its parent, we have not
  // found the parent and must say "unknown" rather than compare against the
  // wrong round.
  if (parent.runId !== record.parentRoundId) return null;
  if (!record.evalSetHash || !parent.evalSetHash) return null;
  return record.evalSetHash === parent.evalSetHash;
}
