// Wheel geometry for the flywheel pane (#390 part 2) — rounds arranged around
// a circle so the loop visibly closes on itself and accumulating rounds read
// as momentum.
//
// Pure geometry, no rendering: given a round count and a viewport it returns
// SVG paths. That keeps it testable, and it means a live append is a plain
// recompute — the pane re-renders on `fs-change` during a run and must not
// thrash. Note that appending genuinely re-partitions the whole ring: every
// wedge gets a new angle, because the wedges always divide a full turn
// between them. Nothing animates (the round index is the React key and there
// are no transitions), but the ring redistributes rather than growing into
// spare space.
//
// Constraints this has to survive, all from the issue:
//   - the pane's default preset is one third of a workspace column, and the
//     user can drag it to any aspect ratio, including a 3:1 letterbox;
//   - two rounds must not look like a broken circle, and fifty must not
//     produce unreadable spokes;
//   - it must stay readable *as data* — a wheel that answers "what happened
//     in round 7" worse than the timeline is a regression.

export interface WheelSegment {
  /** The round's own index — what the user calls it. */
  index: number;
  /** Position in the ring, 0-based, oldest first. */
  ordinal: number;
  startAngle: number;
  endAngle: number;
  midAngle: number;
  /** SVG path for the segment, an annulus wedge. */
  path: string;
  /** Where the index label goes, or null when the wedge is too thin for one. */
  label: { x: number; y: number } | null;
}

export interface WheelGeometry {
  cx: number;
  cy: number;
  rInner: number;
  rOuter: number;
  segments: WheelSegment[];
}

/** Below this the ring is illegible and the caller should stay on the list. */
const MIN_RADIUS = 34;
const PADDING = 6;
/** Ring thickness as a fraction of the outer radius. */
const THICKNESS = 0.34;
/** Angular gap between wedges, shrunk when wedges get thin. */
const MAX_GAP = 0.05;
/** A label needs roughly this much arc length to be worth drawing. */
const MIN_LABEL_ARC_PX = 15;

function point(
  cx: number,
  cy: number,
  r: number,
  angle: number,
): [number, number] {
  return [cx + r * Math.cos(angle), cy + r * Math.sin(angle)];
}

function fmt(n: number): string {
  return Number.isFinite(n) ? n.toFixed(2) : "0";
}

/**
 * An annulus wedge from `a0` to `a1`, drawn outer-arc clockwise then
 * inner-arc back.
 *
 * A wedge is never allowed to span a full turn: with a gap always subtracted,
 * `a1 - a0 < 2π` holds even for a single round, which keeps the SVG arc from
 * degenerating to a zero-length path when start and end coincide.
 */
export function wedgePath(
  cx: number,
  cy: number,
  rInner: number,
  rOuter: number,
  a0: number,
  a1: number,
): string {
  const large = a1 - a0 > Math.PI ? 1 : 0;
  const [x0, y0] = point(cx, cy, rOuter, a0);
  const [x1, y1] = point(cx, cy, rOuter, a1);
  const [x2, y2] = point(cx, cy, rInner, a1);
  const [x3, y3] = point(cx, cy, rInner, a0);
  return [
    `M ${fmt(x0)} ${fmt(y0)}`,
    `A ${fmt(rOuter)} ${fmt(rOuter)} 0 ${large} 1 ${fmt(x1)} ${fmt(y1)}`,
    `L ${fmt(x2)} ${fmt(y2)}`,
    `A ${fmt(rInner)} ${fmt(rInner)} 0 ${large} 0 ${fmt(x3)} ${fmt(y3)}`,
    "Z",
  ].join(" ");
}

/**
 * Build the ring.
 *
 * `indices` are the round indices in timeline order (oldest first); the ring
 * runs clockwise from 12 o'clock, so the newest round is the one that just
 * closed the gap back toward the top.
 *
 * Returns null when the box is too small to draw a legible ring — the caller
 * keeps showing the list rather than rendering a smudge.
 */
export function wheelGeometry(
  indices: number[],
  width: number,
  height: number,
): WheelGeometry | null {
  if (indices.length === 0) return null;
  // The short side governs: in a 3:1 letterbox the circle must fit the height,
  // not overflow it.
  const rOuter = Math.min(width, height) / 2 - PADDING;
  if (!Number.isFinite(rOuter) || rOuter < MIN_RADIUS) return null;

  const cx = width / 2;
  const cy = height / 2;
  const rInner = rOuter * (1 - THICKNESS);
  const rLabel = (rOuter + rInner) / 2;

  const step = (Math.PI * 2) / indices.length;
  // With many rounds a fixed gap would eat the wedge entirely; with few, a
  // proportional gap would be a canyon. Take whichever is smaller.
  const gap = Math.min(MAX_GAP, step * 0.25);
  const start = -Math.PI / 2;

  const segments = indices.map((index, ordinal) => {
    const a0 = start + ordinal * step;
    const a1 = a0 + step - gap;
    const mid = (a0 + a1) / 2;
    // Tangential room is the usual constraint, but at 3 and 9 o'clock the
    // text runs across the ring's thickness instead, so the band has to fit
    // it too — otherwise a three-digit index overflows the ring in a small
    // pane. Gate on whichever is tighter.
    const arcPx = Math.min(rLabel * (a1 - a0), rOuter - rInner);
    const [lx, ly] = point(cx, cy, rLabel, mid);
    return {
      index,
      ordinal,
      startAngle: a0,
      endAngle: a1,
      midAngle: mid,
      path: wedgePath(cx, cy, rInner, rOuter, a0, a1),
      // Past the point where labels stop fitting the ring keeps its shape and
      // drops the numbers; identity comes from hover and click instead.
      label: arcPx >= MIN_LABEL_ARC_PX ? { x: lx, y: ly } : null,
    };
  });

  return { cx, cy, rInner, rOuter, segments };
}
