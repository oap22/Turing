// MetricsPane wiring tests, against a miniature of a real emitted results
// tree. These cover the four things the pure helpers in metrics.test.ts cannot:
// what auto-follow actually tails, what `.viewer.json` actually selects, that
// two pinned runs do not accumulate ghost curves, and that the ETA strip
// describes the run being charted rather than the newest file on disk.
//
// The fake `inv` mirrors `fsroots.rs`: `fs_list` returns entries newest-mtime
// first, which is the ordering the auto-follow defect depended on.

import { createElement } from "react";
import {
  act,
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { invMock, fsChangeHandlers } = vi.hoisted(() => ({
  invMock: vi.fn(),
  // Every `fs-change` callback the pane has subscribed. Most tests assert on
  // the initial load and never touch these; the verdict-badge tests below
  // deliver a change through them, which is how the real watcher reaches the
  // pane (`fsroots.rs` emits one event per created/modified file).
  fsChangeHandlers: [] as Array<
    (payload: { root: string; rel_path: string }) => void
  >,
}));

vi.mock("../desktop/tauri", () => ({
  isTauri: () => true,
  inv: (cmd: string, args?: Record<string, unknown>) => invMock(cmd, args),
  subscribe: (
    _event: string,
    handler: (payload: { root: string; rel_path: string }) => void,
  ) => {
    fsChangeHandlers.push(handler);
    return { unsubscribe: () => {}, ready: Promise.resolve() };
  },
}));

import MetricsPane from "../desktop/panes/MetricsPane";
// Not mocked: publishing here reaches the pane through the same module the
// flywheel pane's [metrics] link publishes through.
import {
  __resetPaneLinkForTests,
  publishMetricsTarget,
} from "../desktop/panes/paneLink";

const LOOP = "loop-probe";
const R0 = `${LOOP}/round-00/attempts`;
const R1 = `${LOOP}/round-01/attempts`;
// The auto row names the run it currently follows (the newest listed file)
// rather than reading a generic "auto (newest)" — see RunPicker.
const AUTO_R1 = `auto → ${R1}/cuda/matmul-speedup`;

/**
 * A `metrics.jsonl` chain: one JSON object per solver step.
 *
 * `_chain` is the field `MetricsWriter._append_sync` adds last, as
 * `"<seq>:<digest>"`. The digests here are not real sha256 — nothing on this
 * side recomputes them — but they are per-run and per-line distinct, which is
 * the property the badge's byte binding turns on: the digest of the LAST line
 * is the string the loop copies into the verdict's `chain_head`.
 *
 * Every line ends in `\n`, as the writer's own `handle.write("\n")` leaves it
 * — and as the fake `fs_tail` below requires, since (like the real one) it
 * hands back whole lines only.
 */
function chain(
  steps: number,
  progressStep: number,
  speedup: number,
  tag = "seed",
): string {
  return Array.from(
    { length: steps },
    (_, i) => `${chainLine(i, progressStep, speedup, tag)}\n`,
  ).join("");
}

function chainLine(
  i: number,
  progressStep: number,
  speedup: number,
  tag: string,
): string {
  return JSON.stringify({
    step: i + 1,
    total_steps: 10,
    ts: 1786845156 + i,
    progress: progressStep * (i + 1),
    speedup_ratio: speedup + i * 0.1,
    consumed_steps: i + 1,
    _chain: `${i}:${digestOf(tag, i)}`,
  });
}

function digestOf(tag: string, seq: number): string {
  return `${tag}-${seq}`.padEnd(64, "0");
}

/** The digest a `chain(steps, …, tag)` ends on — i.e. its sidecar's `final`. */
function headOf(tag: string, steps: number): string {
  return digestOf(tag, steps - 1);
}

/** A `metrics.json` summary: the pretty-printed object `results.py` writes. */
function summary(extra: Record<string, unknown>): string {
  return JSON.stringify({ schema_version: 1, ...extra }, null, 2);
}

// Newest-mtime first, exactly as `fs_list` returns it — so index 0 (what
// auto-follow charts) is the round summary a completed round writes last.
const TREE: Array<[string, string]> = [
  [
    `${LOOP}/round-01/metrics.json`,
    summary({ round_index: 1, attempts: 3, tokens: 1320 }),
  ],
  [
    `${R1}/cuda/matmul-speedup/metrics.json`,
    summary({ problem_id: "cuda/matmul-speedup", best_score: 2.4 }),
  ],
  [`${R1}/cuda/matmul-speedup/metrics.jsonl`, chain(4, 0.12, 2.4, "r1cuda")],
  [
    `${R1}/flat-baseline/metrics.json`,
    summary({ problem_id: "flat-baseline", best_score: 2.05 }),
  ],
  [`${R1}/flat-baseline/metrics.jsonl`, chain(3, 0.09, 2.05, "r1flat")],
  [`${LOOP}/round-00/metrics.json`, summary({ round_index: 0, attempts: 4 })],
  // A rotated, superseded chain. `_viewer_runs` omits it, and its path begins
  // with the directory of the live run right below it.
  [
    `${R0}/cuda/matmul-speedup/prior-1/metrics.jsonl`,
    chain(2, 0.5, 9.9, "r0prior"),
  ],
  [`${R0}/cuda/matmul-speedup/metrics.jsonl`, chain(5, 0.07, 1.7, "r0cuda")],
  [`${R0}/bad-instrument/metrics.jsonl`, chain(2, 0.03, 1.1, "r0bad")],
];

const RUN_FILES = TREE.map(([p]) => p).filter((p) =>
  p.endsWith("metrics.jsonl"),
);
const SUMMARY_FILES = TREE.map(([p]) => p).filter((p) =>
  p.endsWith("metrics.json"),
);

let viewerJson: string | null = null;
// The results root, mutable so a test can model what the loop does to it
// mid-session (a re-drive rotating a run's files into `prior-N/`, a solver
// appending a line). Insertion order is `fs_list`'s newest-mtime-first order,
// so a `set` on an existing key leaves that file where it was.
let contents: Map<string, string>;
// `metrics.verdict.json` bodies by relative path. Not part of `TREE` because
// `fs_list` is called with `exts: ["jsonl"]` and never walks them — the pane
// reads each one by name, beside the run file it is charting.
const verdictFiles = new Map<string, string>();
// Inode per run file, assigned lazily on first read. `contents.set` on an
// existing path is a write to the same file (append, or truncate in place —
// same inode); `replaceFile` below is what a re-drive does: the old file moves
// away and a NEW file appears at the path.
const inodes = new Map<string, number>();
let nextIno = 100;

function inodeOf(rel: string): number {
  let ino = inodes.get(rel);
  if (ino === undefined) {
    ino = nextIno++;
    inodes.set(rel, ino);
  }
  return ino;
}

/** A new file at `rel` — new inode — the way the loop opens a fresh
 * `metrics.jsonl` after moving the old one into `prior-N/`. */
function replaceFile(rel: string, body: string) {
  inodes.delete(rel);
  contents.set(rel, body);
}

/**
 * `fsroots.rs::tail_impl`, modelled field for field. Byte offsets are string
 * offsets here — every byte the tests write is ASCII, so the two agree.
 *
 *   restarted := offset > len          start := restarted ? 0 : offset
 *   data      := bytes [start, len) cut after the LAST `\n` (a trailing
 *                partial line is held back; no `\n` at all → "")
 *   offset    := start + data.length
 *   dev/ino   := the file's identity
 */
function fakeTail(rel: string, offset: number) {
  const data = contents.get(rel);
  if (data === undefined) throw new Error(`path does not exist: ${rel}`);
  const restarted = offset > data.length;
  const start = restarted ? 0 : offset;
  const raw = data.slice(start);
  const nl = raw.lastIndexOf("\n");
  const buf = nl < 0 ? "" : raw.slice(0, nl + 1);
  return {
    data: buf,
    offset: start + buf.length,
    start,
    dev: 1,
    ino: inodeOf(rel),
    restarted,
  };
}

function installFs() {
  invMock.mockImplementation(
    async (cmd: string, args: Record<string, unknown> = {}) => {
      if (cmd === "fs_watch") return null;
      if (cmd === "fs_list") {
        return [...contents.keys()].map((rel, i) => ({
          rel_path: rel,
          is_dir: false,
          size: (contents.get(rel) ?? "").length,
          mtime_ms: 2_000_000 - i,
        }));
      }
      if (cmd === "fs_read_text") {
        if (args.rel === ".viewer.json" && viewerJson !== null)
          return viewerJson;
        const verdict = verdictFiles.get(String(args.rel));
        if (verdict !== undefined) return verdict;
        throw new Error(`path does not exist: ${String(args.rel)}`);
      }
      if (cmd === "fs_tail")
        return fakeTail(String(args.rel), Number(args.offset ?? 0));
      throw new Error(`unexpected command ${cmd}`);
    },
  );
}

/** The y-axis tick labels of the chart, as numbers — the domain the drawn
 * curve spans. Points from a rotated-away generation stapled in front of the
 * live one show up here as a domain that reaches down to the old values. */
function yTicks(): number[] {
  return Array.from(document.querySelectorAll('svg text[x="4"]')).map((t) =>
    Number(t.textContent),
  );
}

function tailedPaths(): string[] {
  return invMock.mock.calls
    .filter(([cmd]) => cmd === "fs_tail")
    .map(([, args]) => String((args as Record<string, unknown>).rel));
}

function runOptions(): HTMLElement[] {
  return within(screen.getByRole("listbox", { name: "runs" })).getAllByRole(
    "option",
  );
}

function selectedRunLabels(): string[] {
  return runOptions()
    .filter((o) => o.getAttribute("aria-selected") === "true")
    .map((o) => o.textContent ?? "");
}

// This vitest/jsdom/Node combination doesn't provide a working `localStorage`
// out of the box (jsdom 29 defers to the platform's Web Storage, which Node
// gates behind `--localstorage-file`) — stub a minimal in-memory one per
// test, the same way theme.test.ts does and for the same reason.
function memoryStorage(): Storage {
  const store = new Map<string, string>();
  return {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
    key: (i: number) => Array.from(store.keys())[i] ?? null,
    get length() {
      return store.size;
    },
  } as Storage;
}

beforeEach(() => {
  vi.stubGlobal("localStorage", memoryStorage());
  viewerJson = null;
  contents = new Map(TREE);
  verdictFiles.clear();
  inodes.clear();
  fsChangeHandlers.length = 0;
  invMock.mockReset();
  installFs();
});

/**
 * Deliver one watcher event, exactly as `fsroots.rs` emits it.
 *
 * The handler emits for `EventKind::Create | Modify` **and** only when
 * `path.is_file()` at handler time — so a path that has been renamed away
 * produces nothing at all for its old name. Modelling that guard is the whole
 * point: the rotation ghost lives in the events the watcher never sends.
 */
function emitFsChange(relPath: string) {
  if (!contents.has(relPath) && !verdictFiles.has(relPath)) return;
  for (const handler of fsChangeHandlers)
    handler({ root: "results", rel_path: relPath });
}

/** The body `results.write_attempt_verdict` writes. */
function verdictJson(
  state: string,
  linesChecked: number,
  chainHead: string | null,
): string {
  return JSON.stringify({
    schema_version: 1,
    state,
    lines_checked: linesChecked,
    checked_at_ms: 1786845156000,
    checked_by: "loop",
    chain_head: chainHead,
    detail: `<dir>: ${state.toUpperCase()} (${linesChecked} line(s) checked)`,
    note: "detects alteration; does not prevent it — see OPEN-QUESTIONS R2/Q11",
  });
}

function badge(): HTMLElement {
  return screen.getByTestId("metrics-verdict");
}

afterEach(() => {
  cleanup();
  __resetPaneLinkForTests();
  vi.unstubAllGlobals();
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
    expect(labels[0]).toBe(AUTO_R1);
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
    await waitFor(() =>
      expect(tailedPaths()).toContain(
        `${R1}/cuda/matmul-speedup/metrics.jsonl`,
      ),
    );
    for (const s of SUMMARY_FILES) expect(tailedPaths()).not.toContain(s);

    // And the pane is not stuck on "no metrics yet" for a completed round.
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "progress" }),
      ).toBeInTheDocument(),
    );
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
    expect(selectedRunLabels()).not.toContain(
      `${R0}/cuda/matmul-speedup/prior-1`,
    );
  });

  it("is not satisfied by the loop name alone (the pre-fix comparison)", async () => {
    viewerJson = JSON.stringify({ runs: [LOOP] });
    render(createElement(MetricsPane));

    await waitFor(() => expect(runOptions().length).toBe(RUN_FILES.length + 1));
    // No run matches, so the default "auto" selection survives rather than
    // every run being swept in by a first-segment comparison.
    await waitFor(() => expect(selectedRunLabels()).toEqual([AUTO_R1]));
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

    await waitFor(() =>
      expect(container.querySelectorAll("polyline").length).toBe(2),
    );
    for (const name of ["speedup_ratio", "consumed_steps", "progress"]) {
      screen.getByRole("button", { name }).click();
      await waitFor(() =>
        expect(container.querySelectorAll("polyline").length).toBe(2),
      );
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

// `bad-instrument`'s chain is 2 lines long and ends on `headOf("r0bad", 2)`,
// so a verdict carrying that head describes exactly the bytes the pane parses
// and one carrying anything else does not.
const RUN = `${R0}/bad-instrument`;
const RUN_FILE = `${RUN}/metrics.jsonl`;
const RUN_VERDICT = `${RUN}/metrics.verdict.json`;
const RUN_HEAD = headOf("r0bad", 2);

describe("MetricsPane — verdict badge (RES-18)", () => {
  it("says chain ok, without claiming the numbers are trustworthy", async () => {
    verdictFiles.set(RUN_VERDICT, verdictJson("ok", 2, RUN_HEAD));
    viewerJson = JSON.stringify({ runs: [RUN] });
    render(createElement(MetricsPane));

    await waitFor(() =>
      expect(badge()).toHaveAttribute("data-state", "verified"),
    );
    expect(badge()).toHaveTextContent("chain ok");
    // The qualifier is the point of the badge: a green chip that stopped
    // there would be read as "these numbers are real".
    const title = badge().getAttribute("title") ?? "";
    expect(title).toContain(
      "not proof the numbers are authentic or meaningful",
    );
    // Runnable as printed: rooted where the pane is actually reading.
    expect(title).toContain(
      `python -m turing.research.loop.verify results/${RUN}`,
    );
  });

  it("reads unverified when the loop left no verdict beside the run", async () => {
    viewerJson = JSON.stringify({ runs: [RUN] });
    render(createElement(MetricsPane));

    await waitFor(() =>
      expect(badge()).toHaveAttribute("data-state", "unverified"),
    );
  });

  it("reads stale when the verdict describes a different number of lines", async () => {
    verdictFiles.set(RUN_VERDICT, verdictJson("ok", 1, digestOf("r0bad", 0)));
    viewerJson = JSON.stringify({ runs: [RUN] });
    render(createElement(MetricsPane));

    await waitFor(() => expect(badge()).toHaveAttribute("data-state", "stale"));
  });

  it("reads stale when the head matches nothing this pane has parsed", async () => {
    // Same line count, different bytes: a re-drive that happened to produce a
    // log of the same length is a different log, and only the digest says so.
    verdictFiles.set(
      RUN_VERDICT,
      verdictJson("ok", 2, headOf("some-other-run", 2)),
    );
    viewerJson = JSON.stringify({ runs: [RUN] });
    render(createElement(MetricsPane));

    await waitFor(() => expect(badge()).toHaveAttribute("data-state", "stale"));
  });

  it("reads failed when the loop's own check failed", async () => {
    verdictFiles.set(RUN_VERDICT, verdictJson("failed", 2, RUN_HEAD));
    viewerJson = JSON.stringify({ runs: [RUN] });
    render(createElement(MetricsPane));

    await waitFor(() =>
      expect(badge()).toHaveAttribute("data-state", "failed"),
    );
  });

  it("picks the verdict up when the loop writes it mid-session", async () => {
    viewerJson = JSON.stringify({ runs: [RUN] });
    render(createElement(MetricsPane));
    await waitFor(() =>
      expect(badge()).toHaveAttribute("data-state", "unverified"),
    );

    // The attempt ends: `write_attempt_verdict` lands the file and the
    // watcher reports it. The operator must not have to reopen the pane.
    verdictFiles.set(RUN_VERDICT, verdictJson("ok", 2, RUN_HEAD));
    emitFsChange(RUN_VERDICT);
    await waitFor(() =>
      expect(badge()).toHaveAttribute("data-state", "verified"),
    );
  });

  it("re-reads the verdict when it is rewritten", async () => {
    verdictFiles.set(RUN_VERDICT, verdictJson("ok", 2, RUN_HEAD));
    viewerJson = JSON.stringify({ runs: [RUN] });
    render(createElement(MetricsPane));
    await waitFor(() =>
      expect(badge()).toHaveAttribute("data-state", "verified"),
    );

    verdictFiles.set(RUN_VERDICT, verdictJson("failed", 2, RUN_HEAD));
    emitFsChange(RUN_VERDICT);
    await waitFor(() =>
      expect(badge()).toHaveAttribute("data-state", "failed"),
    );
  });

  it("gives each pinned run its own badge, named in its own visible text", async () => {
    // One badge for two runs would attribute one run's verdict to the other —
    // the same lie the ETA strip's run label exists to prevent. And two chips
    // reading the identical `✓ chain ok`, distinguished only by an attribute
    // no operator can see, is that same lie with an extra step: the useful
    // case is one red chip in a stack, and "which run?" must be answerable
    // without a mouse.
    verdictFiles.set(RUN_VERDICT, verdictJson("ok", 2, RUN_HEAD));
    verdictFiles.set(
      `${R1}/flat-baseline/metrics.verdict.json`,
      verdictJson("failed", 3, headOf("r1flat", 3)),
    );
    viewerJson = JSON.stringify({ runs: [RUN, `${R1}/flat-baseline`] });
    render(createElement(MetricsPane));

    await waitFor(() =>
      expect(screen.getAllByTestId("metrics-verdict").length).toBe(2),
    );
    const chips = screen.getAllByTestId("metrics-verdict");
    const states = chips.map((b) => [
      b.getAttribute("data-run"),
      b.getAttribute("data-state"),
    ]);
    expect(states).toContainEqual([RUN, "verified"]);
    expect(states).toContainEqual([`${R1}/flat-baseline`, "failed"]);

    const texts = chips.map((b) => b.textContent ?? "");
    expect(texts[0]).not.toBe(texts[1]);
    expect(texts.join("|")).toContain("bad-instrument");
    expect(texts.join("|")).toContain("flat-baseline");
    // And the red chip names ITS run in its own text — the case this exists for.
    const failed = chips.find(
      (b) => b.getAttribute("data-state") === "failed",
    )!;
    expect(failed.textContent).toContain("flat-baseline");
    expect(failed.textContent).not.toContain("bad-instrument");
  });

  it("is reachable and describable without a mouse", async () => {
    verdictFiles.set(RUN_VERDICT, verdictJson("ok", 2, RUN_HEAD));
    viewerJson = JSON.stringify({ runs: [RUN] });
    render(createElement(MetricsPane));

    await waitFor(() =>
      expect(badge()).toHaveAttribute("data-state", "verified"),
    );
    // A `title` on a `<span>` reaches a mouse and nothing else; the qualifier
    // is the part of this badge that must not be mouse-only.
    expect(badge().tagName).toBe("BUTTON");
    const describedBy = badge().getAttribute("aria-describedby");
    expect(describedBy).toBeTruthy();
    expect(document.getElementById(describedBy!)?.textContent).toContain(
      "not proof the numbers are authentic or meaningful",
    );
  });

  it("caps the badge column the way the run listbox beside it is capped", async () => {
    // One chip per charted run, and `.viewer.json` can name any number of
    // them; an uncapped column pushes the chart off the bottom of the pane.
    viewerJson = JSON.stringify({
      runs: [RUN, `${R0}/cuda/matmul-speedup`, `${R1}/flat-baseline`],
    });
    render(createElement(MetricsPane));

    await waitFor(() =>
      expect(screen.getAllByTestId("metrics-verdict").length).toBe(3),
    );
    const column = screen.getAllByTestId("metrics-verdict")[0].parentElement!;
    expect(column.className).toMatch(/max-h-/);
    expect(column.className).toMatch(/overflow-/);
  });

  it("says it is still reading, rather than that nothing was ever checked", async () => {
    verdictFiles.set(RUN_VERDICT, verdictJson("ok", 2, RUN_HEAD));
    viewerJson = JSON.stringify({ runs: [RUN] });

    // Hold the verdict read open, the way a cold read on a network share does.
    let release!: () => void;
    const held = new Promise<void>((r) => (release = r));
    const base = invMock.getMockImplementation()!;
    invMock.mockImplementation(
      async (cmd: string, args: Record<string, unknown> = {}) => {
        if (cmd === "fs_read_text" && args.rel === RUN_VERDICT) await held;
        return base(cmd, args);
      },
    );

    render(createElement(MetricsPane));
    await waitFor(() => expect(badge()).toBeInTheDocument());
    const title = badge().getAttribute("title") ?? "";
    expect(title).toContain("reading the verdict");
    expect(title).not.toContain("never recorded a check here");

    release();
    await waitFor(() =>
      expect(badge()).toHaveAttribute("data-state", "verified"),
    );
  });

  it("says nothing was ever checked once the read comes back empty, not that it is still reading", async () => {
    // `undefined` ("not read yet") and `null` ("read, nothing there") share a
    // colour and not a sentence. The first read has to land even when it lands
    // on nothing — a `sameVerdict` that called the two equal would leave the
    // badge saying "reading…" forever on a run with no verdict.
    viewerJson = JSON.stringify({ runs: [RUN] });
    let release!: () => void;
    const held = new Promise<void>((r) => (release = r));
    const base = invMock.getMockImplementation()!;
    invMock.mockImplementation(
      async (cmd: string, args: Record<string, unknown> = {}) => {
        if (cmd === "fs_read_text" && args.rel === RUN_VERDICT) await held;
        return base(cmd, args);
      },
    );

    render(createElement(MetricsPane));
    await waitFor(() => expect(badge()).toBeInTheDocument());
    expect(badge().getAttribute("title")).toContain("reading the verdict");

    release();
    await waitFor(() =>
      expect(badge().getAttribute("title")).toContain(
        "never recorded a check here",
      ),
    );
    expect(badge().getAttribute("title")).not.toContain("reading the verdict");
    expect(badge()).toHaveAttribute("data-state", "unverified");
  });
});

describe("MetricsPane — a re-drive rotates the run out from under the badge", () => {
  /**
   * A re-drive moves the metrics trio and the verdict together into
   * `prior-N/`, and the loop then opens a brand new `metrics.jsonl` in their
   * place — a new file, hence a new inode, at the same path.
   */
  function rotate(nextChain: string) {
    for (const [name, store] of [
      ["metrics.jsonl", contents],
      ["metrics.verdict.json", verdictFiles],
    ] as const) {
      const body = store.get(`${RUN}/${name}`);
      if (body !== undefined) {
        store.set(`${RUN}/prior-1/${name}`, body);
        store.delete(`${RUN}/${name}`);
      }
    }
    replaceFile(RUN_FILE, nextChain);
  }

  it("stops claiming chain ok over a directory the verdict has left", async () => {
    verdictFiles.set(RUN_VERDICT, verdictJson("ok", 2, RUN_HEAD));
    viewerJson = JSON.stringify({ runs: [RUN] });
    render(createElement(MetricsPane));
    await waitFor(() =>
      expect(badge()).toHaveAttribute("data-state", "verified"),
    );

    rotate("");
    // What the watcher actually reports: creations at the destinations and at
    // the new empty run file, and NOTHING for the old verdict path, whose
    // `is_file()` is false by the time the handler runs.
    emitFsChange(`${RUN}/prior-1/metrics.jsonl`);
    emitFsChange(`${RUN}/prior-1/metrics.verdict.json`);
    emitFsChange(RUN_VERDICT);
    emitFsChange(RUN_FILE);

    await waitFor(() =>
      expect(badge()).toHaveAttribute("data-state", "unverified"),
    );
  });

  it("drops the superseded generation's points instead of stapling new ones to them", async () => {
    verdictFiles.set(RUN_VERDICT, verdictJson("ok", 2, RUN_HEAD));
    viewerJson = JSON.stringify({ runs: [RUN] });
    render(createElement(MetricsPane));
    await waitFor(() =>
      expect(badge()).toHaveAttribute("data-state", "verified"),
    );
    expect(screen.getByTestId("metrics-eta")).toHaveTextContent("last step: 2");
    expect(
      screen.getByRole("button", { name: "progress" }),
    ).toBeInTheDocument();

    // The new attempt's first line lands, and it measures something else —
    // a different solver on the same problem id. The new file is shorter than
    // the pane's offset (`fs_tail` restarts at 0) AND a different inode;
    // either alone must reset the run: appending the chunk would chart a
    // rotated-away attempt with a new one stapled to its end, and would keep
    // offering the old attempt's series as tabs.
    rotate(
      `${JSON.stringify({ step: 1, total_steps: 4, restarted: 1, _chain: "0:redrive" })}\n`,
    );
    emitFsChange(RUN_FILE);

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "restarted" }),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByRole("button", { name: "progress" })).toBeNull();
    expect(screen.getByTestId("metrics-eta")).toHaveTextContent("last step: 1");
    expect(badge()).toHaveAttribute("data-state", "unverified");
  });

  it("resets on a truncate in place too — same inode, shorter file", async () => {
    verdictFiles.set(RUN_VERDICT, verdictJson("ok", 2, RUN_HEAD));
    viewerJson = JSON.stringify({ runs: [RUN] });
    render(createElement(MetricsPane));
    await waitFor(() =>
      expect(badge()).toHaveAttribute("data-state", "verified"),
    );

    // Not a re-drive: something rewrote the file in place with less in it.
    // The identity is unchanged, so this is the case `restarted` exists for.
    contents.set(
      RUN_FILE,
      `${JSON.stringify({ step: 1, total_steps: 4, rewritten: 1, _chain: "0:again" })}\n`,
    );
    emitFsChange(RUN_FILE);

    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "rewritten" }),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByRole("button", { name: "progress" })).toBeNull();
    expect(screen.getByTestId("metrics-eta")).toHaveTextContent("last step: 1");
    // The old verdict is still beside the file, and it is now about bytes
    // that are no longer there.
    expect(badge()).toHaveAttribute("data-state", "stale");
  });

  it("keeps what it holds when a verdict event re-tails a run file that has been deleted", async () => {
    // The verdict branch re-tails the run; if that tail rejected on a missing
    // file the failure would surface as an unhandled rejection, not as a
    // badge — and the pane must neither blank the chart nor stop reading.
    verdictFiles.set(RUN_VERDICT, verdictJson("ok", 2, RUN_HEAD));
    viewerJson = JSON.stringify({ runs: [RUN] });
    render(createElement(MetricsPane));
    await waitFor(() =>
      expect(badge()).toHaveAttribute("data-state", "verified"),
    );
    const tailsBefore = tailedPaths().filter((p) => p === RUN_FILE).length;

    contents.delete(RUN_FILE);
    verdictFiles.set(RUN_VERDICT, verdictJson("failed", 2, RUN_HEAD));
    emitFsChange(RUN_VERDICT);

    await waitFor(() =>
      expect(badge()).toHaveAttribute("data-state", "failed"),
    );
    // The re-tail was issued and rejected; the points survived it.
    await waitFor(() =>
      expect(tailedPaths().filter((p) => p === RUN_FILE).length).toBe(
        tailsBefore + 1,
      ),
    );
    expect(screen.getByTestId("metrics-eta")).toHaveTextContent("last step: 2");
    expect(
      screen.getByRole("button", { name: "progress" }),
    ).toBeInTheDocument();
  });
});

