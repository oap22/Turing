"""Contracts for the RSI workstation loop (``python -m turing.research.rsi``).

The RSI workstation is the loop ``scripts/rsi-loop.sh`` runs: one ``claude -p``
round after another in a sandbox directory, streaming ``trajectory.json`` and
``metrics.jsonl`` into a results directory the desktop panes watch. This
package turns that loop into a Python engine with a frozen verifier, a frozen
error taxonomy, a scaffold self-edit step with rollback, and a cheat detector.
Every other module in :mod:`turing.research.rsi` codes against the types here.

What this module guarantees:

* **The verifier is frozen (I1).** :class:`VerifierLock` pins the command
  string and the files it names by SHA-256; :func:`check_verifier_lock`
  raises :class:`~turing.research.contracts.FrozenVerifierError` naming what
  changed. The lock is a record, not a sandbox — the loop must call the check
  before *and* after every round, and stop on a raise.
* **The loop measures the score (I2).** :class:`RoundRecord` keeps the
  loop-measured ``score`` and the agent's ``agent_reported_score`` in separate
  fields; nothing here ever copies one into the other.
* **The self-edit summary is narrow (I4).** :class:`SelfEditInputs` is a
  closed record of aggregate stats and refuses to be built if the scaffold
  text or notes tail contains the verifier command or the lock-file marker.
* **The trajectory stays bash-compatible (I7).** :meth:`RoundRecord.to_json`
  emits ``round/started/ended/exit`` first, and :meth:`RoundRecord.from_json`
  reads a line the bash script wrote (those four keys only).

What it does not do:

* It does not run anything. :class:`Engine` and :class:`SelfEditStep` are
  protocols; the implementations live in sibling modules.
* It does not enforce the sandbox boundary. That is the cheat detector's job;
  :class:`CheatVerdict` only carries its verdict.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol

from turing.research.contracts import ContractViolationError, FrozenVerifierError
from turing.research.rsi.taxonomy import FailureCategory

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

__all__ = [
    "BASH_TRAJECTORY_KEYS",
    "SLUG_PATTERN",
    "VERIFIER_LOCK_FILENAME",
    "VERIFIER_LOCK_MARKER",
    "CheatVerdict",
    "ContractViolationError",
    "Engine",
    "EngineResult",
    "FrozenVerifierError",
    "LoopEvent",
    "RoundRecord",
    "RoundSummary",
    "RsiConfig",
    "SelfEditInputs",
    "SelfEditStep",
    "VerifierLock",
    "VerifierOutcome",
    "VerifierSpec",
    "check_verifier_lock",
    "compute_verifier_lock",
    "iter_forbidden",
    "parse_score",
    "sha256_file",
    "sha256_text",
]

#: Same regex as ``scripts/rsi-loop.sh`` and ``webui/src/desktop/rsi.ts``.
SLUG_PATTERN: re.Pattern[str] = re.compile(r"^[a-z0-9-]+$")

#: The lock file inside the sandbox. Its *name* is also the marker
#: :class:`SelfEditInputs` refuses in scaffold/notes text.
VERIFIER_LOCK_FILENAME: str = "VERIFIER.json"
VERIFIER_LOCK_MARKER: str = VERIFIER_LOCK_FILENAME

#: The keys the bash script writes, in the order it writes them.
BASH_TRAJECTORY_KEYS: tuple[str, ...] = ("round", "started", "ended", "exit")

_CHEAT_CATEGORIES: frozenset[FailureCategory] = frozenset(
    {
        FailureCategory.CHEAT_DETECTED,
        FailureCategory.SANDBOX_ESCAPE,
        FailureCategory.VERIFIER_TAMPERED,
    }
)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RsiConfig:
    """Resolved loop configuration; mirrors the bash script's flags plus the new ones.

    ``sandbox_dir`` and ``results_dir`` follow the bash layout exactly
    (``<workspace_root>/rsi-<slug>`` and ``<results_root>/loop-rsi-<slug>``)
    so the desktop panes keep working. A leading ``~`` is expanded, as the
    script does.
    """

    slug: str
    results_root: Path
    workspace_root: Path = field(default_factory=lambda: Path("~/turing-workspace"))
    rounds: int = 10
    self_edit_every: int = 3
    self_edit_budget: int = 3
    noise_floor: float | None = None
    round_timeout_seconds: float = 1800.0
    verifier_timeout_seconds: float = 600.0

    def __post_init__(self) -> None:
        if not isinstance(self.slug, str) or not SLUG_PATTERN.match(self.slug):
            raise ContractViolationError(
                f"slug must match {SLUG_PATTERN.pattern} (got {self.slug!r})"
            )
        for name in ("rounds", "self_edit_every", "self_edit_budget"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ContractViolationError(f"{name} must be a non-negative int (got {value!r})")
        for name in ("round_timeout_seconds", "verifier_timeout_seconds"):
            seconds = getattr(self, name)
            if not isinstance(seconds, (int, float)) or not seconds > 0 or math.isinf(seconds):
                raise ContractViolationError(f"{name} must be a positive finite number")
        if self.noise_floor is not None and (
            not math.isfinite(self.noise_floor) or self.noise_floor < 0
        ):
            raise ContractViolationError("noise_floor must be a non-negative finite float")
        object.__setattr__(self, "results_root", Path(self.results_root).expanduser())
        object.__setattr__(self, "workspace_root", Path(self.workspace_root).expanduser())

    @property
    def sandbox_dir(self) -> Path:
        return self.workspace_root / f"rsi-{self.slug}"

    @property
    def results_dir(self) -> Path:
        return self.results_root / f"loop-rsi-{self.slug}"


# --------------------------------------------------------------------------- #
# The frozen verifier
# --------------------------------------------------------------------------- #


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class VerifierSpec:
    """The verifier as the operator supplied it: a shell command plus the files it depends on.

    ``files`` are sandbox-relative paths whose content is part of the lock.
    The command's first token is added automatically when it names a file
    inside the sandbox (see :func:`compute_verifier_lock`).
    """

    command: str
    files: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.command, str) or not self.command.strip():
            raise ContractViolationError("verifier command must be a non-empty string")
        object.__setattr__(self, "files", tuple(self.files))
        for rel in self.files:
            _reject_unsafe_relative(rel, what="verifier file")


def _reject_unsafe_relative(rel: str, *, what: str) -> None:
    if not isinstance(rel, str) or not rel:
        raise ContractViolationError(f"{what} must be a non-empty sandbox-relative path")
    p = Path(rel)
    if p.is_absolute() or ".." in p.parts:
        raise ContractViolationError(
            f"{what} {rel!r} must be relative to the sandbox and may not climb out of it"
        )


@dataclass(frozen=True, slots=True)
class VerifierLock:
    """What ``VERIFIER.json`` holds: the command and the SHA-256 of everything it pins."""

    command: str
    command_sha256: str
    file_sha256s: Mapping[str, str]
    created_at_ms: int

    def __post_init__(self) -> None:
        if not self.command:
            raise ContractViolationError("verifier lock must carry a command")
        if self.command_sha256 != sha256_text(self.command):
            raise FrozenVerifierError(
                "verifier lock is internally inconsistent: command_sha256 does not match "
                "the command string"
            )
        object.__setattr__(self, "file_sha256s", MappingProxyType(dict(self.file_sha256s)))
        if isinstance(self.created_at_ms, bool) or not isinstance(self.created_at_ms, int):
            raise ContractViolationError("created_at_ms must be an int")

    def to_json(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "command_sha256": self.command_sha256,
            "file_sha256s": dict(sorted(self.file_sha256s.items())),
            "created_at_ms": self.created_at_ms,
        }

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> VerifierLock:
        try:
            return cls(
                command=str(payload["command"]),
                command_sha256=str(payload["command_sha256"]),
                file_sha256s={str(k): str(v) for k, v in dict(payload["file_sha256s"]).items()},
                created_at_ms=int(payload["created_at_ms"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise FrozenVerifierError(f"verifier lock is malformed: {exc!r}") from exc


def _first_token_file(command: str, sandbox: Path) -> str | None:
    """The command's first token, if it names an existing regular file inside the sandbox."""
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if not tokens:
        return None
    token = tokens[0]
    candidate = Path(token)
    if candidate.is_absolute() or ".." in candidate.parts:
        return None
    full = sandbox / candidate
    if not full.is_file():
        return None
    try:
        full.resolve().relative_to(sandbox.resolve())
    except ValueError:
        return None
    return candidate.as_posix()


