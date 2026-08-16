// Pure parsing/aggregation for the metrics pane: `metrics.jsonl` (one JSON
// object per line, tolerant of a trailing partial line) or a whole-file
// JSON array of points.

export type Point = Record<string, number> & { step?: number };

// ---------------------------------------------------------------------------
// Which files are runs, and what a run is called
// ---------------------------------------------------------------------------
//
// The results tree contains two files whose basename starts with `metrics.`,
// and they are not the same kind of thing:
//
//   round-NN/attempts/<problem-id>/metrics.jsonl   one JSON object per solver
//                                                  step — the chart series
//   round-NN/attempts/<problem-id>/metrics.json    `write_attempt_summary` —
//                                                  a pretty-printed JSON
//                                                  *object* of totals
//   round-NN/metrics.json                          `write_round_summary` —
//                                                  the per-cell round record
//
// The pane used to accept both basenames as runs. That is not a cosmetic
// mistake: `fs_list` returns newest-mtime-first and the round summary is the
// LAST metrics-named file a round writes, so auto-follow ("newest run")
// reliably landed on `round-NN/metrics.json` the instant a round finished.
// `parseMetricsText` yields zero points for a bare object — deliberately, see
// #401, and that behaviour stays — so the operator watching a run complete got
// "no metrics yet" on the default workspace. On the real emitted tree 9 of the
// 18 offered "runs" were summaries with no series at all.
//
// So: summaries are excluded from the run list outright rather than listed and
// annotated. The run picker's entire job is choosing what to chart, and a
// summary has no series *by construction* — listing one only offers a row that
// can do nothing but blank the chart, and auto-follow would still need to skip
// it. The summaries lose nothing by this: they remain the citable per-attempt
// and per-round record, read by `verify` and by the round-record UI, which is
// where a reader who wants totals rather than a curve should be looking.
const CHART_RUN_BASENAME = "metrics.jsonl";

/** True for the step log the chart can actually draw; false for summaries. */
export function isChartRunFile(relPath: string): boolean {
  return (relPath.split("/").pop() ?? "") === CHART_RUN_BASENAME;
}

// A run's identity is its *directory* relative to the results root — exactly
// the string the loop emits into `.viewer.json`'s `runs`
// (`RoundRunner._viewer_runs`, which globs `round-*/attempts/**/metrics.jsonl`
// and reports `match.parent`). Using the same string as label, as match key,
// and as chart-series identity keeps the pane and the control file speaking
// one vocabulary, so a `runs` entry an operator copies out of the listbox is
// the entry that selects that row.
//
// It is also the only label that distinguishes the rows. The old label was the
// first path segment, i.e. the loop name — every run under one loop rendered
// the identical string and the real listbox showed 18 rows all reading
// `loop-probe`. The round and the (possibly nested, e.g. `cuda/matmul-speedup`)
// problem id are what tell them apart, so the whole path is kept.
export function runIdOf(relPath: string): string {
  const cut = relPath.lastIndexOf("/");
  // A run file directly at the results root has no directory to name; fall
  // back to the file's own path rather than to the empty string.
  return cut > 0 ? relPath.slice(0, cut) : relPath;
}

/**
 * Does `relPath` (a run file) belong to one of `.viewer.json`'s `runs` entries?
 *
 * `runs` entries name directories while run files name `<dir>/metrics.jsonl`,
 * and reconciling that by prefix would be wrong, not merely loose: the runner
 * rotates a superseded chain into `<dir>/prior-N/metrics.jsonl` and
 * deliberately omits it from `runs`, yet `.../matmul-speedup/prior-1/...`
 * starts with `.../matmul-speedup`. A prefix rule would therefore draw a
 * superseded attempt's curve as if the loop had asked for it. The comparison
 * is equality on the derived run id instead, so a sibling that merely shares a
 * path prefix can never match.
 *
 * (The previous code compared `runs` against the first path segment, which no
 * multi-segment entry could ever equal — the field was inert: re-delivering
 * the real `.viewer.json` selected nothing at all.)
 */
export function matchesViewerRuns(relPath: string, runs: readonly string[]): boolean {
  const id = runIdOf(relPath);
  for (const raw of runs) {
    // Tolerate a hand-written trailing slash; nothing else is normalized,
    // because every other difference is a genuinely different path.
    const entry = raw.endsWith("/") ? raw.slice(0, -1) : raw;
    // The emitter writes the directory. An entry naming the run file itself is
    // accepted too — that is the same identity spelled out, not a new format.
    if (entry === id || entry === relPath) return true;
  }
  return false;
}