describe("MetricsPane — a replacement file at least as long as the held offset", () => {
  // The old and the new generation are built with the same digit counts, so
  // the new file's first lines end at exactly the byte offsets the old ones
  // did: no length check can see the change. Only the identity can.
  const OLD_SPEEDUP = 2.5; // 2.5, 2.6, 2.7
  const NEW_SPEEDUP = 7.5; // 7.5, 7.6, 7.7 — same lengths, different numbers

  function redrive(nextChain: string) {
    for (const [name, store] of [
      ["metrics.jsonl", contents],
      ["metrics.verdict.json", verdictFiles],
    ] as const) {
      const body = store.get(`${RUN}/${name}`);
      if (body !== undefined) {
        store.set(`${RUN}/prior-1/${name}`, body);
        store.delete(`${RUN}/${name}`);
      }
    }
    replaceFile(RUN_FILE, nextChain);
  }

  it("A1: a re-drive whose first line is the same length as the old file is a new run, not a continuation", async () => {
    contents.set(RUN_FILE, chain(1, 0.03, OLD_SPEEDUP, "r0bad"));
    verdictFiles.set(RUN_VERDICT, verdictJson("ok", 1, headOf("r0bad", 1)));
    viewerJson = JSON.stringify({ runs: [RUN] });
    render(createElement(MetricsPane));
    await waitFor(() =>
      expect(badge()).toHaveAttribute("data-state", "verified"),
    );
    const oldLen = contents.get(RUN_FILE)!.length;

    // Trio and verdict move into `prior-1/`; the new writer's first line lands
    // and it is byte-for-byte as long as the old file.
    const gen2 = chain(1, 0.03, NEW_SPEEDUP, "redrive");
    expect(gen2.length).toBe(oldLen);
    redrive(gen2);
    emitFsChange(RUN_FILE);
    await waitFor(() =>
      expect(badge()).toHaveAttribute("data-state", "unverified"),
    );

    // Steps 2 and 3 of the new attempt, then the loop's verdict for it.
    contents.set(RUN_FILE, chain(3, 0.03, NEW_SPEEDUP, "redrive"));
    emitFsChange(RUN_FILE);
    verdictFiles.set(RUN_VERDICT, verdictJson("ok", 3, headOf("redrive", 3)));
    emitFsChange(RUN_VERDICT);
    await waitFor(() =>
      expect(badge()).toHaveAttribute("data-state", "verified"),
    );
    expect(screen.getByTestId("metrics-eta")).toHaveTextContent("last step: 3");

    // What is under that green badge must be the new generation ALONE. With
    // the old line 1 stapled in front, the speedup curve would reach down to
    // 2.5 and its axis would say so.
    screen.getByRole("button", { name: "speedup_ratio" }).click();
    await waitFor(() =>
      expect(document.querySelectorAll("polyline").length).toBe(1),
    );
    expect(
      document.querySelector("polyline")!.getAttribute("points")!.split(" ")
        .length,
    ).toBe(3);
    const ticks = yTicks();
    expect(ticks.length).toBeGreaterThan(0);
    for (const t of ticks) expect(t).toBeGreaterThan(7);
  });

  it("A2: the create and the first appends coalesce into one event for a file already longer than the old one", async () => {
    contents.set(RUN_FILE, chain(2, 0.03, OLD_SPEEDUP, "r0bad"));
    verdictFiles.set(RUN_VERDICT, verdictJson("ok", 2, headOf("r0bad", 2)));
    viewerJson = JSON.stringify({ runs: [RUN] });
    render(createElement(MetricsPane));
    await waitFor(() =>
      expect(badge()).toHaveAttribute("data-state", "verified"),
    );
    const oldLen = contents.get(RUN_FILE)!.length;

    // The new attempt's line 2 ends exactly where the old file ended, and the
    // pane hears about the file once, when it is already 3 lines long — the
    // watcher's 300 ms debounce swallowed the create and the second append.
    expect(chain(2, 0.03, NEW_SPEEDUP, "redrive").length).toBe(oldLen);
    redrive(chain(3, 0.03, NEW_SPEEDUP, "redrive"));
    emitFsChange(RUN_FILE);
    verdictFiles.set(RUN_VERDICT, verdictJson("ok", 3, headOf("redrive", 3)));
    emitFsChange(RUN_VERDICT);

    await waitFor(() =>
      expect(badge()).toHaveAttribute("data-state", "verified"),
    );
    await waitFor(() =>
      expect(screen.getByTestId("metrics-eta")).toHaveTextContent(
        "last step: 3",
      ),
    );
    screen.getByRole("button", { name: "speedup_ratio" }).click();
    await waitFor(() =>
      expect(document.querySelectorAll("polyline").length).toBe(1),
    );
    // Three points, all of them the new generation's — not old lines 1–2
    // with new line 3 stapled on, which would satisfy the line count and the
    // digest both and still be a chart of two runs.
    expect(
      document.querySelector("polyline")!.getAttribute("points")!.split(" ")
        .length,
    ).toBe(3);
    const ticks = yTicks();
    expect(ticks.length).toBeGreaterThan(0);
    for (const t of ticks) expect(t).toBeGreaterThan(7);
  });
});