def compute_verifier_lock(spec: VerifierSpec, sandbox: Path, *, now_ms: int) -> VerifierLock:
    """Hash the command and every file it pins.

    Raises:
        ContractViolationError: a listed file is missing or is not a regular
            file inside the sandbox.
    """
    to_hash: dict[str, str] = {}
    first = _first_token_file(spec.command, sandbox)
    names = list(spec.files)
    if first is not None and first not in names:
        names.insert(0, first)
    for rel in names:
        full = sandbox / rel
        if not full.is_file():
            raise ContractViolationError(
                f"verifier file {rel!r} is not a regular file in the sandbox {sandbox}"
            )
        try:
            full.resolve().relative_to(sandbox.resolve())
        except ValueError:
            raise ContractViolationError(
                f"verifier file {rel!r} resolves outside the sandbox {sandbox}"
            ) from None
        to_hash[Path(rel).as_posix()] = sha256_file(full)
    return VerifierLock(
        command=spec.command,
        command_sha256=sha256_text(spec.command),
        file_sha256s=to_hash,
        created_at_ms=now_ms,
    )


def check_verifier_lock(
    lock: VerifierLock, sandbox: Path, *, expected_command: str | None = None
) -> None:
    """Re-verify the lock against the sandbox. Raise naming what changed.

    Args:
        lock: the lock read back from ``VERIFIER.json``.
        sandbox: the sandbox directory.
        expected_command: when the operator passed ``--verifier`` on a resume,
            the command they passed. A different command is refused loudly
            rather than silently ignored.

    Raises:
        FrozenVerifierError: the command hash, a pinned file's hash, a pinned
            file's existence, or the expected command differs.
    """
    if expected_command is not None and expected_command != lock.command:
        raise FrozenVerifierError(
            f"--verifier {expected_command!r} differs from the locked verifier "
            f"{lock.command!r} in {VERIFIER_LOCK_FILENAME}; the lock wins and a "
            "different command is refused, not ignored"
        )
    if sha256_text(lock.command) != lock.command_sha256:
        raise FrozenVerifierError(
            f"verifier command hash changed: {VERIFIER_LOCK_FILENAME} command_sha256 "
            "does not match its own command string"
        )
    for rel, expected in lock.file_sha256s.items():
        full = sandbox / rel
        if not full.is_file():
            raise FrozenVerifierError(f"verifier file {rel!r} is missing from the sandbox")
        actual = sha256_file(full)
        if actual != expected:
            raise FrozenVerifierError(
                f"verifier file {rel!r} changed: locked sha256 {expected[:12]}…, now {actual[:12]}…"
            )


