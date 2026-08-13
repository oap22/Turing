// Pure helpers for the agent-viewer panes: turning `fs_list` results into a
// session list, and tolerantly parsing Claude Code / Codex CLI transcript
// lines (the two on-disk JSONL formats drift a bit, hence the leniency).

export interface Entry {
  rel_path: string;
  is_dir: boolean;
  size: number;
  mtime_ms: number;
}

export interface AgentSession {
  agent: "claude" | "codex";
  root: string;
  rel: string;
  project: string;
  mtimeMs: number;
  active: boolean;
}

const ACTIVE_WINDOW_MS = 120_000;
const SESSION_CAP = 50;

function prettifySlug(slug: string): string {
  const segs = slug.split("-").filter(Boolean);
  return segs.slice(-2).join("/");
}

export function sessionsFrom(
  claudeList: Entry[],
  codexList: Entry[],
  nowMs: number,
): AgentSession[] {
  const sessions: AgentSession[] = [];

  for (const e of claudeList) {
    if (e.is_dir || !e.rel_path.endsWith(".jsonl")) continue;
    const parts = e.rel_path.split("/");
    if (parts.length !== 2) continue;
    sessions.push({
      agent: "claude",
      root: "claude-sessions",
      rel: e.rel_path,
      project: prettifySlug(parts[0]),
      mtimeMs: e.mtime_ms,
      active: nowMs - e.mtime_ms < ACTIVE_WINDOW_MS,
    });
  }

  for (const e of codexList) {
    if (e.is_dir || !e.rel_path.endsWith(".jsonl")) continue;
    const filename = e.rel_path.split("/").pop() ?? e.rel_path;
    sessions.push({
      agent: "codex",
      root: "codex-sessions",
      rel: e.rel_path,
      project: filename,
      mtimeMs: e.mtime_ms,
      active: nowMs - e.mtime_ms < ACTIVE_WINDOW_MS,
    });
  }

  sessions.sort((a, b) => b.mtimeMs - a.mtimeMs);
  return sessions.slice(0, SESSION_CAP);
}

// Shared between AgentsPane (writer) and AgentFeedPane (reader) so the two
// panes agree on where the excluded-project filter lives and how it's kept
// in sync live: localStorage for persistence across restarts, plus a
// same-tab CustomEvent (storage events don't fire in the tab that wrote the
// value) so both panes update without a reload.
export const AGENT_FILTER_STORAGE_KEY = "turing.agents.filter";
export const AGENT_FILTER_CHANGE_EVENT = "agentfilterchange";

export function parseExcludedProjects(raw: string | null): Set<string> {
  if (!raw) return new Set();
  try {
    const parsed: unknown = JSON.parse(raw);
    if (Array.isArray(parsed)) {
      return new Set(parsed.filter((x): x is string => typeof x === "string"));
    }
  } catch {
    // fall through to empty set
  }
  return new Set();
}

// `enabled` here is actually the set of projects the caller wants to KEEP —
// see AgentsPane/AgentFeedPane, which invert their stored *excluded*-project
// list into this "keep" set before calling. `null` means no filter (show
// everything); an unknown project name in the set is simply never matched
// against, which is harmless.
export function filterSessions(
  sessions: AgentSession[],
  enabled: ReadonlySet<string> | null,
): AgentSession[] {
  if (enabled === null) return sessions;
  return sessions.filter((s) => enabled.has(s.project));
}

export interface TranscriptEntry {
  kind: "user" | "assistant" | "tool" | "meta";
  text: string;
}

function truncate(s: string): string {
  return s.replace(/\r?\n/g, " ").slice(0, 200);
}

function isObj(x: unknown): x is Record<string, unknown> {
  return typeof x === "object" && x !== null;
}

function tryClaudeShape(o: Record<string, unknown>): TranscriptEntry | null {
  const type = o.type;
  if (type === "user" || type === "assistant") {
    const message = isObj(o.message) ? o.message : undefined;
    const content = message?.content;
    if (typeof content === "string") {
      return { kind: type, text: truncate(content) };
    }
    if (Array.isArray(content)) {
      const toolUse = content.find((it) => isObj(it) && it.type === "tool_use");
      if (toolUse && isObj(toolUse)) {
        const name = typeof toolUse.name === "string" ? toolUse.name : "?";
        return { kind: "tool", text: truncate(`[tool] ${name}`) };
      }
      const text = content
        .filter((it): it is Record<string, unknown> => isObj(it) && it.type === "text")
        .map((it) => String(it.text ?? ""))
        .join("");
      return { kind: type, text: truncate(text) };
    }
    return null;
  }
  if (typeof type === "string") {
    return { kind: "meta", text: truncate(type) };
  }
  return null;
}

export function parseTranscriptLine(
  agent: "claude" | "codex",
  line: string,
): TranscriptEntry | null {
  const t = line.trim();
  if (t === "") return null;
  let parsed: unknown;
  try {
    parsed = JSON.parse(t);
  } catch {
    return null;
  }
  if (!isObj(parsed)) return null;

  const claudeResult = tryClaudeShape(parsed);
  if (claudeResult) return claudeResult;

  if (agent === "codex") {
    const payload = isObj(parsed.payload) ? parsed.payload : undefined;
    if (payload && typeof payload.type === "string") {
      return { kind: "meta", text: truncate(payload.type) };
    }
    const textVal =
      typeof parsed.text === "string"
        ? parsed.text
        : typeof parsed.content === "string"
          ? parsed.content
          : null;
    if (textVal !== null && textVal.length < 400) {
      return { kind: "assistant", text: truncate(textVal) };
    }
  }

  return null;
}
