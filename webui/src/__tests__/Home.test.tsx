// The home page: keyboard behaviour, the destructive-action confirm, and the
// per-row summaries. The store logic it drives is covered DOM-free in
// sessions.test.ts; this file only asserts what the page itself decides.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import Home, { paneSummary, relativeTime, workspaceMarks } from "../desktop/Home";
import { emptyLayout, openPane } from "../desktop/layout";
import { createSession, emptyStore, listSessions, type Session } from "../desktop/sessions";

// jsdom has no layout engine and so no scrollIntoView; the keep-the-cursor-
// visible effect calls it on every cursor move.
beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn();
});

// `globals: false` in vite.config.ts means RTL never registers its own
// auto-cleanup, so unmount explicitly (same as App.tabs.test.tsx).
afterEach(cleanup);

function twoSessions(): Session[] {
  let store = createSession(
    emptyStore(),
    "bench",
    openPane(emptyLayout(), "term", undefined, "a"),
    1000,
    "s1",
  );
  store = createSession(
    store,
    "triage",
    openPane(emptyLayout(), "metrics", undefined, "b"),
    2000,
    "s2",
  );
  return listSessions(store);
}

function setup(sessions: Session[] = twoSessions(), activeId: string | null = "s2") {
  const handlers = {
    onOpen: vi.fn(),
    onCreate: vi.fn(),
    onRename: vi.fn(),
    onRemove: vi.fn(),
    onResume: vi.fn(),
    onThemeChange: vi.fn(),
  };
  render(
    <Home sessions={sessions} activeId={activeId} now={2000} theme="turing" {...handlers} />,
  );
  return { dialog: screen.getByRole("dialog"), ...handlers };
}

