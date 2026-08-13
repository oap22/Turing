// Regression tests for the pty-output buffering race (issue #382 follow-up).
//
// The bug: TermPane awaited `pty_spawn` and only then subscribed to
// `pty-output`. Tauri drops events that have no listener, so everything the
// shell printed before the spawn response arrived was lost — with a working
// TERM that is the entire oh-my-posh prompt, so both panes rendered blank.

import { describe, expect, it } from "vitest";
import { createPtyStream, MAX_PENDING_CHUNKS_PER_ID } from "../desktop/ptyStream";

function harness() {
  const written: string[] = [];
  const exits: Array<number | null> = [];
  const stream = createPtyStream({
    write: (d) => written.push(d),
    onExit: (code) => exits.push(code),
  });
  return { stream, written, exits };
}

describe("createPtyStream", () => {
  it("replays output that arrived before the id was known", () => {
    const { stream, written } = harness();
    // The prompt burst, emitted while pty_spawn was still in flight.
    stream.output(7, "\x1b[32m❯\x1b[0m ");
    stream.output(7, "prompt");
    expect(written).toEqual([]);

    stream.adopt(7);
    expect(written).toEqual(["\x1b[32m❯\x1b[0m ", "prompt"]);
  });

  it("preserves order across the buffered/live boundary", () => {
    const { stream, written } = harness();
    stream.output(1, "a");
    stream.output(1, "b");
    stream.adopt(1);
    stream.output(1, "c");
    stream.output(1, "d");
    expect(written.join("")).toBe("abcd");
  });

  it("ignores other panes' ptys, buffered or live", () => {
    const { stream, written } = harness();
    stream.output(1, "mine-early");
    stream.output(2, "theirs-early");
    stream.adopt(1);
    stream.output(2, "theirs-live");
    stream.output(1, "mine-live");
    expect(written).toEqual(["mine-early", "mine-live"]);
  });

  it("drops other ids' buffers on adopt so they cannot leak", () => {
    const { stream, written } = harness();
    stream.output(2, "theirs");
    stream.adopt(1);
    // Adopting 1 must not later replay 2's buffer under any circumstance.
    stream.adopt(2);
    expect(written).toEqual([]);
    expect(stream.adoptedId()).toBe(1);
  });

  it("reports an exit that landed before the id was known, after its output", () => {
    const { stream, written, exits } = harness();
    stream.output(3, "bye");
    stream.exit(3, 0);
    expect(exits).toHaveLength(0);

    stream.adopt(3);
    expect(written).toEqual(["bye"]);
    expect(exits).toEqual([0]);
  });

  it("reports a live exit only for the adopted id", () => {
    const { stream, exits } = harness();
    stream.adopt(5);
    stream.exit(9, 0);
    expect(exits).toHaveLength(0);
    stream.exit(5, 0);
    expect(exits).toHaveLength(1);
  });

  it("carries the exit code through, buffered and live", () => {
    const early = harness();
    early.stream.exit(3, 42);
    early.stream.adopt(3);
    expect(early.exits).toEqual([42]);

    const live = harness();
    live.stream.adopt(4);
    live.stream.exit(4, null);
    expect(live.exits).toEqual([null]);
  });

  it("caps the pre-adoption buffer, keeping the most recent chunks", () => {
    const { stream, written } = harness();
    const total = MAX_PENDING_CHUNKS_PER_ID + 50;
    for (let i = 0; i < total; i++) stream.output(1, `${i};`);
    stream.adopt(1);

    expect(written).toHaveLength(MAX_PENDING_CHUNKS_PER_ID);
    // Oldest dropped, newest retained — the current frame survives.
    expect(written[written.length - 1]).toBe(`${total - 1};`);
    expect(written[0]).toBe(`${total - MAX_PENDING_CHUNKS_PER_ID};`);
  });

  it("is inert before adoption and never double-replays", () => {
    const { stream } = harness();
    expect(stream.adoptedId()).toBeNull();
    stream.output(1, "x");
    stream.adopt(1);
    const { stream: s2, written } = harness();
    s2.output(1, "x");
    s2.adopt(1);
    s2.adopt(1);
    expect(written).toEqual(["x"]);
  });
});
