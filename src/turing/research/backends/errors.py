"""Failure modes of the model-backend seam.

Deliberately provider-neutral: every implementation raises from this hierarchy,
so the solver branches on *what happened* rather than on which vendor SDK
raised. Nothing here names a vendor.

The distinction the solver cares about most is
:class:`BackendCapacityError` versus everything else. Capacity means "the
budget window closed" — the attempt should be paused and checkpointed, not
failed. An interruption must cost the remainder of an attempt, not the attempt.

Every error may carry a :class:`~turing.research.backends.accounting.Usage`
describing what was consumed *before* the failure. The accounting layer adds it
to the ledger before re-raising, because tokens spent on a call that then blew
up are still tokens spent, and a cap that misses them is a brake that does not
brake.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from turing.research.backends.accounting import Usage


class BackendError(Exception):
    """Base class for every backend failure.

    ``usage`` is what the failed call is known (or conservatively believed) to
    have consumed. Leave it ``None`` when nothing is known — the accounting
    layer then falls back to a conservative estimate rather than to zero.

    ``provider_calls`` is how many requests were actually shipped before the
    failure. It is diagnostic rather than a cap dimension, but a failure that
    burned three round trips and reports one hides the shape of a retry storm.
    """

    def __init__(
        self,
        message: str,
        *,
        usage: Usage | None = None,
        provider_calls: int = 1,
    ) -> None:
        super().__init__(message)
        self.usage = usage
        self.provider_calls = provider_calls


class BackendConfigurationError(BackendError):
    """The backend cannot run as configured — missing credentials, bad policy."""


class BackendCapacityError(BackendError):
    """The provider refused for capacity reasons after exhausting retries.

    Rate limits, quota exhaustion, overload. The correct response is to
    checkpoint the attempt and resume later, not to record a failure: the
    budget window closing says nothing about whether the problem was solvable.
    """


class BackendProtocolError(BackendError):
    """The provider returned something the backend cannot interpret."""


class BackendContextLengthError(BackendProtocolError):
    """The request did not fit the model's context window.

    Distinct from a generic protocol error because the recovery is specific and
    available: compact the transcript and try again. Undifferentiated, it reads
    as "the request was malformed", and the caller retries the same over-long
    transcript or gives up on a problem it could still have solved.

    A subclass of :class:`BackendProtocolError` so callers that do not care
    keep their existing handling.
    """


class BackendAccountingError(BackendError):
    """A ledger invariant was violated.

    Raised rather than silently absorbed. Under-counting consumption defeats
    the runaway brake, so an accounting bug must be loud.
    """


class BackendResumeError(BackendError):
    """A resume token could not be applied to this backend.

    Includes the engine-drift case: a token minted against one engine
    configuration must not silently resume on another, or a round's numbers
    stop being attributable to the engine that produced them.
    """


class BackendNotImplementedError(BackendError, NotImplementedError):
    """A documented seam that has no implementation yet."""


class ScriptExhaustedError(BackendError):
    """A scripted test backend ran out of scripted turns.

    Surfacing this beats looping or returning empty responses: a test that
    makes more calls than it scripted has a bug worth failing on.
    """
