// ⌘/ overlay: a static table of the keymap, including the two bindings that
// moved off the omarchy defaults to avoid a collision (see keymap.ts).

interface Props {
  onClose: () => void;
}

const ROWS: ReadonlyArray<[string, string]> = [
  ["⌘ Return", "new terminal"],
  ["⌘ w", "close focused pane — or the workspace itself, when it's empty"],
  ["⌘ hjkl / arrows", "move focus"],
  ["⌘ shift + hjkl / arrows", "swap focused pane"],
  ["⌘ 1..9", "switch workspace"],
  ["⌘ shift + 1..9", "send focused pane to workspace"],
  ["⌘ n", "new workspace (past the seeded three, up to nine)"],
  ["⌘ f", "toggle zoom"],
  ["⌘ t", "toggle split direction (moved off ⌘j — collides with focus-down)"],
  ["⌘ -", "shrink focused pane"],
  ["⌘ =", "grow focused pane"],
  ["⌘ 0", "home — the workstation list"],
  ["⌘ p", "launcher — panes, runners and sessions"],
  ["⌘ /", "this cheatsheet (moved off ⌘k — collides with focus-up)"],
];

// Workstations (a.k.a. saved sessions) are named pane layouts. Home (⌘0) is
// the surface for the ones you keep coming back to — open, new, rename,
// remove — while ⌘p carries the in-flight commands that only make sense with a
// layout already on screen, chiefly "save what's here under a new name".
const SESSION_ROWS: ReadonlyArray<[string, string]> = [
  ["⌘ 0", "home — open, create, rename or remove a workstation"],
  ["⌘ p → save", "save the current layout as a new named session"],
  ["⌘ p → rename", "rename the current session"],
  ["⌘ p → switch to …", "load another saved session (fresh shells)"],
  ["⌘ p → delete", "delete the current session"],
];

export default function Cheatsheet({ onClose }: Props) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
      onClick={onClose}
      onKeyDown={(e) => {
        if (e.key === "Escape" || (e.metaKey && e.key === "/")) onClose();
      }}
    >
      <div
        role="dialog"
        aria-label="Keymap cheatsheet"
        onClick={(e) => e.stopPropagation()}
        className="w-[480px] border border-term-edge bg-term-panel p-3"
      >
        <div className="mb-2 text-[11px] uppercase tracking-widest text-term-dim">
          keymap
        </div>
        <table className="w-full text-xs">
          <tbody>
            {ROWS.map(([keys, desc]) => (
              <tr key={keys} className="border-t border-term-edge/60">
                <td className="whitespace-nowrap py-1 pr-3 font-mono text-term-accent">
                  {keys}
                </td>
                <td className="py-1 text-term-fg">{desc}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="mb-2 mt-3 text-[11px] uppercase tracking-widest text-term-dim">
          sessions
        </div>
        <table className="w-full text-xs">
          <tbody>
            {SESSION_ROWS.map(([keys, desc]) => (
              <tr key={keys} className="border-t border-term-edge/60">
                <td className="whitespace-nowrap py-1 pr-3 font-mono text-term-accent">
                  {keys}
                </td>
                <td className="py-1 text-term-fg">{desc}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <div className="mt-2 text-[10px] text-term-dim">
          in a focused terminal, ⌘k clears the buffer — use arrows/⌘↑ to focus upward from a
          terminal
        </div>
      </div>
    </div>
  );
}