describe("MetricsPane — a rewrite of the same inode, longer than what the pane holds", () => {
  it("B1: the seam check catches a held offset that lands inside a line of the rewrite", async () => {
    contents.set(RUN_FILE, chain(2, 0.03, 2.5, "r0bad"));
    verdictFiles.set(RUN_VERDICT, verdictJson("ok", 2, headOf("r0bad", 2)));
    viewerJson = JSON.stringify({ runs: [RUN] });
    render(createElement(MetricsPane));
    await waitFor(() =>
      expect(badge()).toHaveAttribute("data-state", "verified"),
    );
    const oldLen = contents.get(RUN_FILE)!.length;

    // `cp other/metrics.jsonl metrics.jsonl`: truncate + write, SAME inode,
    // and longer than what the pane holds — no identity or length check can
    // see it. Its lines are one byte longer each (10.5 vs 2.5), so the held
    // offset lands two bytes before the end of the rewrite's line 2 and the
    // chunk `fs_tail` returns begins `}\n`: not a whole line, not an object.
    const rewrite = chain(3, 0.03, 10.5, "rewrite");
    expect(rewrite.slice(oldLen, oldLen + 2)).toBe("}\n");
    contents.set(RUN_FILE, rewrite);
    emitFsChange(RUN_FILE);
    verdictFiles.set(RUN_VERDICT, verdictJson("ok", 3, headOf("rewrite", 3)));
    emitFsChange(RUN_VERDICT);
    await waitFor(() =>
      expect(badge()).toHaveAttribute("data-state", "verified"),
    );

    // The rewrite alone — not old lines 1–2 with the rewrite's line 3 stapled
    // on under a green badge (which the digest and line count both allow).
    screen.getByRole("button", { name: "speedup_ratio" }).click();
    await waitFor(() =>
      expect(document.querySelectorAll("polyline").length).toBe(1),
    );
    expect(
      document.querySelector("polyline")!.getAttribute("points")!.split(" ")
        .length,
    ).toBe(3);
    const ticks = yTicks();
    expect(ticks.length).toBeGreaterThan(0);
    for (const t of ticks) expect(t).toBeGreaterThan(9);
  });

  it("counts `dev` in identity: the same inode number on another device is another file", async () => {
    contents.set(RUN_FILE, chain(2, 0.03, 2.5, "r0bad"));
    viewerJson = JSON.stringify({ runs: [RUN] });
    render(createElement(MetricsPane));
    await waitFor(() =>
      expect(screen.getByTestId("metrics-eta")).toHaveTextContent(
        "last step: 2",
      ),
    );
    const base = invMock.getMockImplementation()!;
    invMock.mockImplementation(
      async (cmd: string, args: Record<string, unknown> = {}) => {
        const r = await base(cmd, args);
        return cmd === "fs_tail" ? { ...r, dev: 2 } : r;
      },
    );
    // Same length, same `ino`, different `dev`: with `dev` ignored this reads
    // as "nothing new" and the old numbers stay on the chart.
    contents.set(RUN_FILE, chain(2, 0.03, 7.5, "other"));
    emitFsChange(RUN_FILE);
    screen.getByRole("button", { name: "speedup_ratio" }).click();
    await waitFor(() =>
      expect(yTicks().every((t) => t > 7) && yTicks().length > 0).toBe(true),
    );
  });
});

