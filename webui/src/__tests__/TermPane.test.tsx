import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

const invCalls: Array<{ command: string; args?: Record<string, unknown> }> = [];
const eventHandlers = new Map<string, (payload: unknown) => void>();
let ptyExited = false;
let resizeFailsWhileActive = false;
let deferSpawn = false;
let resolveSpawn: ((id: number) => void) | null = null;

vi.mock("@xterm/addon-canvas", () => ({
  CanvasAddon: class {},
}));

vi.mock("@xterm/addon-fit", () => ({
  FitAddon: class {
    fit() {}
  },
}));

vi.mock("@xterm/xterm", () => ({
  Terminal: class {
    cols = 80;
    rows = 24;
    options = { theme: {} };
    loadAddon() {}
    attachCustomKeyEventHandler() {}
    open() {}
    onData() {}
    write() {}
    focus() {}
    dispose() {}
  },
}));

vi.mock("../desktop/tauri", () => ({
  inv: vi.fn((command: string, args?: Record<string, unknown>) => {
    invCalls.push({ command, args });
    if (command === "pty_spawn") {
      if (deferSpawn) {
        return new Promise<number>((resolve) => {
          resolveSpawn = resolve;
        });
      }
      return Promise.resolve(7);
    }
    if (command === "pty_resize" && (ptyExited || resizeFailsWhileActive)) {
      return Promise.reject(new Error("no such pty"));
    }
    return Promise.resolve(undefined);
  }),
  subscribe: vi.fn((event: string, cb: (payload: unknown) => void) => {
    eventHandlers.set(event, cb);
    return { ready: Promise.resolve(), unsubscribe: vi.fn() };
  }),
}));

vi.mock("../desktop/theme", () => ({
  readTermTokens: () => ({ bg: "#000", fg: "#fff", accent: "#0f0", edge: "#333" }),
}));

import TermPane from "../desktop/panes/TermPane";

vi.stubGlobal(
  "ResizeObserver",
  class {
    observe() {}
    disconnect() {}
  },
);
vi.stubGlobal("requestAnimationFrame", (callback: FrameRequestCallback) => {
  callback(0);
  return 0;
});
vi.stubGlobal("cancelAnimationFrame", () => {});

afterEach(() => {
  cleanup();
  invCalls.length = 0;
  eventHandlers.clear();
  ptyExited = false;
  resizeFailsWhileActive = false;
  deferSpawn = false;
  resolveSpawn = null;
});

describe("TermPane PTY lifecycle", () => {
  it("does not resize a PTY after its clean exit", async () => {
    const { rerender } = render(<TermPane leafId="leaf-1" visible />);

    await waitFor(() => {
      expect(invCalls.filter(({ command }) => command === "pty_spawn")).toHaveLength(1);
    });

    ptyExited = true;
    eventHandlers.get("pty-exit")?.({ id: 7, code: 0 });

    // A workspace switch tears down the visible-fit observer and mounts it
    // again when returning. Before the fix, that second fit used the stale
    // id and surfaced the backend's "no such pty" as a false red diagnostic.
    rerender(<TermPane leafId="leaf-1" visible={false} />);
    rerender(<TermPane leafId="leaf-1" visible />);

    await waitFor(() => {
      expect(invCalls.filter(({ command }) => command === "pty_resize")).toHaveLength(1);
    });
  });

  it("still reports a resize failure while the PTY is active", async () => {
    resizeFailsWhileActive = true;
    render(<TermPane leafId="leaf-1" visible />);

    await waitFor(() => {
      expect(document.querySelector('[data-testid="term-diagnostics"]')).toHaveTextContent(
        "terminal resize failed: no such pty",
      );
    });
  });

  it("does not resurrect a PTY that exits before spawn resolves", async () => {
    deferSpawn = true;
    render(<TermPane leafId="leaf-1" visible />);

    await waitFor(() => {
      expect(resolveSpawn).not.toBeNull();
    });
    ptyExited = true;
    eventHandlers.get("pty-exit")?.({ id: 7, code: 0 });
    resolveSpawn?.(7);

    await waitFor(() => {
      expect(document.querySelector('[data-testid="term-dead"]')).toBeInTheDocument();
      expect(invCalls.filter(({ command }) => command === "pty_resize")).toHaveLength(0);
    });
  });
});
