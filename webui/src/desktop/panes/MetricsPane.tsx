// Live metrics pane: tails every `metrics.jsonl` under the `results` root,
// auto-follows the newest run, and lets the operator pin additional runs to
// overlay for comparison. (The `metrics.json` summaries that sit beside those
// step logs are deliberately NOT runs — see `isChartRunFile` in metrics.ts for
// why listing them made a completed round render as "no metrics yet".) Shows
// one series at a time via tabs (rather than every series stacked, which read
// as jumbled and clipped its own top tick label against the row above it —
// see Chart.tsx for the tick-clipping fix, and for why the chart's React keys
// must not be built from a display label). Watches `.viewer.json` too, so a
// coding agent can point the pane at a specific series/run set — see
// desktop/README.md's "Agent-driven viewing" section for the file format.
// The flywheel pane can point it too, via an in-app click event rather than
// the file (paneLink.ts) — the two share one request slot, so whichever
// spoke last wins, and this pane never writes `.viewer.json` back.
// The run-selection state machine (auto resolution, the 8-line cap, sticky
// empty on clear) lives in `runSelection.ts` and per-run color stickiness in
// `seriesColors.ts`, both pure and unit-tested; this file is the wiring.
//
// **The contract this pane keeps.** A run's chart is built only from bytes of
// ONE file generation, applied in order: `fs_tail` reports the chunk's start
// offset and the file's identity; the pane applies a chunk only if its start
// equals the offset it holds AND the identity matches — a changed identity or
// a shorter file resets the run to that chunk alone; a chunk with any other
// start is discarded, never appended. A trailing partial line is not consumed.
// Two qualifications: (a) where the platform reports no identity (`dev`/`ino`
// null — non-unix), identity-based reset is unavailable and detection is
// length-only, so a re-drive to a file at least as long as the held offset
// is caught only if the seam check below catches it; (b) an in-place rewrite
// of the SAME inode that ends longer than the pane's offset is caught only by
// the seam check — the first non-blank line of an appended chunk must parse
// as a JSON object, else the run is re-read from 0 — which is a heuristic,
// not identity: a rewrite whose lines end where the old ones did passes it.
// `verified` means the verdict's `chain_head` equals the `_chain` digest of the
// last non-blank line of the bytes that last arrived, and a last line it
// cannot parse — or an appended chunk holding only blank lines, which
// `integrity.py` calls a malformed record — makes the run `stale`. See
// `metrics.ts` for why the digest binding is load-bearing; `tailRun` below is
// where the byte side of this is kept, and the `fs-change` handler is where
// the verdict is re-read beside every change to the run.

import { Fragment, useEffect, useId, useMemo, useRef, useState } from "react";
import { inv, subscribe } from "../tauri";
import Chart from "./Chart";
import {
  badgeLabelOf,
  badgeRunTailOf,
  badgeStateOf,
  badgeTitleOf,
  chainDigestOfLastLine,
  etaOf,
  isChartRunFile,
  parseMetricsText,
  parseVerdictFile,
  parseViewerFile,
  pickSeries,
  runIdOf,
  appendSeries,
  verdictPathOf,
  type BadgeState,
  type Point,
  type RunVerdict,
  type ViewerFile,
} from "./metrics";
import {
  // aliased: the local memo below owns the bare name
  activePaths as activePathsOf,
  autoPath,
  canPick,
  clearAll,
  INITIAL_SELECTION,
  isHeldByAuto,
  lineCount,
  MAX_LINES,
  removePath,
  toggleAuto,
  togglePin,
  type RunSelection,
} from "./runSelection";
import {
  assignColors,
  colorForSlot,
  type ColorAssignment,
} from "./seriesColors";
import {
  resolveRunRequest,
  subscribeMetricsTarget,
  type RunRequest,
} from "./paneLink";

interface Entry {
  rel_path: string;
  is_dir: boolean;
  size: number;
  mtime_ms: number;
}

/** What `fsroots.rs::fs_tail` returns — see `TailChunk` there for the contract
 * behind each field. Every offset is a byte offset computed on the Rust side;
 * nothing here does arithmetic on `data`. */
interface TailChunk {
  /** Whole lines only: bytes `[start, offset)`, cut at the last `\n`. */
  data: string;
  /** Where the next read begins — just past the last `\n` this read consumed. */
  offset: number;
  /** Where this read actually began: the caller's offset, or 0 if `restarted`. */
  start: number;
  /** File identity (unix `dev`/`ino`); `null` where the platform has none. */
  dev: number | null;
  ino: number | null;
  /** The file was shorter than the caller's offset, so the read began at 0. */
  restarted: boolean;
}

