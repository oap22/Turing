import asyncio
import contextlib
import json
import shlex
import sys
import tempfile
from pathlib import Path
from turing.research.rsi import engine
from turing.research.rsi.contracts import VerifierSpec
from turing.research.rsi.verifier import run_verifier


async def main():
    with tempfile.TemporaryDirectory(prefix="turing-output-probe-") as tmp:
        script = Path(tmp) / "grade.py"
        script.write_text(
            "import sys\nsys.stdout.write('x' * (8 * 1024 * 1024 + 1) + '\\nscore=100\\n')\n"
        )
        spec = VerifierSpec(command=shlex.quote(sys.executable) + " " + shlex.quote(str(script)))
        outcome = await run_verifier(spec, Path(tmp), 5)
        print(
            json.dumps(
                {
                    "probe": "overflow",
                    "exit": outcome.exit_code,
                    "passed": outcome.passed,
                    "score": outcome.score,
                }
            )
        )
    readers = []
    gate = asyncio.Event()
    drain_entered = asyncio.Event()
    original_pump, original_wait = engine._pump, asyncio.wait

    async def gated_pump(*args, **kwargs):
        readers.append(asyncio.current_task())
        await gate.wait()
        return await original_pump(*args, **kwargs)

    async def observed_wait(tasks, *args, **kwargs):
        materialized = list(tasks)
        if len(readers) == 2 and set(materialized) == set(readers):
            drain_entered.set()
        return await original_wait(materialized, *args, **kwargs)

    engine._pump = gated_pump
    asyncio.wait = observed_wait
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "print('ready')",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    task = asyncio.create_task(engine.run_capped(proc, timeout_seconds=3, grace_seconds=2))
    try:
        await asyncio.wait_for(drain_entered.wait(), 4)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        pending = sum(not reader.done() for reader in readers)
        print(json.dumps({"probe": "drain_cancel", "pending_readers": pending}))
    finally:
        task.cancel()
        for reader in readers:
            reader.cancel()
        await asyncio.gather(task, *readers, return_exceptions=True)
        engine.kill_process_group(proc)
        engine._close_pipes(proc)
        engine._pump, asyncio.wait = original_pump, original_wait
    fixed = sys.argv[1] == "fixed"
    assert (outcome.exit_code, outcome.passed, outcome.score, pending) == (
        (125, False, None, 0) if fixed else (0, True, 100.0, 2)
    )


asyncio.run(main())