# --------------------------------------------------------------------------- #
# Verifier outcome and score parsing
# --------------------------------------------------------------------------- #

_SCORE_LINE: re.Pattern[str] = re.compile(r"^score=([-+0-9.eE]+)$")


def parse_score(stdout: str) -> float | None:
    """The float from the LAST line matching ``^score=([-+0-9.eE]+)$`` (whitespace-stripped).

    ``score = 1`` (spaces) does not match. A matching line whose payload is
    not a finite float (``score=1e999``, ``score=.``) is skipped and the scan
    continues to earlier lines; no match at all → ``None``.
    """
    for raw in reversed(stdout.splitlines()):
        m = _SCORE_LINE.match(raw.strip())
        if m is None:
            continue
        try:
            value = float(m.group(1))
        except ValueError:
            continue
        if math.isfinite(value):
            return value
    return None


@dataclass(frozen=True, slots=True)
class VerifierOutcome:
    """One verifier run, as the loop measured it.

    ``passed`` is defined as ``exit_code == 0`` and nothing else; a mismatch
    is a construction error so no call site can invent a pass.
    """

    exit_code: int
    score: float | None
    passed: bool
    stdout_tail: str
    wall_seconds: float

    def __post_init__(self) -> None:
        if self.passed != (self.exit_code == 0):
            raise ContractViolationError(
                f"passed={self.passed} contradicts exit_code={self.exit_code}; "
                "pass/fail is the verifier's exit code"
            )
        if self.score is not None and not math.isfinite(self.score):
            raise ContractViolationError("score must be finite or None")
        if self.wall_seconds < 0:
            raise ContractViolationError("wall_seconds must be non-negative")


# --------------------------------------------------------------------------- #
# Engine
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class EngineResult:
    """What one engine invocation produced."""

    exit_code: int
    stdout: str
    stderr: str
    wall_seconds: float
    timed_out: bool

    def __post_init__(self) -> None:
        if self.wall_seconds < 0:
            raise ContractViolationError("wall_seconds must be non-negative")


class Engine(Protocol):
    """Runs one round's prompt in ``cwd`` and returns when it exits or times out."""

    async def run(self, prompt: str, *, cwd: Path, timeout_seconds: float) -> EngineResult: ...


