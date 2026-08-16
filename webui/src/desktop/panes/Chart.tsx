// Pure-SVG line chart — no library, no animation. Used by MetricsPane, one
// instance per series name (union across the selected runs).
//
// Responsive by measurement, not by `preserveAspectRatio="none"`: that
// attribute stretches text glyphs along with the lines, which makes tick
// labels illegible at odd aspect ratios. Instead a ResizeObserver measures
// the actual pixel size of the chart's own container and the svg's viewBox
// is set to exactly that size, so 1 viewBox unit === 1 CSS pixel and nothing
// is ever non-uniformly scaled.
//
// Visually this matches the rest of the terminal-aesthetic shell (dark
// pane background, theme-token colors) — an earlier pass tried a white
// plot background with a print-style palette for tick legibility, but that
// broke the dark theme everywhere else in the app, so it's reverted here.
// Legibility instead comes from the label gutters below (ticks rendered
// outside the plot area, never over a curve) plus a subtle plot-area border.

import { useEffect, useRef, useState } from "react";

interface Series {
  /**
   * Stable identity, independent of what the series is *called*. Optional so a
   * caller with genuinely unique labels needn't invent one; see `seriesKey`
   * for why relying on the label instead is not safe.
   */
  id?: string;
  label: string;
  points: Array<[number, number]>;
  color?: string;
}

// A React key has to be unique and stable. `label` is neither by construction:
// it is a display string, and MetricsPane composes it as `<run>/<series
// title>`, so two runs charting the same series produce the *same* key. That
// is not theoretical — with every run mislabelled by the loop name, a real
// session logged "Encountered two children with the same key" 442 times and
// React left the duplicate-keyed nodes mounted. Switching series with two runs
// pinned then accumulated 3 → 4 → 5 → 6 polylines, and the stale ones were
// rescaled onto the new axis: old `consumed_steps` and `speedup_ratio` data
// drawn as entirely plausible-looking `progress` curves. Fabricated data that
// reads as real is the worst failure this pane has, so the key must not depend
// on a display string even after the labels are fixed upstream.
//
// The two branches are namespaced apart so a caller that supplies `id` for
// some series and not others cannot collide an id of "0" with index 0.
function seriesKey(s: Series, index: number): string {
  return s.id !== undefined ? `id:${s.id}` : `idx:${index}`;
}

interface Props {
  series: Series[];
  height?: number;
}

const PALETTE = ["#7aa2f7", "#a7c080", "#ebbcba", "#fabd2f", "#88c0d0"];
const TICK_COLOR = "var(--t-dim)";
const PLOT_BORDER_COLOR = "var(--t-edge)";
const LEFT_GUTTER = 48;
const BOTTOM_GUTTER = 18;
// The top y-tick sits at the very top of the plot rect; with no gutter its
// label text (baseline at the rect's top edge, glyphs extending upward from
// there) rendered half above the svg's y=0 — clipped/overlapping whatever
// sat above the chart. A top gutter moves the whole plot rect down far
// enough that the top tick's full glyph height fits inside the svg.
const TOP_GUTTER = 16;

function colorFor(index: number): string {
  if (index === 0) return "var(--t-accent)";
  return PALETTE[(index - 1) % PALETTE.length];
}

// 4-significant-digit formatting for axis ticks and the latest-value label
// (e.g. 0.1523, 1.234, 12.35).
function formatSig(n: number, sig = 4): string {
  if (!Number.isFinite(n)) return "—";
  if (n === 0) return "0";
  return n.toPrecision(sig);
}

function useSize(fallback: { w: number; h: number }): [
  React.RefObject<HTMLDivElement | null>,
  { w: number; h: number },
] {
  const ref = useRef<HTMLDivElement | null>(null);
  const [size, setSize] = useState(fallback);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const observer = new ResizeObserver((entries) => {
      const entry = entries[0];
      if (!entry) return;
      const { width, height } = entry.contentRect;
      if (width > 0 && height > 0) setSize({ w: width, h: height });
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);
  return [ref, size];
}

export default function Chart({ series, height = 120 }: Props) {
  const [containerRef, size] = useSize({ w: 600, h: height });
  const W = size.w;
  const H = size.h;

  const allY = series.flatMap((s) => s.points.map(([, y]) => y));
  const minY = allY.length > 0 ? Math.min(...allY) : 0;
  const maxY = allY.length > 0 ? Math.max(...allY) : 1;
  const pad = (maxY - minY) * 0.05 || 1;
  const domainMin = minY - pad;
  const domainMax = maxY + pad;

  const allX = series.flatMap((s) => s.points.map(([x]) => x));
  const minX = allX.length > 0 ? Math.min(...allX) : 0;
  const maxX = allX.length > 0 ? Math.max(...allX) : 1;
  const xRange = maxX - minX || 1;
  const yRange = domainMax - domainMin || 1;

  // Axis-label gutters sit outside the plot rect so tick text never
  // overlaps a curve — including the top gutter, which keeps the top
  // y-tick's label fully inside the svg instead of clipped at y=0.
  const plotX0 = LEFT_GUTTER;
  const plotY0 = TOP_GUTTER;
  const plotW = Math.max(1, W - LEFT_GUTTER);
  const plotH = Math.max(1, H - TOP_GUTTER - BOTTOM_GUTTER);

  function toSvgX(x: number): number {
    return plotX0 + ((x - minX) / xRange) * plotW;
  }
  function toSvgY(y: number): number {
    return plotY0 + plotH - ((y - domainMin) / yRange) * plotH;
  }

  const ticks = [domainMin, (domainMin + domainMax) / 2, domainMax];

  return (
    <div className="flex h-full w-full flex-col">
      <div className="mb-1 flex flex-wrap gap-x-3 text-[12px] leading-none">
        {series.map((s, i) => {
          const latest = s.points.length > 0 ? s.points[s.points.length - 1][1] : null;
          return (
            <span key={seriesKey(s, i)} style={{ color: s.color ?? colorFor(i) }}>
              {s.label}
              {latest !== null ? `: ${formatSig(latest)}` : ""}
            </span>
          );
        })}
      </div>
      <div ref={containerRef} className="min-h-0 flex-1">
        <svg width="100%" height="100%" viewBox={`0 0 ${W} ${H}`} className="block" role="img">
          <rect
            data-testid="chart-plot-border"
            x={plotX0}
            y={plotY0}
            width={plotW}
            height={plotH}
            fill="none"
            stroke={PLOT_BORDER_COLOR}
            strokeWidth={1}
          />
          {ticks.map((t, i) => (
            <text key={i} x={4} y={toSvgY(t) + 4} fontSize={12} fill={TICK_COLOR}>
              {formatSig(t)}
            </text>
          ))}
          {minX !== maxX && (
            <>
              <text x={plotX0} y={H - 4} fontSize={12} fill={TICK_COLOR}>
                {minX}
              </text>
              <text x={plotX0 + plotW - 28} y={H - 4} fontSize={12} fill={TICK_COLOR}>
                {maxX}
              </text>
            </>
          )}
          {series.map((s, i) => {
            const points = s.points.map(([x, y]) => `${toSvgX(x)},${toSvgY(y)}`).join(" ");
            return (
              <polyline
                key={seriesKey(s, i)}
                // Keyed by the same rule as the React key, so the testid is
                // unique for the same reason the key is.
                data-testid={`chart-line-${seriesKey(s, i)}`}
                points={points}
                fill="none"
                stroke={s.color ?? colorFor(i)}
                strokeWidth={1.5}
              />
            );
          })}
        </svg>
      </div>
    </div>
  );
}