describe("MetricsPane — two events for one run a few milliseconds apart", () => {
  it("A3: the last append's event and the verdict's event do not wipe the run", async () => {
    // Attempt running: 2 lines charted, no verdict yet.
    viewerJson = JSON.stringify({ runs: [RUN] });
    render(createElement(MetricsPane));
    await waitFor(() =>
      expect(badge()).toHaveAttribute("data-state", "unverified"),
    );
    expect(screen.getByTestId("metrics-eta")).toHaveTextContent("last step: 2");

    // The solver writes line 3 (its event is emitted — this solver is slower
    // than 3 Hz), the attempt ends, and the verdict lands a few ms later. Both
    // events reach the pane before the first `fs_tail` round trip resolves —
    // the ordinary end of every attempt, not a race anyone contrived.
    contents.set(RUN_FILE, chain(3, 0.03, 1.1, "r0bad"));
    emitFsChange(RUN_FILE);
    verdictFiles.set(RUN_VERDICT, verdictJson("ok", 3, headOf("r0bad", 3)));
    emitFsChange(RUN_VERDICT);

    await waitFor(() =>
      expect(badge()).toHaveAttribute("data-state", "verified"),
    );
    expect(screen.getByTestId("metrics-eta")).toHaveTextContent("last step: 3");
    screen.getByRole("button", { name: "progress" }).click();
    await waitFor(() =>
      expect(document.querySelectorAll("polyline").length).toBe(1),
    );
    // All three points, once each: the same bytes were not applied twice, and
    // the second read did not wipe the first's points down to one.
    expect(
      document.querySelector("polyline")!.getAttribute("points")!.split(" ")
        .length,
    ).toBe(3);
  });

  it("issues at most one fs_tail at a time per run, and one follow-up for a burst", async () => {
    viewerJson = JSON.stringify({ runs: [RUN] });
    render(createElement(MetricsPane));
    await waitFor(() =>
      expect(badge()).toHaveAttribute("data-state", "unverified"),
    );
    const before = tailedPaths().filter((p) => p === RUN_FILE).length;

    // Hold every read open, fire a burst of events, then release.
    let release!: () => void;
    const held = new Promise<void>((r) => (release = r));
    const base = invMock.getMockImplementation()!;
    invMock.mockImplementation(
      async (cmd: string, args: Record<string, unknown> = {}) => {
        if (cmd === "fs_tail" && args.rel === RUN_FILE) await held;
        return base(cmd, args);
      },
    );
    contents.set(RUN_FILE, chain(3, 0.03, 1.1, "r0bad"));
    emitFsChange(RUN_FILE);
    emitFsChange(RUN_FILE);
    verdictFiles.set(RUN_VERDICT, verdictJson("ok", 3, headOf("r0bad", 3)));
    emitFsChange(RUN_VERDICT);
    emitFsChange(RUN_VERDICT);
    // Only the first request went out; the rest coalesced behind it.
    expect(tailedPaths().filter((p) => p === RUN_FILE).length).toBe(before + 1);

    release();
    await waitFor(() =>
      expect(badge()).toHaveAttribute("data-state", "verified"),
    );
    // …into exactly one follow-up read once it landed.
    await waitFor(() =>
      expect(tailedPaths().filter((p) => p === RUN_FILE).length).toBe(
        before + 2,
      ),
    );
    expect(screen.getByTestId("metrics-eta")).toHaveTextContent("last step: 3");
  });
});

