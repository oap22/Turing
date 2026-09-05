// Metrics pane parsing/aggregation tests (issue #382), plus a render test
// for the Chart component the pane stacks per series.

import { createElement } from "react";
import { render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  dedupeRunFiles,
  VERDICT_QUALIFIER,
  badgeLabelOf,
  badgeRunTailOf,
  badgeStateOf,
  badgeTitleOf,
  chainDigestOfLastLine,
  etaOf,
  isChartRunFile,
  matchesViewerRuns,
  parseMetricsText,
  parseVerdictFile,
  parseViewerFile,
  pickSeries,
  runLabelOf,
  runIdOf,
  seriesOf,
  verdictPathOf,
} from "../desktop/panes/metrics";
import Chart from "../desktop/panes/Chart";

describe("parseMetricsText", () => {
  it("skips unparseable and non-object lines", () => {
    const text = [
      '{"step":1,"loss":0.5}',
      "not json at all",
      "42",
      '["array", "line"]',
      '{"step":2,"loss":0.3}',
    ].join("\n");
    const points = parseMetricsText(text);
    expect(points).toEqual([
      { step: 1, loss: 0.5 },
      { step: 2, loss: 0.3 },
    ]);
  });

  it("accepts a whole-file JSON array", () => {
    const text = JSON.stringify([
      { step: 0, loss: 1.0 },
      { step: 1, loss: 0.8 },
    ]);
    const points = parseMetricsText(text);
    expect(points).toEqual([
      { step: 0, loss: 1.0 },
      { step: 1, loss: 0.8 },
    ]);
  });

  it("keeps only finite-number values", () => {
    const text = '{"step":1,"loss":0.5,"tag":"x","bad":null,"inf":Infinity}';
    // NaN/Infinity aren't valid JSON literals, so this line is unparseable —
    // exercise the finite-filter with a parseable line containing a string.
    const parseable = '{"step":1,"loss":0.5,"tag":"x"}';
    expect(parseMetricsText(text)).toEqual([]);
    expect(parseMetricsText(parseable)).toEqual([{ step: 1, loss: 0.5 }]);
  });

  it("returns an empty array for empty input", () => {
    expect(parseMetricsText("")).toEqual([]);
    expect(parseMetricsText("   \n  \n")).toEqual([]);
  });
});

describe("runLabelOf", () => {
  it("uses the run directory, or the whole path when there isn't one", () => {
    expect(runLabelOf("run-42/metrics.jsonl")).toBe("run-42");
    expect(runLabelOf("run-42/ckpt/metrics.json")).toBe("run-42");
    expect(runLabelOf("metrics.jsonl")).toBe("metrics.jsonl");
  });
});

describe("dedupeRunFiles", () => {
  it("keeps only the newest file per run so a run can't be plotted twice", () => {
    const files = [
      { rel_path: "run-a/metrics.jsonl", mtime_ms: 100 },
      { rel_path: "run-a/ckpt/metrics.json", mtime_ms: 300 },
      { rel_path: "run-b/metrics.jsonl", mtime_ms: 200 },
      { rel_path: "run-a/metrics.json", mtime_ms: 50 },
    ];
    expect(dedupeRunFiles(files)).toEqual([
      { rel_path: "run-a/ckpt/metrics.json", mtime_ms: 300 },
      { rel_path: "run-b/metrics.jsonl", mtime_ms: 200 },
    ]);
  });

  it("preserves input order and leaves already-unique lists alone", () => {
    const files = [
      { rel_path: "run-b/metrics.jsonl", mtime_ms: 2 },
      { rel_path: "run-a/metrics.jsonl", mtime_ms: 1 },
    ];
    expect(dedupeRunFiles(files)).toEqual(files);
    expect(dedupeRunFiles([])).toEqual([]);
  });
});

