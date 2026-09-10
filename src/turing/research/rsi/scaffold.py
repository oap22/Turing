"""The scaffold self-edit step: loop 2, scoped to the RSI workstation.

The scaffold is ``<sandbox>/SCAFFOLD.md`` — standing instructions the loop
prepends verbatim to every round prompt. Every ``self_edit_every`` rounds the
loop lets the engine rewrite that one file from an aggregate summary of how
the rounds went, commits the result in the sandbox's git repo, and later
reverts the commit if the following rounds got worse (ADR 0011 §15).

What this module guarantees:

* **The summary is narrow (I4).** :func:`build_self_edit_inputs` reads only
  ``SCAFFOLD.md`` and the tail of ``NOTES.md`` from the sandbox — and only
  when they are regular files inside it, never through a symlink — aggregates
  the trajectory into :class:`~turing.research.rsi.contracts.RoundSummary`
  rows and taxonomy counts, and adds the verifier command and the text of
  ``VERIFIER.json`` (when present) to the forbidden substrings so
  :class:`~turing.research.rsi.contracts.SelfEditInputs` refuses to be built
  if either leaks into the scaffold or the notes. :func:`render_self_edit_prompt`
  re-checks the rendered prompt against the same forbidden list.
* **The step may write only ``SCAFFOLD.md`` (I3).**
  :meth:`ScaffoldSelfEditStep.propose` snapshots the repo before the engine
  runs — HEAD, the index tree, every dirty path (ignored files included) with
  its content, and the repo's own ``.git/HEAD``, ``.git/config``,
  ``.git/info`` and ``.git/hooks`` — and compares afterwards. The edit is
  kept, committed (hooks disabled) and its SHA returned only if the working
  tree's sole change is a regular-file ``SCAFFOLD.md`` and nothing else
  moved. Anything else — another path created, modified, deleted or
  reverted, a commit or index change made by the engine, ``SCAFFOLD.md``
  replaced by a symlink or directory, a hook or config planted under
  ``.git`` — rejects the whole edit: HEAD and index go back to the snapshot,
  paths the engine touched are restored to their pre-run bytes (or removed
  if they did not exist), and ``rsi.self_edit.rejected`` is logged with the
  reason and paths. Nothing under the results dir and no ``metrics.jsonl``
  is ever discarded. Filenames are never interpreted as globs.
* **The scaffold stays bounded.** The summary's rounds table is a fixed
  size (:data:`SUMMARY_ROUNDS_HEAD` + one aggregate row +
  :data:`SUMMARY_ROUNDS_TAIL`), the notes tail is capped in characters as
  well as lines (:func:`compact_notes_tail`), and an edit that leaves
  ``SCAFFOLD.md`` over :data:`SCAFFOLD_MAX_BYTES` is rejected like any
  other bad edit — the scaffold is paid on every round's prompt, and an
  unbounded one eventually cannot be passed to the engine at all.
* **The step never adjusts the verifier (I1, I5).** If ``VERIFIER.json`` or
  any file the lock pins changed in the working tree, the index or a commit
  the engine made, the step raises
  :class:`~turing.research.contracts.FrozenVerifierError` before touching
  anything and leaves the tree as it is for the operator.
* **Rollback reverts only a scaffold commit, and never half-way.**
  :func:`rollback_scaffold` refuses
  (:class:`~turing.research.contracts.ContractViolationError`) to revert a
  commit that touches anything other than ``SCAFFOLD.md``; a revert that
  conflicts is aborted so the tree is left clean, then raised.

What it does not do:

* It does not decide *when* to self-edit, whether the budget is spent, or
  whether the following rounds were worse than the noise floor. The loop
  owns that schedule; :mod:`turing.research.rsi.loop` currently performs
  the revert with its own git helper, so :func:`rollback_scaffold` and
  :func:`scaffold_head` are public conveniences for other callers (blocking;
  async wrappers are :func:`rollback_scaffold_async` and
  :func:`scaffold_head_async`).
* It does not check the verifier lock. The loop runs
  :func:`~turing.research.rsi.contracts.check_verifier_lock` before and after
  every engine invocation, this one included. This module only refuses to
  proceed when it can see the lock or its pinned files were touched.
* It does not sandbox the engine. The comparison covers the sandbox repo and
  its ``.git`` internals listed above; writes elsewhere (and to
  ``.git/objects`` or refs other than HEAD) are the cheat detector's problem.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from turing.research.contracts import ContractViolationError, FrozenVerifierError
from turing.research.rsi.contracts import (
    VERIFIER_LOCK_FILENAME,
    Engine,
    EngineResult,
    RoundSummary,
    SelfEditInputs,
    VerifierLock,
    iter_forbidden,
)
from turing.research.rsi.taxonomy import taxonomy_counts

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from turing.research.rsi.contracts import RoundRecord, RsiConfig

logger = structlog.get_logger(__name__)

__all__ = [
    "DEFAULT_SCAFFOLD_TEXT",
    "NOTES_FILENAME",
    "NOTES_TAIL_LINES",
    "NOTES_TAIL_MAX_CHARS",
    "SCAFFOLD_FILENAME",
    "SCAFFOLD_MAX_BYTES",
    "SUMMARY_ROUNDS_HEAD",
    "SUMMARY_ROUNDS_TAIL",
    "ScaffoldSelfEditStep",
    "build_self_edit_inputs",
    "compact_notes_tail",
    "read_or_create_scaffold",
    "render_self_edit_prompt",
    "rollback_scaffold",
    "rollback_scaffold_async",
    "scaffold_head",
    "scaffold_head_async",
    "scaffold_size_problem",
]

SCAFFOLD_FILENAME: str = "SCAFFOLD.md"
NOTES_FILENAME: str = "NOTES.md"
#: How many trailing lines of ``NOTES.md`` the summary carries.
NOTES_TAIL_LINES: int = 40
#: ...and how many characters at most. A line cap alone is no cap: forty
#: lines of a pasted log can be hundreds of kilobytes. The tail keeps its
#: *last* characters (the newest notes) and says how much it dropped.
NOTES_TAIL_MAX_CHARS: int = 8_000
#: Largest ``SCAFFOLD.md`` a self-edit may leave behind. The scaffold is
#: prepended verbatim to every round prompt, so its size is paid on every
#: round; "keep it short" is enforced here, not requested. Well under the
#: engine's argv cap so a scaffold can never brick the loop.
SCAFFOLD_MAX_BYTES: int = 24 * 1024
#: The rounds table in the self-edit summary shows the first ``HEAD`` and
#: last ``TAIL`` rounds in full; rounds between are collapsed into one
#: aggregate row. Category counts always cover every round.
SUMMARY_ROUNDS_HEAD: int = 3
SUMMARY_ROUNDS_TAIL: int = 12

#: Written when ``SCAFFOLD.md`` is missing. Short on purpose: the first
#: self-edit is where the real content comes from.
DEFAULT_SCAFFOLD_TEXT: str = (
    "# Scaffold\n"
    "\n"
    "Standing instructions for every round of this research loop. Keep them\n"
    "short, concrete, and specific to this problem. The loop prepends this\n"
    "file to each round's prompt verbatim.\n"
)

_GIT_ENV_OVERRIDES: dict[str, str] = {
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
    # Engine-chosen filenames are never globs: ``a*`` means the file ``a*``.
    "GIT_LITERAL_PATHSPECS": "1",
}
_GIT_IDENTITY: tuple[str, ...] = (
    "-c",
    "commit.gpgsign=false",
    "-c",
    "tag.gpgsign=false",
    "-c",
    "user.name=turing-rsi",
    "-c",
    "user.email=rsi@turing.local",
    # A hook planted by the engine must never run inside our commit/revert.
    "-c",
    "core.hooksPath=/dev/null",
)
#: Wall-clock cap on any single git call; a hung git must not hang the loop.
_GIT_TIMEOUT_SECONDS: float = 60.0
_METRICS_FILENAME: str = "metrics.jsonl"
#: Repo internals an engine could plant things in that ``git status`` never
#: shows; snapshotted and restored around the engine run.
_GIT_INTERNALS: tuple[str, ...] = ("HEAD", "config", "info", "hooks")
#: Largest pre-existing dirty file whose bytes are kept for restoration.
_RESTORE_MAX_BYTES: int = 64 * 1024 * 1024
_SHA_RE: re.Pattern[str] = re.compile(r"^[0-9a-f]{7,40}$")
_REGULAR_FILE_MODE: str = "100644"
#: Largest pinned verifier file whose body becomes a forbidden substring of the summary.
_NEEDLE_MAX_BYTES: int = 1024 * 1024
#: Shortest pinned-file body worth forbidding; anything shorter would match ordinary text.
_NEEDLE_MIN_CHARS: int = 8


# --------------------------------------------------------------------------- #
# Inputs and prompt
# --------------------------------------------------------------------------- #


def _assert_plain_file_inside(path: Path, sandbox: Path, *, must_exist: bool) -> bool:
    """``True`` if ``path`` is a regular file inside ``sandbox``; ``False`` if absent.

    Raises:
        ContractViolationError: the path is a symlink, a directory or other
            non-regular file, or resolves outside the sandbox — reading it
            could carry content from outside the sandbox (I4).
    """
    if path.is_symlink():
        raise ContractViolationError(
            f"{path.name} in the sandbox is a symlink; the self-edit step reads only regular "
            "files inside the sandbox"
        )
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        if must_exist:
            raise ContractViolationError(f"{path.name} is missing from the sandbox") from None
        return False
    if not stat.S_ISREG(st.st_mode):
        raise ContractViolationError(f"{path.name} in the sandbox is not a regular file")
    root = sandbox.resolve()
    if path.resolve().parent != root:
        raise ContractViolationError(f"{path.name} does not resolve inside the sandbox")
    return True


def _read_plain_file(path: Path, sandbox: Path) -> str:
    """Read ``path`` without following a symlink (``O_NOFOLLOW``); see :func:`_assert_plain_file_inside`."""
    _assert_plain_file_inside(path, sandbox, must_exist=True)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    with os.fdopen(fd, "rb") as fh:
        return fh.read().decode("utf-8", errors="replace")


def read_or_create_scaffold(sandbox: Path) -> str:
    """Return ``SCAFFOLD.md``'s text, writing :data:`DEFAULT_SCAFFOLD_TEXT` first if absent.

    Raises:
        ContractViolationError: ``SCAFFOLD.md`` exists but is not a regular
            file inside the sandbox (a symlink — dangling or not — or a
            directory). Nothing is written through a symlink.
    """
    path = sandbox / SCAFFOLD_FILENAME
    if not _assert_plain_file_inside(path, sandbox, must_exist=False):
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(DEFAULT_SCAFFOLD_TEXT)
        logger.info("rsi.scaffold.created", path=str(path))
        return DEFAULT_SCAFFOLD_TEXT
    return _read_plain_file(path, sandbox)


def compact_notes_tail(
    text: str, *, lines: int = NOTES_TAIL_LINES, max_chars: int = NOTES_TAIL_MAX_CHARS
) -> str:
    """The last ``lines`` lines of ``text``, then the last ``max_chars`` of those.

    The character cut lands on a line boundary when one exists inside the
    kept window, so the tail starts with a whole line; a single line longer
    than the cap is cut mid-line. A cut is announced on a first line of its
    own, so the reader knows the notes continue above what it sees.
    """
    if lines < 0 or max_chars < 0:
        raise ContractViolationError("notes tail bounds must be non-negative")
    tail = "\n".join(text.splitlines()[-lines:]) if lines else ""
    if len(tail) <= max_chars:
        return tail
    dropped = len(tail) - max_chars
    kept = tail[-max_chars:] if max_chars else ""
    newline = kept.find("\n")
    if 0 <= newline < len(kept) - 1:
        dropped += newline + 1
        kept = kept[newline + 1 :]
    return f"[notes tail cut: {dropped} earlier characters omitted]\n{kept}"


def _notes_tail(sandbox: Path, lines: int = NOTES_TAIL_LINES) -> str:
    path = sandbox / NOTES_FILENAME
    if not _assert_plain_file_inside(path, sandbox, must_exist=False):
        return ""
    return compact_notes_tail(_read_plain_file(path, sandbox), lines=lines)


def scaffold_size_problem(
    size: int, *, previous: int | None = None, max_bytes: int = SCAFFOLD_MAX_BYTES
) -> str | None:
    """Why a ``SCAFFOLD.md`` of ``size`` bytes may not be kept, or ``None``.

    The cap is on *growth*: an edit is refused when it leaves the scaffold
    over ``max_bytes`` **and** larger than ``previous``. A scaffold already
    over the cap (seeded that way, or written before the cap existed) can
    therefore still be trimmed back down in steps; it can never grow.
    """
    if size > max_bytes and (previous is None or size > previous):
        return f"{SCAFFOLD_FILENAME} is {size} bytes, over the {max_bytes}-byte cap"
    return None


def _read_lock(sandbox: Path) -> tuple[str | None, VerifierLock | None]:
    """``(raw text, parsed lock)`` of ``VERIFIER.json``; ``(None, None)`` when absent."""
    path = sandbox / VERIFIER_LOCK_FILENAME
    if path.is_symlink() or not path.is_file():
        return None, None
    raw = path.read_text(encoding="utf-8", errors="replace")
    try:
        return raw, VerifierLock.from_json(json.loads(raw))
    except (ValueError, ContractViolationError):
        # A malformed lock is the loop's problem (its check raises); the raw
        # text is still forbidden here.
        return raw, None


def _verifier_forbidden(sandbox: Path) -> list[str]:
    """The verifier command, its hash, the raw lock text, pinned hashes and bodies — all of I4."""
    raw, lock = _read_lock(sandbox)
    if raw is None:
        return []
    needles: list[str] = [raw.strip()]
    if lock is not None:
        needles.extend(iter_forbidden(lock))
        needles.extend(lock.file_sha256s.values())
        for rel in lock.file_sha256s:
            path = sandbox / rel
            try:
                if path.is_symlink() or not path.is_file():
                    continue
                if os.lstat(path).st_size > _NEEDLE_MAX_BYTES:
                    continue
                body = path.read_text(encoding="utf-8", errors="replace").strip()
            except OSError:
                continue
            if len(body) >= _NEEDLE_MIN_CHARS:
                needles.append(body)
    return [n for n in needles if n]


def _verifier_paths(sandbox: Path) -> frozenset[str]:
    """``VERIFIER.json`` plus every path the lock pins: the paths the step must never let move."""
    _, lock = _read_lock(sandbox)
    pinned = set(lock.file_sha256s) if lock is not None else set()
    return frozenset({VERIFIER_LOCK_FILENAME, *pinned})


def _assert_inside_config(config: RsiConfig, sandbox: Path) -> None:
    if sandbox.resolve() != config.sandbox_dir.resolve():
        raise ContractViolationError(
            f"self-edit sandbox {sandbox} is not the configured sandbox {config.sandbox_dir}; "
            "the self-edit summary may only read the sandbox"
        )


def build_self_edit_inputs(
    config: RsiConfig,
    records: Sequence[RoundRecord],
    sandbox: Path,
    *,
    forbidden: Iterable[str] = (),
) -> SelfEditInputs:
    """Assemble the aggregate summary the self-edit step may see.

    ``records`` are the trajectory's round lines so far (events excluded),
    in order. Void rounds contribute their categories but never their score.
    ``forbidden`` is what the loop already knows must not leak (the verifier
    command); the lock file's text and the substrings
    :func:`~turing.research.rsi.contracts.iter_forbidden` derives from it
    are added here so the guarantee does not depend on the caller.

    Raises:
        ContractViolationError: ``sandbox`` is not ``config.sandbox_dir``,
            ``SCAFFOLD.md``/``NOTES.md`` is not a regular file inside it, or
            the scaffold/notes text contains verifier internals (raised by
            :class:`~turing.research.rsi.contracts.SelfEditInputs`).
    """
    _assert_inside_config(config, sandbox)
    summaries = tuple(RoundSummary.from_record(r) for r in records)
    counted_scores = [r.score for r in records if r.passed and not r.void and r.score is not None]
    best_score = max(counted_scores) if counted_scores else None
    round_index = records[-1].round if records else 0

    needles: list[str] = []
    for needle in (*forbidden, *_verifier_forbidden(sandbox)):
        if needle and needle not in needles:
            needles.append(needle)

    return SelfEditInputs(
        round_index=round_index,
        best_score=best_score,
        rounds=summaries,
        taxonomy_counts=taxonomy_counts(records),
        scaffold_text=read_or_create_scaffold(sandbox),
        notes_tail=_notes_tail(sandbox),
        forbidden=tuple(needles),
    )


def _fmt_score(score: float | None) -> str:
    return "-" if score is None else f"{score:.6g}"


def _summary_row(r: RoundSummary) -> str:
    return (
        f"| {r.round} | {_fmt_score(r.score)} | {'yes' if r.passed else 'no'} | "
        f"{', '.join(r.categories) or 'improved'} | {r.wall_seconds:.0f} |"
    )


def _summary_rows(
    rounds: Sequence[RoundSummary],
    *,
    head: int = SUMMARY_ROUNDS_HEAD,
    tail: int = SUMMARY_ROUNDS_TAIL,
) -> list[str]:
    """Table rows: every round when few, else head + one aggregate row + tail.

    The aggregate row carries what a reader would otherwise scan the elided
    rows for — how many, how many passed, the best score among them, and
    their category counts — so the table stays a fixed size however long
    the loop has run, without hiding a trend.
    """
    if len(rounds) <= head + tail:
        return [_summary_row(r) for r in rounds]
    middle = rounds[head : len(rounds) - tail]
    scores = [r.score for r in middle if r.score is not None]
    counts: dict[str, int] = {}
    for r in middle:
        for c in r.categories:
            counts[c] = counts.get(c, 0) + 1
    cats = ", ".join(f"{name}×{n}" for name, n in sorted(counts.items())) or "improved"
    first, last = middle[0].round, middle[-1].round
    elided = (
        f"| {first}–{last} | best {_fmt_score(max(scores) if scores else None)} | "
        f"{sum(1 for r in middle if r.passed)}/{len(middle)} | "
        f"{len(middle)} rounds elided: {cats} | "
        f"{sum(r.wall_seconds for r in middle):.0f} |"
    )
    return [
        *(_summary_row(r) for r in rounds[:head]),
        elided,
        *(_summary_row(r) for r in rounds[len(rounds) - tail :]),
    ]


def render_self_edit_prompt(inputs: SelfEditInputs) -> str:
    """The prompt for the self-edit round: edit ``SCAFFOLD.md`` only, do not commit.

    Raises:
        ContractViolationError: the rendered text contains a forbidden
            substring (cannot happen when ``inputs`` was built by
            :func:`build_self_edit_inputs`, re-checked anyway).
    """
    table = "\n".join(
        [
            "| round | score | passed | categories | wall s |",
            "|---|---|---|---|---|",
            *_summary_rows(inputs.rounds),
        ]
    )
    counts = "\n".join(f"- {name}: {n}" for name, n in inputs.taxonomy_counts.items())
    scaffold_bytes = len(inputs.scaffold_text.encode("utf-8"))
    prompt = f"""You are the self-edit step of a continuous research loop, running after
