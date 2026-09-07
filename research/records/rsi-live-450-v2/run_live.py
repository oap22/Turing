"""Bounded campaign launcher; invoked by log_run.py from a Turing terminal."""

from __future__ import annotations

import hashlib
import json
import os
import stat
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


PhysicalEntry = tuple[str, int, bytes]
PhysicalManifest = dict[str, PhysicalEntry]


def _physical_source_manifest(repo: Path) -> PhysicalManifest:
    """Return every physical entry below ``src`` and ``tests``.

    This deliberately walks the filesystem instead of asking Git for paths, so
    ignored and untracked files, symlinks, executable bits, unusual names, and
    special files cannot silently evade provenance checks. Directories are
    structural and are represented by their children; empty directories have no
    source contents to hash.
    """
    manifest: PhysicalManifest = {}

    def visit(path: Path, relative: str) -> None:
        try:
            metadata = path.lstat()
            mode = metadata.st_mode
            if stat.S_ISLNK(mode):
                manifest[relative] = ("symlink", 0, os.fsencode(os.readlink(path)))
                return
            if stat.S_ISDIR(mode):
                with os.scandir(path) as entries:
                    children = sorted(entries, key=lambda entry: os.fsencode(entry.name))
                    for entry in children:
                        visit(Path(entry.path), f"{relative}/{entry.name}")
                return
            if stat.S_ISREG(mode):
                manifest[relative] = (
                    "file",
                    0o755 if mode & 0o111 else 0o644,
                    hashlib.sha256(path.read_bytes()).digest(),
                )
                return
            descriptor = f"{stat.S_IFMT(mode):o}:{stat.S_IMODE(mode):o}".encode("ascii")
            manifest[relative] = ("special", 0, descriptor)
        except OSError as exc:
            raise RuntimeError(f"cannot inspect candidate source entry {path}: {exc}") from exc

    for root_name in ("src", "tests"):
        root = repo / root_name
        if root.exists() or root.is_symlink():
            visit(root, root_name)
    return manifest


def _seed_source_manifest(repo: Path, seed_sha: str) -> PhysicalManifest:
    """Read the committed seed's source/test manifest without changing the checkout."""
    try:
        tree = subprocess.check_output(
            ["git", "ls-tree", "-r", "-z", seed_sha, "--", "src", "tests"], cwd=repo
        )
        entries: list[tuple[str, bytes, bytes]] = []
        for record in tree.split(b"\0"):
            if not record:
                continue
            metadata, path_bytes = record.split(b"\t", 1)
            mode, object_type, object_id = metadata.split()
            if object_type != b"blob":
                raise RuntimeError(
                    f"unsupported committed source entry {os.fsdecode(path_bytes)!r}"
                )
            entries.append((os.fsdecode(path_bytes), mode, object_id))

        manifest: PhysicalManifest = {}
        if entries:
            content_stream = subprocess.check_output(
                ["git", "cat-file", "--batch"],
                cwd=repo,
                input=b"".join(object_id + b"\n" for _, _, object_id in entries),
            )
            offset = 0
            for relative, mode, object_id in entries:
                line_end = content_stream.index(b"\n", offset)
                header = content_stream[offset:line_end].split()
                if len(header) != 3 or header[0] != object_id or header[1] != b"blob":
                    raise RuntimeError(f"cannot read committed source entry {relative!r}")
                size = int(header[2])
                start = line_end + 1
                content = content_stream[start : start + size]
                if len(content) != size or content_stream[start + size : start + size + 1] != b"\n":
                    raise RuntimeError(f"truncated committed source entry {relative!r}")
                offset = start + size + 1
                if mode == b"120000":
                    manifest[relative] = ("symlink", 0, content)
                else:
                    executable = int(mode, 8) & 0o111
                    manifest[relative] = (
                        "file",
                        0o755 if executable else 0o644,
                        hashlib.sha256(content).digest(),
                    )
        return manifest
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        raise RuntimeError(
            f"cannot read committed candidate source tree {seed_sha}: {exc}"
        ) from exc


def _manifest_identity(manifest: PhysicalManifest) -> str:
    digest = hashlib.sha256()
    for relative, (kind, mode, content) in sorted(manifest.items()):
        digest.update(b"PATH\0")
        digest.update(os.fsencode(relative))
        digest.update(b"\0KIND\0")
        digest.update(kind.encode("ascii"))
        digest.update(b"\0MODE\0")
        digest.update(f"{mode:o}".encode("ascii"))
        digest.update(b"\0CONTENT\0")
        digest.update(content)
        digest.update(b"\0")
    return digest.hexdigest()


def source_tree_identity(repo: Path) -> str:
    """Hash the physical source/test tree, including ignored and untracked entries."""
    return _manifest_identity(_physical_source_manifest(repo))


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
    """Require the frozen commit and a matching physical source/test tree before launch."""
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
    expected = _seed_source_manifest(repo, expected_sha)
    actual = _physical_source_manifest(repo)
    if actual != expected:
        changed = sorted(set(actual) | set(expected))
        details = []
        for relative in changed:
            if relative not in expected:
                details.append(f"unexpected {relative!r}")
            elif relative not in actual:
                details.append(f"missing {relative!r}")
            elif actual[relative] != expected[relative]:
                details.append(f"changed {relative!r}")
            if len(details) == 8:
                break
        suffix = "; ".join(details)
        raise RuntimeError(
            "candidate source/test tree does not match the committed seed; "
            f"physical provenance requires a pristine tree ({suffix})"
        )
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
