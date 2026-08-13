// Hyprland-style window gaps, applied in the *render* layer only.
//
// `layout.ts` stays gap-free on purpose: its rects are the tiling geometry
// (they must tile the viewport exactly, with no slack), and the splitter
// boundaries, dwindle split-direction choice and spatial focus navigation all
// read those rects. Baking gaps into that math would make every rect
// gap-dependent and break the exhaustive pixel-exact suite for no gain.
// Instead `DesktopShell` insets the viewport by `GAPS_OUT_PX` before handing
// it to `rects()`, then insets each leaf rect by half of `GAPS_IN_PX` when
// positioning the pane div — two neighbouring panes each give up half the
// gap, so the visible space between them is exactly `GAPS_IN_PX`.

import type { Rect } from "./layout";

// Space between the tiling area and the window edge.
export const GAPS_OUT_PX = 8;
// Space between two adjacent panes (each contributes half).
export const GAPS_IN_PX = 6;

// Shrink `rect` by `inset` on every side. Clamped at zero so a pane squeezed
// smaller than the gap degenerates to a zero-size box rather than inverting.
export function applyGaps(rect: Rect, inset: number): Rect {
  return {
    x: rect.x + inset,
    y: rect.y + inset,
    w: Math.max(0, rect.w - inset * 2),
    h: Math.max(0, rect.h - inset * 2),
  };
}
