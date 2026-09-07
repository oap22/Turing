"""Bounded campaign launcher; invoked by log_run.py from a Turing terminal."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
STATE = Path("/private/tmp/turing-rsi450")
CANDIDATE = STATE / "workspace/rsi-turing-rsi-450-v2"
RESULTS = Path("/Users/owenpacetti/research-results")
PYTHON = "/Users/owenpacetti/Developer/active/Turing/.venv/bin/python"
LAUNCH_CONFIG = ROOT / "research/records/rsi-live-450-v2/launch-config.json"


def _git_text(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def _source_paths(repo: Path) -> list[str]:
    paths = subprocess.check_output(
        [
            "git",
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "--",
            "src",
            "tests",
        ],
        cwd=repo,
        text=True,
    ).splitlines()
    return sorted(set(paths))


def source_tree_identity(repo: Path) -> str:
    """Hash committed and working source/test tree for before/after provenance."""
    digest = hashlib.sha256()
    tree = subprocess.check_output(
        ["git", "ls-tree", "-r", "--full-tree", "HEAD", "--", "src", "tests"],
        cwd=repo,
    )
    dirty_diff = subprocess.check_output(
        ["git", "diff", "--binary", "HEAD", "--", "src", "tests"],
        cwd=repo,
    )
    digest.update(b"HEAD-TREE\0")
    digest.update(tree)
    digest.update(b"WORKTREE-DIFF\0")
    digest.update(dirty_diff)
    for rel in _source_paths(repo):
        path = repo / rel
        digest.update(b"PATH\0")
        digest.update(rel.encode())
        digest.update(b"\0")
        if path.is_symlink():
            digest.update(b"SYMLINK\0")
            digest.update(os.readlink(path).encode())
        elif path.is_file():
            digest.update(b"FILE\0")
            digest.update(path.read_bytes())
        else:
            digest.update(b"MISSING\0")
    return digest.hexdigest()


def source_diff_identity(repo: Path) -> str:
    """Retain historical source-diff metric alongside stronger tree identity."""
    digest = hashlib.sha256()
    source_diff = subprocess.check_output(
        ["git", "diff", "--binary", "HEAD", "--", "src", "tests"], cwd=repo
    )
    digest.update(source_diff)
    for rel in subprocess.check_output(
        ["git", "ls-files", "--others", "--exclude-standard", "--", "src", "tests"],
        cwd=repo,
        text=True,
    ).splitlines():
        digest.update(rel.encode())
        digest.update((repo / rel).read_bytes())
    return digest.hexdigest()


def validate_candidate_start(repo: Path, seed_ref: str) -> tuple[str, str]:
    """Require frozen seed commit and clean source/test tree before launch."""
    try:
        actual_sha = _git_text(repo, "rev-parse", "HEAD")
        expected_sha = _git_text(repo, "rev-parse", "--verify", f"{seed_ref}^{{commit}}")
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"cannot resolve candidate seed {seed_ref!r}: {exc}") from exc
    if actual_sha != expected_sha:
        raise RuntimeError(
            f"candidate seed mismatch: launch config requires {seed_ref} ({expected_sha}), "
            f"but candidate HEAD is {actual_sha}"
        )
    dirty = subprocess.check_output(
        ["git", "status", "--porcelain=v1", "--untracked-files=all", "--", "src", "tests"],
        cwd=repo,
        text=True,
    ).strip()
    if dirty:
        raise RuntimeError(f"candidate source tree is dirty before launch:\n{dirty}")
    return actual_sha, source_tree_identity(repo)


def main() -> int:
    run = Path(os.environ["RESEARCH_RUN_DIR"])
    launch_config = json.loads(LAUNCH_CONFIG.read_text(encoding="utf-8"))
    seed_ref = launch_config.get("seed_candidate")
    if not isinstance(seed_ref, str) or not seed_ref:
        raise RuntimeError("launch config must provide a non-empty seed_candidate")
    candidate_before_sha, candidate_source_before = validate_candidate_start(CANDIDATE, seed_ref)

    command = [
        str(ROOT / "scripts/rsi-loop.sh"),
        "--slug",
        "turing-rsi-450-v2",
        "--results-root",
        str(RESULTS),
        "--workspace-root",
        str(STATE / "workspace"),
        "--rounds",
        "1",
        "--engine",
        "codex",
        "--self-edit-every",
        "0",
        "--round-timeout-seconds",
        "600",
        "--verifier-timeout-seconds",
        "120",
        "--problem",
        (STATE / "problem-v2.txt").read_text(),
        "--verifier",
        "./verify-rsi-450.sh",
        "--verifier-file",
        "verify-rsi-450.sh",
    ]
    env = dict(os.environ, TURING_RSI_PYTHON=PYTHON, PYTHONPATH=str(ROOT / "src"))
    print("campaign_engine_source=" + str(ROOT / "src"), flush=True)
    print("candidate_source=" + str(CANDIDATE / "src"), flush=True)
    print("candidate_seed=" + candidate_before_sha, flush=True)
    print("candidate_source_before=" + candidate_source_before, flush=True)
    print(
        "engine=codex model=gpt-5.6-luna reasoning=xhigh rounds=1 timeout_per_round=600",
        flush=True,
    )
    trajectory = RESULTS / "loop-rsi-turing-rsi-450-v2/trajectory.json"
    prior_rows = (
        [json.loads(line) for line in trajectory.read_text().splitlines() if line.strip()]
        if trajectory.exists()
        else []
    )
    prior_round_count = sum("event" not in row for row in prior_rows)
    engine_sha = _git_text(ROOT, "rev-parse", "HEAD")
    started = time.monotonic()
    completed = subprocess.run(command, cwd=ROOT, env=env, check=False)
    rows = []
    if trajectory.exists():
        rows = [json.loads(line) for line in trajectory.read_text().splitlines() if line.strip()]
    rounds = [row for row in rows if "event" not in row][prior_round_count:]
    passes = sum(row.get("passed") is True and row.get("void") is False for row in rounds)
    latest_accepted = bool(
        rounds
        and rounds[-1].get("exit") == 0
        and rounds[-1].get("passed") is True
        and rounds[-1].get("void") is False
    )
    candidate_after_sha = _git_text(CANDIDATE, "rev-parse", "HEAD")
    candidate_source_after = source_tree_identity(CANDIDATE)
    checker = ROOT / "research/records/rsi-live-450-v2/evaluator-manifest.json"
    metrics = {
        "completed_rounds": len(rounds),
        "verifier_passes": passes,
        "latest_round_accepted": latest_accepted,
        "engine_sha": engine_sha,
        "candidate_seed_sha": candidate_before_sha,
        "candidate_source_before_sha256": candidate_source_before,
        "candidate_source_after_sha256": candidate_source_after,
        "candidate_source_changed": candidate_source_before != candidate_source_after,
        "candidate_source_diff_sha256": source_diff_identity(CANDIDATE),
        "void_rounds": sum(row.get("void") is True for row in rounds),
        "wall_seconds": time.monotonic() - started,
        "seed": "not-applicable: provider seed unavailable",
        "n_examples": 8,
        "eval_set": "rsi-trajectory-integrity-external-v2",
        "eval_set_sha256": hashlib.sha256(checker.read_bytes()).hexdigest(),
        "model": "gpt-5.6-luna",
        "reasoning_effort": "xhigh",
        "cost_usd": "unknown: subscription usage not metered by harness",
        "candidate_sha": candidate_after_sha,
    }
    (run / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n")
    (run / "notes.md").write_text(
        "# Turing live RSI source improvement\n\n"
        "Question: can a real Codex/Luna RSI round repair trajectory evidence corruption in Turing?\n\n"
        f"Observed verifier passes: {passes}; process exit: {completed.returncode}. "
        "Independent post-run review and holdout probes are still required. "
        "This is a bounded engineering test, not evidence of causal scaffold self-improvement.\n"
    )
    print(json.dumps(metrics), flush=True)
    return completed.returncode if completed.returncode else (0 if latest_accepted else 1)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(2) from exc
