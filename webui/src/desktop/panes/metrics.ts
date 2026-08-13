// Pure parsing/aggregation for the metrics pane: `metrics.jsonl` (one JSON
// object per line, tolerant of a trailing partial line) or a whole-file
// `metrics.json` array.

export type Point = Record<string, number> & { step?: number };

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
//     "series": "loss",
//     "runs": ["run-42"],
//     "titles": { "loss": "DPO loss — run 42" }
//   }
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
