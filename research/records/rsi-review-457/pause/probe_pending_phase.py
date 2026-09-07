from __future__ import annotations

import asyncio
import json
import re
import tempfile
from pathlib import Path

from turing.research.rsi.cheat import CheatDetector, run_git
from turing.research.rsi.contracts import EngineResult, RsiConfig, VerifierSpec
from turing.research.rsi.engine import ok_result
from turing.research.rsi.loop import RsiLoop


class Engine:
    def __init__(self, results: Path, scores: list[tuple[int, int]]):
        self.results = results
        self.scores = list(scores)
        self.calls = []

    async def run(self, prompt: str, *, cwd: Path, timeout_seconds: float) -> EngineResult:
        del timeout_seconds
        round_no = int(re.search(r"round (\d+)", prompt).group(1))
        self.calls.append(round_no)
        exit_code, score = self.scores.pop(0)
        (cwd / "SCORE").write_text(f"score={score}\n")
        with (self.results / "metrics.jsonl").open("a") as fh:
            fh.write(json.dumps({"step": round_no, "score": score}) + "\n")
        return EngineResult(exit_code, "", "", 0.0, False)


class Edit:
    def __init__(self, sandbox: Path):
        self.sandbox = sandbox

    async def propose(self, inputs):
        (self.sandbox / "SCAFFOLD.md").write_text("BAD\n")
        await run_git(self.sandbox, "add", "--", "SCAFFOLD.md", check=True)
        await run_git(self.sandbox, "commit", "-q", "-m", "edit", check=True)
        return (await run_git(self.sandbox, "rev-parse", "HEAD", check=True)).stdout.strip()


async def main():
    with tempfile.TemporaryDirectory(prefix="rsi-phase-") as root:
        root = Path(root)
        cfg = RsiConfig(slug="phase", workspace_root=root / "w", results_root=root / "r", rounds=6, self_edit_every=3, self_edit_budget=1)
        first_results = cfg.results_dir
        e1 = Engine(first_results, [(0, 5), (0, 5), (0, 5), (1, 1), (1, 1), (1, 1)])
        first = await RsiLoop(cfg, engine=e1, verifier=VerifierSpec("cat SCORE"), self_edit=Edit(cfg.sandbox_dir), cheat=CheatDetector(), problem="phase").run()
        print("first", first, "calls", e1.calls)
        cfg2 = RsiConfig(slug="phase", workspace_root=root / "w", results_root=root / "r", rounds=1, self_edit_every=0, self_edit_budget=0)
        e2 = Engine(cfg2.results_dir, [(1, 1)])
        second = await RsiLoop(cfg2, engine=e2, verifier=None, self_edit=None, cheat=CheatDetector()).run()
        print("second", second, "calls", e2.calls)
        events = [json.loads(x) for x in (cfg.results_dir / "trajectory.json").read_text().splitlines() if json.loads(x).get("event")]
        print("events", [(x["event"], x["round"]) for x in events])


asyncio.run(main())
