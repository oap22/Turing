// FlywheelPane's round detail, driven through the real fs commands (#390).
//
// The parser rules are covered DOM-free in roundRecord.test.ts; this file
// asserts what the operator actually reads off the screen, because both
// defects it guards were invisible at the parser level:
//
//   1. The pane painted a green "comparable to parent" for a round whose own
//      summary records `comparable_to_parent: false` — in the same box as a
//      red `all_attempts_completed`, two `refused_attempt_lost` verdicts and
//      an empty Δ column. Comparability came from recomputed eval-set hashes,
//      which answer a weaker question and agreed with none of it.
//   2. `problems[]` was never parsed, so a real problem id — `cuda/matmul-
//      speedup`, ids are free-form path components — appeared nowhere in the
//      UI as text.
//
// The fixtures below are trimmed from a real emitted `research-results/` tree.

import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const files = new Map<string, string>();
let listing: Array<{
  rel_path: string;
  is_dir: boolean;
  size: number;
  mtime_ms: number;
}> = [];

vi.mock("../desktop/tauri", () => ({
  isTauri: () => true,
  inv: async (cmd: string, args?: Record<string, unknown>) => {
    if (cmd === "fs_list") return listing;
    if (cmd === "fs_read_text") {
      const rel = String(args?.rel);
      const text = files.get(rel);
      // The real command rejects for a file that isn't there; the pane's
      // "not written yet" path depends on that, so the fake must too.
      if (text === undefined) throw new Error(`ENOENT ${rel}`);
      return text;
    }
    return undefined;
  },
  subscribe: () => ({ unsubscribe: () => {}, ready: Promise.resolve() }),
}));

// Not mocked: the pane publishes through the real module, and these tests
// listen on the same one — which is exactly how MetricsPane hears it.
import {
  __resetPaneLinkForTests,
  subscribeMetricsTarget,
  type RoundTarget,
} from "../desktop/panes/paneLink";

const EVAL_HASH = "6b7ba0521382b111502a03724c87df78d0c2598cd03c1663556d58f9790b4ebf";

/** Round 1 of the real run: it lost an attempt but kept its eval set. */
const ROUND_01 = {
  round_index: 1,
  run_id: "r01",
  parent_round_id: "r00",
  eval_set_hash: EVAL_HASH,
  created_at_ms: 1786845157448,
  engine: {
    backend: "claude",
    orchestrator_model: "opus",
    substep_model: "haiku",
    scaffold_git_sha: "abc1234",
  },
  gates: {
    all_attempts_completed: false,
    eval_set_stable: true,
    lineage_recorded: true,
    noise_floor_available: true,
  },
  problems: [
    {
      problem_id: "cuda/matmul-speedup",
      problem_type: "speedup",
      split: "practice",
      score: 2.4,
      score_scale: "speedup_ratio",
      scored: true,
      passed_correctness: true,
    },
    {
      problem_id: "flat-baseline",
      problem_type: "speedup",
      split: "practice",
      score: 2.05,
      score_scale: "speedup_ratio",
      scored: true,
      passed_correctness: true,
    },
    {
      problem_id: "kaggle/titanic",
      problem_type: "kaggle",
      split: "held_out",
      score: 55.0,
      score_scale: "leaderboard_percentile",
      scored: true,
      passed_correctness: true,
    },
  ],
  type_scores: [
    {
      problem_type: "speedup",
      split: "practice",
      mean_score: 2.2249999999999996,
      n: 2,
      correctness_pass_rate: 1.0,
    },
    {
      problem_type: "kaggle",
      split: "held_out",
      mean_score: 55.0,
      n: 1,
      correctness_pass_rate: 1.0,
    },
  ],
  deltas: [],
  saturation: [
    {
      problem_type: "speedup",
      split: "practice",
      verdict: "refused_attempt_lost",
      reason: "at least one attempt was lost",
    },
  ],
  cost: { attempts: 3, tokens: 1320, wall_clock_seconds: 0.67 },
  escalation_count: 4,
  verdict: "1 of 4 attempt(s) failed and are absent from every cell",
};

const ROUND_00 = {
  ...ROUND_01,
  round_index: 0,
  run_id: "r00",
  parent_round_id: null,
  gates: { ...ROUND_01.gates, all_attempts_completed: true },
  saturation: [],
};