describe("isChartRunFile", () => {
  // The regression this guards: `results.py` writes *summaries* named
  // `metrics.json` (`write_attempt_summary`, `write_round_summary`), the round
  // summary is the last metrics-named file a round writes, and `fs_list` is
  // newest-first — so accepting that basename put auto-follow on a
  // pretty-printed JSON object and the operator saw "no metrics yet" the
  // moment a round completed.
  it("accepts the step log and rejects the summaries beside it", () => {
    expect(
      isChartRunFile(
        "loop/round-00/attempts/cuda/matmul-speedup/metrics.jsonl",
      ),
    ).toBe(true);
    expect(isChartRunFile("demo-run/metrics.jsonl")).toBe(true);
    expect(isChartRunFile("metrics.jsonl")).toBe(true);

    expect(isChartRunFile("loop/round-00/metrics.json")).toBe(false);
    expect(
      isChartRunFile("loop/round-00/attempts/cuda/matmul-speedup/metrics.json"),
    ).toBe(false);
    expect(isChartRunFile("loop/round-00/round.json")).toBe(false);
    expect(isChartRunFile("loop/round-00/attempts/x/metrics.chain.json")).toBe(
      false,
    );
  });

  // A summary must not merely be un-followed — it must not present as a
  // chartable run at all, because `parseMetricsText` yields nothing for it.
  it("a summary object it would have listed carries no series (#401 behaviour, unchanged)", () => {
    const summary = JSON.stringify(
      {
        schema_version: 1,
        problem_id: "flat-baseline",
        best_score: 2.05,
        consumed_steps: 3,
      },
      null,
      2,
    );
    expect(parseMetricsText(summary)).toEqual([]);
    expect(seriesOf(parseMetricsText(summary)).size).toBe(0);
  });
});

describe("runIdOf", () => {
  // The old label was the first path segment — the loop name — so the real
  // listbox showed 18 rows all reading `loop-probe`.
  it("names the run directory, keeping the round and a nested problem id legible", () => {
    expect(
      runIdOf("loop-probe/round-00/attempts/cuda/matmul-speedup/metrics.jsonl"),
    ).toBe("loop-probe/round-00/attempts/cuda/matmul-speedup");
    expect(
      runIdOf("loop-probe/round-01/attempts/flat-baseline/metrics.jsonl"),
    ).toBe("loop-probe/round-01/attempts/flat-baseline");
    expect(runIdOf("demo-run/metrics.jsonl")).toBe("demo-run");
  });

  it("distinguishes runs that differ only in round or problem id", () => {
    const ids = [
      "loop-probe/round-00/attempts/cuda/matmul-speedup/metrics.jsonl",
      "loop-probe/round-01/attempts/cuda/matmul-speedup/metrics.jsonl",
      "loop-probe/round-00/attempts/flat-baseline/metrics.jsonl",
    ].map(runIdOf);
    expect(new Set(ids).size).toBe(3);
  });

  it("falls back to the path itself for a run file at the results root", () => {
    expect(runIdOf("metrics.jsonl")).toBe("metrics.jsonl");
  });
});

describe("matchesViewerRuns", () => {
  // These are verbatim `runs` entries from a real emitted `.viewer.json`
  // (`RoundRunner._viewer_runs`): attempt *directories*, several segments
  // deep, against run files named `<dir>/metrics.jsonl`.
  const runs = [
    "loop-probe/round-00/attempts/bad-instrument",
    "loop-probe/round-00/attempts/cuda/matmul-speedup",
    "loop-probe/round-01/attempts/flat-baseline",
  ];

  it("selects exactly the runs the loop emitted", () => {
    expect(
      matchesViewerRuns(
        "loop-probe/round-00/attempts/bad-instrument/metrics.jsonl",
        runs,
      ),
    ).toBe(true);
    expect(
      matchesViewerRuns(
        "loop-probe/round-00/attempts/cuda/matmul-speedup/metrics.jsonl",
        runs,
      ),
    ).toBe(true);
    expect(
      matchesViewerRuns(
        "loop-probe/round-01/attempts/flat-baseline/metrics.jsonl",
        runs,
      ),
    ).toBe(true);
  });

  it("does not select an unnamed run", () => {
    expect(
      matchesViewerRuns(
        "loop-probe/round-01/attempts/kaggle/titanic/metrics.jsonl",
        runs,
      ),
    ).toBe(false);
    expect(
      matchesViewerRuns(
        "loop-probe/round-00/attempts/flat-baseline/metrics.jsonl",
        runs,
      ),
    ).toBe(false);
  });

  // The prefix trap, and the reason this is equality and not `startsWith`: the
  // runner rotates a superseded chain into `<dir>/prior-N/` and deliberately
  // leaves it out of `runs`, yet its path begins with the named directory. A
  // prefix rule would draw a superseded attempt as if the loop had asked for it.
  it("does not select a sibling directory sharing the whole prefix", () => {
    expect(
      matchesViewerRuns(
        "loop-probe/round-00/attempts/cuda/matmul-speedup/prior-1/metrics.jsonl",
        runs,
      ),
    ).toBe(false);
    expect(
      matchesViewerRuns(
        "loop-probe/round-00/attempts/bad-instrument-2/metrics.jsonl",
        runs,
      ),
    ).toBe(false);
  });

  // The old code compared against the first path segment, which no
  // multi-segment entry could ever equal — the field was wholly inert.
  it("is not satisfied by the loop name alone", () => {
    expect(
      matchesViewerRuns(
        "loop-probe/round-00/attempts/bad-instrument/metrics.jsonl",
        ["loop-probe"],
      ),
    ).toBe(false);
  });

  it("accepts the run file spelled out, and tolerates a trailing slash", () => {
    expect(
      matchesViewerRuns(
        "loop-probe/round-00/attempts/bad-instrument/metrics.jsonl",
        ["loop-probe/round-00/attempts/bad-instrument/metrics.jsonl"],
      ),
    ).toBe(true);
    expect(
      matchesViewerRuns(
        "loop-probe/round-00/attempts/bad-instrument/metrics.jsonl",
        ["loop-probe/round-00/attempts/bad-instrument/"],
      ),
    ).toBe(true);
  });
});

