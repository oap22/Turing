"""Escalation delivery: rich request out over ntfy, three-valued decision in.

The loop runs fully unattended and **an escalation is its only interrupt**. So
this module has two jobs and one prohibition.

*Out* — :class:`NtfyEscalationNotifier` pushes the request to the operator's
ntfy topic, reusing :class:`~turing.coordinator.alerts.ntfy_client.NtfyAlertClient`
unchanged. That client was written as the closed-laptop fallback for hardware
alerts; an unattended research loop that must reach the operator at 2am is the
same problem. It never raises, which is right for alerts and dangerous here —
a dropped push would leave the loop suspended with nobody informed — so the
channel **re-pushes on an interval** for as long as it waits.

*In* — :class:`FileDropDecisionInbox` watches a directory for
``<request_id>.decision.json``. A file drop rather than a socket because the
loop must survive its own process dying: the request file and the attempt
checkpoint are both on disk, so a decision written while the loop is down is
picked up when it comes back.

*The prohibition* — the reply vocabulary is exactly ``continue`` / ``abandon``
/ ``extend_cap``, plus numbers for ``extend_cap``. :func:`decode_decision`
rejects any unknown key, and it rejects them loudly rather than ignoring them,
because the thing that would arrive in an unknown key is advice. Free-form
guidance ("try gradient boosting on that one") would make the operator the
improvement mechanism, confound the next round's delta, and turn human-gate
load into a measure of the operator's ML knowledge. A malformed or advice-
bearing reply is moved aside and the operator is told why; the loop keeps
waiting, because inventing a decision would erase a driving-function #4 event.

The operator answers with :mod:`turing.research.loop.cli`::

    python -m turing.research.loop.cli --loop-dir ~/research-results/loop-x \\
        <request_id> extend_cap --extra-steps 200
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from turing.research.contracts import (
    CapExtension,
    EscalationDecision,
    EscalationProtocolError,
    EscalationVerdict,
)
from turing.research.loop.protocols import SystemClock
from turing.research.loop.settings import ResearchLoopSettings

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

    from turing.coordinator.alerts.ntfy_client import NtfyAlertClient
    from turing.research.contracts import EscalationRequest
    from turing.research.loop.protocols import Clock

logger = structlog.get_logger(__name__)

__all__ = [
    "ALLOWED_DECISION_KEYS",
    "ALLOWED_EXTENSION_KEYS",
    "DECISION_SUFFIX",
    "REQUEST_SUFFIX",
    "FileDropDecisionInbox",
    "NtfyEscalationNotifier",
    "OperatorEscalationChannel",
    "OperatorEscalationGate",
    "build_operator_channel",
    "decision_filename",
    "decode_decision",
    "encode_decision",
    "encode_request",
    "request_filename",
]

REQUEST_SUFFIX = ".request.json"
DECISION_SUFFIX = ".decision.json"

#: The complete reply schema. Anything else is advice by another name.
ALLOWED_DECISION_KEYS = frozenset({"request_id", "verdict", "decided_at_ms", "cap_extension"})
ALLOWED_EXTENSION_KEYS = frozenset({"extra_steps", "extra_tokens", "extra_wall_clock_seconds"})

_DEFAULT_POLL_SECONDS = 5.0
_DEFAULT_REPUSH_SECONDS = 1800.0


def request_filename(request_id: str) -> str:
    return f"{request_id}{REQUEST_SUFFIX}"


def decision_filename(request_id: str) -> str:
    return f"{request_id}{DECISION_SUFFIX}"


# --------------------------------------------------------------------------- #
# Serialisation
# --------------------------------------------------------------------------- #


def encode_request(request: EscalationRequest) -> dict[str, Any]:
    """Render a request for the operator. Information flows outward freely."""
    best = request.best_result
    return {
        "request_id": request.request_id,
        "problem_id": request.problem_id,
        "attempt_id": request.attempt_id,
        "round_id": request.round_id,
        "reason": request.reason.value,
        "summary": request.summary,
        "created_at_ms": request.created_at_ms,
        "cap": {
            "max_steps": request.cap.max_steps,
            "max_tokens": request.cap.max_tokens,
            "max_wall_clock_seconds": request.cap.max_wall_clock_seconds,
            "extension_count": request.cap.extension_count,
        },
        "consumed": {
            "steps": request.consumed.steps,
            "tokens": request.consumed.tokens,
            "wall_clock_seconds": request.consumed.wall_clock_seconds,
        },
        "best_result": None
        if best is None
        else {
            "score": best.score,
            "score_scale": best.score_scale,
            "passed_correctness": best.passed_correctness,
            "detail": best.detail,
        },
        "reply_with": sorted(v.value for v in EscalationVerdict),
    }


def encode_decision(decision: EscalationDecision) -> dict[str, Any]:
    """Render a decision. There is no field here for advice, by design."""
    payload: dict[str, Any] = {
        "request_id": decision.request_id,
        "verdict": decision.verdict.value,
        "decided_at_ms": decision.decided_at_ms,
    }
    if decision.cap_extension is not None:
        payload["cap_extension"] = {
            "extra_steps": decision.cap_extension.extra_steps,
            "extra_tokens": decision.cap_extension.extra_tokens,
            "extra_wall_clock_seconds": decision.cap_extension.extra_wall_clock_seconds,
        }
    return payload


def _require_mapping(value: object, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise EscalationProtocolError(f"{what} must be a JSON object, got {type(value).__name__}")
    return value


def decode_decision(
    payload: object, *, expected_request_id: str | None = None
) -> EscalationDecision:
    """Parse an operator reply, strictly.

    Raises:
        EscalationProtocolError: unknown keys (the shape advice would arrive
            in), an unknown verdict, a mismatched request id, or a numerically
            invalid cap extension.
    """
    body = _require_mapping(payload, "an escalation decision")
    unknown = set(body) - ALLOWED_DECISION_KEYS
    if unknown:
        raise EscalationProtocolError(
            f"decision carries unsupported field(s) {sorted(unknown)}; the operator's reply "
            "vocabulary is exactly continue/abandon/extend_cap plus a numeric cap extension. "
            "Guidance would make the operator the improvement mechanism and confound the "
            "next round's delta"
        )
    for required in ("request_id", "verdict"):
        if required not in body:
            raise EscalationProtocolError(f"decision is missing {required!r}")
    request_id = body["request_id"]
    if not isinstance(request_id, str):
        raise EscalationProtocolError("request_id must be a string")
    if expected_request_id is not None and request_id != expected_request_id:
        raise EscalationProtocolError(
            f"decision answers {request_id!r}, not the open request {expected_request_id!r}"
        )
    raw_verdict = body["verdict"]
    if not isinstance(raw_verdict, str):
        raise EscalationProtocolError("verdict must be a string")
    try:
        verdict = EscalationVerdict(raw_verdict)
    except ValueError as exc:
        allowed = ", ".join(sorted(v.value for v in EscalationVerdict))
        raise EscalationProtocolError(
            f"{raw_verdict!r} is not an operator verdict; reply with one of: {allowed}"
        ) from exc
    decided_at_ms = body.get("decided_at_ms", 0)
    if not isinstance(decided_at_ms, int) or isinstance(decided_at_ms, bool):
        raise EscalationProtocolError("decided_at_ms must be an integer")
    extension: CapExtension | None = None
    if "cap_extension" in body and body["cap_extension"] is not None:
        raw = _require_mapping(body["cap_extension"], "cap_extension")
        unknown_ext = set(raw) - ALLOWED_EXTENSION_KEYS
        if unknown_ext:
            raise EscalationProtocolError(
                f"cap_extension carries unsupported field(s) {sorted(unknown_ext)}; an "
                "extension is pure budget and carries no information about how to spend it"
            )
        for key, value in raw.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise EscalationProtocolError(f"cap_extension.{key} must be a number")
        extension = CapExtension(
            extra_steps=int(raw.get("extra_steps", 0)),
            extra_tokens=int(raw.get("extra_tokens", 0)),
            extra_wall_clock_seconds=float(raw.get("extra_wall_clock_seconds", 0.0)),
        )
    # EscalationDecision.__post_init__ enforces the extend_cap/extension pairing.
    return EscalationDecision(
        request_id=request_id,
        verdict=verdict,
        decided_at_ms=decided_at_ms,
        cap_extension=extension,
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    """Atomic-enough write: temp file in the same directory, then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


