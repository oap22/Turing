// Agent-viewer pure helpers (issue #382): session listing from fs_list
// entries, and tolerant Claude/Codex transcript-line parsing.

import { describe, expect, it } from "vitest";
import {
  filterSessions,
  parseTranscriptLine,
  sessionsFrom,
  type AgentSession,
  type Entry,
} from "../desktop/panes/agents";

describe("sessionsFrom", () => {
  it("builds claude sessions from depth-2 jsonl files and prettifies the slug", () => {
    const claudeList: Entry[] = [
      { rel_path: "-Users-owen-Developer-active-Turing/abc-123.jsonl", is_dir: false, size: 10, mtime_ms: 1000 },
      { rel_path: "not-a-session-dir", is_dir: true, size: 0, mtime_ms: 999 },
    ];
    const sessions = sessionsFrom(claudeList, [], 2000);
    expect(sessions).toHaveLength(1);
    expect(sessions[0]).toMatchObject({
      agent: "claude",
      project: "active/Turing",
      active: true,
    });
  });

  it("builds codex sessions with the filename as project, sorts newest first, caps at 50", () => {
    const codexList: Entry[] = Array.from({ length: 60 }, (_, i) => ({
      rel_path: `2026/01/${String(i).padStart(2, "0")}/session-${i}.jsonl`,
      is_dir: false,
      size: 1,
      mtime_ms: i,
    }));
    const sessions = sessionsFrom([], codexList, 1_000_000);
    expect(sessions).toHaveLength(50);
    expect(sessions[0].mtimeMs).toBeGreaterThan(sessions[1].mtimeMs);
    expect(sessions[0].project).toBe("session-59.jsonl");
  });

  it("marks a session inactive once it's older than the active window", () => {
    const claudeList: Entry[] = [
      { rel_path: "a-b/uuid.jsonl", is_dir: false, size: 1, mtime_ms: 0 },
    ];
    const sessions = sessionsFrom(claudeList, [], 200_000);
    expect(sessions[0].active).toBe(false);
  });
});

describe("filterSessions", () => {
  function session(project: string): AgentSession {
    return { agent: "claude", root: "claude-sessions", rel: `${project}.jsonl`, project, mtimeMs: 0, active: false };
  }

  it("a null filter passes every session through unchanged", () => {
    const sessions = [session("alpha"), session("beta")];
    expect(filterSessions(sessions, null)).toEqual(sessions);
  });

  it("an exclusion (projects not in the kept set) drops matching sessions", () => {
    const sessions = [session("alpha"), session("beta"), session("gamma")];
    const kept = filterSessions(sessions, new Set(["alpha", "gamma"]));
    expect(kept.map((s) => s.project)).toEqual(["alpha", "gamma"]);
  });

  it("an unknown project name in the kept set is harmless (matches nothing extra)", () => {
    const sessions = [session("alpha")];
    expect(filterSessions(sessions, new Set(["nonexistent-project"]))).toEqual([]);
    expect(filterSessions(sessions, new Set(["alpha", "nonexistent-project"]))).toEqual(sessions);
  });

  it("an empty kept set drops every session", () => {
    const sessions = [session("alpha"), session("beta")];
    expect(filterSessions(sessions, new Set())).toEqual([]);
  });
});

describe("parseTranscriptLine", () => {
  it("parses a claude user text line", () => {
    const line = JSON.stringify({
      type: "user",
      message: { content: "hello there" },
    });
    expect(parseTranscriptLine("claude", line)).toEqual({
      kind: "user",
      text: "hello there",
    });
  });

  it("parses a claude tool_use entry", () => {
    const line = JSON.stringify({
      type: "assistant",
      message: { content: [{ type: "tool_use", name: "Read", input: {} }] },
    });
    expect(parseTranscriptLine("claude", line)).toEqual({
      kind: "tool",
      text: "[tool] Read",
    });
  });

  it("treats a non-user/assistant type as meta", () => {
    const line = JSON.stringify({ type: "summary" });
    expect(parseTranscriptLine("claude", line)).toEqual({ kind: "meta", text: "summary" });
  });

  it("falls back to payload.type as meta, or a short text field, for codex", () => {
    const metaLine = JSON.stringify({ payload: { type: "turn_start" } });
    expect(parseTranscriptLine("codex", metaLine)).toEqual({
      kind: "meta",
      text: "turn_start",
    });

    const textLine = JSON.stringify({ text: "short reply" });
    expect(parseTranscriptLine("codex", textLine)).toEqual({
      kind: "assistant",
      text: "short reply",
    });
  });

  it("returns null for garbage and unparseable shapes", () => {
    expect(parseTranscriptLine("claude", "not json")).toBeNull();
    expect(parseTranscriptLine("claude", "")).toBeNull();
    expect(parseTranscriptLine("codex", JSON.stringify({ some: "field" }))).toBeNull();
  });
});
