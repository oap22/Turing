// paneLink — the flywheel→metrics cross-pane link (#390 item 3), DOM-free.
//
// The mapping and the channel are covered here; the precedence rule ("last
// action wins" between a flywheel click and a `.viewer.json` delivery) is a
// property of the metrics pane holding ONE request slot, so it is asserted
// where the slot lives: MetricsPane.test.tsx's cross-pane describe block.

import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  __resetPaneLinkForTests,
  matchesRound,
  publishMetricsTarget,
  resolveRunRequest,
  subscribeMetricsTarget,
  type RoundTarget,
} from "../desktop/panes/paneLink";

const LOOP = "loop-probe";
const R0: RoundTarget = { loop: LOOP, round: 0 };

// The miniature results tree the MetricsPane tests use, plus the shapes the
// exclusions exist for: a rotated chain, a staging dir, another loop.
const RUN_FILES = [
  `${LOOP}/round-01/attempts/cuda/matmul-speedup/metrics.jsonl`,
  `${LOOP}/round-01/attempts/flat-baseline/metrics.jsonl`,
  `${LOOP}/round-00/attempts/cuda/matmul-speedup/prior-1/metrics.jsonl`,
  `${LOOP}/round-00/attempts/cuda/matmul-speedup/metrics.jsonl`,
  `${LOOP}/round-00/attempts/bad-instrument/metrics.jsonl`,
];

describe("matchesRound", () => {
  it("matches every live attempt of the round, nested problem ids included", () => {
    expect(matchesRound(`${LOOP}/round-00/attempts/bad-instrument/metrics.jsonl`, R0)).toBe(true);
    expect(matchesRound(`${LOOP}/round-00/attempts/cuda/matmul-speedup/metrics.jsonl`, R0)).toBe(
      true,
    );
  });

  it("does not match another round or another loop", () => {
    expect(matchesRound(`${LOOP}/round-01/attempts/flat-baseline/metrics.jsonl`, R0)).toBe(false);
    expect(
      matchesRound(`loop-other/round-00/attempts/bad-instrument/metrics.jsonl`, R0),
    ).toBe(false);
  });

  it("spells the round dir round-NN, so round 7 is round-07 and not round-7", () => {
    expect(matchesRound(`${LOOP}/round-07/attempts/p/metrics.jsonl`, { loop: LOOP, round: 7 })).toBe(
      true,
    );
    expect(matchesRound(`${LOOP}/round-7/attempts/p/metrics.jsonl`, { loop: LOOP, round: 7 })).toBe(
      false,
    );
  });

  it("excludes rotated prior-N chains — a superseded generation is not this round", () => {
    // Mirrors `RoundRunner._viewer_runs`: the click and the agent channel must
    // not disagree about what counts as a run of the round.
    expect(
      matchesRound(`${LOOP}/round-00/attempts/cuda/matmul-speedup/prior-1/metrics.jsonl`, R0),
    ).toBe(false);
  });

  it("excludes a dot-named staging directory — a rotation in progress is not a run", () => {
    expect(
      matchesRound(`${LOOP}/round-00/attempts/cuda/matmul-speedup/.rotating/metrics.jsonl`, R0),
    ).toBe(false);
  });

  it("requires the attempts segment, so round-level files can never be swept in", () => {
    expect(matchesRound(`${LOOP}/round-00/metrics.jsonl`, R0)).toBe(false);
    expect(matchesRound(`${LOOP}/round-00/attempts/metrics.jsonl`, R0)).toBe(false);
  });
});

describe("resolveRunRequest", () => {
  it("maps a flywheel round to that round's live run files, in discovery order", () => {
    expect(resolveRunRequest({ source: "flywheel", target: R0 }, RUN_FILES)).toEqual([
      `${LOOP}/round-00/attempts/cuda/matmul-speedup/metrics.jsonl`,
      `${LOOP}/round-00/attempts/bad-instrument/metrics.jsonl`,
    ]);
  });

  it("resolves a viewer request by the same rules the pane always used", () => {
    expect(
      resolveRunRequest(
        {
          source: "viewer",
          runs: [`${LOOP}/round-00/attempts/cuda/matmul-speedup`],
        },
        RUN_FILES,
      ),
    ).toEqual([`${LOOP}/round-00/attempts/cuda/matmul-speedup/metrics.jsonl`]);
  });

  it("resolves an unmappable round to nothing rather than throwing or guessing", () => {
    expect(
      resolveRunRequest({ source: "flywheel", target: { loop: LOOP, round: 42 } }, RUN_FILES),
    ).toEqual([]);
    expect(resolveRunRequest({ source: "flywheel", target: R0 }, [])).toEqual([]);
  });
});

describe("the channel", () => {
  beforeEach(() => {
    __resetPaneLinkForTests();
  });

  it("delivers a published target to every subscriber", () => {
    const seen: RoundTarget[] = [];
    subscribeMetricsTarget((t) => seen.push(t));
    subscribeMetricsTarget((t) => seen.push(t));
    publishMetricsTarget(R0);
    expect(seen).toEqual([R0, R0]);
  });

  it("stops delivering after unsubscribe, and publishing to nobody is a no-op", () => {
    const cb = vi.fn();
    const unsub = subscribeMetricsTarget(cb);
    unsub();
    expect(() => publishMetricsTarget(R0)).not.toThrow();
    expect(cb).not.toHaveBeenCalled();
  });

  it("keeps delivering to siblings when one subscriber throws", () => {
    const seen: RoundTarget[] = [];
    subscribeMetricsTarget(() => {
      throw new Error("render bug");
    });
    subscribeMetricsTarget((t) => seen.push(t));
    expect(() => publishMetricsTarget(R0)).not.toThrow();
    expect(seen).toEqual([R0]);
  });
});