# --------------------------------------------------------------------------- #
# Trajectory lines
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RoundRecord:
    """One round's trajectory line.

    ``score`` is what the loop measured with the verifier;
    ``agent_reported_score`` is what the agent wrote to ``metrics.jsonl`` and
    is informational only (I2). ``void`` marks a round that does not count
    (cheat, tamper); a void round must carry at least one category.
    """

    round: int
    started: int
    ended: int
    exit: int
    score: float | None = None
    passed: bool = False
    categories: frozenset[FailureCategory] = frozenset()
    scaffold_sha: str | None = None
    void: bool = False
    agent_reported_score: float | None = None
    verifier_wall_seconds: float | None = None
    #: Characters in the prompt the engine was given (scaffold included);
    #: ``None`` on lines written before the loop recorded it. Token spend is
    #: proportional, so this is the per-round efficiency number.
    prompt_chars: int | None = None

    def __post_init__(self) -> None:
        if self.round < 1:
            raise ContractViolationError(f"round must be >= 1 (got {self.round})")
        if self.ended < self.started:
            raise ContractViolationError("ended must not precede started")
        object.__setattr__(self, "categories", frozenset(self.categories))
        for c in self.categories:
            if not isinstance(c, FailureCategory):
                raise ContractViolationError(f"category {c!r} is not a FailureCategory")
        if self.void and not self.categories:
            raise ContractViolationError("a void round must carry the category that voided it")
        for name in ("score", "agent_reported_score"):
            v = getattr(self, name)
            if v is not None and not math.isfinite(v):
                raise ContractViolationError(f"{name} must be finite or None")
        if self.verifier_wall_seconds is not None and self.verifier_wall_seconds < 0:
            raise ContractViolationError("verifier_wall_seconds must be non-negative")
        if self.prompt_chars is not None and (
            isinstance(self.prompt_chars, bool) or self.prompt_chars < 0
        ):
            raise ContractViolationError("prompt_chars must be a non-negative int or None")

    @property
    def improved(self) -> bool:
        """The success sentinel: passed, not void, and no category at all."""
        return self.passed and not self.void and not self.categories

    def to_json(self) -> dict[str, Any]:
        """Bash-compatible keys first, then the engine's additions."""
        return {
            "round": self.round,
            "started": self.started,
            "ended": self.ended,
            "exit": self.exit,
            "score": self.score,
            "passed": self.passed,
            "categories": sorted(c.value for c in self.categories),
            "scaffold_sha": self.scaffold_sha,
            "void": self.void,
            "agent_reported_score": self.agent_reported_score,
            "verifier_wall_seconds": self.verifier_wall_seconds,
            "prompt_chars": self.prompt_chars,
        }

    def to_json_line(self) -> str:
        return json.dumps(self.to_json(), separators=(", ", ": "))

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> RoundRecord:
        """Read a line; a bash-script line (four keys) loads with defaults."""
        if "event" in payload:
            raise ContractViolationError("payload is an event line, not a round record")
        try:
            categories = frozenset(FailureCategory(v) for v in payload.get("categories", ()))
            return cls(
                round=int(payload["round"]),
                started=int(payload["started"]),
                ended=int(payload["ended"]),
                exit=int(payload["exit"]),
                score=_opt_float(payload.get("score")),
                passed=bool(payload.get("passed", False)),
                categories=categories,
                scaffold_sha=_opt_str(payload.get("scaffold_sha")),
                void=bool(payload.get("void", False)),
                agent_reported_score=_opt_float(payload.get("agent_reported_score")),
                verifier_wall_seconds=_opt_float(payload.get("verifier_wall_seconds")),
                prompt_chars=_opt_int(payload.get("prompt_chars")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ContractViolationError(f"malformed trajectory line: {exc!r}") from exc


def _opt_int(v: object) -> int | None:
    return None if v is None else int(v)  # type: ignore[call-overload]


def _opt_float(v: object) -> float | None:
    return None if v is None else float(v)  # type: ignore[arg-type]


def _opt_str(v: object) -> str | None:
    return None if v is None else str(v)


@dataclass(frozen=True, slots=True)
class LoopEvent:
    """A non-round trajectory line (``self_edit``, ``self_edit_rejected``, ``rollback`` …).

    Distinguished from a :class:`RoundRecord` line by the ``event`` key; the
    resume logic skips these when counting rounds. ``details`` are flattened
    into the line and may not shadow the three fixed keys.
    """

    event: str
    round: int
    ts: int
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.event:
            raise ContractViolationError("event name must be non-empty")
        if self.round < 0:
            raise ContractViolationError("event round must be >= 0")
        details = dict(self.details)
        clash = {"event", "round", "ts"} & details.keys()
        if clash:
            raise ContractViolationError(f"event details may not shadow {sorted(clash)}")
        object.__setattr__(self, "details", MappingProxyType(details))

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {"event": self.event, "round": self.round, "ts": self.ts}
        out.update(self.details)
        return out

    def to_json_line(self) -> str:
        return json.dumps(self.to_json(), separators=(", ", ": "))

    @classmethod
    def from_json(cls, payload: Mapping[str, Any]) -> LoopEvent:
        if "event" not in payload:
            raise ContractViolationError("payload is not an event line")
        try:
            details = {k: v for k, v in payload.items() if k not in {"event", "round", "ts"}}
            return cls(
                event=str(payload["event"]),
                round=int(payload["round"]),
                ts=int(payload["ts"]),
                details=details,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ContractViolationError(f"malformed event line: {exc!r}") from exc


# --------------------------------------------------------------------------- #
# Self-edit (loop 2, scoped to the RSI workstation)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class RoundSummary:
    """The per-round aggregate a self-edit step may see: no stdout, no paths."""

    round: int
    score: float | None
    passed: bool
    categories: tuple[str, ...]
    wall_seconds: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "categories", tuple(sorted(self.categories)))

    @classmethod
    def from_record(cls, record: RoundRecord) -> RoundSummary:
        return cls(
            round=record.round,
            score=record.score,
            passed=record.passed,
            categories=tuple(c.value for c in record.categories),
            wall_seconds=float(record.ended - record.started),
        )


@dataclass(frozen=True, slots=True)
class SelfEditInputs:
    """Everything the self-edit step may read. Aggregate stats only (I4).

    Refuses construction if ``scaffold_text`` or ``notes_tail`` contains
    :data:`VERIFIER_LOCK_MARKER` or any of ``forbidden`` — the loop passes the
    verifier command there. An empty forbidden string is refused (it would
    match everything).
    """

    round_index: int
    best_score: float | None
    rounds: tuple[RoundSummary, ...]
    taxonomy_counts: Mapping[str, int]
    scaffold_text: str
    notes_tail: str
    forbidden: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.round_index < 0:
            raise ContractViolationError("round_index must be >= 0")
        object.__setattr__(self, "rounds", tuple(self.rounds))
        object.__setattr__(self, "forbidden", tuple(self.forbidden))
        object.__setattr__(self, "taxonomy_counts", MappingProxyType(dict(self.taxonomy_counts)))
        for r in self.rounds:
            if not isinstance(r, RoundSummary):
                raise ContractViolationError("rounds must be RoundSummary records")
        needles = (VERIFIER_LOCK_MARKER, *self.forbidden)
        for needle in needles:
            if not needle:
                raise ContractViolationError("forbidden substrings must be non-empty")
            for label in ("scaffold_text", "notes_tail"):
                text: str = getattr(self, label)
                if needle in text:
                    shown = needle if needle == VERIFIER_LOCK_MARKER else "<verifier internals>"
                    raise ContractViolationError(
                        f"{label} contains {shown!r}; the self-edit summary may not carry "
                        "verifier internals"
                    )


class SelfEditStep(Protocol):
    """Proposes a ``SCAFFOLD.md`` edit from ``inputs`` alone.

    Returns the new scaffold commit SHA, or ``None`` when no edit was kept
    (nothing changed, or the edit touched something other than SCAFFOLD.md
    and was discarded).
    """

    async def propose(self, inputs: SelfEditInputs) -> str | None: ...


# --------------------------------------------------------------------------- #
# Cheat detector verdict
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class CheatVerdict:
    """What the cheat detector concluded about one round.

    ``fired`` iff ``categories`` is non-empty; categories are restricted to
    the detector's own (``cheat_detected``, ``sandbox_escape``,
    ``verifier_tampered``); a fired verdict must say why.
    """

    fired: bool
    categories: frozenset[FailureCategory] = frozenset()
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "categories", frozenset(self.categories))
        object.__setattr__(self, "reasons", tuple(self.reasons))
        if self.fired != bool(self.categories):
            raise ContractViolationError("fired must be True exactly when categories is non-empty")
        bad = self.categories - _CHEAT_CATEGORIES
        if bad:
            raise ContractViolationError(
                f"cheat verdict may only carry detector categories, not {sorted(c.value for c in bad)}"
            )
        if self.fired and not any(r.strip() for r in self.reasons):
            raise ContractViolationError("a fired cheat verdict must give at least one reason")


def iter_forbidden(lock: VerifierLock) -> Iterable[str]:
    """Substrings the self-edit summary must not contain, derived from the lock."""
    yield lock.command
    yield lock.command_sha256
