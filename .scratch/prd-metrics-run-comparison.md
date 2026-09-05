# Metrics pane — run comparison

Spec for the desktop metrics pane's run selection, line colors, and clearing.
Decided by interview (see `feature-interview` skill); supersedes the ad-hoc
behavior shipped in `16cb50d`.

## Model

**One metric at a time, one line per run.** The tab row selects a single series
(`train_loss`, `grad_norm`, …); every selected run contributes exactly one line
to that chart. Comparing runs against each other is the pane's purpose.
Comparing metrics against each other is not — that's what the tabs are for.

## 1. Colors

1.1 Each theme defines 8 categorical series colors (`--t-series-1..8`).

1.2 Color is **sticky per run for the session**. A run claims a color the first
time it's plotted and keeps it until it leaves the chart — across metric-tab
switches and across other runs being added or removed. Removing a run from the
middle of the selection does not re-color the runs after it.

1.3 A run that leaves the chart frees its color; the next run to be plotted
takes the lowest-numbered free color.

1.4 Colors do not persist across restarts (see 5.1).

## 2. The run picker

2.1 One row per run. A run directory holding several metrics files
(`metrics.jsonl` beside `metrics.json`, or a nested copy) yields one row, backed
by the newest of those files. *(Shipped in `16cb50d`.)*

2.2 The `auto` row stays, and **resolves visibly**: it displays the run it
currently points at (`auto → 2026-08-13-shakespeare-char`).

2.3 The run auto is currently holding is shown **dimmed** in the list below, so
one run is never two live selections.

2.4 Clicking that dimmed row **promotes** it: the run becomes an explicit pin
and auto switches off. It will no longer be swapped out when a newer run lands.

2.5 **Auto is exactly one live line — always the newest run.** When a newer run
appears, auto switches to it and the previous run's line *disappears* and frees
its color. To keep a run across the handover, pin it (2.4) before it happens.

## 3. The cap

3.1 At most **8 lines**, because there are 8 colors. The invariant "no two lines
share a color" is absolute.

3.2 **Auto counts toward the 8.** With auto on, 7 more runs can be pinned.

3.3 At 8, the remaining picker rows go disabled with a hint to clear one first.
The header shows the count (`runs (8/8)`).

## 4. Removing lines

4.1 Every legend entry above the chart carries an `x` that drops that run's line
and frees its color.

4.2 The `x` on auto's line works too, and switches auto off — the legend `x`
always means "this line goes away", with no exceptions.

4.3 **`clear` empties the chart completely**, auto included. The pane shows
"no runs selected". Clear does not touch the selected metric tab.

## 5. Persistence

5.1 The run selection does not persist. Every time the pane opens it is in the
same state: **auto on, nothing pinned, not cleared.**

5.2 The selected metric tab continues to persist (`turing.metrics.series`), as
it does today.

## 6. `.viewer.json`

6.1 The control file keeps its authority: a write to it replaces the current
selection, so an agent finishing a sweep can steer the pane.

6.2 **Except after a manual clear.** `clear` puts the pane in a *sticky empty*
state that `.viewer.json` will not override. Clear means clear.

6.3 The next selection the user makes by hand releases the sticky state and
hands control back to the file.

6.4 Runs past the 8th in a `.viewer.json` `runs` list are dropped (3.1 wins).

## Decided without asking

Routine calls made while writing this up — say the word on any of them:

- A run claims the **lowest-numbered free color**, so with runs added and
  removed the palette stays packed at the low end rather than drifting upward.
- `.viewer.json` over-cap (6.4) keeps the **first 8 in file order** and ignores
  the rest silently rather than showing an error.
- The cap counter renders as `runs (n/8)` in the pane header next to `clear`.
- The dimmed auto-held row (2.3) stays keyboard-reachable, since clicking it is
  a real action (2.4) rather than a disabled control.
- `clear` stays disabled when nothing is selected and the pane isn't already in
  the sticky-empty state.
