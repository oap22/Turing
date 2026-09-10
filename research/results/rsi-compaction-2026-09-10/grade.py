#!/usr/bin/env python3
"""Frozen verifier for the RSI problem "compaction".

The sandbox's compact.py must read an agent-session transcript on stdin and
write a shorter text on stdout that still carries every fact a later reader
would need. Transcripts are generated here from a template family with a
fresh random seed on every run, so the compactor has to be general, not a
lookup of one file.

Pass: for every generated transcript, compact.py exits 0 within 20 s, its
output is no longer than its input, and every fact string (case-insensitive
substring) is present in the output. Fail otherwise.
Score: mean over transcripts of len(input) / len(output). Higher is better;
1.0 is the identity. Printed as the last line, score=<number>.
"""
from __future__ import annotations

import random
import subprocess
import sys
import time
from pathlib import Path

TRANSCRIPTS = 5
TIMEOUT_S = 20

BANNER = (
    "================================================================\n"
    " turing agent session | node pi-alpha | mode development\n"
    " tools: shell, read_file, write_file, web_fetch | audit: sqlite\n"
    "================================================================\n"
)
NOISE = [
    "[debug] heartbeat ok, peers=3, uptime={u}s",
    "[debug] cache stats: hits={h} misses={m}",
    "[info] embedding batch flushed ({n} vectors)",
    "[debug] nats subject mesh.presence.heartbeat published",
    "[trace] safety gate evaluated pattern set v{v}",
]
MODULES = ["memory/store", "llm/router", "tools/shell", "agent/executor", "mesh/node",
           "coordinator/lifecycle", "research/rsi/loop", "vault/committer"]
DECISIONS = ["use sqlite for the audit log", "route tool calls to cloud", "keep the verifier frozen",
             "defer matplotlib to first use", "cap the notes tail at eight thousand chars",
             "retry the local model once then fall back", "pin the taxonomy digest"]
ERRORS = ["ModuleNotFoundError", "TimeoutError", "PermissionError", "sqlite3.OperationalError",
          "ConnectionRefusedError"]


def make_transcript(rng: random.Random) -> tuple[str, list[str]]:
    lines = [BANNER]
    facts: list[str] = []
    turns = rng.randint(40, 70)
    for t in range(1, turns + 1):
        kind = rng.choices(["tool", "result", "user", "decision", "error", "noise"],
                           weights=[3, 3, 2, 1, 1, 6])[0]
        if kind == "tool":
            mod = rng.choice(MODULES)
            n = rng.randint(100, 9999)
            path = f"src/turing/{mod}_{n}.py"
            lines.append(f"[turn {t}] tool_call shell: pytest tests/{Path(path).stem}_test.py -q")
            lines.append(f"[turn {t}] tool_call read_file: {path}")
            facts.append(path)
        elif kind == "result":
            code = rng.randint(0, 4)
            secs = rng.randint(1, 900)
            lines.append(f"[turn {t}] tool_result shell: exit code {code} after {secs} s")
            lines.append("[turn %d] tool_result stdout: %s" % (t, " ".join(f"line{i}" for i in range(rng.randint(5, 40)))))
            facts.append(f"exit code {code} after {secs} s")
        elif kind == "user":
            tokens = rng.randint(1000, 90000)
            lines.append(f"[turn {t}] user: the budget for this task is {tokens} tokens, stay under it")
            facts.append(f"{tokens} tokens")
        elif kind == "decision":
            d = rng.choice(DECISIONS)
            lines.append(f"[turn {t}] assistant: DECISION: {d}. Rationale: " + " ".join(["because"] * rng.randint(10, 60)))
            facts.append(f"DECISION: {d}")
        elif kind == "error":
            e = rng.choice(ERRORS)
            mod = rng.choice(MODULES)
            line_no = rng.randint(10, 999)
            lines.append(f"[turn {t}] tool_result stderr: Traceback (most recent call last):")
            for _ in range(rng.randint(3, 12)):
                lines.append(f'  File "src/turing/{rng.choice(MODULES)}.py", line {rng.randint(1, 999)}, in <module>')
            lines.append(f'  File "src/turing/{mod}.py", line {line_no}, in run')
            lines.append(f"{e}: {rng.choice(['boom', 'no such table', 'denied', 'refused'])}")
            facts.append(f"{e}")
            facts.append(f'src/turing/{mod}.py", line {line_no}')
        else:
            for _ in range(rng.randint(1, 4)):
                lines.append(rng.choice(NOISE).format(u=rng.randint(1, 10**6), h=rng.randint(0, 999),
                                                      m=rng.randint(0, 99), n=rng.randint(1, 512),
                                                      v=rng.randint(1, 9)))
            if rng.random() < 0.3:
                lines.append(BANNER)
    return "\n".join(lines) + "\n", facts


def main() -> int:
    sandbox = Path.cwd()
    compactor = sandbox / "compact.py"
    if not compactor.is_file():
        print("verifier: compact.py missing")
        return 1
    seed = int(time.time_ns()) ^ random.SystemRandom().getrandbits(64)
    rng = random.Random(seed)
    ratios = []
    for i in range(TRANSCRIPTS):
        text, facts = make_transcript(rng)
        try:
            proc = subprocess.run([sys.executable, str(compactor)], input=text, text=True,
                                  capture_output=True, timeout=TIMEOUT_S, cwd=sandbox)
        except subprocess.TimeoutExpired:
            print(f"verifier: transcript {i}: compact.py exceeded {TIMEOUT_S}s")
            return 1
        if proc.returncode != 0:
            print(f"verifier: transcript {i}: compact.py exit {proc.returncode}: {proc.stderr[-300:]}")
            return 1
        out = proc.stdout
        if len(out) > len(text):
            print(f"verifier: transcript {i}: output longer than input")
            return 1
        low = out.lower()
        missing = [f for f in facts if f.lower() not in low]
        if missing:
            print(f"verifier: transcript {i}: {len(missing)}/{len(facts)} facts missing, e.g. {missing[:3]!r}")
            return 1
        ratios.append(len(text) / max(1, len(out)))
        print(f"transcript {i}: in={len(text)} out={len(out)} facts={len(facts)} ratio={ratios[-1]:.3f}")
    print(f"seed={seed}")
    print(f"score={sum(ratios) / len(ratios):.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
