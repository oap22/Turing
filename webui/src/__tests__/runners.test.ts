// Runner table tests (issue #382).

import { execFileSync } from "node:child_process";
import { beforeEach, describe, expect, it } from "vitest";
import {
  __resetResultsRootForTests,
  AGENT_SANDBOX,
  RESULTS_ROOT_TOKEN,
  resolveRunner,
  resultsRoot,
  RUNNERS,
  substituteResultsRoot,
} from "../desktop/runners";

describe("RUNNERS", () => {
  beforeEach(() => {
    __resetResultsRootForTests();
  });

  it("has unique ids", () => {
    const ids = RUNNERS.map((r) => r.id);
    expect(new Set(ids).size).toBe(ids.length);
  });

  it("has a non-empty command for every runner", () => {
    for (const r of RUNNERS) {
      expect(r.command.length).toBeGreaterThan(0);
    }
  });

  it("uses every group present in the runner table (the spec's table has no 'research' entries, only agents/verify/remote/docs)", () => {
    const groups = new Set(RUNNERS.map((r) => r.group));
    expect(groups).toEqual(new Set(["agents", "verify", "remote", "docs"]));
  });

  // The agent runners exist to stop permission prompts, so each must carry
  // the results-root grant — and only the sandbox one may skip prompts, and
  // only from inside its sandbox directory, never from a checkout.
  it("grants claude runners the results root and confines prompt-skipping to the sandbox", () => {
    for (const id of ["claude", "claude-continue", "claude-sandbox"]) {
      const runner = RUNNERS.find((r) => r.id === id);
      expect(runner, id).toBeDefined();
      expect(runner?.command, id).toContain(`--add-dir ${RESULTS_ROOT_TOKEN}`);
      expect(runner?.group, id).toBe("agents");
    }
    for (const r of RUNNERS) {
      if (r.command.includes("--dangerously-skip-permissions")) {
        expect(r.id).toBe("claude-sandbox");
        expect(r.command).toContain(`cd ${AGENT_SANDBOX} &&`);
        expect(r.cwd).not.toContain("Turing");
      }
    }
  });

  it("substitutes the results root into the claude runners", async () => {
    const runner = RUNNERS.find((r) => r.id === "claude")!;
    const resolved = await resolveRunner(runner);
    expect(resolved.command).not.toContain(RESULTS_ROOT_TOKEN);
    expect(resolved.command).toMatch(/--add-dir \S+research-results/);
  });

  // Both remote streamers carry placeholders, so they must be pre-typed for
  // editing rather than run on selection.
  it("marks the placeholder-bearing ssh runners as autorun: false", () => {
    for (const id of ["ssh-follow-metrics", "ssh-pull-assets"]) {
      const runner = RUNNERS.find((r) => r.id === id);
      expect(runner, id).toBeDefined();
      expect(runner?.autorun, id).toBe(false);
      expect(runner?.command).toContain("<SSH_HOST>");
      expect(runner?.command).toContain("<REMOTE_RUN_DIR>");
    }
  });

  // Anything that actually runs on selection must be ready to run as-is —
  // `<RESULTS_ROOT>` excepted, since resolveRunner() fills that in for the
  // user before the command reaches a shell.
  it("leaves no user-supplied placeholder in an autorun runner", () => {
    for (const r of RUNNERS.filter((x) => x.autorun)) {
      expect(r.command.split(RESULTS_ROOT_TOKEN).join(""), r.id).not.toMatch(/<[A-Z_]+>/);
    }
  });

  // Anything runnable must be fully substituted after resolution, cwd included.
  it("resolves every autorun runner to a placeholder-free command and cwd", async () => {
    for (const r of RUNNERS.filter((x) => x.autorun)) {
      const resolved = await resolveRunner(r);
      expect(resolved.command, r.id).not.toMatch(/<[A-Z_]+>/);
      expect(resolved.cwd ?? "", r.id).not.toMatch(/<[A-Z_]+>/);
    }
  });

  it("keeps the rosie-specific conveniences pointed at the ROSIE alias", () => {
    for (const id of ["rosie-preflight", "rosie-queue"]) {
      const runner = RUNNERS.find((r) => r.id === id);
      expect(runner?.group, id).toBe("remote");
      expect(runner?.command, id).toContain("ROSIE");
    }
  });

  it("pulls remote assets into the watched results dir on a loop", () => {
    const pull = RUNNERS.find((r) => r.id === "ssh-pull-assets");
    expect(pull?.command).toContain("rsync");
    expect(pull?.command).toContain(`${RESULTS_ROOT_TOKEN}/rosie-live/`);
    expect(pull?.command).toContain("sleep 1");
  });

  // The whole point of the placeholder: nothing may pin the watched results
  // directory inside the Turing checkout, since research code lives in other
  // repos.
  it("hardcodes no in-repo results path", () => {
    for (const r of RUNNERS) {
      expect(r.command, r.id).not.toContain("research/results");
      expect(r.cwd ?? "", r.id).not.toContain("research/results");
    }
  });

  // Outside Tauri (tests, plain browser) there is no `app_config` to ask, so
  // resolution falls back to the same default `config.rs` ships.
  it("falls back to the project-agnostic default results root", async () => {
    expect(await resultsRoot()).toBe("~/research-results");
    const pull = RUNNERS.find((r) => r.id === "ssh-pull-assets")!;
    expect((await resolveRunner(pull)).command).toContain("~/research-results/rosie-live/");
    const results = RUNNERS.find((r) => r.id === "research-results")!;
    expect((await resolveRunner(results)).cwd).toBe("~/research-results");
  });

  it("round-trips configured roots with shell syntax and path suffixes", () => {
    const root = "/tmp/research results;$(printf injected)";
    const command = substituteResultsRoot(
      `rsi --results-root ${RESULTS_ROOT_TOKEN} tee ${RESULTS_ROOT_TOKEN}/rosie-live/metrics.jsonl`,
      root,
    );
    const args = execFileSync(
      "/bin/sh",
      ["-c", `set -- ${command}; printf '%s\\n' "$@"`],
      { encoding: "utf8" },
    )
      .trimEnd()
      .split("\n");
    expect(args).toEqual([
      "rsi",
      "--results-root",
      root,
      "tee",
      `${root}/rosie-live/metrics.jsonl`,
    ]);
  });

  it("keeps tilde expansion when a configured home path contains spaces", () => {
    const command = substituteResultsRoot(
      `tee ${RESULTS_ROOT_TOKEN}/rosie-live/metrics.jsonl`,
      "~/research results",
    );
    const args = execFileSync(
      "/bin/sh",
      ["-c", `set -- ${command}; printf '%s\\n' "$@"`],
      { encoding: "utf8", env: { ...process.env, HOME: "/tmp/fake-rsi-home" } },
    )
      .trimEnd()
      .split("\n");
    expect(args).toEqual(["tee", "/tmp/fake-rsi-home/research results/rosie-live/metrics.jsonl"]);
  });
});