round {inputs.round_index}. The file {SCAFFOLD_FILENAME} in this working directory holds
the standing instructions that are prepended verbatim to every round's prompt.
Your job is to make those instructions better at this problem, using only the
aggregate results below.

Rules — these are enforced by the loop, not merely requested:
- You may edit ONLY {SCAFFOLD_FILENAME}. Do not create, modify, delete or move
  any other file. If anything else changes, the whole edit is discarded.
- Do NOT commit. The loop commits your edit and can revert it if the next
  rounds get worse.
- Do not run experiments, do not append to metrics.jsonl, do not touch
  NOTES.md or PROBLEM.md, do not create STOP.
- Keep the scaffold short and concrete: what to try next, what to avoid,
  what has been measured to work. Remove instructions that did not help.
  It is prepended to EVERY round's prompt, so every byte is paid every round.
  Hard cap: {SCAFFOLD_MAX_BYTES} bytes (it is {scaffold_bytes} bytes now); an
  edit that grows it past the cap is discarded whole.
- If the current scaffold is already as good as you can make it, change nothing.

## Rounds so far (score is measured by the loop's verifier; higher is better)
Best score: {_fmt_score(inputs.best_score)}

{table}

## Failure category counts
{counts}

## Current {SCAFFOLD_FILENAME}
```
{inputs.scaffold_text}
```

