// Keymap tests (issue #382): every binding, shift variants, mod-less events
// rejected, and the two documented omarchy-collision resolutions.

import { describe, expect, it } from "vitest";
import { actionFor, movesFocus } from "../desktop/keymap";

function key(k: string, opts: Partial<{ metaKey: boolean; shiftKey: boolean; ctrlKey: boolean; altKey: boolean }> = {}) {
  return {
    metaKey: opts.metaKey ?? true,
    shiftKey: opts.shiftKey ?? false,
    ctrlKey: opts.ctrlKey ?? false,
    altKey: opts.altKey ?? false,
    key: k,
  };
}

describe("actionFor", () => {
  it("⌘Return opens a new terminal", () => {
    expect(actionFor(key("Enter"))).toEqual({ type: "newTerm" });
  });

  it("⌘w closes the focused pane", () => {
    expect(actionFor(key("w"))).toEqual({ type: "close" });
  });

  it("hjkl and arrows move focus in the omarchy directions", () => {
    expect(actionFor(key("h"))).toEqual({ type: "focus", dir: "left" });
    expect(actionFor(key("l"))).toEqual({ type: "focus", dir: "right" });
    expect(actionFor(key("k"))).toEqual({ type: "focus", dir: "up" });
    expect(actionFor(key("j"))).toEqual({ type: "focus", dir: "down" });
    expect(actionFor(key("ArrowLeft"))).toEqual({ type: "focus", dir: "left" });
    expect(actionFor(key("ArrowRight"))).toEqual({ type: "focus", dir: "right" });
    expect(actionFor(key("ArrowUp"))).toEqual({ type: "focus", dir: "up" });
    expect(actionFor(key("ArrowDown"))).toEqual({ type: "focus", dir: "down" });
  });

  it("shift + hjkl/arrows swaps instead of focusing", () => {
    expect(actionFor(key("h", { shiftKey: true }))).toEqual({ type: "swap", dir: "left" });
    expect(actionFor(key("ArrowDown", { shiftKey: true }))).toEqual({ type: "swap", dir: "down" });
  });

  it("real macOS ⌘⇧h chord (uppercase key) still swaps", () => {
    // macOS delivers the shifted character for ⌘⇧<letter> chords, so a real
    // browser event carries key:"H", not key:"h".
    expect(actionFor(key("H", { shiftKey: true }))).toEqual({ type: "swap", dir: "left" });
  });

  it("lowercase key with shiftKey (some layouts/browsers) still swaps", () => {
    expect(actionFor(key("h", { shiftKey: true }))).toEqual({ type: "swap", dir: "left" });
  });

  it("real macOS ⌘⇧1 chord (shifted symbol key) sends to workspace 1", () => {
    // macOS/US layout delivers the shifted symbol for ⌘⇧<digit> chords, so a
    // real browser event carries key:"!", not key:"1".
    expect(actionFor(key("!", { shiftKey: true }))).toEqual({ type: "sendWs", i: 0 });
  });

  it("real macOS shifted-digit symbols map to workspaces 1..5", () => {
    const symbols = ["!", "@", "#", "$", "%"];
    symbols.forEach((sym, idx) => {
      expect(actionFor(key(sym, { shiftKey: true }))).toEqual({ type: "sendWs", i: idx });
    });
  });

  it("digits 1..5 switch workspace, shift+digit sends to it", () => {
    for (let i = 1; i <= 5; i++) {
      expect(actionFor(key(String(i)))).toEqual({ type: "ws", i: i - 1 });
      expect(actionFor(key(String(i), { shiftKey: true }))).toEqual({
        type: "sendWs",
        i: i - 1,
      });
    }
  });

  it("⌘f toggles zoom", () => {
    expect(actionFor(key("f"))).toEqual({ type: "zoom" });
  });

  it("⌘t toggles split direction (moved off ⌘j to avoid the focus-down collision)", () => {
    expect(actionFor(key("t"))).toEqual({ type: "toggleDir" });
  });

  it("⌘- and ⌘= resize", () => {
    expect(actionFor(key("-"))).toEqual({ type: "resize", delta: -0.05 });
    expect(actionFor(key("="))).toEqual({ type: "resize", delta: 0.05 });
  });

  it("⌘p opens the launcher", () => {
    expect(actionFor(key("p"))).toEqual({ type: "launcher" });
  });

  it("⌘0 goes home, without disturbing the ⌘1–5 workspace keys", () => {
    expect(actionFor(key("0"))).toEqual({ type: "home" });
    expect(actionFor(key("1"))).toEqual({ type: "ws", i: 0 });
  });

  it("⌘/ opens the cheatsheet (moved off ⌘k to avoid the focus-up collision)", () => {
    expect(actionFor(key("/"))).toEqual({ type: "cheatsheet" });
    expect(actionFor(key("k"))).toEqual({ type: "focus", dir: "up" });
  });

  it("plain ⌘k is focus-up unconditionally — no pane may claim it", () => {
    // TermPane once handled ⌘k as "clear the terminal buffer", which could
    // never run: DesktopShell's capturing window listener swallows every
    // chord actionFor recognizes before xterm's listener (on its own hidden
    // textarea) sees it. The keymap is exclusive; a pane-local chord has to
    // be one actionFor returns null for. ⌃/⌥ are reserved for that.
    expect(actionFor(key("k"))).toEqual({ type: "focus", dir: "up" });
    expect(actionFor(key("K", { shiftKey: true }))).toEqual({ type: "swap", dir: "up" });
    expect(actionFor(key("l", { ctrlKey: true, metaKey: false }))).toBeNull();
  });

  it("leaves the chords TermPane handles locally unbound", () => {
    // Copy/paste in a terminal (⌘c/⌘v) work precisely because the keymap
    // does not recognize them, so the shell never preventDefaults them.
    expect(actionFor(key("c"))).toBeNull();
    expect(actionFor(key("v"))).toBeNull();
  });

  it("returns null for non-mod events", () => {
    expect(actionFor(key("w", { metaKey: false }))).toBeNull();
    expect(actionFor(key("Enter", { metaKey: false }))).toBeNull();
  });

  it("returns null when ctrl or alt rides along with meta", () => {
    expect(actionFor(key("w", { ctrlKey: true }))).toBeNull();
    expect(actionFor(key("w", { altKey: true }))).toBeNull();
  });

  it("returns null for unbound keys, including ⌘c", () => {
    expect(actionFor(key("c"))).toBeNull();
    expect(actionFor(key("z"))).toBeNull();
    expect(actionFor(key("v"))).toBeNull();
  });
});

describe("movesFocus", () => {
  it("is true for actions that relocate the focused pane", () => {
    expect(movesFocus({ type: "focus", dir: "left" })).toBe(true);
    expect(movesFocus({ type: "swap", dir: "down" })).toBe(true);
    expect(movesFocus({ type: "ws", i: 2 })).toBe(true);
    expect(movesFocus({ type: "sendWs", i: 3 })).toBe(true);
    expect(movesFocus({ type: "newTerm" })).toBe(true);
    expect(movesFocus({ type: "close" })).toBe(true);
  });

  it("is false for actions that reshape without moving focus", () => {
    // Warping the pointer on every resize keystroke would fight the user.
    expect(movesFocus({ type: "resize", delta: 0.02 })).toBe(false);
    expect(movesFocus({ type: "zoom" })).toBe(false);
    expect(movesFocus({ type: "toggleDir" })).toBe(false);
  });

  it("is false for the overlay actions", () => {
    expect(movesFocus({ type: "launcher" })).toBe(false);
    expect(movesFocus({ type: "cheatsheet" })).toBe(false);
  });
});
