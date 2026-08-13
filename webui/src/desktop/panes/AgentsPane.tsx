// Agent viewer: left column lists live Claude Code / Codex CLI sessions
// (from `~/.claude/projects` and `~/.codex/sessions`), right column tails
// the selected transcript.

import { useEffect, useMemo, useRef, useState } from "react";
import { inv, subscribe } from "../tauri";
import {
  AGENT_FILTER_CHANGE_EVENT,
  AGENT_FILTER_STORAGE_KEY,
  filterSessions,
  parseExcludedProjects,
  sessionsFrom,
  parseTranscriptLine,
  type AgentSession,
  type Entry,
  type TranscriptEntry,
} from "./agents";

const CLAUDE_ROOT = "claude-sessions";
const CODEX_ROOT = "codex-sessions";
const TAIL_INITIAL_BYTES = 64 * 1024;
const MAX_ENTRIES = 500;
const LIST_DEBOUNCE_MS = 1000;

function ageLabel(mtimeMs: number, nowMs: number): string {
  const s = Math.max(0, Math.floor((nowMs - mtimeMs) / 1000));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h`;
}

function kindClass(kind: TranscriptEntry["kind"]): string {
  if (kind === "user") return "text-term-fg";
  if (kind === "assistant") return "text-term-accent";
  if (kind === "tool") return "text-amber-400";
  return "text-term-dim";
}

export default function AgentsPane() {
  const [sessions, setSessions] = useState<AgentSession[]>([]);
  const [selected, setSelected] = useState<AgentSession | null>(null);
  const [entries, setEntries] = useState<TranscriptEntry[]>([]);
  const [excluded, setExcluded] = useState<Set<string>>(() =>
    parseExcludedProjects(localStorage.getItem(AGENT_FILTER_STORAGE_KEY)),
  );
  const [filterOpen, setFilterOpen] = useState(false);
  const offsetRef = useRef(0);
  const listRef = useRef<HTMLDivElement | null>(null);
  const stickToBottom = useRef(true);
  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  function toggleProject(project: string) {
    setExcluded((prev) => {
      const next = new Set(prev);
      if (next.has(project)) next.delete(project);
      else next.add(project);
      localStorage.setItem(AGENT_FILTER_STORAGE_KEY, JSON.stringify([...next]));
      window.dispatchEvent(
        new CustomEvent(AGENT_FILTER_CHANGE_EVENT, { detail: [...next] }),
      );
      return next;
    });
  }

  async function refreshList() {
    const [claudeList, codexList] = await Promise.all([
      inv<Entry[]>("fs_list", { root: CLAUDE_ROOT, rel: "", exts: ["jsonl"] }),
      inv<Entry[]>("fs_list", { root: CODEX_ROOT, rel: "", exts: ["jsonl"] }),
    ]);
    setSessions(sessionsFrom(claudeList, codexList, Date.now()));
  }

  useEffect(() => {
    let cancelled = false;
    void Promise.all([
      inv("fs_watch", { root: CLAUDE_ROOT, rel: "" }),
      inv("fs_watch", { root: CODEX_ROOT, rel: "" }),
    ]).then(() => {
      if (!cancelled) void refreshList();
    });
    const sub = subscribe<{ root: string; rel_path: string }>("fs-change", (payload) => {
      if (cancelled) return;
      if (payload.root !== CLAUDE_ROOT && payload.root !== CODEX_ROOT) return;
      if (selected && payload.root === selected.root && payload.rel_path === selected.rel) {
        void tailSelected();
        return;
      }
      if (debounceRef.current) clearTimeout(debounceRef.current);
      debounceRef.current = setTimeout(() => void refreshList(), LIST_DEBOUNCE_MS);
    });
    return () => {
      cancelled = true;
      sub.unsubscribe();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected]);

  async function selectSession(s: AgentSession) {
    setSelected(s);
    setEntries([]);
    const fileEntries = s.agent === "claude"
      ? await inv<Entry[]>("fs_list", { root: CLAUDE_ROOT, rel: "", exts: ["jsonl"] })
      : await inv<Entry[]>("fs_list", { root: CODEX_ROOT, rel: "", exts: ["jsonl"] });
    const meta = fileEntries.find((e) => e.rel_path === s.rel);
    const size = meta?.size ?? 0;
    offsetRef.current = Math.max(0, size - TAIL_INITIAL_BYTES);
    await tailFor(s);
  }

  async function tailFor(s: AgentSession) {
    const chunk = await inv<{ data: string; offset: number }>("fs_tail", {
      root: s.root,
      rel: s.rel,
      offset: offsetRef.current,
    });
    offsetRef.current = chunk.offset;
    const parsed = chunk.data
      .split("\n")
      .map((line) => parseTranscriptLine(s.agent, line))
      .filter((e): e is TranscriptEntry => e !== null);
    if (parsed.length > 0) {
      setEntries((prev) => [...prev, ...parsed].slice(-MAX_ENTRIES));
    }
  }

  async function tailSelected() {
    if (selected) await tailFor(selected);
  }

  useEffect(() => {
    const el = listRef.current;
    if (el && stickToBottom.current) el.scrollTop = el.scrollHeight;
  }, [entries]);

  function onScroll() {
    const el = listRef.current;
    if (!el) return;
    stickToBottom.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
  }

  const now = Date.now();
  const allProjects = useMemo(
    () => [...new Set(sessions.map((s) => s.project))].sort(),
    [sessions],
  );
  const rows = useMemo(() => {
    if (excluded.size === 0) return filterSessions(sessions, null);
    const kept = new Set(allProjects.filter((p) => !excluded.has(p)));
    return filterSessions(sessions, kept);
  }, [sessions, excluded, allProjects]);

  return (
    <div className="flex h-full text-xs">
      <div className="flex w-[30%] shrink-0 flex-col overflow-hidden border-r border-term-edge">
        <div className="border-b border-term-edge">
          <button
            type="button"
            onClick={() => setFilterOpen((v) => !v)}
            className={`w-full px-2 py-1 text-left text-[11px] ${
              filterOpen ? "text-term-accent" : "text-term-dim hover:text-term-fg"
            }`}
          >
            [filter]
          </button>
          {filterOpen && (
            <div className="max-h-32 overflow-auto px-2 pb-1">
              {allProjects.length === 0 && (
                <div className="text-[10px] text-term-dim">no sessions yet</div>
              )}
              {allProjects.map((p) => (
                <label key={p} className="flex items-center gap-1.5 py-0.5 text-[10px] text-term-dim">
                  <input
                    type="checkbox"
                    checked={!excluded.has(p)}
                    onChange={() => toggleProject(p)}
                  />
                  <span className="truncate">{p}</span>
                </label>
              ))}
            </div>
          )}
        </div>
        <div className="flex-1 overflow-auto">
        {rows.map((s) => (
          <button
            key={`${s.agent}/${s.rel}`}
            type="button"
            onClick={() => void selectSession(s)}
            className={`flex w-full items-center gap-2 px-2 py-1 text-left ${
              selected?.rel === s.rel && selected.agent === s.agent
                ? "bg-term-raised text-term-fg"
                : "text-term-dim hover:text-term-fg"
            }`}
          >
            <span className={s.active ? "text-term-accent" : "text-term-dim"}>●</span>
            <span className="truncate">{s.project}</span>
            <span className="ml-auto text-[10px] text-term-dim">{ageLabel(s.mtimeMs, now)}</span>
          </button>
        ))}
        </div>
      </div>
      <div ref={listRef} onScroll={onScroll} className="flex-1 overflow-auto p-2 font-mono">
        {entries.map((e, i) => (
          <div key={i} className={kindClass(e.kind)}>
            {e.text}
          </div>
        ))}
      </div>
    </div>
  );
}