## Last {NOTES_TAIL_LINES} lines of {NOTES_FILENAME}
```
{inputs.notes_tail}
```
"""
    for needle in inputs.forbidden:
        if needle in prompt:
            raise ContractViolationError(
                "rendered self-edit prompt contains verifier internals; refusing to send it"
            )
    return prompt


# --------------------------------------------------------------------------- #
# git plumbing
# --------------------------------------------------------------------------- #


def _git(sandbox: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, **_GIT_ENV_OVERRIDES}
    env.pop("GIT_DIR", None)
    env.pop("GIT_WORK_TREE", None)
    try:
        proc = subprocess.run(
            ["git", *_GIT_IDENTITY, *args],
            cwd=sandbox,
            env=env,
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired as exc:  # subprocess.run already killed the child
        raise ContractViolationError(
            f"git {' '.join(args)} in {sandbox} exceeded {_GIT_TIMEOUT_SECONDS:.0f}s and was killed"
        ) from exc
    if check and proc.returncode != 0:
        raise ContractViolationError(
            f"git {' '.join(args)} failed in {sandbox} (exit {proc.returncode}): "
            f"{proc.stderr.strip()}"
        )
    return proc


def _assert_toplevel(sandbox: Path) -> None:
    """The sandbox must be the repo root: porcelain paths are root-relative."""
    proc = _git(sandbox, "rev-parse", "--show-toplevel", check=False)
    top = proc.stdout.strip()
    if proc.returncode != 0 or not top:
        raise ContractViolationError(f"{sandbox} is not a git repository")
    if Path(top).resolve() != sandbox.resolve():
        raise ContractViolationError(
            f"{sandbox} is nested inside the repository at {top}; the self-edit step needs "
            "the sandbox to be the repository root"
        )


def _status_paths(sandbox: Path) -> dict[str, str]:
    """``{path: XY}`` for every dirty path, untracked and ignored files listed individually."""
    proc = _git(
        sandbox,
        "status",
        "--porcelain",
        "--untracked-files=all",
        "--ignored=traditional",
        "-z",
    )
    entries = proc.stdout.split("\0")
    out: dict[str, str] = {}
    i = 0
    while i < len(entries):
        entry = entries[i]
        i += 1
        if not entry:
            continue
        status, path = entry[:2], entry[3:]
        out[path] = status
        if status[0] in "RC" and i < len(entries):
            # rename/copy: the next NUL-separated entry is the original path
            out[entries[i]] = status
            i += 1
    return out


def _digest_or_none(path: Path, *, cheap: bool = False) -> str | None:
    """Content digest for a regular file, ``link:<target>`` for a symlink, ``dir`` for a directory.

    Uses ``lstat`` so a path replaced by a symlink counts as a change. With
    ``cheap`` a regular file is signed by ``(size, mtime_ns)`` instead of its
    content — used for git-ignored files, which can be large build artefacts.
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(st.st_mode):
        return f"link:{os.readlink(path)}"
    if stat.S_ISDIR(st.st_mode):
        return "dir"
    if not stat.S_ISREG(st.st_mode):
        return f"special:{st.st_mode:o}"
    if cheap:
        return f"stat:{st.st_size}:{st.st_mtime_ns}"
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_content_digest(digest: str) -> bool:
    return not digest.startswith(("link:", "special:", "stat:")) and digest != "dir"


