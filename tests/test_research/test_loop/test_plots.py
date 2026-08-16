"""Deterministic SVG plots off the points an attempt or round already emitted."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from turing.research.loop.plots import (
    PLOT_FILENAMES,
    ROUND_PLOT_FILENAME,
    render_attempt_plots,
    render_round_plot,
)

if TYPE_CHECKING:
    from pathlib import Path


class TestRenderAttemptPlots:
    async def test_progress_and_cap_svgs_are_written_and_well_formed(self, tmp_path: Path) -> None:
        points = (
            {"step": 1, "progress": 0.0, "score": 1.0, "tokens_used": 10, "wall_clock_s": 1.0},
            {"step": 2, "progress": 0.5, "score": 1.5, "tokens_used": 20, "wall_clock_s": 2.0},
            {"step": 3, "progress": 1.0, "score": 2.0, "tokens_used": 30, "wall_clock_s": 3.0},
        )
        paths = await render_attempt_plots(tmp_path, points, forcing_series="score", target=2.0)
        assert set(p.name for p in paths) == set(PLOT_FILENAMES)
        for filename in PLOT_FILENAMES:
            data = (tmp_path / filename).read_bytes()
            assert data
            assert b"<svg" in data[:1000]

    async def test_rendering_the_same_points_twice_is_byte_identical(self, tmp_path: Path) -> None:
        """This is the test that pins ``metadata={"Date": None}`` and the hash salt."""
        points = (
            {"step": 1, "progress": 0.0, "score": 1.0, "tokens_used": 10, "wall_clock_s": 1.0},
            {"step": 2, "progress": 1.0, "score": 2.0, "tokens_used": 20, "wall_clock_s": 2.0},
        )
        # Same basename under two different parents — the title embeds
        # ``directory.name``, so a differing basename would make an
        # unrelated field the cause of a byte mismatch, not the thing this
        # test is pinning.
        first = tmp_path / "a" / "attempt"
        second = tmp_path / "b" / "attempt"
        await render_attempt_plots(first, points, forcing_series="score", target=2.0)
        await render_attempt_plots(second, points, forcing_series="score", target=2.0)
        for filename in PLOT_FILENAMES:
            assert (first / filename).read_bytes() == (second / filename).read_bytes()

    async def test_empty_points_write_nothing_and_return_an_empty_tuple(
        self, tmp_path: Path
    ) -> None:
        paths = await render_attempt_plots(tmp_path, (), forcing_series="score", target=1.0)
        assert paths == ()
        for filename in PLOT_FILENAMES:
            assert not (tmp_path / filename).exists()

    async def test_points_lacking_tokens_used_and_wall_clock_produce_no_cap_svg(
        self, tmp_path: Path
    ) -> None:
        points = ({"step": 1, "progress": 0.0, "score": 1.0},)
        paths = await render_attempt_plots(tmp_path, points, forcing_series="score", target=1.0)
        assert (tmp_path / "cap.svg") not in paths
        assert not (tmp_path / "cap.svg").exists()
        assert (tmp_path / "progress.svg") in paths

    async def test_a_single_point_still_renders_both_plots(self, tmp_path: Path) -> None:
        points = ({"step": 1, "progress": 0.5, "tokens_used": 5, "wall_clock_s": 1.0},)
        paths = await render_attempt_plots(tmp_path, points, forcing_series=None, target=None)
        assert len(paths) == 2
        for path in paths:
            assert path.stat().st_size > 0

    async def test_all_identical_values_render_without_error(self, tmp_path: Path) -> None:
        """Zero range on every series must not raise (matplotlib auto-pads a flat axis)."""
        row: dict[str, Any] = {
            "step": 1,
            "progress": 0.5,
            "tokens_used": 100,
            "wall_clock_s": 5.0,
        }
        points = (row, dict(row), dict(row))
        paths = await render_attempt_plots(tmp_path, points, forcing_series=None, target=None)
        assert len(paths) == 2
        for path in paths:
            assert path.stat().st_size > 0

    async def test_a_none_target_omits_the_reference_line_but_still_renders(
        self, tmp_path: Path
    ) -> None:
        points = (
            {"step": 1, "progress": 0.0, "score": 1.0},
            {"step": 2, "progress": 1.0, "score": 2.0},
        )
        no_target_dir = tmp_path / "no_target"
        with_target_dir = tmp_path / "with_target"
        paths_no_target = await render_attempt_plots(
            no_target_dir, points, forcing_series="score", target=None
        )
        paths_with_target = await render_attempt_plots(
            with_target_dir, points, forcing_series="score", target=2.0
        )
        assert len(paths_no_target) == 1  # progress.svg only — no cap series present
        assert len(paths_with_target) == 1
        # A drawn reference line changes the file; the two are not the same bytes.
        assert (no_target_dir / "progress.svg").read_bytes() != (
            with_target_dir / "progress.svg"
        ).read_bytes()

    async def test_nan_and_infinite_values_do_not_crash_and_stay_deterministic(
        self, tmp_path: Path
    ) -> None:
        points = (
            {"step": 1, "progress": 0.0, "score": 1.0, "tokens_used": 10, "wall_clock_s": 1.0},
            {
                "step": 2,
                "progress": float("nan"),
                "score": float("inf"),
                "tokens_used": 20,
                "wall_clock_s": float("-inf"),
            },
            {"step": 3, "progress": 1.0, "score": 2.0, "tokens_used": 30, "wall_clock_s": 3.0},
        )
        first = tmp_path / "a" / "attempt"
        second = tmp_path / "b" / "attempt"
        paths_first = await render_attempt_plots(first, points, forcing_series="score", target=2.0)
        paths_second = await render_attempt_plots(
            second, points, forcing_series="score", target=2.0
        )
        assert len(paths_first) == len(paths_second) == 2
        for filename in PLOT_FILENAMES:
            assert (first / filename).read_bytes() == (second / filename).read_bytes()

    async def test_a_skipped_plot_leaves_an_earlier_render_on_disk_untouched(
        self, tmp_path: Path
    ) -> None:
        """The invariant ``runner._rotate_stale_metrics`` exists to compensate for.

        "Skipped" means *nothing is written*, so a second render into a
        directory a first render already used leaves the first plot in place,
        byte for byte, while the plots that do have their series are rewritten.
        For an attempt directory that means one chart from each of two
        different attempts sitting side by side, which is what rotation now
        prevents by moving the previous attempt's plots into ``prior-N/``
        before the re-drive starts.

        Do not "fix" this by blanking or deleting a skipped plot here: an
        empty chart in the images pane reads as "the run produced nothing",
        which is the false claim the skip exists to avoid (see the module
        docstring). This test pins the behaviour so the trade stays visible to
        whoever reads it next.
        """
        scored = (
            {"step": 1, "progress": 0.0, "score": 1.0, "tokens_used": 10, "wall_clock_s": 1.0},
            {"step": 2, "progress": 1.0, "score": 2.0, "tokens_used": 20, "wall_clock_s": 2.0},
        )
        first = await render_attempt_plots(tmp_path, scored, forcing_series="score", target=2.0)
        assert set(p.name for p in first) == set(PLOT_FILENAMES)
        progress_before = (tmp_path / "progress.svg").read_bytes()
        cap_before = (tmp_path / "cap.svg").read_bytes()

        # A second attempt that never verified: no progress, no raw score, but
        # the cap series is on every line it managed to write.
        unscored = (
            {"step": 0, "tokens_used": 99, "wall_clock_s": 9.0},
            {"step": 0, "tokens_used": 99, "wall_clock_s": 9.0},
        )
        second = await render_attempt_plots(tmp_path, unscored, forcing_series="score", target=2.0)
        assert [p.name for p in second] == ["cap.svg"]
        assert (tmp_path / "cap.svg").read_bytes() != cap_before
        assert (tmp_path / "progress.svg").read_bytes() == progress_before

    async def test_a_very_large_point_count_renders_without_error(self, tmp_path: Path) -> None:
        points = tuple(
            {
                "step": i,
                "progress": min(1.0, i / 20_000),
                "tokens_used": i,
                "wall_clock_s": float(i),
            }
            for i in range(20_000)
        )
        paths = await render_attempt_plots(tmp_path, points, forcing_series=None, target=None)
        assert len(paths) == 2
        for path in paths:
            assert path.stat().st_size > 0


class TestRenderRoundPlot:
    async def test_empty_cells_return_none_and_write_nothing(self, tmp_path: Path) -> None:
        result = await render_round_plot(tmp_path, ())
        assert result is None
        assert not (tmp_path / ROUND_PLOT_FILENAME).exists()

    async def test_two_cells_both_appear_as_text_and_are_never_pooled(self, tmp_path: Path) -> None:
        cells = (
            {
                "cell": "speedup/practice",
                "problem_type": "speedup",
                "split": "practice",
                "mean_score": 1.5,
                "n": 3,
                "correctness_passes": 3,
            },
            {
                "cell": "leaderboard_percentile/held-out",
                "problem_type": "leaderboard_percentile",
                "split": "held-out",
                "mean_score": 42.0,
                "n": 2,
                "correctness_passes": 1,
            },
        )
        path = await render_round_plot(tmp_path, cells)
        assert path == tmp_path / ROUND_PLOT_FILENAME
        text = path.read_text(encoding="utf-8")
        assert "speedup/practice" in text
        assert "leaderboard_percentile/held-out" in text

    async def test_rendering_the_same_cells_twice_is_byte_identical(self, tmp_path: Path) -> None:
        cells = (
            {
                "cell": "speedup/practice",
                "problem_type": "speedup",
                "split": "practice",
                "mean_score": 1.5,
                "n": 3,
            },
        )
        first = tmp_path / "a" / "round-00"
        second = tmp_path / "b" / "round-00"
        await render_round_plot(first, cells)
        await render_round_plot(second, cells)
        assert (first / ROUND_PLOT_FILENAME).read_bytes() == (
            second / ROUND_PLOT_FILENAME
        ).read_bytes()
