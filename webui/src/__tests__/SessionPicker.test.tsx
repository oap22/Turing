// Startup picker: keyboard behaviour only. The store logic it drives is
// covered DOM-free in sessions.test.ts.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import SessionPicker from "../desktop/SessionPicker";
import { emptyLayout, openPane } from "../desktop/layout";
import { createSession, emptyStore, listSessions } from "../desktop/sessions";

// jsdom has no layout engine and so no scrollIntoView; the keep-the-cursor-
// visible effect calls it on every cursor move.
beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn();
});

// `globals: false` in vite.config.ts means RTL never registers its own
// auto-cleanup, so unmount explicitly (same as App.tabs.test.tsx).
afterEach(cleanup);

function twoSessions() {
  let store = createSession(emptyStore(), "bench", openPane(emptyLayout(), "term", undefined, "a"), 1000, "s1");
  store = createSession(store, "triage", openPane(emptyLayout(), "term", undefined, "b"), 2000, "s2");
  return listSessions(store);
}

function setup(activeId = "s2") {
  const onPick = vi.fn();
  const onSkip = vi.fn();
  render(
    <SessionPicker sessions={twoSessions()} activeId={activeId} onPick={onPick} onSkip={onSkip} />,
  );
  return { dialog: screen.getByRole("dialog"), onPick, onSkip };
}

describe("SessionPicker", () => {
  it("lists every session", () => {
    setup();
    expect(screen.getByText("bench")).toBeInTheDocument();
    expect(screen.getByText("triage")).toBeInTheDocument();
  });

  it("starts on the last-used session, so a bare return resumes it", () => {
    const { dialog, onPick } = setup("s1");
    fireEvent.keyDown(dialog, { key: "Enter" });
    expect(onPick).toHaveBeenCalledWith("s1");
  });

  it("moves the cursor with the arrow keys", () => {
    const { dialog, onPick } = setup("s2");
    fireEvent.keyDown(dialog, { key: "ArrowDown" });
    fireEvent.keyDown(dialog, { key: "Enter" });
    // Most-recent-first ordering: s2 then s1.
    expect(onPick).toHaveBeenCalledWith("s1");
  });

  it("clamps the cursor at both ends", () => {
    const { dialog, onPick } = setup("s2");
    fireEvent.keyDown(dialog, { key: "ArrowUp" });
    fireEvent.keyDown(dialog, { key: "ArrowDown" });
    fireEvent.keyDown(dialog, { key: "ArrowDown" });
    fireEvent.keyDown(dialog, { key: "Enter" });
    expect(onPick).toHaveBeenCalledWith("s1");
  });

  it("escapes to the last used session without picking", () => {
    const { dialog, onPick, onSkip } = setup();
    fireEvent.keyDown(dialog, { key: "Escape" });
    expect(onSkip).toHaveBeenCalled();
    expect(onPick).not.toHaveBeenCalled();
  });

  it("picks on click", () => {
    const { onPick } = setup();
    fireEvent.click(screen.getByText("bench"));
    expect(onPick).toHaveBeenCalledWith("s1");
  });
});
