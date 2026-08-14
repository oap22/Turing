// Regression coverage for issue: the tiling shell's viewport ResizeObserver
// was attached in a `useEffect(() => {...}, [])` on `containerRef`, but the
// container only exists in the DOM once `home` flips to false — and that
// effect runs exactly once, at first mount, while `home` is still true and
// the container isn't rendered yet. It silently never re-ran, so every pane
// laid out against the hardcoded 1600x900 placeholder for the rest of the
// session regardless of the real window size.
//
// jsdom has no layout engine, so a full pixel-accuracy assertion isn't
// possible here (`getBoundingClientRect()` always reports 0x0). What *is*
// testable without a real layout engine is the wiring itself: does the
// container actually get observed when the shell mounts, un-observed when it
// unmounts (Home), and observed again on the way back in — across Home →
// workstation → Home (⌘0) → another workstation, exactly the sequence the
// original bug broke.
//
// A `queue` pane is used for the test layout (rather than `term`, `metrics`,
// etc.) because it renders from `surface` props alone with no `inv()` calls
// into the Tauri bridge — see QueuePane.tsx — so it mounts cleanly under
// jsdom without a Tauri runtime to answer those calls.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import DesktopShell, { type Surface } from "../DesktopShell";
import { emptyLayout, openPane } from "../layout";
import { createSession, emptyStore, RESEED_KEY, saveSessions } from "../sessions";

// Node 20+ ships its own experimental global `localStorage`, which throws
// without a `--localstorage-file` flag — it shadows jsdom's window.localStorage
// in this test environment, so DesktopShell's (and sessions.ts's) direct
// `localStorage` references need a stubbed-in, in-memory replacement rather
// than the real thing.
function memoryStorage(): Storage {
  const data = new Map<string, string>();
  return {
    getItem: (k) => data.get(k) ?? null,
    setItem: (k, v) => void data.set(k, v),
    removeItem: (k) => void data.delete(k),
    clear: () => data.clear(),
    key: (i) => Array.from(data.keys())[i] ?? null,
    get length() {
      return data.size;
    },
  };
}

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn();
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

class MockResizeObserver {
  static instances: MockResizeObserver[] = [];
  observe = vi.fn();
  unobserve = vi.fn();
  disconnect = vi.fn();
  constructor(public callback: ResizeObserverCallback) {
    MockResizeObserver.instances.push(this);
  }
}

function seedSession() {
  const layout = openPane(emptyLayout(), "queue", undefined, "q1");
  const store = createSession(emptyStore(), "bench", layout, 1000, "s1");
  saveSessions(localStorage, store);
  // Park the profile in its steady state. This test is about *when* the
  // ResizeObserver attaches, not about the one-time layout reseed — leaving
  // the marker unset would hand the shell a reseeded preset instead of the
  // single `queue` pane seeded above, and that pane was chosen deliberately
  // (see the header) because it mounts under jsdom without a Tauri runtime.
  localStorage.setItem(RESEED_KEY, "1");
}

function surface(): Surface {
  return {
    queue: { items: [] },
    chat: { sessions: [] },
    obs: {
      graphState: { nodes: {}, edges: {} },
      highlightedEdge: null,
      specsRows: [],
      liveTrace: [],
      onTraceSelect: vi.fn(),
    },
    wsBadge: null,
  };
}

beforeEach(() => {
  vi.stubGlobal("localStorage", memoryStorage());
  MockResizeObserver.instances = [];
  vi.stubGlobal("ResizeObserver", MockResizeObserver);
  seedSession();
});

describe("DesktopShell viewport observer wiring", () => {
  it("attaches on entering a workstation, detaches on ⌘0, and re-attaches on the way back in", () => {
    render(<DesktopShell surface={surface()} />);

    // Boots on Home — the tiling container isn't mounted yet, so nothing
    // should have been observed.
    expect(screen.getByRole("dialog", { name: "Workstations" })).toBeInTheDocument();
    expect(MockResizeObserver.instances).toHaveLength(0);

    // Open the saved workstation (Home → shell).
    fireEvent.click(screen.getByText("bench"));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(MockResizeObserver.instances).toHaveLength(1);
    expect(MockResizeObserver.instances[0].observe).toHaveBeenCalledTimes(1);
    expect(MockResizeObserver.instances[0].disconnect).not.toHaveBeenCalled();

    // Back to Home (the ⌂ button in the top bar, same action as ⌘0).
    fireEvent.click(screen.getByTitle("workstations (⌘0)"));
    expect(screen.getByRole("dialog", { name: "Workstations" })).toBeInTheDocument();
    expect(MockResizeObserver.instances[0].disconnect).toHaveBeenCalledTimes(1);

    // Re-enter the same workstation. The bug this guards against was the
    // observer never re-attaching after the *first* Home→shell transition —
    // a second entry is exactly where the old code stayed broken forever.
    fireEvent.click(screen.getByText("bench"));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(MockResizeObserver.instances).toHaveLength(2);
    expect(MockResizeObserver.instances[1].observe).toHaveBeenCalledTimes(1);
  });
});
