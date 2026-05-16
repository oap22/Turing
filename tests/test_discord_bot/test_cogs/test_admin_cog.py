"""Tests for ``turing.discord_bot.cogs.admin.AdminCog``."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from turing.discord_bot.cogs.admin import AdminCog

# ---------------------------------------------------------------------------
# cog_check (admin gate)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cog_check_allows_admin(make_bot, make_context) -> None:
    bot = make_bot(admin_ids=(1,))
    cog = AdminCog(bot)
    ctx = make_context(bot=bot, author_id=1)
    assert await cog.cog_check(ctx) is True
    ctx.reply.assert_not_called()


@pytest.mark.asyncio
async def test_cog_check_denies_non_admin_and_replies(make_bot, make_context) -> None:
    bot = make_bot(admin_ids=(1,))
    cog = AdminCog(bot)
    ctx = make_context(bot=bot, author_id=999)
    assert await cog.cog_check(ctx) is False
    ctx.reply.assert_awaited_once()
    assert "permission" in ctx.reply.await_args.args[0].lower()


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_status_reports_node_info(make_bot, make_context) -> None:
    mesh = SimpleNamespace(peers={"node-2": {"status": "alive"}})
    bot = make_bot(mesh_node=mesh)
    cog = AdminCog(bot)
    ctx = make_context(bot=bot)

    await cog.status_command.callback(cog, ctx)

    ctx.reply.assert_awaited_once()
    body = ctx.reply.await_args.args[0]
    assert "test-node" in body
    assert "Mesh Peers" in body


@pytest.mark.asyncio
async def test_status_handles_psutil_exception(monkeypatch, make_bot, make_context) -> None:
    bot = make_bot()
    cog = AdminCog(bot)
    ctx = make_context(bot=bot)
    import turing.discord_bot.cogs.admin as admin_mod

    monkeypatch.setattr(
        admin_mod.psutil,
        "virtual_memory",
        lambda: (_ for _ in ()).throw(RuntimeError("fail")),
    )

    await cog.status_command.callback(cog, ctx)

    ctx.reply.assert_awaited_once()
    assert "failed" in ctx.reply.await_args.args[0].lower()


# ---------------------------------------------------------------------------
# peers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_peers_without_mesh(make_bot, make_context) -> None:
    bot = make_bot(mesh_node=None)
    cog = AdminCog(bot)
    ctx = make_context(bot=bot)

    await cog.peers_command.callback(cog, ctx)

    ctx.reply.assert_awaited_once()
    assert "not enabled" in ctx.reply.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_peers_with_empty_mesh(make_bot, make_context) -> None:
    bot = make_bot(mesh_node=SimpleNamespace(peers={}))
    cog = AdminCog(bot)
    ctx = make_context(bot=bot)

    await cog.peers_command.callback(cog, ctx)

    ctx.reply.assert_awaited_once()
    assert "no mesh peers" in ctx.reply.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_peers_lists_connected_peers(make_bot, make_context) -> None:
    mesh = SimpleNamespace(peers={"node-2": {"status": "alive"}, "node-3": "ok"})
    bot = make_bot(mesh_node=mesh)
    cog = AdminCog(bot)
    ctx = make_context(bot=bot)

    await cog.peers_command.callback(cog, ctx)

    body = ctx.reply.await_args.args[0]
    assert "node-2" in body and "node-3" in body


# ---------------------------------------------------------------------------
# memory
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_rejects_unknown_action(make_bot, make_context) -> None:
    bot = make_bot(memory_store=SimpleNamespace())
    cog = AdminCog(bot)
    ctx = make_context(bot=bot)

    await cog.memory_command.callback(cog, ctx, "delete", query="x")

    ctx.reply.assert_awaited_once()
    assert "usage" in ctx.reply.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_memory_without_store(make_bot, make_context) -> None:
    bot = make_bot(memory_store=None)
    cog = AdminCog(bot)
    ctx = make_context(bot=bot)

    await cog.memory_command.callback(cog, ctx, "search", query="x")

    ctx.reply.assert_awaited_once()
    assert "not available" in ctx.reply.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_memory_no_results(make_bot, make_context) -> None:
    store = SimpleNamespace(search_facts=AsyncMock(return_value=[]))
    bot = make_bot(memory_store=store)
    cog = AdminCog(bot)
    ctx = make_context(bot=bot)

    await cog.memory_command.callback(cog, ctx, "search", query="x")

    assert "no memories" in ctx.reply.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_memory_returns_results(make_bot, make_context) -> None:
    store = SimpleNamespace(
        search_facts=AsyncMock(return_value=[{"text": "alice likes tea"}, "raw fact"])
    )
    bot = make_bot(memory_store=store)
    cog = AdminCog(bot)
    ctx = make_context(bot=bot)

    await cog.memory_command.callback(cog, ctx, "search", query="alice")

    body = ctx.reply.await_args.args[0]
    assert "alice likes tea" in body
    assert "raw fact" in body


# ---------------------------------------------------------------------------
# forget
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forget_without_store(make_bot, make_context) -> None:
    bot = make_bot(memory_store=None)
    cog = AdminCog(bot)
    ctx = make_context(bot=bot)

    await cog.forget_command.callback(cog, ctx, topic="tea")
    assert "not available" in ctx.reply.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_forget_no_results(make_bot, make_context) -> None:
    store = SimpleNamespace(search_facts=AsyncMock(return_value=[]))
    bot = make_bot(memory_store=store)
    cog = AdminCog(bot)
    ctx = make_context(bot=bot)

    await cog.forget_command.callback(cog, ctx, topic="tea")
    assert "no memories" in ctx.reply.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_forget_calls_forget_method(make_bot, make_context) -> None:
    forget_mock = AsyncMock()
    store = SimpleNamespace(
        search_facts=AsyncMock(return_value=[{"text": "x"}]),
        forget=forget_mock,
    )
    bot = make_bot(memory_store=store)
    cog = AdminCog(bot)
    ctx = make_context(bot=bot)

    await cog.forget_command.callback(cog, ctx, topic="tea")

    forget_mock.assert_awaited_once_with("tea")
    assert "removed" in ctx.reply.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_forget_store_without_delete_support(make_bot, make_context) -> None:
    store = SimpleNamespace(search_facts=AsyncMock(return_value=[{"text": "x"}]))
    bot = make_bot(memory_store=store)
    cog = AdminCog(bot)
    ctx = make_context(bot=bot)

    await cog.forget_command.callback(cog, ctx, topic="tea")
    assert "does not support deletion" in ctx.reply.await_args.args[0].lower()


# ---------------------------------------------------------------------------
# config
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_config_redacts_secrets(make_bot, make_context) -> None:
    bot = make_bot()
    cog = AdminCog(bot)
    ctx = make_context(bot=bot)

    await cog.config_command.callback(cog, ctx)

    body = ctx.reply.await_args.args[0]
    # Secrets must not appear in clear, replaced by asterisks.
    assert "test-discord-token" not in body
    assert "test-anthropic-key" not in body
    assert "********" in body
    # Non-secret fields are visible.
    assert "test-node" in body