def _walk_files(root: Path) -> dict[str, str | None]:
    """``{relative path: digest}`` of every entry under ``root`` (a file or a directory)."""
    out: dict[str, str | None] = {}
    if root.is_symlink() or not root.exists():
        return out
    if not root.is_dir():
        out[""] = _digest_or_none(root)
        return out
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            p = Path(dirpath) / name
            out[str(p.relative_to(root))] = _digest_or_none(p)
    return out


@dataclass(frozen=True, slots=True)
class _TreeSnapshot:
    """Everything :meth:`ScaffoldSelfEditStep.propose` compares after the engine ran."""

    head: str
    index_tree: str
    dirty: dict[str, str | None]
    internals: dict[str, dict[str, str | None]]
    #: ``VERIFIER.json`` plus the lock's pinned files, read before the engine ran.
    guarded: frozenset[str]
    #: Directory holding a copy of every pre-existing dirty regular file (for restore).
    keep_dir: Path
    kept: dict[str, Path] = field(default_factory=dict)
    #: Size of ``SCAFFOLD.md`` before the engine ran (``None`` when absent).
    scaffold_bytes: int | None = None


def _snapshot(sandbox: Path) -> _TreeSnapshot:
    head = scaffold_head(sandbox)
    if head is None:
        raise ContractViolationError(
            "sandbox repo has no commits; the loop seeds one before the first self-edit"
        )
    tree = _git(sandbox, "write-tree", check=False)
    if tree.returncode != 0:
        raise ContractViolationError(
            f"sandbox index cannot be snapshotted (unmerged?): {tree.stderr.strip()}"
        )
    status = _status_paths(sandbox)
    dirty = {rel: _digest_or_none(sandbox / rel, cheap=xy == "!!") for rel, xy in status.items()}
    keep_dir = Path(tempfile.mkdtemp(prefix="rsi-self-edit-"))
    kept: dict[str, Path] = {}
    for rel, digest in dirty.items():
        src = sandbox / rel
        if digest is None or not _is_content_digest(digest):
            # ignored files are not copied: git does not version them either
            continue
        try:
            if os.lstat(src).st_size > _RESTORE_MAX_BYTES:
                continue
            dst = keep_dir / hashlib.sha256(rel.encode("utf-8")).hexdigest()
            shutil.copy2(src, dst, follow_symlinks=False)
        except OSError as exc:
            logger.warning("rsi.self_edit.snapshot_skipped", path=rel, error=str(exc))
            continue
        kept[rel] = dst
    git_dir = sandbox / ".git"
    internals = {name: _walk_files(git_dir / name) for name in _GIT_INTERNALS}
    for name in _GIT_INTERNALS:
        src = git_dir / name
        dst = keep_dir / "_git" / name
        if src.is_symlink() or not src.exists():
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            shutil.copytree(src, dst, symlinks=True)
        else:
            shutil.copy2(src, dst, follow_symlinks=False)
    scaffold_path = sandbox / SCAFFOLD_FILENAME
    return _TreeSnapshot(
        head=head,
        index_tree=tree.stdout.strip(),
        dirty=dirty,
        internals=internals,
        guarded=_verifier_paths(sandbox),
        keep_dir=keep_dir,
        kept=kept,
        scaffold_bytes=scaffold_path.stat().st_size if scaffold_path.is_file() else None,
    )


