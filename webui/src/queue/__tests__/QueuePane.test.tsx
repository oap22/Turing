// Tests for the question-queue manager pane (ADR 0010 Slice C).
//
//  - reducer: snapshot replaces state, delta upserts a single item, the
//    monotonic frontier keeps items as they move between columns.
//  - QueuePane: renders the five frontier columns, places cards by status,
//    surfaces the right curation affordance per stage, and drives the
//    approve / accept / reject / edit POSTs to the gateway endpoints.

import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { QueueDeltaFrame, QueueItem, QueueSnapshotFrame } from "../../ws";
import QueuePane from "../QueuePane";
import { applyQueueFrame, emptyQueue, listQueue } from "../reducer";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function item(overrides: Partial<QueueItem> = {}): QueueItem {
  return {
    id: "q1",
    prompt: "What is the half-life of a flywheel idea?",
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

function okFetch(body: unknown) {
  return vi.fn(
    async () =>
      new Response(JSON.stringify(body), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
  );
}

describe("queue reducer", () => {
  it("snapshot frame replaces the whole map", () => {
    let state = emptyQueue();
    const snap: QueueSnapshotFrame = {
      type: "queue.snapshot",
      items: [item({ id: "a" }), item({ id: "b", created_at_ms: 2 })],
      timestamp_ms: 5,
    };
    state = applyQueueFrame(state, snap);
    expect(listQueue(state)).toHaveLength(2);

    // A second snapshot is authoritative — it does not merge with the first.
    state = applyQueueFrame(state, {
      type: "queue.snapshot",
      items: [item({ id: "c" })],
      timestamp_ms: 6,
    });
    expect(listQueue(state).map((i) => i.id)).toEqual(["c"]);
  });

  it("delta frame upserts a single item and moves it between columns", () => {
    let state = emptyQueue();
    state = applyQueueFrame(state, {
      type: "queue.snapshot",
      items: [item({ id: "a", status: "proposed" })],
      timestamp_ms: 1,
    });
    const delta: QueueDeltaFrame = {
      type: "queue.delta",
      action: "approve",
      item: item({ id: "a", status: "approved", approved_at_ms: 2 }),
      timestamp_ms: 2,
    };
    state = applyQueueFrame(state, delta);
    expect(listQueue(state)).toHaveLength(1);
    expect(listQueue(state)[0].status).toBe("approved");
  });

  it("orders items oldest-first with a stable id tiebreak", () => {
    let state = emptyQueue();
    state = applyQueueFrame(state, {
      type: "queue.snapshot",
      items: [
        item({ id: "z", created_at_ms: 10 }),
        item({ id: "a", created_at_ms: 10 }),
        item({ id: "m", created_at_ms: 5 }),
      ],
      timestamp_ms: 1,
    });
    expect(listQueue(state).map((i) => i.id)).toEqual(["m", "a", "z"]);
  });
});

describe("QueuePane columns", () => {
  it("renders all five frontier columns in order", () => {
    render(<QueuePane items={[]} />);
    for (const s of ["proposed", "approved", "in-flight", "drafted", "curated"]) {
      expect(screen.getByTestId(`queue-column-${s}`)).toBeInTheDocument();
    }
  });

  it("places each card in its status column with a per-column count", () => {
    render(
      <QueuePane
        items={[
          item({ id: "p1", status: "proposed" }),
          item({ id: "d1", status: "drafted", episode_id: "e1" }),
          item({ id: "d2", status: "drafted", episode_id: "e2" }),
        ]}
      />,
    );
    expect(screen.getByTestId("queue-column-proposed").getAttribute("data-count")).toBe("1");
    expect(screen.getByTestId("queue-column-drafted").getAttribute("data-count")).toBe("2");
    expect(screen.getByTestId("queue-card-p1").getAttribute("data-status")).toBe("proposed");
    expect(screen.getByTestId("queue-card-d1").getAttribute("data-status")).toBe("drafted");
  });

  it("shows a synthesis-upstream badge only for drafts that consumed upstreams", () => {
    render(
      <QueuePane
        items={[
          item({ id: "s1", status: "drafted", episode_id: "e1", consumed_upstreams: ["u1", "u2"] }),
          item({ id: "s2", status: "drafted", episode_id: "e2", consumed_upstreams: [] }),
        ]}
      />,
    );
    expect(screen.getByTestId("queue-upstreams-s1").textContent).toMatch(/2 upstreams/);
    expect(screen.queryByTestId("queue-upstreams-s2")).not.toBeInTheDocument();
  });

  it("tints a curated card by its decision", () => {
    render(
      <QueuePane
        items={[
          item({ id: "c1", status: "curated", decision: "accept" }),
          item({ id: "c2", status: "curated", decision: "reject" }),
          item({ id: "c3", status: "curated", decision: "edit", corrected_answer: "fixed" }),
        ]}
      />,
    );
    expect(screen.getByTestId("queue-card-c1").className).toMatch(/emerald/);
    expect(screen.getByTestId("queue-card-c2").className).toMatch(/rose/);
    expect(screen.getByTestId("queue-card-c3").className).toMatch(/amber/);
    // An edited card surfaces the operator's correction of record.
    expect(screen.getByText("fixed")).toBeInTheDocument();
  });
});

describe("QueuePane curation affordances", () => {
  it("offers approve only on proposed items", () => {
    render(<QueuePane items={[item({ id: "p1", status: "proposed" })]} />);
    expect(screen.getByTestId("queue-approve-p1")).toBeInTheDocument();
    expect(screen.queryByTestId("queue-accept-p1")).not.toBeInTheDocument();
  });

  it("offers accept / reject / edit only on drafted items", () => {
    render(<QueuePane items={[item({ id: "d1", status: "drafted", episode_id: "e1" })]} />);
    expect(screen.getByTestId("queue-accept-d1")).toBeInTheDocument();
    expect(screen.getByTestId("queue-reject-d1")).toBeInTheDocument();
    expect(screen.getByTestId("queue-edit-d1")).toBeInTheDocument();
    expect(screen.queryByTestId("queue-approve-d1")).not.toBeInTheDocument();
  });

  it("offers no curation buttons on in-flight or curated items", () => {
    render(
      <QueuePane
        items={[
          item({ id: "f1", status: "in-flight" }),
          item({ id: "c1", status: "curated", decision: "accept" }),
        ]}
      />,
    );
    expect(screen.queryByTestId("queue-accept-f1")).not.toBeInTheDocument();
    expect(screen.queryByTestId("queue-accept-c1")).not.toBeInTheDocument();
  });
});

describe("QueuePane endpoint wiring", () => {
  it("approve POSTs to the approve endpoint", async () => {
    const fetchMock = okFetch(item({ id: "p1", status: "approved" }));
    vi.stubGlobal("fetch", fetchMock);
    render(<QueuePane items={[item({ id: "p1", status: "proposed" })]} />);

    fireEvent.click(screen.getByTestId("queue-approve-p1"));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/queue/approve/p1",
        expect.objectContaining({ method: "POST", credentials: "same-origin" }),
      ),
    );
  });

  it("accept POSTs to the accept endpoint", async () => {
    const fetchMock = okFetch(item({ id: "d1", status: "curated", decision: "accept" }));
    vi.stubGlobal("fetch", fetchMock);
    render(<QueuePane items={[item({ id: "d1", status: "drafted", episode_id: "e1" })]} />);

    fireEvent.click(screen.getByTestId("queue-accept-d1"));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/queue/accept/d1",
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });

  it("reject POSTs to the reject endpoint", async () => {
    const fetchMock = okFetch(item({ id: "d1", status: "curated", decision: "reject" }));
    vi.stubGlobal("fetch", fetchMock);
    render(<QueuePane items={[item({ id: "d1", status: "drafted", episode_id: "e1" })]} />);

    fireEvent.click(screen.getByTestId("queue-reject-d1"));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/queue/reject/d1",
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });

  it("edit opens an input, then POSTs the corrected answer", async () => {
    const fetchMock = okFetch(
      item({ id: "d1", status: "curated", decision: "edit", corrected_answer: "the real answer" }),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<QueuePane items={[item({ id: "d1", status: "drafted", episode_id: "e1" })]} />);

    // The save button only appears after opening the editor, and is disabled
    // until the operator types a correction.
    fireEvent.click(screen.getByTestId("queue-edit-d1"));
    const submit = screen.getByTestId("queue-edit-submit-d1") as HTMLButtonElement;
    expect(submit.disabled).toBe(true);

    fireEvent.change(screen.getByTestId("queue-edit-input-d1"), {
      target: { value: "the real answer" },
    });
    expect(submit.disabled).toBe(false);
    fireEvent.click(submit);

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/queue/edit/d1",
        expect.objectContaining({
          method: "POST",
          body: JSON.stringify({ corrected_answer: "the real answer" }),
        }),
      ),
    );
  });

  it("edit can be cancelled without a POST", () => {
    const fetchMock = okFetch({});
    vi.stubGlobal("fetch", fetchMock);
    render(<QueuePane items={[item({ id: "d1", status: "drafted", episode_id: "e1" })]} />);

    fireEvent.click(screen.getByTestId("queue-edit-d1"));
    fireEvent.click(screen.getByTestId("queue-edit-cancel-d1"));
    expect(screen.queryByTestId("queue-edit-input-d1")).not.toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
