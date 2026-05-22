"""Tests for DiscordAlertClient — the best-effort Discord DM fallback.

The contract under test: ``dm_operator`` never raises. Every failure mode
— no operator configured, no bot, bot not ready, ``fetch_user`` raising, the
DM send raising — is swallowed so a Discord hiccup cannot perturb the alert
engine.
"""

from __future__ import annotations

import pytest

from turing.coordinator.alerts.discord_client import DiscordAlertClient


class _FakeUser:
    def __init__(self, *, send_raises: bool = False) -> None:
        self.sent: list[str] = []
        self._send_raises = send_raises

    async def send(self, content: str) -> None:
        if self._send_raises:
            raise RuntimeError("cannot send messages to this user")
        self.sent.append(content)


class _FakeBot:
    def __init__(
        self,
        *,
        ready: bool = True,
        user: _FakeUser | None = None,
        fetch_raises: bool = False,
        ready_raises: bool = False,
    ) -> None:
        self._ready = ready
        self._ready_raises = ready_raises
        self._user = user or _FakeUser()
        self._fetch_raises = fetch_raises
        self.fetch_calls: list[int] = []

    def is_ready(self) -> bool:
        if self._ready_raises:
            raise RuntimeError("bot in a bad state")
        return self._ready

    async def fetch_user(self, user_id: int) -> _FakeUser:
        self.fetch_calls.append(user_id)
        if self._fetch_raises:
            raise RuntimeError("unknown user")
        return self._user


@pytest.mark.asyncio
async def test_happy_path_dms_the_operator() -> None:
    user = _FakeUser()
    bot = _FakeBot(user=user)
    client = DiscordAlertClient(bot, operator_id=4242)
    await client.dm_operator("⚠ pi-beta TEMP danger: 87.4°C (>82.0°C)")
    assert bot.fetch_calls == [4242]
    assert user.sent == ["⚠ pi-beta TEMP danger: 87.4°C (>82.0°C)"]


@pytest.mark.asyncio
async def test_operator_id_none_is_a_silent_noop() -> None:
    """Config gate: an unset operator id disables the fallback entirely —
    the bot is never even consulted."""
    bot = _FakeBot()
    client = DiscordAlertClient(bot, operator_id=None)
    await client.dm_operator("⚠ anything")
    assert bot.fetch_calls == []


@pytest.mark.asyncio
async def test_bot_none_is_tolerated() -> None:
    client = DiscordAlertClient(None, operator_id=4242)
    await client.dm_operator("⚠ anything")  # must not raise


@pytest.mark.asyncio
async def test_bot_not_ready_drops_without_fetch() -> None:
    bot = _FakeBot(ready=False)
    client = DiscordAlertClient(bot, operator_id=4242)
    await client.dm_operator("⚠ anything")
    assert bot.fetch_calls == []


@pytest.mark.asyncio
async def test_is_ready_raising_is_treated_as_not_ready() -> None:
    bot = _FakeBot(ready_raises=True)
    client = DiscordAlertClient(bot, operator_id=4242)
    await client.dm_operator("⚠ anything")
    assert bot.fetch_calls == []


@pytest.mark.asyncio
async def test_fetch_user_raising_is_swallowed() -> None:
    bot = _FakeBot(fetch_raises=True)
    client = DiscordAlertClient(bot, operator_id=4242)
    await client.dm_operator("⚠ anything")  # must not raise


@pytest.mark.asyncio
async def test_send_raising_is_swallowed() -> None:
    bot = _FakeBot(user=_FakeUser(send_raises=True))
    client = DiscordAlertClient(bot, operator_id=4242)
    await client.dm_operator("⚠ anything")  # must not raise
