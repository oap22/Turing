// Metrics pane parsing/aggregation tests (issue #382), plus a render test
// for the Chart component the pane stacks per series.

import { createElement } from "react";
import { render } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  dedupeRunFiles,
  etaOf,
  parseMetricsText,
  parseViewerFile,
  pickSeries,
  runLabelOf,
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

  it("gives every overlaid series its own theme-palette color", () => {
    const series = Array.from({ length: 8 }, (_, i) => ({
      label: `run${i}/loss`,
      points: [[0, i], [1, i + 1]] as Array<[number, number]>,
    }));
    const { container } = render(createElement(Chart, { series }));
    const strokes = Array.from(container.querySelectorAll("polyline")).map((p) =>
      p.getAttribute("stroke"),
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
        series: [...series, { label: "run8/loss", points: [[0, 0]] as Array<[number, number]> }],
      }),
    );
    const wrappedStrokes = Array.from(wrapped.container.querySelectorAll("polyline"));
    expect(wrappedStrokes[8].getAttribute("stroke")).toBe("var(--t-series-1)");
  });

  it("prefers an explicitly assigned color over the positional fallback", () => {
    // MetricsPane assigns sticky per-run colors, so the run in slot 0 of the
    // chart is not necessarily the run holding series-1.
    const series = [
      { label: "run-b/loss", points: [[0, 1]] as Array<[number, number]>, color: "var(--t-series-4)" },
    ];
    const { container } = render(createElement(Chart, { series }));
    expect(container.querySelector("polyline")?.getAttribute("stroke")).toBe("var(--t-series-4)");
  });

  it("renders a legend remove button only for series that can be removed", () => {
    const onRemove = vi.fn();
    const series = [
      { label: "run-a/loss", points: [[0, 1]] as Array<[number, number]>, onRemove },
      { label: "run-b/loss", points: [[0, 2]] as Array<[number, number]> },
    ];
    const { container } = render(createElement(Chart, { series }));
    const buttons = container.querySelectorAll("button");
    expect(buttons.length).toBe(1);
    expect(buttons[0].getAttribute("aria-label")).toBe("remove run-a/loss");
    (buttons[0] as HTMLButtonElement).click();
    expect(onRemove).toHaveBeenCalledTimes(1);
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