describe("Home", () => {
  it("lists every workstation", () => {
    setup();
    expect(screen.getByText("bench")).toBeInTheDocument();
    expect(screen.getByText("triage")).toBeInTheDocument();
  });

  it("starts on the last-used workstation, so a bare return resumes it", () => {
    const { dialog, onOpen } = setup(twoSessions(), "s1");
    fireEvent.keyDown(dialog, { key: "Enter" });
    expect(onOpen).toHaveBeenCalledWith("s1");
  });

  it("moves the cursor with the arrow keys and with j/k", () => {
    const { dialog, onOpen } = setup();
    fireEvent.keyDown(dialog, { key: "ArrowDown" });
    fireEvent.keyDown(dialog, { key: "Enter" });
    // Most-recent-first ordering: s2 then s1.
    expect(onOpen).toHaveBeenCalledWith("s1");

    fireEvent.keyDown(dialog, { key: "k" });
    fireEvent.keyDown(dialog, { key: "Enter" });
    expect(onOpen).toHaveBeenLastCalledWith("s2");
  });

  it("clamps the cursor at both ends", () => {
    const { dialog, onOpen } = setup();
    fireEvent.keyDown(dialog, { key: "ArrowUp" });
    fireEvent.keyDown(dialog, { key: "ArrowDown" });
    fireEvent.keyDown(dialog, { key: "ArrowDown" });
    fireEvent.keyDown(dialog, { key: "Enter" });
    expect(onOpen).toHaveBeenCalledWith("s1");
  });

  it("escapes to the last used workstation without opening one from the list", () => {
    const { dialog, onOpen, onResume } = setup();
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(onResume).toHaveBeenCalled();
    expect(onOpen).not.toHaveBeenCalled();
  });

  it("opens on click", () => {
    const { onOpen } = setup();
    fireEvent.click(screen.getByText("bench"));
    expect(onOpen).toHaveBeenCalledWith("s1");
  });

  it("ignores ⌘-chords so the shell's own keymap still works", () => {
    const { dialog, onResume } = setup();
    fireEvent.keyDown(dialog, { key: "Escape", metaKey: true });
    expect(onResume).not.toHaveBeenCalled();
  });

  describe("removing", () => {
    it("confirms before removing, and removes on return", () => {
      const { dialog, onRemove } = setup();
      fireEvent.keyDown(dialog, { key: "d" });
      expect(onRemove).not.toHaveBeenCalled();
      expect(screen.getByText(/remove\?/)).toBeInTheDocument();
      fireEvent.keyDown(dialog, { key: "Enter" });
      expect(onRemove).toHaveBeenCalledWith("s2");
    });

    it("backs out of the confirm on escape without resuming", () => {
      const { dialog, onRemove, onResume } = setup();
      fireEvent.keyDown(dialog, { key: "Backspace" });
      fireEvent.keyDown(dialog, { key: "Escape" });
      expect(onRemove).not.toHaveBeenCalled();
      expect(onResume).not.toHaveBeenCalled();
      // Back on the list: escape now resumes.
      fireEvent.keyDown(dialog, { key: "Escape" });
      expect(onResume).toHaveBeenCalled();
    });

    it("confirms the row whose × was clicked, not the cursor's", () => {
      const { dialog, onRemove } = setup();
      fireEvent.click(screen.getByLabelText("remove bench"));
      fireEvent.keyDown(dialog, { key: "Enter" });
      expect(onRemove).toHaveBeenCalledWith("s1");
    });
  });

  describe("creating", () => {
    it("prompts for a name and creates on return", () => {
      const { dialog, onCreate } = setup();
      fireEvent.keyDown(dialog, { key: "n" });
      const input = screen.getByLabelText("new workstation name");
      fireEvent.change(input, { target: { value: "  writing  " } });
      fireEvent.keyDown(input, { key: "Enter" });
      expect(onCreate).toHaveBeenCalledWith("writing");
    });

    it("refuses an empty name rather than creating an unnamed workstation", () => {
      const { dialog, onCreate } = setup();
      fireEvent.keyDown(dialog, { key: "n" });
      fireEvent.keyDown(screen.getByLabelText("new workstation name"), { key: "Enter" });
      expect(onCreate).not.toHaveBeenCalled();
    });

    it("offers creation as the only thing return can do when nothing is saved", () => {
      const { dialog, onCreate, onOpen } = setup([], null);
      expect(screen.getByText(/no workstations yet/)).toBeInTheDocument();
      fireEvent.keyDown(dialog, { key: "Enter" });
      expect(onOpen).not.toHaveBeenCalled();
      fireEvent.change(screen.getByLabelText("new workstation name"), {
        target: { value: "first" },
      });
      fireEvent.keyDown(screen.getByLabelText("new workstation name"), { key: "Enter" });
      expect(onCreate).toHaveBeenCalledWith("first");
    });
  });

  it("renames the selected workstation, seeded with its current name", () => {
    const { dialog, onRename } = setup();
    fireEvent.keyDown(dialog, { key: "r" });
    const input = screen.getByLabelText<HTMLInputElement>("new name");
    expect(input.value).toBe("triage");
    fireEvent.change(input, { target: { value: "triage-2" } });
    fireEvent.keyDown(input, { key: "Enter" });
    expect(onRename).toHaveBeenCalledWith("s2", "triage-2");
  });

  describe("row summaries", () => {
    it("counts panes per type, busiest first", () => {
      let layout = openPane(emptyLayout(), "term", undefined, "a");
      layout = openPane(layout, "term", undefined, "b");
      layout = openPane(layout, "metrics", undefined, "c");
      const session = createSession(emptyStore(), "x", layout, 1, "s").sessions.s;
      expect(paneSummary(session)).toBe("2 term · metrics");
      expect(workspaceMarks(session)).toBe("●····");
    });

    it("says so when a workstation has no panes at all", () => {
      const session = createSession(emptyStore(), "x", emptyLayout(), 1, "s").sessions.s;
      expect(paneSummary(session)).toBe("empty");
      expect(workspaceMarks(session)).toBe("·····");
    });

    it("reports recency coarsely", () => {
      const min = 60_000;
      expect(relativeTime(0, 30_000)).toBe("just now");
      expect(relativeTime(0, 5 * min)).toBe("5m ago");
      expect(relativeTime(0, 180 * min)).toBe("3h ago");
      expect(relativeTime(0, 48 * 60 * min)).toBe("2d ago");
    });
  });
});
