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

import { useEffect, useMemo, useRef, useState } from "react";
import { inv, subscribe } from "../tauri";
import Chart from "./Chart";
import {
  badgeLabelOf,
  badgeStateOf,
  badgeTitleOf,
  etaOf,
  isChartRunFile,
  matchesViewerRuns,
  parseMetricsText,
  parseVerdictFile,
  parseViewerFile,
  pickSeries,
  runIdOf,
  seriesOf,
  verdictPathOf,
  type BadgeState,
  type Point,
  type RunVerdict,
  type ViewerFile,
} from "./metrics";

interface Entry {
  rel_path: string;
  is_dir: boolean;
  size: number;
  mtime_ms: number;
}

interface RunState {
  path: string;
  label: string;
  offset: number;
  points: Point[];
  arrivals: number[];
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

interface RunMultiSelectProps {
  runFiles: Entry[];
  selected: string[];
  onChange: (selected: string[]) => void;
}

// Custom multi-select listbox standing in for `<select multiple size={4}>` —
// same visual vocabulary as Select.tsx (bordered term-panel box, term-raised/
// term-accent highlighted rows), same `string[]` state and "auto" sentinel.
// Each row is an independent toggle rather than a single commit-on-Enter
// cursor, so this doesn't reuse Select.tsx's single-select state machine.
function RunMultiSelect({ runFiles, selected, onChange }: RunMultiSelectProps) {
  const options = [
    { value: "auto", label: "auto (newest)" },
    ...runFiles.map((f) => ({ value: f.rel_path, label: runIdOf(f.rel_path) })),
  ];

  function toggle(value: string) {
    if (selected.includes(value)) {
      onChange(selected.filter((v) => v !== value));
    } else {
      onChange([...selected, value]);
    }
  }

  return (
    <ul
      role="listbox"
      aria-multiselectable="true"
      aria-label="runs"
      // Roughly the old size={4}/min-w-[160px] footprint so the pane header
      // doesn't reflow.
      className="max-h-[88px] min-w-[160px] overflow-auto border border-term-edge bg-term-panel"
    >
      {options.map((opt) => {
        const isSelected = selected.includes(opt.value);
        return (
          <li key={opt.value} role="presentation">
            <button
              type="button"
              role="option"
              aria-selected={isSelected}
              onClick={() => toggle(opt.value)}
              // The label is a full run path and the box is ~160px wide, so
              // rows clip. A tooltip means a clipped row is still
              // identifiable without horizontal scrolling.
              title={opt.label}
              className={`block w-full whitespace-nowrap px-2 py-0.5 text-left text-xs ${
                isSelected ? "bg-term-raised text-term-accent" : "text-term-fg"
              }`}
            >
              {opt.label}
            </button>
          </li>
        );
      })}
    </ul>
  );
}

export default function MetricsPane() {
  const [runFiles, setRunFiles] = useState<Entry[]>([]);
  const [selected, setSelected] = useState<string[]>(["auto"]);
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

  useEffect(() => {
    let cancelled = false;
    async function load() {
      await inv("fs_watch", { root: RESULTS_ROOT, rel: "" });
      const entries = await inv<Entry[]>("fs_list", {
        root: RESULTS_ROOT,
        rel: "",
        // `json` used to be listed too, purely so `metrics.json` could be
        // offered as a run; it never was one. Every other `.json` in the tree
        // (`round.json`, `trajectory.json`, `metrics.chain.json`) was listed
        // and discarded, so dropping the extension just stops walking them.
        exts: ["jsonl"],
      });
      if (cancelled) return;
      // `fs_list` is newest-mtime-first and `runFiles[0]` is what auto-follow
      // charts, so this filter is also what guarantees auto-follow lands on a
      // file that can have points at all — a rotated `prior-N/metrics.jsonl`
      // is still a real chain and stays listed, just never emitted into
      // `.viewer.json`'s `runs`.
      setRunFiles(entries.filter((e) => isChartRunFile(e.rel_path)));
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, []);

  async function tailRun(path: string) {
    let run = runsRef.current.get(path);
    if (!run) {
      run = { path, label: runIdOf(path), offset: 0, points: [], arrivals: [] };
      runsRef.current.set(path, run);
    }
    const chunk = await inv<{ data: string; offset: number }>("fs_tail", {
      root: RESULTS_ROOT,
      rel: path,
      offset: run.offset,
    });
    if (chunk.offset === run.offset) return;
    run.offset = chunk.offset;
    const newPoints = parseMetricsText(chunk.data);
    if (newPoints.length > 0) {
      run.points = [...run.points, ...newPoints];
      run.arrivals = [...run.arrivals, Date.now()];
    }
    forceRender((n) => n + 1);
  }

  // What `verify` said about each run, as recorded by the loop itself in
  // `metrics.verdict.json` (`results.write_attempt_verdict`). Keyed by run
  // file path. `undefined` means "not read yet", `null` means "read and there
  // is nothing there" — both render as `unverified`, which is the honest
  // reading of "this pane has no evidence either way".
  const [verdicts, setVerdicts] = useState<Record<string, RunVerdict | null>>({});

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
    setVerdicts((prev) => ({ ...prev, [path]: parsed }));
  }

  const activePaths = useMemo(() => {
    const newest = runFiles[0]?.rel_path;
    const pinned = selected.filter((s) => s !== "auto");
    const paths = new Set(pinned);
    if (selected.includes("auto") && newest) paths.add(newest);
    return Array.from(paths);
  }, [runFiles, selected]);

  useEffect(() => {
    for (const path of activePaths) {
      void tailRun(path);
      void loadVerdict(path);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activePaths.join("|")]);

  const seriesNames = useMemo(() => {
    const names = new Set<string>();
    for (const path of activePaths) {
      const run = runsRef.current.get(path);
      if (!run) continue;
      for (const name of seriesOf(run.points).keys()) names.add(name);
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
  // Which request each half has already been honoured for, by object identity
  // (`parseViewerFile` returns a fresh object per read). "Once per delivery"
  // rather than "whenever the deps change": a series re-applied every time a
  // run discovers a new key would silently undo the operator's own tab click.
  const runsHonoredFor = useRef<ViewerFile | null>(null);
  const seriesHonoredFor = useRef<ViewerFile | null>(null);

  useEffect(() => {
    const runs = viewerRequest?.runs;
    if (!runs || runs.length === 0 || runsHonoredFor.current === viewerRequest) return;
    const matched = runFiles.filter((f) => matchesViewerRuns(f.rel_path, runs)).map((f) => f.rel_path);
    // A total miss is "the runs this file names aren't on this machine (yet)",
    // not "show nothing" — leave the operator's current selection alone and
    // stay unhonoured so a later run list can still satisfy it.
    if (matched.length === 0) return;
    runsHonoredFor.current = viewerRequest;
    setSelected(matched);
  }, [viewerRequest, runFiles]);

  useEffect(() => {
    const series = viewerRequest?.series;
    if (!series || seriesHonoredFor.current === viewerRequest) return;
    if (!seriesNames.includes(series)) return;
    seriesHonoredFor.current = viewerRequest;
    selectSeries(series);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [viewerRequest, seriesNames]);

  async function loadViewerFile() {
    try {
      const text = await inv<string>("fs_read_text", { root: RESULTS_ROOT, rel: VIEWER_FILE_REL });
      const parsed = parseViewerFile(text);
      if (!parsed) return;
      // Titles need nothing else to exist, so they apply immediately.
      setTitles(parsed.titles ?? {});
      setViewerRequest(parsed);
    } catch {
      // No viewer file (or unreadable) — nothing to apply, not an error.
    }
  }

  useEffect(() => {
    void loadViewerFile();
    let cancelled = false;
    const sub = subscribe<{ root: string; rel_path: string }>("fs-change", (payload) => {
      if (cancelled || payload.root !== RESULTS_ROOT) return;
      if (payload.rel_path === VIEWER_FILE_REL) {
        void loadViewerFile();
        return;
      }
      if (runsRef.current.has(payload.rel_path)) {
        void tailRun(payload.rel_path);
        return;
      }
      // The loop writes `metrics.verdict.json` once the attempt ends, i.e.
      // while this pane is already open on the run — so the badge has to
      // arrive on a watcher event, not only on mount.
      for (const runPath of runsRef.current.keys()) {
        if (payload.rel_path === verdictPathOf(runPath)) {
          void loadVerdict(runPath);
          return;
        }
      }
    });
    return () => {
      cancelled = true;
      sub.unsubscribe();
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
      points: seriesOf(run.points).get(activeSeries) ?? [],
    }));
  }, [activeRuns, activeSeries, titles]);

  return (
    <div className="flex h-full flex-col text-xs">
      <div className="flex items-center gap-2 border-b border-term-edge p-2">
        <span className="text-[10px] uppercase tracking-wider text-term-dim">runs</span>
        {/* WKWebView draws its own OS bezel around `<select multiple>` and
            frequently ignores author `option:checked` backgrounds, so — same
            reasoning as Select.tsx — this is a hand-rolled multi-select
            listbox instead of a native control CSS can't fully reach. Same
            `string[]` contract and "auto" sentinel as before, just built from
            toggleable rows. */}
        <RunMultiSelect runFiles={runFiles} selected={selected} onChange={setSelected} />
        {/* One badge per charted run, each naming its own run in the tooltip.
            A single badge over a multi-run overlay would attribute one run's
            verdict to another — the same lie the ETA strip's run label exists
            to prevent. See metrics.ts for what these states mean and for why
            the good one reads "chain ok" rather than anything stronger. */}
        {activeRuns.length > 0 && (
          <div className="flex shrink-0 flex-col gap-px">
            {activeRuns.map((run) => {
              const verdict = verdicts[run.path];
              const state = badgeStateOf(verdict, run.points.length);
              return (
                <span
                  key={run.path}
                  data-testid="metrics-verdict"
                  data-run={run.label}
                  data-state={state}
                  title={`${run.label}\n${badgeTitleOf(state, verdict, run.label)}`}
                  className={`whitespace-nowrap border px-1 text-[10px] ${BADGE_CLASSES[state]}`}
                >
                  {badgeLabelOf(state)}
                </span>
              );
            })}
          </div>
        )}
        {eta && etaRun && (
          <div data-testid="metrics-eta" className="ml-2 truncate font-mono text-term-dim">
            <span title={etaRun.label}>{etaRun.label}</span>
            <span className="ml-3">steps/s: {eta.stepsPerSec?.toFixed(2) ?? "—"}</span>
            <span className="ml-3">eta: {eta.etaSec !== null ? `${Math.round(eta.etaSec)}s` : "—"}</span>
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
        {activeSeries ? (
          <Chart series={chartSeries} />
        ) : (
          <div className="p-4 text-term-dim">no metrics yet</div>
        )}
      </div>
    </div>
  );
}
