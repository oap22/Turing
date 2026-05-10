"""Regression tests for TuringBot.on_message mention handling.

Covers bug #141: bot silently dropped ~2/3 of @-mentions because
(a) detection missed role/reply mentions and post-reconnect self.user
    drift, and (b) only `<@id>` / `<@!id>` mention shapes were stripped,
and (c) silent early-returns produced no log line for forensics.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from turing.discord_bot.bot import _strip_bot_mentions


class TestStripBotMentions:
    def test_strips_plain_mention(self) -> None:
        assert _strip_bot_mentions("<@123> hello", 123) == "hello"

    def test_strips_nickname_mention(self) -> None:
        assert _strip_bot_mentions("<@!123> hello", 123) == "hello"

    def test_strips_multiple_occurrences(self) -> None:
        assert _strip_bot_mentions("<@123> hi <@!123>", 123) == "hi"

    def test_leaves_other_user_mentions_intact(self) -> None:
        assert _strip_bot_mentions("<@999> ping <@123>", 123) == "<@999> ping"

    def test_leaves_role_mentions_intact(self) -> None:
        assert _strip_bot_mentions("<@&555> hi <@123>", 123) == "<@&555> hi"

    def test_no_user_id_returns_input(self) -> None:
        assert _strip_bot_mentions("<@123> hi", None) == "<@123> hi"


@pytest.mark.asyncio
async def test_on_message_logs_message_received_before_filters(
    mock_config, monkeypatch
) -> None:
    """Every message that reaches on_message must produce a `bot.message_received`
    log entry before any early-return path, so silent drops are observable."""
    from turing.discord_bot.bot import TuringBot

    bot = TuringBot(mock_config)
    captured: list[tuple[str, dict]] = []

    def fake_debug(event: str, **kw) -> None:
        captured.append((event, kw))

    bot.logger = MagicMock()
    bot.logger.debug = fake_debug
    bot.logger.info = lambda *a, **k: None
    bot.logger.warning = lambda *a, **k: None

    # author is a bot -> should still log, then early-return
    msg = MagicMock()
    msg.author.bot = True
    msg.channel.id = 1
    msg.author.id = 2
    msg.content = "hi"
    msg.mentions = []

    monkeypatch.setattr(bot, "process_commands", AsyncMock())
    await bot.on_message(msg)

    assert any(evt == "bot.message_received" for evt, _ in captured), (
        f"expected bot.message_received log, got {captured}"
    )


@pytest.mark.asyncio
async def test_on_message_uses_mentioned_in_for_detection(
    mock_config, monkeypatch
) -> None:
    """Detection must use `self.user.mentioned_in(message)` so role mentions
    and reply auto-pings count, not just `self.user in message.mentions`."""
    from turing.discord_bot.bot import TuringBot

    bot = TuringBot(mock_config)
    bot.logger = MagicMock()

    bot_user = MagicMock()
    bot_user.id = 42
    bot_user.mentioned_in = MagicMock(return_value=True)
    monkeypatch.setattr(type(bot), "user", property(lambda self: bot_user))

    agent = MagicMock()
    agent.handle_message = AsyncMock(return_value="pong")
    bot.agent = agent

    msg = MagicMock()
    msg.author.bot = False
    msg.author.id = 7
    msg.author.display_name = "alice"
    msg.channel.id = 1
    msg.channel.__class__ = MagicMock  # not a DMChannel
    msg.content = "<@42> hello"
    msg.mentions = []  # simulate role-mention case: user not in .mentions
    msg.reply = AsyncMock()

    # typing() context manager
    typing_cm = MagicMock()
    typing_cm.__aenter__ = AsyncMock()
    typing_cm.__aexit__ = AsyncMock()
    msg.channel.typing = MagicMock(return_value=typing_cm)

    monkeypatch.setattr(bot, "process_commands", AsyncMock())
    await bot.on_message(msg)

    bot_user.mentioned_in.assert_called_once_with(msg)
    agent.handle_message.assert_awaited_once()
    assert agent.handle_message.await_args.kwargs["message"] == "hello"


@pytest.mark.asyncio
async def test_on_message_logs_when_dropping_empty_content(
    mock_config, monkeypatch
) -> None:
    """When a mention has no text after stripping, the drop must be logged
    rather than silently returning."""
    from turing.discord_bot.bot import TuringBot

    bot = TuringBot(mock_config)
    captured: list[tuple[str, dict]] = []
    bot.logger = MagicMock()
    bot.logger.debug = lambda evt, **kw: captured.append((evt, kw))
    bot.logger.info = lambda *a, **k: None

    bot_user = MagicMock()
    bot_user.id = 42
    bot_user.mentioned_in = MagicMock(return_value=True)
    monkeypatch.setattr(type(bot), "user", property(lambda self: bot_user))

    bot.agent = MagicMock()

    msg = MagicMock()
    msg.author.bot = False
    msg.author.id = 7
    msg.channel.id = 1
    msg.content = "<@42>"  # mention-only
    msg.mentions = [bot_user]

    monkeypatch.setattr(bot, "process_commands", AsyncMock())
    await bot.on_message(msg)

    assert any(evt == "bot.message_dropped" for evt, _ in captured), (
        f"expected bot.message_dropped log, got {captured}"
    )
