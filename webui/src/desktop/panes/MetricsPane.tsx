// Live metrics pane: tails every `metrics.jsonl`/`metrics.json` under the
// `results` root, follows the newest run, and lets the operator pin additional
// runs to compare against it. Shows one series at a time via tabs (rather than
// every series stacked, which read as jumbled and clipped its own top tick
// label against the row above it — see Chart.tsx for the tick-clipping fix),
// with one line per selected run.
//
// The selection state machine (auto resolution, the 8-line cap, sticky-empty
// clearing) lives in `runSelection.ts` and the per-run color stickiness in
// `seriesColors.ts`, both pure and unit-tested; this file is the wiring. The
// behavior contract is `.scratch/prd-metrics-run-comparison.md`.
//
// Also watches `.viewer.json` at the results root so a coding agent can point
// the pane at a specific series/run set — see desktop/README.md's
// "Agent-driven viewing" section for the file format.

import { useEffect, useMemo, useRef, useState } from "react";
import { inv, subscribe } from "../tauri";
import Chart from "./Chart";
import {
  dedupeRunFiles,
  etaOf,
  parseMetricsText,
  parseViewerFile,
  pickSeries,
  runLabelOf,
  seriesOf,
  type Point,
  type ViewerFile,
} from "./metrics";
import {
  // aliased: the local memo below owns the bare name
  activePaths as activePathsOf,
  applyViewerRuns,
  autoPath,
  canPick,
  clearAll,
  INITIAL_SELECTION,
  isHeldByAuto,
  lineCount,
  MAX_LINES,
  toggleAuto,
  togglePin,
  removePath,
  type RunSelection,
} from "./runSelection";
import { assignColors, colorForSlot, type ColorAssignment } from "./seriesColors";

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
function RunPicker({ runFiles, selection, onToggleAuto, onTogglePin }: RunPickerProps) {
  const held = autoPath(selection, runFiles);
  const heldLabel = held ? runLabelOf(held) : null;

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
              // The auto-held row stays clickable even though it's dimmed —
              // clicking it promotes the run to an explicit pin, which is a
              // real action, not a disabled control.
              title={dimmed ? "held by auto — click to pin it explicitly" : undefined}
              className={`block w-full whitespace-nowrap px-2 py-0.5 text-left text-xs ${
                pinned
                  ? "bg-term-raised text-term-accent"
                  : dimmed
                    ? "text-term-dim italic"
                    : "text-term-fg"
              } ${enabled ? "" : "cursor-not-allowed opacity-40"}`}
            >
              {runLabelOf(path)}
            </button>
          </li>
        );
      })}
    </ul>
  );
}

export default function MetricsPane() {
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

  useEffect(() => {
    let cancelled = false;
    async function load() {
      await inv("fs_watch", { root: RESULTS_ROOT, rel: "" });
      const entries = await inv<Entry[]>("fs_list", {
        root: RESULTS_ROOT,
        rel: "",
        exts: ["jsonl", "json"],
      });
      if (cancelled) return;
      const filtered = entries.filter((e) => {
        const base = e.rel_path.split("/").pop() ?? "";
        return base === "metrics.jsonl" || base === "metrics.json";
      });
      // At most one file per run, so a run can never be pinned twice.
      setRunFiles(dedupeRunFiles(filtered));
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, []);

  async function tailRun(path: string) {
    let run = runsRef.current.get(path);
    if (!run) {
      run = { path, label: runLabelOf(path), offset: 0, points: [], arrivals: [] };
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

  // Refs so the fs-change / viewer-file handlers below always see current
  // values without re-subscribing the listener on every render.
  const runFilesRef = useRef(runFiles);
  runFilesRef.current = runFiles;
  const seriesNamesRef = useRef(seriesNames);
  seriesNamesRef.current = seriesNames;

  function applyViewerFile(parsed: ViewerFile) {
    if (parsed.series && seriesNamesRef.current.includes(parsed.series)) {
      selectSeries(parsed.series);
    }
    setTitles(parsed.titles ?? {});
    if (parsed.runs && parsed.runs.length > 0) {
      const runs = parsed.runs;
      // `applyViewerRuns` is the one that knows a manual clear outranks the
      // file until the operator picks something by hand again.
      setSelection((sel) => applyViewerRuns(sel, runFilesRef.current, runs));
    }
  }

  async function loadViewerFile() {
    try {
      const text = await inv<string>("fs_read_text", { root: RESULTS_ROOT, rel: VIEWER_FILE_REL });
      const parsed = parseViewerFile(text);
      if (parsed) applyViewerFile(parsed);
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
      }
    });
    return () => {
      cancelled = true;
      sub.unsubscribe();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const newestPath = runFiles[0]?.rel_path;
  const newestRun = newestPath ? runsRef.current.get(newestPath) : undefined;
  const eta = newestRun ? etaOf(newestRun.points, newestRun.arrivals) : null;

  const activeSeries = useMemo(
    () => pickSeries(seriesNames, storedSeries),
    [seriesNames, storedSeries],
  );

  const chartSeries = useMemo(() => {
    if (!activeSeries) return [];
    return activePaths
      .map((path) => ({ path, run: runsRef.current.get(path) }))
      .filter((r): r is { path: string; run: RunState } => r.run !== undefined)
      .map(({ path, run }) => ({
        label: `${run.label}/${titles[activeSeries] ?? activeSeries}`,
        points: seriesOf(run.points).get(activeSeries) ?? [],
        color: colorForSlot(colors.get(path) ?? 0),
        // The legend `×` always means "this line goes away" — including for
        // auto's line, which it switches auto off to remove.
        onRemove: () => setSelection((sel) => removePath(sel, runFilesRef.current, path)),
      }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activePaths, activeSeries, titles, colors, tick]);

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
          onToggleAuto={() => setSelection((sel) => toggleAuto(sel, runFiles))}
          onTogglePin={(path) => setSelection((sel) => togglePin(sel, runFiles, path))}
        />
        <button
          type="button"
          onClick={() => setSelection(clearAll)}
          disabled={lines === 0}
          className="shrink-0 border border-term-edge px-2 py-0.5 text-[11px] lowercase text-term-dim hover:text-term-accent disabled:opacity-40 disabled:hover:text-term-dim"
        >
          clear
        </button>
        {eta && (
          <div className="ml-2 font-mono text-term-dim">
            <span>steps/s: {eta.stepsPerSec?.toFixed(2) ?? "—"}</span>
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