export function parseMetricsText(text: string): Point[] {
  const trimmed = text.trim();
  if (trimmed === "") return [];

  if (trimmed.startsWith("[")) {
    try {
      const arr: unknown = JSON.parse(trimmed);
      if (Array.isArray(arr)) {
        return arr.map(cleanPoint).filter((p): p is Point => p !== null);
      }
    } catch {
      // fall through to line-oriented parsing
    }
  }

  const points: Point[] = [];
  for (const line of text.split("\n")) {
    const t = line.trim();
    if (t === "") continue;
    let parsed: unknown;
    try {
      parsed = JSON.parse(t);
    } catch {
      continue;
    }
    const point = cleanPoint(parsed);
    if (point) points.push(point);
  }
  return points;
}

function cleanPoint(value: unknown): Point | null {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return null;
  const out: Point = {};
  for (const [k, v] of Object.entries(value as Record<string, unknown>)) {
    if (typeof v === "number" && Number.isFinite(v)) {
      out[k] = v;
    }
  }
  return out;
}

const EXCLUDED_SERIES_KEYS = new Set(["step", "total_steps", "ts"]);

export function seriesOf(points: Point[]): Map<string, Array<[number, number]>> {
  const series = new Map<string, Array<[number, number]>>();
  points.forEach((p, i) => {
    const x = typeof p.step === "number" ? p.step : i;
    for (const [k, v] of Object.entries(p)) {
      if (EXCLUDED_SERIES_KEYS.has(k)) continue;
      if (typeof v !== "number") continue;
      const arr = series.get(k) ?? [];
      arr.push([x, v]);
      series.set(k, arr);
    }
  });
  return series;
}

// Tab-selection fallback for the single active series MetricsPane shows:
// prefer whatever's persisted (localStorage `turing.metrics.series`) if it's
// still among the discovered series, else "loss" if present, else the first
// series alphabetically, else null (nothing to show yet).
export function pickSeries(available: string[], stored: string | null): string | null {
  if (stored !== null && available.includes(stored)) return stored;
  if (available.includes("loss")) return "loss";
  if (available.length === 0) return null;
  return [...available].sort()[0];
}

export interface Eta {
  stepsPerSec: number | null;
  etaSec: number | null;
  lastStep: number | null;
  totalSteps: number | null;
}

function normalizeTs(ts: number): number {
  // Epoch seconds vs. milliseconds: anything past ~2001 in ms terms (1e12)
  // is already ms; smaller values are seconds.
  return ts > 1e12 ? ts : ts * 1000;
}

export function etaOf(points: Point[], arrivalTimesMs: number[]): Eta {
  let totalSteps: number | null = null;
  let lastStep: number | null = null;
  for (const p of points) {
    if (typeof p.total_steps === "number") totalSteps = p.total_steps;
    if (typeof p.step === "number") lastStep = p.step;
  }
  if (lastStep === null && points.length > 0) {
    lastStep = points.length - 1;
  }

  const tsPoints = points.filter((p) => typeof p.ts === "number");
  let stepsPerSec: number | null = null;

  if (tsPoints.length >= 2) {
    const first = tsPoints[0];
    const last = tsPoints[tsPoints.length - 1];
    const t0 = normalizeTs(first.ts as number);
    const t1 = normalizeTs(last.ts as number);
    const dtSec = (t1 - t0) / 1000;
    const step0 = typeof first.step === "number" ? first.step : 0;
    const step1 = typeof last.step === "number" ? last.step : tsPoints.length - 1;
    const dSteps = step1 - step0;
    if (dtSec > 0 && dSteps > 0) {
      stepsPerSec = dSteps / dtSec;
    }
  } else if (arrivalTimesMs.length >= 2) {
    const t0 = arrivalTimesMs[0];
    const t1 = arrivalTimesMs[arrivalTimesMs.length - 1];
    const dtSec = (t1 - t0) / 1000;
    const dChunks = arrivalTimesMs.length - 1;
    if (dtSec > 0 && dChunks > 0) {
      stepsPerSec = dChunks / dtSec;
    }
  }

  let etaSec: number | null = null;
  if (totalSteps !== null && stepsPerSec !== null && stepsPerSec > 0 && lastStep !== null) {
    const remaining = totalSteps - lastStep;
    etaSec = remaining > 0 ? remaining / stepsPerSec : 0;
  }

  return { stepsPerSec, etaSec, lastStep, totalSteps };
}

