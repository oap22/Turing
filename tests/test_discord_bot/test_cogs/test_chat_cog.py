"""Tests for ``turing.discord_bot.cogs.chat.ChatCog``."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from turing.discord_bot.cogs.chat import ChatCog


@pytest.mark.asyncio
async def test_ask_replies_with_agent_response(make_bot, make_context) -> None:
    agent = AsyncMock()
    agent.handle_message = AsyncMock(return_value="hi there")
    bot = make_bot(agent=agent)
    cog = ChatCog(bot)
    ctx = make_context(bot=bot)

    await cog.ask_command.callback(cog, ctx, question="hello?")

    agent.handle_message.assert_awaited_once()
    kwargs = agent.handle_message.await_args.kwargs
    assert kwargs["message"] == "hello?"
    assert kwargs["channel_id"] == "100"
    assert kwargs["user_id"] == "1"
    ctx.reply.assert_awaited()
    # Reply was called with the (single) chunk of the response.
    assert ctx.reply.await_args_list[0].args[0] == "hi there"


@pytest.mark.asyncio
async def test_ask_when_agent_missing_replies_with_warning(make_bot, make_context) -> None:
    bot = make_bot(agent=None)
    cog = ChatCog(bot)
    ctx = make_context(bot=bot)

    await cog.ask_command.callback(cog, ctx, question="hello?")

    ctx.reply.assert_awaited_once()
    assert "not initialized" in ctx.reply.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_ask_handles_agent_exception(make_bot, make_context) -> None:
    agent = AsyncMock()
    agent.handle_message = AsyncMock(side_effect=RuntimeError("boom"))
    bot = make_bot(agent=agent)
    cog = ChatCog(bot)
    ctx = make_context(bot=bot)

    await cog.ask_command.callback(cog, ctx, question="hi")

    ctx.reply.assert_awaited_once()
    assert "error" in ctx.reply.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_think_prefixes_step_by_step_instruction(make_bot, make_context) -> None:
    agent = AsyncMock()
    agent.handle_message = AsyncMock(return_value="reasoned answer")
    bot = make_bot(agent=agent)
    cog = ChatCog(bot)
    ctx = make_context(bot=bot)

    await cog.think_command.callback(cog, ctx, question="why?")

    sent = agent.handle_message.await_args.kwargs["message"]
    assert "step by step" in sent.lower()
    assert "why?" in sent
    ctx.reply.assert_awaited()


@pytest.mark.asyncio
async def test_think_when_agent_missing(make_bot, make_context) -> None:
    bot = make_bot(agent=None)
    cog = ChatCog(bot)
    ctx = make_context(bot=bot)

    await cog.think_command.callback(cog, ctx, question="why?")

    ctx.reply.assert_awaited_once()
    assert "not initialized" in ctx.reply.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_think_handles_agent_exception(make_bot, make_context) -> None:
    agent = AsyncMock()
    agent.handle_message = AsyncMock(side_effect=RuntimeError("boom"))
    bot = make_bot(agent=agent)
    cog = ChatCog(bot)
    ctx = make_context(bot=bot)

    await cog.think_command.callback(cog, ctx, question="why?")

    ctx.reply.assert_awaited_once()
    assert "error" in ctx.reply.await_args.args[0].lower()