# --------------------------------------------------------------------------- #
# Outward: ntfy
# --------------------------------------------------------------------------- #


class NtfyEscalationNotifier:
    """Pushes one escalation line to the operator's ntfy topic.

    Wraps the coordinator's existing client rather than reimplementing it. The
    client swallows every failure (correct for hardware alerts, where the alert
    engine must not be perturbed); here a swallowed failure means an unattended
    loop waits on a human who was never told, so the caller re-pushes on an
    interval and this class logs every attempt.
    """

    def __init__(self, client: NtfyAlertClient, *, decision_hint: str = "") -> None:
        self._client = client
        self._decision_hint = decision_hint

    async def notify(self, request: EscalationRequest, *, attempt_number: int = 1) -> None:
        lines = [
            f"[turing-research] escalation ({request.reason.value})",
            f"problem {request.problem_id} · round {request.round_id}",
            request.summary,
        ]
        if request.best_result is not None:
            lines.append(
                f"best so far: {request.best_result.score:.4g} "
                f"{request.best_result.score_scale} "
                f"(correct={request.best_result.passed_correctness})"
            )
        lines.append(f"reply: continue | abandon | extend_cap — id {request.request_id}")
        if self._decision_hint:
            lines.append(self._decision_hint)
        await self._client.ntfy_push("\n".join(lines))
        logger.info(
            "research.escalation.pushed",
            request_id=request.request_id,
            problem_id=request.problem_id,
            reason=request.reason.value,
            attempt_number=attempt_number,
        )


