// Tolerant parser for `loop-*/trajectory.json` under the `results` root — the
// flywheel round timeline. Accepts a JSON array, `{rounds: [...]}`, or JSONL,
// and maps each round loosely since the trajectory shape has drifted across
// research-loop iterations.

export type RoundStatus = "pass" | "fail" | "other";

export interface Round {
  index: number;
  label: string;
  status: RoundStatus;
  detail: string;
}

const INDEX_KEYS = ["round", "index"];
const LABEL_KEYS = ["cell", "phase", "step", "slug"];
const STATUS_KEYS = ["passed", "ok", "success"];

function toRound(raw: unknown, position: number): Round | null {
  if (typeof raw !== "object" || raw === null || Array.isArray(raw)) return null;
  const obj = raw as Record<string, unknown>;

  let index = position;
  for (const k of INDEX_KEYS) {
    if (typeof obj[k] === "number") {
      index = obj[k] as number;
      break;
    }
  }

  let label = String(position);
  for (const k of LABEL_KEYS) {
    if (obj[k] !== undefined && obj[k] !== null) {
      label = String(obj[k]);
      break;
    }
  }

  let status: RoundStatus = "other";
  let statusKeyUsed: string | null = null;
  for (const k of STATUS_KEYS) {
    if (typeof obj[k] === "boolean") {
      status = obj[k] ? "pass" : "fail";
      statusKeyUsed = k;
      break;
    }
  }

  const used = new Set<string>([...INDEX_KEYS, ...LABEL_KEYS]);
  if (statusKeyUsed) used.add(statusKeyUsed);

  const detailFields: string[] = [];
  for (const [k, v] of Object.entries(obj)) {
    if (used.has(k)) continue;
    if (v === null || (typeof v !== "string" && typeof v !== "number" && typeof v !== "boolean")) {
      continue;
    }
    detailFields.push(k);
    if (detailFields.length >= 6) break;
  }
  const detailObj: Record<string, unknown> = {};
  for (const k of detailFields) detailObj[k] = obj[k];
  const detail = JSON.stringify(detailObj);

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
        const rounds = parsed.map((r, i) => toRound(r, i)).filter((r): r is Round => r !== null);
        return rounds.length > 0 ? rounds : null;
      }
      if (
        typeof parsed === "object" &&
        parsed !== null &&
        Array.isArray((parsed as Record<string, unknown>).rounds)
      ) {
        const arr = (parsed as Record<string, unknown>).rounds as unknown[];
        const rounds = arr.map((r, i) => toRound(r, i)).filter((r): r is Round => r !== null);
        return rounds.length > 0 ? rounds : null;
      }
    } catch {
      // fall through to JSONL
    }
  }

  const lines = text.split("\n").map((l) => l.trim()).filter(Boolean);
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
