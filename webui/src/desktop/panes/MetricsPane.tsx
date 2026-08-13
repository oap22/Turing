// Live metrics pane: tails every `metrics.jsonl`/`metrics.json` under the
// `results` root, auto-follows the newest run, and lets the operator pin
// additional runs to overlay for comparison. Shows one series at a time via
// tabs (rather than every series stacked, which read as jumbled and clipped
// its own top tick label against the row above it — see Chart.tsx for the
// tick-clipping fix). Also watches `.viewer.json` at the results root so a
// coding agent can point the pane at a specific series/run set — see
// desktop/README.md's "Agent-driven viewing" section for the file format.

import { useEffect, useMemo, useRef, useState } from "react";
import { inv, subscribe } from "../tauri";
import Chart from "./Chart";
import {
  etaOf,
  parseMetricsText,
  parseViewerFile,
  pickSeries,
  seriesOf,
  type Point,
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

function runLabelOf(relPath: string): string {
  const parts = relPath.split("/");
  return parts.length > 1 ? parts[0] : relPath;
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
        exts: ["jsonl", "json"],
      });
      if (cancelled) return;
      const filtered = entries.filter((e) => {
        const base = e.rel_path.split("/").pop() ?? "";
        return base === "metrics.jsonl" || base === "metrics.json";
      });
      setRunFiles(filtered);
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
      const matched = runFilesRef.current
        .filter((f) => parsed.runs?.includes(runLabelOf(f.rel_path)))
        .map((f) => f.rel_path);
      if (matched.length > 0) setSelected(matched);
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
      .map((path) => runsRef.current.get(path))
      .filter((r): r is RunState => r !== undefined)
      .map((run) => ({
        label: `${run.label}/${titles[activeSeries] ?? activeSeries}`,
        points: seriesOf(run.points).get(activeSeries) ?? [],
      }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activePaths, activeSeries, titles, tick]);

  return (
    <div className="flex h-full flex-col text-xs">
      <div className="flex items-center gap-2 border-b border-term-edge p-2">
        <span className="text-[10px] uppercase tracking-wider text-term-dim">runs</span>
        <select
          multiple
          size={4}
          value={selected}
          onChange={(e) =>
            setSelected(Array.from(e.target.selectedOptions).map((o) => o.value))
          }
          className="min-w-[160px] border border-term-edge bg-term-bg text-term-fg"
        >
          <option value="auto">auto (newest)</option>
          {runFiles.map((f) => (
            <option key={f.rel_path} value={f.rel_path}>
              {runLabelOf(f.rel_path)}
            </option>
          ))}
        </select>
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
        {activeSeries ? (
          <Chart series={chartSeries} />
        ) : (
          <div className="p-4 text-term-dim">no metrics yet</div>
        )}
      </div>
    </div>
  );
}
