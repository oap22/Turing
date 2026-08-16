// MetricsPane wiring tests, against a miniature of a real emitted results
// tree. These cover the four things the pure helpers in metrics.test.ts cannot:
// what auto-follow actually tails, what `.viewer.json` actually selects, that
// two pinned runs do not accumulate ghost curves, and that the ETA strip
// describes the run being charted rather than the newest file on disk.
//
// The fake `inv` mirrors `fsroots.rs`: `fs_list` returns entries newest-mtime
// first, which is the ordering the auto-follow defect depended on.

import { createElement } from "react";
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { invMock, fsChangeHandlers } = vi.hoisted(() => ({
  invMock: vi.fn(),
  // Every `fs-change` callback the pane has subscribed. Most tests assert on
  // the initial load and never touch these; the verdict-badge tests below
  // deliver a change through them, which is how the real watcher reaches the
  // pane (`fsroots.rs` emits one event per created/modified file).
  fsChangeHandlers: [] as Array<(payload: { root: string; rel_path: string }) => void>,
}));

vi.mock("../desktop/tauri", () => ({
  isTauri: () => true,
  inv: (cmd: string, args?: Record<string, unknown>) => invMock(cmd, args),
  subscribe: (_event: string, handler: (payload: { root: string; rel_path: string }) => void) => {
    fsChangeHandlers.push(handler);
    return { unsubscribe: () => {}, ready: Promise.resolve() };
  },
}));

import MetricsPane from "../desktop/panes/MetricsPane";

const LOOP = "loop-probe";
const R0 = `${LOOP}/round-00/attempts`;
const R1 = `${LOOP}/round-01/attempts`;

/** A `metrics.jsonl` chain: one JSON object per solver step. */
function chain(steps: number, progressStep: number, speedup: number): string {
  return Array.from({ length: steps }, (_, i) =>
    JSON.stringify({
      step: i + 1,
      total_steps: 10,
      ts: 1786845156 + i,
      progress: progressStep * (i + 1),
      speedup_ratio: speedup + i * 0.1,
      consumed_steps: i + 1,
    }),
  ).join("\n");
}

/** A `metrics.json` summary: the pretty-printed object `results.py` writes. */
function summary(extra: Record<string, unknown>): string {
  return JSON.stringify({ schema_version: 1, ...extra }, null, 2);
}

// Newest-mtime first, exactly as `fs_list` returns it — so index 0 (what
// auto-follow charts) is the round summary a completed round writes last.
const TREE: Array<[string, string]> = [
  [`${LOOP}/round-01/metrics.json`, summary({ round_index: 1, attempts: 3, tokens: 1320 })],
  [`${R1}/cuda/matmul-speedup/metrics.json`, summary({ problem_id: "cuda/matmul-speedup", best_score: 2.4 })],
  [`${R1}/cuda/matmul-speedup/metrics.jsonl`, chain(4, 0.12, 2.4)],
  [`${R1}/flat-baseline/metrics.json`, summary({ problem_id: "flat-baseline", best_score: 2.05 })],
  [`${R1}/flat-baseline/metrics.jsonl`, chain(3, 0.09, 2.05)],
  [`${LOOP}/round-00/metrics.json`, summary({ round_index: 0, attempts: 4 })],
  // A rotated, superseded chain. `_viewer_runs` omits it, and its path begins
  // with the directory of the live run right below it.
  [`${R0}/cuda/matmul-speedup/prior-1/metrics.jsonl`, chain(2, 0.5, 9.9)],
  [`${R0}/cuda/matmul-speedup/metrics.jsonl`, chain(5, 0.07, 1.7)],
  [`${R0}/bad-instrument/metrics.jsonl`, chain(2, 0.03, 1.1)],
];

const RUN_FILES = TREE.map(([p]) => p).filter((p) => p.endsWith("metrics.jsonl"));
const SUMMARY_FILES = TREE.map(([p]) => p).filter((p) => p.endsWith("metrics.json"));

let viewerJson: string | null = null;
// `metrics.verdict.json` bodies by relative path. Not part of `TREE` because
// `fs_list` is called with `exts: ["jsonl"]` and never walks them — the pane
// reads each one by name, beside the run file it is charting.
const verdictFiles = new Map<string, string>();

