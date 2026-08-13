// Tolerant parser for `loop-*/trajectory.json` under the `results` root — the
// flywheel round timeline. Accepts a JSON array, `{rounds: [...]}`, or JSONL.
//
// Two schemas are mapped, in priority order:
//
//   1. The real producer, `src/turing/research/loop/trajectory.py`
//      (`encode_trajectory_row`): `round`, `primary`, `delta`,
//      `delta_in_noise_units`, `cost`, `human_interventions`, `constraints`,
//      `saturation`, `verdict`. Every per-cell field is an object keyed
//      `"<type>/<split>"` — there is deliberately no scalar `primary`.
//   2. The loose legacy shapes earlier research-loop iterations wrote
//      (`cell`/`phase`/`step`/`slug` labels, `passed`/`ok`/`success` booleans).
//
// Keeping (2) matters: old loop directories on disk still have to render. But
// (1) is what the loop writes today, and mapping it by the legacy rules gave
// a numbered list of "·" with a lineage hash blob — every round "other",
// every label a position string, every real number dropped for being an
// object rather than a scalar (#390).

export type RoundStatus = "pass" | "fail" | "saturated" | "refused" | "other";

export interface Round {
  index: number;
  label: string;
  status: RoundStatus;
  detail: string;
}

const INDEX_KEYS = ["round", "index"];
const LABEL_KEYS = ["cell", "phase", "step", "slug"];
const STATUS_KEYS = ["passed", "ok", "success"];

type Obj = Record<string, unknown>;

