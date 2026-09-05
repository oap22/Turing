import asyncio
import json

import pytest

from turing.local_fleet import run_fleet
from turing.worker.executor.loop import ExecutorOutput


@pytest.mark.asyncio
async def test_each_task_once_bounded_per_worker_with_overlap(tmp_path):
    active = set()
    peak = 0
    seen = []
    both_started = asyncio.Event()

    def factory(worker_id):
        async def execute(task):
            nonlocal peak
            assert worker_id not in active
            active.add(worker_id)
            peak = max(peak, len(active))
            if len(active) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), 1)
            await asyncio.sleep(0)
            seen.append(task.prompt)
            active.remove(worker_id)
            return ExecutorOutput.completed(output=task.prompt, tokens_used=1, model="test")

        return execute

    output = tmp_path / "run"
    rows = await run_fleet(
        ["a", "b", "c", "d"],
        workers=2,
        timeout=2,
        output=output,
        executor_factory=factory,
        mode="test",
    )
    assert sorted(seen) == ["a", "b", "c", "d"]
    assert peak == 2
    assert [r["worker_id"] for r in rows] == ["local-1", "local-2"] * 2
    assert all(r["status"] == "COMPLETED" for r in rows)
    assert len((output / "results.jsonl").read_text().splitlines()) == 4
    progress = json.loads((output / "metrics.jsonl").read_text().splitlines()[-1])
    assert progress["completed"] == progress["total_steps"] == 4


@pytest.mark.asyncio
async def test_failure_timeout_and_later_task_survive(tmp_path):
    def factory(_):
        async def execute(task):
            if task.prompt == "fail":
                raise RuntimeError("expected failure")
            if task.prompt == "hang":
                await asyncio.sleep(10)
            return ExecutorOutput.completed(output="ok", tokens_used=0, model="test")

        return execute

    rows = await run_fleet(
        ["fail", "hang", "ok"],
        workers=1,
        timeout=0.1,
        output=tmp_path / "run",
        executor_factory=factory,
        mode="test",
    )
    assert [r["status"] for r in rows] == ["FAILED", "TIMED_OUT", "COMPLETED"]


@pytest.mark.asyncio
async def test_existing_results_never_overwritten(tmp_path):
    sentinel = tmp_path / "sentinel"
    sentinel.write_text("keep")
    with pytest.raises(FileExistsError):
        await run_fleet(
            ["task"],
            workers=1,
            timeout=1,
            output=tmp_path,
            executor_factory=lambda _: None,
            mode="test",
        )
    assert sentinel.read_text() == "keep"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "workers,timeout,prompts", [(0, 1, ["a"]), (9, 1, ["a"]), (1, 0, ["a"]), (1, 1, [""])]
)
async def test_invalid_budgets_do_not_create_output(tmp_path, workers, timeout, prompts):
    output = tmp_path / "run"
    with pytest.raises(ValueError):
        await run_fleet(
            prompts,
            workers=workers,
            timeout=timeout,
            output=output,
            executor_factory=lambda _: None,
            mode="test",
        )
    assert not output.exists()