function installFs() {
  const contents = new Map(TREE);
  invMock.mockImplementation(async (cmd: string, args: Record<string, unknown> = {}) => {
    if (cmd === "fs_watch") return null;
    if (cmd === "fs_list") {
      return TREE.map(([rel], i) => ({
        rel_path: rel,
        is_dir: false,
        size: (contents.get(rel) ?? "").length,
        mtime_ms: 2_000_000 - i,
      }));
    }
    if (cmd === "fs_read_text") {
      if (args.rel === ".viewer.json" && viewerJson !== null) return viewerJson;
      const verdict = verdictFiles.get(String(args.rel));
      if (verdict !== undefined) return verdict;
      throw new Error(`path does not exist: ${String(args.rel)}`);
    }
    if (cmd === "fs_tail") {
      const data = contents.get(String(args.rel)) ?? "";
      const offset = Number(args.offset ?? 0);
      return { data: data.slice(offset), offset: data.length };
    }
    throw new Error(`unexpected command ${cmd}`);
  });
}

function tailedPaths(): string[] {
  return invMock.mock.calls
    .filter(([cmd]) => cmd === "fs_tail")
    .map(([, args]) => String((args as Record<string, unknown>).rel));
}

function runOptions(): HTMLElement[] {
  return within(screen.getByRole("listbox", { name: "runs" })).getAllByRole("option");
}

function selectedRunLabels(): string[] {
  return runOptions()
    .filter((o) => o.getAttribute("aria-selected") === "true")
    .map((o) => o.textContent ?? "");
}

beforeEach(() => {
  viewerJson = null;
  verdictFiles.clear();
  fsChangeHandlers.length = 0;
  invMock.mockReset();
  installFs();
  localStorage.clear();
});

/** Deliver one watcher event, exactly as `fsroots.rs` emits it. */
function emitFsChange(relPath: string) {
  for (const handler of fsChangeHandlers) handler({ root: "results", rel_path: relPath });
}

/** The body `results.write_attempt_verdict` writes. */
function verdictJson(state: string, linesChecked: number): string {
  return JSON.stringify({
    schema_version: 1,
    state,
    lines_checked: linesChecked,
    checked_at_ms: 1786845156000,
    checked_by: "loop",
    chain_head: "b7f0".repeat(16),
    detail: `<dir>: ${state.toUpperCase()} (${linesChecked} line(s) checked)`,
    note: "detects alteration; does not prevent it — see OPEN-QUESTIONS R2/Q11",
  });
}

function badge(): HTMLElement {
  return screen.getByTestId("metrics-verdict");
}

afterEach(() => {
  cleanup();
});

// jsdom has no ResizeObserver and Chart's `useSize` constructs one on mount.
// Installed once for the file rather than per-test: React flushes passive
// effects during `cleanup()` too, so a stub torn down around each test can be
// gone at the moment a chart unmounts.
globalThis.ResizeObserver = class {
  observe() {}
  unobserve() {}
  disconnect() {}
} as unknown as typeof ResizeObserver;

describe("MetricsPane — summaries are not runs (defect A)", () => {
  it("offers only the step logs as runs, never a metrics.json summary", async () => {
    render(createElement(MetricsPane));
    await waitFor(() => expect(runOptions().length).toBe(RUN_FILES.length + 1));

    const labels = runOptions().map((o) => o.textContent ?? "");
    expect(labels[0]).toBe("auto (newest)");
    // The old label — the first path segment — made every one of these read
    // `loop-probe`; each run must now name its own round and problem id.
    expect(labels.slice(1).sort()).toEqual(
      [
        `${LOOP}/round-01/attempts/cuda/matmul-speedup`,
        `${LOOP}/round-01/attempts/flat-baseline`,
        `${LOOP}/round-00/attempts/cuda/matmul-speedup/prior-1`,
        `${LOOP}/round-00/attempts/cuda/matmul-speedup`,
        `${LOOP}/round-00/attempts/bad-instrument`,
      ].sort(),
    );
    expect(new Set(labels).size).toBe(labels.length);
  });

  it("auto-follows a file that has points, not the round summary written last", async () => {
    render(createElement(MetricsPane));
    // The defect in one assertion: `fs_list` is newest-first and
    // `loop-probe/round-01/metrics.json` is index 0, so the pre-fix pane
    // tailed a pretty-printed object and charted nothing.
    await waitFor(() => expect(tailedPaths()).toContain(`${R1}/cuda/matmul-speedup/metrics.jsonl`));
    for (const s of SUMMARY_FILES) expect(tailedPaths()).not.toContain(s);

    // And the pane is not stuck on "no metrics yet" for a completed round.
    await waitFor(() => expect(screen.getByRole("button", { name: "progress" })).toBeInTheDocument());
    expect(screen.queryByText("no metrics yet")).toBeNull();
  });
});