interface RunState {
  path: string;
  label: string;
  /** The byte offset the next `fs_tail` for this run is issued at, and the
   * only offset a returned chunk may start at to be appended. */
  offset: number;
  /** `"<dev>:<ino>"` of the file the held bytes came from; `null` until the
   * first chunk lands, or where the platform reports no identity. */
  ident: string | null;
  points: Point[];
  series: Map<string, Array<[number, number]>>;
  arrivals: number[];
  /** The `_chain` digest of the last non-blank line this run holds, or
   * `null` — including when that line does not parse. This is what binds the
   * verdict badge to bytes rather than to a count — see
   * `chainDigestOfLastLine` in metrics.ts. */
  lastDigest: string | null;
  /** At most one `fs_tail` is outstanding per run. A request made while one
   * is in flight sets `pending`, and the in-flight one issues a single
   * follow-up read when it lands — the same bytes are never in two responses
   * that both get applied. */
  tailing: boolean;
  lastTailMs: number;
  pending: boolean;
}

function identOf(chunk: TailChunk): string | null {
  return chunk.dev === null || chunk.ino === null
    ? null
    : `${chunk.dev}:${chunk.ino}`;
}

/**
 * Whether the first non-blank line of a chunk is a JSON object — the seam
 * check for an append (see `tailOnce`). A chunk with no non-blank line at all
 * passes: there is no seam to judge, and its (blank) bytes are judged by the
 * digest rule instead.
 */
function firstLineIsObject(data: string): boolean {
  const first = data.split("\n").find((line) => line.trim() !== "");
  if (first === undefined) return true;
  try {
    const parsed: unknown = JSON.parse(first);
    return (
      typeof parsed === "object" && parsed !== null && !Array.isArray(parsed)
    );
  } catch {
    return false;
  }
}

/** Forget everything a run holds, so the next chunk is read from byte 0. */
function forgetRun(run: RunState) {
  run.offset = 0;
  run.ident = null;
  run.points = [];
  run.series.clear();
  run.arrivals = [];
  run.lastDigest = null;
}

const RESULTS_ROOT = "results";
const SERIES_STORAGE_KEY = "turing.metrics.series";
const VIEWER_FILE_REL = ".viewer.json";

// Colour *and* glyph per badge state (the glyph is in `badgeLabelOf`), for the
// same reason the flywheel pane's round statuses carry both: colour alone is
// not a status an operator can read reliably. `stale` and `incomplete` share
// an amber — neither is an accusation, both mean "this is not a verdict about
// what you are looking at" — and are told apart by their glyph and words.
const BADGE_CLASSES: Record<BadgeState, string> = {
  verified: "border-emerald-400/40 text-emerald-400",
  stale: "border-amber-400/40 text-amber-400",
  incomplete: "border-amber-400/40 text-amber-400",
  failed: "border-rose-400/40 text-rose-400",
  unverified: "border-term-edge text-term-dim",
};

/**
 * Whether two reads of one run's verdict say the same thing.
 *
 * `undefined` ("not read yet") is never equal to anything, including `null`:
 * they share a badge colour but not a sentence, so the first read has to land
 * even when it lands on nothing.
 */
function sameVerdict(
  a: RunVerdict | null | undefined,
  b: RunVerdict | null,
): boolean {
  if (a === undefined) return false;
  if (a === null || b === null) return a === b;
  return (
    a.state === b.state &&
    a.linesChecked === b.linesChecked &&
    a.checkedAtMs === b.checkedAtMs &&
    a.checkedBy === b.checkedBy &&
    a.chainHead === b.chainHead &&
    a.detail === b.detail
  );
}

interface RunPickerProps {
  runFiles: Entry[];
  selection: RunSelection;
  onToggleAuto: () => void;
  onTogglePin: (path: string) => void;
}

// Hand-rolled listbox standing in for `<select multiple size={4}>` — WKWebView
// draws its own OS bezel around the native control and frequently ignores
// author `option:checked` backgrounds, so (same reasoning as Select.tsx) this
// is built from toggleable rows CSS can actually reach. Each row is an
// independent toggle rather than a single commit-on-Enter cursor, so it
// doesn't reuse Select.tsx's single-select state machine.
function RunPicker({
  runFiles,
  selection,
  onToggleAuto,
  onTogglePin,
}: RunPickerProps) {
  const held = autoPath(selection, runFiles);
  const heldLabel = held ? runIdOf(held) : null;

  return (
    <ul
      role="listbox"
      aria-multiselectable="true"
      aria-label="runs"
      // Roughly the old size={4}/min-w-[160px] footprint so the pane header
      // doesn't reflow.
      className="max-h-[88px] min-w-[160px] overflow-auto border border-term-edge bg-term-panel"
    >
      <li role="presentation">
        <button
          type="button"
          role="option"
          aria-selected={selection.auto}
          onClick={onToggleAuto}
          title={heldLabel ?? undefined}
          className={`block w-full whitespace-nowrap px-2 py-0.5 text-left text-xs ${
            selection.auto ? "bg-term-raised text-term-accent" : "text-term-fg"
          }`}
        >
          {/* Auto resolves visibly: naming the run it currently points at is
              what keeps it from reading as a second, mystery selection. */}
          {heldLabel ? `auto → ${heldLabel}` : "auto (no runs)"}
        </button>
      </li>
      {runFiles.map((f) => {
        const path = f.rel_path;
        const label = runIdOf(path);
        const pinned = selection.pinned.includes(path);
        const dimmed = isHeldByAuto(selection, runFiles, path);
        const enabled = canPick(selection, runFiles, path);
        return (
          <li key={path} role="presentation">
            <button
              type="button"
              role="option"
              aria-selected={pinned}
              disabled={!enabled}
              onClick={() => onTogglePin(path)}
              // The label is a full run path and the box is ~160px wide, so
              // rows clip; the tooltip keeps a clipped row identifiable. The
              // auto-held row stays clickable even though it's dimmed —
              // clicking it promotes the run to an explicit pin, which is a
              // real action, not a disabled control.
              title={
                dimmed
                  ? `${label} — held by auto; click to pin it explicitly`
                  : label
              }
              className={`block w-full whitespace-nowrap px-2 py-0.5 text-left text-xs ${
                pinned
                  ? "bg-term-raised text-term-accent"
                  : dimmed
                    ? "text-term-dim italic"
                    : "text-term-fg"
              } ${enabled ? "" : "cursor-not-allowed opacity-40"}`}
            >
              {label}
            </button>
          </li>
        );
      })}
    </ul>
  );
}