def _drop_snapshot(snap: _TreeSnapshot) -> None:
    shutil.rmtree(snap.keep_dir, ignore_errors=True)


def _changed_since(sandbox: Path, before: _TreeSnapshot) -> list[str]:
    """Paths the engine touched: newly dirty, dirty before with different content, or gone."""
    after = _status_paths(sandbox)
    changed: set[str] = set()
    for rel, xy in after.items():
        now = _digest_or_none(sandbox / rel, cheap=xy == "!!")
        if rel not in before.dirty or now != before.dirty[rel]:
            changed.add(rel)
    for rel, digest in before.dirty.items():
        cheap = digest is not None and digest.startswith("stat:")
        if rel not in after and _digest_or_none(sandbox / rel, cheap=cheap) != digest:
            # reverted to HEAD, deleted, stashed: previous rounds' work is gone
            changed.add(rel)
    return sorted(changed)


def _internals_changed(sandbox: Path, before: _TreeSnapshot) -> list[str]:
    git_dir = sandbox / ".git"
    changed: list[str] = []
    for name, files in before.internals.items():
        now = _walk_files(git_dir / name)
        if now != files:
            changed.append(name)
    return changed


def _is_protected(rel: str, sandbox: Path, results_dir: Path) -> bool:
    """Never discard results or metrics, even if they live inside the sandbox.

    A symlink is never protected: removing the link never touches its target,
    and an engine-made link into the results dir is exactly what must go.
    """
    path = sandbox / rel
    if path.is_symlink():
        return False
    if Path(rel).name == _METRICS_FILENAME:
        return True
    try:
        path.resolve().relative_to(results_dir.resolve())
    except ValueError:
        return False
    return True