// `.viewer.json` — the agent-driven (and hand-editable) control file that
// tells this pane what to show. It is a plain JSON file in the results root;
// editing it in vim is as supported as an agent writing it.
//
//   {
//     "series": "progress",
//     "runs": ["loop-probe/round-00/attempts/cuda/matmul-speedup"],
//     "titles": { "progress": "progress toward target" }
//   }
//
// `runs` entries are run *directories* relative to the results root — the
// path holding `metrics.jsonl`, exactly as `RoundRunner._viewer_runs` writes
// them. `matchesViewerRuns` above is what turns one into a selected row.
//
// Every key is optional and independently validated: a malformed or unknown
// key is ignored rather than rejecting the whole file, so a half-written
// edit degrades to "show less" instead of "show nothing".
export interface ViewerFile {
  series?: string;
  runs?: string[];
  /** series name → display title for the chart's label row. */
  titles?: Record<string, string>;
}

// ---------------------------------------------------------------------------
// `metrics.verdict.json` — whether the run being charted verifies
// ---------------------------------------------------------------------------
//
// The chain (`_chain` per line, `metrics.chain.json` beside it) and the
// verifier that recomputes it are Python, and this pane is not. So the loop
// runs `verify_run` itself at the end of every attempt and writes the answer
// beside the summary (`results.write_attempt_verdict`); this reads it back.
//
// What that buys, exactly: the operator reading a curve can see whether the
// log under it recomputes. What it does NOT buy — and what the badge's own
// text has to keep saying — is that the numbers are genuine. The chain is
// self-referential (`integrity.py`'s module docstring concedes a
// self-consistent forgery is undetectable by construction), it is written by
// the same account that could alter it, and a wrong verifier's wrong numbers
// chain perfectly. A green badge here means "not casually altered, as of the
// loop's own last check", nothing more.

const VERDICT_BASENAME = "metrics.verdict.json";

/** The verdict file that belongs to one `metrics.jsonl` run file. */
export function verdictPathOf(runFilePath: string): string {
  const cut = runFilePath.lastIndexOf("/");
  return cut < 0 ? VERDICT_BASENAME : `${runFilePath.slice(0, cut + 1)}${VERDICT_BASENAME}`;
}

/** `verify.RunState` — the three answers the Python verifier can give. */
export type VerdictState = "ok" | "incomplete" | "failed";

/** One parsed `metrics.verdict.json`. Field names are camelCased; the file's
 * are snake_case, matching `results.write_attempt_verdict`'s payload. */
export interface RunVerdict {
  state: VerdictState;
  /** `chain.lines_checked` — how many log lines the loop's check covered. */
  linesChecked: number;
  checkedAtMs: number | null;
  checkedBy: string | null;
  chainHead: string | null;
  /** `verify.format_run_verdict`'s sentence, verbatim. */
  detail: string | null;
}

const VERDICT_STATES = new Set<string>(["ok", "incomplete", "failed"]);

/**
 * Parse a `metrics.verdict.json` body, or return `null`.
 *
 * Stricter than `parseViewerFile`, and deliberately so: that file is a
 * *request* the pane can honour partially, this one is a *claim about
 * evidence*. A body missing `state` or `lines_checked`, or carrying a state
 * this reader does not know, is treated as no verdict at all — which the badge
 * shows as `unverified`. The failure mode of guessing here would be a badge
 * that reassures on a file it did not understand.
 */
export function parseVerdictFile(text: string): RunVerdict | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return null;
  const o = parsed as Record<string, unknown>;
  if (typeof o.state !== "string" || !VERDICT_STATES.has(o.state)) return null;
  if (typeof o.lines_checked !== "number" || !Number.isFinite(o.lines_checked)) return null;
  return {
    state: o.state as VerdictState,
    linesChecked: o.lines_checked,
    checkedAtMs: typeof o.checked_at_ms === "number" ? o.checked_at_ms : null,
    checkedBy: typeof o.checked_by === "string" ? o.checked_by : null,
    chainHead: typeof o.chain_head === "string" ? o.chain_head : null,
    detail: typeof o.detail === "string" ? o.detail : null,
  };
}

/** What the badge shows. Five states, not three: two of them are facts about
 * the *pane's* relationship to the verdict rather than about the run. */
export type BadgeState = "verified" | "stale" | "incomplete" | "failed" | "unverified";

/**
 * Fold a verdict and the pane's own line count into one badge state.
 *
 * The order of the rules is the whole design:
 *
 * 1. **No verdict → `unverified`.** Never "probably fine".
 * 2. **`failed` → `failed`, whatever the line count is doing.** A failure is
 *    never softened into `stale`; otherwise appending a single line to a log
 *    whose chain does not recompute would downgrade an accusation to a shrug,
 *    which is a one-line evasion nobody should be handed.
 * 3. **Line counts disagree → `stale`.** The loop checked N lines and the pane
 *    parsed M; a verdict about N says nothing about line N+1. This is also the
 *    ordinary state of a run that is still going — the verdict is written once
 *    the attempt ends, so a live chain outruns it — which is exactly why the
 *    badge must not read as either "good" or "broken" here.
 * 4. **`incomplete` → `incomplete`**: an intact chain with no summary, or one
 *    a writer was still appending to when the loop looked.
 * 5. Otherwise `verified` — and see `badgeLabelOf` for why that word never
 *    reaches the operator's eyes on its own.
 */