/** `round-NN/metrics.json` — the only file carrying comparability. */
const SUMMARY_01 = {
  schema_version: 1,
  round_index: 1,
  run_id: "r01",
  parent_round_id: "r00",
  eval_set_hash: EVAL_HASH,
  comparable_to_parent: false,
  cells: [],
};

const SUMMARY_00 = {
  ...SUMMARY_01,
  round_index: 0,
  run_id: "r00",
  parent_round_id: null,
  comparable_to_parent: null,
};

const TRAJECTORY = {
  loop_slug: "probe",
  rounds: [
    {
      round: 0,
      run_id: "r00",
      eval_set_hash: EVAL_HASH,
      primary: { "speedup/practice": 1.7166666666666666 },
      constraints: ROUND_00.gates,
      // The other half of the divergence: this row's own
      // `comparable_to_parent` answers the eval-set question only.
      comparable_to_parent: null,
      saturation: [],
    },
    {
      round: 1,
      run_id: "r01",
      eval_set_hash: EVAL_HASH,
      primary: { "speedup/practice": 2.2249999999999996 },
      constraints: ROUND_01.gates,
      comparable_to_parent: true,
      saturation: ROUND_01.saturation,
    },
  ],
};

/** Populates the fake results root; `omit` drops files to model older runs. */
function seed(omit: string[] = []) {
  files.clear();
  const tree: Record<string, unknown> = {
    "loop-probe/trajectory.json": TRAJECTORY,
    "loop-probe/round-00/round.json": ROUND_00,
    "loop-probe/round-00/metrics.json": SUMMARY_00,
    "loop-probe/round-01/round.json": ROUND_01,
    "loop-probe/round-01/metrics.json": SUMMARY_01,
  };
  for (const [rel, body] of Object.entries(tree)) {
    if (!omit.includes(rel)) files.set(rel, JSON.stringify(body));
  }
  listing = [
    {
      rel_path: "loop-probe/trajectory.json",
      is_dir: false,
      size: 1,
      mtime_ms: 1,
    },
  ];
}

/** Expands the given round in the list view and waits for the detail read. */
async function openRound(index: number) {
  const { default: FlywheelPane } = await import(
    "../desktop/panes/FlywheelPane"
  );
  render(<FlywheelPane />);
  const prefix = String(index).padStart(2, "0");
  // Poll rather than findAllByRole: the header's own buttons resolve that
  // immediately, long before the trajectory read has produced any rows.
  const row = await waitFor(() => {
    const found = screen
      .getAllByRole("button")
      .find((b) => b.textContent?.startsWith(prefix));
    if (!found) throw new Error(`round ${prefix} row not rendered`);
    return found;
  });
  fireEvent.click(row);
  // The detail block always renders a comparability label once the reads land.
  await screen.findByText(/comparab/i);
}

afterEach(() => {
  cleanup();
  files.clear();
  __resetPaneLinkForTests();
});

describe("FlywheelPane round detail — comparability", () => {
  it("reports the summary's refusal, not the matching eval-set hashes", async () => {
    seed();
    await openRound(1);

    // The regression: parent and child share an eval set, so the old
    // hash-only recomputation painted this green.
    expect(screen.queryByText("comparable to parent")).not.toBeInTheDocument();
    expect(screen.getByText("NOT comparable to parent")).toBeInTheDocument();
    // ...and it must not blame the eval set, which did not change: that
    // sends the operator to re-cut a corpus when one attempt was lost.
    expect(
      screen.queryByText(/eval set changed/i),
    ).not.toBeInTheDocument();

    // Everything it used to contradict is still on screen beside it.
    expect(screen.getByText("✗ all_attempts_completed")).toBeInTheDocument();
    expect(screen.getByText("refused_attempt_lost")).toBeInTheDocument();
  });

  it("still renders a baseline round as unknown", async () => {
    seed();
    await openRound(0);
    expect(screen.getByText("comparable: unknown")).toBeInTheDocument();
  });

  it("says unknown, not comparable, when no summary was written", async () => {
    // An older round from before the reporting layer wrote metrics.json. The
    // hashes match, which is exactly when the fallback would have claimed
    // comparability it cannot support.
    seed(["loop-probe/round-01/metrics.json"]);
    await openRound(1);
    expect(screen.getByText("comparable: unknown")).toBeInTheDocument();
    expect(screen.queryByText("comparable to parent")).not.toBeInTheDocument();
  });

  it("says unknown when the summary is present but malformed", async () => {
    seed();
    files.set("loop-probe/round-01/metrics.json", '{"comparable_to_p');
    await openRound(1);
    expect(screen.getByText("comparable: unknown")).toBeInTheDocument();
    expect(screen.queryByText("comparable to parent")).not.toBeInTheDocument();
  });
});