describe("MetricsPane — the bytes at the end of the file", () => {
  it("charts a line split across two reads once, and is not stuck stale after the mid-write read", async () => {
    viewerJson = JSON.stringify({ runs: [RUN] });
    render(createElement(MetricsPane));
    await waitFor(() =>
      expect(screen.getByTestId("metrics-eta")).toHaveTextContent(
        "last step: 2",
      ),
    );

    // The watcher fires while the writer is mid-line: the file ends in half
    // of line 3. `fs_tail` holds the fragment back, so the pane still holds
    // exactly two lines and its offset stops before the fragment.
    const line3 = chainLine(2, 0.03, 1.1, "r0bad");
    const twoLines = chain(2, 0.03, 1.1, "r0bad");
    contents.set(RUN_FILE, `${twoLines}${line3.slice(0, 20)}`);
    emitFsChange(RUN_FILE);
    await waitFor(() =>
      expect(
        tailedPaths().filter((p) => p === RUN_FILE).length,
      ).toBeGreaterThanOrEqual(2),
    );
    expect(screen.getByTestId("metrics-eta")).toHaveTextContent("last step: 2");

    // The writer finishes the line and the attempt ends. Line 3 arrives whole
    // — charted once — and the verdict for three lines binds.
    contents.set(RUN_FILE, `${twoLines}${line3}\n`);
    emitFsChange(RUN_FILE);
    verdictFiles.set(RUN_VERDICT, verdictJson("ok", 3, headOf("r0bad", 3)));
    emitFsChange(RUN_VERDICT);
    await waitFor(() =>
      expect(badge()).toHaveAttribute("data-state", "verified"),
    );
    expect(screen.getByTestId("metrics-eta")).toHaveTextContent("last step: 3");
    screen.getByRole("button", { name: "progress" }).click();
    await waitFor(() =>
      expect(document.querySelectorAll("polyline").length).toBe(1),
    );
    expect(
      document.querySelector("polyline")!.getAttribute("points")!.split(" ")
        .length,
    ).toBe(3);
  });

  it("reads stale, not chain ok, once an unparseable line follows the checked one", async () => {
    verdictFiles.set(RUN_VERDICT, verdictJson("ok", 2, RUN_HEAD));
    viewerJson = JSON.stringify({ runs: [RUN] });
    render(createElement(MetricsPane));
    await waitFor(() =>
      expect(badge()).toHaveAttribute("data-state", "verified"),
    );

    // Garbage appended after the verdict. `verify` on disk would say FAILED;
    // the pane cannot know that, but it must not keep saying chain ok over a
    // last line it cannot even parse. (`failed` is the loop's word, not the
    // pane's, so this reads `stale`.)
    contents.set(RUN_FILE, `${chain(2, 0.03, 1.1, "r0bad")}not json at all\n`);
    emitFsChange(RUN_FILE);
    await waitFor(() => expect(badge()).toHaveAttribute("data-state", "stale"));
    // The two good lines are still charted; only the claim about them changed.
    expect(screen.getByTestId("metrics-eta")).toHaveTextContent("last step: 2");
  });

  it("E1: reads stale once a blank line follows the checked one — integrity.py calls that malformed", async () => {
    verdictFiles.set(RUN_VERDICT, verdictJson("ok", 2, RUN_HEAD));
    viewerJson = JSON.stringify({ runs: [RUN] });
    render(createElement(MetricsPane));
    await waitFor(() =>
      expect(badge()).toHaveAttribute("data-state", "verified"),
    );

    // A lone `\n` after the verdict. The writer never emits an interior blank
    // line and `verify` on disk treats one as a malformed record; the pane
    // must not keep saying chain ok over bytes the loop's own check fails.
    contents.set(RUN_FILE, `${chain(2, 0.03, 1.1, "r0bad")}\n`);
    emitFsChange(RUN_FILE);
    await waitFor(() => expect(badge()).toHaveAttribute("data-state", "stale"));
    expect(screen.getByTestId("metrics-eta")).toHaveTextContent("last step: 2");
  });
});

