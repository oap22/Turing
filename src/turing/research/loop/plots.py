"""SVG plots rendered off the metrics the loop just wrote.

This is a thin, deterministic view over :mod:`turing.research.loop.results` —
it does not compute anything ``metrics.py`` or ``results.py`` did not already
decide. Given the points one attempt (or one round) already emitted, it draws
the same numbers the desktop's charts would draw, so the operator can look at
an SVG without opening the desktop app.

**Three matplotlib rules here are correctness requirements, not style:**

1. The backend is selected before any other matplotlib import that would pick
   one on its own — a GUI backend picked by default in a headless async
   process hangs or crashes the loop.
2. :mod:`matplotlib.pyplot` is never imported or called. ``pyplot`` keeps a
   global figure registry, which is unsafe the moment ``run_attempts``
   renders more than one attempt's plots concurrently — two coroutines
   sharing one global "current figure" is a real bug, not a hypothetical one.
   Every figure here is built and torn down through the object-oriented API
   (``Figure`` + ``FigureCanvasAgg``) so nothing is shared across calls.
3. Every ``savefig`` passes ``metadata={"Date": None}``. Matplotlib stamps a
   creation timestamp into SVG output by default, which would make the same
   input render a different file every time and defeats the byte-identical
   test this module is held to.

**Empty or unusable input writes nothing.** A plot whose required series is
absent from every point is skipped, not rendered as a blank figure — an empty
chart in the images pane reads as "the run produced nothing," which is a
different and false claim from "there was nothing to plot yet."
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import matplotlib

matplotlib.use("Agg")
# Text as SVG <text> elements, not vector paths — the round plot's cell labels
# ("speedup/practice") must be greppable strings in the file, not shapes with
# no string content. Matplotlib's default ("path") would make that
# unverifiable and would make every SVG larger for no benefit here.
matplotlib.rcParams["svg.fonttype"] = "none"
# Without a fixed salt, matplotlib derives every clip-path/marker id in the
# SVG from Python's ``id(obj)`` — a memory address that differs between
# interpreter runs even for byte-identical input. That alone would break the
# determinism this module is held to, independently of the ``Date`` metadata.
# A fixed salt makes those ids a hash of content instead.
matplotlib.rcParams["svg.hashsalt"] = "turing-research-loop-plots"

import structlog  # noqa: E402
from matplotlib.backends.backend_agg import FigureCanvasAgg  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

logger = structlog.get_logger(__name__)

__all__ = ["PLOT_FILENAMES", "render_attempt_plots", "render_round_plot"]

PLOT_FILENAMES = ("progress.svg", "cap.svg")
ROUND_PLOT_FILENAME = "scores.svg"

_SAVEFIG_KWARGS: dict[str, Any] = {"format": "svg", "metadata": {"Date": None}}


def _numeric_series(
    points: Sequence[Mapping[str, float]], key: str
) -> tuple[list[float], list[float]]:
    """Steps and values for ``key`` across the points where it is present."""
    steps: list[float] = []
    values: list[float] = []
    for point in points:
        if key in point and "step" in point:
            steps.append(point["step"])
            values.append(point[key])
    return steps, values


def _render_progress_plot(
    directory: Path,
    points: Sequence[Mapping[str, float]],
    *,
    forcing_series: str | None,
    target: float | None,
) -> Path | None:
    progress_steps, progress_values = _numeric_series(points, "progress")
    forcing_steps: list[float] = []
    forcing_values: list[float] = []
    if forcing_series is not None:
        forcing_steps, forcing_values = _numeric_series(points, forcing_series)

    if not progress_values and not forcing_values:
        logger.info(
            "research.plots.skipped",
            filename="progress.svg",
            reason="neither 'progress' nor the forcing series appear in any point",
        )
        return None

    fig = Figure()
    canvas = FigureCanvasAgg(fig)
    ax = fig.add_subplot(111)
    ax.set_title(f"{directory.name} — progress")
    ax.set_xlabel("step")
    ax.set_ylabel("progress (0 = baseline, 1 = target)")

    if progress_values:
        ax.plot(progress_steps, progress_values, label="progress", color="tab:blue")

    if forcing_values and forcing_series is not None:
        twin = ax.twinx()
        twin.set_ylabel(forcing_series)
        twin.plot(forcing_steps, forcing_values, label=forcing_series, color="tab:orange")
        if target is not None:
            twin.axhline(target, linestyle="--", color="tab:red", label="target")
        lines_a, labels_a = ax.get_legend_handles_labels()
        lines_b, labels_b = twin.get_legend_handles_labels()
        ax.legend(lines_a + lines_b, labels_a + labels_b)
    else:
        ax.legend()

    path = directory / "progress.svg"
    directory.mkdir(parents=True, exist_ok=True)
    canvas.print_figure(path, **_SAVEFIG_KWARGS)
    return path


def _render_cap_plot(directory: Path, points: Sequence[Mapping[str, float]]) -> Path | None:
    tokens_steps, tokens_values = _numeric_series(points, "tokens_used")
    wall_steps, wall_values = _numeric_series(points, "wall_clock_s")

    if not tokens_values and not wall_values:
        logger.info(
            "research.plots.skipped",
            filename="cap.svg",
            reason="neither 'tokens_used' nor 'wall_clock_s' appear in any point",
        )
        return None

    fig = Figure()
    canvas = FigureCanvasAgg(fig)
    ax = fig.add_subplot(111)
    ax.set_title(f"{directory.name} — cap consumption")
    ax.set_xlabel("step")

    lines: list = []
    labels: list[str] = []
    if tokens_values:
        ax.set_ylabel("tokens_used")
        (line,) = ax.plot(tokens_steps, tokens_values, label="tokens_used", color="tab:blue")
        lines.append(line)
        labels.append("tokens_used")
        _, tokens_cap_values = _numeric_series(points, "tokens_cap")
        if tokens_cap_values:
            ax.axhline(tokens_cap_values[-1], linestyle="--", color="tab:blue", alpha=0.5)

    if wall_values:
        wall_ax = ax.twinx() if tokens_values else ax
        if tokens_values:
            wall_ax.set_ylabel("wall_clock_s")
        else:
            ax.set_ylabel("wall_clock_s")
        (line,) = wall_ax.plot(wall_steps, wall_values, label="wall_clock_s", color="tab:orange")
        lines.append(line)
        labels.append("wall_clock_s")
        _, wall_cap_values = _numeric_series(points, "wall_clock_cap_s")
        if wall_cap_values:
            wall_ax.axhline(wall_cap_values[-1], linestyle="--", color="tab:orange", alpha=0.5)

    ax.legend(lines, labels)

    path = directory / "cap.svg"
    directory.mkdir(parents=True, exist_ok=True)
    canvas.print_figure(path, **_SAVEFIG_KWARGS)
    return path


def _render_attempt_plots_sync(
    directory: Path,
    points: Sequence[Mapping[str, float]],
    *,
    forcing_series: str | None,
    target: float | None,
) -> tuple[Path, ...]:
    if not points:
        logger.info(
            "research.plots.skipped",
            filename=", ".join(PLOT_FILENAMES),
            reason="no points to plot",
        )
        return ()

    written: list[Path] = []
    progress_path = _render_progress_plot(
        directory, points, forcing_series=forcing_series, target=target
    )
    if progress_path is not None:
        written.append(progress_path)
    cap_path = _render_cap_plot(directory, points)
    if cap_path is not None:
        written.append(cap_path)
    return tuple(written)


async def render_attempt_plots(
    directory: Path,
    points: Sequence[Mapping[str, float]],
    *,
    forcing_series: str | None,
    target: float | None,
) -> tuple[Path, ...]:
    """Render ``progress.svg`` and ``cap.svg`` for one attempt.

    ``points`` is the attempt's ``metrics.jsonl`` read back as finite-numeric
    dicts (see :func:`turing.research.loop.results.read_metrics_points`).
    Returns only the paths actually written — a plot whose series never
    appears in ``points`` is skipped, not written blank. Runs entirely inside
    :func:`asyncio.to_thread`: matplotlib's C extension is synchronous and a
    direct call here would stall every other attempt rendering concurrently.
    """
    return await asyncio.to_thread(
        _render_attempt_plots_sync,
        directory,
        points,
        forcing_series=forcing_series,
        target=target,
    )


def _render_round_plot_sync(directory: Path, cells: Sequence[Mapping[str, object]]) -> Path | None:
    if not cells:
        logger.info(
            "research.plots.skipped",
            filename=ROUND_PLOT_FILENAME,
            reason="no cells to plot",
        )
        return None

    labels = [
        str(cell["cell"]) if "cell" in cell else f"{cell.get('problem_type')}/{cell.get('split')}"
        for cell in cells
    ]
    # Bars are never summed, averaged, or pooled into a total bar — one bar per
    # cell only, matching the invariant metrics.py protects: a speedup ratio
    # and a leaderboard percentile do not share a scale and must never blend.
    # ``cells`` is Mapping[str, object] by contract (the caller builds it from
    # TypeScore/RoundDelta, which this module does not import) — the cast is
    # narrowing a value this module trusts the caller to have put a number in,
    # not a runtime guarantee.
    means = [float(cell["mean_score"]) for cell in cells]  # type: ignore[arg-type]

    fig = Figure()
    canvas = FigureCanvasAgg(fig)
    ax = fig.add_subplot(111)
    ax.set_title(f"{directory.name} — scores")
    ax.set_xlabel("cell")
    ax.set_ylabel("mean_score")
    x = range(len(labels))
    ax.bar(x, means, color="tab:blue")
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels, rotation=30, ha="right")

    path = directory / ROUND_PLOT_FILENAME
    directory.mkdir(parents=True, exist_ok=True)
    canvas.print_figure(path, **_SAVEFIG_KWARGS)
    return path


async def render_round_plot(
    directory: Path,
    cells: Sequence[Mapping[str, object]],
) -> Path | None:
    """Render ``scores.svg`` — one bar per cell, never pooled into a total.

    Returns ``None`` and writes nothing when ``cells`` is empty.
    """
    return await asyncio.to_thread(_render_round_plot_sync, directory, cells)
