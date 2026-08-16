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
// **The contract this badge keeps.** `verified` means the verdict file's
// `chain_head` equals the `_chain` digest of the LAST line the pane has parsed
// for that run — the verdict is bound to the bytes, not to a line count — and
// every state the badge shows is derived from files that are currently beside
// the run, re-read whenever the run's own file changes.
//
// Both halves of that are load-bearing, and each one exists because the other
// half alone was not enough:
//
//   * *Bound to the bytes.* A line count is a property two different logs can
//     share. A re-drive moves a finished attempt's metrics trio and its
//     verdict together into `prior-N/` and a fresh, unrelated chain takes
//     their place at the same path; a badge turning on "the loop checked 4
//     lines and I parsed 4" would go green over bytes nobody checked. The
//     digest is the only field that cannot be re-earned by coincidence.
//   * *Currently beside the run.* Bytes bind the badge to a log, not to a
//     verdict that has since been rotated away. So the pane re-reads the
//     verdict on every change to the run file too — see `MetricsPane`'s
//     `fs-change` handler — rather than only when the verdict path itself
//     changes, which for a *renamed-away* file is an event the watcher never
//     sends (`fsroots.rs` skips paths that are not files at handler time).
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

//: `integrity.CHAIN_FIELD` — the field `MetricsWriter._append_sync` adds last.
const CHAIN_FIELD = "_chain";

/**
 * The digest half of the last line's `_chain`, or `null` if there isn't one.
 *
 * This is the pane's side of the byte binding. The Python that writes both
 * ends of it:
 *
 * ```python
 * digest = integrity.chain_next(self._chain_head, payload)
 * payload[integrity.CHAIN_FIELD] = f"{self._seq}:{digest}"      # the line
 * sidecar = {..., "final": digest, "lines": new_lines}          # the sidecar
 * ```
 *
 * and `results.write_attempt_verdict` copies `chain_head` straight out of that
 * sidecar's `"final"` (`_read_chain_head`). So for a log the pane has read to
 * the end, `chain_head` and this function's return value are the *same string*
 * — which is why comparing them is a statement about bytes rather than about
 * how many lines each side happened to count.
 *
 * The sequence number is deliberately dropped: `chain_head` records the digest
 * alone, and comparing a `"<seq>:"` prefix as well would make the two sides
 * disagree over a field neither is claiming anything about.
 *
 * Only the LAST non-blank line is consulted, and if that line is not an object
 * carrying a `"<seq>:<digest>"` `_chain`, the answer is `null` — not the
 * digest of some earlier line that does. `verify` on disk would fail a log
 * whose last line is garbage; a pane that skipped back to the last *good* line
 * would keep the badge green over exactly the bytes the verifier rejects.
 * `null` reads as `stale` in `badgeStateOf` (`failed` is reserved for what the
 * loop itself recorded), which is the honest state: the verdict does not
 * describe the pane's last line.
 *
 * `fs_tail` never hands this function a partial line — `fsroots.rs::tail_impl`
 * holds a trailing fragment back and returns whole lines only — so an
 * unparseable last line here is a real one, not a writer caught mid-append.
 */
export function chainDigestOfLastLine(text: string): string | null {
  const lines = text.split("\n");
  let t = "";
  for (let i = lines.length - 1; i >= 0; i--) {
    t = lines[i].trim();
    if (t !== "") break;
  }
  if (t === "") return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(t);
  } catch {
    return null;
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) return null;
  const raw = (parsed as Record<string, unknown>)[CHAIN_FIELD];
  if (typeof raw !== "string") return null;
  const cut = raw.indexOf(":");
  if (cut < 0) return null;
  const tail = raw.slice(cut + 1);
  return tail === "" ? null : tail;
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
 * Fold a verdict, the pane's last parsed digest, and its line count into one
 * badge state.
 *
 * The precedence is `failed` > `stale` > `incomplete` > `verified`, with
 * `unverified` standing outside it for "there is no verdict file". The order
 * is the whole design:
 *
 * 1. **No verdict → `unverified`.** Never "probably fine".
 * 2. **`failed` → `failed`, whatever the bytes are doing.** A failure is never
 *    softened into `stale`; otherwise appending a single line to a log whose
 *    chain does not recompute would downgrade an accusation to a shrug, which
 *    is a one-line evasion nobody should be handed.
 * 3. **The bytes do not match → `stale`.** `lastLineDigest` is the `_chain`
 *    digest of the last line *this pane* parsed; `chainHead` is the digest the
 *    loop's own check ended on. Unequal — or either one missing — means the
 *    verdict is not about the bytes on screen. This is the gate: a verdict
 *    with no recorded head cannot bind to anything, and a green badge there
 *    would be asserting a match nobody made.
 * 4. **Line counts disagree → `stale`.** Kept as a secondary check. The digest
 *    already catches every re-drive and every appended line, but a count is
 *    the field an operator can check by eye against the chart, and the two
 *    disagreeing is worth showing as "not current" rather than resolving in
 *    favour of the one that is easier to satisfy.
 * 5. **`incomplete` → `incomplete`**: an intact chain with no summary, or one
 *    a writer was still appending to when the loop looked.
 * 6. Otherwise `verified` — and see `badgeLabelOf` for why that word never
 *    reaches the operator's eyes on its own.
 *
 * Note what is *not* in this list: "the run is still being written". A live
 * attempt has no verdict file beside it at all — `write_attempt_verdict` runs
 * after the summary, once nothing is appending — so a run in progress reads
 * `unverified`, never `stale`.
 */