describe("MetricsPane — .viewer.json runs (defect 1)", () => {
  it("selects exactly the emitted run paths, and not the prior-N sibling under one of them", async () => {
    // Verbatim `_viewer_runs()` output shape: attempt directories.
    viewerJson = JSON.stringify({
      series: "progress",
      runs: [`${R0}/cuda/matmul-speedup`, `${R1}/flat-baseline`],
      titles: { progress: "progress toward target" },
    });
    render(createElement(MetricsPane));

    await waitFor(() => expect(selectedRunLabels().length).toBe(2));
    expect(selectedRunLabels().sort()).toEqual(
      [`${R0}/cuda/matmul-speedup`, `${R1}/flat-baseline`].sort(),
    );
    // `.../matmul-speedup/prior-1` starts with a named directory but is a
    // different, superseded run — a prefix match would have charted it.
    expect(selectedRunLabels()).not.toContain(`${R0}/cuda/matmul-speedup/prior-1`);
  });

  it("is not satisfied by the loop name alone (the pre-fix comparison)", async () => {
    viewerJson = JSON.stringify({ runs: [LOOP] });
    render(createElement(MetricsPane));

    await waitFor(() => expect(runOptions().length).toBe(RUN_FILES.length + 1));
    // No run matches, so the default "auto" selection survives rather than
    // every run being swept in by a first-segment comparison.
    await waitFor(() => expect(selectedRunLabels()).toEqual(["auto (newest)"]));
  });

  it("applies a titles map to the charted series label", async () => {
    viewerJson = JSON.stringify({
      series: "progress",
      runs: [`${R0}/bad-instrument`],
      titles: { progress: "progress toward target" },
    });
    render(createElement(MetricsPane));

    await waitFor(() =>
      expect(
        screen.getByText(/progress toward target/, { selector: "span" }),
      ).toBeInTheDocument(),
    );
  });
});

describe("MetricsPane — chart keys (defect B)", () => {
  it("draws exactly one polyline per pinned run across series switches", async () => {
    viewerJson = JSON.stringify({
      series: "progress",
      // Two runs whose display labels collide once a title is applied is the
      // shape that used to accumulate ghost curves; here they are simply two
      // pinned runs, and the count must be stable across every series tab.
      runs: [`${R0}/cuda/matmul-speedup`, `${R1}/flat-baseline`],
      titles: { progress: "progress toward target" },
    });
    const { container } = render(createElement(MetricsPane));

    await waitFor(() => expect(container.querySelectorAll("polyline").length).toBe(2));
    for (const name of ["speedup_ratio", "consumed_steps", "progress"]) {
      screen.getByRole("button", { name }).click();
      await waitFor(() => expect(container.querySelectorAll("polyline").length).toBe(2));
    }
  });
});

describe("MetricsPane — ETA strip (defect C)", () => {
  it("describes the charted run, not the newest file on disk", async () => {
    viewerJson = JSON.stringify({ runs: [`${R0}/bad-instrument`] });
    render(createElement(MetricsPane));

    const strip = await screen.findByTestId("metrics-eta");
    // Named, so it cannot be read as belonging to some other run — and
    // populated, which it never was while it tracked a summary file.
    expect(strip).toHaveTextContent(`${R0}/bad-instrument`);
    expect(strip).toHaveTextContent("last step: 2");
    expect(strip).not.toHaveTextContent("last step: —");
  });

  it("follows the pinned run rather than staying on the newest file", async () => {
    viewerJson = JSON.stringify({ runs: [`${R0}/cuda/matmul-speedup`] });
    render(createElement(MetricsPane));

    const strip = await screen.findByTestId("metrics-eta");
    // 5 steps in this chain vs. 4 in the newest run file, so the number alone
    // distinguishes "the selected run" from "runFiles[0]".
    await waitFor(() => expect(strip).toHaveTextContent("last step: 5"));
    expect(strip).toHaveTextContent(`${R0}/cuda/matmul-speedup`);
  });
});