export function badgeStateOf(
  verdict: RunVerdict | null | undefined,
  parsedLineCount: number,
): BadgeState {
  if (!verdict) return "unverified";
  if (verdict.state === "failed") return "failed";
  if (verdict.linesChecked !== parsedLineCount) return "stale";
  if (verdict.state === "incomplete") return "incomplete";
  return "verified";
}

const BADGE_LABELS: Record<BadgeState, string> = {
  // "chain ok", never "verified" or "trusted": the claim is about the hash
  // chain recomputing, not about the numbers being real, and a chip that
  // said the latter would be the one lie this whole feature exists to avoid.
  verified: "✓ chain ok",
  stale: "≠ chain stale",
  incomplete: "? chain incomplete",
  failed: "✗ chain FAILED",
  unverified: "· unverified",
};

/** The badge's visible text. Glyph *and* word per state, matching the
 * flywheel pane's rule that colour is never the only carrier of a status. */
export function badgeLabelOf(state: BadgeState): string {
  return BADGE_LABELS[state];
}

/**
 * The qualifier every badge carries, verbatim, with `<dir>` for the run.
 *
 * This is the same refusal-to-overclaim `verify`'s own `HONESTY_LINE` makes on
 * every invocation of the CLI. It is not decoration: a bare green chip beside
 * a chart is read as "these numbers are real", and nothing in this program can
 * establish that.
 */
export const VERDICT_QUALIFIER =
  "chain internally consistent as last checked by the loop — not proof the numbers are " +
  "authentic or meaningful; run `python -m turing.research.loop.verify <dir>` for an " +
  "independent check";

const QUALIFIER_SPLIT = "— not proof";
const [VERIFIED_CLAUSE, QUALIFIER_TAIL] = [
  VERDICT_QUALIFIER.slice(0, VERDICT_QUALIFIER.indexOf(QUALIFIER_SPLIT)).trim(),
  VERDICT_QUALIFIER.slice(VERDICT_QUALIFIER.indexOf(QUALIFIER_SPLIT)),
];

/** What each state actually claims, in place of the verified clause. */
function clauseOf(state: BadgeState, verdict: RunVerdict | null | undefined): string {
  switch (state) {
    case "verified":
      return VERIFIED_CLAUSE;
    case "stale":
      return (
        `the loop last checked ${verdict?.linesChecked ?? 0} line(s); this pane has parsed a ` +
        "different number, so the verdict does not describe what you are reading (the ordinary " +
        "state of a run still being written)"
      );
    case "incomplete":
      return "the loop's check found this run unfinished: an intact chain with no summary, or one a writer was still appending to";
    case "failed":
      return "the loop's check FAILED here: the log does not recompute, or its summary disagrees with it";
    case "unverified":
      return "no verdict file beside this run — the loop never recorded a check here";
  }
}

/** The badge's `title`: what this state claims, the qualifier, and the loop's
 * own sentence when it left one. */
export function badgeTitleOf(
  state: BadgeState,
  verdict: RunVerdict | null | undefined,
  runDir: string,
): string {
  const parts = [`${clauseOf(state, verdict)} ${QUALIFIER_TAIL.replace("<dir>", runDir)}`];
  // Verbatim from `verify.format_run_verdict`, so the badge and the terminal
  // cannot tell an operator two different stories about one run.
  if (verdict?.detail) parts.push(`loop: ${verdict.detail}`);
  return parts.join("\n");
}

export function parseViewerFile(text: string): ViewerFile | null {
  try {
    const parsed: unknown = JSON.parse(text);
    if (typeof parsed !== "object" || parsed === null) return null;
    const o = parsed as Record<string, unknown>;
    const out: ViewerFile = {};
    if (typeof o.series === "string") out.series = o.series;
    if (Array.isArray(o.runs) && o.runs.every((r) => typeof r === "string")) {
      out.runs = o.runs as string[];
    }
    // Must be a plain string→string map. An array passes `typeof === "object"`
    // in JS, hence the explicit exclusion.
    if (
      typeof o.titles === "object" &&
      o.titles !== null &&
      !Array.isArray(o.titles) &&
      Object.values(o.titles).every((v) => typeof v === "string")
    ) {
      out.titles = o.titles as Record<string, string>;
    }
    return out;
  } catch {
    return null;
  }
}
