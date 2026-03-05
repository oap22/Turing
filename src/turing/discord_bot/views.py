"""Discord UI views for interactive actions."""

from __future__ import annotations

import asyncio

import discord


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