describe("MetricsPane — flywheel round clicks vs .viewer.json (#390 item 3)", () => {
  // What the flywheel's [metrics] link publishes for a round of `loop-probe`,
  // and the run selection that round must resolve to: every live attempt of
  // the round — the nested problem id included — and never the rotated
  // `prior-1` chain, which is a superseded generation the click did not ask
  // for. The precedence rule under test is "last action wins": the pane holds
  // one request slot, so a click, a `.viewer.json` delivery, and a second
  // click each replace whatever held the slot before them.
  const ROUND_0_RUNS = [`${R0}/cuda/matmul-speedup`, `${R0}/bad-instrument`];

  function clickRound(round: number) {
    act(() => publishMetricsTarget({ loop: LOOP, round }));
  }

  /** The watcher reporting a `.viewer.json` save, exactly as the pane hears it. */
  function emitViewerChange() {
    for (const handler of fsChangeHandlers)
      handler({ root: "results", rel_path: ".viewer.json" });
  }

  it("applies a round click as the run selection", async () => {
    render(createElement(MetricsPane));
    await waitFor(() => expect(runOptions().length).toBe(RUN_FILES.length + 1));

    clickRound(0);
    await waitFor(() =>
      expect(selectedRunLabels().sort()).toEqual([...ROUND_0_RUNS].sort()),
    );
    expect(selectedRunLabels()).not.toContain(
      `${R0}/cuda/matmul-speedup/prior-1`,
    );
    // The runs, not the series: the operator's tab choice is not the click's
    // to change.
    expect(selectedRunLabels().some((l) => l.startsWith("auto"))).toBe(false);

    // And the click stayed in-process: the app never writes `.viewer.json`
    // (or anything else) to express it — the file is the agent → app
    // direction only. Every command this pane ever issued is a read.
    for (const [cmd, args] of invMock.mock.calls) {
      expect(["fs_watch", "fs_list", "fs_tail", "fs_read_text"]).toContain(cmd);
      if (
        (args as Record<string, unknown> | undefined)?.rel === ".viewer.json"
      ) {
        expect(cmd).toBe("fs_read_text");
      }
    }
  });

  it("lets a later .viewer.json change supersede the click", async () => {
    render(createElement(MetricsPane));
    await waitFor(() => expect(runOptions().length).toBe(RUN_FILES.length + 1));
    clickRound(0);
    await waitFor(() =>
      expect(selectedRunLabels().sort()).toEqual([...ROUND_0_RUNS].sort()),
    );

    // An agent writes the file after the click. The agent channel is a later
    // action, so it wins — the app never wrote the file, so nothing the click
    // did can have stomped what the agent said.
    viewerJson = JSON.stringify({ runs: [`${R1}/flat-baseline`] });
    emitViewerChange();
    await waitFor(() =>
      expect(selectedRunLabels()).toEqual([`${R1}/flat-baseline`]),
    );
  });

  it("lets a second click supersede the .viewer.json request again", async () => {
    render(createElement(MetricsPane));
    await waitFor(() => expect(runOptions().length).toBe(RUN_FILES.length + 1));
    clickRound(0);
    await waitFor(() =>
      expect(selectedRunLabels().sort()).toEqual([...ROUND_0_RUNS].sort()),
    );
    viewerJson = JSON.stringify({ runs: [`${R1}/flat-baseline`] });
    emitViewerChange();
    await waitFor(() =>
      expect(selectedRunLabels()).toEqual([`${R1}/flat-baseline`]),
    );

    clickRound(1);
    await waitFor(() =>
      expect(selectedRunLabels().sort()).toEqual(
        [`${R1}/cuda/matmul-speedup`, `${R1}/flat-baseline`].sort(),
      ),
    );
  });

  it("degrades an unmappable round to keeping the current chart, not to a blank pane", async () => {
    viewerJson = JSON.stringify({ runs: [`${R1}/flat-baseline`] });
    render(createElement(MetricsPane));
    await waitFor(() =>
      expect(selectedRunLabels()).toEqual([`${R1}/flat-baseline`]),
    );

    // A round with no metrics on this machine. Nothing matches, so nothing is
    // honoured: the selection, the series tabs, and the chart all stay.
    clickRound(42);
    await act(async () => {});
    expect(selectedRunLabels()).toEqual([`${R1}/flat-baseline`]);
    expect(
      screen.getByRole("button", { name: "progress" }),
    ).toBeInTheDocument();
    expect(screen.queryByText("no metrics yet")).toBeNull();
  });

  it("does not let a .viewer.json delivery without runs evict a click", async () => {
    render(createElement(MetricsPane));
    await waitFor(() => expect(runOptions().length).toBe(RUN_FILES.length + 1));
    clickRound(0);
    await waitFor(() =>
      expect(selectedRunLabels().sort()).toEqual([...ROUND_0_RUNS].sort()),
    );

    // A titles-only save asks nothing about runs; the click's selection stays.
    viewerJson = JSON.stringify({
      titles: { progress: "progress toward target" },
    });
    emitViewerChange();
    await act(async () => {});
    expect(selectedRunLabels().sort()).toEqual([...ROUND_0_RUNS].sort());
  });

  it("honours a click on a round whose first metrics file lands only after mount", async () => {
    render(createElement(MetricsPane));
    await waitFor(() => expect(runOptions().length).toBe(RUN_FILES.length + 1));

    // Round 2 is live but its solver has not taken its first step: no
    // `metrics.jsonl` exists yet. This is the flagship case — the [metrics]
    // link is deliberately offered on rounds with no round.json yet.
    clickRound(2);
    await act(async () => {});
    expect(selectedRunLabels()).toEqual([AUTO_R1]);

    // The solver's first append creates the file; the watcher reports it.
    // The run list was walked once at mount, so honouring the click now
    // requires the pane to notice a run file it has never listed.
    const newRun = `${LOOP}/round-02/attempts/fresh-problem/metrics.jsonl`;
    contents.set(newRun, chain(1, 0.05, 1.2, "r2fresh"));
    emitFsChange(newRun);

    await waitFor(() =>
      expect(selectedRunLabels()).toEqual([
        `${LOOP}/round-02/attempts/fresh-problem`,
      ]),
    );
  });

  it("auto-follows a run file created after mount", async () => {
    render(createElement(MetricsPane));
    await waitFor(() => expect(runOptions().length).toBe(RUN_FILES.length + 1));
    // Default selection is `auto`: the pane charts the newest listed run.
    await waitFor(() =>
      expect(screen.getByTestId("metrics-eta")).toHaveTextContent(
        `${R1}/cuda/matmul-speedup`,
      ),
    );

    // A new round's first run file lands — newest mtime, so `fs_list` would
    // report it first. Auto-follow must move to it without a remount.
    const newRun = `${LOOP}/round-02/attempts/fresh-problem/metrics.jsonl`;
    contents = new Map([[newRun, chain(2, 0.05, 1.2, "r2fresh")], ...contents]);
    emitFsChange(newRun);

    await waitFor(() =>
      expect(screen.getByTestId("metrics-eta")).toHaveTextContent(
        `${LOOP}/round-02/attempts/fresh-problem`,
      ),
    );
  });

  it("lets a click during an in-flight .viewer.json read win over that read (last action wins)", async () => {
    render(createElement(MetricsPane));
    await waitFor(() => expect(runOptions().length).toBe(RUN_FILES.length + 1));

    // An agent saves the file and the read is slow — RoundRunner rewrites
    // `.viewer.json` at every round boundary, which is exactly when an
    // operator is looking at the flywheel and clicking.
    viewerJson = JSON.stringify({ runs: [`${R1}/flat-baseline`] });
    let release!: () => void;
    const held = new Promise<void>((r) => (release = r));
    const base = invMock.getMockImplementation()!;
    invMock.mockImplementation(
      async (cmd: string, args: Record<string, unknown> = {}) => {
        if (cmd === "fs_read_text" && args.rel === ".viewer.json") await held;
        return base(cmd, args);
      },
    );
    emitViewerChange();

    // The operator clicks a round while that read is still in flight — the
    // click is the LATER action.
    clickRound(0);
    await waitFor(() =>
      expect(selectedRunLabels().sort()).toEqual([...ROUND_0_RUNS].sort()),
    );

    // The stale read resolves. It must not clobber the click.
    release();
    await act(async () => {});
    await act(async () => {});
    expect(selectedRunLabels().sort()).toEqual([...ROUND_0_RUNS].sort());
  });

  it("clears a pending round click when the operator uses the listbox themselves", async () => {
    render(createElement(MetricsPane));
    await waitFor(() => expect(runOptions().length).toBe(RUN_FILES.length + 1));

    // Round 2 has no files yet, so the click stays pending.
    clickRound(2);
    await act(async () => {});
    expect(selectedRunLabels()).toEqual([AUTO_R1]);

    // The operator picks a run by hand. That is now the last action.
    const flat = runOptions().find(
      (o) => o.textContent === `${R1}/flat-baseline`,
    )!;
    act(() => flat.click());
    await waitFor(() =>
      expect(selectedRunLabels()).toContain(`${R1}/flat-baseline`),
    );

    // Round 2's first file lands later. The superseded click must not fire
    // now and rearrange a chart the operator chose by hand.
    const newRun = `${LOOP}/round-02/attempts/fresh-problem/metrics.jsonl`;
    contents.set(newRun, chain(1, 0.05, 1.2, "r2fresh"));
    emitFsChange(newRun);
    await waitFor(() => expect(runOptions().length).toBe(RUN_FILES.length + 2));
    expect(selectedRunLabels().sort()).toEqual(
      // (The fake `fs_list` appends new files, so auto still names R1 here.)
      [AUTO_R1, `${R1}/flat-baseline`].sort(),
    );
  });
});

