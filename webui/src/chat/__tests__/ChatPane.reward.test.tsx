// Pane-side reward-signal tests for the chat pane (ADR 0010 Slice E).
//
// The chat pane is the secondary *input affordance* for the load-bearing reward
// path the queue pane (Slice C) owns. ADR 0010 Slice E says the chat pane
// "reuses the reward emitter from C", so a chat per-subtask thumb is, by
// construction, identical to a queue curation — the backend ChatManager
// delegates the write to a QueueManager over the shared store (proven byte-for-
// byte in `tests/test_gateway/test_chat_reward_equivalence.py`).
//
// To make that reuse un-fakeable on the *surface* too, these tests REUSE Slice
// C's reward-decision parity helper verbatim: the (decision, sign, accent) case
// table is imported from `queue/__tests__/QueuePane.reward.test.tsx` and the
// chat card is asserted against the *same* expectations the queue card is. A
// drift in the chat surface's reward affordance (decision attribute, accent, or
// supported decisions) would fail against C's own table.

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import type { ChatSubtask } from "../../ws";
import ChatPane from "../ChatPane";
// REUSE C's reward-decision parity table (decision → reward sign + accent),
// the same table the queue pane's reward surface is graded against.
import { REWARD_DECISION_CASES } from "../../queue/__tests__/rewardDecisionCases";

afterEach(cleanup);

function subtask(overrides: Partial<ChatSubtask> = {}): ChatSubtask {
  return {
    id: "st1",
    session_id: "s1",
    index: 0,
    specialty: "research",
    prompt: "",
    content: "an answer",
    status: "completed",
    episode_id: "ep1",
    consumed_upstreams: [],
    created_at_ms: 1_700_000_000_000,
    completed_at_ms: 1_700_000_000_001,
    curated_at_ms: null,
    decision: null,
    corrected_answer: null,
    ...overrides,
  };
}

function withSubtask(s: ChatSubtask) {
  return [
    {
      id: "s1",
      prompt: "p",
      specialty: "research",
      created_at_ms: 1_700_000_000_000,
      subtasks: [s],
    },
  ];
}

describe("ChatPane reward-decision surface", () => {
  // Driven by Slice C's shared case table so the chat thumb is graded against
  // the exact (sign, accent) the queue curation is — the visible echo of the
  // reused reward magnitude.
  for (const { decision, sign, accent } of REWARD_DECISION_CASES) {
    it(`records ${decision} (${sign}) on the curated subtask with its accent`, () => {
      render(
        <ChatPane
          sessions={withSubtask(
            subtask({
              id: "c1",
              status: "curated",
              decision,
              corrected_answer: decision === "edit" ? "the corrected answer" : null,
            }),
          )}
        />,
      );
      const card = screen.getByTestId("chat-subtask-c1");
      // The decision of record drives both the data attribute (consumed by the
      // backend reward emitter's POST) and the visible accent.
      expect(card.getAttribute("data-decision")).toBe(decision);
      expect(card.className).toMatch(accent);
      expect(screen.getByTestId("chat-decision-c1").textContent).toMatch(decision);
    });
  }

  it("surfaces no decision affordance until the subtask is curated", () => {
    // A streaming subtask carries no reward-bearing decision yet.
    render(<ChatPane sessions={withSubtask(subtask({ id: "f1", status: "streaming" }))} />);
    const card = screen.getByTestId("chat-subtask-f1");
    expect(card.getAttribute("data-decision")).toBe("");
    expect(screen.queryByTestId("chat-decision-f1")).not.toBeInTheDocument();
  });

  it("flags a synthesis subtask whose thumb fans fractional credit upstream", () => {
    // consumed_upstreams non-empty ⇒ accept/reject additionally credits each
    // upstream ±0.3 (the synthesis fractional-credit path, reused from C). The
    // card surfaces that provenance so the operator knows the blast radius.
    render(
      <ChatPane
        sessions={withSubtask(
          subtask({ id: "s1", episode_id: "syn", consumed_upstreams: ["up-a", "up-b"] }),
        )}
      />,
    );
    const badge = screen.getByTestId("chat-upstreams-s1");
    expect(badge.textContent).toMatch(/2 upstreams/);
    expect(badge.getAttribute("title")).toBe("up-a, up-b");
  });
});