def _remove_path(path: Path) -> None:
    """Remove a file, symlink or directory tree without following symlinks."""
    if path.is_symlink() or not path.is_dir():
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        return
    shutil.rmtree(path)


def _prune_empty_parents(sandbox: Path, rels: Iterable[str]) -> None:
    """Removing a file leaves the directories the engine created; git does not track them."""
    root = sandbox.resolve()
    for rel in rels:
        parent = (sandbox / rel).parent.resolve()
        while parent != root and root in parent.parents:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent


def _in_index(sandbox: Path, rel: str) -> bool:
    proc = _git(sandbox, "ls-files", "-z", "--", rel, check=False)
    return proc.returncode == 0 and bool(proc.stdout.strip("\0"))


def _restore_head_and_index(sandbox: Path, before: _TreeSnapshot) -> None:
    """Put HEAD and the index back where the snapshot had them; the working tree is untouched."""
    if scaffold_head(sandbox) != before.head:
        _git(sandbox, "reset", "-q", "--soft", before.head)
    _git(sandbox, "read-tree", before.index_tree)


def _restore_internals(sandbox: Path, before: _TreeSnapshot, names: Iterable[str]) -> None:
    """Put ``.git/<name>`` back exactly as the snapshot copied it (or remove it if it was absent)."""
    git_dir = sandbox / ".git"
    for name in names:
        root = git_dir / name
        saved = before.keep_dir / "_git" / name
        _remove_path(root)
        if saved.is_dir():
            shutil.copytree(saved, root, symlinks=True)
        elif saved.exists():
            shutil.copy2(saved, root, follow_symlinks=False)
        if _walk_files(root) != before.internals[name]:
            raise ContractViolationError(
                f".git/{name} could not be restored after the self-edit engine changed it; "
                "inspect the sandbox before continuing"
            )


def _discard(
    sandbox: Path, paths: Iterable[str], before: _TreeSnapshot, *, results_dir: Path
) -> None:
    """Put every path in ``paths`` back to its pre-run state.

    Pre-existing dirty files are restored from the snapshot copy; paths that
    were clean and tracked are checked out from the (already restored)
    index; everything else is removed. No git pathspec is ever a glob.
    """
    todo = [p for p in paths if not _is_protected(p, sandbox, results_dir)]
    # Untracked entries under a path that must become a file again go first.
    todo.sort(key=lambda p: (p.count("/"), p), reverse=True)
    removed: list[str] = []
    for rel in todo:
        path = sandbox / rel
        if rel in before.kept:
            _remove_path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(before.kept[rel], path, follow_symlinks=False)
            continue
        if rel in before.dirty:
            digest = before.dirty[rel]
            if digest is None:
                _remove_path(path)
                continue
            if digest.startswith("link:"):
                _remove_path(path)
                os.symlink(digest[5:], path)
                continue
            logger.warning("rsi.self_edit.restore_impossible", path=rel)
            continue
        if _in_index(sandbox, rel):
            if path.exists() and not path.is_symlink() and path.is_dir():
                _remove_path(path)
            elif path.is_symlink():
                path.unlink()
            _git(sandbox, "checkout", "--", rel)
        else:
            _remove_path(path)
            removed.append(rel)
    _prune_empty_parents(sandbox, removed)


