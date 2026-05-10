"""Discord UI views for interactive actions."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

import discord

if TYPE_CHECKING:
    from turing.coordinator.training_approval.proposal import TrainingJobProposal


class ConfirmActionView(discord.ui.View):
    """Confirmation buttons for risky actions.

    Presents Approve / Deny buttons and waits for the authorized user
    to interact.  If no response is received within ``timeout`` seconds
    the action is treated as denied.
    """

    def __init__(
        self,
        action_description: str,
        authorized_user_id: int,
        timeout: float = 60.0,
    ):
        super().__init__(timeout=timeout)
        self.action_description = action_description
        self.authorized_user_id = authorized_user_id
        self.result: bool | None = None
        self._event = asyncio.Event()

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.green, emoji="\u2705")
    async def approve(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        """Handle an approval click."""
        if interaction.user.id != self.authorized_user_id:
            await interaction.response.send_message(
                "Only the requesting user can approve.",
                ephemeral=True,
            )
            return
        self.result = True
        self._event.set()
        await interaction.response.edit_message(
            content=f"\u2705 **Approved**: {self.action_description}",
            view=None,
        )
        self.stop()

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.red, emoji="\u274c")
    async def deny(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        """Handle a denial click."""
        if interaction.user.id != self.authorized_user_id:
            await interaction.response.send_message(
                "Only the requesting user can deny.",
                ephemeral=True,
            )
            return
        self.result = False
        self._event.set()
        await interaction.response.edit_message(
            content=f"\u274c **Denied**: {self.action_description}",
            view=None,
        )
        self.stop()

    async def on_timeout(self) -> None:
        """Treat timeout as denial."""
        self.result = False
        self._event.set()

    async def wait_for_result(self) -> bool:
        """Block until the user responds or the view times out.

        Returns ``True`` if approved, ``False`` otherwise.
        """
        await self._event.wait()
        return self.result or False


class TrainingJobApprovalView(discord.ui.View):
    """Approve / Reject buttons for a proposed training job.

    Mirrors :class:`ConfirmActionView`'s 60-second-default-timeout pattern so
    the operator's mental model is the same. The bound :class:`TrainingJobProposal`
    is exposed via ``self.proposal`` so the surrounding cog can render the
    proposal details into the message body.
    """

    def __init__(
        self,
        *,
        proposal: TrainingJobProposal,
        authorized_user_id: int,
        timeout: float = 60.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.proposal = proposal
        self.authorized_user_id = authorized_user_id
        self.result: bool | None = None
        self._event = asyncio.Event()

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.green, emoji="✅")
    async def approve(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.user.id != self.authorized_user_id:
            await interaction.response.send_message(
                "Only the requesting operator can approve.", ephemeral=True
            )
            return
        self.result = True
        self._event.set()
        await interaction.response.edit_message(
            content=(
                f"✅ **Training approved**: {self.proposal.specialty} "
                f"({self.proposal.method.upper()}, "
                f"~${self.proposal.estimated_cost_usd:.2f})"
            ),
            view=None,
        )
        self.stop()

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.red, emoji="❌")
    async def reject(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if interaction.user.id != self.authorized_user_id:
            await interaction.response.send_message(
                "Only the requesting operator can reject.", ephemeral=True
            )
            return
        self.result = False
        self._event.set()
        await interaction.response.edit_message(
            content=f"❌ **Training rejected**: {self.proposal.specialty}",
            view=None,
        )
        self.stop()

    async def on_timeout(self) -> None:
        self.result = False
        self._event.set()

    async def wait_for_result(self) -> bool:
        await self._event.wait()
        return self.result or False
