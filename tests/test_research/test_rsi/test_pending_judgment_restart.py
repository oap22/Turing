"""Restart coverage for pending RSI self-edit judgments."""

from __future__ import annotations

from turing.research.rsi.engine import FakeEngine
from turing.research.rsi.loop import DEFAULT_SCAFFOLD, SCAFFOLD_FILENAME

from .test_loop import StubSelfEdit, _config, _events, _loop, score_step


async def test_pending_edit_is_judged_when_restart_completes_its_window(rsi_dirs) -> None:
    """A pending edit uses all post-edit rounds, including rounds before restart."""
    results = rsi_dirs.results
    step = StubSelfEdit(rsi_dirs.sandbox, text="BAD\n")
    cfg = _config(rsi_dirs, rounds=3, self_edit_every=2, self_edit_budget=1)

    first = await _loop(
        rsi_dirs,
        FakeEngine(script=[score_step(value, results=results) for value in (5, 5, 1)]),
        config=cfg,
        self_edit=step,
    ).run()
    assert first.self_edits == 1
    assert first.rollbacks == 0

    second = await _loop(
        rsi_dirs,
        FakeEngine(script=[score_step(1, results=results)]),
        config=_config(rsi_dirs, rounds=1, self_edit_every=2, self_edit_budget=1),
        self_edit=step,
        verifier=None,
    ).run()

    assert second.rollbacks == 1
    assert second.self_edits == 0
    assert len(step.seen) == 1
    events = _events(results)
    assert [event["event"] for event in events] == ["self_edit", "rollback"]
    assert events[1]["round"] == 4
    assert (rsi_dirs.sandbox / SCAFFOLD_FILENAME).read_text() == DEFAULT_SCAFFOLD