def scaffold_head(sandbox: Path) -> str | None:
    """The sandbox repo's HEAD SHA, or ``None`` if there is no commit (or no repo). Blocking."""
    proc = _git(sandbox, "rev-parse", "--verify", "-q", "HEAD", check=False)
    if proc.returncode != 0:
        return None
    return proc.stdout.strip() or None


async def scaffold_head_async(sandbox: Path) -> str | None:
    """:func:`scaffold_head` off the event loop."""
    return await asyncio.to_thread(scaffold_head, sandbox)


def _has_conflict_markers(path: Path) -> bool:
    if path.is_symlink() or not path.is_file():
        return False
    text = path.read_text(encoding="utf-8", errors="replace")
    return any(line.startswith(("<<<<<<< ", "=======", ">>>>>>> ")) for line in text.splitlines())


def rollback_scaffold(sandbox: Path, sha: str) -> str:
    """``git revert --no-edit <sha>`` and return the revert commit's SHA. Blocking.

    Raises:
        ContractViolationError: ``sha`` is not a commit SHA (7–40 hex digits
            naming an existing commit), touches anything other than
            ``SCAFFOLD.md`` (it is not a scaffold commit), or the revert
            fails — in which case it is aborted first so the tree is left
            clean, without ``REVERT_HEAD`` or conflict markers.
    """
    sha = (sha or "").strip()
    if not _SHA_RE.match(sha):
        raise ContractViolationError("rollback needs a commit SHA (7-40 hex digits)")
    resolved = _git(sandbox, "rev-parse", "--verify", "-q", f"{sha}^{{commit}}", check=False)
    full = resolved.stdout.strip()
    if resolved.returncode != 0 or not full.startswith(sha):
        raise ContractViolationError(f"{sha} is not a commit in the sandbox repo")
    touched = _git(sandbox, "diff-tree", "--no-commit-id", "--name-only", "-r", full)
    names = [n for n in touched.stdout.splitlines() if n]
    if names != [SCAFFOLD_FILENAME]:
        raise ContractViolationError(
            f"refusing to revert {full[:12]}: it touches {names or 'nothing'}, "
            f"not only {SCAFFOLD_FILENAME}"
        )
    revert = _git(sandbox, "revert", "--no-edit", full, check=False)
    if revert.returncode != 0:
        _git(sandbox, "revert", "--abort", check=False)
        if (sandbox / ".git" / "REVERT_HEAD").exists():
            _git(sandbox, "reset", "-q", "--hard", "HEAD", check=False)
        if _has_conflict_markers(sandbox / SCAFFOLD_FILENAME):
            _git(sandbox, "checkout", "--", SCAFFOLD_FILENAME, check=False)
        raise ContractViolationError(
            f"git revert of {full[:12]} failed and was aborted: {revert.stderr.strip()}"
        )
    head = scaffold_head(sandbox)
    if head is None:
        raise ContractViolationError("git revert left no HEAD")
    logger.info("rsi.scaffold.rollback", reverted=full, revert_commit=head)
    return head


async def rollback_scaffold_async(sandbox: Path, sha: str) -> str:
    """:func:`rollback_scaffold` off the event loop."""
    return await asyncio.to_thread(rollback_scaffold, sandbox, sha)


# --------------------------------------------------------------------------- #
# The step
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Inspection:
    """What the engine run changed, compared against the snapshot."""

    changed: list[str]
    head_after: str | None
    index_tree_after: str | None
    committed: list[str]
    staged: list[str]
    internals: list[str]

    @property
    def repo_moved(self) -> bool:
        return bool(self.committed or self.staged) or self.head_after is None


