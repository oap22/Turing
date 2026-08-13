"""Offline doubles for the Claude backend.

Nothing in this package touches the network. The Claude backend takes its SDK
client by injection precisely so its retry, parsing, and accounting paths can
be driven from here.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import pytest

from turing.research.backends import GenerationRequest, ModelMessage, ModelTier
from turing.research.backends.tiering import TieringPolicy

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

#: Environment variables :class:`BackendSettings` reads. Every one of them is a
#: variable the operator plausibly has set in the shell that runs the suite.
_SETTINGS_ENV_PREFIXES = ("TURING_RESEARCH_",)
_SETTINGS_ENV_NAMES = ("TURING_ANTHROPIC_API_KEY",)


@pytest.fixture(autouse=True)
def isolated_settings_environment(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Run every test in this package against an empty settings environment.

    ``_env_file=None`` disables the dotenv *file*; it does nothing about the
    process environment, and the key this backend reads is the same one the
    operator's own assistant needs exported. Without this the settings tests
    pass on CI and fail on the machine the code was written on, which is the
    worst of the two orders to discover it in.
    """
    for name in list(os.environ):
        if name in _SETTINGS_ENV_NAMES or name.startswith(_SETTINGS_ENV_PREFIXES):
            monkeypatch.delenv(name, raising=False)
    yield


@dataclass
class FakeUsage:
    """Stands in for the SDK's usage object, including the cache fields."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0


@dataclass
class FakeTextBlock:
    text: str
    type: str = "text"


@dataclass
class FakeToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: str = "tool_use"


@dataclass
class FakeUnknownBlock:
    """A block the backend drops: reasoning, provider-side tool use, or a type
    that did not exist when the parser was written."""

    type: str = "thinking"


@dataclass
class FakeMessage:
    """Stands in for the SDK's response object."""

    content: list[Any] = field(default_factory=list)
    stop_reason: str = "end_turn"
    model: str = "test-model"
    usage: FakeUsage | None = None


class FakeStatusError(Exception):
    """An SDK-shaped error: classified by its ``status_code`` attribute."""

    def __init__(self, status_code: int, message: str = "boom") -> None:
        super().__init__(message)
        self.status_code = status_code


class FakeConnectionError(Exception):
    """An SDK-shaped transport error: classified by its class name."""


class FakeMessagesResource:
    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("fake SDK client ran out of scripted responses")
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class FakeAnthropicClient:
    """Minimal stand-in exposing only ``messages.create`` and ``close``."""

    def __init__(self, responses: list[Any] | None = None) -> None:
        self.messages = FakeMessagesResource(responses or [])
        self.closed = False

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def policy() -> TieringPolicy:
    return TieringPolicy(
        orchestrator_model="test-orchestrator",
        substep_model="test-substep",
        orchestrator_max_tokens=4000,
        substep_max_tokens=200,
    )


@pytest.fixture
def request_factory() -> Callable[..., GenerationRequest]:
    def _make(
        *,
        tier: ModelTier = ModelTier.ORCHESTRATOR,
        text: str = "solve the problem",
        **kwargs: Any,
    ) -> GenerationRequest:
        return GenerationRequest(tier=tier, messages=(ModelMessage.user(text),), **kwargs)

    return _make


async def noop_sleep(_delay: float) -> None:
    """Retry back-off that costs no test time."""
    return None