function isObj(v: unknown): v is Obj {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

function numberMap(v: unknown): Array<[string, number]> {
  if (!isObj(v)) return [];
  return Object.entries(v).filter(
    (e): e is [string, number] => typeof e[1] === "number",
  );
}

/** `0.6234` → `"0.62"`, trimming a trailing `.00` so whole numbers stay short. */
function fmtScore(n: number): string {
  return n.toFixed(2).replace(/\.00$/, "");
}

/** Signed, one decimal — deltas read as movement, so the sign always shows. */
function fmtSigned(n: number): string {
  return `${n >= 0 ? "+" : ""}${n.toFixed(1)}`;
}

function fmtCount(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return String(Math.round(n));
}

function fmtSeconds(n: number): string {
  if (n >= 3600) return `${(n / 3600).toFixed(1)}h`;
  if (n >= 60) return `${Math.round(n / 60)}m`;
  return `${Math.round(n)}s`;
}

/**
 * The cells this round scored, as the label.
 *
 * The real schema has no name field at all — a round's identity is its cells,
 * so those are the label. Two are shown in full and the rest counted, which
 * keeps the line readable in a pane that defaults to a third of a column.
 */
function cellLabel(obj: Obj): string | null {
  const cells = numberMap(obj.primary).map(([k]) => k);
  if (cells.length === 0) return null;
  if (cells.length <= 2) return cells.join(" ");
  return `${cells.slice(0, 2).join(" ")} +${cells.length - 2}`;
}

/**
 * Status from the structured fields the loop actually writes.
 *
 * `verdict` itself is a prose string (`round_verdict` joins assessment
 * reasons), so it is shown as detail rather than decoded into a glyph. The
 * machine-readable signals are the gates and the per-cell saturation verdicts.
 */
// Two of the three gates the runner writes are the *cause* of a refusal, not
// an independent failure. `noise_floor_available: false` is exactly the
// condition that produces `refused_no_noise_floor`, and `eval_set_stable:
// false` produces `refused_eval_set_changed`. Counting those as failures made
// every round of a perfectly ordinary run — one started without a noise-floor
// report, which the runner explicitly supports with a warning — render as a
// failed round. That is worse than the "flat" misreading refusals were kept
// distinct to avoid.
//
// `lineage_recorded` has no corresponding refusal and stays a real failure.
const REFUSAL_EXPLAINED_GATES: Record<string, string> = {
  noise_floor_available: "refused_no_noise_floor",
  eval_set_stable: "refused_eval_set_changed",
};

function realStatus(obj: Obj): RoundStatus | null {
  const assessments = Array.isArray(obj.saturation) ? obj.saturation : [];
  const verdicts = assessments
    .filter(isObj)
    .map((a) => a.verdict)
    .filter((v): v is string => typeof v === "string");
  const seen = new Set(verdicts);

  const gates = isObj(obj.constraints) ? Object.entries(obj.constraints) : [];
  const failed = gates.some(([name, value]) => {
    if (value !== false) return false;
    const explanation = REFUSAL_EXPLAINED_GATES[name];
    return explanation === undefined || !seen.has(explanation);
  });
  // A round that failed a gate for its own reasons is usually the most
  // informative round in the sweep — it is logged, not deleted, and it should
  // not read as "flat".
  if (failed) return "fail";

  // Gates passing says the round was valid, not that it improved — a round 0
  // baseline has no parent to be assessed against and carries no verdicts at
  // all. Claiming "improving" there would invent a result.
  if (verdicts.length === 0) return null;

  if (verdicts.includes("improving")) return "pass";
  // Refusals are not failures: they are the honest answer when the
  // measurement needed to make the call was never made. They must not read as
  // a round that came out flat.
  if (verdicts.every((v) => v.startsWith("refused"))) return "refused";
  if (verdicts.includes("saturated")) return "saturated";
  return "other";
}

/**
 * The driving-function numbers, which is what the loop is actually steered by:
 * per-cell primary score and gain in noise units, then cost and human-gate
 * load. Lineage (`run_id`, `eval_set_hash`) is deliberately not here — it was
 * all the old mapping managed to surface, and it is the least useful part.
 */
function realDetail(obj: Obj): string | null {
  const parts: string[] = [];

  const primary = numberMap(obj.primary);
  const noiseUnits = new Map(numberMap(obj.delta_in_noise_units));
  const rawDelta = new Map(numberMap(obj.delta));
  for (const [cell, score] of primary.slice(0, 3)) {
    const nu = noiseUnits.get(cell);
    const raw = rawDelta.get(cell);
    if (nu !== undefined)
      parts.push(`${cell} ${fmtScore(score)} ${fmtSigned(nu)}σ`);
    else if (raw !== undefined)
      parts.push(`${cell} ${fmtScore(score)} ${fmtSigned(raw)}`);
    else parts.push(`${cell} ${fmtScore(score)}`);
  }

  if (isObj(obj.cost)) {
    const { tokens, wall_clock_seconds: wall } = obj.cost;
    if (typeof tokens === "number") parts.push(`${fmtCount(tokens)} tok`);
    if (typeof wall === "number") parts.push(fmtSeconds(wall));
  }

  if (
    typeof obj.human_interventions === "number" &&
    obj.human_interventions > 0
  ) {
    parts.push(`${obj.human_interventions} esc`);
  }

  // The prose verdict last: it explains a refusal or a saturation call, which
  // the numbers alone don't.
  if (typeof obj.verdict === "string" && obj.verdict !== "")
    parts.push(obj.verdict);

  return parts.length > 0 ? parts.join(" · ") : null;
}

/** The legacy scalar-field detail — first six scalars, JSON-stringified. */
function legacyDetail(obj: Obj, used: Set<string>): string {
  const detailFields: string[] = [];
  for (const [k, v] of Object.entries(obj)) {
    if (used.has(k)) continue;
    if (
      v === null ||
      (typeof v !== "string" && typeof v !== "number" && typeof v !== "boolean")
    ) {
      continue;
    }
    detailFields.push(k);
    if (detailFields.length >= 6) break;
  }
  const detailObj: Obj = {};
  for (const k of detailFields) detailObj[k] = obj[k];
  return JSON.stringify(detailObj);
}

function toRound(raw: unknown, position: number): Round | null {
  if (!isObj(raw)) return null;
  const obj = raw;

  let index = position;
  for (const k of INDEX_KEYS) {
    if (typeof obj[k] === "number") {
      index = obj[k] as number;
      break;
    }
  }

  // Legacy keys win when present so old loop dirs render exactly as before.
  let label: string | null = null;
  let labelKeyUsed = false;
  for (const k of LABEL_KEYS) {
    if (obj[k] !== undefined && obj[k] !== null) {
      label = String(obj[k]);
      labelKeyUsed = true;
      break;
    }
  }
  label ??= cellLabel(obj) ?? String(position);

  let status: RoundStatus | null = null;
  let statusKeyUsed: string | null = null;
  for (const k of STATUS_KEYS) {
    if (typeof obj[k] === "boolean") {
      status = obj[k] ? "pass" : "fail";
      statusKeyUsed = k;
      break;
    }
  }
  status ??= realStatus(obj) ?? "other";

  const used = new Set<string>([...INDEX_KEYS, ...LABEL_KEYS]);
  if (statusKeyUsed) used.add(statusKeyUsed);
  // "Legacy wins when present" has to hold for detail too, not just label and
  // status. `realDetail` returns non-null as soon as it finds *any* field it
  // recognises — a `verdict` string, or a `cost` object — so a legacy row
  // carrying either would silently lose every other field it had. A row that
  // identified itself as legacy through its label or status key gets the
  // legacy detail.
  const isLegacyRow = labelKeyUsed || statusKeyUsed !== null;
  const detail = isLegacyRow
    ? legacyDetail(obj, used)
    : (realDetail(obj) ?? legacyDetail(obj, used));

  return { index, label, status, detail };
}

export function parseTrajectory(text: string): Round[] | null {
  const trimmed = text.trim();
  if (trimmed === "") return null;

  // A whole-file JSON array or `{rounds: [...]}` object is only genuinely
  // that shape if it parses as a single JSON value across the *entire*
  // text — a JSONL file's first line can also start with "{" or "[", so a
  // parse failure (or a parse that doesn't match either shape) falls
  // through to line-oriented JSONL parsing rather than giving up.
  if (trimmed.startsWith("[") || trimmed.startsWith("{")) {
    try {
      const parsed: unknown = JSON.parse(trimmed);
      if (Array.isArray(parsed)) {
        const rounds = parsed
          .map((r, i) => toRound(r, i))
          .filter((r): r is Round => r !== null);
        return rounds.length > 0 ? rounds : null;
      }
      if (isObj(parsed) && Array.isArray(parsed.rounds)) {
        const arr = parsed.rounds as unknown[];
        const rounds = arr
          .map((r, i) => toRound(r, i))
          .filter((r): r is Round => r !== null);
        return rounds.length > 0 ? rounds : null;
      }
    } catch {
      // fall through to JSONL
    }
  }

  const lines = text
    .split("\n")
    .map((l) => l.trim())
    .filter(Boolean);
  const rounds: Round[] = [];
  lines.forEach((line, i) => {
    try {
      const parsed: unknown = JSON.parse(line);
      const round = toRound(parsed, i);
      if (round) rounds.push(round);
    } catch {
      // skip unparseable line
    }
  });
  return rounds.length > 0 ? rounds : null;
}