# --------------------------------------------------------------------------- #
# Inward: file drop
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _RejectedReply:
    path: Path
    error: str


class FileDropDecisionInbox:
    """Watches a directory for ``<request_id>.decision.json``.

    On-disk on purpose: the loop suspends across process restarts and even
    across a closed subscription window, so the channel has to be state that
    outlives the process. :meth:`publish` writes the request beside it so the
    operator can read the full context the push only summarised.
    """

    def __init__(
        self,
        directory: Path,
        *,
        clock: Clock | None = None,
    ) -> None:
        self._dir = directory
        self._clock = clock or SystemClock()
        #: Why the last operator reply was refused, if one was — surfaced so a
        #: caller can tell the operator what to fix.
        self.last_rejection: _RejectedReply | None = None

    @property
    def directory(self) -> Path:
        return self._dir

    def request_path(self, request_id: str) -> Path:
        return self._dir / request_filename(request_id)

    def decision_path(self, request_id: str) -> Path:
        return self._dir / decision_filename(request_id)

    async def publish(self, request: EscalationRequest) -> Path:
        path = self.request_path(request.request_id)
        await asyncio.to_thread(_write_json, path, encode_request(request))
        logger.info(
            "research.escalation.published",
            request_id=request.request_id,
            path=str(path),
        )
        return path

    async def poll_once(self, request_id: str) -> EscalationDecision | None:
        """Return a valid decision, or ``None`` if none has arrived yet.

        An invalid reply is moved aside as ``.rejected-<ms>.json`` and reported
        through :attr:`last_rejection`, never silently accepted and never
        allowed to terminate the wait.
        """
        path = self.decision_path(request_id)
        raw = await asyncio.to_thread(self._read_if_present, path)
        if raw is None:
            return None
        try:
            payload = json.loads(raw)
            return decode_decision(payload, expected_request_id=request_id)
        except (json.JSONDecodeError, EscalationProtocolError) as exc:
            rejected = self._dir / f"{request_id}.rejected-{self._clock.now_ms()}.json"
            await asyncio.to_thread(path.replace, rejected)
            self.last_rejection = _RejectedReply(path=rejected, error=str(exc))
            logger.error(
                "research.escalation.reply_rejected",
                request_id=request_id,
                moved_to=str(rejected),
                error=str(exc),
            )
            return None

    @staticmethod
    def _read_if_present(path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None


class OperatorEscalationChannel:
    """The loop's only interrupt: push, suspend, resume on a decision.

    Implements :class:`~turing.research.loop.protocols.EscalationChannel`. It
    **never times out into a default verdict** — a timeout that resolved to
    ``continue`` would delete a human-gate-load event, and human-gate load is
    the number the self-improvement claim lives or dies on. It waits, and it
    keeps reminding the operator that it is waiting.
    """

    def __init__(
        self,
        inbox: FileDropDecisionInbox,
        notifier: NtfyEscalationNotifier | None = None,
        *,
        poll_interval_seconds: float = _DEFAULT_POLL_SECONDS,
        repush_interval_seconds: float | None = _DEFAULT_REPUSH_SECONDS,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        clock: Clock | None = None,
    ) -> None:
        if poll_interval_seconds <= 0:
            raise ValueError("poll_interval_seconds must be positive")
        self._inbox = inbox
        self._notifier = notifier
        self._poll = poll_interval_seconds
        self._repush = repush_interval_seconds
        self._sleep = sleep or asyncio.sleep
        self._clock = clock or SystemClock()

    async def request_decision(
        self, request: EscalationRequest, *, reopened: bool = False
    ) -> EscalationDecision:
        """Publish, page, and poll until the operator answers.

        ``reopened`` (a restart re-entering a wait a previous process died in)
        changes one thing: the first push is skipped. The operator was paged
        when the request was raised, and a decision may already be sitting in
        the inbox — so this polls first, and if nothing has arrived it falls
        into the ordinary reminder cadence (``repush_interval_seconds`` after
        the restart, then every interval), rather than paging again on every
        restart for a question that was asked once. The request file is
        re-published either way: that is idempotent, and it keeps
        ``cli --list`` truthful if the earlier process died between publish
        and push. The cost of the choice, stated: if that earlier process died
        *before* its push, the operator first hears of the request one repush
        interval after the restart — and with reminders disabled
        (``repush_interval_seconds=None``) not at all until the next fresh
        escalation. Reminders are on by default; that is what they are for.
        """
        await self._inbox.publish(request)
        pushes = 0
        waited = 0.0
        if reopened:
            logger.info(
                "research.escalation.reopened",
                request_id=request.request_id,
                problem_id=request.problem_id,
                detail=(
                    "re-entering a wait a previous process died in; polling for the answer "
                    "before paging again, then keeping the reminder cadence"
                ),
            )
        while True:
            if self._notifier is not None and (
                (pushes == 0 and not reopened)
                or (self._repush is not None and waited >= self._repush)
            ):
                pushes += 1
                waited = 0.0
                await self._notifier.notify(request, attempt_number=pushes)
            decision = await self._inbox.poll_once(request.request_id)
            if decision is not None:
                logger.info(
                    "research.escalation.decided",
                    request_id=request.request_id,
                    verdict=decision.verdict.value,
                    reminders_sent=pushes,
                )
                return decision
            await self._sleep(self._poll)
            waited += self._poll


def build_operator_channel(
    escalations_dir: Path,
    settings: ResearchLoopSettings | None = None,
    *,
    clock: Clock | None = None,
) -> OperatorEscalationChannel:
    """Wire the production channel from config.

    Reuses :class:`~turing.coordinator.alerts.ntfy_client.NtfyAlertClient`
    as-is — self-hosted ntfy on the Surface coordinator, per-operator topic,
    built as the closed-laptop fallback. An unattended loop that must reach the
    operator at 2am is exactly what it was written for.

    With no topic or base URL configured the push is disabled and the channel
    still suspends and still waits for a decision file: an unreachable operator
    must never resolve into a default verdict.
    """
    from turing.coordinator.alerts.ntfy_client import NtfyAlertClient

    resolved = settings or ResearchLoopSettings()
    notifier = NtfyEscalationNotifier(
        NtfyAlertClient(resolved.coordinator_ntfy_base_url, resolved.operator_ntfy_topic),
        decision_hint=(
            f"python -m turing.research.loop.cli --loop-dir {escalations_dir} "
            "<request_id> continue|abandon|extend_cap"
        ),
    )
    return OperatorEscalationChannel(
        FileDropDecisionInbox(escalations_dir, clock=clock),
        notifier,
        poll_interval_seconds=resolved.research_escalation_poll_seconds,
        repush_interval_seconds=resolved.research_escalation_repush_seconds,
        clock=clock,
    )


class OperatorEscalationGate:
    """The same delivery, in the non-blocking shape a per-attempt driver wants.

    :class:`OperatorEscalationChannel` blocks until the operator answers, which
    is what the round runner wants: a round is a single unattended pass and an
    escalation is its only interrupt. A driver that checkpoints at
    ``ESCALATED`` and returns instead — so a closed subscription window costs
    the remainder of the attempt rather than a blocked process — wants the two
    halves separately:

    * :meth:`raise_escalation` — publish and push, never wait.
    * :meth:`await_decision` — one poll; ``None`` means nobody has answered
      yet, which is the normal unattended case.

    Both shapes share one inbox directory and one ntfy topic, so a request
    raised through either is answered by the same operator CLI and counts once
    toward the round's human-gate load.

    Duplicate structurally rather than by import on purpose: this module has no
    dependency on :mod:`turing.research.solver`, and structural typing is what
    lets both packages evolve without a coupling.
    """

    def __init__(
        self,
        inbox: FileDropDecisionInbox,
        notifier: NtfyEscalationNotifier | None = None,
    ) -> None:
        self._inbox = inbox
        self._notifier = notifier

    @property
    def inbox(self) -> FileDropDecisionInbox:
        return self._inbox

    async def raise_escalation(self, request: EscalationRequest) -> None:
        await self._inbox.publish(request)
        if self._notifier is not None:
            await self._notifier.notify(request)

    async def await_decision(self, request_id: str) -> EscalationDecision | None:
        return await self._inbox.poll_once(request_id)
