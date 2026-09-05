export type XY = [number, number];

/** No argument spreading: long runs must not overflow the JS call stack. */
export function boundsOf(series: { points: XY[] }[]) {
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (const { points } of series) {
    for (const [x, y] of points) {
      minX = Math.min(minX, x); maxX = Math.max(maxX, x);
      minY = Math.min(minY, y); maxY = Math.max(maxY, y);
    }
  }
  return minX === Infinity
    ? { minX: 0, maxX: 1, minY: 0, maxY: 1 }
    : { minX, maxX, minY, maxY };
}

/** Keep first/last and both extrema in each screen column, in source order.
 * Only display geometry is reduced; callers retain every original sample.
 * Nonmonotonic x values use the full path so backtracking is never fabricated.
 */
export function pixelEnvelope(points: XY[], minX: number, maxX: number, width: number): XY[] {
  const columns = Math.max(1, Math.ceil(width));
  if (points.length <= columns * 4) return points;
  for (let i = 1; i < points.length; i++) {
    if (points[i][0] < points[i - 1][0]) return points;
  }
  const range = maxX - minX || 1;
  const result: XY[] = [];
  let start = 0;
  while (start < points.length) {
    const column = Math.floor(((points[start][0] - minX) / range) * columns);
    let end = start + 1, low = start, high = start;
    while (end < points.length && Math.floor(((points[end][0] - minX) / range) * columns) === column) {
      if (points[end][1] < points[low][1]) low = end;
      if (points[end][1] > points[high][1]) high = end;
      end++;
    }
    const indices = [start, low, high, end - 1].sort((a, b) => a - b);
    let previous = -1;
    for (const index of indices) {
      if (index !== previous) result.push(points[index]);
      previous = index;
    }
    start = end;
  }
  return result;
}
