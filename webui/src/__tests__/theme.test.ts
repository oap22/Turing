// Theme tests (issue #382): the `data-theme` attribute, persistence, and the
// theme catalog's shape.
//
// This vitest/jsdom/Node combination doesn't provide a working
// `localStorage` out of the box (jsdom 29 defers to the platform's Web
// Storage, which Node gates behind `--localstorage-file`) — stub a minimal
// in-memory implementation for these tests rather than touch the shared
// `vite.config.ts`/`test-setup.ts`.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { applyTheme, THEMES } from "../desktop/theme";

function memoryStorage(): Storage {
  const store = new Map<string, string>();
  return {
    getItem: (k: string) => store.get(k) ?? null,
    setItem: (k: string, v: string) => void store.set(k, v),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
    key: (i: number) => Array.from(store.keys())[i] ?? null,
    get length() {
      return store.size;
    },
  } as Storage;
}

beforeEach(() => {
  vi.stubGlobal("localStorage", memoryStorage());
  delete document.documentElement.dataset.theme;
});

afterEach(() => {
  vi.unstubAllGlobals();
  delete document.documentElement.dataset.theme;
});

describe("applyTheme", () => {
  it("sets data-theme for a non-default theme and persists it", () => {
    applyTheme("nord");
    expect(document.documentElement.dataset.theme).toBe("nord");
    expect(localStorage.getItem("turing.theme")).toBe("nord");
  });

  it("removes data-theme for the default 'turing' theme", () => {
    applyTheme("nord");
    applyTheme("turing");
    expect(document.documentElement.dataset.theme).toBeUndefined();
    expect(localStorage.getItem("turing.theme")).toBe("turing");
  });
});

describe("THEMES", () => {
  it("has 9 unique theme ids", () => {
    expect(THEMES.length).toBe(9);
    expect(new Set(THEMES.map((t) => t.id)).size).toBe(9);
  });

  it("includes the default 'turing' theme", () => {
    expect(THEMES.some((t) => t.id === "turing")).toBe(true);
  });
});