describe("MetricsPane — concurrent run-list walks", () => {
  it("discards a stale walk that resolves after a newer one, instead of regressing the list", async () => {
    render(createElement(MetricsPane));
    await waitFor(() => expect(runOptions().length).toBe(RUN_FILES.length + 1));

    // Hold the NEXT fs_list open, snapshotting its result at call time — a
    // slow walk answers with what the directory held when it started, not
    // with what it holds when the response finally lands.
    const base = invMock.getMockImplementation()!;
    let releaseStale: (() => void) | null = null;
    let heldOne = false;
    invMock.mockImplementation(
      async (cmd: string, args: Record<string, unknown> = {}) => {
        if (cmd === "fs_list" && !heldOne) {
          heldOne = true;
          const snapshot = await base(cmd, args);
          await new Promise<void>((r) => (releaseStale = r));
          return snapshot;
        }
        return base(cmd, args);
      },
    );

    // Walk 1: run file X lands; the watcher re-walks; the response hangs
    // holding a snapshot that contains X but not Y.
    const runX = `${LOOP}/round-02/attempts/problem-x/metrics.jsonl`;
    contents.set(runX, chain(1, 0.05, 1.2, "r2x"));
    emitFsChange(runX);
    await waitFor(() => expect(releaseStale).not.toBeNull());

    // Walk 2: run file Y lands while walk 1 is still in flight; this walk
    // resolves immediately and the list now names both new files.
    const runY = `${LOOP}/round-02/attempts/problem-y/metrics.jsonl`;
    contents.set(runY, chain(1, 0.05, 1.3, "r2y"));
    emitFsChange(runY);
    await waitFor(() =>
      expect(runOptions().map((o) => o.textContent)).toContain(
        `${LOOP}/round-02/attempts/problem-y`,
      ),
    );

    // Walk 1 resolves LAST, carrying its stale snapshot. Applying it would
    // transiently drop the newest run from the list (and from auto-follow's
    // and a pending [metrics] click's view of the world).
    await act(async () => releaseStale!());
    await act(async () => {});
    const labels = runOptions().map((o) => o.textContent);
    expect(labels).toContain(`${LOOP}/round-02/attempts/problem-y`);
    expect(labels).toContain(`${LOOP}/round-02/attempts/problem-x`);
  });
});

