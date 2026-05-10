"""Tests for Discord UI views (ConfirmActionView)."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from turing.discord_bot.views import ConfirmActionView

# ----------------------------------------------------------------------
# Helpers -- lightweight mocks for discord.Interaction
# ----------------------------------------------------------------------


def _make_interaction(user_id: int) -> MagicMock:
    """Create a mock ``discord.Interaction`` with the given user ID."""
    interaction = MagicMock()
    interaction.user = MagicMock()
    interaction.user.id = user_id
    interaction.response = MagicMock()
    interaction.response.send_message = AsyncMock()
    interaction.response.edit_message = AsyncMock()
    return interaction


async def _invoke_approve(view: ConfirmActionView, interaction: MagicMock) -> None:
    """Invoke the approve callback the way discord.py does internally."""
    await view.approve.callback(interaction)


async def _invoke_deny(view: ConfirmActionView, interaction: MagicMock) -> None:
    """Invoke the deny callback the way discord.py does internally."""
    await view.deny.callback(interaction)


# ----------------------------------------------------------------------
# Tests
# ----------------------------------------------------------------------


class TestConfirmActionView:
    """Tests for the ConfirmActionView confirmation dialog."""

    @pytest.fixture()
    def view(self) -> ConfirmActionView:
        """Create a view authorized for user 12345."""
        return ConfirmActionView(
            action_description="Delete all logs",
            authorized_user_id=12345,
            timeout=5.0,
        )

    async def test_approve_sets_result_true(self, view: ConfirmActionView) -> None:
        """Clicking Approve sets result to True and edits the message."""
        interaction = _make_interaction(user_id=12345)

        await _invoke_approve(view, interaction)

        assert view.result is True
        interaction.response.edit_message.assert_awaited_once()
        call_kwargs = interaction.response.edit_message.call_args.kwargs
        assert "Approved" in call_kwargs["content"]
        assert call_kwargs["view"] is None

    async def test_deny_sets_result_false(self, view: ConfirmActionView) -> None:
        """Clicking Deny sets result to False and edits the message."""
        interaction = _make_interaction(user_id=12345)

        await _invoke_deny(view, interaction)

        assert view.result is False
        interaction.response.edit_message.assert_awaited_once()
        call_kwargs = interaction.response.edit_message.call_args.kwargs
        assert "Denied" in call_kwargs["content"]
        assert call_kwargs["view"] is None

    async def test_unauthorized_user_cannot_approve(self, view: ConfirmActionView) -> None:
        """A user who is not authorized receives an ephemeral rejection."""
        interaction = _make_interaction(user_id=99999)

        await _invoke_approve(view, interaction)

        # Result should remain None (unchanged)
        assert view.result is None
        interaction.response.send_message.assert_awaited_once()
        call_kwargs = interaction.response.send_message.call_args
        assert "Only the requesting user" in call_kwargs.args[0]
        assert call_kwargs.kwargs.get("ephemeral") is True

    async def test_unauthorized_user_cannot_deny(self, view: ConfirmActionView) -> None:
        """A user who is not authorized cannot deny either."""
        interaction = _make_interaction(user_id=99999)

        await _invoke_deny(view, interaction)

        assert view.result is None
        interaction.response.send_message.assert_awaited_once()
        call_kwargs = interaction.response.send_message.call_args
        assert "Only the requesting user" in call_kwargs.args[0]
        assert call_kwargs.kwargs.get("ephemeral") is True

    async def test_timeout_results_in_deny(self, view: ConfirmActionView) -> None:
        """When the view times out, result should be False."""
        await view.on_timeout()
        assert view.result is False

    async def test_wait_for_result_after_approve(self, view: ConfirmActionView) -> None:
        """wait_for_result returns True after approval."""
        interaction = _make_interaction(user_id=12345)

        async def do_approve() -> None:
            await asyncio.sleep(0.05)
            await _invoke_approve(view, interaction)

        task = asyncio.create_task(do_approve())
        result = await view.wait_for_result()
        assert result is True
        await task

    async def test_wait_for_result_after_timeout(self, view: ConfirmActionView) -> None:
        """wait_for_result returns False after timeout."""

        async def do_timeout() -> None:
            await asyncio.sleep(0.05)
            await view.on_timeout()

        task = asyncio.create_task(do_timeout())
        result = await view.wait_for_result()
        assert result is False
        await task

    async def test_action_description_in_approve_message(self) -> None:
        """The action description appears in the approval confirmation."""
        view = ConfirmActionView(
            action_description="Restart the server",
            authorized_user_id=42,
        )
        interaction = _make_interaction(user_id=42)

        await _invoke_approve(view, interaction)

        call_kwargs = interaction.response.edit_message.call_args.kwargs
        assert "Restart the server" in call_kwargs["content"]

    async def test_action_description_in_deny_message(self) -> None:
        """The action description appears in the denial confirmation."""
        view = ConfirmActionView(
            action_description="Format disk",
            authorized_user_id=42,
        )
        interaction = _make_interaction(user_id=42)

        await _invoke_deny(view, interaction)

        call_kwargs = interaction.response.edit_message.call_args.kwargs
        assert "Format disk" in call_kwargs["content"]