describe("seriesOf", () => {
  it("extracts every numeric key except step/total_steps/ts, x from step", () => {
    const points = [
      { step: 0, loss: 1.0, acc: 0.1, total_steps: 100, ts: 1000 },
      { step: 1, loss: 0.8, acc: 0.2, total_steps: 100, ts: 1001 },
    ];
    const series = seriesOf(points);
    expect(Array.from(series.keys()).sort()).toEqual(["acc", "loss"]);
    expect(series.get("loss")).toEqual([
      [0, 1.0],
      [1, 0.8],
    ]);
    expect(series.get("acc")).toEqual([
      [0, 0.1],
      [1, 0.2],
    ]);
  });

  it("falls back to index for x when step is absent", () => {
    const points = [{ loss: 1.0 }, { loss: 0.5 }];
    const series = seriesOf(points);
    expect(series.get("loss")).toEqual([
      [0, 1.0],
      [1, 0.5],
    ]);
  });
});

describe("etaOf", () => {
  it("computes a rate from ts-in-seconds deltas and an ETA when total_steps is present", () => {
    const points = [
      { step: 0, ts: 1000, total_steps: 100 },
      { step: 10, ts: 1010, total_steps: 100 },
    ];
    const eta = etaOf(points, []);
    expect(eta.stepsPerSec).toBeCloseTo(1);
    expect(eta.totalSteps).toBe(100);
    expect(eta.lastStep).toBe(10);
    expect(eta.etaSec).toBeCloseTo(90);
  });

  it("returns a null ETA when total_steps is never present", () => {
    const points = [
      { step: 0, ts: 1000 },
      { step: 10, ts: 1010 },
    ];
    const eta = etaOf(points, []);
    expect(eta.stepsPerSec).toBeCloseTo(1);
    expect(eta.totalSteps).toBeNull();
    expect(eta.etaSec).toBeNull();
  });
});

describe("pickSeries", () => {
  it("keeps the stored series when it's still available", () => {
    expect(pickSeries(["acc", "loss", "grad_norm"], "grad_norm")).toBe(
      "grad_norm",
    );
  });

  it("falls back to loss when the stored series is missing", () => {
    expect(pickSeries(["acc", "loss"], "nonexistent")).toBe("loss");
    expect(pickSeries(["acc", "loss"], null)).toBe("loss");
  });

  it("falls back to the first series alphabetically when loss isn't present", () => {
    expect(pickSeries(["grad_norm", "acc"], null)).toBe("acc");
    expect(pickSeries(["grad_norm", "acc"], "nonexistent")).toBe("acc");
  });

  it("returns null when there are no series at all", () => {
    expect(pickSeries([], null)).toBeNull();
    expect(pickSeries([], "loss")).toBeNull();
  });
});

