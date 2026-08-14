// RSI command-building tests — pure functions, no DOM. The loop engine
// itself (scripts/rsi-loop.sh) is exercised separately via its --dry-run
// flag, not from vitest.

import { describe, expect, it } from "vitest";
import { isRsiParams, rsiRunner, rsiSlug, shellQuoteSingle } from "../desktop/rsi";
import { REPO, RESULTS_ROOT_TOKEN } from "../desktop/runners";

describe("rsiSlug", () => {
  it("lowercases and collapses non-alphanumerics to single hyphens", () => {
    expect(rsiSlug("My Cool Experiment!")).toBe("my-cool-experiment");
  });

  it("falls back to 'experiment' when nothing alphanumeric survives", () => {
    expect(rsiSlug("---")).toBe("experiment");
    expect(rsiSlug("")).toBe("experiment");
  });

  it("truncates long names to 40 chars with no trailing hyphen", () => {
    const long = "a".repeat(60);
    const slug = rsiSlug(long);
    expect(slug.length).toBeLessThanOrEqual(40);
    expect(slug.endsWith("-")).toBe(false);
  });

  it("always matches the slug charset", () => {
    for (const name of ["My Cool Experiment!", "---", "", "a".repeat(60), "  weird__name--", "42"]) {
      expect(rsiSlug(name)).toMatch(/^[a-z0-9-]+$/);
    }
  });
});

describe("shellQuoteSingle", () => {
  it("round-trips plain text wrapped in single quotes", () => {
    expect(shellQuoteSingle("hello world")).toBe("'hello world'");
  });

  it("escapes an embedded single quote", () => {
    expect(shellQuoteSingle("it's")).toBe("'it'\\''s'");
  });

  it("collapses embedded newlines and tabs to single spaces", () => {
    const quoted = shellQuoteSingle("line one\nline two\ttabbed");
    expect(quoted).not.toContain("\n");
    expect(quoted).not.toContain("\t");
    expect(quoted).toBe("'line one line two tabbed'");
  });
});

describe("isRsiParams", () => {
  it("accepts a well-formed params object", () => {
    expect(isRsiParams({ slug: "a-1", problem: "x" })).toBe(true);
  });

  it("rejects null and missing fields", () => {
    expect(isRsiParams(null)).toBe(false);
    expect(isRsiParams({})).toBe(false);
    expect(isRsiParams({ slug: "a" })).toBe(false);
    expect(isRsiParams({ problem: "x" })).toBe(false);
  });

  it("rejects an empty problem", () => {
    expect(isRsiParams({ slug: "a", problem: "" })).toBe(false);
  });

  it("rejects a slug with spaces or uppercase", () => {
    expect(isRsiParams({ slug: "a b", problem: "x" })).toBe(false);
    expect(isRsiParams({ slug: "Abc", problem: "x" })).toBe(false);
  });
});

describe("rsiRunner", () => {
  it("builds a pre-typed, never-autorun command", () => {
    const runner = rsiRunner({ slug: "demo", problem: "solve x" });
    expect(runner.command).toContain(RESULTS_ROOT_TOKEN);
    expect(runner.command).toContain("--slug demo");
    expect(runner.command).toContain(shellQuoteSingle("solve x"));
    expect(runner.autorun).toBe(false);
    expect(runner.group).toBe("research");
    expect(runner.cwd).toBe(REPO);
  });
});
