// Tests for the chat pane (ADR 0010 Slice E).
//
//  - reducer: snapshot replaces state, a `submit` delta upserts a session, a
//    subtask delta upserts the single subtask within its session, subtasks stay
//    ordered by their plan index.
//  - ChatPane: renders the thread, submits a free-form prompt to /api/chat/submit,
//    streams subtask content into the same thread, surfaces thumbs only on a
//    completed subtask, and drives the accept / reject / edit POSTs.

import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type {
  ChatSession,
  ChatSnapshotFrame,
  ChatSubtask,
  ChatSubtaskDeltaFrame,
} from "../../ws";
import ChatPane from "../ChatPane";
import { applyChatFrame, emptyChat, listChat } from "../reducer";

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function subtask(overrides: Partial<ChatSubtask> = {}): ChatSubtask {
  return {
    id: "st1",
    session_id: "s1",
    index: 0,
    specialty: "research",
    prompt: "",
    content: "",
    status: "pending",
    episode_id: null,
    consumed_upstreams: [],
    created_at_ms: 1_700_000_000_000,
    completed_at_ms: null,
    curated_at_ms: null,
    decision: null,
    corrected_answer: null,
    ...overrides,
  };
}

function session(overrides: Partial<ChatSession> = {}): ChatSession {
  return {
    id: "s1",
    prompt: "ad-hoc: summarize the flywheel",
    specialty: "research",
    created_at_ms: 1_700_000_000_000,
    subtasks: [],
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

describe("chat reducer", () => {
  it("snapshot frame replaces the whole map", () => {
    let state = emptyChat();
    const snap: ChatSnapshotFrame = {
      type: "chat.snapshot",
      sessions: [session({ id: "a" }), session({ id: "b", created_at_ms: 2 })],
      timestamp_ms: 5,
    };
    state = applyChatFrame(state, snap);
    expect(listChat(state)).toHaveLength(2);

    // A second snapshot is authoritative — it does not merge with the first.
    state = applyChatFrame(state, {
      type: "chat.snapshot",
      sessions: [session({ id: "c" })],
      timestamp_ms: 6,
    });
    expect(listChat(state).map((s) => s.id)).toEqual(["c"]);
  });

  it("submit delta upserts a session", () => {
    let state = emptyChat();
    state = applyChatFrame(state, {
      type: "chat.delta",
      action: "submit",
      session: session({ id: "s1" }),
      timestamp_ms: 1,
    });
    expect(listChat(state)).toHaveLength(1);
    expect(listChat(state)[0].id).toBe("s1");
  });

  it("subtask delta upserts the single subtask within its session", () => {
    let state = emptyChat();
    state = applyChatFrame(state, {
      type: "chat.delta",
      action: "submit",
      session: session({ id: "s1" }),
      timestamp_ms: 1,
    });
    const plan: ChatSubtaskDeltaFrame = {
      type: "chat.delta",
      action: "plan",
      session_id: "s1",
      subtask: subtask({ id: "st1", status: "pending" }),
      timestamp_ms: 2,
    };
    state = applyChatFrame(state, plan);
    expect(listChat(state)[0].subtasks).toHaveLength(1);

    // A streaming delta on the same subtask updates it in place, not appends.
    state = applyChatFrame(state, {
      type: "chat.delta",
      action: "stream",
      session_id: "s1",
      subtask: subtask({ id: "st1", status: "streaming", content: "partial" }),
      timestamp_ms: 3,
    });
    expect(listChat(state)[0].subtasks).toHaveLength(1);
    expect(listChat(state)[0].subtasks[0].status).toBe("streaming");
    expect(listChat(state)[0].subtasks[0].content).toBe("partial");
  });

  it("orders subtasks by their stable plan index", () => {
    let state = emptyChat();
    state = applyChatFrame(state, {
      type: "chat.snapshot",
      sessions: [
        session({
          id: "s1",
          subtasks: [
            subtask({ id: "c", index: 2 }),
            subtask({ id: "a", index: 0 }),
            subtask({ id: "b", index: 1 }),
          ],
        }),
      ],
      timestamp_ms: 1,
    });
    expect(listChat(state)[0].subtasks.map((s) => s.id)).toEqual([
      "a",
      "b",
      "c",
    ]);
  });
});

describe("ChatPane thread", () => {
  it("shows an empty-state hint with no sessions", () => {
    render(<ChatPane sessions={[]} />);
    expect(screen.getByTestId("chat-thread").textContent).toMatch(/no chats yet/);
  });

  it("renders a session's prompt and its streamed subtask content", () => {
    render(
      <ChatPane
        sessions={[
          session({
            id: "s1",
            prompt: "why is the sky blue?",
            subtasks: [
              subtask({
                id: "st1",
                status: "completed",
                content: "Rayleigh scattering.",
              }),
            ],
          }),
        ]}
      />,
    );
    // The prompt renders in both the thread list and the detail header.
    expect(screen.getAllByText("why is the sky blue?")).toHaveLength(2);
    expect(screen.getByText("Rayleigh scattering.")).toBeInTheDocument();
    expect(
      screen.getByTestId("chat-session-s1").getAttribute("data-subtask-count"),
    ).toBe("1");
  });

  it("flags a synthesis subtask whose thumb fans fractional credit upstream", () => {
    render(
      <ChatPane
        sessions={[
          session({
            id: "s1",
            subtasks: [
              subtask({
                id: "st1",
                status: "completed",
                episode_id: "syn",
                consumed_upstreams: ["up-a", "up-b"],
              }),
            ],
          }),
        ]}
      />,
    );
    const badge = screen.getByTestId("chat-upstreams-st1");
    expect(badge.textContent).toMatch(/2 upstreams/);
    expect(badge.getAttribute("title")).toBe("up-a, up-b");
  });
});

describe("ChatPane master-detail", () => {
  function twoSessions() {
    return [
      session({
        id: "old",
        prompt: "the older thread",
        created_at_ms: 1,
        subtasks: [
          subtask({
            id: "o1",
            session_id: "old",
            status: "completed",
            content: "older answer",
          }),
        ],
      }),
      session({
        id: "new",
        prompt: "the newer thread",
        created_at_ms: 2,
        subtasks: [
          subtask({ id: "n1", session_id: "new", content: "newer answer" }),
        ],
      }),
    ];
  }

  it("auto-selects the newest thread", () => {
    render(<ChatPane sessions={twoSessions()} />);
    expect(screen.getByTestId("chat-session-new")).toBeInTheDocument();
    expect(screen.queryByTestId("chat-session-old")).not.toBeInTheDocument();
    expect(
      screen
        .getByTestId("chat-thread-item-new")
        .getAttribute("data-active"),
    ).toBe("true");
  });

  it("clicking a thread list item pins that thread into the detail pane", () => {
    render(<ChatPane sessions={twoSessions()} />);
    fireEvent.click(screen.getByTestId("chat-thread-item-old"));
    expect(screen.getByTestId("chat-session-old")).toBeInTheDocument();
    expect(screen.queryByTestId("chat-session-new")).not.toBeInTheDocument();
    expect(screen.getByText("older answer")).toBeInTheDocument();
  });

  it("interacting with the detail pane pins the followed thread", () => {
    const { rerender } = render(<ChatPane sessions={twoSessions()} />);
    expect(screen.getByTestId("chat-session-new")).toBeInTheDocument();
    // Touching the detail pane (e.g. starting an edit) pins the thread…
    fireEvent.pointerDown(screen.getByTestId("chat-session-new"));
    // …so another client's submit can't yank it out from under the operator.
    rerender(
      <ChatPane
        sessions={[
          ...twoSessions(),
          session({ id: "intruder", created_at_ms: 3 }),
        ]}
      />,
    );
    expect(screen.getByTestId("chat-session-new")).toBeInTheDocument();
    expect(
      screen.queryByTestId("chat-session-intruder"),
    ).not.toBeInTheDocument();
  });

  it("clears a stale pin when the pinned thread disappears", () => {
    const { rerender } = render(<ChatPane sessions={twoSessions()} />);
    fireEvent.click(screen.getByTestId("chat-thread-item-old"));
    expect(screen.getByTestId("chat-session-old")).toBeInTheDocument();
    // A reconnect snapshot no longer carries the pinned session: fall back to
    // following the newest…
    const [old, newest] = twoSessions();
    rerender(<ChatPane sessions={[newest]} />);
    expect(screen.getByTestId("chat-session-new")).toBeInTheDocument();
    // …and the pin is cleared, so the id reappearing doesn't snap the view
    // back to it.
    rerender(<ChatPane sessions={[old, newest]} />);
    expect(screen.getByTestId("chat-session-new")).toBeInTheDocument();
    expect(screen.queryByTestId("chat-session-old")).not.toBeInTheDocument();
  });

  it("flags completed-unrewarded subtasks on the thread list item", () => {
    render(<ChatPane sessions={twoSessions()} />);
    expect(
      screen.getByTestId("chat-thread-awaiting-old").textContent,
    ).toBe("1");
    // The newer thread's subtask is pending — no decision owed yet.
    expect(
      screen.queryByTestId("chat-thread-awaiting-new"),
    ).not.toBeInTheDocument();
  });
});

describe("ChatPane prompt submission", () => {
  it("submits the prompt to /api/chat/submit and clears the box", async () => {
    const fetchMock = okFetch(session({ id: "s1" }));
    vi.stubGlobal("fetch", fetchMock);
    render(<ChatPane sessions={[]} />);

    const input = screen.getByTestId("chat-prompt-input") as HTMLTextAreaElement;
    const submit = screen.getByTestId("chat-submit") as HTMLButtonElement;
    expect(submit.disabled).toBe(true); // empty prompt is not submittable

    fireEvent.change(input, { target: { value: "do an ad-hoc task" } });
    expect(submit.disabled).toBe(false);
    fireEvent.click(submit);

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/chat/submit",
        expect.objectContaining({
          method: "POST",
          credentials: "same-origin",
          body: JSON.stringify({ prompt: "do an ad-hoc task" }),
        }),
      ),
    );
    await waitFor(() => expect(input.value).toBe(""));
  });

  it("does not submit a blank prompt", () => {
    const fetchMock = okFetch({});
    vi.stubGlobal("fetch", fetchMock);
    render(<ChatPane sessions={[]} />);
    fireEvent.change(screen.getByTestId("chat-prompt-input"), {
      target: { value: "   " },
    });
    fireEvent.submit(screen.getByTestId("chat-prompt-form"));
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("ChatPane per-subtask thumbs", () => {
  function completed() {
    return [
      session({
        id: "s1",
        subtasks: [subtask({ id: "st1", status: "completed", episode_id: "ep1" })],
      }),
    ];
  }

  it("offers thumbs only on a completed subtask", () => {
    render(
      <ChatPane
        sessions={[
          session({
            id: "s1",
            subtasks: [
              subtask({ id: "p", status: "pending" }),
              subtask({ id: "s", status: "streaming" }),
              subtask({ id: "d", status: "completed", episode_id: "ep" }),
            ],
          }),
        ]}
      />,
    );
    expect(screen.queryByTestId("chat-accept-p")).not.toBeInTheDocument();
    expect(screen.queryByTestId("chat-accept-s")).not.toBeInTheDocument();
    expect(screen.getByTestId("chat-accept-d")).toBeInTheDocument();
    expect(screen.getByTestId("chat-reject-d")).toBeInTheDocument();
    expect(screen.getByTestId("chat-edit-d")).toBeInTheDocument();
  });

  it("accept POSTs to the accept endpoint", async () => {
    const fetchMock = okFetch(
      subtask({ id: "st1", status: "curated", decision: "accept" }),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<ChatPane sessions={completed()} />);
    fireEvent.click(screen.getByTestId("chat-accept-st1"));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/chat/s1/st1/accept",
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });

  it("reject POSTs to the reject endpoint", async () => {
    const fetchMock = okFetch(
      subtask({ id: "st1", status: "curated", decision: "reject" }),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<ChatPane sessions={completed()} />);
    fireEvent.click(screen.getByTestId("chat-reject-st1"));
    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/chat/s1/st1/reject",
        expect.objectContaining({ method: "POST" }),
      ),
    );
  });

  it("edit opens an input, then POSTs the corrected answer", async () => {
    const fetchMock = okFetch(
      subtask({
        id: "st1",
        status: "curated",
        decision: "edit",
        corrected_answer: "the real answer",
      }),
    );
    vi.stubGlobal("fetch", fetchMock);
    render(<ChatPane sessions={completed()} />);

    fireEvent.click(screen.getByTestId("chat-edit-st1"));
    const submit = screen.getByTestId("chat-edit-submit-st1") as HTMLButtonElement;
    fireEvent.change(screen.getByTestId("chat-edit-input-st1"), {
      target: { value: "the real answer" },
    });
    expect(submit.disabled).toBe(false);
    fireEvent.click(submit);

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        "/api/chat/s1/st1/edit",
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
    render(<ChatPane sessions={completed()} />);
    fireEvent.click(screen.getByTestId("chat-edit-st1"));
    fireEvent.click(screen.getByTestId("chat-edit-cancel-st1"));
    expect(screen.queryByTestId("chat-edit-input-st1")).not.toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
