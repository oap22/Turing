import { describe, expect, it, vi } from "vitest";
import { appendSeries, seriesOf, type Point } from "../desktop/panes/metrics";
import { boundsOf, pixelEnvelope, type XY } from "../desktop/panes/chartGeometry";
import { createPtyInput } from "../desktop/ptyInput";

describe("live graph work", () => {
  it("incremental indexing matches a full rebuild across sparse chunks and resets", () => {
    const points: Point[] = [{ loss: 9 }, { step: 7, loss: 8, accuracy: 2 }, { accuracy: 4 }];
    const indexed = new Map<string, XY[]>();
    appendSeries(indexed, points.slice(0, 1), 0);
    appendSeries(indexed, points.slice(1), 1);
    expect(indexed).toEqual(seriesOf(points));
    expect(indexed.get("accuracy")).toEqual([[7, 2], [2, 4]]);
    indexed.clear();
    appendSeries(indexed, [{ loss: 3 }], 0);
    expect(indexed).toEqual(new Map([["loss", [[0, 3]]]]));
  });
  it("handles long histories without argument overflow and preserves spikes in source order", () => {
    const points: XY[] = Array.from({ length: 200_000 }, (_, i) => [i, 0]);
    points[9][1] = 100;
    points[10][1] = -100;
    expect(boundsOf([{ points }])).toEqual({ minX: 0, maxX: 199999, minY: -100, maxY: 100 });
    const envelope = pixelEnvelope(points, 0, 199999, 600);
    expect(envelope.length).toBeLessThanOrEqual(2404);
    expect(envelope[0]).toBe(points[0]);
    expect(envelope.at(-1)).toBe(points.at(-1));
    expect(envelope).toContain(points[9]);
    expect(envelope).toContain(points[10]);
    expect(envelope.indexOf(points[9])).toBeLessThan(envelope.indexOf(points[10]));
    expect(envelope.every((p, i) => !i || p[0] >= envelope[i - 1][0])).toBe(true);
  });
  it("keeps a backtracking curve and small curves unchanged", () => {
    const points: XY[] = Array.from({ length: 100 }, (_, i) => [100 - i, i]);
    expect(pixelEnvelope(points, 0, 100, 1)).toBe(points);
    expect(pixelEnvelope(points, 0, 100, 600)).toBe(points);
  });
});

describe("terminal input", () => {
  it("sends the first key immediately and coalesces later keys behind a slow write in order", async () => {
    let finish!: () => void;
    const write = vi.fn().mockImplementationOnce(() => new Promise<void>((r) => { finish = r; })).mockResolvedValue(undefined);
    const queue = createPtyInput(write, vi.fn());
    queue.push("a"); queue.push("b"); queue.push("😀");
    expect(write.mock.calls).toEqual([["a"]]);
    finish();
    await Promise.resolve(); await Promise.resolve();
    expect(write.mock.calls).toEqual([["a"], ["b😀"]]);
  });
  it("discards queued input on disposal and reports failed writes without retry", async () => {
    let finish!: () => void;
    const write = vi.fn(() => new Promise<void>((r) => { finish = r; }));
    const queue = createPtyInput(write, vi.fn());
    queue.push("a"); queue.push("b"); queue.close(); finish();
    await Promise.resolve();
    expect(write).toHaveBeenCalledTimes(1);
    const error = vi.fn();
    const failed = vi.fn().mockRejectedValue(new Error("closed"));
    const broken = createPtyInput(failed, error);
    broken.push("a"); broken.push("b");
    await Promise.resolve();
    broken.push("c");
    expect(failed).toHaveBeenCalledTimes(1);
    expect(error).toHaveBeenCalledTimes(1);
  });
});
