"""Bounded, hardware-independent coordinator and local inference workers.

Uses the existing signed dispatch and WorkerLoop contracts in one process.
No shell tools, cloud fallback, autonomous planner, or hardware provisioning.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from turing.coordinator.dispatch import SubtaskDispatch
from turing.coordinator.dispatch.client import SubtaskDispatchClient
from turing.llm.base import Message, Role
from turing.llm.local import OllamaProvider
from turing.transport.bus import InMemoryBus
from turing.transport.signed_transport import SignedTransport
from turing.transport.signer import MessageSigner
from turing.worker.executor.loop import Executor, ExecutorOutput, WorkerLoop

if TYPE_CHECKING:
    from collections.abc import Callable


async def run_fleet(
    prompts: list[str],
    *,
    workers: int,
    timeout: float,
    output: Path,
    executor_factory: Callable[[str], Executor],
    mode: str,
) -> list[dict]:
    """Dispatch directly, serially per worker, with at most workers tasks active."""
    if not 1 <= workers <= 8 or not 0 < timeout <= 3600:
        raise ValueError("workers must be 1..8 and timeout must be >0 and <=3600 seconds")
    if not prompts or any(not p.strip() for p in prompts):
        raise ValueError("at least one nonempty prompt is required")
    output.mkdir(parents=True, exist_ok=False)
    clock = lambda: int(time.time() * 1000)  # noqa: E731
    run_id = str(uuid.uuid4())
    ids = [f"local-{i + 1}" for i in range(workers)]
    signers = {name: MessageSigner.generate() for name in ["coordinator", *ids]}
    trusted = {name: signer.public_key for name, signer in signers.items()}
    bus = InMemoryBus()
    transports = {
        name: SignedTransport(bus=bus, signer=signer, trusted_keys=trusted, now_ms=clock)
        for name, signer in signers.items()
    }
    client = SubtaskDispatchClient(
        transport=transports["coordinator"], sender_id="coordinator", now_ms=clock
    )
    for worker_id in ids:
        worker = WorkerLoop(
            transport=transports[worker_id],
            worker_id=worker_id,
            specialties=("general",),
            executor=executor_factory(worker_id),
            now_ms=clock,
        )
        await worker.start()
    (output / "workflow.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "mode": mode,
                "workers": ids,
                "tasks": len(prompts),
                "transport": "signed-in-process",
                "timeout_seconds": timeout,
                "research_result": False,
            },
            indent=2,
        )
        + "\n"
    )
    results: list[dict] = []

    async def consume(worker_index: int) -> None:
        worker_id = ids[worker_index]
        for index in range(worker_index, len(prompts), workers):
            deadline = clock() + int(timeout * 1000)
            envelope = SubtaskDispatch(
                subtask_id=f"{run_id}-{index}",
                task_id=run_id,
                specialty="general",
                prompt=prompts[index],
                source_inputs=[],
                deadline_ms=deadline,
            )
            # InMemoryBus awaits handlers during publish. Bound the entire
            # dispatch, including publish, not just the client's reply wait.
            try:
                async with asyncio.timeout(timeout + 2):
                    result = await client.dispatch(
                        envelope,
                        worker_id=worker_id,
                        deadline_ms=deadline,
                        grace_s=1,
                    )
                row = {"index": index, **result.to_dict()}
            except TimeoutError:
                row = {
                    "index": index,
                    "worker_id": worker_id,
                    "status": "TIMED_OUT",
                    "error": "dispatch exceeded deadline",
                    "subtask_id": envelope.subtask_id,
                }
            results.append(row)
            # No await inside these writes: completion records cannot interleave.
            with (output / "results.jsonl").open("a") as stream:
                stream.write(json.dumps(row) + "\n")
            with (output / "metrics.jsonl").open("a") as stream:
                stream.write(
                    json.dumps(
                        {
                            "step": len(results),
                            "total_steps": len(prompts),
                            "ts": time.time(),
                            "completed": sum(r["status"] == "COMPLETED" for r in results),
                            "failed": sum(r["status"] != "COMPLETED" for r in results),
                        }
                    )
                    + "\n"
                )

    await asyncio.gather(*(consume(i) for i in range(workers)))
    return sorted(results, key=lambda row: row["index"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", action="append", required=True)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("--host", default="http://localhost:11434")
    parser.add_argument("--model", default="gemma3:1b")
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument(
        "--output", type=Path, required=True, help="new directory; never overwritten"
    )
    parser.add_argument("--smoke", action="store_true", help="synthetic executor, no inference")
    args = parser.parse_args()
    if not 1 <= args.max_tokens <= 32768:
        parser.error("max-tokens must be 1..32768")

    def factory(worker_id: str) -> Executor:
        provider = None if args.smoke else OllamaProvider(host=args.host, model=args.model)

        async def execute(envelope: SubtaskDispatch) -> ExecutorOutput:
            if provider is None:
                await asyncio.sleep(0)
                return ExecutorOutput.completed(
                    output=f"SMOKE {worker_id}: {envelope.prompt}", tokens_used=0, model="synthetic"
                )
            response = await provider.complete(
                [Message(role=Role.USER, content=envelope.prompt)],
                system="Complete the assigned task. State uncertainty. You have no tool access.",
                max_tokens=args.max_tokens,
            )
            if not response.content.strip() or response.tool_calls:
                raise ValueError("worker returned empty text or requested unavailable tools")
            return ExecutorOutput.completed(
                output=response.content,
                tokens_used=sum(response.usage.values()),
                model=response.model or args.model,
            )

        return execute

    try:
        rows = asyncio.run(
            run_fleet(
                args.prompt,
                workers=args.workers,
                timeout=args.timeout,
                output=args.output,
                executor_factory=factory,
                mode="smoke" if args.smoke else "ollama",
            )
        )
    except (ValueError, FileExistsError) as exc:
        parser.error(str(exc))
    print(json.dumps(rows, indent=2))
    raise SystemExit(0 if all(row["status"] == "COMPLETED" for row in rows) else 1)


if __name__ == "__main__":
    main()