describe("Chart", () => {
  // jsdom has no ResizeObserver; Chart's `useSize` hook falls back to its
  // initial {w, h} state when the observer never fires a callback, which is
  // enough to render and assert against.
  beforeEach(() => {
    vi.stubGlobal(
      "ResizeObserver",
      class {
        observe() {}
        unobserve() {}
        disconnect() {}
      },
    );
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("renders one polyline per series and a bordered (not white-filled) plot area", () => {
    const series = [
      {
        label: "run1/loss",
        points: [
          [0, 1],
          [1, 0.5],
          [2, 0.25],
        ] as Array<[number, number]>,
      },
      {
        label: "run2/loss",
        points: [
          [0, 1.2],
          [1, 0.6],
        ] as Array<[number, number]>,
      },
    ];
    const { container } = render(createElement(Chart, { series }));
    const polylines = container.querySelectorAll("polyline");
    expect(polylines.length).toBe(2);
    expect(polylines[0].getAttribute("points")?.split(" ").length).toBe(3);
    expect(polylines[1].getAttribute("points")?.split(" ").length).toBe(2);

    // The plot area is a themed outline, not a filled (white) background —
    // the dark terminal aesthetic carries through instead of a print-style
    // white chart background.
    const border = container.querySelector('[data-testid="chart-plot-border"]');
    expect(border).not.toBeNull();
    expect(border?.getAttribute("fill")).toBe("none");
    expect(border?.getAttribute("stroke")).toBe("var(--t-edge)");
  });

  it("gives every overlaid series its own theme-palette color", () => {
    const series = Array.from({ length: 8 }, (_, i) => ({
      label: `run${i}/loss`,
      points: [
        [0, i],
        [1, i + 1],
      ] as Array<[number, number]>,
    }));
    const { container } = render(createElement(Chart, { series }));
    const strokes = Array.from(container.querySelectorAll("polyline")).map(
      (p) => p.getAttribute("stroke"),
    );
    expect(strokes).toEqual([
      "var(--t-series-1)",
      "var(--t-series-2)",
      "var(--t-series-3)",
      "var(--t-series-4)",
      "var(--t-series-5)",
      "var(--t-series-6)",
      "var(--t-series-7)",
      "var(--t-series-8)",
    ]);
    // Past the palette length the colors cycle rather than going undefined.
    const wrapped = render(
      createElement(Chart, {
        series: [
          ...series,
          { label: "run8/loss", points: [[0, 0]] as Array<[number, number]> },
        ],
      }),
    );
    const wrappedStrokes = Array.from(
      wrapped.container.querySelectorAll("polyline"),
    );
    expect(wrappedStrokes[8].getAttribute("stroke")).toBe("var(--t-series-1)");
  });

  it("prefers an explicitly assigned color over the positional fallback", () => {
    // MetricsPane assigns sticky per-run colors, so the run in slot 0 of the
    // chart is not necessarily the run holding series-1.
    const series = [
      {
        label: "run-b/loss",
        points: [[0, 1]] as Array<[number, number]>,
        color: "var(--t-series-4)",
      },
    ];
    const { container } = render(createElement(Chart, { series }));
    expect(container.querySelector("polyline")?.getAttribute("stroke")).toBe(
      "var(--t-series-4)",
    );
  });

  it("renders a legend remove button only for series that can be removed", () => {
    const onRemove = vi.fn();
    const series = [
      {
        label: "run-a/loss",
        points: [[0, 1]] as Array<[number, number]>,
        onRemove,
      },
      { label: "run-b/loss", points: [[0, 2]] as Array<[number, number]> },
    ];
    const { container } = render(createElement(Chart, { series }));
    const buttons = container.querySelectorAll("button");
    expect(buttons.length).toBe(1);
    expect(buttons[0].getAttribute("aria-label")).toBe("remove run-a/loss");
    (buttons[0] as HTMLButtonElement).click();
    expect(onRemove).toHaveBeenCalledTimes(1);
  });

  // The ghost-curve defect: the key used to be `s.label`, a display string
  // that two pinned runs charting the same series produce identically. React
  // logged "Encountered two children with the same key" and left the
  // duplicate-keyed nodes mounted, so switching series accumulated polylines
  // (3 → 4 → 5 → 6 measured in one real session) whose stale data was
  // rescaled onto the new axis — old `consumed_steps` values drawn as a
  // plausible-looking `progress` curve.
  it("renders one polyline per series even when two series share a display label", () => {
    const series = [
      {
        id: "runA/metrics.jsonl::progress",
        label: "loop-probe/progress",
        points: [
          [0, 1],
          [1, 0.5],
        ] as Array<[number, number]>,
      },
      {
        id: "runB/metrics.jsonl::progress",
        label: "loop-probe/progress",
        points: [
          [0, 2],
          [1, 1.5],
        ] as Array<[number, number]>,
      },
    ];
    const { container } = render(createElement(Chart, { series }));
    expect(container.querySelectorAll("polyline").length).toBe(2);
    // Duplicate testids are the same footgun as duplicate keys.
    expect(
      new Set(
        Array.from(container.querySelectorAll("polyline")).map((p) =>
          p.getAttribute("data-testid"),
        ),
      ).size,
    ).toBe(2);
  });

  it("does not accumulate stale polylines across series switches with colliding labels", () => {
    // Two runs, the same label on every series (the pre-fix condition), cycled
    // through three series names and back. Pre-fix this ended at six
    // polylines; the count must stay at exactly one per charted series.
    // MetricsPane's real label shape — `<run>/<series title>` — so the label
    // both collides between the two runs AND changes on every switch, which is
    // exactly the sequence that left orphaned nodes mounted.
    function frame(seriesName: string) {
      return ["runA", "runB"].map((run, i) => ({
        id: `${run}/metrics.jsonl::${seriesName}`,
        label: `loop-probe/${seriesName}`,
        points: [
          [0, i + 1],
          [1, i + 2],
        ] as Array<[number, number]>,
      }));
    }
    const { container, rerender } = render(
      createElement(Chart, { series: frame("progress") }),
    );
    for (const name of ["speedup_ratio", "consumed_steps", "progress"]) {
      rerender(createElement(Chart, { series: frame(name) }));
      expect(container.querySelectorAll("polyline").length).toBe(2);
    }
  });

  // The key must be unique even for a caller that supplies no `id` at all, so
  // the chart cannot accumulate stale nodes however it is driven.
  it("stays one-polyline-per-series when labels collide and no id is supplied", () => {
    const dup = [
      {
        label: "same",
        points: [
          [0, 1],
          [1, 2],
        ] as Array<[number, number]>,
      },
      {
        label: "same",
        points: [
          [0, 3],
          [1, 4],
        ] as Array<[number, number]>,
      },
      {
        label: "same",
        points: [
          [0, 5],
          [1, 6],
        ] as Array<[number, number]>,
      },
    ];
    const { container } = render(createElement(Chart, { series: dup }));
    expect(container.querySelectorAll("polyline").length).toBe(3);
  });
});

describe("parseViewerFile", () => {
  it("reads series and runs", () => {
    expect(parseViewerFile('{"series":"loss","runs":["run-42"]}')).toEqual({
      series: "loss",
      runs: ["run-42"],
    });
  });

  it("applies a titles map", () => {
    expect(
      parseViewerFile(
        '{"series":"loss","titles":{"loss":"DPO loss — run 42"}}',
      ),
    ).toEqual({
      series: "loss",
      titles: { loss: "DPO loss — run 42" },
    });
  });

  it("ignores titles that are not a string map", () => {
    // Each of these is silently dropped rather than failing the whole file,
    // so a half-finished hand edit degrades to "no titles".
    for (const bad of [
      '{"titles":"loss"}',
      '{"titles":42}',
      '{"titles":null}',
      '{"titles":["loss"]}',
      '{"titles":{"loss":42}}',
      '{"titles":{"loss":{"nested":"no"}}}',
    ]) {
      expect(parseViewerFile(bad), bad).toEqual({});
    }
  });

  it("accepts an empty titles map", () => {
    expect(parseViewerFile('{"titles":{}}')).toEqual({ titles: {} });
  });

  it("ignores unknown keys", () => {
    expect(parseViewerFile('{"series":"loss","nope":123}')).toEqual({
      series: "loss",
    });
  });

  it("drops individually malformed keys but keeps the valid ones", () => {
    expect(
      parseViewerFile('{"series":7,"runs":["a"],"titles":{"a":"A"}}'),
    ).toEqual({
      runs: ["a"],
      titles: { a: "A" },
    });
    expect(parseViewerFile('{"runs":["a",2]}')).toEqual({});
  });

  it("returns null for malformed or non-object JSON", () => {
    expect(parseViewerFile("not json")).toBeNull();
    expect(parseViewerFile("null")).toBeNull();
    expect(parseViewerFile("42")).toBeNull();
    expect(parseViewerFile('"loss"')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// metrics.verdict.json — what the badge beside the run selector is derived from
// ---------------------------------------------------------------------------

// The digest the loop recorded, and the digest the pane's last parsed line
// carries. `results.write_attempt_verdict` copies `chain_head` straight out of
// the sidecar's `"final"`, and `MetricsWriter._append_sync` writes that same
// digest into the last line's `_chain` as `"<seq>:<digest>"` — so these two
// strings are the same string whenever the pane has read the whole log.
const HEAD = "b7f0".repeat(16);
const CHECKED_AT_MS = 1786845156000;

/** The shape `results.write_attempt_verdict` emits. */
function verdictJson(extra: Record<string, unknown> = {}): string {
  return JSON.stringify(
    {
      schema_version: 1,
      state: "ok",
      lines_checked: 4,
      checked_at_ms: CHECKED_AT_MS,
      checked_by: "loop",
      chain_head: HEAD,
      detail: "/results/round-00/attempts/s1: OK (4 line(s) checked)",
      note: "detects alteration; does not prevent it — see OPEN-QUESTIONS R2/Q11",
      ...extra,
    },
    null,
    2,
  );
}

describe("verdictPathOf", () => {
  it("names the verdict beside the run file, not beside the results root", () => {
    expect(verdictPathOf("loop/round-00/attempts/s1/metrics.jsonl")).toBe(
      "loop/round-00/attempts/s1/metrics.verdict.json",
    );
  });

  it("handles a run file sitting directly at the results root", () => {
    expect(verdictPathOf("metrics.jsonl")).toBe("metrics.verdict.json");
  });
});

describe("parseVerdictFile", () => {
  it("reads the emitted shape", () => {
    expect(parseVerdictFile(verdictJson())).toEqual({
      state: "ok",
      linesChecked: 4,
      checkedAtMs: CHECKED_AT_MS,
      checkedBy: "loop",
      chainHead: HEAD,
      detail: "/results/round-00/attempts/s1: OK (4 line(s) checked)",
    });
  });

  it("reads the two non-ok states", () => {
    expect(parseVerdictFile(verdictJson({ state: "failed" }))?.state).toBe(
      "failed",
    );
    expect(parseVerdictFile(verdictJson({ state: "incomplete" }))?.state).toBe(
      "incomplete",
    );
  });

  it("returns null rather than guessing when the state is unknown or absent", () => {
    // A file this reader cannot understand must read as "no verdict", never
    // as a passing one: the badge's whole job is to not overclaim.
    expect(
      parseVerdictFile(verdictJson({ state: "probably-fine" })),
    ).toBeNull();
    expect(parseVerdictFile('{"lines_checked":4}')).toBeNull();
    expect(parseVerdictFile("not json")).toBeNull();
    expect(parseVerdictFile("null")).toBeNull();
    expect(parseVerdictFile("[]")).toBeNull();
  });

  it("returns null when lines_checked is missing or not a number", () => {
    // Without it there is nothing to compare the pane's own parse against,
    // so every badge would be a guess about whether it is current.
    expect(parseVerdictFile(verdictJson({ lines_checked: "4" }))).toBeNull();
    expect(parseVerdictFile('{"state":"ok"}')).toBeNull();
  });

  it("tolerates missing optional fields", () => {
    expect(parseVerdictFile('{"state":"ok","lines_checked":0}')).toEqual({
      state: "ok",
      linesChecked: 0,
      checkedAtMs: null,
      checkedBy: null,
      chainHead: null,
      detail: null,
    });
  });
});

describe("chainDigestOfLastLine", () => {
  // `MetricsWriter._append_sync`: `payload["_chain"] = f"{seq}:{digest}"`.
  function line(seq: number, digest: string): string {
    return JSON.stringify({
      step: seq + 1,
      loss: 0.5,
      _chain: `${seq}:${digest}`,
    });
  }

  it("returns the digest half of the last line's `_chain`", () => {
    expect(
      chainDigestOfLastLine([line(0, "aa"), line(1, HEAD)].join("\n")),
    ).toBe(HEAD);
  });

  it("ignores the sequence number, which is not what `chain_head` records", () => {
    expect(chainDigestOfLastLine(line(41, HEAD))).toBe(HEAD);
  });

  it("is null when no line carries a usable `_chain`", () => {
    expect(chainDigestOfLastLine("")).toBeNull();
    expect(chainDigestOfLastLine('{"step":1,"loss":0.5}')).toBeNull();
    expect(chainDigestOfLastLine('{"step":1,"_chain":7}')).toBeNull();
    expect(chainDigestOfLastLine('{"step":1,"_chain":"nocolon"}')).toBeNull();
    expect(chainDigestOfLastLine('{"step":1,"_chain":"0:"}')).toBeNull();
  });

  it("ignores trailing blank lines, which the writer's own `\\n` produces", () => {
    expect(chainDigestOfLastLine(`${line(0, "aa")}\n${line(1, HEAD)}\n`)).toBe(
      HEAD,
    );
    expect(chainDigestOfLastLine(`${line(1, HEAD)}\n\n  \n`)).toBe(HEAD);
  });

  it("is null — not the last good line's digest — when the last line does not parse", () => {
    // `verify` on disk fails a log whose last line is garbage; skipping back
    // to the last good line would keep the badge green over exactly the bytes
    // the verifier rejects. `fs_tail` holds a mid-write fragment back, so an
    // unparseable last line here is a real one, not a writer mid-append.
    expect(chainDigestOfLastLine(`${line(0, HEAD)}\ngarbage\n`)).toBeNull();
    expect(
      chainDigestOfLastLine(`${line(0, HEAD)}\n{"step":2,"loss":1}\n`),
    ).toBeNull();
    expect(chainDigestOfLastLine(`${line(0, HEAD)}\n[1,2]\n`)).toBeNull();
    expect(
      chainDigestOfLastLine(`${line(0, HEAD)}\n{"step":2,"_ch`),
    ).toBeNull();
  });
});

describe("badgeStateOf", () => {
  const ok = parseVerdictFile(verdictJson());

  it("is verified only when the verdict's chain head is the pane's own last line", () => {
    expect(badgeStateOf(ok, 4, HEAD)).toBe("verified");
  });

  it("is stale when the pane's last line is not the line the loop checked", () => {
    // The gate is the bytes, not the count: a log that was re-driven to the
    // same length as the checked one is a different log, and the digest is
    // what says so.
    expect(badgeStateOf(ok, 4, "0000".repeat(16))).toBe("stale");
    expect(badgeStateOf(ok, 4, null)).toBe("stale");
  });

  it("is stale when the verdict recorded no chain head to bind to", () => {
    // No head means the sidecar could not be read; there is nothing to bind
    // the badge to, and a green chip would be asserting a match nobody made.
    expect(
      badgeStateOf(
        parseVerdictFile(verdictJson({ chain_head: null })),
        4,
        HEAD,
      ),
    ).toBe("stale");
  });

  it("is stale when the log has grown or shrunk since the loop checked", () => {
    expect(badgeStateOf(ok, 5, HEAD)).toBe("stale");
    expect(badgeStateOf(ok, 3, HEAD)).toBe("stale");
  });

  it("is unverified when there is no verdict file", () => {
    expect(badgeStateOf(null, 4, HEAD)).toBe("unverified");
    expect(badgeStateOf(undefined, 0, null)).toBe("unverified");
  });

  it("reports incomplete and failed verdicts as themselves", () => {
    expect(
      badgeStateOf(
        parseVerdictFile(verdictJson({ state: "incomplete" })),
        4,
        HEAD,
      ),
    ).toBe("incomplete");
    expect(
      badgeStateOf(parseVerdictFile(verdictJson({ state: "failed" })), 4, HEAD),
    ).toBe("failed");
  });

  it("never softens a failed verdict into stale, however the bytes moved", () => {
    // Otherwise appending one line to a log whose chain does not recompute
    // would downgrade the badge from an accusation to a shrug.
    const failed = parseVerdictFile(verdictJson({ state: "failed" }));
    expect(badgeStateOf(failed, 99, "0000".repeat(16))).toBe("failed");
    expect(badgeStateOf(failed, 0, null)).toBe("failed");
  });

  it("reports a moved chain head under an incomplete verdict as stale", () => {
    // "Unfinished as of line 4" says nothing about line 5; the honest badge
    // is the one saying the verdict no longer describes this file.
    const incomplete = parseVerdictFile(verdictJson({ state: "incomplete" }));
    expect(badgeStateOf(incomplete, 6, "0000".repeat(16))).toBe("stale");
  });
});

describe("badgeRunTailOf", () => {
  it("names the segments that tell two runs of one round apart", () => {
    expect(badgeRunTailOf("loop-probe/round-00/attempts/bad-instrument")).toBe(
      "round-00/bad-instrument",
    );
    expect(
      badgeRunTailOf("loop-probe/round-00/attempts/cuda/matmul-speedup"),
    ).toBe("cuda/matmul-speedup");
  });

  it("degrades to whatever the run id has", () => {
    expect(badgeRunTailOf("metrics.jsonl")).toBe("metrics.jsonl");
    expect(badgeRunTailOf("solo")).toBe("solo");
  });
});

describe("badgeLabelOf", () => {
  it("never reads as a trust boundary on the good path", () => {
    const label = badgeLabelOf("verified");
    expect(label).toContain("chain ok");
    expect(label.toLowerCase()).not.toContain("trusted");
    expect(label.toLowerCase()).not.toContain("verified");
    expect(label.toLowerCase()).not.toContain("authentic");
  });

  it("gives every state its own glyph as well as its own words", () => {
    const labels = (
      ["verified", "stale", "incomplete", "failed", "unverified"] as const
    ).map(badgeLabelOf);
    expect(new Set(labels).size).toBe(labels.length);
    expect(new Set(labels.map((l) => l[0])).size).toBe(labels.length);
  });
});

describe("badgeTitleOf", () => {
  const dir = "loop/round-00/attempts/s1";

  it("carries the honesty qualifier, with the run's own directory in the command", () => {
    const title = badgeTitleOf(
      "verified",
      parseVerdictFile(verdictJson()),
      dir,
      "results",
    );
    expect(title).toContain(
      VERDICT_QUALIFIER.replace("<dir>", `results/${dir}`),
    );
    expect(title).toContain("python -m turing.research.loop.verify");
  });

  it("writes a command the operator can paste, rooted where the pane is reading", () => {
    expect(badgeTitleOf("verified", null, dir, "results")).toContain(
      `python -m turing.research.loop.verify results/${dir}`,
    );
    // No root to name is said, not silently dropped — a bare relative path
    // would be a command that fails from the operator's shell.
    expect(badgeTitleOf("verified", null, dir)).toContain(
      `python -m turing.research.loop.verify <results-root>/${dir}`,
    );
  });

  it("offers the independent check from every state, including the failing ones", () => {
    for (const state of [
      "verified",
      "stale",
      "incomplete",
      "failed",
      "unverified",
    ] as const) {
      const title = badgeTitleOf(state, null, dir, "results");
      expect(title, state).toContain(
        `python -m turing.research.loop.verify results/${dir}`,
      );
    }
  });

  it("hedges only the green state, and never the accusation", () => {
    // The qualifier parses against the *verified* clause and nothing else.
    // Welded onto FAILED it produced "the loop's check FAILED here … — not
    // proof the numbers are authentic or meaningful", which reads as though
    // the failure itself were being walked back.
    const failed = parseVerdictFile(verdictJson({ state: "failed" }));
    const title = badgeTitleOf("failed", failed, dir, "results");
    expect(title).toContain("FAILED here");
    expect(title).not.toContain(
      "not proof the numbers are authentic or meaningful",
    );
    expect(
      badgeTitleOf("verified", parseVerdictFile(verdictJson()), dir, "results"),
    ).toContain("not proof the numbers are authentic or meaningful");
    for (const state of ["stale", "incomplete", "unverified"] as const) {
      expect(badgeTitleOf(state, null, dir, "results"), state).not.toContain(
        "not proof the numbers are authentic or meaningful",
      );
    }
  });

  it("says when the loop last looked, so 'as last checked' names a time", () => {
    const title = badgeTitleOf(
      "verified",
      parseVerdictFile(verdictJson()),
      dir,
      "results",
    );
    expect(title).toContain("as last checked by loop at");
    expect(title).toContain(new Date(CHECKED_AT_MS).toLocaleString());
  });

  it("describes stale as a mismatch of digests, not as a run still being written", () => {
    // A live run has no verdict file at all — it renders `unverified`. Calling
    // stale "the ordinary state of a run still being written" described a
    // state that cannot happen, and taught the operator to shrug at the one
    // badge that means the pane and the loop are looking at different bytes.
    const title = badgeTitleOf(
      "stale",
      parseVerdictFile(verdictJson()),
      dir,
      "results",
    );
    expect(title).toContain("4");
    expect(title).toContain("re-driven");
    expect(title).not.toContain("still being written");
  });

  it("does not claim nothing was checked while the read is still in flight", () => {
    // `undefined` is "not read yet", `null` is "read, nothing there". They
    // share one visual state on purpose; they must not share one sentence.
    expect(badgeTitleOf("unverified", undefined, dir, "results")).toContain(
      "reading the verdict",
    );
    expect(badgeTitleOf("unverified", undefined, dir, "results")).not.toContain(
      "never recorded a check here",
    );
    expect(badgeTitleOf("unverified", null, dir, "results")).toContain(
      "never recorded a check here",
    );
  });

  it("quotes the loop's own sentence when it has one", () => {
    // The badge and the terminal must tell the same story, so the reason
    // travels verbatim from `verify`'s own formatter rather than being
    // re-worded here.
    const detail =
      "/results/…/s1: FAIL chain: line 0 digest does not match the recomputed chain";
    const title = badgeTitleOf(
      "failed",
      parseVerdictFile(verdictJson({ state: "failed", detail })),
      dir,
    );
    expect(title).toContain(detail);
  });
});
