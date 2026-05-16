"""Shared fixtures for discord_bot cog tests.

Cogs are tested by direct invocation: we instantiate the cog with a mock
TuringBot, build a mock ``commands.Context``, and call the underlying
command callback directly. This avoids any live Discord gateway connection.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest


class _Typing:
    """Async-context-manager stand-in for ``ctx.typing()``."""

    async def __aenter__(self) -> _Typing:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def make_ctx(
    *,
    bot: object,
    author_id: int = 1,
    channel_id: int = 100,
    display_name: str = "alice",
) -> MagicMock:
    """Build a mock ``commands.Context`` with an async ``reply``."""
    ctx = MagicMock()
    ctx.bot = bot
    ctx.author = SimpleNamespace(id=author_id, display_name=display_name, bot=False)
    ctx.channel = SimpleNamespace(id=channel_id)
    ctx.reply = AsyncMock()
    ctx.typing = MagicMock(return_value=_Typing())
    return ctx


class _ConfigProxy:
    """Wrap a real TuringConfig but allow attribute overrides.

    The admin cog reads ``bot.config.discord_admin_ids`` and dumps the
    config via ``bot.config.model_dump()``; this proxy supports both.
    """

    def __init__(self, base, **overrides: object) -> None:
        self._base = base
        self._overrides = overrides

    def __getattr__(self, name: str) -> object:
        if name in self._overrides:
            return self._overrides[name]
        return getattr(self._base, name)

    def model_dump(self) -> dict[str, object]:
        data = self._base.model_dump()
        data.update(self._overrides)
        return data


@pytest.fixture()
def make_bot(mock_config):
    """Return a factory that builds a mock TuringBot with overridable parts."""

    def _factory(
        *,
        agent: object | None = None,
        mesh_node: object | None = None,
        memory_store: object | None = None,
        admin_ids: tuple[int, ...] = (1,),
    ) -> SimpleNamespace:
        cfg = _ConfigProxy(mock_config, discord_admin_ids=list(admin_ids))
        return SimpleNamespace(
            config=cfg,
            agent=agent,
            mesh_node=mesh_node,
            memory_store=memory_store,
        )

    return _factory


@pytest.fixture()
def make_context():
    """Expose ``make_ctx`` to tests."""
    return make_ctx


@pytest.fixture()
def make_async_mock():
    """Convenience for building AsyncMock returning a given value."""

    def _factory(return_value: object = None) -> AsyncMock:
        return AsyncMock(return_value=return_value)

    return _factory
