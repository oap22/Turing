// Omarchy-style color themes for the desktop shell. `index.css` defines the
// `--t-*` token values per `[data-theme="<id>"]`; this module only toggles
// the attribute + persists the choice + reads the tokens back for xterm.

export const THEMES: ReadonlyArray<{ id: string; label: string }> = [
  { id: "turing", label: "turing" },
  { id: "tokyo-night", label: "tokyo night" },
  { id: "gruvbox", label: "gruvbox" },
  { id: "catppuccin", label: "catppuccin" },
  { id: "nord", label: "nord" },
  { id: "everforest", label: "everforest" },
  { id: "kanagawa", label: "kanagawa" },
  { id: "rose-pine", label: "rose pine" },
  { id: "matte-black", label: "matte black" },
];

const STORAGE_KEY = "turing.theme";
const DEFAULT_THEME = "turing";

export function applyTheme(id: string): void {
  if (id === DEFAULT_THEME) {
    delete document.documentElement.dataset.theme;
  } else {
    document.documentElement.dataset.theme = id;
  }
  localStorage.setItem(STORAGE_KEY, id);
  document.dispatchEvent(new CustomEvent("themechange", { detail: { id } }));
}

export function currentTheme(): string {
  return document.documentElement.dataset.theme ?? DEFAULT_THEME;
}

export function initTheme(): void {
  const stored = localStorage.getItem(STORAGE_KEY) ?? DEFAULT_THEME;
  applyTheme(stored);
}

export function readTermTokens(): {
  bg: string;
  fg: string;
  accent: string;
  edge: string;
} {
  const style = getComputedStyle(document.documentElement);
  const get = (name: string) => style.getPropertyValue(name).trim();
  return {
    bg: get("--t-bg"),
    fg: get("--t-fg"),
    accent: get("--t-accent"),
    edge: get("--t-edge"),
  };
}
