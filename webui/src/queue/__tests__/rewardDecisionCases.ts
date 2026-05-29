// Shared reward-decision parity table (ADR 0010 Slice C / Slice E).
//
// The reward sign each operator decision writes, paired with the Tailwind
// accent the curated card must carry — the visible echo of the load-bearing
// reward magnitude (accept +1.0, reject −1.0, edit +0.3, written verbatim from
// the retired Discord surface by the backend emitter).
//
// This lives in `queue/__tests__` because the queue manager owns the reward
// path. The chat pane's reward-decision surface (Slice E) imports the *same*
// table so its curated-subtask card is graded against identical expectations —
// the surface-side proof that the chat thumb reuses this reward path rather
// than re-deriving its own magnitudes. Extracted to a non-test module so both
// suites can import it without re-executing each other's `describe` blocks.

export interface RewardDecisionCase {
  decision: "accept" | "reject" | "edit";
  sign: string;
  accent: RegExp;
}

export const REWARD_DECISION_CASES: RewardDecisionCase[] = [
  { decision: "accept", sign: "+1.0", accent: /emerald/ },
  { decision: "reject", sign: "-1.0", accent: /rose/ },
  { decision: "edit", sign: "+0.3", accent: /amber/ },
];