export default function MetricsPane() {
  // Prefix for the badges' `aria-describedby` targets, so two panes mounted at
  // once cannot claim the same ids.
  const paneId = useId();
  const [runFiles, setRunFiles] = useState<Entry[]>([]);
  const [selection, setSelection] = useState<RunSelection>(INITIAL_SELECTION);
  const runsRef = useRef<Map<string, RunState>>(new Map());
  const [tick, setTick] = useState(0);
  const forceRender = (updater: (n: number) => number) => setTick(updater);
  const [storedSeries, setStoredSeriesState] = useState<string | null>(() =>
    localStorage.getItem(SERIES_STORAGE_KEY),
  );
  // series name → display title, from `.viewer.json`. The file is
  // authoritative: dropping the key drops the titles again.
  const [titles, setTitles] = useState<Record<string, string>>({});

  function selectSeries(name: string) {
    setStoredSeriesState(name);
    localStorage.setItem(SERIES_STORAGE_KEY, name);
  }

  // The listed run files, mirrored into a ref so the mount-once `fs-change`
  // handler can ask "have I listed this path?" without re-subscribing on
  // every list change.
  const runFilesRef = useRef<Entry[]>([]);
  // Debounce handle for watcher-triggered run-list refreshes: one burst of
  // create events (a round starting several attempts at once) is one walk.
  const runListTimer = useRef<number | null>(null);
  // Monotonic walk sequence (the reloadSeq pattern FlywheelPane uses): two
  // walks can be in flight at once — the mount walk plus a watcher-triggered
  // one, or two watcher bursts more than the debounce apart — and `fs_list`
  // gives no ordering guarantee. A walk answers with what the directory held
  // when it STARTED, so the earlier walk's snapshot resolving last would
  // overwrite the newer list and transiently drop the newest run file.
  const runListSeq = useRef(0);

  /** Walk the results root and replace the run list with what is there NOW.
   * Called at mount and again whenever the watcher reports a run file this
   * pane has never listed — `fs_list` runs once per refresh, not once per
   * pane lifetime, or a `metrics.jsonl` created after mount (a live round's
   * first solver step — the case the [metrics] link exists for) would never
   * enter `runFiles`, never be auto-followed, and never satisfy a pending
   * request. */
  async function refreshRunList() {
    const seq = ++runListSeq.current;
    const entries = await inv<Entry[]>("fs_list", {
      root: RESULTS_ROOT,
      rel: "",
      // `json` used to be listed too, purely so `metrics.json` could be
      // offered as a run; it never was one. Every other `.json` in the tree
      // (`round.json`, `trajectory.json`, `metrics.chain.json`) was listed
      // and discarded, so dropping the extension just stops walking them.
      exts: ["jsonl"],
    });
    // A newer walk started while this one was in flight: its snapshot, not
    // this one, describes the directory now. Applying this one anyway would
    // regress the list to a stale snapshot.
    if (runListSeq.current !== seq) return;
    // `fs_list` is newest-mtime-first and `runFiles[0]` is what auto-follow
    // charts, so this filter is also what guarantees auto-follow lands on a
    // file that can have points at all — a rotated `prior-N/metrics.jsonl`
    // is still a real chain and stays listed, just never emitted into
    // `.viewer.json`'s `runs`.
    const files = entries.filter((e) => isChartRunFile(e.rel_path));
    runFilesRef.current = files;
    setRunFiles(files);
  }

  function scheduleRunListRefresh() {
    if (runListTimer.current !== null) return;
    runListTimer.current = window.setTimeout(() => {
      runListTimer.current = null;
      void refreshRunList();
    }, 50);
  }

  useEffect(() => {
    let cancelled = false;
    async function load() {
      await inv("fs_watch", { root: RESULTS_ROOT, rel: "" });
      if (!cancelled) await refreshRunList();
    }
    void load();
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  /**
   * Read whatever the run file has past what this pane holds, and apply it.
   *
   * Serialized per run: at most one `fs_tail` is outstanding for a path, and a
   * request that arrives while one is in flight coalesces into exactly one
   * follow-up read once it lands. Without this, the ordinary end of an
   * attempt — the last append's event and the verdict's event a few
   * milliseconds apart — issued two reads at the same offset, both came back
   * with the same bytes, and the second was applied on top of the first: seen
   * from the offset the first had just advanced to, it looked like a restart,
   * wiped the points down to that one chunk, and pinned the badge `stale`.
   */
  async function tailRun(path: string) {
    let run = runsRef.current.get(path);
    if (!run) {
      run = {
        path,
        label: runIdOf(path),
        offset: 0,
        ident: null,
        points: [],
        series: new Map(),
        arrivals: [],
        lastDigest: null,
        tailing: false,
        lastTailMs: 0,
        pending: false,
      };
      runsRef.current.set(path, run);
    }
    if (run.tailing) {
      run.pending = true;
      return;
    }
    run.tailing = true;
    try {
      do {
        run.pending = false;
        await tailOnce(run);
      } while (run.pending);
    } finally {
      run.tailing = false;
      run.lastTailMs = Date.now();
    }
  }

  /**
   * One `fs_tail` round trip and the rule for applying its chunk.
   *
   * The rule, exactly (`chunk` is `fsroots.rs`'s `TailChunk`; `run.offset`
   * and `run.ident` are what the pane holds):
   *
   *   generation changed  := run.ident !== null && chunk ident !== null
   *                          && they differ
   *   RESET   if chunk.start === 0 && run.offset > 0
   *              && (generation changed || chunk.restarted)
   *           → the run becomes this chunk alone.
   *   APPEND  else if !generation changed && chunk.start === run.offset
   *           → the chunk is the run's next bytes — subject to the SEAM
   *             CHECK when run.offset > 0: the chunk's first non-blank line
   *             must parse as a JSON object (the held offset sits just after
   *             a `\n`, so a whole line begins there). If it does not, the
   *             same inode was rewritten in place to something longer than
   *             what the pane held, and the chunk is treated as RE-READ.
   *   RE-READ else if generation changed
   *           → nothing in this chunk is ours (it was read from an offset
   *             that only meant something in the old file, and a chunk
   *             starting at 0 with nothing held is an append anyway):
   *             forget the run and read the new file from byte 0.
   *   DISCARD otherwise
   *           → a duplicate or stale response; not applied, not appended.
   *
   * Why identity and not only `restarted`: a re-drive moves the trio and the
   * verdict together into `prior-N/` and the loop opens a fresh `metrics.jsonl`
   * — a new inode. If the new file is at least as long as the offset the pane
   * held (same-length first line; a create and two appends coalesced under
   * the watcher's debounce), a length check sees nothing and the old points
   * get the new lines stapled on, with a `verified` badge over the seam. A
   * truncate in place (same inode, shorter) is the case `restarted` covers.
   *
   * Two limits, stated plainly. Where the platform reports no identity
   * (`dev`/`ino` null, non-unix), the generation check is unavailable and
   * only lengths (`restarted`) and the seam check remain. And a rewrite of
   * the SAME inode that ends longer than the pane's offset is caught only by
   * the seam check — a heuristic that sees a mid-line landing, not a rewrite
   * whose lines happen to end where the old ones did.
   */
  async function tailOnce(run: RunState) {
    let chunk: TailChunk;
    try {
      chunk = await inv<TailChunk>("fs_tail", {
        root: RESULTS_ROOT,
        rel: run.path,
        offset: run.offset,
      });
    } catch {
      // The run file is gone — a re-drive rotated it into `prior-N/` between
      // the event and this read, or the directory was removed outright. Same
      // reasoning as `loadVerdict`'s catch: an absent file is a state, not an
      // error, and letting it reject here would surface as an unhandled
      // rejection rather than as anything the operator can see. The pane keeps
      // what it has already parsed; the verdict read running beside this one
      // is what turns the badge honest.
      return;
    }
    const ident = identOf(chunk);
    const generationChanged =
      run.ident !== null && ident !== null && ident !== run.ident;
    const reset =
      chunk.start === 0 &&
      run.offset > 0 &&
      (generationChanged || chunk.restarted);
    const append = !reset && !generationChanged && chunk.start === run.offset;
    if (!reset && !append) {
      if (generationChanged) {
        // A different file, read from an offset that only meant something in
        // the old one. Start over from its first byte; the loop in `tailRun`
        // issues that read.
        forgetRun(run);
        run.pending = true;
        forceRender((n) => n + 1);
      }
      return;
    }
    if (ident !== null) run.ident = ident;
    // Nothing new past what the pane holds: same offset, same file. This is
    // the common answer to a verdict-event re-tail and must not re-render.
    if (append && chunk.offset === run.offset) return;
    // SEAM CHECK, on an append that began mid-file: the held offset sits just
    // after a `\n` of the file it was read from, so the chunk's first
    // non-blank line must be a whole line — a JSON object. If it is not, the
    // offset landed inside a line of some other content: the same inode was
    // rewritten in place and ended longer than what the pane held, which no
    // identity or length check can see. Treat it as a generation change.
    // (Only a heuristic — a rewrite whose lines happen to end where the old
    // ones did is invisible here; see the contract comment above.)
    if (append && run.offset > 0 && !firstLineIsObject(chunk.data)) {
      forgetRun(run);
      run.pending = true;
      forceRender((n) => n + 1);
      return;
    }
    run.offset = chunk.offset;
    const newPoints = parseMetricsText(chunk.data);
    if (reset) {
      run.series.clear();
      appendSeries(run.series, newPoints, 0);
      run.points = newPoints;
      run.arrivals = newPoints.length > 0 ? [Date.now()] : [];
    } else if (newPoints.length > 0) {
      appendSeries(run.series, newPoints, run.points.length);
      for (const point of newPoints) run.points.push(point);
      run.arrivals.push(Date.now());
    }
    // The digest of the last non-blank line of the bytes that just arrived —
    // what the badge is bound to. `fs_tail` returns whole lines only, so if
    // the chunk has a non-blank line at all, its last one IS the run's last
    // line, and its digest (or `null`, if it will not parse — which reads
    // `stale`) replaces the previous one. A non-empty chunk of only blank
    // lines ALSO clears the digest: the writer never emits an interior blank
    // line, `integrity.py` treats one as a malformed record, and the pane must
    // not keep saying chain ok over bytes the loop's own check would fail.
    run.lastDigest = chainDigestOfLastLine(chunk.data);
    forceRender((n) => n + 1);
  }

  // What `verify` said about each run, as recorded by the loop itself in
  // `metrics.verdict.json` (`results.write_attempt_verdict`). Keyed by run
  // file path. `undefined` means "not read yet", `null` means "read and there
  // is nothing there" — both render as `unverified`, which is the honest
  // reading of "this pane has no evidence either way", but they do not read
  // as the same sentence (see `clauseOf` in metrics.ts: only one of them is a
  // fact about the run).
  const [verdicts, setVerdicts] = useState<Record<string, RunVerdict | null>>(
    {},
  );

  async function loadVerdict(path: string) {
    let parsed: RunVerdict | null = null;
    try {
      const text = await inv<string>("fs_read_text", {
        root: RESULTS_ROOT,
        rel: verdictPathOf(path),
      });
      parsed = parseVerdictFile(text);
    } catch {
      // No verdict file — the ordinary state of an attempt still running, and
      // of every run written before this file existed. Not an error.
      parsed = null;
    }
    // Every change to a run file now re-reads its verdict too, so this runs on
    // every append of a live run whose directory has no verdict in it. Keeping
    // the same object when nothing changed keeps that from being a re-render
    // per solver step for a value that reads `null` either way.
    setVerdicts((prev) =>
      sameVerdict(prev[path], parsed) ? prev : { ...prev, [path]: parsed },
    );
  }

  const activePaths = useMemo(
    () => activePathsOf(selection, runFiles),
    [selection, runFiles],
  );

  // Sticky per-run colors. `assignColors` is idempotent for a given key set —
  // re-running it on its own output returns that output — so threading the
  // previous assignment through a ref is safe under StrictMode's double
  // render, and a run keeps its color across metric-tab switches and across
  // other runs being added or removed.
  const colorsRef = useRef<ColorAssignment>(new Map());
  const colors = useMemo(() => {
    const next = assignColors(colorsRef.current, activePaths);
    colorsRef.current = next;
    return next;
  }, [activePaths]);

  useEffect(() => {
    for (const path of activePaths) {
      void tailRun(path);
      void loadVerdict(path);
    }
    // FSEvents can withhold updates for a long-lived open writer, and remote
    // filesystems may miss notifications. Reconcile only selected runs after
    // 50 ms without a completed tail (checked every 50 ms); fast event-driven streams add no polls.
    // Reads remain serialized, and unchanged tails do not trigger a render.
    if (activePaths.length === 0) return;
    const timer = window.setInterval(() => {
      for (const path of activePaths) {
        const run = runsRef.current.get(path);
        if (run && !run.tailing && Date.now() - run.lastTailMs >= 50) {
          void tailRun(path);
          void loadVerdict(path);
        }
      }
    }, 50);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activePaths.join("|")]);

  const seriesNames = useMemo(() => {
    const names = new Set<string>();
    for (const path of activePaths) {
      const run = runsRef.current.get(path);
      if (!run) continue;
      for (const name of run.series.keys()) names.add(name);
    }
    return Array.from(names).sort();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activePaths, tick]);

  // The last `.viewer.json` delivery, held as state rather than applied on the
  // spot. `.viewer.json` is read at mount, but the run list and the series
  // names it refers to only exist after `fs_list` and the first tail have
  // come back — and the read reliably WINS that race (confirmed in a real
  // session: the viewer file was read several hundred milliseconds before
  // `fs_list` returned). Applying it at read time therefore dropped every
  // reference it made against state that was still empty, which is the other
  // half of why `runs` looked inert. Holding the request and honouring it when
  // what it names shows up is what makes the file actually control the pane.
  const [viewerRequest, setViewerRequest] = useState<ViewerFile | null>(null);
  // The single pending run request — from `.viewer.json` OR from a flywheel
  // round click (see paneLink.ts). ONE slot on purpose: whichever source set
  // it last is the one that gets honoured, so "last action wins" is the shape
  // of the state rather than a comparison of timestamps. An unhonoured older
  // request that gets replaced is thereby superseded and can never fire late.
  const [runRequest, setRunRequest] = useState<RunRequest | null>(null);
  // Which request each half has already been honoured for, by object identity
  // (every delivery — viewer read or click — is a fresh object). "Once per
  // delivery" rather than "whenever the deps change": a selection re-applied
  // every time a run discovers a new key would silently undo the operator's
  // own click in the listbox.
  const runsHonoredFor = useRef<RunRequest | null>(null);
  const seriesHonoredFor = useRef<ViewerFile | null>(null);
  // Monotonic stamp over every action that speaks for the run slot: a click,
  // a listbox selection, and the START of each `.viewer.json` read. The read
  // captures the stamp before its await and writes the slot only if nothing
  // newer happened meanwhile — without this, a click landing during an
  // in-flight read (RoundRunner rewrites `.viewer.json` at every round
  // boundary, exactly when operators click) was evicted when the stale read
  // resolved, violating "last action wins" through mere latency.
  const deliverySeq = useRef(0);

  useEffect(() => {
    if (!runRequest || runsHonoredFor.current === runRequest) return;
    const matched = resolveRunRequest(
      runRequest,
      runFiles.map((f) => f.rel_path),
    );
    // A total miss is "nothing on this machine satisfies it (yet)" — a round
    // clicked before its first attempt has written a line, or a `.viewer.json`
    // naming runs that are not here. Not "show nothing": leave the operator's
    // current selection alone and stay unhonoured, so a later run list can
    // still satisfy it (and a newer request can still replace it).
    if (matched.length === 0) return;
    runsHonoredFor.current = runRequest;
    // A manual `clear` is sticky against the FILE (runSelection.ts's
    // `cleared`) — `.viewer.json` is rewritten at every round boundary and
    // must not undo an operator's empty chart — but a flywheel click is the
    // operator acting, so it always lands. `matched` is capped the same way
    // `applyViewerRuns` caps a label list: never more lines than colors.
    setSelection((sel) => {
      if (sel.cleared && runRequest.source === "viewer") return sel;
      return {
        auto: false,
        pinned: matched.slice(0, MAX_LINES),
        cleared: false,
      };
    });
  }, [runRequest, runFiles]);

  // Flywheel → metrics: a `[metrics]` click in an expanded round lands here as
  // the pane's next run request, replacing whatever `.viewer.json` last asked
  // for — and being replaced in turn by the file's next change. Only mounted
  // panes hear it; the subscription lives exactly as long as the pane.
  useEffect(() => {
    return subscribeMetricsTarget((target) => {
      // A click is delivered synchronously, so it claims the stamp and the
      // slot in one step — any older read still in flight is now stale.
      deliverySeq.current += 1;
      setRunRequest({ source: "flywheel", target });
    });
  }, []);

  /** The operator's own listbox action. It is itself a "last action": it
   * clears the pending slot, so a round clicked earlier — whose files land
   * later — cannot fire then and rearrange a chart the operator has since
   * chosen by hand; and it bumps the delivery stamp, so an in-flight
   * `.viewer.json` read cannot resolve over it either. */
  function operatorSelect(update: (sel: RunSelection) => RunSelection) {
    deliverySeq.current += 1;
    setRunRequest(null);
    setSelection(update);
  }

  useEffect(() => {
    const series = viewerRequest?.series;
    if (!series || seriesHonoredFor.current === viewerRequest) return;
    if (!seriesNames.includes(series)) return;
    seriesHonoredFor.current = viewerRequest;
    selectSeries(series);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [viewerRequest, seriesNames]);

  async function loadViewerFile() {
    // Claimed BEFORE the await: the delivery is the file change that
    // triggered this read, not the read resolving. A click (or a newer read)
    // that lands while `fs_read_text` is in flight bumps the stamp past this
    // one, and the stale resolution below must not touch the run slot.
    const seq = ++deliverySeq.current;
    try {
      const text = await inv<string>("fs_read_text", {
        root: RESULTS_ROOT,
        rel: VIEWER_FILE_REL,
      });
      const parsed = parseViewerFile(text);
      if (!parsed) return;
      // Titles need nothing else to exist, so they apply immediately.
      setTitles(parsed.titles ?? {});
      setViewerRequest(parsed);
      // The runs half competes with flywheel clicks for the one request slot
      // — this delivery supersedes any click before it, and the next click
      // supersedes this delivery. A file without a usable `runs` key asks
      // nothing about runs and must not evict a click that did — and a read
      // that has been superseded mid-flight must not evict anything at all.
      if (
        parsed.runs &&
        parsed.runs.length > 0 &&
        seq === deliverySeq.current
      ) {
        setRunRequest({ source: "viewer", runs: parsed.runs });
      }
    } catch {
      // No viewer file (or unreadable) — nothing to apply, not an error.
    }
  }

  useEffect(() => {
    void loadViewerFile();
    let cancelled = false;
    const sub = subscribe<{ root: string; rel_path: string }>(
      "fs-change",
      (payload) => {
        if (cancelled || payload.root !== RESULTS_ROOT) return;
        if (payload.rel_path === VIEWER_FILE_REL) {
          void loadViewerFile();
          return;
        }
        // Either half of a run's pair of files changing re-reads BOTH. The badge
        // is a claim about a verdict and a log *together*, and each file can move
        // without producing an event of its own for the other:
        //
        //   * The verdict can be rotated away with no event at all. A re-drive
        //     moves the metrics trio and the verdict together into `prior-N/`;
        //     `fsroots.rs` skips any path that is not a file by the time the
        //     handler runs, and the verdict's old path is precisely that — so
        //     the only event a re-drive produces for this directory is one for
        //     the new `metrics.jsonl`. Re-reading the verdict on the run's own
        //     event is what stops the badge sitting green over a directory the
        //     verdict has left.
        //   * The verdict event can arrive before the final metrics read
        //     resolves. Re-tail both to bind the badge to the latest bytes.
        if (runsRef.current.has(payload.rel_path)) {
          void tailRun(payload.rel_path);
          void loadVerdict(payload.rel_path);
          return;
        }
        // A run file this pane has never listed: it was created after the
        // mount-time walk — a live round's first solver step, exactly the run
        // an operator clicks [metrics] on. Without a re-list it would never
        // enter `runFiles`: auto-follow would sit on an older run and a
        // pending request naming it would stay unhonoured forever.
        if (
          isChartRunFile(payload.rel_path) &&
          !runFilesRef.current.some((e) => e.rel_path === payload.rel_path)
        ) {
          scheduleRunListRefresh();
          return;
        }
        // The loop writes `metrics.verdict.json` once the attempt ends, i.e.
        // while this pane is already open on the run — so the badge has to
        // arrive on a watcher event, not only on mount.
        for (const runPath of runsRef.current.keys()) {
          if (payload.rel_path === verdictPathOf(runPath)) {
            void loadVerdict(runPath);
            void tailRun(runPath);
            return;
          }
        }
      },
    );
    return () => {
      cancelled = true;
      sub.unsubscribe();
      if (runListTimer.current !== null) {
        window.clearTimeout(runListTimer.current);
        runListTimer.current = null;
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // The runs actually on the chart, in the order the chart draws them. The ETA
  // strip is derived from this list rather than from `runFiles[0]` (the newest
  // *file*, which is not necessarily selected and — while summaries counted as
  // runs — was never a run with any points, so the strip read
  // `steps/s: — eta: — last step: —` permanently). A readout that describes
  // something other than what is plotted is worse than no readout.
  const activeRuns = useMemo(
    () =>
      activePaths
        .map((path) => runsRef.current.get(path))
        .filter((r): r is RunState => r !== undefined),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [activePaths, tick],
  );

  // With several runs pinned there is no single honest ETA — they finish at
  // different times — so rather than average them into a number describing no
  // run at all, the strip describes exactly one: the first one charted. It is
  // rendered with that run's name attached, because an unlabelled ETA beside a
  // multi-run overlay is itself a small lie about which run it belongs to.
  const etaRun = activeRuns[0];
  const eta = etaRun ? etaOf(etaRun.points, etaRun.arrivals) : null;

  const activeSeries = useMemo(
    () => pickSeries(seriesNames, storedSeries),
    [seriesNames, storedSeries],
  );

  const chartSeries = useMemo(() => {
    if (!activeSeries) return [];
    return activeRuns.map((run) => ({
      // Identity for React's key, kept separate from the display label: the
      // run's file path is unique per run by construction (it is the
      // `runsRef` map key) and the series name is fixed across the list, so
      // no two entries can collide however the labels read. See Chart.tsx.
      id: `${run.path}::${activeSeries}`,
      label: `${run.label}/${titles[activeSeries] ?? activeSeries}`,
      points: run.series.get(activeSeries) ?? [],
      color: colorForSlot(colors.get(run.path) ?? 0),
      // The legend `×` always means "this line goes away" — including for
      // auto's line, which it switches auto off to remove.
      onRemove: () =>
        operatorSelect((sel) => removePath(sel, runFilesRef.current, run.path)),
    }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeRuns, activeSeries, titles, colors]);

  const lines = lineCount(selection, runFiles);

  return (
    <div className="flex h-full flex-col text-xs">
      <div className="flex items-center gap-2 border-b border-term-edge p-2">
        <span className="text-[10px] uppercase tracking-wider text-term-dim">
          runs ({lines}/{MAX_LINES})
        </span>
        <RunPicker
          runFiles={runFiles}
          selection={selection}
          onToggleAuto={() =>
            operatorSelect((sel) => toggleAuto(sel, runFiles))
          }
          onTogglePin={(path) =>
            operatorSelect((sel) => togglePin(sel, runFiles, path))
          }
        />
        <button
          type="button"
          onClick={() => operatorSelect(clearAll)}
          disabled={lines === 0}
          className="shrink-0 border border-term-edge px-2 py-0.5 text-[11px] lowercase text-term-dim hover:text-term-accent disabled:opacity-40 disabled:hover:text-term-dim"
        >
          clear
        </button>
        {/* One badge per charted run, each naming its own run in the tooltip.
            A single badge over a multi-run overlay would attribute one run's
            verdict to another — the same lie the ETA strip's run label exists
            to prevent. See metrics.ts for what these states mean and for why
            the good one reads "chain ok" rather than anything stronger. */}
        {activeRuns.length > 0 && (
          // Capped and scrollable, matching the run listbox beside it: there is
          // one chip per charted run and `.viewer.json` can name any number of
          // them, so an uncapped column grows the header until it pushes the
          // chart it annotates off the bottom of the pane.
          <div className="flex max-h-[88px] shrink-0 flex-col gap-px overflow-auto">
            {activeRuns.map((run) => {
              const verdict = verdicts[run.path];
              const state = badgeStateOf(
                verdict,
                run.points.length,
                run.lastDigest,
              );
              const title = badgeTitleOf(
                state,
                verdict,
                run.label,
                RESULTS_ROOT,
              );
              const describedById = `${paneId}-verdict-${run.path}`;
              return (
                // Fragment rather than a wrapper element: the chips are the
                // column's own children, so the column is what scrolls.
                <Fragment key={run.path}>
                  {/* A `<button>` rather than a `<span title=…>`. The title is
                      reachable by mouse and by nothing else, and the qualifier
                      — the sentence that keeps a green chip from reading as
                      "these numbers are real" — is the part of this badge that
                      must not be mouse-only. The button does not act: it is
                      focusable so the description can be announced, which is
                      the whole job. */}
                  <button
                    type="button"
                    data-testid="metrics-verdict"
                    data-run={run.label}
                    data-state={state}
                    title={`${run.label}\n${title}`}
                    aria-describedby={describedById}
                    className={`whitespace-nowrap border px-1 text-left text-[10px] ${BADGE_CLASSES[state]}`}
                  >
                    {/* The run's distinguishing tail, in the chip's own visible
                        text. Five identically-worded chips told apart only by
                        an attribute are unreadable in exactly the case this
                        badge exists for: one red chip in a stack, and no way to
                        say which run failed without a mouse. */}
                    {badgeLabelOf(state)}{" "}
                    <span className="opacity-70">
                      {badgeRunTailOf(run.label)}
                    </span>
                  </button>
                  <span id={describedById} className="sr-only">
                    {title}
                  </span>
                </Fragment>
              );
            })}
          </div>
        )}
        {eta && etaRun && (
          <div
            data-testid="metrics-eta"
            className="ml-2 truncate font-mono text-term-dim"
          >
            <span title={etaRun.label}>{etaRun.label}</span>
            <span className="ml-3">
              steps/s: {eta.stepsPerSec?.toFixed(2) ?? "—"}
            </span>
            <span className="ml-3">
              eta: {eta.etaSec !== null ? `${Math.round(eta.etaSec)}s` : "—"}
            </span>
            <span className="ml-3">last step: {eta.lastStep ?? "—"}</span>
          </div>
        )}
      </div>
      <div className="flex shrink-0 items-center gap-3 overflow-x-auto border-b border-term-edge px-2">
        {seriesNames.length === 0 && (
          <span className="py-1 text-[11px] text-term-dim">no metrics yet</span>
        )}
        {seriesNames.map((name) => (
          <button
            key={name}
            type="button"
            onClick={() => selectSeries(name)}
            className={`whitespace-nowrap border-b-2 py-1 text-[11px] lowercase ${
              name === activeSeries
                ? "border-term-accent text-term-accent"
                : "border-transparent text-term-dim hover:text-term-fg"
            }`}
          >
            {name}
          </button>
        ))}
      </div>
      <div className="min-h-0 flex-1 p-2">
        {activeSeries && chartSeries.length > 0 ? (
          <Chart series={chartSeries} />
        ) : (
          <div className="p-4 text-term-dim">
            {lines === 0 ? "no runs selected" : "no metrics yet"}
          </div>
        )}
      </div>
    </div>
  );
}
