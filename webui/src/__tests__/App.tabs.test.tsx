// Tabbed-view shell tests (issue #358): one view at a time (Queue default),
// hash-synced navigation via click and 1/2/3 hotkeys, hotkeys suppressed while
// typing, and action-needed badges driven by the live reducers.

import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import App from "../App";
import type { ChatSnapshotFrame, QueueItem, QueueSnapshotFrame } from "../ws";

let frameSink: ((f: unknown) => void) | null = null;

vi.mock("../ws", () => ({
  connectGatewayWS: ({ onFrame }: { onFrame: (f: unknown) => void }) => {
    frameSink = onFrame;
    return () => {
      frameSink = null;
    };
  },
}));
vi.mock("../CallGraphCanvas", () => ({ default: () => null }));

beforeEach(() => {
  window.location.hash = "";
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(JSON.stringify({ peers: [], count: 0 }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
    ),
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  cleanup();
  frameSink = null;
  window.history.replaceState(null, "", "/");
  window.location.hash = "";
});

function queueItem(overrides: Partial<QueueItem> = {}): QueueItem {
  return {
    id: "q1",
    prompt: "a research question",
    specialty: "research",
    status: "proposed",
    origin_task_id: null,
    origin_question_id: null,
    proposed_by: "jetson-1",
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

describe("App — tabbed views", () => {
  it("defaults to the queue view with the other views unmounted", () => {
    render(<App />);
    expect(screen.getByTestId("queue-pane")).toBeInTheDocument();
    expect(screen.queryByTestId("chat-pane")).not.toBeInTheDocument();
    expect(screen.queryByTestId("observability-view")).not.toBeInTheDocument();
    expect(screen.getByTestId("tab-queue").getAttribute("aria-current")).toBe(
      "page",
    );
  });

  it("starts on the view named by the URL hash", () => {
    window.location.hash = "#chat";
    render(<App />);
    expect(screen.getByTestId("chat-pane")).toBeInTheDocument();
    expect(screen.queryByTestId("queue-pane")).not.toBeInTheDocument();
  });

  it("clicking a tab switches the view and syncs the hash", () => {
    render(<App />);
    fireEvent.click(screen.getByTestId("tab-chat"));
    expect(screen.getByTestId("chat-pane")).toBeInTheDocument();
    expect(screen.queryByTestId("queue-pane")).not.toBeInTheDocument();
    expect(window.location.hash).toBe("#chat");
  });

  it("hotkeys 1/2/3 switch views", () => {
    render(<App />);
    fireEvent.keyDown(window, { key: "3" });
    expect(screen.getByTestId("observability-view")).toBeInTheDocument();
    expect(window.location.hash).toBe("#obs");
    fireEvent.keyDown(window, { key: "1" });
    expect(screen.getByTestId("queue-pane")).toBeInTheDocument();
    expect(window.location.hash).toBe("#queue");
  });

  it("hotkeys are suppressed while typing in a form control", () => {
    window.location.hash = "#chat";
    render(<App />);
    const input = screen.getByTestId("chat-prompt-input");
    fireEvent.keyDown(input, { key: "1" });
    expect(screen.getByTestId("chat-pane")).toBeInTheDocument();
    expect(screen.queryByTestId("queue-pane")).not.toBeInTheDocument();
  });

  it("queue badge counts proposed + drafted items; curated items don't count", async () => {
    render(<App />);
    await waitFor(() => expect(frameSink).not.toBeNull());
    const snapshot: QueueSnapshotFrame = {
      type: "queue.snapshot",
      items: [
        queueItem({ id: "a", status: "proposed" }),
        queueItem({ id: "b", status: "drafted" }),
        queueItem({ id: "c", status: "in-flight" }),
        queueItem({ id: "d", status: "curated", decision: "accept" }),
      ],
      timestamp_ms: 1,
    };
    frameSink!(snapshot);
    await waitFor(() => {
      expect(screen.getByTestId("tab-badge-queue").textContent).toBe("2");
    });
  });

  it("chat badge counts completed-unrewarded subtasks across sessions", async () => {
    render(<App />);
    await waitFor(() => expect(frameSink).not.toBeNull());
    const snapshot: ChatSnapshotFrame = {
      type: "chat.snapshot",
      sessions: [
        {
          id: "s1",
          prompt: "p",
          specialty: "research",
          created_at_ms: 1,
          subtasks: [
            {
              id: "st1",
              session_id: "s1",
              index: 0,
              specialty: "research",
              prompt: "",
              content: "done",
              status: "completed",
              episode_id: "ep1",
              consumed_upstreams: [],
              created_at_ms: 1,
              completed_at_ms: 2,
              curated_at_ms: null,
              decision: null,
              corrected_answer: null,
            },
            {
              id: "st2",
              session_id: "s1",
              index: 1,
              specialty: "research",
              prompt: "",
              content: "judged",
              status: "curated",
              episode_id: "ep2",
              consumed_upstreams: [],
              created_at_ms: 1,
              completed_at_ms: 2,
              curated_at_ms: 3,
              decision: "accept",
              corrected_answer: null,
            },
          ],
        },
      ],
      timestamp_ms: 1,
    };
    frameSink!(snapshot);
    await waitFor(() => {
      expect(screen.getByTestId("tab-badge-chat").textContent).toBe("1");
    });
    expect(screen.queryByTestId("tab-badge-obs")).not.toBeInTheDocument();
  });
});
