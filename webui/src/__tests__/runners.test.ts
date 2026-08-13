// Runner table tests (issue #382).

import { beforeEach, describe, expect, it } from "vitest";
import {
  __resetResultsRootForTests,
  RESULTS_ROOT_TOKEN,
  resolveRunner,
  resultsRoot,
  RUNNERS,
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

  it("uses every group present in the runner table (the spec's table has no 'research' entries, only verify/remote/docs)", () => {
    const groups = new Set(RUNNERS.map((r) => r.group));
    expect(groups).toEqual(new Set(["verify", "remote", "docs"]));
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
    expect(pull?.command).toContain("sleep 30");
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
});