// `bad-instrument`'s chain is 2 lines long, so a verdict claiming 2 describes
// exactly what the pane parses and one claiming anything else does not.
const RUN = `${R0}/bad-instrument`;
const RUN_VERDICT = `${RUN}/metrics.verdict.json`;

describe("MetricsPane — verdict badge (RES-18)", () => {
  it("says chain ok, without claiming the numbers are trustworthy", async () => {
    verdictFiles.set(RUN_VERDICT, verdictJson("ok", 2));
    viewerJson = JSON.stringify({ runs: [RUN] });
    render(createElement(MetricsPane));

    await waitFor(() => expect(badge()).toHaveAttribute("data-state", "verified"));
    expect(badge()).toHaveTextContent("chain ok");
    // The qualifier is the point of the badge: a green chip that stopped
    // there would be read as "these numbers are real".
    const title = badge().getAttribute("title") ?? "";
    expect(title).toContain("not proof the numbers are authentic or meaningful");
    expect(title).toContain(`python -m turing.research.loop.verify ${RUN}`);
  });

  it("reads unverified when the loop left no verdict beside the run", async () => {
    viewerJson = JSON.stringify({ runs: [RUN] });
    render(createElement(MetricsPane));

    await waitFor(() => expect(badge()).toHaveAttribute("data-state", "unverified"));
  });

  it("reads stale when the verdict describes a different number of lines", async () => {
    verdictFiles.set(RUN_VERDICT, verdictJson("ok", 1));
    viewerJson = JSON.stringify({ runs: [RUN] });
    render(createElement(MetricsPane));

    await waitFor(() => expect(badge()).toHaveAttribute("data-state", "stale"));
  });

  it("reads failed when the loop's own check failed", async () => {
    verdictFiles.set(RUN_VERDICT, verdictJson("failed", 2));
    viewerJson = JSON.stringify({ runs: [RUN] });
    render(createElement(MetricsPane));

    await waitFor(() => expect(badge()).toHaveAttribute("data-state", "failed"));
  });

  it("picks the verdict up when the loop writes it mid-session", async () => {
    viewerJson = JSON.stringify({ runs: [RUN] });
    render(createElement(MetricsPane));
    await waitFor(() => expect(badge()).toHaveAttribute("data-state", "unverified"));

    // The attempt ends: `write_attempt_verdict` lands the file and the
    // watcher reports it. The operator must not have to reopen the pane.
    verdictFiles.set(RUN_VERDICT, verdictJson("ok", 2));
    emitFsChange(RUN_VERDICT);
    await waitFor(() => expect(badge()).toHaveAttribute("data-state", "verified"));
  });

  it("re-reads the verdict when it is rewritten", async () => {
    verdictFiles.set(RUN_VERDICT, verdictJson("ok", 2));
    viewerJson = JSON.stringify({ runs: [RUN] });
    render(createElement(MetricsPane));
    await waitFor(() => expect(badge()).toHaveAttribute("data-state", "verified"));

    verdictFiles.set(RUN_VERDICT, verdictJson("failed", 2));
    emitFsChange(RUN_VERDICT);
    await waitFor(() => expect(badge()).toHaveAttribute("data-state", "failed"));
  });

  it("gives each pinned run its own badge, named", async () => {
    // One badge for two runs would attribute one run's verdict to the other —
    // the same lie the ETA strip's run label exists to prevent.
    verdictFiles.set(RUN_VERDICT, verdictJson("ok", 2));
    verdictFiles.set(`${R1}/flat-baseline/metrics.verdict.json`, verdictJson("failed", 3));
    viewerJson = JSON.stringify({ runs: [RUN, `${R1}/flat-baseline`] });
    render(createElement(MetricsPane));

    await waitFor(() => expect(screen.getAllByTestId("metrics-verdict").length).toBe(2));
    const states = screen
      .getAllByTestId("metrics-verdict")
      .map((b) => [b.getAttribute("data-run"), b.getAttribute("data-state")]);
    expect(states).toContainEqual([RUN, "verified"]);
    expect(states).toContainEqual([`${R1}/flat-baseline`, "failed"]);
  });
});
