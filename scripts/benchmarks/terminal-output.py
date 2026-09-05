"""Capture the real PTY benchmark under research-loop's provenance wrapper."""
import json
import os
import pathlib
import re
import subprocess

command = ["cargo", "test", "--manifest-path", "desktop/src-tauri/Cargo.toml",
           "--offline", "measure_output_batches", "--", "--ignored", "--nocapture"]
result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
print(result.stdout, end="", flush=True)
if result.returncode:
    raise SystemExit(result.returncode)
rows = [dict(baseline=base == "true", bytes=int(size), events=int(events),
             elapsed_ms=float(elapsed), first_ms=float(first))
        for base, size, events, elapsed, first in re.findall(
            r"pty_output baseline=(true|false) bytes=(\d+) events=(\d+) elapsed_ms=([\d.]+) first_ms=([\d.]+)", result.stdout)]
assert len(rows) == 6, "benchmark output missing"
metrics = {"measurements": rows, "seed": "not-applicable: deterministic engineering workload",
           "n_examples": 4_800_000, "eval_set": "not-applicable: synthetic PTY bytes",
           "eval_set_sha256": "not-applicable: synthetic PTY bytes"}
if directory := os.environ.get("RESEARCH_RUN_DIR"):
    pathlib.Path(directory, "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
