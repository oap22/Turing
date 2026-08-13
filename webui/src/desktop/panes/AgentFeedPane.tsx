// Cross-session activity feed: watches both agent roots and surfaces only
// *new* tool/assistant activity as it streams in, across every session.

import { useEffect, useRef, useState } from "react";
import { inv, subscribe } from "../tauri";
import {
  AGENT_FILTER_CHANGE_EVENT,
  AGENT_FILTER_STORAGE_KEY,
  parseExcludedProjects,
  parseTranscriptLine,
  type Entry,
  type TranscriptEntry,
} from "./agents";

const CLAUDE_ROOT = "claude-sessions";
const CODEX_ROOT = "codex-sessions";
const FEED_CAP = 200;

interface FeedItem {
  ts: number;
  agent: "claude" | "codex";
  project: string;
  text: string;
}

function agentOf(root: string): "claude" | "codex" {
  return root === CLAUDE_ROOT ? "claude" : "codex";
}

function projectFromRel(root: string, rel: string): string {
  if (root === CLAUDE_ROOT) {
    const slug = rel.split("/")[0] ?? rel;
    const segs = slug.split("-").filter(Boolean);
    return segs.slice(-2).join("/");
  }
  return rel.split("/").pop() ?? rel;
}

function timeLabel(ts: number): string {
  return new Date(ts).toTimeString().slice(0, 8);
}

function agentClass(agent: "claude" | "codex"): string {
  return agent === "claude" ? "text-term-accent" : "text-emerald-400";
}

export default function AgentFeedPane() {
  const [items, setItems] = useState<FeedItem[]>([]);
  const offsets = useRef<Map<string, number>>(new Map());
  const excludedRef = useRef<Set<string>>(
    parseExcludedProjects(localStorage.getItem(AGENT_FILTER_STORAGE_KEY)),
  );

  useEffect(() => {
    let cancelled = false;

    function onFilterChange(e: Event) {
      const detail = (e as CustomEvent<string[]>).detail;
      excludedRef.current = Array.isArray(detail)
        ? new Set(detail)
        : parseExcludedProjects(localStorage.getItem(AGENT_FILTER_STORAGE_KEY));
    }
    window.addEventListener(AGENT_FILTER_CHANGE_EVENT, onFilterChange);

    async function initOffsets() {
      await Promise.all([
        inv("fs_watch", { root: CLAUDE_ROOT, rel: "" }),
        inv("fs_watch", { root: CODEX_ROOT, rel: "" }),
      ]);
      const [claudeList, codexList] = await Promise.all([
        inv<Entry[]>("fs_list", { root: CLAUDE_ROOT, rel: "", exts: ["jsonl"] }),
        inv<Entry[]>("fs_list", { root: CODEX_ROOT, rel: "", exts: ["jsonl"] }),
      ]);
      if (cancelled) return;
      for (const e of claudeList) offsets.current.set(`${CLAUDE_ROOT}/${e.rel_path}`, e.size);
      for (const e of codexList) offsets.current.set(`${CODEX_ROOT}/${e.rel_path}`, e.size);
    }
    void initOffsets();

    const sub = subscribe<{ root: string; rel_path: string }>("fs-change", (payload) => {
      if (cancelled) return;
      if (payload.root !== CLAUDE_ROOT && payload.root !== CODEX_ROOT) return;
      void handleChange(payload.root, payload.rel_path);
    });

    async function handleChange(root: string, rel: string) {
      // Re-read the exclusion in case it changed and this pane missed the
      // same-tab CustomEvent (e.g. it fired before this listener attached).
      excludedRef.current = parseExcludedProjects(localStorage.getItem(AGENT_FILTER_STORAGE_KEY));

      const key = `${root}/${rel}`;
      const offset = offsets.current.get(key) ?? 0;
      const chunk = await inv<{ data: string; offset: number }>("fs_tail", {
        root,
        rel,
        offset,
      });
      offsets.current.set(key, chunk.offset);
      const agent = agentOf(root);
      const project = projectFromRel(root, rel);
      if (excludedRef.current.has(project)) return;
      const now = Date.now();
      const parsed = chunk.data
        .split("\n")
        .map((line) => parseTranscriptLine(agent, line))
        .filter((e): e is TranscriptEntry => e !== null)
        .filter((e) => e.kind === "tool" || e.kind === "assistant");
      if (parsed.length === 0) return;
      setItems((prev) =>
        [
          ...parsed.map((e) => ({ ts: now, agent, project, text: e.text })).reverse(),
          ...prev,
        ].slice(0, FEED_CAP),
      );
    }

    return () => {
      cancelled = true;
      sub.unsubscribe();
      window.removeEventListener(AGENT_FILTER_CHANGE_EVENT, onFilterChange);
    };
  }, []);

  return (
    <div className="h-full overflow-auto p-2 font-mono text-xs">
      {items.length === 0 && (
        <div className="p-2 text-term-dim">waiting for agent activity…</div>
      )}
      {items.map((item, i) => (
        <div key={i} className="flex gap-2">
          <span className="text-term-dim">{timeLabel(item.ts)}</span>
          <span className={agentClass(item.agent)}>
            {item.agent}/{item.project}
          </span>
          <span className="truncate text-term-fg">{item.text}</span>
        </div>
      ))}
    </div>
  );
}