describe("MetricsPane — a missed run event must not pin the badge amber", () => {
  it("catches up on the run when the loop's final verdict lands", async () => {
    viewerJson = JSON.stringify({ runs: [RUN] });
    render(createElement(MetricsPane));
    await waitFor(() =>
      expect(badge()).toHaveAttribute("data-state", "unverified"),
    );

    // The solver writes its final line, and the watcher drops the event: the
    // real handler debounces writes to one path inside 300 ms, which a solver
    // stepping faster than ~3 Hz hits routinely.
    contents.set(RUN_FILE, chain(3, 0.03, 1.1, "r0bad"));

    // The attempt ends. This event is the only one the pane will ever get for
    // this directory, so it has to carry the pane the rest of the way.
    verdictFiles.set(RUN_VERDICT, verdictJson("ok", 3, headOf("r0bad", 3)));
    emitFsChange(RUN_VERDICT);

    await waitFor(() =>
      expect(badge()).toHaveAttribute("data-state", "verified"),
    );
  });
});


describe("MetricsPane — missing native notifications", () => {
  it("reconciles a silent final append and stops polling after unmount", async () => {
    const path = `${R1}/cuda/matmul-speedup/metrics.jsonl`;
    const view = render(createElement(MetricsPane));
    await waitFor(() => expect(tailedPaths()).toContain(path));
    const previous = contents.get(path)!;
    const line = chainLine(20, 0.1, 3, "silent");
    contents.set(path, previous + line + "\n");
    // No fs-change event: held-open producers must still reach the consumer.
    await waitFor(() => expect(screen.getByTestId("metrics-eta").textContent).toContain("last step: 21"));
    view.unmount();
    const calls = invMock.mock.calls.length;
    await new Promise((resolve) => setTimeout(resolve, 250));
    expect(invMock.mock.calls.length).toBe(calls);
  });
});

describe("MetricsPane — active notification streams", () => {
  it("does not add watchdog reads while native updates arrive frequently", async () => {
    const path = `${R1}/cuda/matmul-speedup/metrics.jsonl`;
    vi.useFakeTimers();
    try {
      await act(async () => {
        render(createElement(MetricsPane));
        await vi.advanceTimersByTimeAsync(1);
      });
      expect(tailedPaths()).toContain(path);
      invMock.mockClear();
      for (let i = 0; i < 10; i++) {
        await act(async () => {
          for (const handler of fsChangeHandlers) handler({ root: "results", rel_path: path });
          await vi.advanceTimersByTimeAsync(20);
        });
      }
      expect(invMock.mock.calls.filter(([cmd]) => cmd === "fs_tail")).toHaveLength(10);
    } finally {
      cleanup();
      vi.useRealTimers();
    }
  });
});
