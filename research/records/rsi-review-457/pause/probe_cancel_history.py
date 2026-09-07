from __future__ import annotations

import asyncio
import json
import re
import tempfile
from pathlib import Path

from turing.research.rsi.cheat import CheatDetector
from turing.research.rsi.contracts import EngineResult, RsiConfig, VerifierSpec
from turing.research.rsi.engine import ok_result
from turing.research.rsi.loop import RsiLoop, read_trajectory


class Engine:
    def __init__(self, results: Path):
        self.results = results

    async def run(self, prompt: str, *, cwd: Path, timeout_seconds: float) -> EngineResult:
        del timeout_seconds
        round_no = int(re.search(r"You are round (\d+)", prompt).group(1))
        (cwd / "SCORE").write_text("score=1\n")
        with (self.results / "metrics.jsonl").open("a") as fh:
            fh.write(json.dumps({"step": round_no, "score": 1}) + "\n")
        return ok_result()


class CancellingEdit:
    def __init__(self, results: Path):
        self.results = results
        self.ready = asyncio.Event()
        self.block = asyncio.Event()

    async def propose(self, inputs):
        del inputs
        trajectory = self.results / "trajectory.json"
        with trajectory.open("a") as fh:
            fh.write(json.dumps({"round": 777, "started": 1, "ended": 2, "exit": 0, "score": 999, "passed": True, "categories": [], "void": False}) + "\n")
        self.ready.set()
        await self.block.wait()
        return None


async def main():
    with tempfile.TemporaryDirectory(prefix="rsi-cancel-") as root:
        root = Path(root)
        cfg = RsiConfig(slug="cancel", workspace_root=root / "w", results_root=root / "r", rounds=1, self_edit_every=1, self_edit_budget=1)
        edit = CancellingEdit(cfg.results_dir)
        loop = RsiLoop(cfg, engine=Engine(cfg.results_dir), verifier=VerifierSpec("cat SCORE"), self_edit=edit, cheat=CheatDetector(), problem="cancel")
        task = asyncio.create_task(loop.run())
        await edit.ready.wait()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            print("cancelled")
        trajectory = cfg.results_dir / "trajectory.json"
        print("after-cancel-lines", trajectory.read_text().splitlines())
        print("after-cancel-state", read_trajectory(trajectory))
        cfg2 = RsiConfig(slug="cancel", workspace_root=root / "w", results_root=root / "r", rounds=1, self_edit_every=0, self_edit_budget=0)
        resumed = await RsiLoop(cfg2, engine=Engine(cfg2.results_dir), verifier=None, self_edit=None, cheat=CheatDetector()).run()
        print("resumed", resumed)
        print("resume-lines", trajectory.read_text().splitlines())


asyncio.run(main())