describe("FlywheelPane round detail — per-problem rows", () => {
  it("shows nested problem ids under the cell they are reduced into", async () => {
    seed();
    await openRound(1);

    // The defect: this id is real and supported and appeared nowhere as text.
    expect(screen.getByText("↳ cuda/matmul-speedup")).toBeInTheDocument();
    expect(screen.getByText("↳ flat-baseline")).toBeInTheDocument();
    expect(screen.getByText("↳ kaggle/titanic")).toBeInTheDocument();

    // Each problem's own score, so a cell mean can be read against the
    // problems it averages rather than taken on faith.
    expect(screen.getByText("2.40")).toBeInTheDocument();
    expect(screen.getByText("2.05")).toBeInTheDocument();
    // Formatted exactly as the cell means above them, so the two read as one
    // scale rather than as two different kinds of number.
    expect(screen.getByText("2.22")).toBeInTheDocument();

    // Grouped under their cell: the speedup problems follow the speedup row.
    const rows = Array.from(document.querySelectorAll("tbody tr")).map(
      (r) => r.textContent ?? "",
    );
    const cellRow = rows.findIndex((t) => t.startsWith("speedup/practice"));
    const problemRow = rows.findIndex((t) =>
      t.startsWith("↳ cuda/matmul-speedup"),
    );
    expect(cellRow).toBeGreaterThanOrEqual(0);
    expect(problemRow).toBe(cellRow + 1);
  });

  it("shows a problem whose cell never made it into type_scores", async () => {
    // Invisible is the failure being fixed, so an orphan is listed with its
    // cell name rather than dropped for having no row to sit under.
    seed();
    files.set(
      "loop-probe/round-01/round.json",
      JSON.stringify({ ...ROUND_01, type_scores: [] }),
    );
    await openRound(1);
    expect(
      screen.getByText("↳ speedup/practice · cuda/matmul-speedup"),
    ).toBeInTheDocument();
  });

  it("publishes the loop and round when [metrics] is clicked, and only then (#390 item 3)", async () => {
    const seen: RoundTarget[] = [];
    subscribeMetricsTarget((t) => seen.push(t));
    seed();
    await openRound(1);
    // Expanding the round is a read, not a cross-pane gesture: nothing has
    // been published yet. Re-aiming the metrics pane takes the explicit link.
    expect(seen).toEqual([]);

    fireEvent.click(screen.getByRole("button", { name: "[metrics]" }));
    expect(seen).toEqual([{ loop: "loop-probe", round: 1 }]);
    // The detail is still expanded — the link must not collapse what the
    // operator is reading.
    expect(screen.getByText("NOT comparable to parent")).toBeInTheDocument();
  });

  it("offers [metrics] even before round.json lands — the live round is the one being watched", async () => {
    const seen: RoundTarget[] = [];
    subscribeMetricsTarget((t) => seen.push(t));
    seed(["loop-probe/round-01/round.json"]);
    const { default: FlywheelPane } = await import("../desktop/panes/FlywheelPane");
    render(<FlywheelPane />);
    const row = await waitFor(() => {
      const found = screen
        .getAllByRole("button")
        .find((b) => b.textContent?.startsWith("01"));
      if (!found) throw new Error("round 01 row not rendered");
      return found;
    });
    fireEvent.click(row);
    await screen.findByText("no round.json for this round yet");

    fireEvent.click(screen.getByRole("button", { name: "[metrics]" }));
    expect(seen).toEqual([{ loop: "loop-probe", round: 1 }]);
  });

  it("marks an unscored problem instead of showing a measured-looking zero", async () => {
    seed();
    files.set(
      "loop-probe/round-01/round.json",
      JSON.stringify({
        ...ROUND_01,
        problems: [
          {
            problem_id: "cuda/matmul-speedup",
            problem_type: "speedup",
            split: "practice",
            scored: false,
            passed_correctness: false,
          },
        ],
      }),
    );
    await openRound(1);
    expect(screen.getByText("unscored")).toBeInTheDocument();
    expect(screen.getByTitle("failed correctness")).toBeInTheDocument();
  });
});
