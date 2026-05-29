// Pane-side reward-signal tests for the question-queue manager (ADR 0010 Slice C).
//
// The QueuePane is the *input affordance* for the load-bearing reward path: a
// curated card's recorded decision is the operator's reward-bearing choice
// (accept +1.0, reject -1.0, edit +0.3 — the magnitudes the backend emitter
// writes verbatim from the retired Discord surface). These tests pin the
// surface that drives those decisions: the per-decision `data-decision`
// attribute and accent the card carries once curated, and the synthesis
// affordance that flags a draft whose accept/reject fans fractional credit to
// consumed upstreams. They complement `QueuePane.test.tsx` (which pins the
// columns/affordances/endpoint wiring) by tying the visible decision to its
// reward sign.

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { QueueItem } from "../../ws";
import QueuePane from "../QueuePane";

afterEach(cleanup);

function item(overrides: Partial<QueueItem> = {}): QueueItem {
  return {
    id: "q1",
    prompt: "Why is the sky blue?",
    specialty: "research",
    status: "proposed",
    origin_task_id: null,
    origin_question_id: null,
    proposed_by: "operator",
    episode_id: null,
    consumed_upstreams: [],
    created_at_ms: 1_700_000_000_000,
    approved_at_ms: null,
    dispatched_at_ms: null,
    drafted_at_ms: null,
    curated_at_ms: null,
    decision: null,
    corrected_answer: null,
    ...overrides,
  };
}

describe("QueuePane reward-decision surface", () => {
  // The reward sign each operator decision writes, paired with the Tailwind
  // accent the curated card must carry — the visible echo of the magnitude.
  const cases = [
    { decision: "accept" as const, sign: "+1.0", accent: /emerald/ },
    { decision: "reject" as const, sign: "-1.0", accent: /rose/ },
    { decision: "edit" as const, sign: "+0.3", accent: /amber/ },
  ];

  for (const { decision, sign, accent } of cases) {
    it(`records ${decision} (${sign}) on the curated card with its accent`, () => {
      render(
        <QueuePane
          items={[
            item({
              id: "c1",
              status: "curated",
              decision,
              episode_id: "ep1",
              corrected_answer: decision === "edit" ? "the corrected answer" : null,
            }),
          ]}
        />,
      );
      const card = screen.getByTestId("queue-card-c1");
      // The decision of record drives both the data attribute (consumed by the
      // backend reward emitter's POST) and the visible accent.
      expect(card.getAttribute("data-decision")).toBe(decision);
      expect(card.className).toMatch(accent);
      expect(screen.getByTestId("queue-decision-c1").textContent).toMatch(decision);
    });
  }

  it("surfaces no decision affordance until the draft is curated", () => {
    // An in-flight item carries no reward-bearing decision yet.
    render(<QueuePane items={[item({ id: "f1", status: "in-flight" })]} />);
    const card = screen.getByTestId("queue-card-f1");
    expect(card.getAttribute("data-decision")).toBe("");
    expect(screen.queryByTestId("queue-decision-f1")).not.toBeInTheDocument();
  });

  it("flags a synthesis draft whose curation fans fractional credit upstream", () => {
    // consumed_upstreams non-empty ⇒ accept/reject additionally credits each
    // upstream ±0.3 (the synthesis fractional-credit path). The card must make
    // that provenance visible so the operator knows the blast radius of a thumb.
    render(
      <QueuePane
        items={[
          item({
            id: "s1",
            status: "drafted",
            episode_id: "syn",
            consumed_upstreams: ["up-a", "up-b"],
          }),
        ]}
      />,
    );
    const badge = screen.getByTestId("queue-upstreams-s1");
    expect(badge.textContent).toMatch(/2 upstreams/);
    // The upstream ids are available (the credit targets) for operator inspection.
    expect(badge.getAttribute("title")).toBe("up-a, up-b");
  });
});
