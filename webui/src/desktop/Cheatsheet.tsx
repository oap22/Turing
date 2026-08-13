// ⌘/ overlay: a static table of the keymap, including the two bindings that
// moved off the omarchy defaults to avoid a collision (see keymap.ts).

interface Props {
  onClose: () => void;
}

const ROWS: ReadonlyArray<[string, string]> = [
  ["⌘ Return", "new terminal"],
  ["⌘ w", "close focused pane"],
  ["⌘ hjkl / arrows", "move focus"],
  ["⌘ shift + hjkl / arrows", "swap focused pane"],
  ["⌘ 1..5", "switch workspace"],
  ["⌘ shift + 1..5", "send focused pane to workspace"],
  ["⌘ f", "toggle zoom"],
  ["⌘ t", "toggle split direction (moved off ⌘j — collides with focus-down)"],
  ["⌘ -", "shrink focused pane"],
  ["⌘ =", "grow focused pane"],
  ["⌘ p", "launcher"],
  ["⌘ /", "this cheatsheet (moved off ⌘k — collides with focus-up)"],
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
        <div className="mt-2 text-[10px] text-term-dim">
          in a focused terminal, ⌘k clears the buffer — use arrows/⌘↑ to focus upward from a
          terminal
        </div>
      </div>
    </div>
  );
}