export function badgeStateOf(
  verdict: RunVerdict | null | undefined,
  parsedLineCount: number,
  lastLineDigest: string | null,
): BadgeState {
  if (!verdict) return "unverified";
  if (verdict.state === "failed") return "failed";
  if (verdict.chainHead === null || verdict.chainHead !== lastLineDigest) return "stale";
  if (verdict.linesChecked !== parsedLineCount) return "stale";
  if (verdict.state === "incomplete") return "incomplete";
  return "verified";
}

//: Path segments that name the results layout rather than the run. Keeping
//: `attempts` in a two-segment tail would spend half of it on a word every
//: run shares.
const LAYOUT_SEGMENTS = new Set(["attempts"]);

/**
 * The shortest tail of a run id that still tells it from its siblings.
 *
 * Chips stack in a column a few characters wide, so they cannot carry the full
 * `loop/round-NN/attempts/<problem-id>` the listbox and the ETA strip show.
 * They still have to carry *something*: a stack of identically-worded chips
 * distinguished only by an attribute is unreadable in exactly the case the
 * badge exists for — one red chip among several green ones, and no way to say
 * which run failed without a mouse.
 *
 * Two segments is what a nested problem id (`cuda/matmul-speedup`) needs, and
 * what a flat one spends on its round instead (`round-00/bad-instrument`).
 */
export function badgeRunTailOf(runId: string): string {
  const segments = runId.split("/").filter((s) => s !== "" && !LAYOUT_SEGMENTS.has(s));
  const tail = segments.slice(-2).join("/");
  return tail === "" ? runId : tail;
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

/**
 * The command that checks this run independently, as the operator would type
 * it. `verify.main` takes a path from the shell's cwd, not from the pane's
 * results root, so a bare run id is a command that fails; the pane passes the
 * root it is actually reading, and when there is none to name the placeholder
 * is said out loud rather than quietly dropped.
 */
function verifyCommandPathOf(runDir: string, resultsRoot: string | null | undefined): string {
  return `${resultsRoot ?? "<results-root>"}/${runDir}`;
}

/** What each state actually claims, in place of the verified clause. */
function clauseOf(state: BadgeState, verdict: RunVerdict | null | undefined): string {
  switch (state) {
    case "verified":
      return VERIFIED_CLAUSE;
    case "stale":
      // NOT "the ordinary state of a run still being written", which is what
      // this used to say and is a state that cannot occur: the verdict file is
      // written after the attempt ends, so a live run has none beside it and
      // renders `unverified`. Describing an impossible benign case taught the
      // operator to shrug at the one badge meaning the pane and the loop are
      // looking at different bytes.
      return (
        "this pane's last parsed line is not the line the loop's check ended on " +
        `(it checked ${verdict?.linesChecked ?? 0} line(s)) — either this pane is behind the ` +
        "file, or the directory was re-driven and this verdict describes a generation that is " +
        "no longer here"
      );
    case "incomplete":
      return "the loop's check found this run unfinished: an intact chain with no summary, or one a writer was still appending to";
    case "failed":
      return "the loop's check FAILED here: the log does not recompute, or its summary disagrees with it";
    case "unverified":
      // `undefined` is "the read has not come back", `null` is "the read came
      // back and there is nothing there". One visual state, because the pane
      // has no evidence either way in both — but not one sentence, because
      // only one of them is a fact about the run.
      return verdict === undefined
        ? "reading the verdict beside this run…"
        : "no verdict file beside this run — the loop never recorded a check here";
  }
}

/**
 * How each state closes.
 *
 * Only `verified` gets the honesty tail. Welded onto the others it produced a
 * double-em-dash run-on whose hedge attached to the wrong clause: "the loop's
 * check FAILED here … — not proof the numbers are authentic or meaningful"
 * reads as though the *failure* were being walked back, which is the one
 * reading this badge must never allow. Every state still offers the
 * independent check, because "go look yourself" is useful from all five.
 */
function closerOf(state: BadgeState, commandPath: string): string {
  const check = `run \`python -m turing.research.loop.verify ${commandPath}\``;
  switch (state) {
    case "verified":
      return QUALIFIER_TAIL.replace("<dir>", commandPath);
    case "stale":
      return `This is a fact about this pane's reading, not a finding about the run; ${check} for a check of the bytes on disk now.`;
    case "incomplete":
      return `Not an accusation: a run that stopped early looks exactly like this; ${check} for a current check.`;
    case "failed":
      return `This is the loop's own finding, quoted, not a guess by this pane; ${check} to reproduce it.`;
    case "unverified":
      return `Nothing is claimed here in either direction; ${check} to check it yourself.`;
  }
}

/** The badge's `title`: what this state claims, how it closes, when the loop
 * last looked, and the loop's own sentence when it left one. */
export function badgeTitleOf(
  state: BadgeState,
  verdict: RunVerdict | null | undefined,
  runDir: string,
  resultsRoot?: string | null,
): string {
  const commandPath = verifyCommandPathOf(runDir, resultsRoot);
  const parts = [`${clauseOf(state, verdict)} ${closerOf(state, commandPath)}`];
  // "As last checked by the loop" is a claim about a moment, and the file
  // records which one. Without it the operator cannot tell a check from ten
  // seconds ago from one from last week's run of the same directory.
  if (verdict?.checkedAtMs !== null && verdict?.checkedAtMs !== undefined) {
    parts.push(
      `as last checked by ${verdict.checkedBy ?? "the loop"} at ` +
        new Date(verdict.checkedAtMs).toLocaleString(),
    );
  }
  // Verbatim from `verify.format_run_verdict`, so the badge and the terminal
  // cannot tell an operator two different stories about one run. It is a
  // secondary line, and stays last: it names an absolute path on whatever host
  // ran the loop, which is not the path the tooltip's first line is about.
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