class ScaffoldSelfEditStep:
    """:class:`~turing.research.rsi.contracts.SelfEditStep` backed by an :class:`Engine`.

    ``propose`` runs the engine in the sandbox with
    :func:`render_self_edit_prompt` and keeps the edit only if
    ``SCAFFOLD.md`` is the sole path that changed (see the module docstring).
    """

    def __init__(
        self,
        engine: Engine,
        config: RsiConfig,
        sandbox: Path,
        *,
        timeout_seconds: float | None = None,
    ) -> None:
        _assert_inside_config(config, sandbox)
        self._engine = engine
        self._config = config
        self._sandbox = sandbox
        self._timeout = (
            config.round_timeout_seconds if timeout_seconds is None else float(timeout_seconds)
        )
        if not self._timeout > 0:
            raise ContractViolationError("self-edit timeout must be positive")
        #: Length of the prompt the last ``propose`` actually sent; the loop
        #: records it on the trajectory event. ``None`` before the first call.
        self.last_prompt_chars: int | None = None

    async def propose(self, inputs: SelfEditInputs) -> str | None:
        prompt = render_self_edit_prompt(inputs)
        self.last_prompt_chars = len(prompt)
        sandbox = self._sandbox
        before = await asyncio.to_thread(self._prepare)
        try:
            result = await self._engine.run(prompt, cwd=sandbox, timeout_seconds=self._timeout)
            return await asyncio.to_thread(self._settle, inputs.round_index, before, result)
        finally:
            _drop_snapshot(before)

    # ------------------------------------------------------------ blocking

    def _prepare(self) -> _TreeSnapshot:
        _assert_toplevel(self._sandbox)
        _assert_plain_file_inside(
            self._sandbox / SCAFFOLD_FILENAME, self._sandbox, must_exist=False
        )
        return _snapshot(self._sandbox)

    def _inspect(self, before: _TreeSnapshot) -> _Inspection:
        sandbox = self._sandbox
        head_after = scaffold_head(sandbox)
        tree = _git(sandbox, "write-tree", check=False)
        index_after = tree.stdout.strip() if tree.returncode == 0 else None
        committed: list[str] = []
        if head_after is not None and head_after != before.head:
            diff = _git(sandbox, "diff", "--name-only", before.head, head_after, check=False)
            committed = sorted(p for p in diff.stdout.splitlines() if p.strip())
            if diff.returncode != 0:
                committed = ["<unreadable: HEAD diverged>"]
        staged: list[str] = []
        if index_after != before.index_tree:
            diff = _git(sandbox, "diff-index", "--cached", "--name-only", before.index_tree)
            staged = sorted(p for p in diff.stdout.splitlines() if p.strip())
        return _Inspection(
            changed=_changed_since(sandbox, before),
            head_after=head_after,
            index_tree_after=index_after,
            committed=committed,
            staged=staged,
            internals=_internals_changed(sandbox, before),
        )

    def _settle(
        self,
        round_index: int,
        before: _TreeSnapshot,
        result: EngineResult,
    ) -> str | None:
        sandbox = self._sandbox
        scaffold_path = sandbox / SCAFFOLD_FILENAME
        seen = self._inspect(before)

        # I1/I5 first, before anything is put back: the operator sees the tampered tree.
        guarded = before.guarded | {VERIFIER_LOCK_FILENAME}
        touched_verifier = sorted(
            set(seen.changed) & guarded | set(seen.committed) & guarded | set(seen.staged) & guarded
        )
        if touched_verifier:
            raise FrozenVerifierError(
                f"self-edit step touched {touched_verifier} (the verifier lock or a file it "
                "pins); the tree is left untouched for inspection and the loop must stop"
            )

        reason: str | None = None
        if result.timed_out or result.exit_code != 0:
            reason = f"engine failed (exit {result.exit_code}, timed_out={result.timed_out})"
        elif seen.internals:
            reason = f"engine changed repo internals .git/{seen.internals}"
        elif seen.repo_moved:
            what = "committed" if seen.committed or seen.head_after is None else "staged"
            reason = f"engine {what} changes itself ({seen.committed or seen.staged})"
        else:
            offending = [p for p in seen.changed if p != SCAFFOLD_FILENAME]
            if offending:
                reason = f"paths other than {SCAFFOLD_FILENAME} changed: {offending}"
            elif SCAFFOLD_FILENAME in seen.changed and (
                scaffold_path.is_symlink() or not scaffold_path.is_file()
            ):
                reason = f"{SCAFFOLD_FILENAME} is no longer a regular file"
            elif SCAFFOLD_FILENAME in seen.changed:
                reason = scaffold_size_problem(
                    scaffold_path.stat().st_size, previous=before.scaffold_bytes
                )

        if reason is not None:
            self._reject(round_index, before, seen, reason)
            return None

        if not seen.changed:
            logger.info("rsi.self_edit.unchanged", round=round_index)
            return None

        sha = self._commit(round_index, before, seen)
        if sha is None:
            return None
        logger.info("rsi.self_edit.committed", round=round_index, scaffold_sha=sha)
        return sha

    def _reject(
        self, round_index: int, before: _TreeSnapshot, seen: _Inspection, reason: str
    ) -> None:
        sandbox = self._sandbox
        logger.warning(
            "rsi.self_edit.rejected",
            round=round_index,
            reason=reason,
            paths=seen.changed,
            committed=seen.committed,
            staged=seen.staged,
            internals=seen.internals,
        )
        try:
            if seen.internals:
                _restore_internals(sandbox, before, seen.internals)
            if seen.repo_moved or seen.index_tree_after != before.index_tree:
                _restore_head_and_index(sandbox, before)
            # After HEAD/index are back, the engine's committed edits show up
            # as working-tree changes; discard those too.
            changed = seen.changed if not seen.repo_moved else _changed_since(sandbox, before)
            if changed:
                _discard(sandbox, changed, before, results_dir=self._config.results_dir)
        except (ContractViolationError, OSError) as exc:
            logger.error(
                "rsi.self_edit.discard_failed", round=round_index, error=str(exc), reason=reason
            )
            raise ContractViolationError(
                f"self-edit rejected ({reason}) but the sandbox could not be restored: {exc}"
            ) from exc

    def _commit(self, round_index: int, before: _TreeSnapshot, seen: _Inspection) -> str | None:
        sandbox = self._sandbox
        _git(sandbox, "add", "--", SCAFFOLD_FILENAME)
        listed = _git(sandbox, "ls-files", "-s", "-z", "--", SCAFFOLD_FILENAME).stdout
        mode = listed.split(" ", 1)[0] if listed else ""
        if mode != _REGULAR_FILE_MODE:
            _git(sandbox, "read-tree", before.index_tree)
            self._reject(round_index, before, seen, f"{SCAFFOLD_FILENAME} has mode {mode or '?'}")
            return None
        _git(
            sandbox,
            "commit",
            "-q",
            "--no-verify",
            "--only",
            "-m",
            f"rsi: self-edit after round {round_index}",
            "--",
            SCAFFOLD_FILENAME,
        )
        head = scaffold_head(sandbox)
        if head is None:
            raise ContractViolationError("git commit left no HEAD")
        return head
