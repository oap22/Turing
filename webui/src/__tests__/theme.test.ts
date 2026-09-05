// Theme tests (issue #382): the `data-theme` attribute, persistence, and the
// theme catalog's shape.
//
// This vitest/jsdom/Node combination doesn't provide a working
// `localStorage` out of the box (jsdom 29 defers to the platform's Web
// Storage, which Node gates behind `--localstorage-file`) — stub a minimal
// in-memory implementation for these tests rather than touch the shared
// `vite.config.ts`/`test-setup.ts`.

import { readFileSync } from "node:fs";
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

// Chart.tsx strokes curves with `var(--t-series-N)`, so a theme that forgets
// the palette would draw invisible lines — and repeated values would put two
// overlaid runs in the same color, which is exactly what the palette exists to
// prevent. Neither jsdom nor a `?raw` import gives the test the stylesheet
// (vitest stubs CSS imports to ""), so read the source file — vitest's cwd is
// the `webui` package root.
describe("chart series palette", () => {
  const css = readFileSync("src/index.css", "utf8");

  function blockFor(themeId: string): string {
    const selector = themeId === "turing" ? ":root" : `[data-theme="${themeId}"]`;
    const start = css.indexOf(`${selector} {`);
    expect(start, `no CSS block for ${themeId}`).toBeGreaterThan(-1);
    return css.slice(start, css.indexOf("}", start));
  }

  for (const theme of THEMES) {
    it(`${theme.id} defines 8 distinct series colors`, () => {
      const block = blockFor(theme.id);
      const colors = Array.from({ length: 8 }, (_, i) => {
        const match = block.match(new RegExp(`--t-series-${i + 1}:\\s*([^;]+);`));
        expect(match, `${theme.id} is missing --t-series-${i + 1}`).not.toBeNull();
        return match![1].trim();
      });
      expect(new Set(colors).size).toBe(8);
      // Series 1 is the theme accent, so single-run charts keep their color.
      const accent = block.match(/--t-accent:\s*([^;]+);/)?.[1].trim();
      expect(colors[0]).toBe(accent);
    });
  }
});
