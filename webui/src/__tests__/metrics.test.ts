// Metrics pane parsing/aggregation tests (issue #382), plus a render test
// for the Chart component the pane stacks per series.

import { createElement } from "react";
import { render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  etaOf,
  isChartRunFile,
  matchesViewerRuns,
  parseMetricsText,
  parseViewerFile,
  pickSeries,
  runIdOf,
  seriesOf,
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
    expect(points).toEqual([{ step: 1, loss: 0.5 }, { step: 2, loss: 0.3 }]);
  });

  it("accepts a whole-file JSON array", () => {
    const text = JSON.stringify([{ step: 0, loss: 1.0 }, { step: 1, loss: 0.8 }]);
    const points = parseMetricsText(text);
    expect(points).toEqual([{ step: 0, loss: 1.0 }, { step: 1, loss: 0.8 }]);
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

describe("isChartRunFile", () => {
  // The regression this guards: `results.py` writes *summaries* named
  // `metrics.json` (`write_attempt_summary`, `write_round_summary`), the round
  // summary is the last metrics-named file a round writes, and `fs_list` is
  // newest-first — so accepting that basename put auto-follow on a
  // pretty-printed JSON object and the operator saw "no metrics yet" the
  // moment a round completed.
  it("accepts the step log and rejects the summaries beside it", () => {
    expect(isChartRunFile("loop/round-00/attempts/cuda/matmul-speedup/metrics.jsonl")).toBe(true);
    expect(isChartRunFile("demo-run/metrics.jsonl")).toBe(true);
    expect(isChartRunFile("metrics.jsonl")).toBe(true);

    expect(isChartRunFile("loop/round-00/metrics.json")).toBe(false);
    expect(isChartRunFile("loop/round-00/attempts/cuda/matmul-speedup/metrics.json")).toBe(false);
    expect(isChartRunFile("loop/round-00/round.json")).toBe(false);
    expect(isChartRunFile("loop/round-00/attempts/x/metrics.chain.json")).toBe(false);
  });

  // A summary must not merely be un-followed — it must not present as a
  // chartable run at all, because `parseMetricsText` yields nothing for it.
  it("a summary object it would have listed carries no series (#401 behaviour, unchanged)", () => {
    const summary = JSON.stringify(
      { schema_version: 1, problem_id: "flat-baseline", best_score: 2.05, consumed_steps: 3 },
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
    expect(runIdOf("loop-probe/round-00/attempts/cuda/matmul-speedup/metrics.jsonl")).toBe(
      "loop-probe/round-00/attempts/cuda/matmul-speedup",
    );
    expect(runIdOf("loop-probe/round-01/attempts/flat-baseline/metrics.jsonl")).toBe(
      "loop-probe/round-01/attempts/flat-baseline",
    );
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
    expect(matchesViewerRuns("loop-probe/round-00/attempts/bad-instrument/metrics.jsonl", runs)).toBe(true);
    expect(
      matchesViewerRuns("loop-probe/round-00/attempts/cuda/matmul-speedup/metrics.jsonl", runs),
    ).toBe(true);
    expect(matchesViewerRuns("loop-probe/round-01/attempts/flat-baseline/metrics.jsonl", runs)).toBe(true);
  });

  it("does not select an unnamed run", () => {
    expect(matchesViewerRuns("loop-probe/round-01/attempts/kaggle/titanic/metrics.jsonl", runs)).toBe(false);
    expect(matchesViewerRuns("loop-probe/round-00/attempts/flat-baseline/metrics.jsonl", runs)).toBe(false);
  });

  // The prefix trap, and the reason this is equality and not `startsWith`: the
  // runner rotates a superseded chain into `<dir>/prior-N/` and deliberately
  // leaves it out of `runs`, yet its path begins with the named directory. A
  // prefix rule would draw a superseded attempt as if the loop had asked for it.
  it("does not select a sibling directory sharing the whole prefix", () => {
    expect(
      matchesViewerRuns("loop-probe/round-00/attempts/cuda/matmul-speedup/prior-1/metrics.jsonl", runs),
    ).toBe(false);
    expect(
      matchesViewerRuns("loop-probe/round-00/attempts/bad-instrument-2/metrics.jsonl", runs),
    ).toBe(false);
  });

  // The old code compared against the first path segment, which no
  // multi-segment entry could ever equal — the field was wholly inert.
  it("is not satisfied by the loop name alone", () => {
    expect(matchesViewerRuns("loop-probe/round-00/attempts/bad-instrument/metrics.jsonl", ["loop-probe"])).toBe(
      false,
    );
  });

  it("accepts the run file spelled out, and tolerates a trailing slash", () => {
    expect(
      matchesViewerRuns("loop-probe/round-00/attempts/bad-instrument/metrics.jsonl", [
        "loop-probe/round-00/attempts/bad-instrument/metrics.jsonl",
      ]),
    ).toBe(true);
    expect(
      matchesViewerRuns("loop-probe/round-00/attempts/bad-instrument/metrics.jsonl", [
        "loop-probe/round-00/attempts/bad-instrument/",
      ]),
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
    expect(series.get("loss")).toEqual([[0, 1.0], [1, 0.8]]);
    expect(series.get("acc")).toEqual([[0, 0.1], [1, 0.2]]);
  });

  it("falls back to index for x when step is absent", () => {
    const points = [{ loss: 1.0 }, { loss: 0.5 }];
    const series = seriesOf(points);
    expect(series.get("loss")).toEqual([[0, 1.0], [1, 0.5]]);
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
    expect(pickSeries(["acc", "loss", "grad_norm"], "grad_norm")).toBe("grad_norm");
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
      { label: "run1/loss", points: [[0, 1], [1, 0.5], [2, 0.25]] as Array<[number, number]> },
      { label: "run2/loss", points: [[0, 1.2], [1, 0.6]] as Array<[number, number]> },
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
        points: [[0, 1], [1, 0.5]] as Array<[number, number]>,
      },
      {
        id: "runB/metrics.jsonl::progress",
        label: "loop-probe/progress",
        points: [[0, 2], [1, 1.5]] as Array<[number, number]>,
      },
    ];
    const { container } = render(createElement(Chart, { series }));
    expect(container.querySelectorAll("polyline").length).toBe(2);
    // Duplicate testids are the same footgun as duplicate keys.
    expect(new Set(
      Array.from(container.querySelectorAll("polyline")).map((p) => p.getAttribute("data-testid")),
    ).size).toBe(2);
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
        points: [[0, i + 1], [1, i + 2]] as Array<[number, number]>,
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
      { label: "same", points: [[0, 1], [1, 2]] as Array<[number, number]> },
      { label: "same", points: [[0, 3], [1, 4]] as Array<[number, number]> },
      { label: "same", points: [[0, 5], [1, 6]] as Array<[number, number]> },
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
    expect(parseViewerFile('{"series":"loss","titles":{"loss":"DPO loss — run 42"}}')).toEqual({
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
    expect(parseViewerFile('{"series":"loss","nope":123}')).toEqual({ series: "loss" });
  });

  it("drops individually malformed keys but keeps the valid ones", () => {
    expect(parseViewerFile('{"series":7,"runs":["a"],"titles":{"a":"A"}}')).toEqual({
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
