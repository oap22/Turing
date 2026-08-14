// Keymap: mod=⌘ only (ctrl/alt must both be false — this keeps ⌥/⌃ chords
// free for terminal apps running inside a `term` pane). Two conflicts in the
// omarchy source map are resolved and documented in `Cheatsheet.tsx`:
// toggleDir moved off ⌘J (which collides with focus-down) to ⌘T; cheatsheet
// moved off ⌘K (which collides with focus-up) to ⌘/.

export type Dir4 = "left" | "right" | "up" | "down";

export type Action =
  | { type: "newTerm" }
  | { type: "newWs" }
  | { type: "close" }
  | { type: "focus"; dir: Dir4 }
  | { type: "swap"; dir: Dir4 }
  | { type: "ws"; i: number }
  | { type: "sendWs"; i: number }
  | { type: "zoom" }
  | { type: "toggleDir" }
  | { type: "resize"; delta: number }
  | { type: "cheatsheet" }
  | { type: "launcher" }
  // Back to the home page (the workstation list). ⌘0 rather than a letter:
  // every ⌘-letter worth having is spoken for, and 0 sits next to the ⌘1–9
  // workspace keys it is the "all of them, from outside" companion to.
  | { type: "home" };

// Window event the shell fires when a `term` pane becomes the focused leaf.
// A terminal's real focus target is xterm's own hidden textarea, which only
// TermPane can reach, so the shell asks rather than reaching in. Lives here
// (not in DesktopShell) because TermPane already imports this module and
// importing the shell would be circular.
export const PANE_FOCUS_EVENT = "turing:pane-focus";

export interface PaneFocusDetail {
  leafId: string;
}

// Which actions relocate the focused pane, and so should pull DOM focus and
// warp the cursor. Deliberately narrower than "changes the layout":
// `resize`, `zoom` and `toggleDir` reshape the focused pane without moving
// focus elsewhere, and warping the pointer on every resize keystroke would
// fight the user rather than follow them. `swap` counts because the focused
// pane physically moves to the neighbour's position even though its id
// doesn't change; `sendWs` counts because focus lands on a different pane in
// the workspace left behind.
export function movesFocus(a: Action): boolean {
  switch (a.type) {
    case "focus":
    case "swap":
    case "ws":
    case "sendWs":
    case "newTerm":
    case "close":
      return true;
    // `newWs` switches to a workspace that is empty by construction — there
    // is no leaf anywhere in it yet, so there is nothing for the cursor to
    // warp to. Contrast with `ws`, which returns true even though the target
    // workspace *might* also be empty: `ws` visits a workspace that could
    // already hold something, so DOM focus still needs to try. `newWs` never
    // has that case, so it is false unconditionally rather than "true but a
    // no-op most of the time."
    case "newWs":
      return false;
    default:
      return false;
  }
}

interface KeyEventLike {
  metaKey: boolean;
  shiftKey: boolean;
  ctrlKey: boolean;
  altKey: boolean;
  key: string;
}

const FOCUS_KEYS: Record<string, Dir4> = {
  ArrowLeft: "left",
  ArrowRight: "right",
  ArrowUp: "up",
  ArrowDown: "down",
  h: "left",
  l: "right",
  k: "up",
  j: "down",
};

// macOS delivers KeyboardEvent.key as the *shifted* character for ⌘⇧<key>
// chords: a shifted letter arrives uppercase ("H" for ⌘⇧h) and a shifted
// digit arrives as its shifted symbol on a US layout ("!" for ⌘⇧1). Map
// those symbols back to the workspace index they correspond to, while still
// accepting a plain shifted digit (some layouts/browsers report that
// instead). Covers 1..9 now that the workspace count can grow past five —
// ⌘⇧6 through ⌘⇧9 send to a workspace that only exists once ⌘N has created
// it, same as their unshifted counterparts.
const SHIFTED_DIGIT_SYMBOLS: Record<string, number> = {
  "!": 1,
  "@": 2,
  "#": 3,
  $: 4,
  "%": 5,
  "^": 6,
  "&": 7,
  "*": 8,
  "(": 9,
};

export function actionFor(e: KeyEventLike): Action | null {
  if (!e.metaKey || e.ctrlKey || e.altKey) return null;

  // Non-letter keys (Enter, arrows, digits, symbols) are unaffected by case;
  // only single-letter keys need normalizing before lookup.
  const key = e.key.length === 1 && /[a-zA-Z]/.test(e.key) ? e.key.toLowerCase() : e.key;

  if (key === "Enter") return { type: "newTerm" };
  if (key === "w") return { type: "close" };
  if (key === "f") return { type: "zoom" };
  if (key === "t") return { type: "toggleDir" };
  if (key === "p") return { type: "launcher" };
  if (key === "n") return { type: "newWs" };
  if (key === "0") return { type: "home" };
  if (key === "/") return { type: "cheatsheet" };
  if (key === "-") return { type: "resize", delta: -0.05 };
  if (key === "=") return { type: "resize", delta: 0.05 };

  const dir = FOCUS_KEYS[key];
  if (dir) return e.shiftKey ? { type: "swap", dir } : { type: "focus", dir };

  // 1..9, not 1..5: the workspace count can now grow past the seeded three
  // (via ⌘N, up to `MAX_WORKSPACES`), and every direct ⌘-digit chord should
  // be able to reach one once it exists. `switchWs`/`sendToWs` themselves
  // guard against an index past the *current* workspace count, so pressing
  // ⌘7 with only three workspaces open is a harmless no-op rather than
  // something this layer needs to know about.
  if (/^[1-9]$/.test(key)) {
    const i = Number(key) - 1;
    return e.shiftKey ? { type: "sendWs", i } : { type: "ws", i };
  }

  const shiftedDigit = SHIFTED_DIGIT_SYMBOLS[key];
  if (shiftedDigit !== undefined) {
    return { type: "sendWs", i: shiftedDigit - 1 };
  }

  return null;
}
